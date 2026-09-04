"""
agent/resume.py — crash recovery. Reads ONE register (op.state for the
given operation_id), switches on its `kind`, and continues from exactly
that point. No replaying history and inferring position — the register
IS the complete current state, by construction (agent/durable.py commits
it as such after every step).

Note on the crash-point granularity actually observable in this system:
"before a risky effect started" and "died partway through it" are
INDISTINGUISHABLE from storage's perspective, by design — durable.py
never commits anything during the effect itself, only immediately before
(INTENT) and immediately after (SETTLEMENT). This collapses what might
look like 5 distinct crash scenarios (before model call / mid-stream /
before tool exec / mid-tool-exec / after tool exec but before settling)
into really 3 distinguishable RECOVERABLE STATES: checkpoint,
awaiting_model, awaiting_tool. That collapse is the whole point of the
design — recovery logic doesn't need to know or care exactly when the
crash happened, only what was last durably true. See
test_resumability.py for the actual proof.
"""

from __future__ import annotations

from agent.durable import LANE, _append_entry, _drive_turns, _set_state
from agent.loop import _dispatch_one_tool_call, _now_ms
from agent.persistence import build_history, payload_to_message
from agent.types import Message, TextContent, ToolCallContent, ToolResultMessage
from ai.model import Model, Provider
from storage.base import Storage
from tools.base import ToolContext
from tools.registry import ToolRegistry


async def resume(
    storage: Storage,
    operation_id: str,
    system_prompt: str,
    model: Model,
    provider: Provider,
    tools: ToolRegistry,
    api_key: str,
    tool_context: ToolContext,
    max_turns: int = 20,
) -> list[Message]:
    state_reg = await storage.get_register("op.state", operation_id)
    if state_reg is None:
        # Either this operation already finished cleanly (terminal cleanup
        # already deleted its state) or the operation_id is simply wrong.
        # Either way there's nothing to resume — return current history so
        # the caller can inspect what's there.
        return await build_history(storage)

    state = state_reg.value
    kind = state["kind"]

    if kind == "done":
        return await build_history(storage)

    if kind == "checkpoint":
        # Nothing was in flight. Just continue driving turns — the next
        # one will be a fresh model call.
        return await _drive_turns(
            storage, operation_id, system_prompt, model, provider, tools, api_key, tool_context, max_turns,
        )

    if kind == "awaiting_model":
        # Model calls are inherently safe to retry: worst case is a
        # duplicate billed request, never corrupted conversation state,
        # because no response entry was ever committed for this attempt
        # (SETTLEMENT never landed). Reset to checkpoint and just let the
        # normal turn loop issue a fresh call — it's indistinguishable
        # from a call that never started.
        await _set_state(storage, operation_id, {"kind": "checkpoint"})
        return await _drive_turns(
            storage, operation_id, system_prompt, model, provider, tools, api_key, tool_context, max_turns,
        )

    if kind == "awaiting_tool":
        return await _resume_awaiting_tool(
            storage, operation_id, state, system_prompt, model, provider, tools, api_key, tool_context, max_turns,
        )

    raise ValueError(f"Unknown OperationState kind on resume: {kind!r}")


async def _resume_awaiting_tool(
    storage: Storage,
    operation_id: str,
    state: dict,
    system_prompt: str,
    model: Model,
    provider: Provider,
    tools: ToolRegistry,
    api_key: str,
    tool_context: ToolContext,

    max_turns: int,
) -> list[Message]:
    pending_ids: list[str] = state["pending_tool_call_ids"]
    reserved_result_ids: dict[str, str] = state["reserved_result_ids"]
    tool_replay_safety: dict[str, str] = state["tool_replay_safety"]
    assistant_message_id = state.get("assistant_message_id")

    if not pending_ids:
        # Every call in this batch was already completed before the
        # crash — just advance to checkpoint and keep going.
        await _set_state(storage, operation_id, {"kind": "checkpoint"})
        return await _drive_turns(
            storage, operation_id, system_prompt, model, provider, tools, api_key, tool_context, max_turns,
        )

    if assistant_message_id is None:
        raise RuntimeError(
            "Cannot resume: awaiting_tool state is missing assistant_message_id "
            "and has pending calls. This should never happen — durable.py always "
            "records it when calls are still pending."
        )
    entries = await storage.get_entries([assistant_message_id])
    assistant_entry = entries.get(assistant_message_id)
    if assistant_entry is None:
        raise RuntimeError(f"Cannot resume: assistant entry '{assistant_message_id}' not found in storage.")
    assistant_message = payload_to_message(assistant_entry.payload)
    calls_by_id = {c.id: c for c in assistant_message.content if isinstance(c, ToolCallContent)}
    pending_calls = [calls_by_id[cid] for cid in pending_ids]

    leaf_reg = await storage.get_register("lane.leaf", LANE)
    leaf_id = leaf_reg.value if leaf_reg else None

    leaf_id = await _resume_tool_batch(
        storage, operation_id, pending_calls, reserved_result_ids, tool_replay_safety,
        tools, tool_context, leaf_id, assistant_message_id,
    )

    await _set_state(storage, operation_id, {"kind": "checkpoint"})
    return await _drive_turns(
        storage, operation_id, system_prompt, model, provider, tools, api_key, tool_context, max_turns,
    )


async def _resume_tool_batch(
    storage: Storage,
    operation_id: str,
    pending_calls: list[ToolCallContent],
    reserved_result_ids: dict[str, str],
    tool_replay_safety: dict[str, str],
    tools: ToolRegistry,
    tool_context: ToolContext,

    leaf_id: str | None,
    assistant_message_id: str,
) -> str | None:
    """
    The recovery-specific version of tool-batch execution: for each still-
    pending call, consult its declared replay_safety BEFORE deciding
    whether to actually run it. This is the one place the "safe" vs
    "unsafe" distinction in ToolSpec.replay_safety actually matters —
    everywhere else it's just metadata sent to the model.
    """
    completed: list[str] = []
    pending: list[str] = [c.id for c in pending_calls]

    for call in pending_calls:
        safety = tool_replay_safety.get(call.id, "unsafe")

        if safety == "safe":
            tool = tools.get(call.name)
            if tool is None:
                result_msg = ToolResultMessage(
                    tool_call_id=call.id, tool_name=call.name,
                    content=[TextContent(text=f"Tool '{call.name}' not found on resume.")],
                    details=None, is_error=True, timestamp=_now_ms(),
                )
            else:
                result_msg = await _dispatch_one_tool_call(call, tools, tool_context)
        else:
            # UNSAFE: never re-run, regardless of whether it actually
            # completed before the crash or never started. The correct,
            # conservative move is the same either way — synthesize an
            # explicit "unknown outcome" result rather than guess.
            result_msg = ToolResultMessage(
                tool_call_id=call.id,
                tool_name=call.name,
                content=[TextContent(text=(
                    f"Execution of '{call.name}' was interrupted by a crash and was NOT "
                    f"retried, because it is declared replay_safety='unsafe'. Its actual "
                    f"outcome on disk/system state is unknown — verify manually before "
                    f"assuming it did or did not happen."
                ))],
                details={"interrupted": True},
                is_error=True,
                timestamp=_now_ms(),
            )

        result_entry_id = reserved_result_ids[call.id]
        leaf_id = await _append_entry(storage, leaf_id, result_msg)

        pending.remove(call.id)
        completed.append(call.id)
        await _set_state(storage, operation_id, {
            "kind": "awaiting_tool",
            "assistant_message_id": assistant_message_id,
            "pending_tool_call_ids": pending,
            "completed_tool_call_ids": completed,
            "reserved_result_ids": reserved_result_ids,
            "tool_replay_safety": tool_replay_safety,
        })

    return leaf_id
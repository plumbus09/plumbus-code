"""
agent/durable.py — the durable wrapper around agent/loop.py's building
blocks. This is Phase 6: wrap the (already correct, already tested)
in-memory step functions with commits before and after every risky
effect, so a crash at ANY point leaves a resumable, non-corrupted state.

Reuses _stream_assistant_response and _dispatch_one_tool_call from
agent/loop.py directly — the model-call and single-tool-call logic
doesn't change at all. Only the orchestration around them changes: every
risky step is now preceded by an INTENT commit and followed by a
SETTLEMENT commit, per the effect-sandwich pattern.

One operation = one full run_durable() call, start to finish (a "run",
per the harness-doc research) — NOT one operation per turn. Turns happen
inside one operation; "checkpoint" is the state between turns within that
one operation, not a boundary between operations.
"""

from __future__ import annotations

import uuid

from agent.loop import _dispatch_one_tool_call, _error_tool_result, _now_ms, _stream_assistant_response
from agent.persistence import build_history, message_to_payload
from agent.types import Message, TextContent, ToolCallContent, UserMessage
from ai.model import Model, Provider
from storage.base import (
    DeleteRegisterWrite,
    InsertEntryWrite,
    NewEntry,
    SetRegisterWrite,
    Storage,
    Transaction,
)
from tools.base import ToolContext
from tools.registry import ToolRegistry

LANE = "main"  # the only lane that exists — see ARCHITECTURE.md's exclusions table


async def _leaf(storage: Storage) -> str | None:
    reg = await storage.get_register("lane.leaf", LANE)
    return reg.value if reg else None


async def _append_entry(storage: Storage, leaf_id: str | None, message: Message) -> str:
    """Insert one message as an entry, advance the lane leaf, return the new entry id."""
    entry_id = str(uuid.uuid4())
    await storage.commit(Transaction(writes=[
        InsertEntryWrite(entry=NewEntry(id=entry_id, parent_id=leaf_id, type="message", payload=message_to_payload(message))),
        SetRegisterWrite(namespace="lane.leaf", key=LANE, value=entry_id),
    ]))
    return entry_id


async def _set_state(storage: Storage, operation_id: str, state: dict) -> None:
    await storage.commit(Transaction(writes=[
        SetRegisterWrite(namespace="op.state", key=operation_id, value=state),
    ]))


async def _terminal_cleanup(storage: Storage, operation_id: str, outcome: dict) -> None:
    """
    Mirrors the harness doc's terminal transaction: delete op.meta and
    op.state, write lane.lastResult. Nothing about the conversation itself
    (entries) is touched — only the operation's own bookkeeping.
    """
    await storage.commit(Transaction(writes=[
        DeleteRegisterWrite(namespace="op.state", key=operation_id),
        DeleteRegisterWrite(namespace="op.meta", key=operation_id),
        SetRegisterWrite(namespace="lane.lastResult", key=LANE, value=outcome),
    ]))


# ---------------------------------------------------------------------------
# Fresh start
# ---------------------------------------------------------------------------
async def run_durable(
    storage: Storage,
    prompt_text: str,
    system_prompt: str,
    model: Model,
    provider: Provider,
    tools: ToolRegistry,
    api_key: str,
    tool_context: ToolContext,
    max_turns: int = 20,
) -> list[Message]:
    operation_id = str(uuid.uuid4())
    leaf_id = await _leaf(storage)

    prompt_message = UserMessage(content=[TextContent(text=prompt_text)], timestamp=_now_ms())
    prompt_entry_id = str(uuid.uuid4())
    await storage.commit(Transaction(writes=[
        InsertEntryWrite(entry=NewEntry(id=prompt_entry_id, parent_id=leaf_id, type="message", payload=message_to_payload(prompt_message))),
        SetRegisterWrite(namespace="lane.leaf", key=LANE, value=prompt_entry_id),
        SetRegisterWrite(namespace="op.meta", key=operation_id, value={"started_at": _now_ms()}),
        SetRegisterWrite(namespace="op.state", key=operation_id, value={"kind": "checkpoint"}),
    ]))

    return await _drive_turns(
        storage, operation_id, system_prompt, model, provider, tools, api_key, tool_context, max_turns,
    )


# ---------------------------------------------------------------------------
# The turn-driving loop — used by both a fresh run and by resume() once
# it's back at "checkpoint" (nothing in flight).
# ---------------------------------------------------------------------------
async def _drive_turns(
    storage: Storage,
    operation_id: str,
    system_prompt: str,
    model: Model,
    provider: Provider,
    tools: ToolRegistry,
    api_key: str,
    tool_context: ToolContext,

    max_turns: int,
) -> list[Message]:
    for _ in range(max_turns):
        history = await build_history(storage)
        leaf_id = await _leaf(storage)

        # --- INTENT: about to call the model.
        response_entry_id = str(uuid.uuid4())
        await _set_state(storage, operation_id, {"kind": "awaiting_model", "reserved_response_id": response_entry_id})

        # --- EFFECT: the one genuinely non-durable window.
        assistant_message = await _stream_assistant_response(history, system_prompt, model, provider, tools, api_key)

        # --- SETTLEMENT: the response lands under the id we already reserved.
        await storage.commit(Transaction(writes=[
            InsertEntryWrite(entry=NewEntry(id=response_entry_id, parent_id=leaf_id, type="message", payload=message_to_payload(assistant_message))),
            SetRegisterWrite(namespace="lane.leaf", key=LANE, value=response_entry_id),
        ]))
        leaf_id = response_entry_id

        if assistant_message.stop_reason in ("error", "aborted"):
            await _terminal_cleanup(storage, operation_id, {"operation_id": operation_id, "outcome": assistant_message.stop_reason})
            return await build_history(storage)

        tool_calls = [c for c in assistant_message.content if isinstance(c, ToolCallContent)]
        if not tool_calls:
            await _terminal_cleanup(storage, operation_id, {"operation_id": operation_id, "outcome": "completed"})
            return await build_history(storage)

        truncated = assistant_message.stop_reason == "max_tokens"
        reserved_result_ids = {c.id: str(uuid.uuid4()) for c in tool_calls}
        tool_replay_safety: dict[str, str] = {}
        for c in tool_calls:
            tool = tools.get(c.name)
            # Unknown tool -> treated as "unsafe": never guess re-run
            # safety for something we can't even look up.
            tool_replay_safety[c.id] = tool.replay_safety if tool else "unsafe"

        # --- INTENT: about to run this batch of tool calls.
        await _set_state(storage, operation_id, {
            "kind": "awaiting_tool",
            "assistant_message_id": response_entry_id,
            "pending_tool_call_ids": [c.id for c in tool_calls],
            "completed_tool_call_ids": [],
            "reserved_result_ids": reserved_result_ids,
            "tool_replay_safety": tool_replay_safety,
        })

        leaf_id = await _run_tool_batch(
            storage, operation_id, tool_calls, reserved_result_ids, tool_replay_safety,
            tools, tool_context, leaf_id, truncated, response_entry_id,
        )

        await _set_state(storage, operation_id, {"kind": "checkpoint"})

    return await build_history(storage)


async def _run_tool_batch(
    storage: Storage,
    operation_id: str,
    tool_calls: list[ToolCallContent],
    reserved_result_ids: dict[str, str],
    tool_replay_safety: dict[str, str],
    tools: ToolRegistry,
    tool_context: ToolContext,

    leaf_id: str | None,
    truncated: bool,
    assistant_message_id: str,
) -> str | None:
    """
    Executes each tool call in order, committing SETTLEMENT after each one
    individually. Returns the new leaf id after the whole batch.

    FIX: assistant_message_id is threaded through every state write in this
    batch, not just the first — a crash after call 1 settles but before
    call 2 starts must still be able to look up the original assistant
    message to reconstruct call 2's ToolCallContent on resume. Dropping it
    to None partway through the batch (an earlier version of this
    function did exactly that) would silently break resume for any batch
    with more than one tool call.
    """
    pending = [c.id for c in tool_calls]
    completed: list[str] = []
    calls_by_id = {c.id: c for c in tool_calls}

    for call_id in list(pending):
        call = calls_by_id[call_id]
        if truncated:
            result_msg_stub = _error_tool_result(
                call,
                f'Tool call "{call.name}" was not executed: the response hit the output '
                f"token limit, so its arguments may be truncated. Re-issue with complete arguments.",
            )
        else:
            result_msg_stub = await _dispatch_one_tool_call(call, tools, tool_context)

        result_entry_id = reserved_result_ids[call_id]
        leaf_id = await _append_entry(storage, leaf_id, result_msg_stub)

        pending.remove(call_id)
        completed.append(call_id)
        await _set_state(storage, operation_id, {
            "kind": "awaiting_tool",
            "assistant_message_id": assistant_message_id,
            "pending_tool_call_ids": pending,
            "completed_tool_call_ids": completed,
            "reserved_result_ids": reserved_result_ids,
            "tool_replay_safety": tool_replay_safety,
        })

    return leaf_id
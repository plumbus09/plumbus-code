"""
agent/loop.py — the step function and incremental event stream.
Translated from pi's real packages/agent/src/agent-loop.ts.

No persistence here. No I/O beyond calling the Provider and the Tools it's
given. This file works entirely in memory — agent/durable.py wraps this
for durability; this loop doesn't know storage exists.

Two contracts this file depends on and never violates:
  1. Provider.stream() never raises (ai/model.py) — so there is no
     try/except around the model call anywhere below.
  2. Tool.execute() IS allowed to raise (tools/base.py) — so every tool
     call below IS wrapped in try/except, and a raised exception becomes
     an error ToolResult, never propagates out of the loop.

_stream_assistant_response is extracted as a standalone function
(factored out of run_loop_stream's inlined model-call logic) specifically
so agent/durable.py can reuse the exact same model-call code without
duplicating it — durable.py needs a plain "call the model once, get the
final message back" primitive, not an event stream, since it commits its
own INTENT/SETTLEMENT transactions around the call itself.
"""

from __future__ import annotations

import asyncio
import time
from typing import AsyncGenerator

from agent.types import (
    AgentAssistantMessage,
    AgentEvent,
    AgentLoopAborted,
    AgentLoopDone,
    AgentTextDelta,
    AgentToolCallStarted,
    AgentToolExecutionDone,
    AgentToolExecutionStarted,
    AgentToolUpdate,
    AgentTurnDone,
    AgentTurnStart,
    AssistantMessage,
    Message,
    TextContent,
    ToolCallContent,
    ToolResult,
    ToolResultMessage,
    UserMessage,
)
from ai.model import Context, Model, Provider, StreamOptions
from tools.base import ToolContext
from tools.permissions import PermissionPolicy
from tools.registry import ToolRegistry


def _now_ms() -> int:
    return int(time.time() * 1000)


def _is_cancelled(tool_context: ToolContext) -> bool:
    if tool_context.cancel is not None:
        if hasattr(tool_context.cancel, "is_set"):
            return tool_context.cancel.is_set()
        if callable(tool_context.cancel):
            return tool_context.cancel()
        return bool(tool_context.cancel)
    return False


# ---------------------------------------------------------------------------
# The model-call boundary, as a standalone reusable primitive.
# ---------------------------------------------------------------------------
async def _stream_assistant_response(
    history: list[Message],
    system_prompt: str,
    model: Model,
    provider: Provider,
    tools: ToolRegistry,
    api_key: str,
) -> AssistantMessage:
    """
    No try/except here. Provider.stream() is contractually guaranteed to
    never raise for request/model/runtime failures — see ai/model.py's
    Provider docstring. If you ever find yourself adding a try/except
    around this call, the bug is in the provider, not here.
    """
    context = Context(system_prompt=system_prompt, messages=history, tools=tools.specs())
    options = StreamOptions(api_key=api_key)

    final_message: AssistantMessage | None = None
    async for event in provider.stream(model, context, options):
        if event.type == "done":
            final_message = event.message

    assert final_message is not None, "Provider.stream() ended without a 'done' event — contract violation"
    return final_message


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------
async def run_loop_stream(
    prompt_text: str,
    messages: list[Message],
    system_prompt: str,
    model: Model,
    provider: Provider,
    tools: ToolRegistry,
    api_key: str,
    tool_context: ToolContext,
    max_turns: int = 20,
) -> AsyncGenerator[AgentEvent, None]:
    """
    Runs a full agent turn-sequence, yielding incremental AgentEvent objects.
    """
    prompt = UserMessage(content=[TextContent(text=prompt_text)], timestamp=_now_ms())
    history = [*messages, prompt]
    new_messages: list[Message] = [prompt]
    final_stop_reason = "end_turn"

    try:
        for turn_idx in range(max_turns):
            if _is_cancelled(tool_context):
                yield AgentLoopAborted(reason="interrupted_by_user")
                return

            yield AgentTurnStart(turn=turn_idx)

            assistant_message: AssistantMessage | None = None
            options = StreamOptions(api_key=api_key, cancel=tool_context.cancel)
            context = Context(system_prompt=system_prompt, messages=history, tools=tools.specs())

            async for event in provider.stream(model, context, options):
                if _is_cancelled(tool_context):
                    yield AgentLoopAborted(reason="interrupted_by_user")
                    return
                if event.type == "text_delta":
                    yield AgentTextDelta(delta=event.delta)
                elif event.type == "done":
                    assistant_message = event.message

            assert (
                assistant_message is not None
            ), "Provider.stream() ended without a 'done' event — contract violation"

            history = [*history, assistant_message]
            new_messages.append(assistant_message)
            yield AgentAssistantMessage(message=assistant_message)
            final_stop_reason = assistant_message.stop_reason

            if assistant_message.stop_reason in ("error", "aborted"):
                yield AgentTurnDone(turn=turn_idx)
                break

            tool_calls = [
                c for c in assistant_message.content if isinstance(c, ToolCallContent)
            ]
            if not tool_calls:
                yield AgentTurnDone(turn=turn_idx)
                break

            for tc in tool_calls:
                yield AgentToolCallStarted(
                    tool_call_id=tc.id, name=tc.name, arguments=tc.arguments
                )

            if assistant_message.stop_reason == "max_tokens":
                results = [
                    _error_tool_result(
                        tc,
                        f'Tool call "{tc.name}" was not executed: the response hit '
                        f"the output token limit, so its arguments may be truncated. "
                        f"Re-issue the tool call with complete arguments.",
                    )
                    for tc in tool_calls
                ]
                for tc, r in zip(tool_calls, results):
                    yield AgentToolExecutionDone(
                        tool_call_id=tc.id, name=tc.name, result_message=r
                    )
            else:
                results = []
                for call in tool_calls:
                    if _is_cancelled(tool_context):
                        yield AgentLoopAborted(reason="interrupted_by_user")
                        return

                    yield AgentToolExecutionStarted(tool_call_id=call.id, name=call.name)

                    async for event_or_result in _dispatch_one_tool_call_stream(
                        call, tools, tool_context
                    ):
                        if isinstance(event_or_result, AgentToolUpdate):
                            yield event_or_result
                        elif isinstance(event_or_result, ToolResultMessage):
                            results.append(event_or_result)
                            yield AgentToolExecutionDone(
                                tool_call_id=call.id,
                                name=call.name,
                                result_message=event_or_result,
                            )

            history = [*history, *results]
            new_messages.extend(results)

            yield AgentTurnDone(turn=turn_idx)

            if results and all(r.details == "__terminate__" for r in results):
                break

        yield AgentLoopDone(messages=new_messages, stop_reason=final_stop_reason)

    except GeneratorExit:
        return
    except (asyncio.CancelledError, KeyboardInterrupt):
        yield AgentLoopAborted(reason="interrupted_by_user")


async def run_loop(
    prompt_text: str,
    messages: list[Message],
    system_prompt: str,
    model: Model,
    provider: Provider,
    tools: ToolRegistry,
    api_key: str,
    tool_context: ToolContext,
    max_turns: int = 20,
) -> list[Message]:
    """
    Runs a full agent turn-sequence starting from a new user prompt.
    Wrapper around run_loop_stream for backward compatibility.
    """
    new_messages: list[Message] = []
    gen = run_loop_stream(
        prompt_text=prompt_text,
        messages=messages,
        system_prompt=system_prompt,
        model=model,
        provider=provider,
        tools=tools,
        api_key=api_key,
        tool_context=tool_context,
        max_turns=max_turns,
    )
    try:
        async for event in gen:
            if isinstance(event, AgentLoopDone):
                return event.messages
            if isinstance(event, AgentAssistantMessage):
                if event.message not in new_messages:
                    new_messages.append(event.message)
            elif isinstance(event, AgentToolExecutionDone):
                if event.result_message not in new_messages:
                    new_messages.append(event.result_message)
    finally:
        await gen.aclose()
    return new_messages


# ---------------------------------------------------------------------------
# Tool dispatch: prepare -> execute -> finalize
# ---------------------------------------------------------------------------
async def _dispatch_one_tool_call_stream(
    call: ToolCallContent,
    tools: ToolRegistry,
    tool_context: ToolContext,
) -> AsyncGenerator[AgentToolUpdate | ToolResultMessage, None]:
    try:
        # --- prepare -----------------------------------------------------------
        tool = tools.get(call.name)
        if tool is None:
            yield _error_tool_result(call, f"Tool '{call.name}' not found.")
            return

        try:
            args = tool.prepare_arguments(call.arguments)
        except Exception as exc:  # noqa: BLE001
            yield _error_tool_result(call, f"Failed to prepare arguments: {exc}")
            return

        # Permission gate check
        policy: PermissionPolicy = tool_context.policy or PermissionPolicy()
        action = policy.evaluate(tool, args)

        if action == "deny":
            yield _error_tool_result(
                call,
                f"Tool execution denied by permission policy: '{call.name}' is forbidden or matched a denied command pattern.",
            )
            return

        if action == "ask":
            if tool_context.confirm is None:
                yield _error_tool_result(
                    call,
                    f"Tool execution for '{call.name}' requires user confirmation, but no confirmation callback was provided.",
                )
                return
            prompt_text = f"Allow execution of tool '{call.name}' with arguments {args}?"
            try:
                confirmed = await tool_context.confirm(prompt_text)
            except Exception as exc:  # noqa: BLE001
                yield _error_tool_result(call, f"Confirmation callback failed: {exc}")
                return

            if not confirmed:
                yield _error_tool_result(
                    call,
                    f"Tool execution for '{call.name}' denied by user confirmation.",
                )
                return

        # --- execute -------------------------------------------------------
        tool_updates: list[AgentToolUpdate] = []

        def on_update_callback(update_result: ToolResult) -> None:
            tool_updates.append(
                AgentToolUpdate(tool_call_id=call.id, name=call.name, result=update_result)
            )

        try:
            tool_result = await tool.execute(call.id, args, tool_context, on_update=on_update_callback)
            for upd in tool_updates:
                yield upd
            is_error = False
        except Exception as exc:  # noqa: BLE001
            yield _error_tool_result(call, str(exc))
            return

        # --- finalize --------------------------------------------------------
        yield ToolResultMessage(
            tool_call_id=call.id,
            tool_name=call.name,
            content=tool_result.content,
            details=tool_result.details,
            is_error=is_error,
            timestamp=_now_ms(),
        )
    except GeneratorExit:
        return


async def _dispatch_one_tool_call(
    call: ToolCallContent,
    tools: ToolRegistry,
    tool_context: ToolContext,
) -> ToolResultMessage:
    gen = _dispatch_one_tool_call_stream(call, tools, tool_context)
    try:
        async for res in gen:
            if isinstance(res, ToolResultMessage):
                return res
    finally:
        await gen.aclose()
    return _error_tool_result(call, "Tool execution completed without returning a result.")


def _error_tool_result(call: ToolCallContent, message: str) -> ToolResultMessage:
    return ToolResultMessage(
        tool_call_id=call.id,
        tool_name=call.name,
        content=[TextContent(text=message)],
        details=None,
        is_error=True,
        timestamp=_now_ms(),
    )
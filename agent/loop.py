
"""
agent/loop.py — the step function. Translated from pi's real
packages/agent/src/agent-loop.ts (runLoop / executeToolCallsSequential /
prepareToolCall / executePreparedToolCall / finalizeExecutedToolCall).
 
No persistence here. No I/O beyond calling the Provider and the Tools it's
given. This file works entirely in memory — Phase 5 (storage) wraps THIS
loop for durability later; this loop doesn't know storage exists.
 
Two contracts this file depends on and never violates:
  1. Provider.stream() never raises (ai/model.py) — so there is no
     try/except around the model call anywhere below.
  2. Tool.execute() IS allowed to raise (tools/base.py) — so every tool
     call below IS wrapped in try/except, and a raised exception becomes
     an error ToolResult, never propagates out of the loop.
"""
 
from __future__ import annotations
 
import time
import uuid
from typing import Any
 
from agent.types import (
    AssistantMessage,
    Message,
    TextContent,
    ToolCallContent,
    ToolResultMessage,
    UserMessage,
)
from ai.model import Context, Model, Provider, StreamOptions
from tools.base import ToolContext
from tools.registry import ToolRegistry
 
 
def _now_ms() -> int:
    return int(time.time() * 1000)
 
 
# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------
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
 
    Returns the list of NEW messages produced (prompt included) — mirrors
    pi's runAgentLoop return shape. `messages` is the existing history;
    this function does not mutate it in place, it returns a fresh combined
    list via the caller appending, matching the "never mutate a message,
    only append" rule from Phase 0.
    """
    prompt = UserMessage(content=[TextContent(text=prompt_text)], timestamp=_now_ms())
    history = [*messages, prompt]
    new_messages: list[Message] = [prompt]
 
    for _ in range(max_turns):
        assistant_message = await _stream_assistant_response(
            history, system_prompt, model, provider, tools, api_key
        )
        history = [*history, assistant_message]
        new_messages.append(assistant_message)
 
        if assistant_message.stop_reason in ("error", "aborted"):
            # Terminal per the Provider contract — nothing more to do.
            # The caller inspects assistant_message.error_message.
            break
 
        tool_calls = [c for c in assistant_message.content if isinstance(c, ToolCallContent)]
        if not tool_calls:
            # No tool calls requested -> the model is done for this turn.
            break
 
        if assistant_message.stop_reason == "max_tokens":
            # Truncation guard, direct port of pi's
            # failToolCallsFromTruncatedMessage: arguments may be silently
            # incomplete even though they parsed and validated. Never
            # execute them.
            results = [
                _error_tool_result(
                    tc,
                    f'Tool call "{tc.name}" was not executed: the response hit '
                    f"the output token limit, so its arguments may be truncated. "
                    f"Re-issue the tool call with complete arguments.",
                )
                for tc in tool_calls
            ]
        else:
            results = await _execute_tool_calls(tool_calls, tools, tool_context)
 
        history = [*history, *results]
        new_messages.extend(results)
 
        if results and all(r.details == "__terminate__" for r in results):
            # Batch-wide AND termination hint — see note in _execute_tool_calls
            # about how ToolResult.terminate is threaded through here.
            break
 
    return new_messages
 
 
# ---------------------------------------------------------------------------
# Model call boundary
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
# Tool dispatch: prepare -> execute -> finalize, sequential only (v1)
# ---------------------------------------------------------------------------
async def _execute_tool_calls(
    tool_calls: list[ToolCallContent],
    tools: ToolRegistry,
    tool_context: ToolContext,
) -> list[ToolResultMessage]:
    results: list[ToolResultMessage] = []
    for call in tool_calls:
        result = await _dispatch_one_tool_call(call, tools, tool_context)
        results.append(result)
        # No early-exit on error: one failing tool call shouldn't cancel
        # the rest of the batch. The model sees each result independently
        # and decides how to proceed next turn.
    return results
 
 
async def _dispatch_one_tool_call(
    call: ToolCallContent,
    tools: ToolRegistry,
    tool_context: ToolContext,
) -> ToolResultMessage:
    # --- prepare -----------------------------------------------------------
    tool = tools.get(call.name)
    if tool is None:
        return _error_tool_result(call, f"Tool '{call.name}' not found.")
 
    try:
        args = tool.prepare_arguments(call.arguments)
        # Real JSON-schema validation belongs here once you add a validator
        # (e.g. jsonschema) — deliberately skipped for now; add it the
        # first time a model sends you malformed arguments, not before.
    except Exception as exc:  # noqa: BLE001 — preparation failures are data, like pi's prepareToolCall.
        return _error_tool_result(call, f"Failed to prepare arguments: {exc}")
 
    # Permission gate goes here in Phase 4 (tool_context.confirm(...)).
    # For now every prepared call proceeds straight to execution.
 
    # --- execute -------------------------------------------------------
    try:
        tool_result = await tool.execute(call.id, args, tool_context)
        is_error = False
    except Exception as exc:  # noqa: BLE001 — this is the ONE place a tool's raise is allowed to be caught.
        return _error_tool_result(call, str(exc))
 
    # --- finalize --------------------------------------------------------
    # Hook point for an afterToolCall-equivalent override lives here in a
    # later phase. For now, pass the tool's result straight through.
    return ToolResultMessage(
        tool_call_id=call.id,
        tool_name=call.name,
        content=tool_result.content,
        details=tool_result.details,
        is_error=is_error,
        timestamp=_now_ms(),
    )
 
 
def _error_tool_result(call: ToolCallContent, message: str) -> ToolResultMessage:
    return ToolResultMessage(
        tool_call_id=call.id,
        tool_name=call.name,
        content=[TextContent(text=message)],
        details=None,
        is_error=True,
        timestamp=_now_ms(),
    )
 
 
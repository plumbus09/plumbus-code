"""
Smoke test for agent/loop.py — a scripted FakeProvider stands in for
OpenRouter, so this proves the loop's control flow (tool dispatch,
truncation guard, termination) without spending a real API call or
needing network access.
"""

import asyncio

from agent.types import AssistantMessage, TextContent, ToolCallContent, ToolResult
from ai.model import Context, Model, StreamDone, StreamOptions
from agent.loop import run_loop
from tools.base import Tool, ToolContext
from tools.registry import ToolRegistry


class ReadFileTool(Tool):
    name = "read_file"
    label = "Read a file"
    parameters_schema = {"type": "object", "properties": {"path": {"type": "string"}}}
    replay_safety = "safe"

    async def execute(self, tool_call_id, arguments, context, on_update=None):
        return ToolResult(content=[TextContent(text=f"contents of {arguments['path']}: hello world")])


class FakeProvider:
    """
    Scripted responses, one per call to stream(). Turn 1: request a tool
    call. Turn 2: read the tool result and answer with plain text.
    """

    def __init__(self):
        self.call_count = 0

    async def stream(self, model: Model, context: Context, options: StreamOptions):
        self.call_count += 1
        if self.call_count == 1:
            msg = AssistantMessage(
                content=[ToolCallContent(id="call_1", name="read_file", arguments={"path": "a.txt"})],
                stop_reason="tool_use",
                timestamp=0,
            )
        else:
            # Confirm the tool result actually made it into context.
            last = context.messages[-1]
            assert last.role == "tool_result", context.messages
            assert "hello world" in last.content[0].text
            msg = AssistantMessage(
                content=[TextContent(text="The file says hello world.")],
                stop_reason="end_turn",
                timestamp=0,
            )
        yield StreamDone(message=msg)


class TruncatedProvider:
    """Always returns a tool call with stop_reason='max_tokens'."""

    async def stream(self, model: Model, context: Context, options: StreamOptions):
        msg = AssistantMessage(
            content=[ToolCallContent(id="call_1", name="read_file", arguments={"path": "a.txt"})],
            stop_reason="max_tokens",
            timestamp=0,
        )
        yield StreamDone(message=msg)


async def test_tool_calling_round_trip():
    registry = ToolRegistry()
    registry.register(ReadFileTool())
    ctx = ToolContext(cwd="/tmp")

    messages = await run_loop(
        prompt_text="what's in a.txt?",
        messages=[],
        system_prompt="You are helpful.",
        model=Model(id="test/model", provider="fake", name="fake", context_window=8000),
        provider=FakeProvider(),
        tools=registry,
        api_key="unused",
        tool_context=ctx,
    )

    roles = [m.role for m in messages]
    assert roles == ["user", "assistant", "tool_result", "assistant"], roles
    assert messages[-1].content[0].text == "The file says hello world."
    print("PASS: tool-calling round trip ->", [m.role for m in messages])


async def test_unknown_tool_becomes_error_result_not_exception():
    registry = ToolRegistry()  # no tools registered
    ctx = ToolContext(cwd="/tmp")

    class OneShotToolCallProvider:
        async def stream(self, model, context, options):
            msg = AssistantMessage(
                content=[ToolCallContent(id="c1", name="read_file", arguments={"path": "a.txt"})],
                stop_reason="tool_use",
                timestamp=0,
            )
            yield StreamDone(message=msg)

    messages = await run_loop(
        prompt_text="read a.txt",
        messages=[],
        system_prompt="sys",
        model=Model(id="test/model", provider="fake", name="fake", context_window=8000),
        provider=OneShotToolCallProvider(),
        tools=registry,
        api_key="unused",
        tool_context=ctx,
        max_turns=1,
    )
    tool_result = next(m for m in messages if m.role == "tool_result")
    assert tool_result.is_error is True
    assert "not found" in tool_result.content[0].text
    print("PASS: unknown tool -> error ToolResult, no exception raised ->", tool_result.content[0].text)


async def test_truncated_message_blocks_tool_execution():
    registry = ToolRegistry()
    registry.register(ReadFileTool())
    ctx = ToolContext(cwd="/tmp")

    messages = await run_loop(
        prompt_text="read a.txt",
        messages=[],
        system_prompt="sys",
        model=Model(id="test/model", provider="fake", name="fake", context_window=8000),
        provider=TruncatedProvider(),
        tools=registry,
        api_key="unused",
        tool_context=ctx,
        max_turns=1,
    )
    tool_result = next(m for m in messages if m.role == "tool_result")
    assert tool_result.is_error is True
    assert "output token limit" in tool_result.content[0].text
    print("PASS: truncated message -> tool NOT executed, error result instead")


async def test_exploding_tool_becomes_error_result_not_exception():
    class ExplodingTool(Tool):
        name = "explode"
        label = "fails"
        parameters_schema = {"type": "object", "properties": {}}
        replay_safety = "unsafe"

        async def execute(self, tool_call_id, arguments, context, on_update=None):
            raise RuntimeError("boom")

    registry = ToolRegistry()
    registry.register(ExplodingTool())
    ctx = ToolContext(cwd="/tmp")

    class OneShotProvider:
        async def stream(self, model, context, options):
            msg = AssistantMessage(
                content=[ToolCallContent(id="c1", name="explode", arguments={})],
                stop_reason="tool_use",
                timestamp=0,
            )
            yield StreamDone(message=msg)

    messages = await run_loop(
        prompt_text="explode",
        messages=[],
        system_prompt="sys",
        model=Model(id="test/model", provider="fake", name="fake", context_window=8000),
        provider=OneShotProvider(),
        tools=registry,
        api_key="unused",
        tool_context=ctx,
        max_turns=1,
    )
    tool_result = next(m for m in messages if m.role == "tool_result")
    assert tool_result.is_error is True
    assert "boom" in tool_result.content[0].text
    print("PASS: tool raising RuntimeError -> caught, became error ToolResult, loop did not crash")


async def main():
    await test_tool_calling_round_trip()
    await test_unknown_tool_becomes_error_result_not_exception()
    await test_truncated_message_blocks_tool_execution()
    await test_exploding_tool_becomes_error_result_not_exception()
    print("\nAll smoke tests passed.")


if __name__ == "__main__":
    asyncio.run(main())
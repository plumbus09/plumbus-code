"""
Integration test — everything wired together EXCEPT the network call:

  agent/loop.py (real)  +  tools/bash.py (real, spawns real subprocesses)
  +  FakeProvider (scripted, stands in for OpenRouterProvider since this
     sandbox can't reach openrouter.ai)

This is not a mock of your logic — BashTool actually runs `echo`, `pwd`,
etc. as real subprocesses via asyncio.create_subprocess_shell, in a real
temp directory. Only the model call itself is scripted, because that's the
one piece requiring live network access. See test_openrouter_live.py for
the version that also exercises the real OpenRouterProvider — run that one
yourself, locally, with a real API key.
"""

import asyncio
import tempfile

from agent.types import AssistantMessage, TextContent, ToolCallContent
from ai.model import Context, Model, StreamDone, StreamOptions
from agent.loop import run_loop
from tools.bash import BashTool
from tools.base import ToolContext
from tools.registry import ToolRegistry


class ScriptedProvider:
    """
    Turn 1: ask the model (scripted) to run a real bash command.
    Turn 2: scripted model reads the REAL tool result and answers from it.
    This proves the tool result that flows back from a REAL subprocess
    execution round-trips correctly through Context.messages.
    """

    def __init__(self):
        self.call_count = 0

    async def stream(self, model: Model, context: Context, options: StreamOptions):
        self.call_count += 1
        if self.call_count == 1:
            msg = AssistantMessage(
                content=[ToolCallContent(
                    id="call_1", name="bash",
                    arguments={"command": "echo 'hello from real subprocess' && exit 0"},
                )],
                stop_reason="tool_use",
                timestamp=0,
            )
        else:
            last = context.messages[-1]
            assert last.role == "tool_result", context.messages
            # This text is REAL subprocess output, not scripted — proves
            # the round trip actually happened, not just that the test
            # asserts what it expects to see.
            real_output = last.content[0].text
            msg = AssistantMessage(
                content=[TextContent(text=f"The command printed: {real_output.strip()}")],
                stop_reason="end_turn",
                timestamp=0,
            )
        yield StreamDone(message=msg)


class ScriptedNonZeroExitProvider:
    """
    Verifies the bash.py fix: non-zero exit is data, not an exception.

    Uses a file created inside the test's own tmpdir rather than a fixed
    system path like /etc/hostname — that file exists on Linux but NOT on
    macOS (macOS has no /etc/hostname; hostname is set via scutil instead),
    which is exactly what caused this test to fail with exit code 2
    ("no such file") instead of the intended exit code 1 ("no match
    found"). Always prefer paths the test itself controls over assuming
    something about the host OS.
    """

    def __init__(self):
        self.call_count = 0

    async def stream(self, model: Model, context: Context, options: StreamOptions):
        self.call_count += 1
        if self.call_count == 1:
            msg = AssistantMessage(
                content=[ToolCallContent(
                    id="call_1", name="bash",
                    # grep against a file we know exists (created by the
                    # test below), searching for a pattern we know isn't
                    # in it -> guaranteed exit code 1, portably.
                    arguments={"command": "grep 'nonexistent_pattern' needle.txt"},
                )],
                stop_reason="tool_use",
                timestamp=0,
            )
        else:
            last = context.messages[-1]
            assert last.role == "tool_result"
            # Critical assertion: this must NOT be is_error. grep exiting 1
            # (no matches found) is normal tool output, not a tool failure.
            assert last.is_error is False, f"grep exit 1 was wrongly treated as a tool error: {last}"
            msg = AssistantMessage(content=[TextContent(text="No matches found.")], stop_reason="end_turn", timestamp=0)
        yield StreamDone(message=msg)


async def auto_confirm(prompt: str) -> bool:
    return True


async def test_real_bash_tool_round_trip():
    with tempfile.TemporaryDirectory() as tmpdir:
        registry = ToolRegistry()
        registry.register(BashTool())
        ctx = ToolContext(cwd=tmpdir, confirm=auto_confirm)

        messages = await run_loop(
            prompt_text="run a command that says hello",
            messages=[],
            system_prompt="You are helpful.",
            model=Model(id="test/model", provider="fake", name="fake", context_window=8000),
            provider=ScriptedProvider(),
            tools=registry,
            api_key="unused",
            tool_context=ctx,
        )

        roles = [m.role for m in messages]
        assert roles == ["user", "assistant", "tool_result", "assistant"], roles
        tool_result = messages[2]
        assert tool_result.is_error is False
        assert "hello from real subprocess" in tool_result.content[0].text
        assert tool_result.details["exit_code"] == 0
        final = messages[-1]
        assert "hello from real subprocess" in final.content[0].text
        print("PASS: real BashTool subprocess output round-tripped through the loop correctly")
        print("      tool_result.details:", tool_result.details)


async def test_nonzero_exit_is_not_treated_as_tool_error():
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create the file ourselves so the test doesn't depend on any
        # particular file existing on the host OS.
        with open(f"{tmpdir}/needle.txt", "w") as f:
            f.write("some content with no matching pattern here\n")

        registry = ToolRegistry()
        registry.register(BashTool())
        ctx = ToolContext(cwd=tmpdir, confirm=auto_confirm)

        messages = await run_loop(
            prompt_text="check for a pattern",
            messages=[],
            system_prompt="sys",
            model=Model(id="test/model", provider="fake", name="fake", context_window=8000),
            provider=ScriptedNonZeroExitProvider(),
            tools=registry,
            api_key="unused",
            tool_context=ctx,
        )
        tool_result = next(m for m in messages if m.role == "tool_result")
        assert tool_result.is_error is False
        assert tool_result.details["exit_code"] == 1
        print("PASS: grep exit-1 (no matches) flowed through as normal data, not an error")
        print("      tool_result.details:", tool_result.details)


async def test_bad_command_still_raises_and_becomes_error_result():
    """
    Sanity check on the OTHER side of the fix: a genuinely broken tool
    execution (bad argument type) still becomes an error ToolResult via
    the loop's except-and-convert path — we didn't accidentally make
    EVERYTHING non-erroring.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        registry = ToolRegistry()
        registry.register(BashTool())
        ctx = ToolContext(cwd=tmpdir, confirm=auto_confirm)

        class BadArgsProvider:
            async def stream(self, model, context, options):
                msg = AssistantMessage(
                    content=[ToolCallContent(id="c1", name="bash", arguments={"command": ""})],
                    stop_reason="tool_use", timestamp=0,
                )
                yield StreamDone(message=msg)

        messages = await run_loop(
            prompt_text="run empty command",
            messages=[], system_prompt="sys",
            model=Model(id="test/model", provider="fake", name="fake", context_window=8000),
            provider=BadArgsProvider(),
            tools=registry, api_key="unused", tool_context=ctx, max_turns=1,
        )
        tool_result = next(m for m in messages if m.role == "tool_result")
        assert tool_result.is_error is True
        assert "non-empty string" in tool_result.content[0].text
        print("PASS: genuinely invalid arguments still raise -> caught -> real error ToolResult")


async def main():
    await test_real_bash_tool_round_trip()
    await test_nonzero_exit_is_not_treated_as_tool_error()
    await test_bad_command_still_raises_and_becomes_error_result()
    print("\nAll integration tests passed — loop + real BashTool wiring confirmed correct.")


if __name__ == "__main__":
    asyncio.run(main())
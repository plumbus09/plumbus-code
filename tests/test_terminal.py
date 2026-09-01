"""
tests/test_terminal.py — Tests for event streaming, Terminal UI rendering, diffs, confirmation, and Ctrl+C interrupt handling.
"""

import asyncio
import io
import os
import tempfile
import unittest

from rich.console import Console

from agent.loop import run_loop_stream
from agent.types import (
    AgentAssistantMessage,
    AgentLoopAborted,
    AgentLoopDone,
    AgentTextDelta,
    AgentToolExecutionDone,
    AgentTurnStart,
    AssistantMessage,
    TextContent,
    ToolCallContent,
)
from ai.model import Context, Model, StreamDone, StreamOptions, TextDelta
from terminal.confirm import TerminalConfirm
from terminal.diff import render_diff
from terminal.renderer import TerminalRenderer
from terminal.runner import CancelToken, run_terminal_session
from tools import EditTool, ReadFileTool, ToolContext, ToolRegistry, WriteFileTool


class ScriptedStreamProvider:
    """Provider yielding streamed text deltas followed by a tool call."""

    def __init__(self):
        self.call_count = 0

    async def stream(self, model: Model, context: Context, options: StreamOptions):
        self.call_count += 1
        if self.call_count == 1:
            yield TextDelta(delta="Hello! ")
            yield TextDelta(delta="Executing tool...")
            msg = AssistantMessage(
                content=[
                    TextContent(text="Hello! Executing tool..."),
                    ToolCallContent(
                        id="call_t1",
                        name="read_file",
                        arguments={"path": "test.txt"},
                    ),
                ],
                stop_reason="tool_use",
                timestamp=0,
            )
            yield StreamDone(message=msg)
        else:
            msg = AssistantMessage(
                content=[TextContent(text="Done!")],
                stop_reason="end_turn",
                timestamp=0,
            )
            yield StreamDone(message=msg)


class TestTerminalEventStream(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.cwd = self.temp_dir.name
        with open(os.path.join(self.cwd, "test.txt"), "w", encoding="utf-8") as f:
            f.write("hello world\n")

        self.registry = ToolRegistry()
        self.registry.register(ReadFileTool())
        self.registry.register(WriteFileTool())
        self.registry.register(EditTool())
        self.model = Model(id="test/model", provider="fake", name="fake", context_window=8000)
        self.tool_context = ToolContext(cwd=self.cwd)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_run_loop_stream_yields_expected_events(self):
        events = []

        async def collect():
            async for ev in run_loop_stream(
                prompt_text="read test.txt",
                messages=[],
                system_prompt="sys",
                model=self.model,
                provider=ScriptedStreamProvider(),
                tools=self.registry,
                api_key="unused",
                tool_context=self.tool_context,
                max_turns=2,
            ):
                events.append(ev)

        asyncio.run(collect())

        event_types = [type(e) for e in events]
        self.assertIn(AgentTurnStart, event_types)
        self.assertIn(AgentTextDelta, event_types)
        self.assertIn(AgentAssistantMessage, event_types)
        self.assertIn(AgentToolExecutionDone, event_types)
        self.assertIn(AgentLoopDone, event_types)

        text_deltas = [e.delta for e in events if isinstance(e, AgentTextDelta)]
        self.assertEqual(text_deltas, ["Hello! ", "Executing tool..."])

    def test_diff_rendering(self):
        diff_text = "--- a/code.py\n+++ b/code.py\n@@ -1,2 +1,2 @@\n-old line\n+new line\n"
        renderable = render_diff(diff_text, title="Edit code.py")
        console = Console(file=io.StringIO(), force_terminal=True)
        console.print(renderable)
        output = console.file.getvalue()
        self.assertIn("Edit code.py", output)
        self.assertIn("old line", output)
        self.assertIn("new line", output)

    def test_terminal_confirm_auto_approve(self):
        confirm = TerminalConfirm(auto_approve=True)
        res = asyncio.run(confirm.confirm("Write file created.txt?"))
        self.assertTrue(res)

    def test_ctrl_c_clean_interrupt_handling(self):
        cancel_token = CancelToken()

        class SlowStreamProvider:
            async def stream(self, model: Model, context: Context, options: StreamOptions):
                yield TextDelta(delta="Starting...")
                # Trigger Ctrl+C cancel signal mid-stream
                cancel_token.set()
                msg = AssistantMessage(
                    content=[TextContent(text="Starting...")],
                    stop_reason="end_turn",
                    timestamp=0,
                )
                yield StreamDone(message=msg)

        ctx = ToolContext(cwd=self.cwd, cancel=cancel_token)
        events = []

        async def collect():
            async for ev in run_loop_stream(
                prompt_text="slow turn",
                messages=[],
                system_prompt="sys",
                model=self.model,
                provider=SlowStreamProvider(),
                tools=self.registry,
                api_key="unused",
                tool_context=ctx,
                max_turns=5,
            ):
                events.append(ev)

        asyncio.run(collect())

        abort_events = [e for e in events if isinstance(e, AgentLoopAborted)]
        self.assertEqual(len(abort_events), 1)
        self.assertEqual(abort_events[0].reason, "interrupted_by_user")

    def test_run_terminal_session_end_to_end(self):
        buf = io.StringIO()
        console = Console(file=buf, force_terminal=True)

        messages = asyncio.run(
            run_terminal_session(
                prompt_text="read test.txt",
                messages=[],
                system_prompt="sys",
                model=self.model,
                provider=ScriptedStreamProvider(),
                tools=self.registry,
                api_key="unused",
                cwd=self.cwd,
                console=console,
                auto_approve=True,
                max_turns=2,
            )
        )

        output = buf.getvalue()
        self.assertIn("Turn", output)
        self.assertIn("Hello! Executing tool...", output)
        self.assertIn("Tool Result (read_file)", output)
        self.assertTrue(len(messages) >= 2)


if __name__ == "__main__":
    unittest.main()

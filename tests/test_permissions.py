"""
tests/test_permissions.py — Unit & e2e tests for permission gate & command deny-list.
"""

import asyncio
import os
from pathlib import Path
import sys
import tempfile
import unittest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.loop import run_loop
from agent.types import AssistantMessage, ToolCallContent
from ai.model import Context, Model, StreamDone, StreamOptions
from tools import (
    BashTool,
    PermissionPolicy,
    ReadFileTool,
    ToolContext,
    ToolRegistry,
    WriteFileTool,
)


class TestPermissionPolicy(unittest.TestCase):
    def setUp(self):
        self.policy = PermissionPolicy()
        self.read_tool = ReadFileTool()
        self.write_tool = WriteFileTool()
        self.bash_tool = BashTool()

    def test_default_tool_policies(self):
        self.assertEqual(self.policy.evaluate(self.read_tool, {"path": "foo.txt"}), "auto")
        self.assertEqual(self.policy.evaluate(self.write_tool, {"path": "foo.txt", "content": "hi"}), "ask")
        self.assertEqual(self.policy.evaluate(self.bash_tool, {"command": "ls -la"}), "ask")

    def test_bash_deny_list_patterns(self):
        dangerous_commands = [
            "rm -rf /",
            "rm -rf ./tmp",
            "rm -f dangerous.txt",
            "rm --force all",
            "sudo rm -rf /etc",
            "su root",
            "chmod -R 777 /",
            "mkfs.ext4 /dev/sda1",
            "dd if=/dev/zero of=/dev/sda",
        ]
        for cmd in dangerous_commands:
            action = self.policy.evaluate(self.bash_tool, {"command": cmd})
            self.assertEqual(action, "deny", f"Command '{cmd}' should have been denied!")

    def test_custom_tool_policy_override(self):
        custom_policy = PermissionPolicy(
            tool_policies={"bash": "deny", "write_file": "auto"}
        )
        self.assertEqual(custom_policy.evaluate(self.bash_tool, {"command": "echo hello"}), "deny")
        self.assertEqual(custom_policy.evaluate(self.write_tool, {"path": "a.txt", "content": "x"}), "auto")


class ScriptedToolCallProvider:
    """
    Scripted provider issuing a single tool call for e2e permission testing.
    """

    def __init__(self, tool_name: str, arguments: dict):
        self.tool_name = tool_name
        self.arguments = arguments
        self.call_count = 0

    async def stream(self, model: Model, context: Context, options: StreamOptions):
        self.call_count += 1
        if self.call_count == 1:
            msg = AssistantMessage(
                content=[
                    ToolCallContent(
                        id="call_perm_1",
                        name=self.tool_name,
                        arguments=self.arguments,
                    )
                ],
                stop_reason="tool_use",
                timestamp=0,
            )
        else:
            msg = AssistantMessage(
                content=[],
                stop_reason="end_turn",
                timestamp=0,
            )
        yield StreamDone(message=msg)


class TestPermissionGateEndToEnd(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.cwd = self.temp_dir.name
        self.registry = ToolRegistry()
        self.registry.register(ReadFileTool())
        self.registry.register(WriteFileTool())
        self.registry.register(BashTool())
        self.model = Model(id="test/model", provider="fake", name="fake", context_window=8000)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_auto_approved_tool_runs_without_confirmation(self):
        # Create file first
        with open(os.path.join(self.cwd, "sample.txt"), "w", encoding="utf-8") as f:
            f.write("content inside sample.txt\n")

        # Confirmation callback raises exception if called
        async def fail_if_asked(prompt: str) -> bool:
            self.fail("confirm() should not have been called for auto-approved tool!")
            return False

        ctx = ToolContext(cwd=self.cwd, confirm=fail_if_asked)
        provider = ScriptedToolCallProvider("read_file", {"path": "sample.txt"})

        messages = asyncio.run(
            run_loop(
                prompt_text="read sample file",
                messages=[],
                system_prompt="sys",
                model=self.model,
                provider=provider,
                tools=self.registry,
                api_key="unused",
                tool_context=ctx,
                max_turns=2,
            )
        )

        tool_result = next(m for m in messages if m.role == "tool_result")
        self.assertFalse(tool_result.is_error)
        self.assertIn("content inside sample.txt", tool_result.content[0].text)

    def test_unsafe_tool_approved_by_user(self):
        confirm_called = []

        async def approve(prompt: str) -> bool:
            confirm_called.append(prompt)
            return True

        ctx = ToolContext(cwd=self.cwd, confirm=approve)
        provider = ScriptedToolCallProvider("write_file", {"path": "created.txt", "content": "approved content"})

        messages = asyncio.run(
            run_loop(
                prompt_text="write file",
                messages=[],
                system_prompt="sys",
                model=self.model,
                provider=provider,
                tools=self.registry,
                api_key="unused",
                tool_context=ctx,
                max_turns=2,
            )
        )

        self.assertEqual(len(confirm_called), 1)
        tool_result = next(m for m in messages if m.role == "tool_result")
        self.assertFalse(tool_result.is_error)
        self.assertIn("Successfully created created.txt", tool_result.content[0].text)
        self.assertTrue(os.path.exists(os.path.join(self.cwd, "created.txt")))

    def test_unsafe_tool_denied_by_user(self):
        confirm_called = []

        async def deny(prompt: str) -> bool:
            confirm_called.append(prompt)
            return False

        ctx = ToolContext(cwd=self.cwd, confirm=deny)
        provider = ScriptedToolCallProvider("write_file", {"path": "forbidden.txt", "content": "blocked"})

        messages = asyncio.run(
            run_loop(
                prompt_text="write file",
                messages=[],
                system_prompt="sys",
                model=self.model,
                provider=provider,
                tools=self.registry,
                api_key="unused",
                tool_context=ctx,
                max_turns=2,
            )
        )

        self.assertEqual(len(confirm_called), 1)
        tool_result = next(m for m in messages if m.role == "tool_result")
        self.assertTrue(tool_result.is_error)
        self.assertIn("denied by user confirmation", tool_result.content[0].text)
        self.assertFalse(os.path.exists(os.path.join(self.cwd, "forbidden.txt")))

    def test_dangerous_bash_command_blocked_by_deny_list(self):
        async def fail_if_asked(prompt: str) -> bool:
            self.fail("confirm() should NOT be called for a denied command!")
            return False

        ctx = ToolContext(cwd=self.cwd, confirm=fail_if_asked)
        provider = ScriptedToolCallProvider("bash", {"command": "rm -rf /important_folder"})

        messages = asyncio.run(
            run_loop(
                prompt_text="delete everything",
                messages=[],
                system_prompt="sys",
                model=self.model,
                provider=provider,
                tools=self.registry,
                api_key="unused",
                tool_context=ctx,
                max_turns=2,
            )
        )

        tool_result = next(m for m in messages if m.role == "tool_result")
        self.assertTrue(tool_result.is_error)
        self.assertIn("denied by permission policy", tool_result.content[0].text)
        self.assertIn("matched a denied command pattern", tool_result.content[0].text)


if __name__ == "__main__":
    unittest.main()

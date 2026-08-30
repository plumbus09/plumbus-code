"""
tests/test_tools.py — Unit tests for built-in tools.
"""

import asyncio
import os
import tempfile
import unittest

from tools import (
    BashTool,
    EditTool,
    GlobTool,
    GrepTool,
    ReadFileTool,
    ToolContext,
    WriteFileTool,
    default_tools,
)


class TestTools(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.cwd = self.temp_dir.name
        self.context = ToolContext(cwd=self.cwd)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_default_tools_registry(self):
        registry = default_tools()
        specs = registry.specs()
        self.assertEqual(len(specs), 6)
        names = {s.name for s in specs}
        self.assertEqual(names, {"bash", "read_file", "write_file", "edit", "glob", "grep"})

    def test_write_and_read_file_tool(self):
        write_tool = WriteFileTool()
        read_tool = ReadFileTool()

        # Write file
        res = asyncio.run(
            write_tool.execute(
                "call-1",
                {"path": "hello.txt", "content": "line 1\nline 2\nline 3\n"},
                self.context,
            )
        )
        self.assertFalse(res.terminate)
        self.assertIn("Successfully created hello.txt", res.content[0].text)

        # Read file
        read_res = asyncio.run(
            read_tool.execute(
                "call-2",
                {"path": "hello.txt", "start_line": 1, "end_line": 2},
                self.context,
            )
        )
        self.assertIn("1: line 1", read_res.content[0].text)
        self.assertIn("2: line 2", read_res.content[0].text)
        self.assertNotIn("3: line 3", read_res.content[0].text)

    def test_read_file_not_found(self):
        read_tool = ReadFileTool()
        with self.assertRaises(FileNotFoundError):
            asyncio.run(read_tool.execute("call-err", {"path": "missing.txt"}, self.context))

    def test_edit_tool(self):
        write_tool = WriteFileTool()
        edit_tool = EditTool()

        asyncio.run(
            write_tool.execute(
                "call-w",
                {"path": "code.py", "content": "def foo():\n    return 42\n"},
                self.context,
            )
        )

        # Successful edit
        res = asyncio.run(
            edit_tool.execute(
                "call-e",
                {
                    "path": "code.py",
                    "old_string": "return 42",
                    "new_string": "return 100",
                },
                self.context,
            )
        )
        self.assertIn("Successfully replaced 1 occurrence", res.content[0].text)

        # Verify content
        with open(os.path.join(self.cwd, "code.py"), "r", encoding="utf-8") as f:
            new_code = f.read()
        self.assertIn("return 100", new_code)

        # Missing target error
        with self.assertRaises(ValueError):
            asyncio.run(
                edit_tool.execute(
                    "call-e2",
                    {
                        "path": "code.py",
                        "old_string": "nonexistent text",
                        "new_string": "bar",
                    },
                    self.context,
                )
            )

    def test_glob_tool(self):
        write_tool = WriteFileTool()
        glob_tool = GlobTool()

        asyncio.run(write_tool.execute("c1", {"path": "a/file1.py", "content": "# a"}, self.context))
        asyncio.run(write_tool.execute("c2", {"path": "b/file2.txt", "content": "# b"}, self.context))

        res = asyncio.run(glob_tool.execute("cg", {"pattern": "**/*.py"}, self.context))
        self.assertIn("file1.py", res.content[0].text)
        self.assertNotIn("file2.txt", res.content[0].text)

    def test_grep_tool(self):
        write_tool = WriteFileTool()
        grep_tool = GrepTool()

        asyncio.run(write_tool.execute("c1", {"path": "main.py", "content": "import sys\nprint('hello')"}, self.context))

        res = asyncio.run(grep_tool.execute("cgrep", {"query": "print"}, self.context))
        self.assertIn("main.py:2:print('hello')", res.content[0].text)

    def test_bash_tool(self):
        bash_tool = BashTool()
        res = asyncio.run(bash_tool.execute("cbash", {"command": "echo 'hello world'"}, self.context))
        self.assertIn("hello world", res.content[0].text)
        self.assertEqual(res.details["exit_code"], 0)

        # Non-zero exit status raises RuntimeError
        with self.assertRaises(RuntimeError):
            asyncio.run(bash_tool.execute("cbash-err", {"command": "exit 1"}, self.context))


if __name__ == "__main__":
    unittest.main()

"""
tools/bash.py — Tool for executing shell commands.
"""

from __future__ import annotations

import asyncio
from typing import Any

from agent.types import TextContent, ToolResult
from tools.base import Tool, ToolContext, ToolUpdateCallback


class BashTool(Tool):
    """
    Executes shell commands in a subshell under context.cwd.
    """

    name = "bash"
    label = "Run Bash Command"
    replay_safety = "unsafe"
    execution_mode = "sequential"

    parameters_schema = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The shell command to execute.",
            },
            "timeout": {
                "type": "integer",
                "description": "Optional timeout in seconds.",
            },
        },
        "required": ["command"],
    }

    async def execute(
        self,
        tool_call_id: str,
        arguments: dict[str, Any],
        context: ToolContext,
        on_update: ToolUpdateCallback | None = None,
    ) -> ToolResult:
        command = arguments.get("command")
        if not command or not isinstance(command, str):
            raise ValueError("Argument 'command' must be a non-empty string.")

        timeout = arguments.get("timeout")
        if timeout is not None and (not isinstance(timeout, (int, float)) or timeout <= 0):
            raise ValueError("Argument 'timeout' must be a positive number if provided.")

        try:
            process = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=context.cwd,
            )
        except Exception as exc:
            raise RuntimeError(f"Failed to spawn shell command: {exc}") from exc

        try:
            if timeout:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    process.communicate(), timeout=float(timeout)
                )
            else:
                stdout_bytes, stderr_bytes = await process.communicate()
        except asyncio.TimeoutError:
            try:
                process.kill()
            except Exception:
                pass
            raise RuntimeError(f"Command timed out after {timeout} seconds: {command}")

        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")

        output = stdout
        if stderr:
            if output:
                output += "\n--- STDERR ---\n" + stderr
            else:
                output = stderr

        if not output.strip():
            output = "(command completed with no output)"

        # Truncate long output to avoid exhausting model context window
        max_chars = 4000
        if len(output) > max_chars:
            half = max_chars // 2
            output = (
                output[:half]
                + f"\n\n... [{len(output) - max_chars} characters truncated] ...\n\n"
                + output[-half:]
            )

        if process.returncode != 0:
            raise RuntimeError(
                f"Command returned non-zero exit status {process.returncode}:\n{output}"
            )

        return ToolResult(
            content=[TextContent(text=output)],
            details={
                "exit_code": process.returncode,
                "command": command,
            },
        )

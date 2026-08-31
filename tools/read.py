"""
tools/read.py — Tool for reading file contents with line numbering.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agent.types import TextContent, ToolResult
from tools.base import Tool, ToolContext, ToolUpdateCallback
from tools.safety import resolve_within_cwd
class ReadFileTool(Tool):
    """
    Reads contents of a text file with 1-indexed line numbers and line slice limits.
    """

    name = "read_file"
    label = "Read File"
    replay_safety = "safe"
    execution_mode = "sequential"

    parameters_schema = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path to the file to read, relative to context.cwd or absolute.",
            },
            "start_line": {
                "type": "integer",
                "description": "Optional 1-indexed start line number (inclusive).",
            },
            "end_line": {
                "type": "integer",
                "description": "Optional 1-indexed end line number (inclusive).",
            },
        },
        "required": ["path"],
    }

    async def execute(
        self,
        tool_call_id: str,
        arguments: dict[str, Any],
        context: ToolContext,
        on_update: ToolUpdateCallback | None = None,
    ) -> ToolResult:
        rel_path = arguments.get("path")
        if not rel_path or not isinstance(rel_path, str):
            raise ValueError("Argument 'path' must be a non-empty string.")

        
        
        target_path = resolve_within_cwd(context.cwd, rel_path)

        if not target_path.exists():
            raise FileNotFoundError(f"File not found: {rel_path}")
        if not target_path.is_file():
            raise ValueError(f"Path is not a regular file: {rel_path}")

        try:
            content = target_path.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            raise RuntimeError(f"Could not read file '{rel_path}': {exc}") from exc

        lines = content.splitlines()
        total_lines = len(lines)

        start_line = arguments.get("start_line", 1)
        end_line = arguments.get("end_line", total_lines)

        if not isinstance(start_line, int) or start_line < 1:
            raise ValueError("start_line must be an integer >= 1.")
        if not isinstance(end_line, int) or end_line < start_line:
            raise ValueError("end_line must be an integer >= start_line.")

        # Cap lines to max 800 per read to prevent context window overflow
        max_slice = 800
        actual_end = min(end_line, total_lines)
        if actual_end - start_line + 1 > max_slice:
            actual_end = start_line + max_slice - 1

        selected_lines = lines[start_line - 1 : actual_end]
        formatted_lines = [
            f"{start_line + i}: {line}" for i, line in enumerate(selected_lines)
        ]
        result_text = "\n".join(formatted_lines)

        if actual_end < total_lines and end_line > actual_end:
            result_text += f"\n\n... [showing lines {start_line}-{actual_end} of {total_lines}] ..."

        return ToolResult(
            content=[TextContent(text=result_text)],
            details={
                "path": str(target_path),
                "total_lines": total_lines,
                "start_line": start_line,
                "end_line": actual_end,
            },
        )

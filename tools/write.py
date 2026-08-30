"""
tools/write.py — Tool for creating or overwriting files.
"""

from __future__ import annotations

import difflib
from pathlib import Path
from typing import Any

from agent.types import TextContent, ToolResult
from tools.base import Tool, ToolContext, ToolUpdateCallback


class WriteFileTool(Tool):
    """
    Writes text content to a file, creating parent directories if needed.
    """

    name = "write_file"
    label = "Write File"
    replay_safety = "unsafe"
    execution_mode = "sequential"

    parameters_schema = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path to the file to write, relative to context.cwd or absolute.",
            },
            "content": {
                "type": "string",
                "description": "Full content to write to the file.",
            },
        },
        "required": ["path", "content"],
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

        content = arguments.get("content")
        if content is None or not isinstance(content, str):
            raise ValueError("Argument 'content' must be a string.")

        target_path = Path(context.cwd) / rel_path if not Path(rel_path).is_absolute() else Path(rel_path)
        target_path = target_path.resolve()

        existed = target_path.exists()
        old_content = ""
        if existed:
            if target_path.is_dir():
                raise ValueError(f"Path is a directory, cannot overwrite: {rel_path}")
            try:
                old_content = target_path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                old_content = ""

        try:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_text(content, encoding="utf-8")
        except Exception as exc:
            raise RuntimeError(f"Failed to write file '{rel_path}': {exc}") from exc

        diff_lines = list(
            difflib.unified_diff(
                old_content.splitlines(),
                content.splitlines(),
                fromfile=f"a/{rel_path}",
                tofile=f"b/{rel_path}",
                lineterm="",
            )
        )
        diff_text = "\n".join(diff_lines)

        action_str = "updated" if existed else "created"
        summary = f"Successfully {action_str} {rel_path} ({len(content.encode('utf-8'))} bytes)."

        return ToolResult(
            content=[TextContent(text=summary)],
            details={
                "path": str(target_path),
                "existed": existed,
                "diff": diff_text,
                "bytes_written": len(content.encode("utf-8")),
            },
        )

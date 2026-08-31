"""
tools/edit.py — Tool for targeted string search-and-replace edits.
"""

from __future__ import annotations

import difflib
from pathlib import Path
from typing import Any

from agent.types import TextContent, ToolResult
from tools.base import Tool, ToolContext, ToolUpdateCallback
from tools.safety import resolve_within_cwd

class EditTool(Tool):
    """
    Performs targeted string replacement in a file.
    """

    name = "edit"
    label = "Edit File"
    replay_safety = "unsafe"
    execution_mode = "sequential"

    parameters_schema = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path to the file to edit, relative to context.cwd or absolute.",
            },
            "old_string": {
                "type": "string",
                "description": "Exact text block to find and replace.",
            },
            "new_string": {
                "type": "string",
                "description": "Replacement text block.",
            },
            "allow_multiple": {
                "type": "boolean",
                "description": "Allow replacing multiple occurrences. Default false (must match exactly once).",
            },
        },
        "required": ["path", "old_string", "new_string"],
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

        old_string = arguments.get("old_string")
        if old_string is None or not isinstance(old_string, str) or not old_string:
            raise ValueError("Argument 'old_string' must be a non-empty string.")

        new_string = arguments.get("new_string")
        if new_string is None or not isinstance(new_string, str):
            raise ValueError("Argument 'new_string' must be a string.")

        allow_multiple = bool(arguments.get("allow_multiple", False))

        target_path = resolve_within_cwd(context.cwd, rel_path)

        if not target_path.exists():
            raise FileNotFoundError(f"File not found: {rel_path}")
        if not target_path.is_file():
            raise ValueError(f"Path is not a regular file: {rel_path}")

        try:
            content = target_path.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            raise RuntimeError(f"Could not read file '{rel_path}': {exc}") from exc

        count = content.count(old_string)
        if count == 0:
            raise ValueError(f"Target 'old_string' was not found in file: {rel_path}")
        if count > 1 and not allow_multiple:
            raise ValueError(
                f"Target 'old_string' matched {count} occurrences in {rel_path}. "
                "Provide more context to make the match unique, or set allow_multiple=True."
            )

        new_content = content.replace(old_string, new_string) if allow_multiple else content.replace(old_string, new_string, 1)

        try:
            target_path.write_text(new_content, encoding="utf-8")
        except Exception as exc:
            raise RuntimeError(f"Failed to write edited file '{rel_path}': {exc}") from exc

        diff_lines = list(
            difflib.unified_diff(
                content.splitlines(),
                new_content.splitlines(),
                fromfile=f"a/{rel_path}",
                tofile=f"b/{rel_path}",
                lineterm="",
            )
        )
        diff_text = "\n".join(diff_lines)

        return ToolResult(
            content=[TextContent(text=f"Successfully replaced {count} occurrence(s) in {rel_path}.")],
            details={
                "path": str(target_path),
                "occurrences_replaced": count,
                "diff": diff_text,
            },
        )

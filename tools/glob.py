"""
tools/glob.py — Tool for finding files by glob pattern.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agent.types import TextContent, ToolResult
from tools.base import Tool, ToolContext, ToolUpdateCallback

DEFAULT_IGNORED_DIRS = {".git", "__pycache__", "venv", ".venv", "node_modules", ".pytest_cache", ".idea", ".vscode"}


class GlobTool(Tool):
    """
    Finds files matching glob pattern while skipping common non-code/cache directories.
    """

    name = "glob"
    label = "Glob Search"
    replay_safety = "safe"
    execution_mode = "sequential"

    parameters_schema = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Glob pattern (e.g. '**/*.py', 'tests/test_*.py').",
            },
            "path": {
                "type": "string",
                "description": "Base directory to search from, relative to context.cwd (defaults to context.cwd).",
            },
        },
        "required": ["pattern"],
    }

    async def execute(
        self,
        tool_call_id: str,
        arguments: dict[str, Any],
        context: ToolContext,
        on_update: ToolUpdateCallback | None = None,
    ) -> ToolResult:
        pattern = arguments.get("pattern")
        if not pattern or not isinstance(pattern, str):
            raise ValueError("Argument 'pattern' must be a non-empty string.")

        rel_base = arguments.get("path", ".")
        base_path = Path(context.cwd) / rel_base if not Path(rel_base).is_absolute() else Path(rel_base)
        base_path = base_path.resolve()

        if not base_path.exists():
            raise FileNotFoundError(f"Search directory not found: {rel_base}")
        if not base_path.is_dir():
            raise ValueError(f"Path is not a directory: {rel_base}")

        matched_files: list[str] = []
        try:
            for p in base_path.glob(pattern):
                # Skip ignored directories in path hierarchy
                if any(part in DEFAULT_IGNORED_DIRS for part in p.parts):
                    continue
                if p.is_file():
                    try:
                        rel = str(p.relative_to(base_path))
                    except ValueError:
                        rel = str(p)
                    matched_files.append(rel)
        except Exception as exc:
            raise RuntimeError(f"Glob search failed for pattern '{pattern}': {exc}") from exc

        matched_files.sort()

        max_results = 500
        truncated = False
        if len(matched_files) > max_results:
            matched_files = matched_files[:max_results]
            truncated = True

        if not matched_files:
            output = f"No files matched pattern '{pattern}'."
        else:
            output = "\n".join(matched_files)
            if truncated:
                output += f"\n\n... [showing first {max_results} results] ..."

        return ToolResult(
            content=[TextContent(text=output)],
            details={
                "base_path": str(base_path),
                "pattern": pattern,
                "count": len(matched_files),
                "truncated": truncated,
            },
        )

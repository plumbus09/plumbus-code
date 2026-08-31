"""
tools/grep.py — Tool for searching text/regex patterns in codebase.
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path
from typing import Any

from agent.types import TextContent, ToolResult
from tools.base import Tool, ToolContext, ToolUpdateCallback
from tools.safety import resolve_within_cwd

DEFAULT_IGNORED_DIRS = {".git", "__pycache__", "venv", ".venv", "node_modules", ".pytest_cache", ".idea", ".vscode"}


class GrepTool(Tool):
    """
    Searches for literal text or regex patterns across files in the workspace.
    """

    name = "grep"
    label = "Grep Search"
    replay_safety = "safe"
    execution_mode = "sequential"

    parameters_schema = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Text string or regex pattern to search for.",
            },
            "path": {
                "type": "string",
                "description": "Directory or file to search in, relative to context.cwd (defaults to context.cwd).",
            },
            "is_regex": {
                "type": "boolean",
                "description": "Set to true if query is a regular expression. Default false (literal text).",
            },
            "includes": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional list of glob patterns to include (e.g. ['*.py']).",
            },
        },
        "required": ["query"],
    }

    async def execute(
        self,
        tool_call_id: str,
        arguments: dict[str, Any],
        context: ToolContext,
        on_update: ToolUpdateCallback | None = None,
    ) -> ToolResult:
        query = arguments.get("query")
        if not query or not isinstance(query, str):
            raise ValueError("Argument 'query' must be a non-empty string.")

        is_regex = bool(arguments.get("is_regex", False))
        includes = arguments.get("includes") or []
        rel_base = arguments.get("path", ".")

        base_path = resolve_within_cwd(context.cwd, rel_base)
        

        if not base_path.exists():
            raise FileNotFoundError(f"Search path not found: {rel_base}")

        pattern: re.Pattern[str]
        if is_regex:
            try:
                pattern = re.compile(query)
            except re.error as exc:
                raise ValueError(f"Invalid regex pattern '{query}': {exc}") from exc
        else:
            pattern = re.compile(re.escape(query))

        target_files: list[Path] = []
        if base_path.is_file():
            target_files.append(base_path)
        else:
            for p in base_path.rglob("*"):
                if any(part in DEFAULT_IGNORED_DIRS for part in p.parts):
                    continue
                if p.is_file():
                    if includes:
                        if not any(fnmatch.fnmatch(p.name, glob_pat) for glob_pat in includes):
                            continue
                    target_files.append(p)

        matches: list[str] = []
        max_matches = 250
        total_matches = 0

        for file_path in target_files:
            if len(matches) >= max_matches:
                break
            try:
                content = file_path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue

            try:
                rel_str = str(file_path.relative_to(Path(context.cwd)))
            except ValueError:
                rel_str = str(file_path)

            for line_idx, line in enumerate(content.splitlines(), start=1):
                if pattern.search(line):
                    total_matches += 1
                    if len(matches) < max_matches:
                        matches.append(f"{rel_str}:{line_idx}:{line}")

        if not matches:
            output = f"No matches found for '{query}'."
        else:
            output = "\n".join(matches)
            if total_matches > max_matches:
                output += f"\n\n... [{total_matches - max_matches} additional matches omitted] ..."

        return ToolResult(
            content=[TextContent(text=output)],
            details={
                "query": query,
                "is_regex": is_regex,
                "total_matches": total_matches,
                "returned_matches": len(matches),
            },
        )

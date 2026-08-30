"""
tools/registry.py — a simple lookup table from tool name to Tool instance.

Deliberately minimal: no plugin discovery, no dynamic loading, no per-tool
enable/disable config yet. Add complexity when a real requirement forces
it, not before — you don't have enough tools yet for any of that to matter.
"""

from __future__ import annotations

from agent.types import ToolSpec
from tools.base import Tool


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Tool '{tool.name}' is already registered.")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def has(self, name: str) -> bool:
        return name in self._tools

    def specs(self) -> list[ToolSpec]:
        """Everything the model should see, via Context.tools."""
        return [t.to_spec() for t in self._tools.values()]

    def names(self) -> list[str]:
        return list(self._tools.keys())
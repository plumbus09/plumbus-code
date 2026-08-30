"""
tools module — Core agent tool definitions and registry builder.
"""

from __future__ import annotations

from tools.base import Tool, ToolContext, ToolResult, ToolUpdateCallback
from tools.bash import BashTool
from tools.edit import EditTool
from tools.glob import GlobTool
from tools.grep import GrepTool
from tools.read import ReadFileTool
from tools.registry import ToolRegistry
from tools.write import WriteFileTool


def default_tools() -> ToolRegistry:
    """
    Returns a ToolRegistry loaded with all built-in core tools.
    """
    registry = ToolRegistry()
    registry.register(BashTool())
    registry.register(ReadFileTool())
    registry.register(WriteFileTool())
    registry.register(EditTool())
    registry.register(GlobTool())
    registry.register(GrepTool())
    return registry


__all__ = [
    "Tool",
    "ToolContext",
    "ToolResult",
    "ToolUpdateCallback",
    "ToolRegistry",
    "BashTool",
    "ReadFileTool",
    "WriteFileTool",
    "EditTool",
    "GlobTool",
    "GrepTool",
    "default_tools",
]

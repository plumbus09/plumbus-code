"""
tools/permissions.py — Permission policy object & command deny-list.

Defines the permission gate that evaluates tool execution requests before
they run. Supports per-tool policies ("auto", "ask", "deny") and regex-based
command deny-lists for shell commands.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from tools.base import Tool

ActionPolicy = Literal["auto", "ask", "deny"]

DEFAULT_BASH_DENY_PATTERNS = [
    r"\brm\s+-[a-zA-Z]*f[a-zA-Z]*\b",  # rm -rf, rm -f, rm -fr, etc.
    r"\brm\s+.*--force\b",             # rm --force
    r"\bsudo\b",                        # sudo
    r"\bsu\b",                          # su
    r"\bchmod\s+-R\s+777\b",            # dangerous recursive chmod
    r"\bmkfs\b",                        # formatting filesystems
    r"\bdd\s+if=",                      # raw disk writes
    r":\(\)\{\s*:\|:&\s*\};:",          # fork bomb
]

DEFAULT_TOOL_POLICIES: dict[str, ActionPolicy] = {
    "read_file": "auto",
    "grep": "auto",
    "glob": "auto",
    "write_file": "ask",
    "edit": "ask",
    "bash": "ask",
}


class PermissionPolicy:
    """
    Policy object that evaluates whether a tool call should be auto-approved,
    require confirmation, or be denied outright.
    """

    def __init__(
        self,
        tool_policies: dict[str, ActionPolicy] | None = None,
        bash_deny_patterns: list[str] | None = None,
    ):
        self.tool_policies: dict[str, ActionPolicy] = {
            **DEFAULT_TOOL_POLICIES,
            **(tool_policies or {}),
        }
        raw_patterns = (
            DEFAULT_BASH_DENY_PATTERNS if bash_deny_patterns is None else bash_deny_patterns
        )
        self.bash_deny_regexes: list[re.Pattern[str]] = [
            re.compile(p, re.IGNORECASE) for p in raw_patterns
        ]

    def evaluate(self, tool: Tool, arguments: dict[str, Any]) -> ActionPolicy:
        """
        Evaluates the requested tool execution against the policy.

        Returns:
          - "deny": blocked outright (e.g. bash deny-list match or explicit deny policy)
          - "ask": requires explicit human approval via ToolContext.confirm()
          - "auto": safe to execute without asking
        """
        # Special check for bash commands against deny-list patterns
        if tool.name == "bash":
            command = str(arguments.get("command", ""))
            for pattern in self.bash_deny_regexes:
                if pattern.search(command):
                    return "deny"

        # Explicit tool policy override if defined
        if tool.name in self.tool_policies:
            return self.tool_policies[tool.name]

        # Fallback to tool's replay_safety declaration
        if getattr(tool, "replay_safety", "unsafe") == "safe":
            return "auto"

        return "ask"

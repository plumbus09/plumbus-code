"""
tools/base.py — abstract tool contract.

Mirrors pi's AgentTool<TParameters, TDetails> (packages/agent/src/types.ts),
adapted to Python. Unlike Provider.stream() in ai/model.py, a Tool's
execute() is ALLOWED to raise — pi's own doc comment says so explicitly:
"Execute the tool call. Throw on failure instead of encoding errors in
content." The loop (agent/loop.py) is responsible for catching that
exception and turning it into an error ToolResult — see
executePreparedToolCall in pi's agent-loop.ts, which wraps every tool
execute() call in try/except. Keep that responsibility in the loop, not
here, so individual tools stay simple to write.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Awaitable, Callable, Literal

from agent.types import ReplaySafety, ToolResult, ToolSpec


class ToolContext:
    """
    Runtime context passed into every tool's execute().

    - cwd: working directory tool paths resolve against.
    - confirm: awaitable permission-prompt callback. A tool that wants
      human approval before doing something destructive calls
      `await context.confirm("About to delete 3 files")` and gets a
      True/False back. The tool decides WHEN to ask; the POLICY of which
      tools need asking (auto/ask/deny per tool) lives one layer up, in a
      permission gate wrapping tool dispatch — Phase 4 work, not built
      yet. This field is just the plumbing that policy will use.
    - cancel: anything with an is_set() method, checked between steps of a
      long-running tool so Ctrl+C can interrupt cleanly. Mirrors
      StreamOptions.cancel in ai/model.py.
    """

    def __init__(
        self,
        cwd: str,
        confirm: Callable[[str], Awaitable[bool]] | None = None,
        cancel: Any = None,
        policy: Any = None,
    ):
        self.cwd = cwd
        self.confirm = confirm
        self.cancel = cancel
        self.policy = policy


ToolUpdateCallback = Callable[[ToolResult], None]
# Optional callback a tool can call mid-execution to report partial progress
# before its final ToolResult is ready. Mirrors pi's
# AgentToolUpdateCallback. Purely a UI nicety — most tools can ignore it.


class Tool(ABC):
    """
    Base class every real tool subclasses. A Tool is two things bolted
    together: a declaration (name/label/schema/replay_safety — what the
    model and the future permission gate need to know about) and a
    behavior (execute() — what it actually does).
    """

    name: str
    label: str
    parameters_schema: dict[str, Any]
    replay_safety: ReplaySafety
    execution_mode: Literal["sequential", "parallel"] = "sequential"
    # Default sequential, not parallel: pi defaults to "parallel" because
    # most of its built-in tools are read-only. Your early tools (bash,
    # write, edit) mutate real state, so sequential is the safer default
    # until you've deliberately audited a specific tool as safe to run
    # concurrently with others.

    def prepare_arguments(self, raw_args: dict[str, Any]) -> dict[str, Any]:
        """
        Optional compatibility shim for raw model-supplied arguments before
        schema validation (e.g. coercing a stringly-typed "true" to a real
        bool some models emit). Override only when you hit a real model
        quirk; default is a no-op passthrough.
        """
        return raw_args

    @abstractmethod
    async def execute(
        self,
        tool_call_id: str,
        arguments: dict[str, Any],
        context: ToolContext,
        on_update: ToolUpdateCallback | None = None,
    ) -> ToolResult:
        """
        Do the actual work. Raise on failure — do not try to encode errors
        into a successful-looking ToolResult yourself. The loop's tool
        dispatch is responsible for catching exceptions here and
        converting them into an error ToolResult.
        """
        ...

    def to_spec(self) -> ToolSpec:
        """The wire-format declaration sent to the model via Context.tools."""
        return ToolSpec(
            name=self.name,
            label=self.label,
            parameters_schema=self.parameters_schema,   # FIXED: was `schema=`
            replay_safety=self.replay_safety,
            execution_mode=self.execution_mode,
        )
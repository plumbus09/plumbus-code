"""
Phase 0 — Core type contracts.

Mirrors pi's packages/agent/src/types.ts, adapted to Python + your single-user,
single-lane scope. Two fixes applied after reading pi's real source (not just
the harness spec):

  1. Tool results split `content` (model-facing) from `details` (app-facing) —
     pi's AgentToolResult<T> does this; a single blob conflates two audiences.
  2. The model-call boundary (StreamFn-equivalent) must NEVER raise for
     request/model/runtime failures. Failure is a normal StopReason value
     ("error" | "aborted"), not an exception. This is what lets the loop stay
     free of try/except around the model call itself — only tool execution
     needs that wrapping (see core/loop.py).

No I/O, no behavior in this file. If you can't describe a resumable state as
one of the OperationState variants below, fix the type before writing loop
logic — don't patch around it later.
"""

from __future__ import annotations

from typing import Any, Literal, Union
from pydantic import BaseModel, ConfigDict


# ---------------------------------------------------------------------------
# Base config: every model here is immutable. The loop never mutates a
# message in place — it only ever appends a new one. This one rule prevents
# most of the state bugs you'd otherwise hit in resumability testing.
# ---------------------------------------------------------------------------
class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


# ---------------------------------------------------------------------------
# Content blocks — what a message is made of.
# ---------------------------------------------------------------------------
class TextContent(Frozen):
    type: Literal["text"] = "text"
    text: str


class ToolCallContent(Frozen):
    type: Literal["tool_call"] = "tool_call"
    id: str
    name: str
    arguments: dict[str, Any]


ContentBlock = Union[TextContent, ToolCallContent]


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------
StopReason = Literal["end_turn", "tool_use", "max_tokens", "error", "aborted"]
# "length" in pi's vocabulary == "max_tokens" here — kept explicit because the
# loop treats it specially (see SettledAssistantMessage note below).


class UserMessage(Frozen):
    role: Literal["user"] = "user"
    content: list[ContentBlock]
    timestamp: int


class AssistantMessage(Frozen):
    role: Literal["assistant"] = "assistant"
    content: list[ContentBlock]
    stop_reason: StopReason
    error_message: str | None = None
    timestamp: int


class ToolResultMessage(Frozen):
    role: Literal["tool_result"] = "tool_result"
    tool_call_id: str
    tool_name: str
    content: list[ContentBlock]          # what the model sees
    details: Any = None                  # what your app/UI sees — never sent to the model
    is_error: bool = False
    timestamp: int


Message = Union[UserMessage, AssistantMessage, ToolResultMessage]


def is_settled(message: AssistantMessage) -> bool:
    """
    Mirrors pi's SettledAssistantMessage: a message is only "done" if its
    stop_reason is not still pending. There's no "pending" value in this
    Python model (we don't stream partials into the same object pi does),
    but keep this helper as the one place that decides "is this final."
    """
    return message.stop_reason in ("end_turn", "tool_use", "max_tokens", "error", "aborted")


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------
ReplaySafety = Literal["safe", "unsafe"]
# "safe"   -> idempotent / read-only; ok to silently re-run after a crash.
# "unsafe" -> has side effects; NEVER re-run blindly after a crash. On resume,
#             synthesize an error/interrupted result instead (see harness doc
#             §0.5 and your Phase 5).


class ToolSpec(Frozen):
    name: str
    label: str                       # human-readable, for UI/logs
    parameters_schema: dict[str, Any]           # JSON schema for arguments
    replay_safety: ReplaySafety
    execution_mode: Literal["sequential", "parallel"] = "parallel"


class ToolResult(Frozen):
    """
    Returned by a tool's execute() function.

    Split mirrors pi's AgentToolResult<T>:
      - content: exactly what goes back to the model as a tool_result message
      - details: structured data for your TUI/logs — never serialized to the model
    """
    content: list[ContentBlock]
    details: Any = None
    terminate: bool = False
    # Batch-wide AND, per pi: the agent only stops early if EVERY tool result
    # in the current batch sets terminate=True. A single tool asking to stop
    # does not override the others still running in the same turn.


# ---------------------------------------------------------------------------
# Operation state — the durable "program counter" from the harness doc.
# This is a DISCRIMINATED UNION on purpose: Python won't enforce exhaustive
# handling of these variants the way TS's compiler would, so any code that
# switches on `kind` should raise on an unhandled branch rather than silently
# falling through. Lean on `pyright --strict` / a manual exhaustiveness check.
# ---------------------------------------------------------------------------
class OpCheckpoint(Frozen):
    kind: Literal["checkpoint"] = "checkpoint"
    # Idle between turns. Nothing in flight. Safe resume point: just wait
    # for the next prompt, or continue the outer loop if one is queued.


class OpAwaitingModel(Frozen):
    kind: Literal["awaiting_model"] = "awaiting_model"
    reserved_response_id: str
    # Intent has been committed (this ID is reserved for the reply) but the
    # model call itself is the one genuinely uncertain window. On crash
    # recovery: assume it MAY have been billed, and just retry the call
    # under a fresh attempt — model calls are inherently safe-to-retry from
    # the user's perspective (worst case: a duplicate charge, not corrupted
    # state), unlike tool side effects.


class OpAwaitingTool(Frozen):
    kind: Literal["awaiting_tool"] = "awaiting_tool"
    assistant_message_id: str
    pending_tool_call_ids: list[str]
    completed_tool_call_ids: list[str]
    reserved_result_ids: dict[str, str]   # tool_call_id -> reserved entry id
    tool_replay_safety: dict[str, ReplaySafety]  # tool_call_id -> declared safety
    # On crash recovery: for each id in pending_tool_call_ids, check
    # tool_replay_safety. "safe" -> re-run. "unsafe" -> synthesize an
    # "interrupted" ToolResultMessage under reserved_result_ids[id] and mark
    # it completed. Never guess; always consult the declared safety.


class OpDone(Frozen):
    kind: Literal["done"] = "done"
    final_stop_reason: StopReason


OperationState = Union[OpCheckpoint, OpAwaitingModel, OpAwaitingTool, OpDone]


# ---------------------------------------------------------------------------
# Entry — one node in the (single-lane, unbranched) conversation history.
# parent_id is kept even though you never branch: it's what makes replay()
# an unambiguous walk instead of a trust-insertion-order guess, and it's
# free to add now vs. expensive to retrofit later (see harness §0.2 tree
# discussion).
# ---------------------------------------------------------------------------
class Entry(Frozen):
    id: str                # UUIDv7
    parent_id: str | None
    seq: int                # storage-assigned at commit, monotonic session-wide
    timestamp: int
    type: Literal["message", "custom"]
    payload: dict[str, Any]


# ---------------------------------------------------------------------------
# The StreamFn contract — the model-call boundary.
#
# pi's actual doc comment, verbatim intent:
#   "Must not throw or return a rejected promise for request/model/runtime
#    failures. Failures must be encoded in the returned stream via ... a
#    final AssistantMessage with stopReason 'error' or 'aborted'."
#
# In Python: your model-calling function's return type is always an
# AssistantMessage. Network errors, rate limits, timeouts — all of these
# become stop_reason="error" + error_message=str(exc), caught INSIDE the
# function. The loop itself never wraps this call in try/except; if you
# find yourself doing that, the contract has been violated somewhere.
# ---------------------------------------------------------------------------
StreamFn = Any  # Callable[[list[Message], list[ToolSpec]], Awaitable[AssistantMessage]]
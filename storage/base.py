"""
storage/base.py — the Storage interface.

Scoped-down version of the harness-doc research: entries / registers /
usage rows, atomic transactions, four verbs only (insert entry, insert
usage, set register, delete register).
Deliberately missing from pi's real , design, on purpose, per ARCHITECTURE.md's exclusions table:
  - no branch/tree structure — single lane, entries chain via parent_id
    but there's exactly one leaf, never multiple
  - no writer-lease/fencing — one process, one user
  - no cross-namespace register scan — same reasoning as the harness doc:
    recovery reads must be index-driven and bounded, never "scan
    everything and infer"

Everything in agent/resume.py (Phase 6) will be built on nothing but the
five read methods below plus commit(). If resumability logic ever needs a
method that isn't here, that's a sign the state design is wrong, not that
this interface is missing something — add methods only when a real,
specific read pattern demands it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Literal, Union

from pydantic import BaseModel, ConfigDict

from agent.types import Entry


class CorruptionError(RuntimeError):
    """
    Raised when a commit would violate an absolute invariant: reusing an
    existing id, or an entry naming a parent that doesn't exist (neither
    already committed nor earlier in the same transaction). Per the
    harness-doc research: "a missing parent is always corruption" — this
    is not a recoverable, catchable-and-continue error anywhere in the
    system. If you ever catch this and try to proceed, that's the bug.
    """


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


# ---------------------------------------------------------------------------
# What you write. seq/timestamp are storage-assigned at commit — never
# supplied by the caller — so NewEntry omits them (mirrors the harness
# doc's Omit<Entry, "seq" | "timestamp">).
# ---------------------------------------------------------------------------
class NewEntry(Frozen):
    id: str
    parent_id: str | None
    type: Literal["message", "custom"]
    payload: dict[str, Any]


class InsertEntryWrite(Frozen):
    kind: Literal["entry"] = "entry"
    entry: NewEntry


class InsertUsageWrite(Frozen):
    kind: Literal["usage"] = "usage"
    id: str
    entry_id: str | None = None
    adjustment: bool = False
    usage: dict[str, Any] = {}


class SetRegisterWrite(Frozen):
    kind: Literal["set_register"] = "set_register"
    namespace: str
    key: str
    value: Any


class DeleteRegisterWrite(Frozen):
    kind: Literal["delete_register"] = "delete_register"
    namespace: str
    key: str


Write = Union[InsertEntryWrite, InsertUsageWrite, SetRegisterWrite, DeleteRegisterWrite]


class Transaction(Frozen):
    writes: list[Write]


class CommitResult(Frozen):
    first_seq: int
    seqs: list[int]


# ---------------------------------------------------------------------------
# What you read back
# ---------------------------------------------------------------------------
class Register(Frozen):
    namespace: str
    key: str
    value: Any
    seq: int


class UsageRow(Frozen):
    id: str
    seq: int
    entry_id: str | None
    adjustment: bool
    usage: dict[str, Any]


# ---------------------------------------------------------------------------
# The interface. Every backend (Memory, SQLite) implements exactly this.
# ---------------------------------------------------------------------------
class Storage(ABC):
    @abstractmethod
    async def commit(self, tx: Transaction) -> CommitResult:
        """All writes in tx land, or none do. Raises CorruptionError for
        an invalid parent/duplicate id — never partially applies."""
        ...

    @abstractmethod
    async def get_entries(self, ids: list[str]) -> dict[str, Entry]:
        """Exact-id batch lookup. Missing ids are simply absent from the
        returned dict, not an error — the caller decides if that's
        corruption in context."""
        ...

    @abstractmethod
    async def get_register(self, namespace: str, key: str) -> Register | None:
        ...

    @abstractmethod
    async def list_registers(self, namespace: str, key_prefix: str = "") -> list[Register]:
        """Indexed prefix listing over one namespace — e.g. terminal
        cleanup's op.* prefix scan. Never a cross-namespace scan."""
        ...

    @abstractmethod
    async def scan_entries(self, from_seq: int = 0) -> list[Entry]:
        """Session-wide, seq-ordered. No branch/tree filtering — single
        lane means this IS the conversation, in order."""
        ...

    @abstractmethod
    async def scan_usage(self, from_seq: int = 0) -> list[UsageRow]:
        ...

    @abstractmethod
    async def close(self) -> None:
        ...
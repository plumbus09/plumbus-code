"""
storage/memory.py — in-memory Storage implementation.

Plain dicts, one asyncio.Lock serializing commits (mirrors "one writer,
one queue" from the harness-doc research, scaled down — no real
concurrency to worry about in a single process, the lock just keeps
commit() atomic against itself if ever called concurrently by mistake).

Purpose: get agent/resume.py's logic built and tested here FIRST, with
zero disk I/O, before storage/sqlite.py exists. Once resumability tests
pass against this, swapping in SQLiteStorage should require no changes to
the code that calls Storage — only the constructor call site changes.
"""

from __future__ import annotations

import asyncio
import time

from agent.types import Entry
from storage.base import (
    CorruptionError,
    CommitResult,
    DeleteRegisterWrite,
    InsertEntryWrite,
    InsertUsageWrite,
    Register,
    SetRegisterWrite,
    Storage,
    Transaction,
    UsageRow,
)


def _now_ms() -> int:
    return int(time.time() * 1000)


class MemoryStorage(Storage):
    def __init__(self) -> None:
        self._entries: dict[str, Entry] = {}
        self._registers: dict[tuple[str, str], Register] = {}
        self._usage: dict[str, UsageRow] = {}
        self._next_seq = 1
        self._lock = asyncio.Lock()
        self._closed = False

    async def commit(self, tx: Transaction) -> CommitResult:
        if self._closed:
            raise RuntimeError("commit() called on a closed Storage instance.")

        async with self._lock:
            # --- validate the WHOLE transaction before applying ANY of it.
            # This is what makes "all writes land, or none do" true even
            # though this backend has no real rollback machinery — we
            # simply never start mutating until every write is known-good.
            known_entry_ids = set(self._entries.keys())
            known_usage_ids = set(self._usage.keys())

            for w in tx.writes:
                if isinstance(w, InsertEntryWrite):
                    if w.entry.id in known_entry_ids or w.entry.id in known_usage_ids:
                        raise CorruptionError(f"id '{w.entry.id}' already exists — ids are write-once.")
                    if w.entry.parent_id is not None and w.entry.parent_id not in known_entry_ids:
                        raise CorruptionError(
                            f"entry '{w.entry.id}' names parent '{w.entry.parent_id}', "
                            f"which doesn't exist (not yet committed, and not earlier in this transaction)."
                        )
                    known_entry_ids.add(w.entry.id)
                elif isinstance(w, InsertUsageWrite):
                    if w.id in known_usage_ids or w.id in known_entry_ids:
                        raise CorruptionError(f"id '{w.id}' already exists — ids are write-once.")
                    known_usage_ids.add(w.id)
                # SetRegisterWrite / DeleteRegisterWrite: always valid —
                # overwrite-or-create and delete-if-present-else-noop have
                # no invariant to violate.

            # --- apply, now that we know every write is valid.
            seq = self._next_seq
            seqs: list[int] = []
            now = _now_ms()

            for w in tx.writes:
                if isinstance(w, InsertEntryWrite):
                    entry = Entry(
                        id=w.entry.id,
                        parent_id=w.entry.parent_id,
                        seq=seq,
                        timestamp=now,
                        type=w.entry.type,
                        payload=w.entry.payload,
                    )
                    self._entries[entry.id] = entry
                elif isinstance(w, InsertUsageWrite):
                    row = UsageRow(
                        id=w.id, seq=seq, entry_id=w.entry_id,
                        adjustment=w.adjustment, usage=w.usage,
                    )
                    self._usage[w.id] = row
                elif isinstance(w, SetRegisterWrite):
                    self._registers[(w.namespace, w.key)] = Register(
                        namespace=w.namespace, key=w.key, value=w.value, seq=seq,
                    )
                elif isinstance(w, DeleteRegisterWrite):
                    self._registers.pop((w.namespace, w.key), None)  # absent key -> no-op

                seqs.append(seq)
                seq += 1

            first_seq = self._next_seq
            self._next_seq = seq
            return CommitResult(first_seq=first_seq, seqs=seqs)

    async def get_entries(self, ids: list[str]) -> dict[str, Entry]:
        return {i: self._entries[i] for i in ids if i in self._entries}

    async def get_register(self, namespace: str, key: str) -> Register | None:
        return self._registers.get((namespace, key))

    async def list_registers(self, namespace: str, key_prefix: str = "") -> list[Register]:
        return [
            r for (ns, k), r in self._registers.items()
            if ns == namespace and k.startswith(key_prefix)
        ]

    async def scan_entries(self, from_seq: int = 0) -> list[Entry]:
        return sorted(
            (e for e in self._entries.values() if e.seq > from_seq),
            key=lambda e: e.seq,
        )

    async def scan_usage(self, from_seq: int = 0) -> list[UsageRow]:
        return sorted(
            (u for u in self._usage.values() if u.seq > from_seq),
            key=lambda u: u.seq,
        )

    async def close(self) -> None:
        self._closed = True
"""
storage/sqlite.py — real SQLite Storage implementation.

Same interface as storage/memory.py, by design — anything built and
tested against MemoryStorage should work here with only the constructor
call site changed.

Notes carried over from the harness-doc research, applied at this smaller
scale:
  - One database file per session (the file IS the session — corruption
    is confined to one file, deletion is unlinking it).
  - Every commit is one SQL transaction: BEGIN IMMEDIATE (not deferred),
    taking the write lock up front. A deferred BEGIN that reads before it
    writes can fail its later upgrade to a write lock if anything else
    committed in between — and no retry-with-backoff fixes that, because
    the read snapshot is already stale. BEGIN IMMEDIATE sidesteps the
    whole problem by never taking a read-only snapshot in the first place.
  - No writer-lease/fencing (unlike the harness doc's real design) — this
    project has exactly one process, one user. sqlite3's own file locking
    is sufficient; a real lease is solving a multi-process problem this
    project doesn't have.
  - Same asyncio.Lock-serialized commits as MemoryStorage — sqlite3 is a
    synchronous library; wrapping calls in the lock keeps this Storage's
    async interface honest even though the underlying I/O is blocking.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
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

_SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    id TEXT PRIMARY KEY,
    parent_id TEXT,
    seq INTEGER NOT NULL,
    type TEXT NOT NULL,
    payload TEXT NOT NULL,
    timestamp INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_entry_parent ON entries(parent_id);
CREATE INDEX IF NOT EXISTS ix_entry_seq ON entries(seq);

CREATE TABLE IF NOT EXISTS registers (
    namespace TEXT NOT NULL,
    key TEXT NOT NULL,
    seq INTEGER NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (namespace, key)
);

CREATE TABLE IF NOT EXISTS usage_ledger (
    id TEXT PRIMARY KEY,
    seq INTEGER NOT NULL,
    entry_id TEXT,
    adjustment INTEGER NOT NULL,
    usage TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_usage_seq ON usage_ledger(seq);

CREATE TABLE IF NOT EXISTS session_meta (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    next_seq INTEGER NOT NULL
);
"""


def _now_ms() -> int:
    return int(time.time() * 1000)


class SQLiteStorage(Storage):
    def __init__(self, path: str):
        self.path = path
        self._conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        # check_same_thread=False is safe here specifically BECAUSE every
        # access goes through self._lock (an asyncio.Lock), which
        # guarantees only one coroutine — and therefore only one
        # asyncio.to_thread worker — touches this connection at a time.
        # Without the lock, this would be a real bug, not a safe relaxation.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        cur = self._conn.execute("SELECT next_seq FROM session_meta WHERE id = 1")
        row = cur.fetchone()
        if row is None:
            self._conn.execute("INSERT INTO session_meta (id, next_seq) VALUES (1, 1)")
        self._lock = asyncio.Lock()
        self._closed = False

    async def commit(self, tx: Transaction) -> CommitResult:
        if self._closed:
            raise RuntimeError("commit() called on a closed Storage instance.")

        async with self._lock:
            return await asyncio.to_thread(self._commit_sync, tx)

    def _commit_sync(self, tx: Transaction) -> CommitResult:
        conn = self._conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            cur = conn.execute("SELECT next_seq FROM session_meta WHERE id = 1")
            seq = cur.fetchone()[0]
            first_seq = seq
            seqs: list[int] = []
            now = _now_ms()

            # --- validate the whole transaction before applying any of it
            known_entry_ids: set[str] = set()
            known_usage_ids: set[str] = set()
            for w in tx.writes:
                if isinstance(w, InsertEntryWrite):
                    if w.entry.id in known_entry_ids or w.entry.id in known_usage_ids or self._id_exists(conn, w.entry.id):
                        raise CorruptionError(f"id '{w.entry.id}' already exists — ids are write-once.")
                    if w.entry.parent_id is not None and w.entry.parent_id not in known_entry_ids:
                        if not self._entry_exists(conn, w.entry.parent_id):
                            raise CorruptionError(
                                f"entry '{w.entry.id}' names parent '{w.entry.parent_id}', "
                                f"which doesn't exist."
                            )
                    known_entry_ids.add(w.entry.id)
                elif isinstance(w, InsertUsageWrite):
                    if w.id in known_usage_ids or w.id in known_entry_ids or self._id_exists(conn, w.id):
                        raise CorruptionError(f"id '{w.id}' already exists — ids are write-once.")
                    known_usage_ids.add(w.id)

            # --- apply
            for w in tx.writes:
                if isinstance(w, InsertEntryWrite):
                    conn.execute(
                        "INSERT INTO entries (id, parent_id, seq, type, payload, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
                        (w.entry.id, w.entry.parent_id, seq, w.entry.type, json.dumps(w.entry.payload), now),
                    )
                elif isinstance(w, InsertUsageWrite):
                    conn.execute(
                        "INSERT INTO usage_ledger (id, seq, entry_id, adjustment, usage) VALUES (?, ?, ?, ?, ?)",
                        (w.id, seq, w.entry_id, int(w.adjustment), json.dumps(w.usage)),
                    )
                elif isinstance(w, SetRegisterWrite):
                    conn.execute(
                        "INSERT INTO registers (namespace, key, seq, value) VALUES (?, ?, ?, ?) "
                        "ON CONFLICT(namespace, key) DO UPDATE SET seq = excluded.seq, value = excluded.value",
                        (w.namespace, w.key, seq, json.dumps(w.value)),
                    )
                elif isinstance(w, DeleteRegisterWrite):
                    conn.execute(
                        "DELETE FROM registers WHERE namespace = ? AND key = ?",
                        (w.namespace, w.key),
                    )
                seqs.append(seq)
                seq += 1

            conn.execute("UPDATE session_meta SET next_seq = ? WHERE id = 1", (seq,))
            conn.execute("COMMIT")
            return CommitResult(first_seq=first_seq, seqs=seqs)
        except Exception:
            conn.execute("ROLLBACK")
            raise

    @staticmethod
    def _entry_exists(conn: sqlite3.Connection, entry_id: str) -> bool:
        cur = conn.execute("SELECT 1 FROM entries WHERE id = ?", (entry_id,))
        return cur.fetchone() is not None

    @staticmethod
    def _id_exists(conn: sqlite3.Connection, any_id: str) -> bool:
        cur = conn.execute(
            "SELECT 1 FROM entries WHERE id = ? UNION SELECT 1 FROM usage_ledger WHERE id = ?",
            (any_id, any_id),
        )
        return cur.fetchone() is not None

    async def get_entries(self, ids: list[str]) -> dict[str, Entry]:
        if not ids:
            return {}
        return await asyncio.to_thread(self._get_entries_sync, ids)

    def _get_entries_sync(self, ids: list[str]) -> dict[str, Entry]:
        placeholders = ",".join("?" for _ in ids)
        cur = self._conn.execute(
            f"SELECT id, parent_id, seq, type, payload, timestamp FROM entries WHERE id IN ({placeholders})", ids,
        )
        result = {}
        for row in cur.fetchall():
            eid, parent_id, seq, etype, payload, ts = row
            result[eid] = Entry(id=eid, parent_id=parent_id, seq=seq, timestamp=ts, type=etype, payload=json.loads(payload))
        return result

    async def get_register(self, namespace: str, key: str) -> Register | None:
        return await asyncio.to_thread(self._get_register_sync, namespace, key)

    def _get_register_sync(self, namespace: str, key: str) -> Register | None:
        cur = self._conn.execute(
            "SELECT namespace, key, seq, value FROM registers WHERE namespace = ? AND key = ?", (namespace, key),
        )
        row = cur.fetchone()
        if row is None:
            return None
        ns, k, seq, value = row
        return Register(namespace=ns, key=k, seq=seq, value=json.loads(value))

    async def list_registers(self, namespace: str, key_prefix: str = "") -> list[Register]:
        return await asyncio.to_thread(self._list_registers_sync, namespace, key_prefix)

    def _list_registers_sync(self, namespace: str, key_prefix: str) -> list[Register]:
        cur = self._conn.execute(
            "SELECT namespace, key, seq, value FROM registers WHERE namespace = ? AND key LIKE ? ESCAPE '\\'",
            (namespace, key_prefix.replace("%", "\\%").replace("_", "\\_") + "%"),
        )
        return [
            Register(namespace=ns, key=k, seq=seq, value=json.loads(value))
            for ns, k, seq, value in cur.fetchall()
        ]

    async def scan_entries(self, from_seq: int = 0) -> list[Entry]:
        return await asyncio.to_thread(self._scan_entries_sync, from_seq)

    def _scan_entries_sync(self, from_seq: int) -> list[Entry]:
        cur = self._conn.execute(
            "SELECT id, parent_id, seq, type, payload, timestamp FROM entries WHERE seq > ? ORDER BY seq ASC",
            (from_seq,),
        )
        return [
            Entry(id=eid, parent_id=parent_id, seq=seq, timestamp=ts, type=etype, payload=json.loads(payload))
            for eid, parent_id, seq, etype, payload, ts in cur.fetchall()
        ]

    async def scan_usage(self, from_seq: int = 0) -> list[UsageRow]:
        return await asyncio.to_thread(self._scan_usage_sync, from_seq)

    def _scan_usage_sync(self, from_seq: int) -> list[UsageRow]:
        cur = self._conn.execute(
            "SELECT id, seq, entry_id, adjustment, usage FROM usage_ledger WHERE seq > ? ORDER BY seq ASC",
            (from_seq,),
        )
        return [
            UsageRow(id=uid, seq=seq, entry_id=entry_id, adjustment=bool(adj), usage=json.loads(usage))
            for uid, seq, entry_id, adj, usage in cur.fetchall()
        ]

    async def close(self) -> None:
        self._closed = True
        await asyncio.to_thread(self._conn.close)
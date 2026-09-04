"""
crash_harness.py — deterministic crash simulation.

CrashingStorage wraps a real Storage and raises SimulatedCrash right after
the Nth commit lands. This is functionally equivalent to a real SIGKILL
for our purposes: durability guarantees only depend on what's actually on
disk, not on which process wrote it. "Let N commits succeed, then blow up,
discard all Python state, open a FRESH Storage on the same file" produces
exactly the artifact a real `kill -9` would leave behind — and it's fully
deterministic, unlike racing a real signal against real I/O timing.

See test_resumability.py for one genuine external SIGKILL test, which
exists specifically to confirm this simulation's assumption holds.
"""

from __future__ import annotations

from agent.types import Entry
from storage.base import CommitResult, Register, Storage, Transaction, UsageRow


class SimulatedCrash(Exception):
    """Raised to simulate the process dying immediately after a commit lands."""


class CrashingStorage(Storage):
    def __init__(self, inner: Storage, crash_after: int):
        self._inner = inner
        self._count = 0
        self._crash_after = crash_after

    async def commit(self, tx: Transaction) -> CommitResult:
        result = await self._inner.commit(tx)
        self._count += 1
        if self._count == self._crash_after:
            raise SimulatedCrash(f"simulated crash immediately after commit #{self._count}")
        return result

    async def get_entries(self, ids: list[str]) -> dict[str, Entry]:
        return await self._inner.get_entries(ids)

    async def get_register(self, namespace: str, key: str) -> Register | None:
        return await self._inner.get_register(namespace, key)

    async def list_registers(self, namespace: str, key_prefix: str = "") -> list[Register]:
        return await self._inner.list_registers(namespace, key_prefix)

    async def scan_entries(self, from_seq: int = 0) -> list[Entry]:
        return await self._inner.scan_entries(from_seq)

    async def scan_usage(self, from_seq: int = 0) -> list[UsageRow]:
        return await self._inner.scan_usage(from_seq)

    async def close(self) -> None:
        await self._inner.close()


async def discover_operation_id(storage: Storage) -> str:
    """
    What a real restarted process would do: there's no operation_id
    passed in from outside after a crash, so find it the same way the
    real app would — list op.state registers. Single-lane means there's
    at most one live operation at a time, so this is unambiguous.
    """
    live_ops = await storage.list_registers("op.state")
    if len(live_ops) != 1:
        raise AssertionError(f"expected exactly one live operation, found {len(live_ops)}")
    return live_ops[0].key
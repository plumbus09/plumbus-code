"""
test_storage_conformance.py — one shared test suite, run against BOTH
backends. This is the actual proof that the Storage abstraction holds,
the same way pi's real Part 9 conformance suite proves Memory/JSONL/SQLite
all behave identically. Passing against one backend proves nothing about
the other; passing against both is what "the interface is solid" means.
"""

import asyncio
import tempfile
import uuid

from storage.base import (
    CorruptionError,
    DeleteRegisterWrite,
    InsertEntryWrite,
    InsertUsageWrite,
    NewEntry,
    SetRegisterWrite,
    Storage,
    Transaction,
)
from storage.memory import MemoryStorage
from storage.sqlite import SQLiteStorage


def _id() -> str:
    return str(uuid.uuid4())


async def check_basic_commit_and_read(storage: Storage):
    entry_id = _id()
    result = await storage.commit(Transaction(writes=[
        InsertEntryWrite(entry=NewEntry(id=entry_id, parent_id=None, type="message", payload={"text": "hi"})),
    ]))
    assert len(result.seqs) == 1
    entries = await storage.get_entries([entry_id])
    assert entries[entry_id].payload == {"text": "hi"}
    assert entries[entry_id].seq == result.seqs[0]
    print("  PASS: basic commit + get_entries")


async def check_parent_chain(storage: Storage):
    e1, e2, e3 = _id(), _id(), _id()
    await storage.commit(Transaction(writes=[
        InsertEntryWrite(entry=NewEntry(id=e1, parent_id=None, type="message", payload={"n": 1})),
    ]))
    await storage.commit(Transaction(writes=[
        InsertEntryWrite(entry=NewEntry(id=e2, parent_id=e1, type="message", payload={"n": 2})),
    ]))
    await storage.commit(Transaction(writes=[
        InsertEntryWrite(entry=NewEntry(id=e3, parent_id=e2, type="message", payload={"n": 3})),
    ]))
    chain = await storage.scan_entries(from_seq=0)
    ids_in_order = [e.id for e in chain]
    assert ids_in_order == [e1, e2, e3], ids_in_order
    print("  PASS: parent chain + scan_entries ordering")


async def check_duplicate_id_is_corruption(storage: Storage):
    entry_id = _id()
    await storage.commit(Transaction(writes=[
        InsertEntryWrite(entry=NewEntry(id=entry_id, parent_id=None, type="message", payload={})),
    ]))
    try:
        await storage.commit(Transaction(writes=[
            InsertEntryWrite(entry=NewEntry(id=entry_id, parent_id=None, type="message", payload={})),
        ]))
        raise AssertionError("expected CorruptionError on duplicate id")
    except CorruptionError:
        pass
    print("  PASS: duplicate id raises CorruptionError")


async def check_missing_parent_is_corruption(storage: Storage):
    try:
        await storage.commit(Transaction(writes=[
            InsertEntryWrite(entry=NewEntry(id=_id(), parent_id="nonexistent-parent-id", type="message", payload={})),
        ]))
        raise AssertionError("expected CorruptionError on missing parent")
    except CorruptionError:
        pass
    print("  PASS: missing parent raises CorruptionError")


async def check_failed_transaction_applies_nothing(storage: Storage):
    good_entry_id = _id()
    dup_id = _id()
    await storage.commit(Transaction(writes=[
        InsertEntryWrite(entry=NewEntry(id=dup_id, parent_id=None, type="message", payload={})),
    ]))

    try:
        await storage.commit(Transaction(writes=[
            SetRegisterWrite(namespace="fact.custom", key="should_not_persist", value="x"),
            InsertEntryWrite(entry=NewEntry(id=good_entry_id, parent_id=None, type="message", payload={})),
            InsertEntryWrite(entry=NewEntry(id=dup_id, parent_id=None, type="message", payload={})),
        ]))
        raise AssertionError("expected CorruptionError")
    except CorruptionError:
        pass

    reg = await storage.get_register("fact.custom", "should_not_persist")
    assert reg is None, "register write leaked from a failed transaction"
    entries = await storage.get_entries([good_entry_id])
    assert good_entry_id not in entries, "entry write leaked from a failed transaction"
    print("  PASS: failed transaction applies NOTHING (all-or-none holds)")


async def check_register_set_get_delete(storage: Storage):
    await storage.commit(Transaction(writes=[
        SetRegisterWrite(namespace="op.state", key="op_1", value={"kind": "checkpoint"}),
    ]))
    reg = await storage.get_register("op.state", "op_1")
    assert reg.value == {"kind": "checkpoint"}

    await storage.commit(Transaction(writes=[
        SetRegisterWrite(namespace="op.state", key="op_1", value={"kind": "done"}),
    ]))
    reg = await storage.get_register("op.state", "op_1")
    assert reg.value == {"kind": "done"}, "overwrite should replace, not merge"

    await storage.commit(Transaction(writes=[
        DeleteRegisterWrite(namespace="op.state", key="op_1"),
    ]))
    reg = await storage.get_register("op.state", "op_1")
    assert reg is None

    await storage.commit(Transaction(writes=[
        DeleteRegisterWrite(namespace="op.state", key="op_1"),
    ]))
    print("  PASS: register set/overwrite/delete/no-op-delete")


async def check_register_prefix_listing(storage: Storage):
    await storage.commit(Transaction(writes=[
        SetRegisterWrite(namespace="op.tool_args", key="op1:step1:0", value={"path": "a.txt"}),
        SetRegisterWrite(namespace="op.tool_args", key="op1:step1:1", value={"path": "b.txt"}),
        SetRegisterWrite(namespace="op.tool_args", key="op2:step1:0", value={"path": "c.txt"}),
    ]))
    op1_only = await storage.list_registers("op.tool_args", key_prefix="op1:")
    assert len(op1_only) == 2, op1_only
    print("  PASS: register prefix listing")


async def check_usage_ledger_survives_register_cleanup(storage: Storage):
    usage_id = _id()
    await storage.commit(Transaction(writes=[
        SetRegisterWrite(namespace="op.state", key="op_x", value={"kind": "awaiting_model"}),
        InsertUsageWrite(id=usage_id, entry_id=None, adjustment=False, usage={"input": 100, "output": 20}),
    ]))
    await storage.commit(Transaction(writes=[
        DeleteRegisterWrite(namespace="op.state", key="op_x"),
    ]))
    reg = await storage.get_register("op.state", "op_x")
    assert reg is None
    usage_rows = await storage.scan_usage(from_seq=0)
    assert any(u.id == usage_id for u in usage_rows), "usage row must survive register cleanup"
    print("  PASS: usage ledger survives register cleanup")


async def check_seq_is_strictly_increasing_across_transactions(storage: Storage):
    seqs_seen: list[int] = []
    for i in range(5):
        result = await storage.commit(Transaction(writes=[
            SetRegisterWrite(namespace="fact.custom", key=f"k{i}", value=i),
        ]))
        seqs_seen.extend(result.seqs)
    assert seqs_seen == sorted(seqs_seen), seqs_seen
    assert len(set(seqs_seen)) == len(seqs_seen), "seq values must be unique"
    print("  PASS: seq strictly increasing across transactions")


CHECKS = [
    check_basic_commit_and_read,
    check_parent_chain,
    check_duplicate_id_is_corruption,
    check_missing_parent_is_corruption,
    check_failed_transaction_applies_nothing,
    check_register_set_get_delete,
    check_register_prefix_listing,
    check_usage_ledger_survives_register_cleanup,
    check_seq_is_strictly_increasing_across_transactions,
]


async def run_suite(name: str, make_storage) -> None:
    print(f"\n=== {name} ===")
    for check in CHECKS:
        storage = await make_storage()
        try:
            await check(storage)
        finally:
            await storage.close()


async def _make_memory():
    return MemoryStorage()


async def main():
    await run_suite("MemoryStorage", _make_memory)

    with tempfile.TemporaryDirectory() as tmpdir:
        counter = {"n": 0}

        async def make_sqlite():
            counter["n"] += 1
            return SQLiteStorage(f"{tmpdir}/conformance_{counter['n']}.db")

        await run_suite("SQLiteStorage", make_sqlite)

    print("\nAll conformance checks passed on BOTH backends.")


if __name__ == "__main__":
    asyncio.run(main())
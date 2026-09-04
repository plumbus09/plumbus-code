"""
agent/persistence.py — Message <-> Entry.payload serialization.

Entries store arbitrary JSON payloads; Messages are typed Pydantic models.
This is the (small, boring, deliberately dumb) bridge between the two.
Dispatch on payload["role"] rather than trying to get Pydantic to
auto-discriminate the Message union — explicit is cheaper to debug than a
Union validator silently picking the wrong branch.
"""

from __future__ import annotations

from agent.types import AssistantMessage, Entry, Message, ToolResultMessage, UserMessage
from storage.base import Storage


def message_to_payload(message: Message) -> dict:
    return message.model_dump(mode="json")


def payload_to_message(payload: dict) -> Message:
    role = payload.get("role")
    if role == "user":
        return UserMessage.model_validate(payload)
    if role == "assistant":
        return AssistantMessage.model_validate(payload)
    if role == "tool_result":
        return ToolResultMessage.model_validate(payload)
    raise ValueError(f"Unknown message role in stored payload: {role!r}")



async def build_history(storage: Storage) -> list[Message]:
    """
    The full conversation, in order. Single-lane means seq order IS
    conversation order — no branch walking needed (see storage/base.py's
    scan_entries docstring). This is intentionally the ONLY way history is
    ever reconstructed — both fresh reads and crash-recovery reads go
    through this same function, so there's no separate "recovery logic"
    that could disagree with normal reads.
    """
    entries: list[Entry] = await storage.scan_entries(from_seq=0)
    return [payload_to_message(e.payload) for e in entries if e.type == "message"]
"""
ai/model.py — provider-agnostic model config + streaming contracts.

Mirrors pi's packages/ai: one Model/Provider abstraction, N providers
underneath it. We're starting with exactly one provider (OpenRouter), but
nothing in this file is OpenRouter-specific — that's the entire point of
splitting this out before writing agent-loop logic. core/loop.py (built
later) will only ever import from here, never from ai/openrouter.py
directly — that's what keeps swapping or adding a provider a one-file change.

Uses Pydantic throughout, matching core/types.py. This buys real runtime
validation (a malformed Context or Model raises at construction, not three
calls deep inside a provider) on top of the static typing dataclasses would
give you for free — worth the extra import for anything that crosses a
network boundary.
"""

from __future__ import annotations

from typing import Annotated, Any, AsyncIterator, Literal, Protocol, Union, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from agent.types import AssistantMessage, Frozen, Message, ToolSpec


class Model(Frozen):
    id: str                      # provider-specific id, e.g. "anthropic/claude-sonnet-4.5"
    provider: str                 # "openrouter"
    name: str                     # display name for UI/logs
    context_window: int
    supports_tools: bool = True
    supports_thinking: bool = False


class StreamOptions(BaseModel):
    # Not frozen: options.cancel is meant to be checked live during a stream
    # (e.g. an asyncio.Event flipped by a Ctrl+C handler) — the object it
    # points at is mutated by the caller, not this model itself.
    model_config = ConfigDict(arbitrary_types_allowed=True)
    api_key: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    thinking_level: Literal["off", "low", "medium", "high"] | None = None
    cancel: Any = None  # anything with an is_set() method; see Provider docstring


class Context(Frozen):
    system_prompt: str
    messages: list[Message]
    tools: list[ToolSpec] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Streaming events — trimmed version of pi's AssistantMessageEvent union.
# Add thinking_delta / image events later if/when you need them; don't build
# them speculatively now.
# ---------------------------------------------------------------------------
class StreamStart(Frozen):
    type: Literal["start"] = "start"


class TextDelta(Frozen):
    delta: str
    type: Literal["text_delta"] = "text_delta"


class ToolCallDelta(Frozen):
    id: str
    name: str
    arguments_delta: str          # raw JSON string fragment — accumulate, then parse once complete
    type: Literal["toolcall_delta"] = "toolcall_delta"


class StreamDone(Frozen):
    message: AssistantMessage
    type: Literal["done"] = "done"


StreamEvent = Annotated[
    Union[StreamStart, TextDelta, ToolCallDelta, StreamDone],
    Field(discriminator="type"),
]


@runtime_checkable
class Provider(Protocol):
    """
    The contract every provider must satisfy. This is the single most
    important rule in this whole layer — copied deliberately from pi's
    StreamFn doc comment because it's the thing that keeps the agent loop
    simple:

        stream() must NEVER raise for request/model/runtime failures —
        bad API key, network timeout, rate limit, malformed response,
        provider 5xx, JSON decode errors, anything about the outside world.
        Every such failure must be caught INSIDE the provider and encoded
        as a final StreamDone event whose message.stop_reason is "error"
        (or "aborted" if `options.cancel` was set), with error_message set.

    The only things allowed to escape stream() as real exceptions are
    programming errors — e.g. calling it with a malformed Context (which,
    with Pydantic, now raises a ValidationError at construction time,
    before stream() is ever called — one more reason this layer is Pydantic
    throughout).

    This is what lets core/loop.py call the model with zero try/except
    around it: if you ever find yourself wrapping a provider.stream() call
    in try/except in the loop, the contract has been violated somewhere in
    the provider and that's the bug to fix, not the loop.
    """

    def stream(
        self,
        model: Model,
        context: Context,
        options: StreamOptions,
    ) -> AsyncIterator[StreamEvent]: ...
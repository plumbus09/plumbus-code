"""
ai/openrouter.py — the one provider implementation.

OpenRouter speaks the OpenAI chat-completions wire format, so this file does
three jobs:
  1. Translate our Context/Message/ToolSpec into OpenAI's JSON shape.
  2. POST it with stream=True and parse the SSE response.
  3. Translate the accumulated response back into our AssistantMessage.

Every failure mode (network, auth, malformed SSE, provider error) is caught
here and turned into a normal StreamDone(message=... stop_reason="error").
Nothing escapes as a raised exception — see Provider's docstring in model.py
for why that matters.
"""

from __future__ import annotations

import json
import time
from typing import Any, AsyncIterator

import httpx

from agent.types import (
    AssistantMessage,
    Message,
    TextContent,
    ToolCallContent,
    ToolResultMessage,
    UserMessage,
)
from ai.model import (
    Context,
    Model,
    Provider,
    StreamDone,
    StreamEvent,
    StreamOptions,
    StreamStart,
    TextDelta,
    ToolCallDelta,
)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _error_message(text: str) -> AssistantMessage:
    return AssistantMessage(
        content=[],
        stop_reason="error",
        error_message=text,
        timestamp=_now_ms(),
    )


def _aborted_message() -> AssistantMessage:
    return AssistantMessage(
        content=[],
        stop_reason="aborted",
        error_message="Cancelled by caller.",
        timestamp=_now_ms(),
    )


# ---------------------------------------------------------------------------
# Message translation: our types -> OpenAI wire format
# ---------------------------------------------------------------------------
def _message_to_openai(message: Message) -> list[dict[str, Any]]:
    """
    Returns a list because a ToolResultMessage maps to exactly one OpenAI
    "tool" message, but an AssistantMessage with both text and tool calls
    still maps to exactly one message with a tool_calls array — kept as a
    list return for uniformity and future cases (e.g. multiple tool results
    batched together) without changing the call site.
    """
    if isinstance(message, UserMessage):
        text = "".join(b.text for b in message.content if isinstance(b, TextContent))
        return [{"role": "user", "content": text}]

    if isinstance(message, AssistantMessage):
        text = "".join(b.text for b in message.content if isinstance(b, TextContent))
        tool_calls = [
            {
                "id": b.id,
                "type": "function",
                "function": {"name": b.name, "arguments": json.dumps(b.arguments)},
            }
            for b in message.content
            if isinstance(b, ToolCallContent)
        ]
        out: dict[str, Any] = {"role": "assistant", "content": text or None}
        if tool_calls:
            out["tool_calls"] = tool_calls
        return [out]

    if isinstance(message, ToolResultMessage):
        text = "".join(b.text for b in message.content if isinstance(b, TextContent))
        return [
            {
                "role": "tool",
                "tool_call_id": message.tool_call_id,
                "content": text,
            }
        ]

    raise TypeError(f"Unknown message type: {type(message)!r}")


def _tool_to_openai(tool_spec) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool_spec.name,
            "description": tool_spec.label,
            "parameters": tool_spec.parameters_schema,
        },
    }


def _build_payload(model: Model, context: Context, options: StreamOptions) -> dict[str, Any]:
    messages: list[dict[str, Any]] = [{"role": "system", "content": context.system_prompt}]
    for m in context.messages:
        messages.extend(_message_to_openai(m))

    payload: dict[str, Any] = {
        "model": model.id,
        "messages": messages,
        "stream": True,
    }
    if context.tools:
        payload["tools"] = [_tool_to_openai(t) for t in context.tools]
    if options.temperature is not None:
        payload["temperature"] = options.temperature
    if options.max_tokens is not None:
        payload["max_tokens"] = options.max_tokens
    return payload


# ---------------------------------------------------------------------------
# Response translation: accumulated OpenAI stream -> our AssistantMessage
# ---------------------------------------------------------------------------
_FINISH_REASON_MAP = {
    "stop": "end_turn",
    "tool_calls": "tool_use",
    "length": "max_tokens",
}


class _Accumulator:
    """Collects streamed deltas into a final message. One per request."""

    def __init__(self) -> None:
        self.text_parts: list[str] = []
        # index -> {id, name, arguments (str, growing)}
        self.tool_calls: dict[int, dict[str, str]] = {}
        self.finish_reason: str | None = None

    def add_delta(self, delta: dict[str, Any]) -> None:
        if "content" in delta and delta["content"]:
            self.text_parts.append(delta["content"])
        for tc in delta.get("tool_calls") or []:
            idx = tc.get("index", 0)
            slot = self.tool_calls.setdefault(idx, {"id": "", "name": "", "arguments": ""})
            if tc.get("id"):
                slot["id"] = tc["id"]
            fn = tc.get("function") or {}
            if fn.get("name"):
                slot["name"] += fn["name"]
            if fn.get("arguments"):
                slot["arguments"] += fn["arguments"]

    def finalize(self) -> AssistantMessage:
        content: list[Any] = []
        text = "".join(self.text_parts)
        if text:
            content.append(TextContent(text=text))

        for slot in self.tool_calls.values():
            try:
                args = json.loads(slot["arguments"]) if slot["arguments"] else {}
            except json.JSONDecodeError:
                # Truncated/malformed args from the provider itself. Don't
                # guess — surface it as an error stop rather than a tool
                # call the loop might try to execute with garbage args.
                return _error_message(
                    f"Model returned malformed tool call arguments for "
                    f"'{slot['name']}': could not parse as JSON."
                )
            content.append(ToolCallContent(id=slot["id"], name=slot["name"], arguments=args))

        stop_reason = _FINISH_REASON_MAP.get(self.finish_reason or "", "error")
        return AssistantMessage(
            content=content,
            stop_reason=stop_reason,          # type: ignore[arg-type]
            error_message=None,
            timestamp=_now_ms(),
        )


def _parse_sse_line(line: str) -> dict[str, Any] | None:
    """Returns the parsed JSON payload of one 'data: {...}' line, or None
    for blank lines / the '[DONE]' sentinel."""
    if not line or not line.startswith("data:"):
        return None
    data = line[len("data:"):].strip()
    if data == "[DONE]":
        return None
    return json.loads(data)


class OpenRouterProvider:
    """Concrete Provider (see ai/model.py for the contract this must satisfy)."""

    def __init__(self, api_key: str | None = None, http_client: httpx.AsyncClient | None = None):
        self._default_api_key = api_key
        self._client = http_client  # optional injection, e.g. for tests

    async def stream(
        self,
        model: Model,
        context: Context,
        options: StreamOptions,
    ) -> AsyncIterator[StreamEvent]:
        api_key = options.api_key or self._default_api_key
        if not api_key:
            yield StreamDone(message=_error_message("No OpenRouter API key provided."))
            return

        payload = _build_payload(model, context, options)
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        acc = _Accumulator()
        yield StreamStart()

        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0))
        try:
            async with client.stream(
                "POST", OPENROUTER_URL, headers=headers, json=payload
            ) as response:
                if response.status_code != 200:
                    body = await response.aread()
                    yield StreamDone(
                        message=_error_message(
                            f"OpenRouter returned HTTP {response.status_code}: "
                            f"{body.decode(errors='replace')[:500]}"
                        )
                    )
                    return

                async for raw_line in response.aiter_lines():
                    if options.cancel is not None and getattr(options.cancel, "is_set", lambda: False)():
                        yield StreamDone(message=_aborted_message())
                        return

                    try:
                        event = _parse_sse_line(raw_line)
                    except json.JSONDecodeError:
                        # One bad line shouldn't kill the whole response;
                        # skip it and keep reading.
                        continue
                    if event is None:
                        continue

                    choices = event.get("choices") or []
                    if not choices:
                        continue
                    choice = choices[0]
                    delta = choice.get("delta") or {}

                    if delta.get("content"):
                        yield TextDelta(delta=delta["content"])
                    for tc in delta.get("tool_calls") or []:
                        fn = tc.get("function") or {}
                        yield ToolCallDelta(
                            id=tc.get("id", ""),
                            name=fn.get("name", ""),
                            arguments_delta=fn.get("arguments", ""),
                        )

                    acc.add_delta(delta)
                    if choice.get("finish_reason"):
                        acc.finish_reason = choice["finish_reason"]

            yield StreamDone(message=acc.finalize())

        except httpx.TimeoutException:
            yield StreamDone(message=_error_message("Request to OpenRouter timed out."))
        except httpx.HTTPError as exc:
            yield StreamDone(message=_error_message(f"HTTP error calling OpenRouter: {exc}"))
        except Exception as exc:  # noqa: BLE001 — last-resort contract enforcement.
            # This except-Exception is deliberate and required by the Provider
            # contract: stream() must never raise. If something truly
            # unexpected happens, it still becomes a normal error message,
            # not a crash of the caller's event loop.
            yield StreamDone(message=_error_message(f"Unexpected provider error: {exc}"))
        finally:
            if owns_client:
                await client.aclose()

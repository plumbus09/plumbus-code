"""
terminal/runner.py — Terminal CLI runner and Ctrl+C interrupt handler.

Owns signal handling for Ctrl+C (SIGINT), kept explicitly distinct from a
SIGKILL crash — a clean interrupt stops the current turn loop gracefully and
preserves accumulated state without terminating the process or requiring crash recovery.
"""

from __future__ import annotations

import asyncio
import signal
import sys
from typing import Any

from rich.console import Console

from agent.loop import run_loop_stream
from agent.types import Message
from ai.model import Model, Provider
from terminal.confirm import TerminalConfirm
from terminal.renderer import TerminalRenderer
from tools.base import ToolContext
from tools.permissions import PermissionPolicy
from tools.registry import ToolRegistry


class CancelToken:
    """Simple thread-safe cancellation token with is_set() method."""

    def __init__(self):
        self._is_set = False

    def set(self) -> None:
        self._is_set = True

    def is_set(self) -> bool:
        return self._is_set


async def run_terminal_session(
    prompt_text: str,
    messages: list[Message],
    system_prompt: str,
    model: Model,
    provider: Provider,
    tools: ToolRegistry,
    api_key: str,
    cwd: str,
    policy: PermissionPolicy | None = None,
    console: Console | None = None,
    auto_approve: bool = False,
    max_turns: int = 20,
) -> list[Message]:
    """
    Runs a terminal session subscribing to run_loop_stream events, rendering output,
    and handling Ctrl+C clean interrupts.
    """
    console = console or Console()
    renderer = TerminalRenderer(console=console)
    confirm_handler = TerminalConfirm(console=console, auto_approve=auto_approve)
    cancel_token = CancelToken()

    tool_context = ToolContext(
        cwd=cwd,
        confirm=confirm_handler.confirm,
        cancel=cancel_token,
        policy=policy or PermissionPolicy(),
    )

    loop = asyncio.get_running_loop()

    def sigint_handler():
        console.print("\n[bold red][Ctrl+C Detected][/bold red] Sending cancel signal...")
        cancel_token.set()

    # Register signal handler for SIGINT if supported
    try:
        loop.add_signal_handler(signal.SIGINT, sigint_handler)
        has_sig_handler = True
    except (NotImplementedError, AttributeError):
        # Windows event loop in some Python versions
        has_sig_handler = False

    history = list(messages)
    try:
        stream = run_loop_stream(
            prompt_text=prompt_text,
            messages=messages,
            system_prompt=system_prompt,
            model=model,
            provider=provider,
            tools=tools,
            api_key=api_key,
            tool_context=tool_context,
            max_turns=max_turns,
        )
        async for event in stream:
            renderer.render_event(event)
            if hasattr(event, "message"):
                history.append(event.message)
            elif hasattr(event, "result_message"):
                history.append(event.result_message)

    except (asyncio.CancelledError, KeyboardInterrupt):
        sigint_handler()

    finally:
        if has_sig_handler:
            try:
                loop.remove_signal_handler(signal.SIGINT)
            except Exception:
                pass

    return history

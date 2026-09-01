"""
terminal/renderer.py — Terminal UI renderer for AgentEvents.
Subscribes to agent/loop.py event stream and renders formatted output,
tool execution blocks, and file diffs using Rich.
"""

from __future__ import annotations

import json
from typing import AsyncIterable

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from agent.types import (
    AgentAssistantMessage,
    AgentEvent,
    AgentLoopAborted,
    AgentLoopDone,
    AgentTextDelta,
    AgentToolCallStarted,
    AgentToolExecutionDone,
    AgentToolExecutionStarted,
    AgentToolUpdate,
    AgentTurnDone,
    AgentTurnStart,
)
from terminal.diff import render_diff


class TerminalRenderer:
    """
    Subscribes to AgentEvent stream and renders live output in the terminal.
    """

    def __init__(self, console: Console | None = None):
        self.console = console or Console()
        self.in_text_stream = False

    async def render_stream(self, events: AsyncIterable[AgentEvent]) -> None:
        """
        Consumes an AgentEvent async iterable and renders events to the terminal console.
        """
        async for event in events:
            self.render_event(event)
        self._flush_stream()

    def render_event(self, event: AgentEvent) -> None:
        if isinstance(event, AgentTurnStart):
            self._flush_stream()
            self.console.print(f"\n[bold blue]─── Turn {event.turn + 1} ───[/bold blue]")

        elif isinstance(event, AgentTextDelta):
            if not self.in_text_stream:
                self.console.print("[bold green]Assistant:[/bold green] ", end="")
                self.in_text_stream = True
            self.console.print(event.delta, end="", highlight=False)

        elif isinstance(event, AgentAssistantMessage):
            self._flush_stream()
            if event.message.error_message:
                self.console.print(
                    f"\n[bold red]Assistant Error:[/bold red] {event.message.error_message}"
                )

        elif isinstance(event, AgentToolCallStarted):
            self._flush_stream()
            args_str = json.dumps(event.arguments, indent=2)
            self.console.print(
                Panel(
                    f"[bold magenta]Arguments:[/bold magenta]\n{args_str}",
                    title=f"Tool Call Requested: [bold yellow]{event.name}[/bold yellow]",
                    border_style="yellow",
                )
            )

        elif isinstance(event, AgentToolExecutionStarted):
            self._flush_stream()
            self.console.print(f"[dim]Executing tool: {event.name}...[/dim]")

        elif isinstance(event, AgentToolUpdate):
            self._flush_stream()
            self.console.print(f"[dim]Tool update ({event.name}): {event.result}[/dim]")

        elif isinstance(event, AgentToolExecutionDone):
            self._flush_stream()
            result_msg = event.result_message
            status_style = "red" if result_msg.is_error else "green"
            status_text = "FAILED" if result_msg.is_error else "SUCCESS"

            # Check if result details contain a file edit diff
            diff_text = None
            if isinstance(result_msg.details, dict) and "diff" in result_msg.details:
                diff_text = result_msg.details["diff"]

            content_text = "\n".join(b.text for b in result_msg.content if b.type == "text")
            self.console.print(
                Panel(
                    content_text,
                    title=f"Tool Result ({event.name}): [{status_style}]{status_text}[/{status_style}]",
                    border_style=status_style,
                )
            )

            if diff_text:
                path = result_msg.details.get("path", "File Change")
                self.console.print(render_diff(diff_text, title=f"File Edit Diff: {path}"))

        elif isinstance(event, AgentTurnDone):
            self._flush_stream()

        elif isinstance(event, AgentLoopDone):
            self._flush_stream()
            self.console.print(
                f"\n[bold blue]─── Turn Sequence Done ({event.stop_reason}) ───[/bold blue]"
            )

        elif isinstance(event, AgentLoopAborted):
            self._flush_stream()
            self.console.print(
                f"\n[bold red][Interrupt][/bold red] Agent loop aborted cleanly ({event.reason})."
            )

    def _flush_stream(self) -> None:
        if self.in_text_stream:
            self.console.print()
            self.in_text_stream = False

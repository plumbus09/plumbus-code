"""
terminal/confirm.py — Interactive implementation for ToolContext.confirm().
"""

from __future__ import annotations

import asyncio

from rich.console import Console
from rich.prompt import Confirm


class TerminalConfirm:
    """
    Interactive confirmation prompt implementation passed to ToolContext.confirm().
    """

    def __init__(self, console: Console | None = None, auto_approve: bool = False):
        self.console = console or Console()
        self.auto_approve = auto_approve

    async def confirm(self, prompt_text: str) -> bool:
        """
        Prompts the user interactively in the terminal.
        """
        if self.auto_approve:
            self.console.print(f"[bold yellow][AUTO-APPROVED][/bold yellow] {prompt_text}")
            return True

        self.console.print(f"\n[bold yellow]Permission Required:[/bold yellow] {prompt_text}")

        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None,
            lambda: Confirm.ask(
                "[bold cyan]Allow execution?[/bold cyan]",
                default=False,
                console=self.console,
            ),
        )
        return result

"""
banner.py — Startup banner for Plumbus-Code terminal agent.
"""

from rich.console import Console
from rich.table import Table
from rich.text import Text


# Compact terminal wordmark.
PLUMBUS_ART = r"""
██████╗ ██╗     ██╗   ██╗███╗   ███╗██████╗ ██╗   ██╗███████╗
██╔══██╗██║     ██║   ██║████╗ ████║██╔══██╗██║   ██║██╔════╝
██████╔╝██║     ██║   ██║██╔████╔██║██████╔╝██║   ██║███████╗
██╔═══╝ ██║     ██║   ██║██║╚██╔╝██║██╔══██╗██║   ██║╚════██║
██║     ███████╗╚██████╔╝██║ ╚═╝ ██║██████╔╝╚██████╔╝███████║
╚═╝     ╚══════╝ ╚═════╝ ╚═╝     ╚═╝╚═════╝  ╚═════╝ ╚══════╝
"""


def _plumbus_logo() -> Text:
    """Create the compact colored Plumbus-Code logo."""

    text = Text()

    lines = PLUMBUS_ART.splitlines()

    for index, line in enumerate(lines):
        if index == 0:
            text.append(line, style="bold #ff8f9c")
        elif index == 1:
            text.append(line, style="bold #f4aaa0")
        else:
            text.append(line, style="bold #efa895")

        if index < len(lines) - 1:
            text.append("\n")

    return text


def _session_info(model_id: str, cwd: str) -> Text:
    """Create the banner's text content."""

    text = Text()

    # Title
    text.append("✳ ", style="bold magenta")
    text.append("plumbus-", style="bold white")
    text.append("code", style="bold magenta")
    text.append(
        "  ·  terminal coding agent",
        style="dim",
    )

    text.append("\n\n")

    # Model
    text.append("model      ", style="dim")
    text.append(model_id, style="bold white")

    text.append("\n")

    # Directory
    text.append("directory  ", style="dim")
    text.append(cwd, style="bold white")

    text.append("\n\n")

    # Status
    text.append("● ", style="bold green")
    text.append("ready", style="bold green")
    text.append("  ·  ", style="dim")

    text.append("type ", style="dim")
    text.append("exit", style="bold cyan")
    text.append(" or ", style="dim")
    text.append("quit", style="bold cyan")
    text.append(" to end the session.", style="dim")

    return text


def print_banner(
    console: Console,
    model_id: str,
    cwd: str,
) -> None:
    """Render the Plumbus-Code startup banner."""

    layout = Table.grid(padding=(0, 3))

    layout.add_column(
        no_wrap=True,
        vertical="top",
    )

    layout.add_column(
        vertical="top",
    )

    layout.add_row(
        _plumbus_logo(),
        _session_info(model_id, cwd),
    )

    console.print()
    console.print(layout)
    console.print()
"""
terminal/diff.py — Rendering syntax-highlighted diffs for file edits.
"""

from __future__ import annotations

from rich.console import ConsoleRenderable
from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text


def render_diff(diff_text: str, title: str = "File Diff") -> ConsoleRenderable:
    """
    Renders a unified diff string with syntax highlighting (green additions,
    red deletions, cyan headers).
    """
    if not diff_text or not diff_text.strip():
        return Text("(no diff content)", style="dim italic")

    syntax = Syntax(
        diff_text,
        lexer="diff",
        theme="ansi_dark",
        line_numbers=False,
        word_wrap=True,
    )
    return Panel(syntax, title=title, border_style="cyan")

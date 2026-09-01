"""
terminal package — Terminal UI, event stream subscription, diff rendering,
interactive confirmation, and Ctrl+C interrupt handling.
"""

from terminal.confirm import TerminalConfirm
from terminal.diff import render_diff
from terminal.renderer import TerminalRenderer
from terminal.runner import CancelToken, run_terminal_session

__all__ = [
    "render_diff",
    "TerminalConfirm",
    "TerminalRenderer",
    "CancelToken",
    "run_terminal_session",
]

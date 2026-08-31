"""
tools/safety.py — minimal path containment, until the real permission gate
(Phase 4: runtime/permissions.py) exists.

This is NOT the permission gate. It's a much smaller, cheaper guarantee:
no tool call can touch a path outside the working directory, full stop,
no policy, no per-tool config, no confirm() prompts. The real gate will
add allow-lists, ask/deny modes, and command filtering for bash on top of
this. Until that exists, this is the only thing standing between a model
and your filesystem outside the project directory.
"""

from __future__ import annotations

from pathlib import Path


class PathEscapeError(ValueError):
    """Raised when a tool argument resolves to a path outside context.cwd."""


def resolve_within_cwd(cwd: str, rel_or_abs_path: str) -> Path:
    """
    Resolves rel_or_abs_path against cwd and raises PathEscapeError if the
    result falls outside cwd — whether via an absolute path pointing
    elsewhere, or a relative path using '../' to climb out.

    Every file-touching tool (read, write, edit, grep, glob) should route
    its path resolution through this function instead of doing the
    is_absolute() check inline.
    """
    base = Path(cwd).resolve()
    candidate = Path(rel_or_abs_path)
    target = candidate if candidate.is_absolute() else base / candidate
    target = target.resolve()

    try:
        target.relative_to(base)
    except ValueError:
        raise PathEscapeError(
            f"Path '{rel_or_abs_path}' resolves to '{target}', which is outside "
            f"the working directory '{base}'. Tool calls may only touch files "
            f"within the project directory."
        )
    return target
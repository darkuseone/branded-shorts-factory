"""Small shared helpers for building FFmpeg command lines."""

from __future__ import annotations

import shlex


def escape_path(value: str) -> str:
    """Quote a command-line fragment for readable log output."""
    return shlex.quote(str(value))

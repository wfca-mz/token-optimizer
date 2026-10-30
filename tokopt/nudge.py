#!/usr/bin/env python3
"""Claude UserPromptSubmit hook: one advisory line when the session context is large.

Reads the hook JSON on stdin ({"transcript_path": ...}). Advisory only: always exits 0.
Context size = last assistant message usage (input + cache read + cache creation).
Wire into ~/.claude/settings.json hooks.UserPromptSubmit as the `tokopt-nudge` command (see README.md).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from tokopt.cli import _open_regular

LIMIT = 150_000
TAIL = 2 * 1024 * 1024  # only the end of a transcript is read


def message(transcript: str) -> str:
    try:
        with _open_regular(Path(transcript)) as f:  # regular files only: a FIFO or device must never block or stream
            f.seek(max(0, f.seek(0, 2) - TAIL))
            tail = f.read(TAIL).decode(errors="replace").splitlines()
    except OSError:
        return ""
    for line in reversed(tail):
        if '"usage"' not in line:
            continue
        try:
            u = json.loads(line)["message"]["usage"]
        except (ValueError, KeyError, TypeError):
            continue  # first tail line may be cut mid-record
        n = sum(u.get(k, 0) for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
        if n > LIMIT:
            return f"[tokopt] context is ~{n // 1000}k tokens; if you are switching tasks, /clear starts fresh and cuts cost."
        return ""
    return ""


def main() -> int:
    """Console entry. A hook must never block the prompt: every failure exits 0 silently."""
    try:
        path = json.loads(sys.stdin.buffer.read(1 << 20)).get("transcript_path", "")
        out = message(path) if path else ""
        if out:
            print(out)
    except Exception:  # noqa: BLE001 - advisory hook, see docstring
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Regression tests for the third review gate. Each guard was seen RED against the previous code."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from test_tokopt import (DAY, claude_session, fake_home, iso, ledger, mcp_names, proc, rows_by_name,  # noqa: F401
                         run, tool_line, write_log, LAUNCHD, make_plugin, age_first_seen, confirm, POLICY)
from tokopt import cli as tokopt

UTC_FMT = "%a %b %e %H:%M:%S %Y"


def fake_ps_lstart(epoch: float):
    """A `ps -o lstart=` that prints in the timezone of the environment it is given (like the real one)."""
    def _run(cmd, env=None):
        if "lstart=" not in cmd:
            return ""
        tz_utc = (env or {}).get("TZ") == "UTC"
        return time.strftime(UTC_FMT, time.gmtime(epoch) if tz_utc else time.localtime(epoch)) + "\n"
    return _run


# ---------- 1: procStart is UTC, ps lstart is local ----------

@pytest.mark.parametrize("tz", ["America/Los_Angeles", "Asia/Kolkata", "UTC"])
def test_claude_procstart_is_utc_and_matches_live_process_in_any_local_timezone(fake_home, monkeypatch, tz):
    monkeypatch.setenv("TZ", tz)
    time.tzset()
    try:
        started = time.time() - 20 * 3600
        monkeypatch.setattr(tokopt, "_run", fake_ps_lstart(started))
        claude_session(fake_home, 5, procStart=time.strftime(UTC_FMT, time.gmtime(started)))
        assert tokopt.real_activity({"pid": 5, "agent": "claude"}) is not None
        claude_session(fake_home, 5, procStart=time.strftime(UTC_FMT, time.gmtime(started + 3600)))
        assert tokopt.real_activity({"pid": 5, "agent": "claude"}) is None  # a different start never matches
    finally:
        monkeypatch.undo()
        time.tzset()

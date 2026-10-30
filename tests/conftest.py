"""Test isolation: no test may read or write the real home.

- An autouse fixture points HOME at a private temp dir for EVERY test and clears every variable that redirects
  agent or tokopt paths (subprocess-based tests inherit it).
- At session start the real tokopt state/config trees are snapshotted; if they differ at the end, the run fails.
"""
from __future__ import annotations

import os
import pwd
from pathlib import Path

import pytest

REAL_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)
WATCHED = (REAL_HOME / ".local/state/tokopt", REAL_HOME / ".config/tokopt")
CLEARED = ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "CLAUDE_CONFIG_DIR", "CODEX_HOME")


def snapshot(roots) -> dict[str, tuple[int, int]]:
    """path -> (mtime_ns, size) of every file under the roots (missing roots are empty)."""
    out: dict[str, tuple[int, int]] = {}
    for root in roots:
        for dirpath, _dirs, files in os.walk(root):
            for name in files:
                p = os.path.join(dirpath, name)
                try:
                    st = os.lstat(p)
                except OSError:
                    continue
                out[p] = (st.st_mtime_ns, st.st_size)
    return out


def changes(before: dict, after: dict) -> list[str]:
    return sorted(f"{'added' if p not in before else 'removed' if p not in after else 'modified'}: {p}"
                  for p in before.keys() | after.keys() if before.get(p) != after.get(p))


_BEFORE = snapshot(WATCHED)


@pytest.fixture(autouse=True)
def _private_home(tmp_path_factory, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path_factory.mktemp("home")))
    for var in CLEARED:
        monkeypatch.delenv(var, raising=False)
    for var in [v for v in os.environ if v.startswith("TOKOPT_")]:
        monkeypatch.delenv(var)


def pytest_sessionfinish(session, exitstatus):
    diff = changes(_BEFORE, snapshot(WATCHED))
    if diff:
        print("\nTESTS TOUCHED THE REAL HOME (tokopt state/config changed during the run):\n  " + "\n  ".join(diff))
        session.exitstatus = 1

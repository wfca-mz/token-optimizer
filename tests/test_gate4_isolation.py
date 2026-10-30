"""The suite never touches the real home (conftest.py): isolation fixture + the snapshot guard itself."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from conftest import REAL_HOME, changes, snapshot
from tokopt import cli as tokopt

ROOT = Path(__file__).resolve().parent.parent


def test_every_test_runs_with_a_private_home_and_no_redirect_variables():
    assert Path(os.environ["HOME"]) != REAL_HOME and str(REAL_HOME) not in os.environ["HOME"]
    for var in ("XDG_STATE_HOME", "CLAUDE_CONFIG_DIR", "CODEX_HOME"):
        assert var not in os.environ
    assert tokopt.state_dir() != REAL_HOME / ".local/state/tokopt"


def test_scan_logs_without_fake_home_writes_into_the_private_home(tmp_path):
    f = tmp_path / "x.jsonl"
    f.write_text("{}\n")
    tokopt.scan_logs("iso", [f], tokopt.CODEX_SCAN)
    assert (tokopt.state_dir() / "scan-iso.json").exists()
    assert not (REAL_HOME / ".local/state/tokopt/scan-iso.json").exists()


def test_subprocesses_inherit_the_private_home():
    out = subprocess.run([sys.executable, "-c", "import os; print(os.environ['HOME'])"], capture_output=True, text=True,
                         cwd=ROOT).stdout.strip()
    assert out == os.environ["HOME"]


def test_snapshot_diff_detects_added_modified_and_removed_files(tmp_path):
    (tmp_path / "a").write_text("1")
    (tmp_path / "b").write_text("1")
    before = snapshot([tmp_path])
    (tmp_path / "a").write_text("22")
    (tmp_path / "b").unlink()
    (tmp_path / "c").write_text("1")
    got = changes(before, snapshot([tmp_path]))
    assert [g.split(":")[0] for g in got] == ["added", "modified", "removed"]
    assert changes(before, before) == []

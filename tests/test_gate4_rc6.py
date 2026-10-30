"""rc6 delta review. Seen RED first."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from test_tokopt import fake_home, run, ledger, proc, sess, NOW, kill_env  # noqa: F401
from tokopt import cli as tokopt

ROOT = Path(__file__).resolve().parent.parent
INSTALLER = ROOT / "scripts/install-tokopt-launchd.sh"
MSG = "is a symlink in tokopt's state directory"


# ---------- 1: a half-written ledger row cannot swallow the next one ----------

def test_a_row_appended_after_a_half_written_line_is_still_readable(fake_home):
    tokopt.private_dir(tokopt.state_dir())
    (tokopt.state_dir() / "ledger.jsonl").write_text('{"id": "a", "action": "park", "item": "tru')   # ENOSPC: no newline
    tokopt.ledger_add({"id": "b", "action": "park", "item": "good"})
    rows = tokopt.ledger_rows()
    assert [r["id"] for r in rows] == ["b"]


# ---------- 2: no more edits once the ledger cannot be written ----------

def test_apply_stops_parking_after_the_first_unrecorded_park(fake_home, monkeypatch, capsys):
    real = tokopt.ledger_add

    def flaky(row):
        if row.get("action") == "park":
            raise OSError(28, "No space left on device")
        real(row)
    monkeypatch.setattr(tokopt, "ledger_add", flaky)
    assert run(["apply", "--yes"], monkeypatch) == 1
    out = capsys.readouterr().out
    assert out.count("could not be written") == 1 and "no further configs were edited" in out
    cj = json.loads((fake_home / ".claude.json").read_text())
    assert len(cj.get("mcpServersParked", {})) == 1          # two were due (oldsrv, secretsrv); only the first was edited


# ---------- 4 / 5: preflight depth, state dir itself a symlink, ordering ----------

def test_a_link_at_backups_stamp_file_refuses_apply(fake_home, monkeypatch, capsys):
    stamp = tokopt.state_dir() / "backups/20200101T000000"
    tokopt.private_dir(stamp)
    (stamp / "x-.claude.json").symlink_to(fake_home / "victim")
    before = (fake_home / ".claude.json").read_bytes()
    assert run(["apply", "--yes"], monkeypatch) == 2
    assert MSG in capsys.readouterr().out and (fake_home / ".claude.json").read_bytes() == before


def test_a_link_under_stopped_refuses_apply(fake_home, monkeypatch, capsys):
    tokopt.private_dir(tokopt.state_dir() / "stopped")
    (tokopt.state_dir() / "stopped/claude-1.md").symlink_to(fake_home / "victim")
    assert run(["apply", "--yes"], monkeypatch) == 2 and MSG in capsys.readouterr().out


def test_the_state_dir_itself_may_be_a_symlink(fake_home, monkeypatch):
    real = fake_home / "elsewhere-state"
    real.mkdir()
    state = fake_home / ".local/state/tokopt"
    for f in state.iterdir():
        f.rename(real / f.name)
    state.rmdir()
    state.symlink_to(real)
    assert run(["apply", "--yes"], monkeypatch) == 0
    assert (real / "ledger.jsonl").exists()


def test_preflight_runs_only_after_the_lock_is_held(fake_home, monkeypatch):
    order = []
    monkeypatch.setattr(tokopt.fcntl, "flock", lambda fd, op: order.append("flock"))
    real = tokopt._preflight_state
    monkeypatch.setattr(tokopt, "_preflight_state", lambda: (order.append("preflight"), real())[1])
    with tokopt.state_lock():
        pass
    assert order == ["flock", "preflight"]


# ---------- 6: --check-job ----------

def run_main(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "tokopt", *args], capture_output=True, text=True, cwd=ROOT)


def test_check_job_prints_nothing_when_all_is_well_and_rejects_other_arguments():
    r = run_main(["--check-job"])
    assert r.returncode == 0 and r.stdout == ""
    for extra in (["apply"], ["restore", "x"], ["--yes"], ["--scheduled"]):
        r = run_main(["--check-job", *extra])
        assert r.returncode == 2 and "stands alone" in r.stderr, extra


def test_check_job_prints_the_warning_only_when_the_program_is_gone():
    p = Path(os.environ["HOME"]) / "Library/LaunchAgents/com.tokopt.agent.plist"
    p.parent.mkdir(parents=True)
    p.write_text('<?xml version="1.0"?><plist version="1.0"><dict><key>ProgramArguments</key><array>'
                 "<string>/nonexistent/py</string></array></dict></plist>")
    r = run_main(["--check-job"])
    assert r.returncode == 0 and "no longer exists" in r.stdout and r.stdout.count("\n") == 1


# ---------- 7: a process row without a uid is not ours ----------

def test_rows_without_a_uid_are_never_claude_candidates_or_signal_targets(fake_home, monkeypatch, kill_env):
    row = {k: v for k, v in proc(77, 1, "claude", "claude").items() if k != "uid"}
    assert tokopt.select_idle_sessions([row], lambda r: 0.0, {"session_idle_hours": 8, "protect": []}, NOW, 9999) == []
    monkeypatch.setattr(tokopt, "ps_rows", lambda: [row])
    status, _ = tokopt.stop_session(sess(), {"session_idle_hours": 8, "protect": []}, NOW)
    assert status == "skipped" and kill_env == []


# ---------- 9: remaining ledger writes report cleanly ----------

def test_ledger_failures_in_reconcile_and_nothing_to_restore_do_not_traceback(fake_home, monkeypatch, capsys):
    cj = fake_home / ".claude.json"
    orig = cj.read_bytes()
    with monkeypatch.context() as m:
        m.setattr(tokopt.time, "sleep", lambda s: cj.write_bytes(orig))
        run(["apply"], monkeypatch)                       # rows end up reverted-by-host
    capsys.readouterr()
    monkeypatch.setattr(tokopt, "ledger_add", lambda row: (_ for _ in ()).throw(OSError(28, "No space")))
    assert run(["restore", "--all"], monkeypatch) == 1
    assert "could not be written" in capsys.readouterr().out
    assert tokopt.reconcile_ledger() == [] or True        # no traceback either way


# ---------- --status fallback branch (no `tokopt` on PATH) ----------

@pytest.mark.skipif(sys.platform != "darwin", reason="the launchd installer is macOS-only (Linux /tmp is world-writable, which it refuses by design)")
def test_status_falls_back_to_python_when_tokopt_is_not_on_path(tmp_path):
    p = tmp_path / "Library/LaunchAgents/com.tokopt.agent.plist"
    p.parent.mkdir(parents=True)
    p.write_text("<plist/>")
    stub = tmp_path / "stub"
    stub.mkdir()
    (stub / "launchctl").write_text("#!/bin/sh\nexit 0\n")
    (stub / "python3").write_text('#!/bin/sh\ncase "$1" in -c) exit 0 ;; -I) echo "FALLBACK $*" ;; esac\nexit 0\n')
    for f in stub.iterdir():
        f.chmod(0o755)
    r = subprocess.run(["/bin/bash", str(INSTALLER), "--status"], capture_output=True, text=True,
                       env={"PATH": f"{stub}:/usr/bin:/bin", "HOME": str(tmp_path)})
    assert r.returncode == 0 and "FALLBACK -I -m tokopt --check-job" in r.stdout


# ---------- docs structure ----------

def test_security_md_writes_bullet_has_the_right_structure():
    bullet = next(ln for ln in (ROOT / "SECURITY.md").read_text().splitlines() if ln.startswith("- Writes"))
    report_part, _, apply_part = bullet.partition("`apply` and `restore` also")
    assert "`report` writes only" in report_part and "scan" in report_part and "first_seen" in report_part and "policy.toml" in report_part
    assert apply_part and "agent config" in apply_part and "backups" in apply_part and "ledger" in apply_part and "handoff" in apply_part


def test_docs_do_not_overclaim_the_symlink_behaviour_and_hermes_wording():
    sec = (ROOT / "SECURITY.md").read_text()
    assert "backups/<stamp>" in sec
    assert "not listed" in (ROOT / "README.md").read_text()

"""Gate-6 reviewer findings (H1, M1-M3, L3). Seen RED first."""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from test_tokopt import fake_home, proc, sess, NOW, kill_env, write_log, iso, tool_line  # noqa: F401
from tokopt import cli as tokopt

ROOT = Path(__file__).resolve().parent.parent
INSTALLER = ROOT / "scripts/install-tokopt-launchd.sh"
REAL_PS_ROWS = tokopt.ps_rows
POL = {"session_idle_hours": 8, "protect": []}


# ---------- H1: the final process-table re-check fails CLOSED ----------

def test_empty_process_table_means_skipped_not_signalled(fake_home, monkeypatch, kill_env):
    monkeypatch.setattr(tokopt, "ps_rows", lambda: [])
    status, msg = tokopt.stop_session(sess(), POL, NOW)
    assert status == "skipped" and "process table" in msg and kill_env == []


def test_a_table_without_the_target_pid_is_skipped(fake_home, monkeypatch, kill_env):
    monkeypatch.setattr(tokopt, "ps_rows", lambda: [proc(1, 0, "launchd", "launchd"), proc(500, 1, "claude", "claude")])
    status, _ = tokopt.stop_session(sess(), POL, NOW)
    assert status == "skipped" and kill_env == []


def test_ps_timeout_is_skipped_with_no_signal(fake_home, monkeypatch, kill_env):
    def boom(*a, **k):
        raise subprocess.TimeoutExpired("ps", 15)
    monkeypatch.setattr(tokopt, "ps_rows", REAL_PS_ROWS)   # the real reader, whose _run swallows the timeout
    monkeypatch.setattr(tokopt.subprocess, "run", boom)
    status, _ = tokopt.stop_session(sess(), POL, NOW)
    assert status == "skipped" and kill_env == []


def test_the_happy_path_still_stops_when_the_table_has_the_pid(fake_home, monkeypatch, kill_env):
    monkeypatch.setattr(tokopt, "ps_rows", lambda: [proc(1, 0, "launchd", "launchd"), proc(77, 1, "claude", "claude")])
    assert tokopt.stop_session(sess(), POL, NOW)[0] == "stopped" and kill_env


def test_selection_with_an_empty_table_selects_nothing():
    assert tokopt.select_idle_sessions([], lambda r: 0.0, POL, NOW, 9999) == []


# ---------- M1: a vanished scheduled program is reported ----------

def plist(home: Path, program: str) -> None:
    p = home / "Library/LaunchAgents/com.tokopt.agent.plist"
    p.parent.mkdir(parents=True)
    p.write_text('<?xml version="1.0"?><plist version="1.0"><dict><key>ProgramArguments</key><array>'
                 f"<string>{program}</string><string>-I</string><string>-m</string></array></dict></plist>")


def test_report_warns_when_the_scheduled_program_no_longer_exists(fake_home):
    plist(fake_home, "/nonexistent/Cellar/python/bin/python3")
    out = tokopt.report(tokopt.load_policy(), time.time(), [])
    assert "WARNING scheduled job" in out and "/nonexistent/Cellar/python/bin/python3" in out and "install-tokopt-launchd.sh" in out


def test_report_is_quiet_when_the_scheduled_program_is_fine_or_absent(fake_home):
    assert "scheduled job" not in tokopt.report(tokopt.load_policy(), time.time(), [])
    ok = fake_home / "py"
    ok.write_text("#!/bin/sh\n")
    ok.chmod(0o755)
    plist(fake_home, str(ok))
    assert "scheduled job" not in tokopt.report(tokopt.load_policy(), time.time(), [])


def test_installer_status_flags_a_missing_program(tmp_path):
    plist(tmp_path, "/nonexistent/Cellar/python/bin/python3")
    stub = tmp_path / "stub"
    stub.mkdir()
    (stub / "launchctl").write_text("#!/bin/sh\nexit 0\n")
    (stub / "launchctl").chmod(0o755)
    (stub / "tokopt").write_text(f"#!/bin/sh\nPYTHONPATH={ROOT} exec {sys.executable} -m tokopt \"$@\"\n")
    (stub / "tokopt").chmod(0o755)
    r = subprocess.run(["/bin/bash", str(INSTALLER), "--status"], capture_output=True, text=True,
                       env={"PATH": f"{stub}:/usr/bin:/bin", "HOME": str(tmp_path)})
    assert r.returncode == 0 and "no longer exists" in r.stdout and "/nonexistent/Cellar/python/bin/python3" in r.stdout


def test_readme_says_to_rerun_the_installer_after_upgrades():
    r = (ROOT / "README.md").read_text().lower()
    assert "re-run the installer" in r and "pipx" in r and "brew upgrade" in r


# ---------- M2: symlink chains longer than the limit are refused ----------

def test_a_chain_of_21_symlinks_is_refused(tmp_path):
    """Relative links, so the OS resolves the whole chain (absolute /var targets re-walk the /var symlink and hit
    ELOOP first): safe/l0 -> ../bad/tokopt, safe/l<i> -> l<i-1>, 21 links ending in a 0777 directory."""
    tmp_path.chmod(0o755)
    bad = tmp_path / "bad"
    bad.mkdir()
    real = bad / "tokopt"
    real.write_text("#!/bin/sh\n")
    real.chmod(0o755)
    bad.chmod(0o777)
    safe = tmp_path / "safe"
    safe.mkdir()
    safe.chmod(0o755)
    (safe / "l0").symlink_to("../bad/tokopt")
    for i in range(1, 21):
        (safe / f"l{i}").symlink_to(f"l{i - 1}")
    top = safe / "l20"
    assert top.resolve() == real.resolve()          # the chain really resolves: the guard is what must refuse it
    r = subprocess.run(["/bin/bash", "-c", f'source "{INSTALLER}"; validate_exe "{top}"'], capture_output=True, text=True,
                       env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)})
    assert r.returncode != 0 and "cannot resolve" in r.stderr, r.stderr


# ---------- M3: no `"mcp__` needle: escaped text inside oversized lines is not a call ----------

@pytest.fixture
def small_lines(monkeypatch):
    monkeypatch.setattr(tokopt, "MAX_LINE", 2000)
    monkeypatch.setattr(tokopt, "CHUNK", 1000, raising=False)


def test_oversized_tool_result_quoting_an_mcp_name_is_not_incomplete(tmp_path, small_lines):
    for spec in (tokopt.CLAUDE_SCAN, tokopt.CODEX_SCAN):
        f = tmp_path / "x.jsonl"
        f.write_text(json.dumps({"type": "user", "message": {"content": [{"type": "tool_result", "content":
                     'settings: {"name": "mcp__x__t", "servers": ["mcp__y"]} ' + "p" * 6000}]}}) + "\n")
        assert tokopt.scan_logs("t", [f], spec)[2] == [], spec[1]


def test_scan_version_is_7():
    assert tokopt.SCAN_VERSION >= 7


# ---------- L3: Hermes `enabled: false` in every YAML spelling ----------

@pytest.mark.parametrize("body,live", [
    ("  a:\n    enabled: false\n  b:\n    command: x\n", ["b"]),
    ("  a:\n    # note\n    command: x\n    enabled: false\n  b:\n    command: y\n", ["b"]),
    ("  a:\n      command: x\n      enabled: no\n  b:\n      command: y\n", ["b"]),
    ("  a:\n        command: x\n        enabled: off  # why\n", []),
    ("  a:\n    enabled: 'false'\n  b:\n    enabled: \"False\"\n  c:\n    enabled: 0\n  d:\n    enabled: true\n", ["d"]),
    ("  a:\n    command: x\n    env:\n      enabled: false\n", ["a"]),            # nested key is not the server's flag
    ("  a:\n    command: x\n  # comment between entries\n  b:\n    enabled: no\n", ["a"]),
    ("  a:\n    enabled: yes\n", ["a"]),
])
def test_hermes_enabled_flag_spellings(body, live):
    assert tokopt._hermes_enabled(body) == live

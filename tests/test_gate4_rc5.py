"""rc5 delta review. Seen RED first."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from test_tokopt import fake_home, proc, sess, NOW, kill_env, run, ledger  # noqa: F401
from tokopt import cli as tokopt

ROOT = Path(__file__).resolve().parent.parent
INSTALLER = ROOT / "scripts/install-tokopt-launchd.sh"
POL = {"session_idle_hours": 8, "protect": []}


# ---------- 1 (HIGH): a planted ledger/state symlink fails closed BEFORE any mutation ----------

def plant(fake_home, name: str) -> Path:
    tokopt.private_dir(tokopt.state_dir())
    target = fake_home / "victim.txt"
    target.write_text("keep\n")
    (tokopt.state_dir() / name).unlink(missing_ok=True)
    (tokopt.state_dir() / name).symlink_to(target)
    return target


@pytest.mark.parametrize("name", ["ledger.jsonl", "first_seen.json", "confirmed"])
def test_apply_with_a_symlinked_state_file_changes_nothing(fake_home, monkeypatch, name):
    cj = fake_home / ".claude.json"
    before = cj.read_bytes()
    target = plant(fake_home, name)
    sent = []
    monkeypatch.setattr(tokopt.os, "kill", lambda pid, sig: sent.append(pid))
    monkeypatch.setattr(tokopt, "idle_sessions", lambda policy, now: [sess(pid=5)])
    assert run(["apply", "--yes"], monkeypatch) != 0
    assert cj.read_bytes() == before and sent == [] and target.read_text() == "keep\n"
    assert not (tokopt.state_dir() / "backups").exists() or not any((tokopt.state_dir() / "backups").rglob("*.json"))


def test_symlinked_backups_dir_is_refused_before_parking(fake_home, monkeypatch):
    cj = fake_home / ".claude.json"
    before = cj.read_bytes()
    tokopt.private_dir(tokopt.state_dir())
    (fake_home / "elsewhere").mkdir()
    (tokopt.state_dir() / "backups").symlink_to(fake_home / "elsewhere")
    assert run(["apply", "--yes"], monkeypatch) != 0
    assert cj.read_bytes() == before and list((fake_home / "elsewhere").iterdir()) == []


def test_restore_also_refuses_a_symlinked_ledger(fake_home, monkeypatch, capsys):
    plant(fake_home, "ledger.jsonl")
    assert run(["restore", "--all"], monkeypatch) == 2
    assert "is a symlink in tokopt's state directory" in capsys.readouterr().out


def test_ledger_rows_never_follows_a_symlink(fake_home):
    target = plant(fake_home, "ledger.jsonl")
    target.write_text('{"id": "x", "action": "park"}\n')
    with pytest.raises(tokopt.Busy):
        tokopt.ledger_rows()


def test_ledger_failure_after_sigterm_is_reported_not_fatal(fake_home, monkeypatch, kill_env):
    monkeypatch.setattr(tokopt, "ps_rows", lambda: [proc(1, 0, "launchd", "launchd"), proc(77, 1, "claude", "claude")])

    def boom(row):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(tokopt, "ledger_add", boom)
    status, msg = tokopt.stop_session(sess(), POL, NOW)
    assert status == "stopped" and kill_env and "ledger" in msg and "WARNING" in msg


def test_ledger_failure_after_a_park_names_the_backup_instead_of_claiming_skipped(fake_home, monkeypatch, capsys):
    real = tokopt.ledger_add
    monkeypatch.setattr(tokopt, "ledger_add", lambda row: (_ for _ in ()).throw(OSError(28, "No space left")) if row.get("action") == "park" else real(row))
    assert run(["apply", "--yes"], monkeypatch) != 0
    out = capsys.readouterr().out
    assert "parked" in out and "ledger row could not be written" in out and "backup" in out and "SKIPPED claude oldsrv" not in out


# ---------- 3: --status uses the one plist parser (plistlib), so XML entities decode ----------

def test_status_decodes_xml_entities_in_the_program_path(tmp_path):
    p = tmp_path / "Library/LaunchAgents/com.tokopt.agent.plist"
    p.parent.mkdir(parents=True)
    p.write_text('<?xml version="1.0"?><plist version="1.0"><dict><key>ProgramArguments</key><array>'
                 "<string>/nonexistent/a&amp;b/python3</string></array></dict></plist>")
    stub = tmp_path / "stub"
    stub.mkdir()
    (stub / "launchctl").write_text("#!/bin/sh\nexit 0\n")
    (stub / "tokopt").write_text(f"#!/bin/sh\nPYTHONPATH={ROOT} exec {sys.executable} -m tokopt \"$@\"\n")
    for f in stub.iterdir():
        f.chmod(0o755)
    r = subprocess.run(["/bin/bash", str(INSTALLER), "--status"], capture_output=True, text=True,
                       env={"PATH": f"{stub}:/usr/bin:/bin", "HOME": str(tmp_path)})
    assert r.returncode == 0 and "no longer exists" in r.stdout and "/nonexistent/a&b/python3" in r.stdout
    assert "&amp;" not in r.stdout


# ---------- 4: bidi / separator controls ----------

BIDI = "\u2028\u2029\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"


def test_mask_and_clean_strip_bidi_and_line_separators():
    assert tokopt.mask("a" + BIDI + "b") == "a" + "?" * len(BIDI) + "b"
    assert tokopt._clean("x" + BIDI + "\n\ty") == "x" + "?" * len(BIDI) + "??y"
    assert tokopt.mask("ok \u00e9\u4e2d tab\tnl\n") == "ok \u00e9\u4e2d tab\tnl\n"        # normal text and tab/newline stay


def test_source_uses_escapes_not_literal_bidi_characters():
    src = (ROOT / "tokopt/cli.py").read_text()
    assert not any(ch in src for ch in BIDI)


# ---------- 5: Hermes parser fails closed when it cannot be sure ----------

@pytest.mark.parametrize("body", [
    "  a:\n    enabled: false\n    enabled: true\n",                    # duplicate keys: last wins in YAML
    "  a:\n    enabled: true\n    enabled: false\n",
    "  a: &anc\n    command: x\n",
    "  a:\n    <<: *base\n    command: x\n",
    "  a:\n    {enabled: false, command: x}\n",
    "  a:\n    \"enabled\": false\n    command: x\n",
    "  a:\n    command: x\n    env: *e\n",
])
def test_hermes_unsure_entries_are_not_candidates(body):
    assert tokopt._hermes_enabled(body) == []


def test_hermes_plain_entries_are_still_listed():
    assert tokopt._hermes_enabled("  a:\n    command: x\n  b:\n    enabled: true\n") == ["a", "b"]


# ---------- 6: the final check also verifies the row is OUR claude ----------

def test_final_check_requires_a_claude_process_of_our_uid(fake_home, monkeypatch, kill_env):
    me = os.getuid()
    for comm, uid in (("python3", me), ("claude", me + 1)):
        monkeypatch.setattr(tokopt, "ps_rows", lambda comm=comm, uid=uid: [{**proc(77, 1, comm, comm), "uid": uid}])
        status, _ = tokopt.stop_session(sess(), POL, NOW)
        assert status == "skipped" and kill_env == [], (comm, uid)
    monkeypatch.setattr(tokopt, "ps_rows", lambda: [{**proc(77, 1, "/usr/local/bin/claude", "claude"), "uid": me}])
    assert tokopt.stop_session(sess(), POL, NOW)[0] == "stopped"

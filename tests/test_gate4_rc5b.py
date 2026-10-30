"""rc5 security gate: B1 (report never crashes on a bad plist), B2 (docs claims tied to behaviour). Seen RED first."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from test_tokopt import fake_home, run  # noqa: F401
from tokopt import cli as tokopt

ROOT = Path(__file__).resolve().parent.parent
PLIST = "Library/LaunchAgents/com.tokopt.agent.plist"


def report_proc(timeout: float = 30) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "tokopt", "report"], capture_output=True, text=True, timeout=timeout, cwd=ROOT)


def plist_at(home: Path) -> Path:
    p = home / PLIST
    p.parent.mkdir(parents=True)
    return p


# ---------- B1: whatever sits at the plist path, report completes ----------

@pytest.mark.parametrize("content", [
    b'<?xml version="1.0"?><plist version="1.0"><dict><key>ProgramArguments</key><array><string>/x',          # truncated
    b'<?xml version="1.0"?><plist version="1.0">' + b"<array>" * 5000 + b"</array>" * 5000 + b"</plist>",     # deeply nested
    b"",                                                                                                      # empty
    bytes(range(256)) * 8,                                                                                    # binary garbage
    b"bplist00" + b"\xff" * 64,                                                                               # corrupt binary plist
])
def test_report_survives_a_bad_plist(tmp_path, content):
    plist_at(Path(os.environ["HOME"])).write_bytes(content)
    r = report_proc()
    assert r.returncode == 0 and r.stdout.startswith("item") and "Traceback" not in r.stderr, r.stderr[-300:]


def test_report_survives_a_fifo_at_the_plist_path():
    os.mkfifo(plist_at(Path(os.environ["HOME"])))
    r = report_proc(timeout=20)
    assert r.returncode == 0 and "Traceback" not in r.stderr


def test_the_first_apply_gate_survives_a_bad_plist(fake_home, monkeypatch):
    (fake_home / PLIST).parent.mkdir(parents=True)
    (fake_home / PLIST).write_bytes(b"<plist><dict><key>ProgramArguments</key><array><string>/x")
    (tokopt.state_dir() / "confirmed").unlink()
    assert run(["apply"], monkeypatch) == 2          # needs --yes: prints the plan (report) and stops, no traceback


# ---------- B2: every claim in SECURITY.md has a behavioural test ----------

def test_state_files_are_never_read_through_a_symlink(fake_home):
    tokopt.private_dir(tokopt.state_dir())
    real = fake_home / "elsewhere.json"
    real.write_text(json.dumps({"claude:mcp:oldsrv": 1.0}))
    for name in ("first_seen.json", "confirmed", "scan-claude.json"):
        (tokopt.state_dir() / name).unlink(missing_ok=True)
        (tokopt.state_dir() / name).symlink_to(real)
        assert tokopt._read_state_json(tokopt.state_dir() / name, "DEFAULT") == "DEFAULT", name
        (tokopt.state_dir() / name).unlink()
    backup = tokopt.state_dir() / "bk"
    backup.symlink_to(real)
    with pytest.raises(OSError):
        tokopt._state_bytes(backup)


def test_state_files_are_never_written_through_a_symlink(fake_home):
    tokopt.private_dir(tokopt.state_dir())
    victim = fake_home / "victim.txt"
    victim.write_text("keep\n")
    for name in ("first_seen.json", "confirmed", "scan-claude.json"):
        (tokopt.state_dir() / name).unlink(missing_ok=True)
        (tokopt.state_dir() / name).symlink_to(victim)
    tokopt.plan(tokopt.load_policy(), time.time())                    # writes first_seen and the scan cache
    tokopt.atomic_write(tokopt.state_dir() / "confirmed", b"{}", follow=False)
    assert victim.read_text() == "keep\n"
    assert not (tokopt.state_dir() / "first_seen.json").is_symlink()   # the planted link was replaced, not followed


def test_agent_config_files_still_follow_symlinks(fake_home):
    real = fake_home / "dotfiles.json"
    real.write_text("{}")
    link = fake_home / "link.json"
    link.symlink_to(real)
    tokopt.atomic_write(link, b'{"a": 1}')
    assert link.is_symlink() and real.read_text() == '{"a": 1}'


def test_report_writes_only_tokopt_own_state_and_never_agent_configs(fake_home):
    def digest(p: Path) -> str:
        return hashlib.sha256(p.read_bytes()).hexdigest()
    cfg = [fake_home / ".claude.json", fake_home / ".claude/projects/-p/a.jsonl"]
    before = [digest(p) for p in cfg]
    before_ledger = (tokopt.state_dir() / "ledger.jsonl").exists()
    tokopt.report(tokopt.load_policy(), time.time(), [])
    assert [digest(p) for p in cfg] == before and (tokopt.state_dir() / "ledger.jsonl").exists() == before_ledger
    assert (tokopt.state_dir() / "first_seen.json").exists()          # ...but it does write its own state


def test_names_cannot_forge_report_lines_and_c1_controls_are_stripped(fake_home):
    cj = fake_home / ".claude.json"
    d = json.loads(cj.read_text())
    d["mcpServers"]["evil\nPARK: forged line\t\x85x"] = {"command": "x"}
    cj.write_text(json.dumps(d, indent=2) + "\n")
    out = tokopt.report(tokopt.load_policy(), time.time(), [])
    assert [ln for ln in out.splitlines() if "forged" in ln].__len__() == 1
    assert not any(ln.startswith("PARK") for ln in out.splitlines()) and "\x85" not in out
    assert tokopt._clean("a\x85b\x9fc\u202ed") == "a?b?c?d"


def test_docs_say_what_the_code_does():
    sec = (ROOT / "SECURITY.md").read_text()
    low = sec.lower()
    assert "never opened through a symlink" not in low
    assert "`report` writes only tokopt's own state" in sec
    assert "follow" in low and "dotfile" in low                       # config files deliberately follow symlinks
    readme = (ROOT / "README.md").read_text().lower()
    for text in (low, readme):
        assert "pip install --user" not in text and "pip --user" not in text
    assert "pipx" in readme and "venv" in readme

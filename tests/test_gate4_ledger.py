"""Gate 4: restore of host-reverted rows, ledger drift, robustness of state files. Seen RED first."""
from __future__ import annotations

import errno
import json
import re
from pathlib import Path

import pytest

from test_tokopt import (ROOT, age_first_seen, confirm, fake_home, ledger, rows_by_name, run)  # noqa: F401
from tokopt import cli as tokopt


def apply_with_host_revert(fake_home, monkeypatch):
    """A running claude rewrites ~/.claude.json with its own copy right after the park."""
    cj = fake_home / ".claude.json"
    orig = cj.read_bytes()
    with monkeypatch.context() as m:
        m.setattr(tokopt.time, "sleep", lambda s: cj.write_bytes(orig))
        assert run(["apply"], monkeypatch) == 1
    return cj, orig


# ---------- 8: restoring a row the host already reverted ----------

def test_restore_of_a_reverted_by_host_row_says_nothing_to_restore_and_keeps_the_evidence(fake_home, monkeypatch, capsys):
    cj, orig = apply_with_host_revert(fake_home, monkeypatch)
    backups = [r["after"]["backup"] for r in ledger(fake_home) if r["action"] == "park"]
    assert backups and all(re.search(r"/backups/", b) for b in backups)
    capsys.readouterr()
    assert run(["restore", "--all"], monkeypatch) == 0
    out = capsys.readouterr().out
    assert "nothing to restore (host already reverted)" in out and "restored " not in out and "REFUSED" not in out
    assert cj.read_bytes() == orig
    for b in backups:  # the pre-park copy is evidence of what happened: restore must not delete it
        assert Path(b).exists(), b
    marks = [r for r in ledger(fake_home) if r["action"] == "restore"]
    assert marks and {r["status"] for r in marks} == {"nothing-to-restore"}
    run(["restore", "--all"], monkeypatch)  # consumed: a second restore finds nothing
    assert "nothing to restore for --all" in capsys.readouterr().out


def test_restore_of_a_reverted_row_whose_server_is_still_parked_really_restores(fake_home, monkeypatch, capsys):
    run(["apply"], monkeypatch)
    rows = [r for r in ledger(fake_home) if r["action"] == "park"]
    p = fake_home / ".local/state/tokopt/ledger.jsonl"
    p.write_text("".join(json.dumps({**r, "status": "reverted-by-host"}) + "\n" for r in rows))  # mislabelled
    capsys.readouterr()
    assert run(["restore", "--all"], monkeypatch) == 0
    assert "restored claude mcp oldsrv" in capsys.readouterr().out
    assert "oldsrv" in json.loads((fake_home / ".claude.json").read_text())["mcpServers"]


# ---------- 9: ledger drift ----------

def test_apply_reclassifies_parks_the_host_has_since_undone(fake_home, monkeypatch, capsys):
    run(["apply"], monkeypatch)
    first = {r["id"] for r in ledger(fake_home) if r["action"] == "park"}
    cj = fake_home / ".claude.json"
    data = json.loads(cj.read_text())
    data["mcpServers"]["oldsrv"] = data["mcpServersParked"].pop("oldsrv")   # claude put it back while running
    cj.write_text(json.dumps(data, indent=2) + "\n")
    capsys.readouterr()
    assert run(["apply"], monkeypatch) == 0
    out = capsys.readouterr().out
    assert "live again" in out and "oldsrv" in out
    drift = [r for r in ledger(fake_home) if r["action"] == "status"]
    assert len(drift) == 1 and drift[0]["status"] == "reverted-by-host" and drift[0]["id"] in first
    # the first oldsrv row is now reverted; the secretsrv row still parked; restore tells them apart
    capsys.readouterr()
    assert run(["restore", "--all"], monkeypatch) == 0
    out = capsys.readouterr().out
    assert "nothing to restore (host already reverted) claude mcp oldsrv" in out
    assert "restored claude mcp secretsrv" in out


def test_apply_leaves_parks_that_are_still_in_place_alone(fake_home, monkeypatch):
    run(["apply"], monkeypatch)
    n = len(ledger(fake_home))
    assert run(["apply"], monkeypatch) == 0
    assert len(ledger(fake_home)) == n and not any(r["action"] == "status" for r in ledger(fake_home))


# ---------- 10: smaller robustness items ----------

def test_codex_header_with_trailing_comment_is_parked_and_unparked():
    src = '[mcp_servers.x] # my server\ncommand = "y"\n'
    out = tokopt.toml_park(src, "x")
    assert out == '[mcp_servers.x] # my server\nenabled = false\ncommand = "y"\n'
    assert tokopt.toml_unpark(out, "x").count("enabled = true") == 1


@pytest.mark.parametrize("junk", ["[1, 2]", '"text"', "42", '{"codex:mcp:x": "yesterday", "claude:mcp:oldsrv": true}'])
def test_first_seen_of_the_wrong_shape_is_treated_as_empty_with_a_warning(fake_home, capsys, junk):
    (tokopt.state_dir() / "first_seen.json").write_text(junk)
    rows = rows_by_name()
    assert rows["oldsrv"][0] in ("park", "keep")
    assert "first_seen.json" in capsys.readouterr().err
    assert isinstance(json.loads((tokopt.state_dir() / "first_seen.json").read_text()), dict)


def test_state_lock_degrades_with_a_clear_message_on_filesystems_without_flock(fake_home, monkeypatch, capsys):
    def nolock(fd, op):
        raise OSError(errno.ENOLCK, "No locks available")
    monkeypatch.setattr(tokopt.fcntl, "flock", nolock)
    with tokopt.state_lock():
        pass
    err = capsys.readouterr().err
    assert "cannot lock" in err and "without the lock" in err
    assert run(["apply"], monkeypatch) == 0


def test_state_lock_still_reports_busy_when_another_run_holds_it(fake_home, monkeypatch):
    def busy(fd, op):
        raise BlockingIOError(errno.EWOULDBLOCK, "busy")
    monkeypatch.setattr(tokopt.fcntl, "flock", busy)
    with pytest.raises(tokopt.Busy):
        with tokopt.state_lock():
            pass


def test_policy_file_is_created_0600(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    tokopt.load_policy()
    assert (tmp_path / ".config/tokopt/policy.toml").stat().st_mode & 0o777 == 0o600


def test_ci_runs_pyright_on_the_package():
    assert re.search(r"(?m)^\s*- run: pyright tokopt\s*$", (ROOT / ".github/workflows/ci.yml").read_text())


def test_security_md_notes_the_spoofable_session_file_as_out_of_scope():
    t = (ROOT / "SECURITY.md").read_text()
    assert "~/.claude/sessions/<pid>.json" in t and "out of scope" in t.lower()


# ---------- 11: README states what is automated and the real hit rate ----------

def test_readme_states_automation_scope_and_the_idle_session_hit_rate():
    r = (ROOT / "README.md").read_text()
    low = r.lower()
    for phrase in ("## what is automated", "advisory", "interactive cli sessions", "status `idle`", "npm exec",
                   "realistic", "hit rate", "sh -c", "nothing to restore (host already reverted)"):
        assert phrase in low, phrase

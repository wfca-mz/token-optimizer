"""Gate 3: restore/park edge cases (items 2, 5, 15)."""
from __future__ import annotations

import fcntl
import json
import os
import time
from pathlib import Path

import pytest

from test_tokopt import DAY, fake_home, ledger, mcp_names, run  # noqa: F401
from tokopt import cli as tokopt


# ---------- 2: restore after the backup was pruned ----------

def test_restore_succeeds_after_backup_pruned_by_a_later_apply(fake_home, monkeypatch, capsys):
    run(["apply"], monkeypatch)
    bdir = fake_home / ".local/state/tokopt/backups"
    old = int((time.time() - 40 * DAY) * 1e9)
    for f in bdir.glob("*/*"):
        os.utime(f, ns=(old, old))
    run(["apply"], monkeypatch)  # prunes
    assert not list(bdir.glob("*/*"))
    capsys.readouterr()
    assert run(["restore", "--all"], monkeypatch) == 0
    assert "REFUSED" not in capsys.readouterr().out
    assert {"oldsrv", "secretsrv"} <= mcp_names(fake_home)
    assert "mcpServersParked" not in json.loads((fake_home / ".claude.json").read_text())


def test_byte_restore_with_corrupt_backup_falls_back_to_the_semantic_inverse(fake_home, monkeypatch):
    run(["apply"], monkeypatch)
    for r in ledger(fake_home):
        Path(r["after"]["backup"]).write_bytes(b"{}")
    assert run(["restore", "--all"], monkeypatch) == 0
    assert "oldsrv" in mcp_names(fake_home)


# ---------- 5: a server present in BOTH mcpServers and mcpServersParked ----------

BOTH = json.dumps({"mcpServers": {"a": {"v": 2}}, "mcpServersParked": {"a": {"v": 1}}}, indent=2) + "\n"


def test_json_unpark_refuses_to_drop_the_parked_copy():
    with pytest.raises(ValueError, match="both"):
        tokopt.json_unpark(BOTH, "a")


def test_still_parked_is_false_when_host_put_the_server_back_alongside_the_parked_copy():
    after = json.dumps({"mcpServers": {}, "mcpServersParked": {"a": {"v": 1}}}, indent=2).encode() + b"\n"
    assert tokopt._still_parked("json", after, after, "a") is True
    assert tokopt._still_parked("json", BOTH.encode(), after, "a") is False


def test_yaml_unpark_refuses_when_server_is_in_both_blocks():
    src = "mcp_servers:\n  a:\n    url: u\n\nmcp_servers_parked:\n  a:\n    url: v\n"
    with pytest.raises(ValueError, match="both"):
        tokopt.yaml_unpark(src, "a")


def test_park_records_reverted_by_host_when_host_readds_server_beside_parked_copy(fake_home, monkeypatch, capsys):
    cj = fake_home / ".claude.json"

    def host(s):
        d = json.loads(cj.read_text())
        d["mcpServers"]["oldsrv"] = {"command": "x"}
        cj.write_text(json.dumps(d, indent=2) + "\n")
    monkeypatch.setattr(tokopt.time, "sleep", host)
    assert run(["apply"], monkeypatch) == 1
    assert "reverted by the running agent" in capsys.readouterr().out


def test_restore_never_deletes_the_parked_copy(fake_home, monkeypatch, capsys):
    run(["apply"], monkeypatch)
    cj = fake_home / ".claude.json"
    d = json.loads(cj.read_text())
    d["mcpServers"]["oldsrv"] = {"command": "host-wrote-this"}
    cj.write_text(json.dumps(d, indent=2) + "\n")
    run(["restore", "--all"], monkeypatch)
    assert "REFUSED" in capsys.readouterr().out
    assert "oldsrv" in json.loads(cj.read_text())["mcpServersParked"]


# ---------- 15: byte-exact restore shares edit_file's race check; one run at a time ----------

def test_byte_exact_restore_uses_the_same_race_check_as_edit_file(fake_home, monkeypatch, capsys):
    run(["apply"], monkeypatch)
    cj = fake_home / ".claude.json"
    parked = cj.read_bytes()
    n = iter(range(1, 100))
    monkeypatch.setattr(tokopt, "_sig", lambda p: (next(n), 0))  # the live process keeps touching the file
    assert run(["restore", "--all"], monkeypatch) == 1
    assert "changed" in capsys.readouterr().out and cj.read_bytes() == parked


def test_apply_and_restore_refuse_to_interleave_with_another_run(fake_home, monkeypatch, capsys):
    run(["apply"], monkeypatch)
    before = (fake_home / ".claude.json").read_bytes()
    with open(tokopt.state_dir() / "lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        assert run(["restore", "--all"], monkeypatch) == 2
        assert run(["apply"], monkeypatch) == 2
    assert "another tokopt" in capsys.readouterr().out
    assert (fake_home / ".claude.json").read_bytes() == before

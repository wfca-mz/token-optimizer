"""Gate 4: regressions on REAL captured shapes (tests/fixtures/real_*). Each guard was seen RED first.

Class lesson from earlier gates: tests built on invented shapes hid real-format bugs, so every format-dependent
test here asserts on a fixture captured read-only from the real files (names/content sanitised, structure kept).
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from test_tokopt import (DAY, ROOT, add_server, age_first_seen, confirm, fake_home, iso, ledger, proc,  # noqa: F401
                         rows_by_name, run, tool_line, write_log)
from tokopt import cli as tokopt

FIX = Path(__file__).resolve().parent / "fixtures"


def keys(fn, obj) -> list[str]:
    raw = json.dumps(obj).encode()
    return fn(json.loads(raw), raw)


# ---------- 1: Codex namespaced calls (real shape: namespace WITHOUT trailing `__`) ----------

def _codex_real_records() -> list[dict]:
    return [json.loads(x) for x in (FIX / "real_codex_namespaced_calls.jsonl").read_text().splitlines()]


def test_codex_real_namespaced_function_call_yields_the_server_key():
    recs = _codex_real_records()
    assert recs[0]["payload"]["namespace"] == "mcp__fixture_srv" and recs[0]["payload"]["name"] == "js"
    assert [keys(tokopt.codex_keys, r) for r in recs] == [["mcp:fixture_srv"], [], []]  # collaboration/clock are not MCP


def test_codex_namespace_variants_all_resolve_to_the_server():
    def ns(n, name="t"):
        return {"payload": {"type": "function_call", "namespace": n, "name": name}}
    assert keys(tokopt.codex_keys, ns("mcp__memory-srv__")) == ["mcp:memory_srv"]      # trailing __ (older rollouts)
    assert keys(tokopt.codex_keys, ns("mcp__a__b__")) == ["mcp:a"]
    assert keys(tokopt.codex_keys, ns("", "mcp__flat__tool")) == ["mcp:flat"]          # flat name, no namespace
    assert keys(tokopt.codex_keys, ns("clock", "mcp__flat__tool")) == ["mcp:flat"]     # non-mcp namespace: flat name wins
    assert keys(tokopt.codex_keys, ns("collaboration", "spawn_agent")) == []


def test_server_used_one_day_ago_in_the_real_shape_is_kept_not_parked(fake_home):
    (fake_home / ".codex").mkdir()
    (fake_home / ".codex/config.toml").write_text('[mcp_servers.fixture_srv]\ncommand = "x"\n\n[mcp_servers.other]\ncommand = "y"\n')
    lines = []
    for r in _codex_real_records():
        r["timestamp"] = iso(1)
        lines.append(json.dumps(r))
    write_log(fake_home / ".codex/sessions/2026/10/01/rollout-a.jsonl", lines, 40)
    tokopt.plan(tokopt.load_policy(), time.time())
    for s in ("fixture_srv", "other"):
        age_first_seen(f"codex:mcp:{s}", 40)  # long-known servers: only real use evidence can keep them
    act, why = rows_by_name()["fixture_srv"]
    assert act == "keep" and "used 1d ago" in why, (act, why)
    assert rows_by_name()["other"][0] == "park"


def test_scan_version_bumped_so_cached_wrong_keys_rebuild():
    assert tokopt.SCAN_VERSION >= 6


# ---------- 2: Hermes `mcp_servers:` block must end at the next top-level key ----------

POL4 = ('mcp_idle_days = 28\nskill_idle_days = 28\nsession_idle_hours = 8\n'
        'protect = ["alpha", "remote", "memory", "node_repl", "computer", "disabled"]\ncheap_agents = []\n')


@pytest.fixture
def hermes_home(fake_home):
    h = fake_home / ".hermes"
    h.mkdir()
    shutil.copy(FIX / "real_hermes_config.yaml", h / "config.yaml")
    con = sqlite3.connect(h / "state.db")
    con.execute("create table messages(timestamp real, tool_name text)")
    con.execute("insert into messages values (?, ?)", (time.time() - 40 * DAY, "mcp_alpha_x"))
    con.commit()
    con.close()
    (fake_home / ".config/tokopt/policy.toml").write_text(POL4)
    confirm()
    return h / "config.yaml"


def hermes_names() -> list[str]:
    sc = tokopt.scan_hermes()
    assert sc is not None
    return [i.name for i in sc.items]


def test_hermes_scan_lists_exactly_the_live_servers_of_the_real_layout(hermes_home):
    assert hermes_names() == ["alpha", "rex", "remote", "memory-srv", "node_repl", "computer"]


def test_hermes_parked_entry_and_unrelated_later_keys_are_not_live_servers(hermes_home, monkeypatch):
    tokopt.plan(tokopt.load_policy(), time.time())
    age_first_seen("hermes:mcp:rex", 40)
    assert run(["apply"], monkeypatch) == 0
    text = hermes_home.read_text()
    assert "mcp_servers_parked:\n  rex:" in text
    names = hermes_names()
    assert "rex" not in names and "fake_child" not in names and "second_child" not in names


def test_second_hermes_apply_exits_zero_after_a_park(hermes_home, monkeypatch, capsys):
    tokopt.plan(tokopt.load_policy(), time.time())
    age_first_seen("hermes:mcp:rex", 40)
    assert run(["apply", "--yes"], monkeypatch) == 0
    capsys.readouterr()
    assert run(["apply", "--yes"], monkeypatch) == 0, capsys.readouterr().out


# ---------- 3: nudge must never hang on a FIFO / device ----------

def _nudge(path: str, timeout: float = 10) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "tokopt.nudge"], input=json.dumps({"transcript_path": path}), text=True,
                          capture_output=True, timeout=timeout, cwd=ROOT)


def test_nudge_does_not_hang_on_a_fifo_with_no_writer(tmp_path):
    fifo = tmp_path / "t.jsonl"
    os.mkfifo(fifo)
    r = _nudge(str(fifo))
    assert r.returncode == 0 and r.stdout == ""


def test_nudge_does_not_hang_on_dev_zero():
    r = _nudge("/dev/zero")
    assert r.returncode == 0 and r.stdout == ""


def test_nudge_refuses_a_symlinked_transcript(tmp_path):
    real = tmp_path / "real.jsonl"
    real.write_text(json.dumps({"message": {"usage": {"input_tokens": 999999}}}) + "\n")
    link = tmp_path / "link.jsonl"
    link.symlink_to(real)
    assert _nudge(str(link)).stdout == ""
    assert "/clear" in _nudge(str(real)).stdout


def test_nudge_reads_only_the_tail(tmp_path):
    from tokopt import nudge
    big = {"message": {"usage": {"input_tokens": 1, "cache_read_input_tokens": 500_000}}}
    f = tmp_path / "t.jsonl"
    pad = json.dumps({"type": "user", "text": "x" * 1000}) + "\n"
    f.write_text(json.dumps(big) + "\n" + pad * (3 * 1024))   # usage line is > 2 MiB from the end
    assert nudge.message(str(f)) == ""
    f.write_text(pad * (3 * 1024) + json.dumps(big) + "\n")
    assert "/clear" in nudge.message(str(f))


def test_nudge_does_not_hang_on_an_endless_stdin():
    with open("/dev/zero", "rb") as z:
        r = subprocess.run([sys.executable, "-m", "tokopt.nudge"], stdin=z, capture_output=True, timeout=10, cwd=ROOT)
    assert r.returncode == 0 and r.stdout == b""
    prod = subprocess.Popen(["/usr/bin/yes"], stdout=subprocess.PIPE)   # a real endless producer on a pipe
    try:
        r = subprocess.run([sys.executable, "-m", "tokopt.nudge"], stdin=prod.stdout, capture_output=True, timeout=10, cwd=ROOT)
    finally:
        prod.kill()
        prod.wait()
    assert r.returncode == 0 and r.stdout == b""

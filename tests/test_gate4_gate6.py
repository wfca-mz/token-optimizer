"""Gate-6 findings N1-N6. Seen RED first. Timing checks are ratios (time(4n)/time(n)), never wall-clock bounds."""
from __future__ import annotations

import json
import os
import time

import pytest

from test_tokopt import fake_home, run, ledger, ROOT  # noqa: F401
from tokopt import cli as tokopt


def scaling(fn, small: int, big: int, reps: int = 5, loops: int = 20) -> float:
    def best(n: int) -> float:
        runs = []
        for _ in range(reps):
            t0 = time.perf_counter()
            for _ in range(loops):
                fn(n)
            runs.append(time.perf_counter() - t0)
        return min(runs)
    return best(big) / best(small)


# ---------- N1: ${VAR} matching is linear, no regex ----------

def test_placeholder_matching_scales_linearly_on_the_polynomial_backtracking_shape():
    cfg = "${V0}x${V1}x${V2}xEND"
    assert scaling(lambda n: tokopt._tok_eq(cfg, "x" * n), 500, 2000, reps=3, loops=1) < 8


@pytest.mark.parametrize("cfg,got,ok", [
    ("${V0}x${V1}x${V2}xEND", "ax" + "b" * 3 + "x" + "c" + "xEND", True),
    ("${A}/server.js", "/opt/r/server.js", True),
    ("${A}/server.js", "/server.js", False),                                    # empty gap
    ("${A}/server.js", "/opt/r/server.jsx", False),
    ("${A}/server.js", "/op t/server.js", False),                               # whitespace in the gap
    ("K=${K}", "K=abc", True),
    ("K=${K}", "K=", False),
    ("a${X}b${Y}c", "aXXbYYc", True),
    ("a${X}b${Y}c", "abc", False),
    ("a${X}b${Y}c", "aXbc", False),
    ("a${X}b${Y}c", "aXb Yc", False),
    ("a${X}b${Y}c", "aXbbYc", True),
    ("${X}", "anything", False),
    ("/${X}", "/tmp/evil", False),
    ("-${X}", "-x", False),
])
def test_placeholder_semantics(cfg, got, ok):
    assert tokopt._tok_eq(cfg, got) is ok


# ---------- N2: control characters never reach the terminal ----------

def test_mask_replaces_control_characters_but_keeps_tab_and_newline():
    assert tokopt.mask("a\x1b[31mred\x07\x00\x7f\x9fz\tq\nw") == "a?[31mred????z\tq\nw"


def test_escape_sequences_in_a_server_name_are_neutralised_in_report_and_apply(fake_home, monkeypatch, capsys):
    evil = "old\x1b[2Jsrv"
    cj = fake_home / ".claude.json"
    d = json.loads(cj.read_text())
    d["mcpServers"][evil] = {"command": "x"}
    cj.write_text(json.dumps(d, indent=2) + "\n")
    tokopt.plan(tokopt.load_policy(), time.time())
    fs = tokopt.state_dir() / "first_seen.json"
    seen = json.loads(fs.read_text())
    fs.write_text(json.dumps({k: time.time() - 40 * 86400 for k in seen}))
    assert "\x1b" not in tokopt.report(tokopt.load_policy(), time.time(), [])
    run(["apply"], monkeypatch)
    out = capsys.readouterr().out
    assert "parked claude mcp old?[2Jsrv" in out and "\x1b" not in out


# ---------- N3: state files are never opened through a symlink ----------

def test_ledger_is_not_written_through_a_planted_symlink(fake_home):
    target = fake_home / "victim.txt"
    target.write_text("keep\n")
    tokopt.private_dir(tokopt.state_dir())
    (tokopt.state_dir() / "ledger.jsonl").symlink_to(target)
    with pytest.raises(OSError):
        tokopt.ledger_add({"id": "x", "action": "park"})
    assert target.read_text() == "keep\n"


def test_lock_file_symlink_is_refused_with_a_clear_message(fake_home, monkeypatch, capsys):
    tokopt.private_dir(tokopt.state_dir())
    (tokopt.state_dir() / "lock").symlink_to(fake_home / "victim.txt")
    assert run(["apply", "--yes"], monkeypatch) == 2
    assert "lock" in capsys.readouterr().out


# ---------- N4: the plist runs python isolated ----------

def test_plist_python_arguments_start_with_dash_I():
    s = (ROOT / "scripts/install-tokopt-launchd.sh").read_text()
    assert 'set -- "${py}" -I -m tokopt apply --scheduled' in s


# ---------- N5: the YAML block header regex is linear ----------

def test_yaml_header_match_scales_linearly_on_trailing_whitespace():
    assert scaling(lambda n: tokopt._yaml_top(["mcp_servers:" + " " * n + "x"], "mcp_servers"), 4000, 16000, reps=3, loops=1) < 8
    assert tokopt._yaml_top(["mcp_servers:   ", "  a:"], "mcp_servers") == (0, 2)
    assert tokopt._yaml_top(["mcp_servers: {}  ", "z: 1"], "mcp_servers") == (0, 1)


# ---------- N6: docs ----------

def test_docs_state_the_real_ps_and_write_scope_and_limits():
    sec = (ROOT / "SECURITY.md").read_text()
    low = sec.lower()
    assert "your own uid only" not in low
    for phrase in ("every uid", "candidate", "first_seen.json", "confirmed", "stopped/", "same-user", "ledger", "scan cache"):
        assert phrase in low, phrase
    assert "your own uid only" not in (ROOT / "CHANGELOG.md").read_text().lower()
    r = (ROOT / "README.md").read_text()
    assert "whole command line must still match" in r

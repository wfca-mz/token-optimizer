"""Gate 4: process graph, idle-child detection on real shapes, named unscannable logs. Seen RED first."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from test_tokopt import add_server, fake_home, proc, rows_by_name, tool_line, write_log  # noqa: F401
from tokopt import cli as tokopt

FIX = Path(__file__).resolve().parent / "fixtures"
LAUNCHD_ROW = proc(1, 0, "/sbin/launchd", "launchd")
LONG_IDLE = lambda r: 1_000_000.0 - 9 * 3600  # noqa: E731
POL = {"session_idle_hours": 8}


# ---------- 4: the uid filter applies to CANDIDATE sessions only, never to their children ----------

def test_a_root_owned_child_still_counts_as_an_active_child(monkeypatch):
    u = os.getuid()
    names = (f"    1     0 0 Ss    0.0 launchd\n"
             f"  101     1 {u} S     0.0 claude\n"
             f"  102   101 0 S     0.0 sudo\n"             # background sudo run by the session: owned by root
             f"  201     1 {u + 1} S     0.0 claude\n")    # another user's claude: never a candidate
    args = "    1 /sbin/launchd\n  101 claude\n  102 sudo -n make install\n  201 claude\n"
    monkeypatch.setattr(tokopt, "_run", lambda cmd, env=None: names if cmd[1] == "-axco" else args)
    rows = tokopt.ps_rows()
    assert {r["pid"] for r in rows} >= {101, 102} and next(r for r in rows if r["pid"] == 102)["uid"] == 0
    assert tokopt.select_idle_sessions(rows, LONG_IDLE, POL, 1_000_000.0, 9999) == []   # 101 has a root child; 201 not ours


def test_other_users_claude_is_never_selected_even_without_children():
    u = os.getuid()
    rows = [{**LAUNCHD_ROW, "uid": 0}, {**proc(10, 1, "claude", "claude"), "uid": u + 1},
            {**proc(11, 1, "claude", "claude"), "uid": u}]
    got = tokopt.select_idle_sessions(rows, LONG_IDLE, POL, 1_000_000.0, 9999)
    assert [s["pid"] for s in got] == [11]


# ---------- 5: idle-child detection on REAL live shapes ----------

def _fixture_children() -> dict[int, str]:
    out = {}
    for ln in (FIX / "real_claude_children_ps.txt").read_text().splitlines():
        if ln.startswith("#") or not ln.strip():
            continue
        pid, _ppid, args = ln.split(None, 2)
        out[int(pid)] = args
    return out


def _fixture_cmds() -> set[tuple[str, ...]]:
    servers = json.loads((FIX / "real_claude_mcp_configs.json").read_text())["servers"]
    return {c for v in servers.values() if (c := tokopt._cmd_tuple(v))}


REAL_IDLE = {5781: "npm exec (npx rewrites its title)", 5784: "npm exec", 5787: "uv tool uvx", 23886: "docker run",
             6019: "python + script (shebang)", 6023: "npm exec", 62259: "caffeinate"}
REAL_ACTIVE = {23288: "Bash-tool shell", 6007: "memory server whose config is `sh -c ...`", 76457: "language server"}


def test_real_live_children_are_classified_idle_or_active_by_shape():
    cmds = _fixture_cmds()
    got = {pid: tokopt.is_idle_child(a, cmds) for pid, a in _fixture_children().items()}
    assert {p for p, v in got.items() if v} == set(REAL_IDLE), got
    assert {p for p, v in got.items() if not v} == set(REAL_ACTIVE)


def test_without_its_config_a_real_server_shape_is_active():
    for pid, a in _fixture_children().items():
        if pid in (5781, 5784, 5787, 23886, 6019, 6023):
            assert not tokopt.is_idle_child(a, set()), a


@pytest.mark.parametrize("args", [
    "node /Users/x/proj/mcp-gateway/server.js",                                  # unconfigured path that merely says mcp
    "npm exec @playwright/mcp@latest --headed",                                  # extra argument
    "npm exec @other/mcp@latest",                                                # other package
    "npm run dev",
    "/opt/homebrew/bin/uv tool uvx awslabs.aws-serverless-mcp-server@latest",    # config has --allow-write
    "sh -c npm exec @playwright/mcp@latest",
    "python3 /opt/elsewhere/bin/srvx mcp",                                       # same script name, other absolute path
    "docker run -i --rm other/image:1",
    "caffeinate-evil -i",
])
def test_lookalikes_stay_active(args):
    assert not tokopt.is_idle_child(args, _fixture_cmds())


def test_expanded_env_placeholders_still_match_the_configured_server():
    assert tokopt.is_idle_child("docker run -i --rm -e TFE_TOKEN=tfe-abc123 hashicorp/terraform-mcp-server:0.4.0", _fixture_cmds())


def test_session_with_only_real_idle_children_is_selected_and_any_real_worker_blocks_it():
    cmds, kids = _fixture_cmds(), _fixture_children()
    idle = [proc(p, 100, "x", kids[p]) for p in REAL_IDLE]
    base = [LAUNCHD_ROW, proc(100, 1, "claude", "claude")]
    got = tokopt.select_idle_sessions(base + idle, LONG_IDLE, POL, 1_000_000.0, 9999, idle_cmds=cmds)
    assert [s["pid"] for s in got] == [100]
    for pid in REAL_ACTIVE:
        got = tokopt.select_idle_sessions(base + idle + [proc(pid, 100, "x", kids[pid])], LONG_IDLE, POL, 1_000_000.0, 9999,
                                          idle_cmds=cmds)
        assert got == [], pid


def test_real_claude_session_file_shape_is_idle_with_matching_utc_procstart(fake_home, monkeypatch):
    info = json.loads((FIX / "real_claude_session.json").read_text())
    (fake_home / ".claude/sessions").mkdir(parents=True)
    (fake_home / ".claude/sessions/100.json").write_text(json.dumps(info))
    start = tokopt._lstart_epoch(info["procStart"])
    monkeypatch.setattr(tokopt, "proc_start_utc", lambda pid: start)
    r = {"pid": 100, "agent": "claude"}
    assert tokopt.real_activity(r) == info["statusUpdatedAt"] / 1000 and r["cwd"] == info["cwd"]


# ---------- 6: an unscannable log is NAMED in the report; oversized lines make the scan incomplete ----------

def test_report_names_the_unscannable_log_with_the_home_masked(fake_home):
    (fake_home / ".claude/projects/-p/linked.jsonl").symlink_to(fake_home / ".claude/projects/-p/a.jsonl")
    out = tokopt.report(tokopt.load_policy(), time.time(), [])
    line = next(ln for ln in out.splitlines() if "incomplete" in ln and "claude" in ln and "linked.jsonl" in ln)
    assert "~/.claude/projects/-p/linked.jsonl" in line and str(fake_home) not in out


def test_report_caps_the_number_of_named_paths(fake_home):
    for i in range(9):
        (fake_home / f".claude/projects/-p/l{i}.jsonl").symlink_to(fake_home / "nowhere")
    assert "(+4 more)" in tokopt.report(tokopt.load_policy(), time.time(), [])


def test_oversized_line_makes_the_scan_incomplete_and_is_named_even_from_cache(fake_home, monkeypatch):
    add_server(fake_home, "big")
    monkeypatch.setattr(tokopt, "MAX_LINE", 1000, raising=False)
    line = json.loads(tool_line("big", 1))
    line["pad"] = "x" * 2000
    write_log(fake_home / ".claude/projects/-p/c.jsonl", [json.dumps(line)], 0)
    for _ in range(2):  # the second pass is served from the scan cache and must say the same
        act, why = rows_by_name()["big"]
        assert act == "skip" and "incomplete" in why
        assert "~/.claude/projects/-p/c.jsonl" in tokopt.report(tokopt.load_policy(), time.time(), [])

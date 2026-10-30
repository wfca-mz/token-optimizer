"""tokopt tests. Fake HOME per test; never touches the real one."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import tomllib
from pathlib import Path

import pytest

from tokopt import cli as tokopt, nudge

ROOT = Path(__file__).resolve().parent.parent
DAY = 86400
TOKEN = "SECRET_TOKEN_123"
POLICY = ("mcp_idle_days = 28\nskill_idle_days = 28\nsession_idle_hours = 8\nprotect = {protect}\n"
          'cheap_agents = ["report"]\n')


def iso(days_ago: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() - days_ago * DAY))


def tool_line(server: str, days_ago: float, tool: str = "do") -> str:
    return json.dumps({"type": "assistant", "timestamp": iso(days_ago), "message": {"content": [
        {"type": "tool_use", "id": "toolu_1", "name": f"mcp__{server}__{tool}", "input": {}}]}}, separators=(",", ":"))


def cmd_line(cmd: str, days_ago: float) -> str:
    return json.dumps({"type": "user", "timestamp": iso(days_ago),
                       "message": {"content": f"<command-name>{cmd}</command-name>"}}, separators=(",", ":"))


def write_log(path: Path, lines: list[str], age_days: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
    t = time.time() - age_days * DAY
    os.utime(path, (t, t))


def make_plugin(home: Path, key: str, *, skills: bool = False, commands: tuple = (), mcp: bool = False,
                installed: str = "2020-01-01T00:00:00.000Z") -> None:
    d = home / "plugins-src" / key.split("@")[0]
    if skills:
        (d / "skills/s").mkdir(parents=True, exist_ok=True)
        (d / "skills/s/SKILL.md").write_text("---\ndescription: " + "d" * 400 + "\n---\n")
    for c in commands:
        (d / "commands").mkdir(parents=True, exist_ok=True)
        (d / "commands" / f"{c}.md").write_text("x")
    if mcp:
        d.mkdir(parents=True, exist_ok=True)
        (d / ".mcp.json").write_text("{}")
    ip = home / ".claude/plugins/installed_plugins.json"
    ip.parent.mkdir(parents=True, exist_ok=True)
    cur = json.loads(ip.read_text()) if ip.exists() else {"plugins": {}}
    cur["plugins"][key] = [{"installPath": str(d), "installedAt": installed}]
    ip.write_text(json.dumps(cur))
    st = home / ".claude/settings.json"
    s = json.loads(st.read_text()) if st.exists() else {"enabledPlugins": {}}
    s["enabledPlugins"][key] = True
    st.write_text(json.dumps(s, indent=2) + "\n")


def age_first_seen(key: str, days: float) -> None:
    fs = tokopt.state_dir() / "first_seen.json"
    d = json.loads(fs.read_text()) if fs.exists() else {}
    d[key] = time.time() - days * DAY
    fs.write_text(json.dumps(d))


SCOPES = ["claude:mcp", "claude:plugin", "codex:mcp", "hermes:mcp", "session:stop"]


def confirm(scopes=SCOPES) -> None:
    """Record a prior confirmed apply for the CURRENT policy (S2/17 tests remove or change it)."""
    d = tokopt.state_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / "confirmed").write_text(json.dumps({"policy": tokopt.policy_hash(tokopt.load_policy()), "scopes": list(scopes),
                                       "paths": sorted(tokopt.known_configs())}))


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(tokopt, "VERIFY_DELAY", 0, raising=False)
    servers = {
        "oldsrv": {"command": "x"}, "freshsrv": {"command": "y"}, "keepsrv": {"command": "z"},
        "secretsrv": {"command": "npx", "env": {"API_TOKEN": TOKEN}},
    }
    (tmp_path / ".claude.json").write_text(json.dumps({"numStartups": 3, "mcpServers": servers}, indent=2) + "\n")
    proj = tmp_path / ".claude/projects/-p"
    write_log(proj / "a.jsonl", [tool_line("oldsrv", 29), tool_line("keepsrv", 29), tool_line("secretsrv", 29)], 40)
    write_log(proj / "b.jsonl", [tool_line("freshsrv", 3)], 0)
    (tmp_path / ".local/state/tokopt").mkdir(parents=True)
    (tmp_path / ".config/tokopt").mkdir(parents=True)
    (tmp_path / ".config/tokopt/policy.toml").write_text(POLICY.format(protect='["keepsrv"]'))
    confirm()
    return tmp_path


def run(argv, monkeypatch):
    monkeypatch.setattr(tokopt, "ps_rows", lambda: [])
    return tokopt.main(argv)


def rows_by_name():
    _, rows = tokopt.plan(tokopt.load_policy(), time.time())
    return {r[0].name: (r[1], r[2]) for r in rows}


def mcp_names(home: Path) -> set:
    return set(json.loads((home / ".claude.json").read_text())["mcpServers"])


def ledger(home: Path) -> list[dict]:
    p = home / ".local/state/tokopt/ledger.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()] if p.exists() else []


def add_server(home: Path, name: str) -> None:
    cj = json.loads((home / ".claude.json").read_text())
    cj["mcpServers"][name] = {"command": "s"}
    (home / ".claude.json").write_text(json.dumps(cj, indent=2) + "\n")


# ---------- evidence: A1 / N7 / N8 / N10 / N12 ----------

def test_a_mcp_unused_parked_used_kept_protected_kept(fake_home, monkeypatch, capsys):
    assert run(["apply"], monkeypatch) == 0
    assert mcp_names(fake_home) == {"freshsrv", "keepsrv"}
    parked = json.loads((fake_home / ".claude.json").read_text())["mcpServersParked"]
    assert set(parked) == {"oldsrv", "secretsrv"}
    assert "parked claude mcp oldsrv" in capsys.readouterr().out


def test_b_insufficient_history_blocks_parking(fake_home, monkeypatch):
    (fake_home / ".claude/projects/-p/a.jsonl").unlink()
    before = (fake_home / ".claude.json").read_bytes()
    run(["apply"], monkeypatch)
    assert (fake_home / ".claude.json").read_bytes() == before
    assert ledger(fake_home) == []
    assert "insufficient history" in tokopt.report(tokopt.load_policy(), time.time(), [])


def test_mcp_call_only_in_subagent_transcript_is_kept(fake_home):
    add_server(fake_home, "subsrv")
    write_log(fake_home / ".claude/projects/-p/sess1/subagents/agent-1.jsonl", [tool_line("subsrv", 3)], 3)
    act, why = rows_by_name()["subsrv"]
    assert act == "keep" and "used 3d ago" in why


def test_plugin_mcp_call_only_in_subagent_is_kept_terraform_case(fake_home):
    make_plugin(fake_home, "terraform@m", mcp=True)
    write_log(fake_home / ".claude/projects/-p/sess1/subagents/agent-1.jsonl",
              [tool_line("plugin_terraform_terraform", 15, "get_latest_provider_version")], 15)
    act, why = rows_by_name()["terraform@m"]
    assert act == "keep" and "used 15d ago" in why


def test_plugin_slash_command_counts_as_use(fake_home):
    make_plugin(fake_home, "cr@m", commands=("code-review",))
    write_log(fake_home / ".claude/projects/-p/c.jsonl", [cmd_line("/code-review", 2)], 2)
    act, why = rows_by_name()["cr@m"]
    assert act == "keep" and "used 2d ago" in why


def test_plugin_prefixed_slash_command_counts_as_use(fake_home):
    make_plugin(fake_home, "cr@m", commands=("code-review",))
    write_log(fake_home / ".claude/projects/-p/c.jsonl", [cmd_line("/cr:code-review", 2)], 2)
    assert rows_by_name()["cr@m"][0] == "keep"


def test_unused_plugin_with_old_first_seen_parks(fake_home):
    make_plugin(fake_home, "idle@m", commands=("x",))
    tokopt.plan(tokopt.load_policy(), time.time())
    age_first_seen("claude:plugin:idle@m", 40)
    assert rows_by_name()["idle@m"][0] == "park"


def test_mcp_server_name_normalised_like_claude_tool_names(fake_home):
    add_server(fake_home, "my.srv name")
    write_log(fake_home / ".claude/projects/-p/c.jsonl", [tool_line("my_srv_name", 3)], 0)
    assert rows_by_name()["my.srv name"][0] == "keep"


def test_zero_mcp_evidence_skips_agent_instead_of_parking_all(fake_home):
    for f in (fake_home / ".claude/projects/-p").glob("*.jsonl"):
        f.unlink()
    write_log(fake_home / ".claude/projects/-p/a.jsonl", [json.dumps({"type": "user", "message": "hi"})], 40)
    act, why = rows_by_name()["oldsrv"]
    assert act == "skip" and "scan found no tool calls" in why


def test_ts_reads_top_level_timestamp_not_nested():
    line = json.dumps({"message": {"timestamp": "1999-01-01T00:00:00Z"}, "timestamp": "2026-10-01T00:00:00Z"}).encode()
    assert tokopt._ts_of(json.loads(line), 5.0) == pytest.approx(1790812800.0)


def test_codex_counts_real_tool_calls_not_inventory(fake_home):
    (fake_home / ".codex").mkdir()
    (fake_home / ".codex/config.toml").write_text('[mcp_servers.listed]\ncommand = "x"\n\n[mcp_servers.called]\ncommand = "y"\n')
    inv = json.dumps({"timestamp": iso(2), "type": "session_meta", "payload": {"tools": [{"name": "mcp__listed__t"}]}})
    call = json.dumps({"timestamp": iso(2), "type": "event_msg", "payload": {"type": "item_completed", "item": {
        "type": "McpToolCall", "id": "c1", "server": "called", "tool": "t"}}})
    write_log(fake_home / ".codex/sessions/2026/10/01/rollout-a.jsonl", [inv, call], 40)
    r = rows_by_name()
    assert r["called"][0] == "keep" and "used 2d ago" in r["called"][1]
    assert "never seen used" in r["listed"][1]  # the inventory mention is not a call


def test_codex_function_call_form_counts(fake_home):
    (fake_home / ".codex").mkdir()
    (fake_home / ".codex/config.toml").write_text('[mcp_servers.memory-srv]\ncommand = "x"\n')
    call = json.dumps({"timestamp": iso(2), "type": "response_item", "payload": {
        "type": "function_call", "name": "mcp__memory_srv__search", "arguments": "{}"}})  # codex maps - to _
    write_log(fake_home / ".codex/sessions/2026/10/01/rollout-a.jsonl", [call], 40)
    assert "used 2d ago" in rows_by_name()["memory-srv"][1]


def test_old_format_scan_cache_is_ignored(fake_home):
    (tokopt.state_dir() / "scan-claude.json").write_text(json.dumps({"/x": [[1, 2], {"mcp:oldsrv": 1e12}]}))
    assert rows_by_name()["oldsrv"][0] == "park"


# ---------- first_seen: A2 ----------

def test_never_seen_plugin_installed_long_ago_is_kept_aging_zero(fake_home):
    make_plugin(fake_home, "old@m", skills=True, installed="2026-01-01T00:00:00.000Z")
    act, why = rows_by_name()["old@m"]
    assert act == "keep" and "aging 0d/28d" in why


def test_first_seen_records_now_never_installed_at(fake_home):
    make_plugin(fake_home, "old@m", skills=True, installed="2026-01-01T00:00:00.000Z")
    now = time.time()
    tokopt.plan(tokopt.load_policy(), now)
    d = json.loads((tokopt.state_dir() / "first_seen.json").read_text())
    assert d["claude:plugin:old@m"] == pytest.approx(now, abs=2)


def test_never_seen_ages_from_first_seen(fake_home):
    add_server(fake_home, "ghost")
    act, why = rows_by_name()["ghost"]
    assert act == "keep" and "aging" in why
    age_first_seen("claude:mcp:ghost", 40)
    assert rows_by_name()["ghost"][0] == "park"


# ---------- apply / restore: B3 ----------

def test_c_restore_is_byte_identical(fake_home, monkeypatch):
    (fake_home / ".claude/settings.json").write_text('{\n  "enabledPlugins": {\n    "x@m": false\n  }\n}\n')
    orig = {p: p.read_bytes() for p in (fake_home / ".claude.json", fake_home / ".claude/settings.json")}
    run(["apply"], monkeypatch)
    assert (fake_home / ".claude.json").read_bytes() != orig[fake_home / ".claude.json"]
    bk = list((fake_home / ".local/state/tokopt/backups").glob("*/*"))
    assert bk and all(oct(b.stat().st_mode & 0o777) == "0o600" for b in bk)
    run(["restore", "--all"], monkeypatch)
    for p, b in orig.items():
        assert p.read_bytes() == b


def test_restore_succeeds_after_unrelated_keys_changed(fake_home, monkeypatch, capsys):
    run(["apply"], monkeypatch)
    cj = fake_home / ".claude.json"
    d = json.loads(cj.read_text())
    d["numStartups"] = 99  # the live claude process rewrote unrelated keys
    d["newKey"] = {"a": 1}
    cj.write_text(json.dumps(d, indent=2) + "\n")
    run(["restore", "--all"], monkeypatch)
    assert "REFUSED" not in capsys.readouterr().out
    got = json.loads(cj.read_text())
    assert set(got["mcpServers"]) == {"oldsrv", "freshsrv", "keepsrv", "secretsrv"}
    assert "mcpServersParked" not in got and got["numStartups"] == 99 and got["newKey"] == {"a": 1}
    assert got["mcpServers"]["secretsrv"]["env"]["API_TOKEN"] == TOKEN


def test_restore_when_host_already_reactivated_server_is_a_noop(fake_home, monkeypatch, capsys):
    cj = fake_home / ".claude.json"
    orig = cj.read_bytes()
    with monkeypatch.context() as m:
        m.setattr(tokopt.time, "sleep", lambda s: cj.write_bytes(orig))
        run(["apply"], monkeypatch)
    run(["restore", "--all"], monkeypatch)
    assert "REFUSED" not in capsys.readouterr().out and cj.read_bytes() == orig


def test_restore_plugin_semantic_after_settings_changed(fake_home, monkeypatch):
    make_plugin(fake_home, "idle@m", commands=("x",))
    tokopt.plan(tokopt.load_policy(), time.time())
    age_first_seen("claude:plugin:idle@m", 40)
    run(["apply"], monkeypatch)
    st = fake_home / ".claude/settings.json"
    s = json.loads(st.read_text())
    assert s["enabledPlugins"]["idle@m"] is False
    s["theme"] = "dark"
    st.write_text(json.dumps(s, indent=2) + "\n")
    run(["restore", "--all"], monkeypatch)
    got = json.loads(st.read_text())
    assert got["enabledPlugins"]["idle@m"] is True and got["theme"] == "dark"


def test_restore_toml_semantic_after_config_changed(fake_home, monkeypatch):
    (fake_home / ".codex").mkdir()
    cfg = fake_home / ".codex/config.toml"
    cfg.write_text('[mcp_servers.a]\ncommand = "x"\n\n[mcp_servers.b]\ncommand = "y"\n')
    call = json.dumps({"timestamp": iso(40), "payload": {"item": {"type": "McpToolCall", "id": "1", "server": "b"}}})
    write_log(fake_home / ".codex/sessions/2026/01/01/r.jsonl", [call], 40)
    tokopt.plan(tokopt.load_policy(), time.time())
    age_first_seen("codex:mcp:a", 40)
    run(["apply"], monkeypatch)
    assert "enabled = false" in cfg.read_text()
    cfg.write_text(cfg.read_text() + '\n[model]\nname = "z"\n')
    run(["restore", "--all"], monkeypatch)
    out = cfg.read_text()
    assert "enabled = true" in out and "enabled = false" not in out and '[model]\nname = "z"' in out


def test_restore_refuses_when_file_not_canonical_and_changed(fake_home, monkeypatch, capsys):
    run(["apply"], monkeypatch)
    cj = fake_home / ".claude.json"
    cj.write_text(cj.read_text() + " ")
    run(["restore", "--all"], monkeypatch)
    assert "REFUSED" in capsys.readouterr().out


def test_park_detects_host_overwrite_and_records_reverted(fake_home, monkeypatch, capsys):
    cj = fake_home / ".claude.json"
    orig = cj.read_bytes()
    monkeypatch.setattr(tokopt.time, "sleep", lambda s: cj.write_bytes(orig))  # claude rewrites its state
    rc = run(["apply"], monkeypatch)
    out = capsys.readouterr().out
    assert rc == 1 and "reverted by the running agent" in out
    assert {r["status"] for r in ledger(fake_home) if r["action"] == "park"} == {"reverted-by-host"}


def test_park_survives_unrelated_host_rewrite(fake_home, monkeypatch):
    cj = fake_home / ".claude.json"

    def host_touch(s):
        d = json.loads(cj.read_text())
        d["numStartups"] += 1
        cj.write_text(json.dumps(d, indent=2) + "\n")
    monkeypatch.setattr(tokopt.time, "sleep", host_touch)
    assert run(["apply"], monkeypatch) == 0
    assert {r["status"] for r in ledger(fake_home) if r["action"] == "park"} == {"parked"}


def test_park_aborts_when_file_mtime_changes_between_read_and_write(fake_home):
    cj = fake_home / ".claude.json"
    before = cj.read_bytes()
    item = next(i for s in tokopt.plan(tokopt.load_policy(), time.time())[0] for i in s.items if i.name == "oldsrv")

    def racing_edit(text, name):
        out = tokopt.json_park(text, name)
        os.utime(cj, ns=(1, 1))  # the live process touched the file mid-edit
        return out

    item.edit = racing_edit
    with pytest.raises(RuntimeError, match="changed"):
        tokopt.park(item, "unused", "stamp")
    assert cj.read_bytes() == before
    assert ledger(fake_home) == []
    assert not list((fake_home / ".local/state/tokopt/backups").glob("*/*"))


def test_secret_files_created_exclusively_0600_and_private_dirs(fake_home, monkeypatch):
    calls = []
    real_open = os.open

    def spy(p, flags, mode=0o777, **kw):
        calls.append((str(p), flags, mode))
        return real_open(p, flags, mode, **kw)
    monkeypatch.setattr(tokopt.os, "open", spy)
    run(["apply"], monkeypatch)
    bk = [c for c in calls if "/backups/" in c[0]]
    assert bk and all(c[1] & os.O_EXCL and c[2] == 0o600 for c in bk)
    root = fake_home / ".local/state/tokopt/backups"
    for d in [root, *root.iterdir()]:
        assert d.stat().st_mode & 0o777 == 0o700


def test_e_secrets_masked_everywhere(fake_home, monkeypatch, capsys):
    assert TOKEN not in tokopt.report(tokopt.load_policy(), time.time(), [])
    run(["apply"], monkeypatch)
    printed = capsys.readouterr().out
    state = "".join(p.read_text() for p in (fake_home / ".local/state/tokopt").glob("*") if p.is_file())
    assert TOKEN not in printed and TOKEN not in state
    assert TOKEN in "".join(p.read_text() for p in (fake_home / ".local/state/tokopt/backups").glob("*/*"))
    assert tokopt.mask("API_TOKEN: abc123") == "API_TOKEN: ***"


def test_f_plugin_parked_via_enabledplugins_and_tokens_measured(fake_home, monkeypatch):
    make_plugin(fake_home, "greptile@m", skills=True)
    make_plugin(fake_home, "superpowers@m", skills=True)
    tokopt.plan(tokopt.load_policy(), time.time())
    age_first_seen("claude:plugin:greptile@m", 40)
    age_first_seen("claude:plugin:superpowers@m", 40)
    (fake_home / ".config/tokopt/policy.toml").write_text(POLICY.format(protect='["superpowers"]'))
    confirm()
    (fake_home / ".claude/skills/myskill").mkdir(parents=True)  # report-only row must not break planning
    (fake_home / ".claude/skills/myskill/SKILL.md").write_text("x")
    rep = tokopt.report(tokopt.load_policy(), time.time(), [])
    run(["apply"], monkeypatch)
    ep = json.loads((fake_home / ".claude/settings.json").read_text())["enabledPlugins"]
    assert ep == {"greptile@m": False, "superpowers@m": True}
    assert re.search(r"(?m)^greptile@m .* ~100 +PARK", rep)  # 400 chars / 4, measured and flagged approximate
    assert re.search(r"(?m)^oldsrv .* n/a +PARK", rep)  # no schema size known: never invented


def test_g_hermes_yaml_text_level_park():
    src = ("# top comment\nmodel: x\n\nmcp_servers:\n  alpha:\n    command: a\n    args:\n      - mcp\n"
           "  beta:\n    command: npx\n    env:\n      K: v\n  # trailing\n\n# next section\nother: 1\n")
    out = tokopt.yaml_park(src, "beta")
    assert out.startswith("# top comment\nmodel: x\n\nmcp_servers:\n  alpha:\n    command: a\n    args:\n      - mcp\n")
    assert "  # trailing\n\n# next section\nother: 1\n" in out
    assert out.endswith("mcp_servers_parked:\n  beta:\n    command: npx\n    env:\n      K: v\n")
    only = tokopt.yaml_park("mcp_servers:\n  a:\n    url: u\nz: 1\n", "a")
    assert only == "mcp_servers: {}\nz: 1\n\nmcp_servers_parked:\n  a:\n    url: u\n"


def test_codex_toml_park_preserves_text():
    src = '[mcp_servers.a]\ncommand = "x"\n# keep\n\n[mcp_servers.a.env]\nK = "v"\n\n[mcp_servers.b]\nenabled = true\ncommand = "y"\n'
    out = tokopt.toml_park(src, "a")
    assert out.startswith('[mcp_servers.a]\nenabled = false\ncommand = "x"\n# keep')
    assert tokopt.toml_park(src, "b").endswith('enabled = false\ncommand = "y"\n')


# ---------- sessions: B1 / B2 / N5 / N6 ----------

def proc(pid, ppid, comm, args, stat="S", cpu=0.0):
    return {"pid": pid, "ppid": ppid, "uid": os.getuid(), "stat": stat, "cpu": cpu, "comm": comm, "args": args}


LAUNCHD = proc(1, 0, "/sbin/launchd", "launchd")


PW = ("npx", "-y", "@playwright/mcp@latest")   # an MCP server command from the agent's own config


def pick(rows, last=None, self_pid=9999, idle_cmds=frozenset({PW})):
    now = 1_000_000.0
    last = last or {}
    got = tokopt.select_idle_sessions(rows, lambda r: last.get(r["pid"], now - 9 * 3600), {"session_idle_hours": 8}, now, self_pid,
                                      idle_cmds=idle_cmds)
    return sorted(s["pid"] for s in got)


def test_d_idle_session_selection():
    now = 1_000_000.0
    rows = [
        LAUNCHD,
        proc(10, 1, "/bin/claude", "claude"),  # idle, clean
        proc(20, 1, "/bin/claude", "claude"),  # idle, running pytest child
        proc(21, 20, "/bin/sh", "pytest -x"),
        proc(30, 1, "/bin/claude", "claude"),  # recent
        proc(40, 1, "/bin/claude", "claude"),  # self ancestor
        proc(41, 40, "python3", "python3 -m tokopt apply"),
        proc(50, 1, "/bin/claude", "claude"),  # idle, only an MCP child
        proc(51, 50, "node", "npx -y some-mcp-server"),
    ]
    last = {10: now - 9 * 3600, 20: now - 9 * 3600, 30: now - 3600, 40: now - 20 * 3600, 50: now - 9 * 3600}
    got = tokopt.select_idle_sessions(rows, lambda r: last.get(r["pid"]), {"session_idle_hours": 8}, now, self_pid=41,
                                      idle_cmds={("npx", "-y", "some-mcp-server")})
    assert sorted(s["pid"] for s in got) == [10, 50]


def test_session_waiting_on_npx_playwright_test_is_active():
    assert pick([LAUNCHD, proc(10, 1, "/bin/claude", "claude"), proc(11, 10, "node", "npx playwright test")]) == []


def test_unknown_child_defaults_to_active():
    assert pick([LAUNCHD, proc(10, 1, "/bin/claude", "claude"), proc(11, 10, "make", "make deploy")]) == []


def test_idle_mcp_server_with_its_own_children_does_not_block_stop():
    rows = [LAUNCHD, proc(10, 1, "/bin/claude", "claude"),
            proc(11, 10, "node", "npx -y @playwright/mcp@latest"), proc(12, 11, "chrome", "chrome --headless")]
    assert pick(rows) == [10]


def test_busy_mcp_child_blocks_stop():
    rows = [LAUNCHD, proc(10, 1, "/bin/claude", "claude"), proc(11, 10, "node", "npx -y @playwright/mcp@latest", cpu=12.0)]
    assert pick(rows) == []


def test_only_claude_sessions_are_ever_stopped():
    rows = [LAUNCHD, proc(10, 1, "/bin/codex", "codex"), proc(20, 1, "/bin/gemini", "gemini"), proc(30, 1, "/bin/claude", "claude")]
    assert pick(rows) == [30]


LSTART = "Fri Oct 30 16:37:10 2026"


def claude_session(home: Path, pid: int, **kw) -> dict:
    d = {"pid": pid, "sessionId": "sid-1", "cwd": "/w", "procStart": LSTART, "kind": "interactive",
         "entrypoint": "cli", "status": "idle", "statusUpdatedAt": int((time.time() - 9 * 3600) * 1000)}
    d.update(kw)
    f = home / ".claude/sessions" / f"{pid}.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(d))
    return d


@pytest.fixture
def ps_lstart(monkeypatch):
    monkeypatch.setattr(tokopt, "_run", lambda cmd, env=None: LSTART + "\n" if cmd[1:3] == ["-p", "5"] else "")


def test_claude_activity_comes_from_the_per_pid_session_map(fake_home, ps_lstart):
    d = claude_session(fake_home, 5)
    r = {"pid": 5, "agent": "claude"}
    assert tokopt.real_activity(r) == pytest.approx(d["statusUpdatedAt"] / 1000)
    assert r["session"] == "sid-1" and r["cwd"] == "/w"


@pytest.mark.parametrize("kw", [{"status": "busy"}, {"status": "shell"}, {"kind": "sdk"}, {"entrypoint": "sdk-cli"},
                                {"procStart": "Mon Jan  1 00:00:00 2024"}])
def test_claude_activity_none_when_busy_noninteractive_or_stale_pid_file(fake_home, ps_lstart, kw):
    claude_session(fake_home, 5, **kw)
    assert tokopt.real_activity({"pid": 5, "agent": "claude"}) is None


def test_claude_activity_none_without_session_file(fake_home, ps_lstart):
    assert tokopt.real_activity({"pid": 5, "agent": "claude"}) is None


def test_run_tolerates_missing_tool_and_timeouts(monkeypatch):
    seen = {}

    def boom(cmd, **kw):
        seen.update(kw)
        raise FileNotFoundError(cmd[0])
    monkeypatch.setattr(tokopt.subprocess, "run", boom)
    assert tokopt._run(["lsof"]) == "" and seen["timeout"] == 15

    def slow(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 15)
    monkeypatch.setattr(tokopt.subprocess, "run", slow)
    assert tokopt._run(["ps"]) == ""


def test_channel_and_noninteractive_claude_are_not_session_targets():
    rows = [LAUNCHD, proc(10, 1, "claude", "claude --channels plugin:x"),
            proc(20, 1, "claude", "claude -p --output-format stream-json"), proc(30, 1, "claude", "claude")]
    assert pick(rows) == [30]


def test_ps_rows_joins_names_and_args_by_pid_and_never_matches_spaced_names(monkeypatch):
    u = os.getuid()
    names = (f"    1     0 {u} Ss    0.0 launchd\n"
             f"  100     1 {u} S     0.0 ChatGPT for Chrome\n"
             f"  101     1 {u} S     0.0 claude\n"
             f"  102     1 {u} S     0.0 Python\n"
             f"  103     1 {u + 1} S     0.0 claude\n")  # another user's claude: never ours to touch
    args = ("    1 /sbin/launchd\n"
            "  100 /Users/x/Library/ChatGPT for Chrome.app/Contents/MacOS/ChatGPT for Chrome --claude --serve\n"
            "  101 claude --resume abc\n"
            "  102 /usr/bin/python3 /Users/x/codex experiments/run.py\n"
            "  103 claude\n")
    seen = []
    monkeypatch.setattr(tokopt, "_run", lambda cmd, env=None: seen.append(cmd[0]) or (names if cmd[1] == "-axco" else args))
    rows = tokopt.ps_rows()
    assert set(seen) == {"/bin/ps"}
    assert [r["comm"] for r in rows] == ["launchd", "ChatGPT for Chrome", "claude", "Python", "claude"]  # every uid is kept
    assert [r["uid"] for r in rows] == [u, u, u, u, u + 1]
    assert rows[2]["args"] == "claude --resume abc" and rows[2]["ppid"] == 1 and rows[1]["cpu"] == 0.0
    assert pick(rows) == [101]  # only the real claude process of OUR uid is an agent


POL = {"session_idle_hours": 8}
NOW = 2_000_000.0
IDENT = "claude Thu Oct  1 01:00:00 2026"


def sess(**kw):
    return {"agent": "claude", "pid": 77, "idle_h": 9, "cwd": "/w", "session": "abc", "ident": IDENT, **kw}


@pytest.fixture
def kill_env(fake_home, monkeypatch):
    sent = []
    monkeypatch.setattr(tokopt.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    monkeypatch.setattr(tokopt, "proc_ident", lambda pid: IDENT)
    monkeypatch.setattr(tokopt, "real_activity", lambda r: NOW - 9 * 3600)
    monkeypatch.setattr(tokopt, "ps_rows", lambda: [proc(1, 0, "launchd", "launchd"), proc(77, 1, "claude", "claude")])
    return sent


def note_path(home: Path) -> Path:
    return home / ".local/state/tokopt/stopped/claude-77.md"


def test_stop_writes_note_before_sigterm_then_ledger(fake_home, monkeypatch, kill_env):
    seen = []
    monkeypatch.setattr(tokopt.os, "kill", lambda pid, sig: seen.append(note_path(fake_home).exists()))
    status, _ = tokopt.stop_session(sess(), POL, NOW)
    assert status == "stopped" and seen == [True]  # note existed when the signal went out
    assert "claude --resume abc" in note_path(fake_home).read_text()
    assert [r["action"] for r in ledger(fake_home)] == ["stop"]


def test_handoff_uses_real_session_id_quoted_and_sanitises_cwd(fake_home, kill_env, monkeypatch):
    def act(r):
        r.update(session="a b;rm -rf", cwd="/w\x1b[31m\nINJECT")
        return NOW - 9 * 3600
    monkeypatch.setattr(tokopt, "real_activity", act)
    tokopt.stop_session(sess(), POL, NOW)
    text = note_path(fake_home).read_text()
    assert "claude --resume 'a b;rm -rf'" in text and "\x1b" not in text and text.count("\n") == 1


def test_stop_skips_when_pid_was_recycled(fake_home, kill_env, monkeypatch):
    monkeypatch.setattr(tokopt, "proc_ident", lambda pid: "vim Fri Oct  2 02:00:00 2026")
    status, msg = tokopt.stop_session(sess(), POL, NOW)
    assert status == "skipped" and "identity" in msg and kill_env == []
    assert ledger(fake_home) == []


def test_identity_rechecked_right_before_kill_after_slow_activity_check(fake_home, kill_env, monkeypatch):
    idents = iter([IDENT, "vim Fri Oct  2 02:00:00 2026"])  # recycled during the activity check
    monkeypatch.setattr(tokopt, "proc_ident", lambda pid: next(idents))
    status, msg = tokopt.stop_session(sess(), POL, NOW)
    assert status == "skipped" and "identity" in msg and kill_env == [] and not note_path(fake_home).exists()


def test_stop_skips_when_user_resumed(fake_home, kill_env, monkeypatch):
    monkeypatch.setattr(tokopt, "real_activity", lambda r: NOW - 60)
    status, msg = tokopt.stop_session(sess(), POL, NOW)
    assert status == "skipped" and "active" in msg and kill_env == []


def test_stop_skips_when_activity_cannot_be_established(fake_home, kill_env, monkeypatch):
    monkeypatch.setattr(tokopt, "real_activity", lambda r: None)
    assert tokopt.stop_session(sess(), POL, NOW)[0] == "skipped" and kill_env == []


@pytest.mark.parametrize("exc", [ProcessLookupError, PermissionError])
def test_stop_kill_failure_removes_note_and_writes_no_ledger(fake_home, monkeypatch, kill_env, exc):
    def boom(pid, sig):
        raise exc()
    monkeypatch.setattr(tokopt.os, "kill", boom)
    status, msg = tokopt.stop_session(sess(), POL, NOW)
    assert status == "failed" and exc.__name__ in msg
    assert ledger(fake_home) == [] and not note_path(fake_home).exists()


def test_apply_continues_after_failed_kill_and_returns_rc1(fake_home, monkeypatch):
    monkeypatch.setattr(tokopt, "idle_sessions", lambda policy, now: [sess(pid=1), sess(pid=2)])
    monkeypatch.setattr(tokopt, "proc_ident", lambda pid: IDENT)
    monkeypatch.setattr(tokopt, "real_activity", lambda r: time.time() - 9 * 3600)
    sent = []

    def kill(pid, sig):
        sent.append(pid)
        if pid == 1:
            raise PermissionError()
    monkeypatch.setattr(tokopt.os, "kill", kill)
    monkeypatch.setattr(tokopt, "ps_rows", lambda: [proc(1, 0, "claude", "claude"), proc(2, 0, "claude", "claude")])
    assert tokopt.main(["apply"]) == 1
    assert sent == [1, 2]
    assert [r["item"] for r in ledger(fake_home) if r["action"] == "stop"] == ["2"]


def test_sessions_stopped_before_configs_are_parked(fake_home, monkeypatch):
    order = []
    monkeypatch.setattr(tokopt, "idle_sessions", lambda policy, now: [sess()])
    monkeypatch.setattr(tokopt, "stop_session", lambda s, policy, now: order.append("stop") or ("stopped", "ok"))
    real_park = tokopt.park
    monkeypatch.setattr(tokopt, "park", lambda *a, **k: order.append("park") or real_park(*a, **k))
    assert run(["apply"], monkeypatch) == 0
    assert order[0] == "stop" and "park" in order


# ---------- S1 / S2 / S3 / N13 / N14 ----------

def test_policy_first_run_writes_documented_generic_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    p = tokopt.load_policy()
    text = (tmp_path / ".config/tokopt/policy.toml").read_text()
    assert p["protect"] == [] and p["mcp_idle_days"] == 28
    assert text.count("#") >= 5 and '# protect = ["' in text
    assert p["cheap_agents"] == []


def test_partial_policy_file_gets_defaults(fake_home):
    (fake_home / ".config/tokopt/policy.toml").write_text('protect = ["keepsrv"]\n')
    assert tokopt.load_policy()["session_idle_hours"] == 8


def test_adapters_disabled_when_agent_not_installed(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".claude").mkdir()
    scans, rows = tokopt.plan(tokopt.load_policy(), time.time())
    assert [s.agent for s in scans] == ["claude"] and rows == []


def test_report_with_nothing_installed_does_not_crash(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert tokopt.report(tokopt.load_policy(), time.time(), []).startswith("item")


def test_no_evidence_adapters_skip(fake_home):
    (fake_home / ".gemini").mkdir()
    (fake_home / ".gemini/settings.json").write_text(json.dumps({"mcpServers": {"secretsrv": {}}}, indent=2))
    g = [r for r in tokopt.plan(tokopt.load_policy(), time.time())[1] if r[0].agent == "gemini"]
    assert g and g[0][1] == "skip" and "no evidence" in g[0][2]


def test_first_apply_without_yes_prints_plan_and_changes_nothing(fake_home, monkeypatch, capsys):
    (fake_home / ".local/state/tokopt/confirmed").unlink()
    before = (fake_home / ".claude.json").read_bytes()
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert run(["apply"], monkeypatch) == 2
    out = capsys.readouterr().out
    assert "--yes" in out and "oldsrv" in out and "PARK" in out
    assert (fake_home / ".claude.json").read_bytes() == before and ledger(fake_home) == []


def test_first_apply_with_yes_applies_and_remembers_confirmation(fake_home, monkeypatch):
    (fake_home / ".local/state/tokopt/confirmed").unlink()
    assert run(["apply", "--yes"], monkeypatch) == 0
    assert (fake_home / ".local/state/tokopt/confirmed").exists()
    assert "oldsrv" not in mcp_names(fake_home)
    assert run(["apply"], monkeypatch) == 0  # no prompt, no flag needed afterwards


def test_first_apply_interactive_answer_n_aborts(fake_home, monkeypatch):
    (fake_home / ".local/state/tokopt/confirmed").unlink()
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    assert run(["apply"], monkeypatch) == 2 and ledger(fake_home) == []


def test_scheduled_apply_never_prompts_and_needs_prior_confirmation(fake_home, monkeypatch):
    (fake_home / ".local/state/tokopt/confirmed").unlink()
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail("prompted in scheduled mode"))
    assert run(["apply", "--scheduled"], monkeypatch) == 2 and ledger(fake_home) == []
    confirm()
    assert run(["apply", "--scheduled"], monkeypatch) == 0


def test_single_atomic_write_and_read_json_implementation():
    src = (ROOT / "tokopt/cli.py").read_text()
    assert src.count("os.replace(") == 1 and src.count("def atomic_write(") == 1
    assert src.count("def _read_json(") == 1
    assert len(re.findall(r"json\.loads\(\w+\.read_text\(\)", src)) == 0  # callers go through _read_json


def test_pyproject_declares_console_script_and_py311():
    d = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert d["project"]["scripts"]["tokopt"] == "tokopt.cli:main"
    assert d["project"]["requires-python"] == ">=3.11" and d["project"].get("dependencies", []) == []


def test_python_dash_m_tokopt_report_runs_against_fake_home(tmp_path):
    (tmp_path / ".claude").mkdir()
    r = subprocess.run([sys.executable, "-m", "tokopt", "report"], cwd=ROOT, capture_output=True, text=True,
                       env={**os.environ, "HOME": str(tmp_path)})
    assert r.returncode == 0 and r.stdout.startswith("item")


def test_tasker_is_not_a_cheap_agent_anywhere():
    assert "tasker" not in (ROOT / "README.md").read_text() and "tasker" not in tokopt.DEFAULT_POLICY


# ---------- installer (N4): only the resolver function runs, under stock bash ----------

INSTALLER = ROOT / "scripts/install-tokopt-launchd.sh"


def resolve(tmp_path, versions: dict) -> subprocess.CompletedProcess:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, ok in versions.items():
        f = bindir / name
        f.write_text(f"#!/bin/sh\n{'exit 0' if ok else 'exit 1'}\n")
        f.chmod(0o755)
    return subprocess.run(["/bin/bash", "-c", f'source "{INSTALLER}"; resolve_python'], capture_output=True, text=True,
                          env={"PATH": f"{bindir}:/usr/bin:/bin", "HOME": str(tmp_path)})


def test_installer_prefers_newest_acceptable_python(tmp_path):
    r = resolve(tmp_path, {"python3.13": True, "python3.12": True, "python3": True})
    assert r.returncode == 0 and r.stdout.strip() == str(tmp_path / "bin/python3.13")


@pytest.mark.skipif(sys.platform != "darwin", reason="the launchd installer is macOS-only (Linux /tmp is world-writable, which it refuses by design)")
def test_installer_skips_too_old_python_and_falls_through(tmp_path):
    r = resolve(tmp_path, {"python3.13": False, "python3.11": True, "python3": False})
    assert r.stdout.strip() == str(tmp_path / "bin/python3.11")


@pytest.mark.skipif(sys.platform != "darwin", reason="the launchd installer is macOS-only (Linux /tmp is world-writable, which it refuses by design)")
def test_installer_refuses_when_no_python_311(tmp_path):
    r = resolve(tmp_path, {"python3.13": False, "python3": False})
    assert r.returncode != 0 and "3.11" in r.stderr


def test_installer_has_no_env_python3_and_no_personal_label():
    s = INSTALLER.read_text()
    assert "/usr/bin/env python3" not in s and "meta" + "zen" not in s


# ---------- nudge ----------

def test_nudge_fires_only_for_big_context(tmp_path):
    def usage(n):
        return json.dumps({"type": "assistant", "message": {"usage": {
            "input_tokens": 1, "cache_read_input_tokens": n, "cache_creation_input_tokens": 0}}})
    big, small = tmp_path / "big.jsonl", tmp_path / "small.jsonl"
    big.write_text(usage(10) + "\n" + usage(200_000) + "\n")
    small.write_text(usage(10_000) + "\n")
    assert "/clear" in nudge.message(str(big))
    assert nudge.message(str(small)) == ""
    assert nudge.message(str(tmp_path / "missing.jsonl")) == ""


# ---------- pass 2: names, validated edits, symlinks, yaml, plugins, nudge ----------

def test_norm_table_claude_keeps_hyphen_codex_does_not(fake_home):
    assert tokopt.norm("a-b.c d", "claude") == "a-b_c_d" and tokopt.norm("a-b.c d", "codex") == "a_b_c_d"
    add_server(fake_home, "my-srv")
    write_log(fake_home / ".claude/projects/-p/c.jsonl", [tool_line("my-srv", 3)], 0)
    assert rows_by_name()["my-srv"][0] == "keep"


def test_toml_park_refuses_edit_that_would_corrupt_the_file():
    with pytest.raises(ValueError):
        tokopt.toml_park('[mcp_servers.x]\nargs = [\n["a"]\n]\nenabled = true\n', "x")


def test_toml_edits_reparse_and_change_only_enabled():
    src = '[mcp_servers.a]\ncommand = "x"\nargs = ["1", "2"]\n\n[mcp_servers.b]\nenabled = true\n'
    out = tomllib.loads(tokopt.toml_park(src, "a"))
    assert out["mcp_servers"]["a"] == {"command": "x", "args": ["1", "2"], "enabled": False}
    assert out["mcp_servers"]["b"] == {"enabled": True}
    back = tomllib.loads(tokopt.toml_unpark(tokopt.toml_park(src, "a"), "a"))
    assert back["mcp_servers"]["a"]["enabled"] is True and back["mcp_servers"]["b"] == {"enabled": True}


def test_plugin_edit_is_validated_by_reparsing_json():
    src = '{\n  "enabledPlugins": {\n    "x@m": true,\n    "y@m": true\n  }\n}\n'
    assert json.loads(tokopt.plugin_park(src, "x@m")) == {"enabledPlugins": {"x@m": False, "y@m": True}}
    with pytest.raises(ValueError):
        tokopt.plugin_park('{"enabledPlugins": {"x@m": true,}}', "x@m")  # source is not valid JSON: write nothing


def test_symlinked_config_keeps_its_link_through_park_and_restore(fake_home, monkeypatch):
    link = fake_home / ".claude.json"
    real = fake_home / "dotfiles/claude.json"
    real.parent.mkdir()
    real.write_bytes(link.read_bytes())
    link.unlink()
    link.symlink_to(real)
    run(["apply"], monkeypatch)
    assert link.is_symlink() and "oldsrv" not in json.loads(real.read_text())["mcpServers"]
    run(["restore", "--all"], monkeypatch)
    assert link.is_symlink() and "oldsrv" in json.loads(real.read_text())["mcpServers"]


HERMES_SRC = "# c\nmodel: x\n\nmcp_servers:\n  alpha:\n    url: u\n  beta:\n    command: npx\n    args:\n      - y\nother: 1\n"


def test_yaml_unpark_inverts_park_even_after_unrelated_edit():
    parked = tokopt.yaml_park(HERMES_SRC, "beta")
    assert tokopt.yaml_unpark(parked, "beta") == HERMES_SRC
    out = tokopt.yaml_unpark(parked.replace("model: x", "model: z"), "beta")
    assert "model: z" in out and "  beta:\n    command: npx\n    args:\n      - y\n" in out and "parked" not in out
    only = tokopt.yaml_park("mcp_servers:\n  a:\n    url: u\nz: 1\n", "a")
    assert tokopt.yaml_unpark(only, "a") == "mcp_servers:\n  a:\n    url: u\nz: 1\n"


@pytest.mark.parametrize("src", ["mcp_servers: {a: {url: u}}\n", "mcp_servers:\n\ta:\n    url: u\n",
                                 "mcp_servers:\n  a: &x\n    url: u\n"])
def test_yaml_refuses_layouts_that_are_not_simple_blocks(src):
    with pytest.raises(ValueError):
        tokopt.yaml_park(src, "a")


def test_hermes_restore_succeeds_after_unrelated_edit(fake_home, monkeypatch):
    h = fake_home / ".hermes"
    h.mkdir()
    cfg = h / "config.yaml"
    cfg.write_text(HERMES_SRC.replace("beta", "beta").replace("alpha", "alpha"))
    con = sqlite3.connect(h / "state.db")
    con.execute("create table messages(timestamp real, tool_name text)")
    con.execute("insert into messages values (?, ?)", (time.time() - 40 * DAY, "mcp_beta_x"))
    con.commit()
    con.close()
    tokopt.plan(tokopt.load_policy(), time.time())
    age_first_seen("hermes:mcp:alpha", 40)
    age_first_seen("hermes:mcp:beta", 40)
    confirm()
    run(["apply"], monkeypatch)
    assert "mcp_servers_parked:\n  alpha:" in cfg.read_text()
    cfg.write_text(cfg.read_text().replace("model: x", "model: z"))
    run(["restore", "--all"], monkeypatch)
    out = cfg.read_text()
    assert "model: z" in out and "mcp_servers_parked" not in out and "  alpha:\n    url: u\n" in out


def test_builtin_slash_commands_do_not_credit_a_plugin_with_the_same_command_name(fake_home):
    make_plugin(fake_home, "greptile@m", commands=("login", "review"))
    write_log(fake_home / ".claude/projects/-p/c.jsonl", [cmd_line("/login", 0.1), cmd_line("/review", 0.1)], 0)
    assert "never seen used" in rows_by_name()["greptile@m"][1]
    write_log(fake_home / ".claude/projects/-p/d.jsonl", [cmd_line("/greptile:review", 0.1)], 0)
    assert rows_by_name()["greptile@m"][0] == "keep" and "used" in rows_by_name()["greptile@m"][1]


def run_nudge(stdin: str) -> subprocess.CompletedProcess:
    code = "import sys; from tokopt.nudge import main; sys.exit(main())"
    return subprocess.run([sys.executable, "-c", code], input=stdin, cwd=ROOT, text=True, capture_output=True)


def test_nudge_console_entry_exists_and_always_exits_zero(tmp_path):
    assert tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["scripts"]["tokopt-nudge"] == "tokopt.nudge:main"
    big = tmp_path / "big.jsonl"
    big.write_text(json.dumps({"message": {"usage": {"input_tokens": 1, "cache_read_input_tokens": 200_000}}}) + "\n")
    r = run_nudge(json.dumps({"transcript_path": str(big)}))
    assert r.returncode == 0 and "/clear" in r.stdout
    for bad in ("not json", '{"transcript_path": ["x"]}', "", "[]"):
        r = run_nudge(bad)
        assert r.returncode == 0 and r.stdout == ""


def _mask_scaling(reps: int = 5) -> float:
    """time(mask(4n)) / time(mask(n)), best of `reps` (5) per size: ~4 for linear work, ~16 for quadratic. A ratio, not a
    wall-clock bound, so machine load (which slows both sizes alike) cannot flip it."""
    def best(n: int) -> float:
        runs = []
        for _ in range(reps):
            t0 = time.perf_counter()
            tokopt.mask("token" * n)
            tokopt.mask(("key" + " " * 50) * (n // 2))
            runs.append(time.perf_counter() - t0)
        return min(runs)
    return best(6000) / best(1500)


def test_mask_scales_linearly_on_pathological_input_and_still_masks():
    assert _mask_scaling() < 8
    assert tokopt.mask("API_TOKEN: abc123 password=hunter2") == "API_TOKEN: *** password=***"


def test_the_scaling_check_catches_a_quadratic_secret_regex(monkeypatch):
    import re
    monkeypatch.setattr(tokopt, "SECRET_RE", re.compile(  # unbounded [\w-]* after each keyword: quadratic on "tokentoken..."
        r"(?i)((?:token|key|secret|password|authorization)[\w-]*\s*[:=]\s*)\S+"))
    assert _mask_scaling(reps=1) > 8  # one run per size keeps the proof fast (~2s)


@pytest.mark.parametrize("body", ["mcp_idle_days = 1", 'mcp_idle_days = "28"', "session_idle_hours = 0",
                                  'protect = "x"', "protect = [1]", "cheap_agents = [true]", "skill_idle_days = true"])
def test_policy_validation_refuses_bad_values(fake_home, monkeypatch, capsys, body):
    (fake_home / ".config/tokopt/policy.toml").write_text(body + "\n")
    with pytest.raises(ValueError, match="policy"):
        tokopt.load_policy()
    assert run(["report"], monkeypatch) == 2 and "policy" in capsys.readouterr().out


def test_already_disabled_codex_server_is_not_a_park_candidate(fake_home):
    (fake_home / ".codex").mkdir()
    (fake_home / ".codex/config.toml").write_text('[mcp_servers.off]\nenabled = false\ncommand = "x"\n\n[mcp_servers.on]\ncommand = "y"\n')
    call = json.dumps({"timestamp": iso(40), "payload": {"item": {"type": "McpToolCall", "id": "1", "server": "other"}}})
    write_log(fake_home / ".codex/sessions/2026/01/01/r.jsonl", [call], 40)
    tokopt.plan(tokopt.load_policy(), time.time())
    age_first_seen("codex:mcp:on", 40)
    r = rows_by_name()
    assert "off" not in r and r["on"][0] == "park"


def test_restore_returns_nonzero_when_any_row_is_refused(fake_home, monkeypatch):
    run(["apply"], monkeypatch)
    cj = fake_home / ".claude.json"
    cj.write_text(cj.read_text() + " ")
    assert run(["restore", "--all"], monkeypatch) == 1


def test_config_dirs_honour_claude_config_dir_and_codex_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    c, x = tmp_path / "alt-claude", tmp_path / "alt-codex"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(c))
    monkeypatch.setenv("CODEX_HOME", str(x))
    c.mkdir()
    x.mkdir()
    (c / ".claude.json").write_text(json.dumps({"mcpServers": {"s": {}}}, indent=2) + "\n")
    write_log(c / "projects/-p/a.jsonl", [tool_line("s", 3)], 40)
    (x / "config.toml").write_text('[mcp_servers.t]\ncommand = "x"\n')
    call = json.dumps({"timestamp": iso(3), "payload": {"item": {"type": "McpToolCall", "id": "1", "server": "t"}}})
    write_log(x / "sessions/2026/01/01/r.jsonl", [call], 40)
    r = rows_by_name()
    assert r["s"][0] == "keep" and r["t"][0] == "keep" and "used 3d" in r["s"][1]


def test_tool_use_with_reordered_keys_and_spaces_still_counts(fake_home):
    add_server(fake_home, "reord")
    line = json.dumps({"timestamp": iso(2), "message": {"content": [{"input": {}, "name": "mcp__reord__do", "id": "x", "type": "tool_use"}]}})
    write_log(fake_home / ".claude/projects/-p/c.jsonl", [line], 0)
    assert rows_by_name()["reord"][0] == "keep"


def test_oversized_lines_skipped_and_vanished_files_tolerated(fake_home, monkeypatch):
    add_server(fake_home, "big")
    monkeypatch.setattr(tokopt, "MAX_LINE", 1000, raising=False)
    line = json.loads(tool_line("big", 1))
    line["pad"] = "x" * 2000
    write_log(fake_home / ".claude/projects/-p/c.jsonl", [json.dumps(line)], 0)
    vanish = fake_home / ".claude/projects/-p/vanish.jsonl"
    write_log(vanish, ["{}"], 0)
    real_open = open

    def flaky(path, *a, **k):
        if str(path).endswith("vanish.jsonl"):
            raise FileNotFoundError(path)
        return real_open(path, *a, **k)
    monkeypatch.setattr("builtins.open", flaky)
    act, why = rows_by_name()["big"]
    assert act == "skip" and "incomplete" in why  # the oversized line is skipped AND the scan is flagged incomplete (gate 4)


def test_policy_change_requires_reconfirmation_even_when_scheduled(fake_home, monkeypatch):
    (fake_home / ".config/tokopt/policy.toml").write_text(POLICY.format(protect='["keepsrv", "other"]'))
    assert run(["apply", "--scheduled"], monkeypatch) == 2 and ledger(fake_home) == []
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert run(["apply"], monkeypatch) == 2
    assert run(["apply", "--yes"], monkeypatch) == 0


def test_new_item_scope_requires_reconfirmation(fake_home, monkeypatch):
    confirm(["claude:plugin"])  # claude:mcp was never confirmed
    assert run(["apply", "--scheduled"], monkeypatch) == 2 and ledger(fake_home) == []


def test_legacy_confirmed_marker_is_not_trusted(fake_home, monkeypatch):
    (fake_home / ".local/state/tokopt/confirmed").write_text("x")
    assert run(["apply", "--scheduled"], monkeypatch) == 2


def test_existing_state_dirs_are_tightened_to_0700(fake_home, monkeypatch):
    d = tokopt.state_dir()
    d.chmod(0o755)
    run(["apply"], monkeypatch)
    assert d.stat().st_mode & 0o777 == 0o700 and (d / "backups").stat().st_mode & 0o777 == 0o700


def test_backups_pruned_after_restore_and_when_older_than_30_days(fake_home, monkeypatch):
    bdir = fake_home / ".local/state/tokopt/backups"
    run(["apply"], monkeypatch)
    assert list(bdir.glob("*/*"))
    run(["restore", "--all"], monkeypatch)
    assert not list(bdir.glob("*/*"))  # restored entries drop their backups (full config copies)
    run(["apply"], monkeypatch)
    old = next(bdir.glob("*/*"))
    ns = int((time.time() - 40 * DAY) * 1e9)
    os.utime(old, ns=(ns, ns))
    run(["apply"], monkeypatch)
    assert not old.exists()


def test_json_park_refuses_to_overwrite_an_existing_parked_key():
    src = json.dumps({"mcpServers": {"a": {"v": 2}}, "mcpServersParked": {"a": {"v": 1}}}, indent=2) + "\n"
    with pytest.raises(ValueError, match="already"):
        tokopt.json_park(src, "a")


def test_ledger_bad_lines_are_skipped_with_a_warning(fake_home, capsys):
    (fake_home / ".local/state/tokopt/ledger.jsonl").write_text('garbage\n{"id": "a", "action": "stop"}\n')
    assert [r["id"] for r in tokopt.ledger_rows()] == ["a"]
    assert "ledger" in capsys.readouterr().err


def test_restore_refuses_ledger_rows_pointing_outside_known_configs(fake_home, monkeypatch, capsys):
    victim = fake_home / "victim.txt"
    victim.write_text("precious")
    evil = fake_home / "evil.bak"
    evil.write_text("pwned")
    row = {"id": "bad1", "agent": "claude", "kind": "mcp", "item": "x", "action": "park", "edit": "json",
           "before": {"file": str(victim), "sha256": tokopt.sha(b"pwned")},
           "after": {"file": str(victim), "sha256": tokopt.sha(b"precious"), "backup": str(evil)}}
    tokopt.private_dir(tokopt.state_dir())
    (tokopt.state_dir() / "ledger.jsonl").write_text(json.dumps(row) + "\n")
    assert run(["restore", "bad1"], monkeypatch) == 1
    assert "REFUSED" in capsys.readouterr().out and victim.read_text() == "precious"


# ---------- installer / CI / repo hygiene ----------

def test_installer_plist_has_no_shell_and_is_hardened():
    s = INSTALLER.read_text()
    assert "bash -c" not in s and "<string>/bin/bash</string>" not in s and "PYTHONPATH" not in s
    assert "/usr/sbin" in s and "mkdir -p -m 700" in s and "-m tokopt --version" in s and "xml_escape" in s


def check_path(tmp_path, path: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["/bin/bash", "-c", f'source "{INSTALLER}"; dir_group() {{ echo wheel; }}; check_safe_path "{path}"'], capture_output=True,
                          text=True, env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)})


def test_installer_refuses_synced_and_writable_locations(tmp_path):
    ok = tmp_path / "ok"
    ok.mkdir()
    ok.chmod(0o755)
    assert check_path(tmp_path, ok).returncode == 0
    for bad in ("Dropbox/x", "Library/CloudStorage/y", "Library/Mobile Documents/z"):
        d = tmp_path / bad
        d.mkdir(parents=True)
        assert check_path(tmp_path, d).returncode != 0, bad
    gw = tmp_path / "gw"
    gw.mkdir()
    gw.chmod(0o775)
    assert check_path(tmp_path, gw).returncode != 0


def test_ci_is_hardened():
    t = (ROOT / ".github/workflows/ci.yml").read_text()
    assert re.search(r"(?m)^permissions:\n  contents: read\n", t) and "macos-latest" in t and "requirements-dev.txt" in t and '"3.13"' in t
    uses = re.findall(r"(?m)^\s*-?\s*uses:\s*(\S+)", t)
    assert uses and all(re.search(r"@[0-9a-f]{40}$", u) for u in uses), uses
    dep = (ROOT / ".github/dependabot.yml").read_text()
    assert "github-actions" in dep and "pip" in dep
    assert re.search(r"(?m)^pytest==\d", (ROOT / "requirements-dev.txt").read_text())


def test_gitignore_blocks_secrets_and_caches():
    g = (ROOT / ".gitignore").read_text().split()
    for pat in (".env*", "*.pem", "*.key", ".benchmarks/", "__pycache__/"):
        assert pat in g, pat


def test_readme_documents_backups_symlinks_scheduling_and_old_policies():
    r = (ROOT / "README.md").read_text().lower()
    for phrase in ("full copies", "30 days", "symlink", "never stops sessions", "older versions"):
        assert phrase in r, phrase


def _tracked() -> list[str]:
    top = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    if not top or Path(top).resolve() != ROOT:
        pytest.skip("not a git checkout of this repo (e.g. a `git archive` copy): the tracked-file scan needs `git ls-files`")
    out = subprocess.run(["git", "ls-files", "-co", "--exclude-standard"], cwd=ROOT, capture_output=True, text=True).stdout
    return out.split("\n")[:-1] if out else []


BAD = re.compile("|".join([
    "/Users/" + "mz", "meta" + "zen", r"(?<![\w-])mz" + "@", r"gh[pousr]_[A-Za-z0-9]{20,}", r"sk-[A-Za-z0-9]{20,}",
    r"AKIA[0-9A-Z]{16}", r"xox[bp]-[0-9A-Za-z-]{10,}", "-----BEGIN [A-Z ]*PRIVATE KEY"]).encode())


def test_tracked_files_have_no_personal_strings_secrets_or_bytecode():
    files = _tracked()
    assert files, "git ls-files returned nothing"
    assert [f for f in files if f.endswith(".pyc") or "__pycache__" in f] == []
    hits = []
    for f in files:
        p = ROOT / f
        m = BAD.search(p.read_bytes()) if p.is_file() else None  # bytes: binaries are scanned too
        if m:
            hits.append(f"{f}: {m.group(0)!r}")
    assert hits == []


@pytest.mark.skipif(not (ROOT / ".git").exists(), reason="no git metadata")
def test_git_history_has_no_personal_strings_or_secrets():
    log = subprocess.run(["git", "log", "--all", "-p", "--no-color"], cwd=ROOT, capture_output=True).stdout
    m = BAD.search(log)
    assert m is None, f"history contains {m.group(0)!r}" if m else ""

"""Gate 3: session selection (items 3, 6, 9)."""
from __future__ import annotations

import json

from test_tokopt import LAUNCHD, fake_home, make_plugin, pick, proc  # noqa: F401
from tokopt import cli as tokopt

GH = ("npx", "-y", "@modelcontextprotocol/server-github")


def child(args: str, idle_cmds=frozenset({GH})):
    return pick([LAUNCHD, proc(10, 1, "claude", "claude"), proc(11, 10, "node", args)], idle_cmds=idle_cmds)


# ---------- 3: Codex / Gemini sessions are never stopped ----------

def test_codex_and_gemini_processes_are_never_session_targets():
    for comm, args in (("codex", "codex"), ("codex", "codex exec fix"), ("codex", "codex proto"), ("codex", "codex mcp"),
                       ("gemini", "gemini"), ("codex", "codex app-server")):
        assert pick([LAUNCHD, proc(10, 1, comm, args)]) == [], args


# ---------- 9: idle child = the agent's OWN configured MCP server command, anchored ----------

def test_path_with_mcp_in_it_is_active_work_not_an_idle_server():
    assert child("node /Users/x/proj/mcp-gateway/server.js") == []
    assert child("python3 -m mcp_tests.run") == []
    assert child("npx jest mcp-tests") == []


def test_configured_server_command_is_idle_and_unconfigured_is_active():
    assert child("npx -y @modelcontextprotocol/server-github") == [10]
    assert child("npx -y @modelcontextprotocol/server-github", idle_cmds=frozenset()) == []
    assert child("npx -y @modelcontextprotocol/server-gitlab") == []


def test_match_is_anchored_to_the_whole_command_line_but_tolerates_an_absolute_executable():
    assert child("/opt/homebrew/bin/npx -y @modelcontextprotocol/server-github") == [10]
    assert child("npx -y @modelcontextprotocol/server-github --write-everything") == []
    assert child("sh -c npx -y @modelcontextprotocol/server-github") == []


def test_caffeinate_is_benign_idle_because_claude_itself_spawns_it():
    assert child("caffeinate -i -t 300") == [10]      # real shape; an earlier test assumed it was work


def test_busy_configured_server_still_blocks_stop():
    rows = [LAUNCHD, proc(10, 1, "claude", "claude"), proc(11, 10, "node", "npx -y @modelcontextprotocol/server-github", cpu=30.0)]
    assert pick(rows, idle_cmds=frozenset({GH})) == []


def test_configured_mcp_commands_come_from_claude_json_and_enabled_plugin_mcp_json(fake_home):
    cj = json.loads((fake_home / ".claude.json").read_text())
    cj["mcpServers"]["gh"] = {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"]}
    cj["mcpServers"]["remote"] = {"url": "https://example.invalid/mcp"}          # no command: ignored
    cj["mcpServers"]["bad"] = {"command": "node", "args": [1, None]}              # malformed args: ignored
    cj["projects"] = {"/w": {"mcpServers": {"p": {"command": "uvx", "args": ["srv"]}}}, "/x": "junk"}
    (fake_home / ".claude.json").write_text(json.dumps(cj))
    make_plugin(fake_home, "pl@m", mcp=True)
    d = fake_home / "plugins-src/pl"
    (d / ".mcp.json").write_text(json.dumps({"mcpServers": {"s": {"command": "${CLAUDE_PLUGIN_ROOT}/bin/srv", "args": ["a"]}}}))
    make_plugin(fake_home, "flat@m", mcp=True)
    (fake_home / "plugins-src/flat/.mcp.json").write_text(json.dumps({"s2": {"command": "node", "args": ["${CLAUDE_PLUGIN_ROOT}/s.js"]}}))
    got = tokopt.configured_mcp_commands()
    assert GH in got and ("uvx", "srv") in got
    assert (f"{d}/bin/srv", "a") in got and ("node", f"{fake_home}/plugins-src/flat/s.js") in got
    assert not any(c[0] in ("node",) and len(c) == 3 for c in got)
    assert all(isinstance(t, str) for c in got for t in c)


# ---------- 6: `protect` is honoured for sessions ----------

def test_protect_applies_to_sessions_by_agent_name_or_working_directory():
    rows = [LAUNCHD, proc(10, 1, "claude", "claude"), proc(20, 1, "claude", "claude")]

    def act(r):
        r["cwd"] = "/work/Secret-Project" if r["pid"] == 20 else "/work/other"
        return 1_000_000.0 - 9 * 3600
    got = tokopt.select_idle_sessions(rows, act, {"session_idle_hours": 8, "protect": ["secret-project"]}, 1_000_000.0, 9999)
    assert [s["pid"] for s in got] == [10]
    assert tokopt.select_idle_sessions(rows, act, {"session_idle_hours": 8, "protect": ["claude"]}, 1_000_000.0, 9999) == []

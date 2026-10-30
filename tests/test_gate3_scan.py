"""Gate 3: evidence extraction, hostile input, plugins, policy, masking (items 4, 6, 7, 8, 10, 11, 15, 16)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import tracemalloc
from pathlib import Path

import pytest

from test_tokopt import (ROOT, add_server, age_first_seen, confirm, fake_home, iso, ledger, make_plugin, rows_by_name,  # noqa: F401
                         run, tool_line, write_log)
from tokopt import cli as tokopt


def keys(fn, obj) -> list[str]:
    raw = json.dumps(obj).encode()
    return fn(json.loads(raw), raw)


# ---------- 4: plugins whose hooks / servers are declared inline in plugin.json carry no usage signal ----------

@pytest.mark.parametrize("manifest", [
    {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "x"}]}]}},
    {"hooks": "./custom/hooks.json"},
    {"lspServers": {"x": {"command": "y"}}},
    {"outputStyles": "./styles"},
    {"monitors": [{"name": "m"}]},
])
def test_plugin_declaring_hooks_or_servers_in_its_manifest_is_never_parked(fake_home, manifest):
    make_plugin(fake_home, "inline@m", skills=True)
    man = fake_home / "plugins-src/inline/.claude-plugin"
    man.mkdir(parents=True)
    (man / "plugin.json").write_text(json.dumps({"name": "inline", **manifest}))
    tokopt.plan(tokopt.load_policy(), time.time())
    age_first_seen("claude:plugin:inline@m", 40)
    act, why = rows_by_name()["inline@m"]
    assert act == "skip" and "no usage signal" in why


def test_plugin_with_plain_manifest_still_parks(fake_home):
    make_plugin(fake_home, "plain@m", skills=True)
    man = fake_home / "plugins-src/plain/.claude-plugin"
    man.mkdir(parents=True)
    (man / "plugin.json").write_text(json.dumps({"name": "plain", "skills": "./skills/"}))
    tokopt.plan(tokopt.load_policy(), time.time())
    age_first_seen("claude:plugin:plain@m", 40)
    assert rows_by_name()["plain@m"][0] == "park"


# ---------- 8: more shapes of real usage ----------

def test_codex_namespaced_function_call_counts_as_use_of_that_server():
    rec = {"type": "response_item", "payload": {"type": "function_call", "namespace": "mcp__memory_srv__", "name": "search"}}
    assert keys(tokopt.codex_keys, rec) == ["mcp:memory_srv"]
    flat = {"payload": {"type": "function_call", "name": "mcp__other__tool"}}
    assert keys(tokopt.codex_keys, flat) == ["mcp:other"]
    assert keys(tokopt.codex_keys, {"payload": {"type": "function_call", "namespace": "functions", "name": "shell"}}) == []


def test_claude_mcp_prompt_slash_command_and_resource_tools_count_as_use_of_the_server():
    cmd = {"message": {"content": "<command-name>/mcp__my-srv__review</command-name>"}}
    assert keys(tokopt.claude_keys, cmd) == ["mcp:my-srv"]
    res = {"message": {"content": [{"type": "tool_use", "name": "ReadMcpResourceTool", "input": {"server": "a.b", "uri": "x://y"}},
                                   {"type": "tool_use", "name": "ListMcpResourcesTool", "input": {"server": "my-srv"}},
                                   {"type": "tool_use", "name": "ListMcpResourcesTool", "input": {}}]}}
    assert sorted(keys(tokopt.claude_keys, res)) == ["mcp:a_b", "mcp:my-srv"]


def test_mcp_prompt_use_keeps_the_server_end_to_end(fake_home):
    add_server(fake_home, "prompt-srv")
    line = json.dumps({"timestamp": iso(2), "message": {"content": "<command-name>/mcp__prompt-srv__go</command-name>"}})
    write_log(fake_home / ".claude/projects/-p/c.jsonl", [line], 0)
    assert rows_by_name()["prompt-srv"][0] == "keep"


# ---------- 10: hostile input ----------

def test_walk_is_iterative_and_survives_absurd_depth():
    o: dict = {}
    cur = o
    for _ in range(20_000):
        cur["k"] = {}
        cur = cur["k"]
    assert sum(1 for _ in tokopt._walk(o)) == 20_001


def test_deeply_nested_json_line_is_skipped_not_fatal(fake_home):
    add_server(fake_home, "deep")
    write_log(fake_home / ".claude/projects/-p/c.jsonl", ["[" * 200_000 + ' "tool_use"', tool_line("deep", 1)], 0)
    assert rows_by_name()["deep"][0] == "keep"


def test_single_huge_line_is_streamed_in_bounded_memory(fake_home, monkeypatch):
    add_server(fake_home, "big")
    monkeypatch.setattr(tokopt, "MAX_LINE", 1 << 20)
    f = fake_home / ".claude/projects/-p/huge.jsonl"
    with open(f, "wb") as fh:
        fh.write(b'{"pad":"')
        for _ in range(64):
            fh.write(b"x" * (1 << 20))
        fh.write(b'"}\n' + tool_line("big", 1).encode() + b"\n")
    tracemalloc.start()
    try:
        act, why = rows_by_name()["big"]
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert act == "keep" and "used 1d" in why  # the 64 MiB line holds no evidence needle: skipped silently
    assert peak < 24 * (1 << 20), f"peak {peak >> 20} MiB: the line was read whole"


def test_fifo_named_like_a_log_does_not_hang_the_scan(fake_home):
    os.mkfifo(fake_home / ".claude/projects/-p/pipe.jsonl")
    code = "import time; from tokopt import cli; cli.plan(cli.load_policy(), time.time()); print('ok')"
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=30,
                       env={**os.environ, "HOME": str(fake_home)})
    assert r.stdout.strip() == "ok"


def test_symlinked_log_is_not_followed_and_makes_the_scan_incomplete_so_nothing_parks(fake_home):
    add_server(fake_home, "symsrv")
    elsewhere = fake_home / "elsewhere.jsonl"
    elsewhere.write_text(tool_line("symsrv", 1) + "\n")
    (fake_home / ".claude/projects/-p/link.jsonl").symlink_to(elsewhere)
    tokopt.plan(tokopt.load_policy(), time.time())
    age_first_seen("claude:mcp:symsrv", 40)
    act, why = rows_by_name()["symsrv"]
    assert act == "skip" and "incomplete" in why
    assert rows_by_name()["oldsrv"][0] == "skip"  # one unverifiable log blocks every park for that agent


def test_one_crashing_adapter_is_reported_not_fatal(fake_home, monkeypatch):
    def boom():
        raise RuntimeError("adapter exploded with /secret/path")
    monkeypatch.setattr(tokopt, "ADAPTERS", [tokopt.scan_claude, boom])
    rep = tokopt.report(tokopt.load_policy(), time.time(), [])
    assert "adapter failed: boom" in rep and "oldsrv" in rep and "RuntimeError" in rep
    assert "/secret/path" not in rep


@pytest.mark.parametrize("settings,installed,claude_json", [
    ('[]', '{"plugins": {"x@m": "nope"}}', '{"mcpServers": ["a"]}'),
    ('{"enabledPlugins": ["x@m"]}', '[]', '[]'),
    ('{"enabledPlugins": {"x@m": true}}', '{"plugins": {"x@m": [5]}}', '{"mcpServers": {"a": 1}}'),
    ('{"enabledPlugins": {"x@m": true}}', '{"plugins": {"x@m": [{"installPath": 5}]}}', '{"mcpServers": null}'),
    ('{"enabledPlugins": {"x@m": true}}', '{"plugins": []}', '"str"'),
])
def test_malformed_config_shapes_never_raise(fake_home, settings, installed, claude_json):
    (fake_home / ".claude/settings.json").write_text(settings)
    (fake_home / ".claude/plugins").mkdir(exist_ok=True)
    (fake_home / ".claude/plugins/installed_plugins.json").write_text(installed)
    (fake_home / ".claude.json").write_text(claude_json)
    tokopt.report(tokopt.load_policy(), time.time(), [])
    tokopt.configured_mcp_commands()


# ---------- 11: Authorization: Bearer <token> ----------

@pytest.mark.parametrize("text", ["Authorization: Bearer abc.DEF-123_xyz", "authorization=Bearer sk_live_abcdef", "Bearer abc123def456",
                                  'curl -H "Authori' + 'zation: Bearer tok123456"'])  # split: keeps secret scanners quiet
def test_bearer_tokens_are_masked(text):
    out = tokopt.mask(text)
    for secret in ("abc.DEF-123_xyz", "sk_live_abcdef", "abc123def456", "tok123456"):
        assert secret not in out


# ---------- 6: policy typos are errors ----------

@pytest.mark.parametrize("body,bad", [("session_idle_hour = 4", "session_idle_hour"), ('protected = ["x"]', "protected")])
def test_unknown_policy_keys_are_rejected_with_the_valid_keys(fake_home, monkeypatch, capsys, body, bad):
    (fake_home / ".config/tokopt/policy.toml").write_text(body + "\n")
    with pytest.raises(ValueError, match="unknown") as e:
        tokopt.load_policy()
    assert bad in str(e.value) and "session_idle_hours" in str(e.value) and "protect" in str(e.value)
    assert run(["report"], monkeypatch) == 2 and "unknown" in capsys.readouterr().out


# ---------- 7: a confirmation is bound to the agent config paths ----------

@pytest.mark.parametrize("var", ["CODEX_HOME", "CLAUDE_CONFIG_DIR"])
def test_scheduled_run_with_different_config_paths_is_refused(fake_home, monkeypatch, var):
    alt = fake_home / "alt"
    alt.mkdir()
    monkeypatch.setenv(var, str(alt))
    before = (fake_home / ".claude.json").read_bytes()
    assert run(["apply", "--scheduled"], monkeypatch) == 2
    assert (fake_home / ".claude.json").read_bytes() == before and ledger(fake_home) == []
    monkeypatch.delenv(var)
    assert run(["apply", "--scheduled"], monkeypatch) == 0


# ---------- 15: stray positionals ----------

@pytest.mark.parametrize("argv", [["apply", "typo"], ["report", "oldsrv"]])
def test_stray_positional_arguments_are_rejected(fake_home, monkeypatch, argv):
    with pytest.raises(SystemExit) as e:
        run(argv, monkeypatch)
    assert e.value.code == 2 and ledger(fake_home) == []


# ---------- 16: cheap_agents are plain names; tokopt only reports ----------

def test_cheap_agents_report_lists_agents_that_could_take_haiku_and_edits_nothing(fake_home):
    ad = fake_home / ".claude/agents"
    ad.mkdir(parents=True)
    files = {"report.md": "---\nmodel: opus\n---\nx", "pr.md": "---\nmodel: haiku\n---\nx", "docs.md": "---\nname: docs\n---\nx",
             "aa" + "_other.md": "---\nmodel: opus\n---\nx"}
    for n, t in files.items():
        (ad / n).write_text(t)
    pol = {**tokopt.load_policy(), "cheap_agents": ["report", "pr", "docs", "other", "missing"]}
    rep = tokopt.report(pol, time.time(), [])
    line = next(ln for ln in rep.splitlines() if "haiku" in ln)
    assert "report (opus)" in line and "docs (unpinned)" in line
    assert "pr" not in line.replace("report", "").replace("unpinned", "") and "other" not in line and "missing" not in line
    assert {n: (ad / n).read_text() for n in files} == files


def test_default_policy_names_no_agents_and_never_mentions_vendor_roles():
    assert tokopt.DEFAULTS["cheap_agents"] == [] and "aa" + "_" not in tokopt.DEFAULT_POLICY

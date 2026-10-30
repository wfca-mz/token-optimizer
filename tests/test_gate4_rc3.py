"""Gate 4, reviewer findings rc3 (B1, M1-M3, L1-L3, H1). Seen RED first."""
from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import sys
import pytest

from test_tokopt import fake_home, proc, write_log, iso, run, kill_env, sess, NOW, POLICY  # noqa: F401
from test_gate4 import FIX, hermes_home, hermes_names  # noqa: F401
from tokopt import cli as tokopt

ROOT = Path(__file__).resolve().parent.parent
INSTALLER = ROOT / "scripts/install-tokopt-launchd.sh"


def sh(tmp_path, body: str) -> subprocess.CompletedProcess:
    return subprocess.run(["/bin/bash", "-c", f'source "{INSTALLER}"; {body}'], capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)})


def exe_in(d: Path, name: str = "tokopt", body: str = "#!/bin/sh\nexit 0\n") -> Path:
    d.mkdir(parents=True, exist_ok=True)
    d.chmod(0o755)
    f = d / name
    f.write_text(body)
    f.chmod(0o755)
    return f


# ---------- B1: every hop of a symlink chain must sit in a safe directory ----------

def test_symlink_in_a_world_writable_dir_is_refused_even_though_the_target_is_valid(tmp_path):
    tmp_path.chmod(0o755)
    real = exe_in(tmp_path / "good")
    bad = tmp_path / "swap"
    bad.mkdir()
    bad.chmod(0o777)
    (bad / "tokopt").symlink_to(real)
    r = sh(tmp_path, f'validate_exe "{bad / "tokopt"}"')
    assert r.returncode != 0 and "writable" in r.stderr


@pytest.mark.skipif(sys.platform != "darwin", reason="the launchd installer is macOS-only (Linux /tmp is world-writable, which it refuses by design)")
def test_an_intermediate_hop_in_an_unsafe_dir_is_refused(tmp_path):
    tmp_path.chmod(0o755)
    real = exe_in(tmp_path / "good")
    bad = tmp_path / "swap"
    bad.mkdir()
    (bad / "hop").symlink_to(real)
    bad.chmod(0o777)                                   # the middle link can be re-pointed by anyone
    top = tmp_path / "fine"
    top.mkdir()
    top.chmod(0o755)
    (top / "tokopt").symlink_to(bad / "hop")
    r = sh(tmp_path, f'validate_exe "{top / "tokopt"}"')
    assert r.returncode != 0 and "writable" in r.stderr
    bad.chmod(0o755)
    assert sh(tmp_path, f'validate_exe "{top / "tokopt"}"').returncode == 0


# ---------- M1: anchored matching ----------

@pytest.mark.parametrize("cfg,live,ok", [
    (("${X}/srv",), "sleep", False),
    (("${X}",), "sleep", False),
    (("${X}", "mcp"), "anything mcp", False),
    (("node", "server.js"), "node /any/dir/server.js", False),       # relative config arg never matches an absolute path
    (("node", "./server.js"), "node /any/dir/server.js", False),
    (("node", "server.js"), "node server.js", True),
    (("node", "/a/server.js"), "node /a/server.js", True),
    (("node", "/a/server.js"), "node /b/server.js", False),
    (("srvx", "mcp"), "/opt/bin/srvx mcp", True),                    # bare executable: any directory, same name
    (("docker", "run", "-e", "K=${K}", "img"), "docker run -e K=abc img", True),
    (("docker", "run", "-e", "K=${K}", "img"), "docker run -e K= img", False),
    (("${X}/srv",), "/opt/x/srv", True),                             # literal text AFTER the placeholder must match too
    (("${X}/srv",), "/opt/x/other", False),
    (("${X}/srv",), "/tmp/evil", False),
    (("node", "${ROOT}/server.js"), "node /opt/r/server.js", True),
    (("node", "${ROOT}/server.js"), "node /tmp/evil.js", False),
    (("/${X}",), "/tmp/evil", False),                                # literal part is only `/` or `-`: never matches
    (("tool", "-${X}"), "tool -x", False),
])
def test_config_tokens_are_anchored(cfg, live, ok):
    assert tokopt.is_idle_child(live, {cfg}) is ok


@pytest.mark.parametrize("args,idle", [("/usr/bin/caffeinate -i -t 300", True), ("caffeinate -i -t 300", True),
                                       ("/tmp/evil/caffeinate -i -t 300", False), ("./caffeinate -i -t 300", False)])
def test_only_the_system_caffeinate_counts(args, idle):
    assert tokopt.is_idle_child(args, set()) is idle


# ---------- M2: re-check children right before the signal ----------

def test_child_appearing_after_selection_blocks_the_signal(fake_home, monkeypatch, kill_env):
    monkeypatch.setattr(tokopt, "ps_rows", lambda: [proc(1, 0, "launchd", "launchd"), proc(77, 1, "claude", "claude"),
                                                    proc(900, 77, "make", "make deploy")])
    status, msg = tokopt.stop_session(sess(), {"session_idle_hours": 8, "protect": []}, NOW)
    assert status == "skipped" and "child" in msg and kill_env == []


def test_idle_children_do_not_block_the_final_check(fake_home, monkeypatch, kill_env):
    monkeypatch.setattr(tokopt, "ps_rows", lambda: [proc(1, 0, "launchd", "launchd"), proc(77, 1, "claude", "claude"),
                                                    proc(900, 77, "caffeinate", "caffeinate -i -t 300")])
    status, _ = tokopt.stop_session(sess(), {"session_idle_hours": 8, "protect": []}, NOW)
    assert status == "stopped" and kill_env


# ---------- M3: pip's `#!/bin/sh` + `'''exec'` wrapper ----------

def pip_wrapper(path: Path, interp: str) -> Path:
    path.write_text(f"#!/bin/sh\n'''exec' \"{interp}\" \"$0\" \"$@\"\n' '''\nprint(1)\n")
    path.chmod(0o755)
    return path


@pytest.mark.skipif(sys.platform != "darwin", reason="the launchd installer is macOS-only (Linux /tmp is world-writable, which it refuses by design)")
def test_pip_exec_wrapper_interpreter_is_validated(tmp_path):
    tmp_path.chmod(0o755)
    py = exe_in(tmp_path / "py", "python3")
    assert sh(tmp_path, f'validate_shebang "{pip_wrapper(tmp_path / "w1", str(py))}"').returncode == 0
    bad = tmp_path / "evil"
    bad.mkdir()
    bad.chmod(0o777)
    evil = exe_in(bad, "python3")
    bad.chmod(0o777)
    r = sh(tmp_path, f'validate_shebang "{pip_wrapper(tmp_path / "w2", str(evil))}"')
    assert r.returncode != 0 and "writable" in r.stderr


def test_pip_exec_wrapper_with_an_unparseable_interpreter_is_refused(tmp_path):
    tmp_path.chmod(0o755)
    w = tmp_path / "w"
    w.write_text("#!/bin/sh\n'''exec' python3 \"$0\" \"$@\"\n' '''\n")
    w.chmod(0o755)
    r = sh(tmp_path, f'validate_shebang "{w}"')
    assert r.returncode != 0 and "Refusing" in r.stderr


# ---------- L1: an unlockable state dir fails closed when unattended ----------

def test_scheduled_apply_refuses_when_flock_is_unsupported(fake_home, monkeypatch, capsys):
    import errno

    def nolock(fd, op):
        raise OSError(errno.ENOLCK, "No locks available")
    monkeypatch.setattr(tokopt.fcntl, "flock", nolock)
    before = (fake_home / ".claude.json").read_bytes()
    assert run(["apply", "--scheduled"], monkeypatch) == 2
    assert "cannot lock" in capsys.readouterr().out and (fake_home / ".claude.json").read_bytes() == before
    assert run(["apply", "--yes"], monkeypatch) == 0     # interactive: warns and continues


# ---------- L3: Hermes servers with `enabled: false` are not candidates ----------

def test_hermes_enabled_false_servers_are_not_listed(hermes_home):
    assert "disabled_one" not in hermes_names()
    assert hermes_names() == ["alpha", "rex", "remote", "memory-srv", "node_repl", "computer"]


# ---------- L2: behavioural installer run (stubs for launchctl/tokopt/python) ----------

def run_installer(tmp_path: Path, extra_env: dict, path_dirs: list[Path]) -> subprocess.CompletedProcess:
    tmp_path.chmod(0o755)
    stub = tmp_path / "stub"
    exe_in(stub, "launchctl")
    repo = tmp_path / "repo"
    repo.mkdir()
    repo.chmod(0o755)
    env = {"PATH": ":".join([*map(str, path_dirs), str(stub), "/usr/bin", "/bin"]), "HOME": str(tmp_path),
           "TOKOPT_REPO_ROOT": str(repo), **extra_env}
    return subprocess.run(["/bin/bash", str(INSTALLER)], capture_output=True, text=True, env=env)


@pytest.mark.skipif(sys.platform != "darwin", reason="the launchd installer is macOS-only (Linux /tmp is world-writable, which it refuses by design)")
def test_installer_writes_the_resolved_validated_program_into_the_plist(tmp_path):
    real = exe_in(tmp_path / "pipx/venv/bin", "tokopt")
    link_dir = tmp_path / "local/bin"
    link_dir.mkdir(parents=True)
    link_dir.chmod(0o755)
    (link_dir / "tokopt").symlink_to(real)
    r = run_installer(tmp_path, {}, [link_dir])
    assert r.returncode == 0, r.stderr
    plist = (tmp_path / "Library/LaunchAgents/com.tokopt.agent.plist").read_text()
    assert f"<string>{real.resolve()}</string>" in plist and "<string>--scheduled</string>" in plist


def test_installer_refuses_and_writes_no_plist_for_a_link_in_a_world_writable_dir(tmp_path):
    real = exe_in(tmp_path / "pipx/venv/bin", "tokopt")
    bad = tmp_path / "swap"
    bad.mkdir()
    (bad / "tokopt").symlink_to(real)
    bad.chmod(0o777)
    r = run_installer(tmp_path, {}, [bad])
    assert r.returncode != 0 and "Refusing" in r.stderr
    assert not (tmp_path / "Library/LaunchAgents/com.tokopt.agent.plist").exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="the launchd installer is macOS-only (Linux /tmp is world-writable, which it refuses by design)")
def test_python_route_smoke_test_runs_isolated_with_tokopt_python(tmp_path):
    log = tmp_path / "args.txt"
    py = exe_in(tmp_path / "pybin", "python3", f'#!/bin/sh\necho "$@" > "{log}"\nexit 0\n')
    r = run_installer(tmp_path, {"TOKOPT_PYTHON": str(py)}, [])
    assert r.returncode == 0, r.stderr
    assert log.read_text().strip() == "-I -m tokopt --version"
    plist = (tmp_path / "Library/LaunchAgents/com.tokopt.agent.plist").read_text()
    assert f"<string>{py.resolve()}</string>" in plist and "<string>-m</string>" in plist


# ---------- H1: typed-prefix skip and quoted needles for oversized lines ----------

def big(rec: dict, pad: int, key: str = "text") -> str:
    """A record whose payload text is `pad` bytes, serialised in the real key order."""
    return json.dumps(rec).replace("@@", "p" * pad)


def spec_bad(tmp_path: Path, line: str, spec) -> list[str]:
    f = tmp_path / "x.jsonl"
    f.write_text(line + "\n")
    return tokopt.scan_logs("t", [f], spec)[2]


@pytest.fixture
def small_lines(monkeypatch):
    monkeypatch.setattr(tokopt, "MAX_LINE", 2000)
    monkeypatch.setattr(tokopt, "CHUNK", 1000, raising=False)


def test_function_call_output_is_not_a_function_call_needle(tmp_path, small_lines):
    line = '{"type":"response_item","payload":{"type":"function_call_output","output":"' + "p" * 5000 + '"}}'
    assert spec_bad(tmp_path, line, tokopt.CODEX_SCAN) == []
    line = '{"zzz":"' + "p" * 5000 + '","kind":"function_call_output"}'
    assert spec_bad(tmp_path, line, tokopt.CODEX_SCAN) == []


def test_compacted_and_non_call_response_items_are_skipped_even_when_they_quote_needles(tmp_path, small_lines):
    quoted = 'user text quoting \\"function_call\\" and \\"McpToolCall\\" and \\"mcp__x\\"'
    comp = {"timestamp": "t", "ordinal": 1, "type": "compacted", "payload": {"message": "", "replacement_history": [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "@@" + quoted}]}]}}
    assert spec_bad(tmp_path, big(comp, 5000), tokopt.CODEX_SCAN) == []
    msg = {"timestamp": "t", "ordinal": 2, "type": "response_item", "payload": {"type": "message", "role": "user",
           "content": [{"type": "input_text", "text": "@@"}], "note": "function_call"}}
    msg["payload"]["content"][0]["text"] = "@@" + quoted
    assert spec_bad(tmp_path, big(msg, 5000), tokopt.CODEX_SCAN) == []


def test_function_call_payload_and_untyped_needles_stay_fail_closed(tmp_path, small_lines):
    call = {"timestamp": "t", "ordinal": 3, "type": "response_item",
            "payload": {"type": "function_call", "name": "js", "namespace": "mcp__x", "arguments": "@@"}}
    assert spec_bad(tmp_path, big(call, 5000), tokopt.CODEX_SCAN) == [str(tmp_path / "x.jsonl")]
    item = {"timestamp": "t", "ordinal": 4, "type": "event_msg",
            "payload": {"type": "item_completed", "item": {"type": "McpToolCall", "server": "s", "blob": "@@"}}}
    assert spec_bad(tmp_path, big(item, 5000), tokopt.CODEX_SCAN) == [str(tmp_path / "x.jsonl")]
    assert spec_bad(tmp_path, '{"mystery":"' + "p" * 5000 + '","t":"function_call"}', tokopt.CODEX_SCAN) != []


def test_nested_type_before_the_top_level_type_does_not_fool_the_prefix_check(tmp_path, small_lines):
    line = '{"a":{"type":"compacted"},"payload":{"type":"function_call","x":"' + "p" * 5000 + '"},"type":"response_item"}'
    assert spec_bad(tmp_path, line, tokopt.CODEX_SCAN) != []


def test_needles_are_quoted():
    assert b'"function_call"' in tokopt.CODEX_SCAN[1] and b'"McpToolCall"' in tokopt.CODEX_SCAN[1]
    assert b'"tool_use"' in tokopt.CLAUDE_SCAN[1]

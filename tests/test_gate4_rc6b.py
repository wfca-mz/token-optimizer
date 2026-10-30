"""rc6 security gate F1-F10. Seen RED first."""
from __future__ import annotations

import json
import os
import plistlib
import subprocess
import sys
import time
from pathlib import Path

import pytest

from test_tokopt import fake_home, make_plugin, run  # noqa: F401
from tokopt import cli as tokopt

ROOT = Path(__file__).resolve().parent.parent
INSTALLER = ROOT / "scripts/install-tokopt-launchd.sh"
PLIST = "Library/LaunchAgents/com.tokopt.agent.plist"


def py(code_or_args: list[str], timeout: float = 30) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, *code_or_args], capture_output=True, text=True, timeout=timeout, cwd=ROOT)


def plist_path() -> Path:
    p = Path(os.environ["HOME"]) / PLIST
    p.parent.mkdir(parents=True)
    return p


def ok(r: subprocess.CompletedProcess) -> bool:
    return r.returncode == 0 and "Traceback" not in r.stderr


def doc(arg) -> bytes:
    return plistlib.dumps({"ProgramArguments": [arg, "-m"]}, fmt=plistlib.FMT_BINARY)


# ---------- F1 + the whole plist repro matrix ----------

def write_truncated(p): p.write_bytes(b'<?xml version="1.0"?><plist version="1.0"><dict><key>ProgramArguments</key><array><string>/x')
def write_nested(p): p.write_bytes(b'<?xml version="1.0"?><plist version="1.0">' + b"<array>" * 5000 + b"</array>" * 5000 + b"</plist>")
def write_fifo(p): os.mkfifo(p)
def write_dir(p): p.mkdir()
def write_devzero_link(p): p.symlink_to("/dev/zero")
def write_nul_path(p): p.write_bytes(doc("/nonexistent/py\x00evil"))
def write_empty(p): p.write_bytes(b"")
def write_garbage(p): p.write_bytes(bytes(range(256)) * 8)
def write_bytes_prog(p): p.write_bytes(doc(b"/bin/sh"))
def write_int_prog(p): p.write_bytes(doc(42))
def write_dict_prog(p): p.write_bytes(doc({"a": 1}))


MATRIX = [write_truncated, write_nested, write_fifo, write_dir, write_devzero_link, write_nul_path, write_empty,
          write_garbage, write_bytes_prog, write_int_prog, write_dict_prog]


@pytest.mark.parametrize("make", MATRIX, ids=lambda f: f.__name__[6:])
def test_report_and_check_job_survive_whatever_sits_at_the_plist_path(make):
    make(plist_path())
    r = py(["-m", "tokopt", "report"], timeout=30)
    assert ok(r) and r.stdout.startswith("item"), r.stderr[-300:]
    r = py(["-m", "tokopt", "--check-job"], timeout=30)
    assert ok(r), r.stderr[-300:]


# ---------- F5: invisible / format characters ----------

def test_mask_strips_zero_width_format_and_tag_characters():
    chars = "\u061c\u200b\u200c\u200d\u2060\u2061\u2062\u2063\u2064\ufeff\U000e0000\U000e0041\U000e007f"
    assert tokopt.mask("a" + chars + "b") == "a" + "?" * len(chars) + "b"
    assert tokopt.mask("café 中文 \U0001f600") == "café 中文 \U0001f600"     # real text survives


# ---------- F6: bounded, regular-file-only reads of plugin and session files ----------

def test_report_does_not_hang_on_a_skill_md_that_is_a_symlink_to_dev_zero(fake_home):
    make_plugin(fake_home, "pl@m", skills=True)
    sk = fake_home / "plugins-src/pl/skills/s/SKILL.md"
    sk.unlink()
    sk.symlink_to("/dev/zero")
    r = py(["-m", "tokopt", "report"], timeout=20)
    assert ok(r) and "pl@m" in r.stdout


def test_session_file_symlinked_to_dev_zero_is_just_not_idle(fake_home):
    (fake_home / ".claude/sessions").mkdir(parents=True)
    (fake_home / ".claude/sessions/5.json").symlink_to("/dev/zero")
    r = py(["-c", "from tokopt import cli; print(cli._claude_activity({'pid': 5, 'agent': 'claude'}))"], timeout=20)
    assert ok(r) and r.stdout.strip() == "None"


def test_oversized_session_file_is_ignored(fake_home):
    (fake_home / ".claude/sessions").mkdir(parents=True)
    (fake_home / ".claude/sessions/5.json").write_text('{"kind": "interactive", "pad": "' + "x" * (2 << 20) + '"}')
    assert tokopt._claude_activity({"pid": 5, "agent": "claude"}) is None


# ---------- F7 / F8: unusable state dir ----------

def test_a_dangling_symlink_as_the_state_dir_exits_2_cleanly(fake_home, monkeypatch, capsys):
    state = fake_home / ".local/state/tokopt"
    for f in state.iterdir():
        f.unlink()
    state.rmdir()
    state.symlink_to(fake_home / "nowhere")
    assert run(["apply", "--yes"], monkeypatch) == 2
    assert "state directory" in capsys.readouterr().out


def test_report_survives_an_unwritable_first_seen(fake_home, monkeypatch):
    real = tokopt.atomic_write

    def deny(path, data, mode=0o600, follow=True):
        if Path(path).name == "first_seen.json":
            raise PermissionError(13, "denied")
        real(path, data, mode, follow)
    monkeypatch.setattr(tokopt, "atomic_write", deny)
    (tokopt.state_dir() / "first_seen.json").unlink(missing_ok=True)
    assert tokopt.report(tokopt.load_policy(), time.time(), []).startswith("item")


# ---------- F9: dev pins ----------

def test_dev_requirements_pin_a_fixed_pytest_and_security_md_notes_unhashed_pins():
    assert "pytest==9.0.3" in (ROOT / "requirements-dev.txt").read_text()
    assert "unhashed" in (ROOT / "SECURITY.md").read_text().lower()


# ---------- F10: planted backup link message ----------

def test_restore_names_a_planted_symlink_for_a_backup(fake_home):
    stamp = tokopt.state_dir() / "backups/20200101T000000"
    tokopt.private_dir(stamp)
    (stamp / "x-.claude.json").symlink_to(fake_home / "victim")
    cj = str(fake_home / ".claude.json")
    row = {"id": "r1", "item": "oldsrv", "agent": "claude", "kind": "mcp", "edit": "json",
           "before": {"file": cj, "sha256": "0"}, "after": {"file": cj, "sha256": "1", "backup": str(stamp / "x-.claude.json")}}
    assert "planted symlink" in tokopt._restore_one(row)


# ---------- F2/F3/F4 docs tied to the installer's real branches ----------

def installer_env(tmp_path: Path):
    stub = tmp_path / "stub"
    stub.mkdir()
    for n in ("launchctl",):
        (stub / n).write_text("#!/bin/sh\nexit 0\n")
        (stub / n).chmod(0o755)
    repo = tmp_path / "repo"
    repo.mkdir()
    repo.chmod(0o755)
    tmp_path.chmod(0o755)
    return stub, repo


def test_tokopt_on_path_wins_over_tokopt_python(tmp_path):
    stub, repo = installer_env(tmp_path)
    exe = tmp_path / "bin/tokopt"
    exe.parent.mkdir()
    exe.parent.chmod(0o755)
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(0o755)
    other = tmp_path / "other/python3"
    other.parent.mkdir()
    other.parent.chmod(0o755)
    other.write_text("#!/bin/sh\nexit 0\n")
    other.chmod(0o755)
    r = subprocess.run(["/bin/bash", str(INSTALLER)], capture_output=True, text=True,
                       env={"PATH": f"{exe.parent}:{stub}:/usr/bin:/bin", "HOME": str(tmp_path), "TOKOPT_REPO_ROOT": str(repo),
                            "TOKOPT_PYTHON": str(other)})
    plist = (tmp_path / PLIST).read_text()
    assert r.returncode == 0 and str(exe.resolve()) in plist and str(other) not in plist and "<string>-I</string>" not in plist


def test_symlinked_venv_python_resolves_to_the_base_interpreter(tmp_path):
    base = tmp_path / "base/python3"
    base.parent.mkdir()
    base.parent.chmod(0o755)
    base.write_text("#!/bin/sh\nexit 0\n")
    base.chmod(0o755)
    venv = tmp_path / "venv/bin"
    venv.mkdir(parents=True)
    (venv / "python").symlink_to(base)
    tmp_path.chmod(0o755)
    r = subprocess.run(["/bin/bash", "-c", f'source "{INSTALLER}"; pinned_exe "{venv / "python"}"'], capture_output=True, text=True,
                       env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)})
    assert r.stdout.strip() == str(base.resolve())     # why the docs must not tell people to pass a venv's python


def test_docs_give_the_recipes_that_work_and_the_right_scope():
    sec, readme, change = [(ROOT / n).read_text() for n in ("SECURITY.md", "README.md", "CHANGELOG.md")]
    for text in (sec, readme):
        low = text.lower()
        assert "pass the venv's python" not in low and "pip install --user" not in low
        assert "pipx" in low and "bin directory on path" in low
        assert "only when no `tokopt` is on path" in low and "own site-packages" in low
        assert "per-user pip install is not seen" not in low
        assert "-i" in low and "tokopt_python" in low
    assert "own-uid rows only" not in change and "all uids are read" in change
    assert "tokopt-nudge" in sec and "2 mib" in sec.lower() and "always exits 0" in sec.lower() and "writes nothing" in sec.lower()
    assert "backups/<stamp>" in sec and "backups/<stamp>/" in change

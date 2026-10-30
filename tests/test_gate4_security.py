"""Gate 4 security-review findings F1-F4. Seen RED first."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from test_tokopt import proc  # noqa: F401
from tokopt import cli as tokopt

ROOT = Path(__file__).resolve().parent.parent
INSTALLER = ROOT / "scripts/install-tokopt-launchd.sh"


# ---------- F1: caffeinate is idle only in the exact shape Claude spawns ----------

@pytest.mark.parametrize("args,idle", [
    ("caffeinate -i -t 300", True),
    ("/usr/bin/caffeinate -i -t 300", True),
    ("caffeinate -i ./build.sh", False),            # wraps a utility: that utility is work
    ("caffeinate -i -t 300 make", False),
    ("caffeinate -i -w 4242", False),               # waits on another process
    ("caffeinate -i", False),
    ("caffeinate -i -t abc", False),
    ("caffeinate -dimsu -t 300", False),
])
def test_caffeinate_idle_only_for_the_exact_self_spawned_shape(args, idle):
    assert tokopt.is_idle_child(args, set()) is idle


# ---------- F2: a ${VAR}-only config token must not match arbitrary live tokens ----------

def test_placeholder_only_config_token_never_matches_arbitrary_commands():
    cmds = {("${X}/srv",), ("${X}", "mcp")}
    assert not tokopt.is_idle_child("sleep", cmds)
    assert not tokopt.is_idle_child("sleep mcp", cmds)
    assert not tokopt.is_idle_child("/bin/sh -c evil", cmds)


def test_placeholder_with_a_literal_prefix_still_matches_after_expansion():
    assert tokopt.is_idle_child("docker run -e K=abc img", {("docker", "run", "-e", "K=${K}", "img")})


# ---------- F3 / F4: installer ----------

def sh(tmp_path, body: str) -> subprocess.CompletedProcess:
    return subprocess.run(["/bin/bash", "-c", f'source "{INSTALLER}"; {body}'], capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)})


def make_exe(tmp_path: Path) -> Path:
    tmp_path.chmod(0o755)
    d = tmp_path / "bin"
    d.mkdir()
    d.chmod(0o755)
    f = d / "tokopt"
    f.write_text("#!/bin/sh\nexit 0\n")
    f.chmod(0o755)
    return f


def test_pinned_exe_prints_the_resolved_real_path(tmp_path):
    real = make_exe(tmp_path)
    link = tmp_path / "ln"
    link.symlink_to(real)
    r = sh(tmp_path, f'pinned_exe "{link}"')
    assert r.returncode == 0 and r.stdout.strip() == str(real.resolve()), r.stderr


def test_a_symlink_in_an_unsafe_directory_is_refused_even_if_its_target_is_safe(tmp_path):
    real = make_exe(tmp_path)
    bad = tmp_path / "swap"
    bad.mkdir()
    bad.chmod(0o777)  # anyone can replace the link inside this directory
    link = bad / "tokopt"
    link.symlink_to(real)
    r = sh(tmp_path, f'validate_exe "{link}"')
    assert r.returncode != 0 and "writable" in r.stderr


def test_check_safe_path_fails_closed_when_find_errors(tmp_path):
    d = tmp_path / "d"
    d.mkdir()
    d.chmod(0o755)
    r = sh(tmp_path, f'find() {{ return 1; }}; check_safe_path "{d}"')
    assert r.returncode != 0 and "Refusing" in r.stderr

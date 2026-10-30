"""Gate 3: installer helpers (items 7, 13), run under stock /bin/bash (3.2 on macOS)."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import sys
import pytest

ROOT = Path(__file__).resolve().parent.parent
INSTALLER = ROOT / "scripts/install-tokopt-launchd.sh"


def sh(tmp_path, body: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["/bin/bash", "-c", f'source "{INSTALLER}"; {body}'], capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), **(env or {})})


@pytest.fixture
def exe(tmp_path) -> Path:
    d = tmp_path / "bin"
    d.mkdir()
    d.chmod(0o755)
    tmp_path.chmod(0o755)
    f = d / "tokopt"
    f.write_text("#!/bin/sh\nexit 0\n")
    f.chmod(0o755)
    return f


@pytest.mark.skipif(sys.platform != "darwin", reason="the launchd installer is macOS-only (Linux /tmp is world-writable, which it refuses by design)")
def test_validate_exe_accepts_a_private_absolute_executable(tmp_path, exe):
    r = sh(tmp_path, f'validate_exe "{exe}"')
    assert r.returncode == 0, r.stderr


def test_validate_exe_refuses_relative_missing_and_foreign_owned(tmp_path, exe):
    for arg, msg in (("tokopt", "absolute"), (str(tmp_path / "nope"), "does not exist")):
        r = sh(tmp_path, f'validate_exe "{arg}"')
        assert r.returncode != 0 and msg in r.stderr and "Refusing" in r.stderr, (arg, r.stderr)
    r = sh(tmp_path, f'id() {{ echo 99999; }}; validate_exe "{exe}"')
    assert r.returncode != 0 and "owned" in r.stderr and "Refusing" in r.stderr


def test_validate_exe_checks_every_parent_directory_and_the_file(tmp_path, exe):
    exe.parent.chmod(0o775)  # a group-writable parent lets another account swap the binary
    r = sh(tmp_path, f'dir_group() {{ echo wheel; }}; validate_exe "{exe}"')  # (admin/staff-with-you is accepted: gate 4)
    assert r.returncode != 0 and "writable" in r.stderr and "Refusing" in r.stderr
    exe.parent.chmod(0o755)
    exe.chmod(0o777)
    assert sh(tmp_path, f'validate_exe "{exe}"').returncode != 0


def test_validate_exe_resolves_symlinks_and_checks_the_target(tmp_path, exe):
    bad = tmp_path / "evil"
    bad.mkdir()
    bad.chmod(0o777)
    target = bad / "tokopt"
    target.write_text("#!/bin/sh\n")
    target.chmod(0o755)
    link = tmp_path / "bin/tokopt-link"
    link.symlink_to(target)
    r = sh(tmp_path, f'validate_exe "{link}"')
    assert r.returncode != 0 and "writable" in r.stderr


def test_resolve_exe_returns_the_physical_path(tmp_path, exe):
    link = tmp_path / "ln"
    link.symlink_to(exe.parent)
    r = sh(tmp_path, f'resolve_exe "{link}/tokopt"')
    assert r.stdout.strip() == str(exe.resolve())


def test_log_dir_is_forced_to_0700_even_when_it_pre_exists(tmp_path):
    log = tmp_path / ".local/state/tokopt/logs"
    log.mkdir(parents=True)
    log.chmod(0o755)
    (tmp_path / ".local/state/tokopt").chmod(0o755)
    r = sh(tmp_path, "prepare_log_dir")
    assert r.returncode == 0, r.stderr
    assert log.stat().st_mode & 0o777 == 0o700 and log.parent.stat().st_mode & 0o777 == 0o700


def test_plist_env_carries_agent_config_dirs_when_set_and_escapes_them(tmp_path):
    r = sh(tmp_path, "extra_env_xml", {"CLAUDE_CONFIG_DIR": "/a&b/<c>", "CODEX_HOME": "/x/codex"})
    assert "<key>CLAUDE_CONFIG_DIR</key>" in r.stdout and "<string>/a&amp;b/&lt;c&gt;</string>" in r.stdout
    assert "<key>CODEX_HOME</key>" in r.stdout and "<string>/x/codex</string>" in r.stdout
    assert sh(tmp_path, "extra_env_xml").stdout.strip() == ""


def test_installer_uses_validation_and_env_helpers_and_stays_bash32_safe():
    s = INSTALLER.read_text()
    assert "validate_exe" in s and "extra_env_xml" in s and "prepare_log_dir" in s
    for banned in ("mapfile", "readarray", "declare -A", "${!", ",,}", "^^}", "&>>"):
        assert banned not in s, banned
    if shutil.which("shellcheck"):
        r = subprocess.run(["shellcheck", "-s", "bash", str(INSTALLER)], capture_output=True, text=True)
        assert r.returncode == 0, r.stdout

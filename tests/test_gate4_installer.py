"""Gate 4: installer treats a stock Homebrew Python and a pipx interpreter the same way. Run under /bin/bash 3.2."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
INSTALLER = ROOT / "scripts/install-tokopt-launchd.sh"
# a stock macOS admin account: group admin, directory owned by me, I am a member of admin
ADMIN = 'dir_group() { echo admin; }; dir_owner() { echo me; }; current_user() { echo me; }; user_in_group() { [ "$1" = admin ]; }'


def sh(tmp_path, body: str) -> subprocess.CompletedProcess:
    return subprocess.run(["/bin/bash", "-c", f'source "{INSTALLER}"; {body}'], capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)})


def tree(tmp_path: Path, mode: int) -> Path:
    """<tmp>/opt/homebrew/Cellar/python/bin/python3, every directory at `mode` (Homebrew default: 775 user:admin)."""
    tmp_path.chmod(0o755)
    d = tmp_path
    for part in ("opt", "homebrew", "Cellar", "python", "bin"):
        d = d / part
        d.mkdir()
        d.chmod(mode)
    exe = d / "python3"
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(0o755)
    return exe


def test_stock_homebrew_layout_775_admin_owned_by_me_is_accepted(tmp_path):
    exe = tree(tmp_path, 0o775)
    r = sh(tmp_path, f'{ADMIN}; validate_exe "{exe}"')
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize("stub,why", [
    (ADMIN + "; dir_group() { echo wheel; }", "other group"),
    (ADMIN + "; dir_group() { echo staff; }; user_in_group() { return 0; }", "staff is effectively every local user"),
    (ADMIN + "; user_in_group() { return 1; }", "user not a member of admin"),
    (ADMIN + "; dir_owner() { echo root; }", "admin-group dir owned by root"),
    (ADMIN + "; dir_owner() { echo someone; }", "admin-group dir owned by another user"),
])
def test_group_writable_is_refused_unless_group_is_admin_owned_by_me_and_i_am_a_member(tmp_path, stub, why):
    exe = tree(tmp_path, 0o775)
    r = sh(tmp_path, f'{stub}; validate_exe "{exe}"')
    assert r.returncode != 0 and "writable" in r.stderr and "Refusing" in r.stderr, why


def test_world_writable_is_always_refused_even_for_admin(tmp_path):
    exe = tree(tmp_path, 0o775)
    exe.parent.parent.chmod(0o777)
    r = sh(tmp_path, f'{ADMIN}; validate_exe "{exe}"')
    assert r.returncode != 0 and "world-writable" in r.stderr


def test_group_writable_file_is_refused_even_in_admin(tmp_path):
    exe = tree(tmp_path, 0o755)
    exe.chmod(0o775)
    assert sh(tmp_path, f'{ADMIN}; validate_exe "{exe}"').returncode != 0


def test_cloud_synced_paths_are_still_refused_for_admin(tmp_path):
    d = tmp_path / "Dropbox/x"
    d.mkdir(parents=True)
    d.chmod(0o755)
    assert sh(tmp_path, f'{ADMIN}; check_safe_path "{d}"').returncode != 0


def script(path: Path, shebang: str) -> Path:
    path.write_text(f"{shebang}\nprint(1)\n")
    path.chmod(0o755)
    return path


def test_shebang_interpreter_is_validated_like_the_program(tmp_path):
    py = tree(tmp_path, 0o755)
    ok = script(tmp_path / "tokopt", f"#!{py}")
    assert sh(tmp_path, f'validate_shebang "{ok}"').returncode == 0
    bad_dir = tmp_path / "bad"
    bad_dir.mkdir()
    bad_dir.chmod(0o777)
    evil = bad_dir / "python3"
    evil.write_text("#!/bin/sh\n")
    evil.chmod(0o755)
    r = sh(tmp_path, f'validate_shebang "{script(tmp_path / "t2", f"#!{evil}")}"')
    assert r.returncode != 0 and "writable" in r.stderr


def test_shebang_via_env_or_missing_interpreter_is_refused_and_binaries_are_skipped(tmp_path):
    tree(tmp_path, 0o755)
    r = sh(tmp_path, f'validate_shebang "{script(tmp_path / "a", "#!/usr/bin/env python3")}"')
    assert r.returncode != 0 and "env" in r.stderr
    assert sh(tmp_path, f'validate_shebang "{script(tmp_path / "b", "#!/nonexistent/python")}"').returncode != 0
    binary = tmp_path / "bin.out"
    binary.write_bytes(b"\xcf\xfa\xed\xfe\x00\x00")
    binary.chmod(0o755)
    assert sh(tmp_path, f'validate_shebang "{binary}"').returncode == 0

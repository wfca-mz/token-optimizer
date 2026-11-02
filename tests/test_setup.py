"""`tokopt setup` wizard. Fake HOME (conftest); launchd and the installer script are always stubbed."""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import tomllib
import stat
import sys
from pathlib import Path

import pytest

from conftest import snapshot
from tokopt import cli, setup as wiz

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests/fixtures/real_claude_settings.json"
NUDGE = "/opt/venv/bin/tokopt-nudge"


def h() -> Path:
    return Path(os.environ["HOME"])


def settings() -> Path:
    return h() / ".claude/settings.json"


def put_settings(text: str | None = None) -> Path:
    p = settings()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(FIXTURE.read_text() if text is None else text)
    p.chmod(0o600)
    return p


def nudges(path: Path | None = None) -> list[str]:
    d = json.loads((path or settings()).read_text())
    return [x["command"] for e in d["hooks"]["UserPromptSubmit"] for x in e["hooks"] if "tokopt-nudge" in x["command"]]


REAL_CONSOLE_SCRIPT = wiz._console_script  # the autouse fixture replaces these; tests that need the real ones re-set them
REAL_RUN_INSTALLER = wiz._run_installer
darwin_only = pytest.mark.skipif(sys.platform != "darwin", reason="tmp dirs on Linux sit under a world-writable /tmp, which the trust chain refuses by design (same as the installer tests)")


@pytest.fixture(autouse=True)
def stubs(monkeypatch):
    calls = {"installer": 0, "installer_args": []}
    monkeypatch.setattr(wiz, "_console_script", lambda name: f"/opt/venv/bin/{name}")
    monkeypatch.setattr(wiz, "_run_installer", lambda *args: calls["installer_args"].append(args) or (
        calls.__setitem__("installer", calls["installer"] + (not args)) or 0))
    monkeypatch.setattr(wiz, "_is_tty", lambda: False)
    monkeypatch.setattr(sys, "platform", "darwin")
    return calls


def tty(monkeypatch, answers: list[str]):
    monkeypatch.setattr(wiz, "_is_tty", lambda: True)
    it = iter(answers)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(it))


# ---------- pure hook edits ----------

def test_hook_add_appends_once_and_keeps_everything_else():
    text = FIXTURE.read_text()
    out = wiz.hook_add(text, NUDGE)
    before, after = json.loads(text), json.loads(out)
    assert after["hooks"]["UserPromptSubmit"][:2] == before["hooks"]["UserPromptSubmit"]
    assert after["hooks"]["UserPromptSubmit"][2]["hooks"][0]["command"] == NUDGE
    after["hooks"]["UserPromptSubmit"].pop()
    assert after == before  # nothing else touched
    assert out.endswith("\n")


def test_hook_add_is_idempotent_for_our_command():
    out = wiz.hook_add(FIXTURE.read_text(), NUDGE)
    with pytest.raises(cli.NoChange):
        wiz.hook_add(out, NUDGE)


def nudges_in(text: str) -> list[str]:
    d = json.loads(text)
    return [x["command"] for e in d["hooks"]["UserPromptSubmit"] for x in e["hooks"] if "nudge" in x["command"]]


def test_a_users_own_tokopt_nudge_elsewhere_is_not_ours_and_survives_add_and_remove(tmp_path):
    own = tmp_path / "tokopt-nudge"  # exists: not stale, so it is the user's to keep
    own.write_text("#!/bin/sh\n")
    mine = str(own)
    base = wiz.hook_add(FIXTURE.read_text(), mine)
    both = wiz.hook_add(base, NUDGE)  # not a duplicate of ours: added next to it
    assert len(nudges_in(both)) == 2
    assert nudges_in(wiz.hook_remove(both, NUDGE)) == [mine]
    with pytest.raises(cli.NoChange):
        wiz.hook_remove(base, NUDGE)  # only the user's hook present: uninstall must not touch it


def test_a_stale_tokopt_hook_is_found_replaced_in_place_and_removed_but_nothing_else_is(tmp_path):
    gone = "/gone/venv/bin/tokopt-nudge"
    gone_py = "/gone/venv/bin/python -I -m tokopt.nudge"
    base = wiz.hook_add(wiz.hook_add(FIXTURE.read_text(), gone), gone_py)
    assert wiz.stale_hooks(base, NUDGE) == [gone, gone_py]
    assert wiz.stale_hooks(FIXTURE.read_text(), NUDGE) == []  # /path/to/hooks/*.sh are not tokopt hooks
    swapped = wiz.hook_add(base, NUDGE, replace_stale=True)
    assert nudges_in(swapped) == [NUDGE]  # first stale rewritten in place, the other stale one dropped: one hook left
    both = wiz.hook_add(base, NUDGE)  # without replace_stale: appended, stale ones untouched
    assert nudges_in(both) == [gone, gone_py, NUDGE]
    assert wiz.hook_remove(both, NUDGE) == FIXTURE.read_text()  # ours AND the stale ones go; the user's hooks stay


def test_hook_remove_keeps_other_hooks_that_share_an_entry_with_ours():
    d = json.loads(wiz.hook_add(FIXTURE.read_text(), NUDGE))
    entry = d["hooks"]["UserPromptSubmit"][-1]
    entry["hooks"].insert(0, {"type": "command", "command": "/path/to/hooks/other.sh", "timeout": 3})
    out = wiz.hook_remove(canon(d), NUDGE)
    kept = json.loads(out)["hooks"]["UserPromptSubmit"][-1]["hooks"]
    assert [x["command"] for x in kept] == ["/path/to/hooks/other.sh"]


def test_setup_reports_and_replaces_a_stale_hook_path(capsys):
    put_settings(wiz.hook_add(FIXTURE.read_text(), "/gone/venv/bin/tokopt-nudge"))
    cli.main(["setup", "--yes"])
    out = capsys.readouterr().out
    assert "no longer exists" in out and "replaced" in out and "already installed" not in out
    assert nudges() == [NUDGE]


def test_hook_add_creates_the_hooks_structure_when_absent():
    d = json.loads(wiz.hook_add('{\n  "theme": "dark"\n}\n', NUDGE))
    assert d["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"] == NUDGE and d["theme"] == "dark"


def canon(o) -> str:
    return json.dumps(o, indent=2, ensure_ascii=False) + "\n"


@pytest.mark.parametrize("bad", ["", "{not json", canon([]), canon({"hooks": []}),
                                 canon({"hooks": {"UserPromptSubmit": {}}}),
                                 '{"theme":"x"}'])  # last: valid JSON but not canonical 2-space: refuse to reformat
def test_hook_add_refuses_what_it_cannot_edit_safely(bad):
    with pytest.raises(ValueError):
        wiz.hook_add(bad, NUDGE)


def test_hook_remove_takes_only_our_entry_and_prunes_what_it_emptied():
    out = wiz.hook_add(FIXTURE.read_text(), NUDGE)
    assert wiz.hook_remove(out, NUDGE) == FIXTURE.read_text()
    only = wiz.hook_add("{}\n", NUDGE)
    assert json.loads(wiz.hook_remove(only, NUDGE)) == {}
    with pytest.raises(cli.NoChange):
        wiz.hook_remove(FIXTURE.read_text(), NUDGE)


def test_nudge_command_falls_back_to_python_module_when_no_console_script(monkeypatch):
    monkeypatch.setattr(wiz, "_console_script", lambda name: None)
    monkeypatch.setattr(wiz, "_safe_exe", lambda p: p)
    monkeypatch.setattr(wiz, "_can_import_nudge", lambda py: True)
    c = wiz.nudge_command()
    assert c.endswith("-I -m tokopt.nudge") and sys.executable in c  # -I: the hook runs in any cwd, which must not be importable


# ---------- guard rails ----------

def test_non_tty_without_yes_refuses_and_never_reads_input(monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda *_: pytest.fail("must not prompt"))
    put_settings()
    before = snapshot([h()])
    assert cli.main(["setup"]) == 2
    assert "--yes" in capsys.readouterr().out
    assert snapshot([h()]) == before


def test_dry_run_writes_nothing_anywhere_and_names_every_step(capsys):
    put_settings()
    (h() / ".codex").mkdir()
    (h() / ".gemini").mkdir()
    before = snapshot([h()])
    assert cli.main(["setup", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert snapshot([h()]) == before
    assert not (h() / ".config/tokopt").exists() and not (h() / ".local/state/tokopt").exists()
    for needle in ("[1/6]", "[2/6]", "[3/6]", "[4/6]", "[5/6]", "[6/6]", NUDGE, "dry run"):
        assert needle in out, needle


def test_dry_run_with_yes_still_writes_nothing(monkeypatch, stubs):
    put_settings()
    before = snapshot([h()])
    assert cli.main(["setup", "--dry-run", "--yes"]) == 0
    assert snapshot([h()]) == before and stubs["installer"] == 0


# ---------- step 1: detection ----------

def test_detection_lists_found_agents_and_what_tokopt_can_do(capsys):
    put_settings()
    (h() / ".gemini").mkdir()
    cli.main(["setup", "--dry-run"])
    out = capsys.readouterr().out
    assert "claude" in out and "found" in out
    assert "gemini" in out and "read-only" in out
    assert "codex" in out and "not found" in out


def test_detection_agrees_with_the_adapters():
    for d in (".claude", ".codex", ".hermes", ".gemini"):
        (h() / d).mkdir(exist_ok=True)
        found = {a for a, ok in wiz.detect_agents().items() if ok}
        scans = {s.agent for fn in cli.ADAPTERS if (s := fn())}
        assert found == scans, d


# ---------- step 2: policy ----------

def test_yes_creates_the_default_policy_without_prompting(capsys):
    cli.main(["setup", "--yes"])
    assert (h() / ".config/tokopt/policy.toml").read_text() == cli.DEFAULT_POLICY


def test_interactive_policy_edit_validates_and_writes(monkeypatch):
    put_settings()
    # change policy? y | mcp days: "3" (rejected, < 7) then "10" | skill: enter | hours: "abc" then "" | protect | cheap
    # then: report n, hook n, launchd n, apply n
    tty(monkeypatch, ["y", "3", "10", "", "abc", "", "keepme, other", "summarizer", "n", "n", "n", "n"])
    assert cli.main(["setup"]) == 0
    p = cli.load_policy()
    assert p["mcp_idle_days"] == 10 and p["skill_idle_days"] == 28 and p["session_idle_hours"] == 8
    assert p["protect"] == ["keepme", "other"] and p["cheap_agents"] == ["summarizer"]
    assert "# tokopt policy" in (h() / ".config/tokopt/policy.toml").read_text()  # comments survive
    assert any((h() / ".local/state/tokopt/backups").rglob("*policy.toml"))


def test_policy_edit_refuses_a_multiline_assignment_instead_of_corrupting_it(monkeypatch, capsys):
    pol = h() / ".config/tokopt/policy.toml"
    pol.parent.mkdir(parents=True)
    pol.write_text("mcp_idle_days = 28\nskill_idle_days = 28\nsession_idle_hours = 8\ncheap_agents = []\n"
                   'protect = [\n  "a",\n]\n')
    original = pol.read_text()
    tty(monkeypatch, ["y", "", "", "", "newprot", "", "n", "n", "n", "n"])
    cli.main(["setup"])
    assert pol.read_text() == original
    assert "edit" in capsys.readouterr().out


def test_an_invalid_existing_policy_stops_the_wizard(capsys):
    pol = h() / ".config/tokopt/policy.toml"
    pol.parent.mkdir(parents=True)
    pol.write_text("mcp_idle_days = 1\n")
    assert cli.main(["setup", "--yes"]) == 2
    assert "policy" in capsys.readouterr().out


# ---------- step 4: hook install ----------

def test_yes_installs_the_hook_with_a_private_backup_and_keeps_the_file_mode():
    p = put_settings()
    p.chmod(0o640)
    original = p.read_bytes()
    assert cli.main(["setup", "--yes"]) == 0
    assert nudges() == [NUDGE]
    assert stat.S_IMODE(p.stat().st_mode) == 0o640
    bks = list((h() / ".local/state/tokopt/backups").rglob("*settings.json"))
    assert len(bks) == 1 and bks[0].read_bytes() == original and stat.S_IMODE(bks[0].stat().st_mode) == 0o600
    json.loads(p.read_text())


def test_rerunning_setup_does_not_duplicate_or_back_up_again(capsys):
    put_settings()
    cli.main(["setup", "--yes"])
    capsys.readouterr()
    cli.main(["setup", "--yes"])
    assert len(nudges()) == 1
    assert len(list((h() / ".local/state/tokopt/backups").rglob("*settings.json"))) == 1
    assert "already installed" in capsys.readouterr().out


def test_invalid_settings_json_is_left_untouched(capsys):
    p = put_settings("{broken")
    cli.main(["setup", "--yes"])
    assert p.read_text() == "{broken"
    assert "settings.json" in capsys.readouterr().out
    assert not list((h() / ".local/state/tokopt").rglob("*settings.json"))


def test_hook_that_would_write_invalid_json_aborts_before_writing(monkeypatch):
    p = put_settings()
    original = p.read_bytes()
    monkeypatch.setattr(wiz, "hook_add", lambda text, cmd, replace_stale=False: "{oops")
    cli.main(["setup", "--yes"])
    assert p.read_bytes() == original


def test_missing_settings_file_is_created_private(monkeypatch):
    (h() / ".claude").mkdir()
    cli.main(["setup", "--yes"])
    assert nudges() == [NUDGE] and stat.S_IMODE(settings().stat().st_mode) == 0o600


def test_no_claude_means_no_hook_step_and_no_claude_dir_is_invented(capsys):
    cli.main(["setup", "--yes"])
    assert not (h() / ".claude").exists()
    assert "Claude Code not found" in capsys.readouterr().out


def test_declining_the_hook_changes_nothing(monkeypatch):
    p = put_settings()
    original = p.read_bytes()
    tty(monkeypatch, ["n", "n", "n", "n", "n"])  # policy edit, report, hook, launchd, apply
    cli.main(["setup"])
    assert p.read_bytes() == original


def test_codex_is_reported_not_edited(capsys):
    put_settings()
    c = h() / ".codex"
    c.mkdir()
    (c / "config.toml").write_text('[features]\nmemories = true\n')
    before = snapshot([c])
    cli.main(["setup", "--yes"])
    assert snapshot([c]) == before
    out = capsys.readouterr().out
    assert "codex" in out.lower() and "nudge" in out.lower() and "not supported" in out


# ---------- step 5: scheduling ----------

def test_macos_yes_runs_the_installer_once(stubs):
    cli.main(["setup", "--yes"])
    assert stubs["installer"] == 1


def test_macos_declined_does_not_run_the_installer(monkeypatch, stubs):
    tty(monkeypatch, ["n", "n", "n", "n", "n"])
    cli.main(["setup"])
    assert stubs["installer"] == 0


def test_other_os_prints_the_cron_line_and_never_fails(monkeypatch, stubs, capsys):
    monkeypatch.setattr(sys, "platform", "linux")
    assert cli.main(["setup", "--yes"]) == 0
    out = capsys.readouterr().out
    assert stubs["installer"] == 0
    assert "30 3 * * *" in out and "apply --scheduled" in out and "not supported" in out


def test_installer_failure_is_reported_and_setup_continues(monkeypatch, capsys):
    monkeypatch.setattr(wiz, "_run_installer", lambda *args: 1)
    assert cli.main(["setup", "--yes"]) == 1  # reported and non-zero, but the remaining steps still run
    out = capsys.readouterr().out
    assert "scheduling failed" in out and "[6/6]" in out


def test_installer_script_missing_is_explained(monkeypatch, capsys):
    monkeypatch.setattr(wiz, "_run_installer", REAL_RUN_INSTALLER)
    monkeypatch.setattr(wiz, "_installer_path", lambda: Path("/nonexistent/install.sh"))
    assert wiz._run_installer() != 0
    assert "reinstall" in capsys.readouterr().out


def test_installer_resolves_from_an_installed_layout_not_the_repo(tmp_path):
    """Copy only what a wheel installs (the package dir) to a fake site-packages and import it from there."""
    site = tmp_path / "site-packages"
    shutil.copytree(ROOT / "tokopt", site / "tokopt", ignore=shutil.ignore_patterns("__pycache__"))
    out = subprocess.run([sys.executable, "-I", "-c", "import sys; sys.path.insert(0, sys.argv[1]);"
                          "import tokopt.setup as s; p = s._installer_path(); print(p, p.is_file())", str(site)],
                         capture_output=True, text=True, cwd=tmp_path)
    assert out.returncode == 0, out.stderr
    path, is_file = out.stdout.split()
    assert path.startswith(str(site)) and is_file == "True" and str(ROOT) not in path


def test_the_installer_is_declared_as_package_data_and_scripts_only_forwards_to_it():
    pkg = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["setuptools"]["package-data"]["tokopt"]
    assert "install-tokopt-launchd.sh" in pkg and (ROOT / "tokopt/install-tokopt-launchd.sh").is_file()
    wrapper = (ROOT / "scripts/install-tokopt-launchd.sh").read_text()
    assert "tokopt/install-tokopt-launchd.sh" in wrapper and len(wrapper.splitlines()) < 10  # no second copy to drift
    assert f'LABEL="{wiz.LABEL}"' in (ROOT / "tokopt/install-tokopt-launchd.sh").read_text()  # plist name kept in step


# ---------- step 6: first apply ----------

def test_yes_does_not_apply_by_default(capsys):
    cli.main(["setup", "--yes"])
    assert not (h() / ".local/state/tokopt/confirmed").exists()
    assert "tokopt apply" in capsys.readouterr().out


def test_accepting_apply_goes_through_the_confirmation_gate(monkeypatch):
    put_settings()
    tty(monkeypatch, ["n", "n", "n", "n", "y"])
    argvs, real = [], cli.main
    monkeypatch.setattr(cli, "main", lambda argv=None: argvs.append(list(argv or [])) or real(argv))
    monkeypatch.setattr(cli, "_ask", lambda: True)  # the gate's own y/N prompt
    real(["setup"])
    assert ["apply"] in argvs and ["apply", "--yes"] not in argvs  # the user must see the plan and answer the gate
    assert (h() / ".local/state/tokopt/confirmed").exists()


def test_interactive_apply_shows_the_plan_and_a_gate_no_writes_nothing(monkeypatch, capsys):
    put_settings()
    tty(monkeypatch, ["n", "n", "n", "n", "y"])
    monkeypatch.setattr(cli, "_ask", lambda: False)
    cli.main(["setup"])
    out = capsys.readouterr().out
    assert "item" in out and "Nothing changed" in out
    assert not (h() / ".local/state/tokopt/confirmed").exists()


def test_declining_apply_writes_no_confirmation(monkeypatch):
    put_settings()
    tty(monkeypatch, ["n", "n", "n", "n", "n"])
    cli.main(["setup"])
    assert not (h() / ".local/state/tokopt/confirmed").exists()


# ---------- uninstall ----------

def test_uninstall_removes_hook_and_job_but_keeps_ledger_and_backups(monkeypatch, stubs):
    p = put_settings()
    cli.main(["setup", "--yes"])
    plist = h() / "Library/LaunchAgents/com.tokopt.agent.plist"
    plist.parent.mkdir(parents=True)
    plist.write_text("<plist/>")
    state = h() / ".local/state/tokopt"
    (state / "ledger.jsonl").write_text('{"id":"x"}\n')
    assert cli.main(["setup", "--uninstall", "--yes"]) == 0
    assert nudges(p) == [] and json.loads(p.read_text()) == json.loads(FIXTURE.read_text())
    assert stubs["installer_args"] == [(), ("--uninstall",)]  # the installer removes its own job
    assert (state / "ledger.jsonl").exists() and any((state / "backups").rglob("*settings.json"))


def test_uninstall_dry_run_changes_nothing():
    put_settings()
    cli.main(["setup", "--yes"])
    before = snapshot([h()])
    assert cli.main(["setup", "--uninstall", "--dry-run"]) == 0
    assert snapshot([h()]) == before


def test_uninstall_when_nothing_installed_is_a_clean_noop(capsys):
    put_settings()
    assert cli.main(["setup", "--uninstall", "--yes"]) == 0
    assert "not installed" in capsys.readouterr().out


def test_uninstall_non_tty_without_yes_refuses():
    assert cli.main(["setup", "--uninstall"]) == 2


# ---------- CLI surface ----------

def test_flags_belong_to_setup_only(capsys):
    with pytest.raises(SystemExit):
        cli.main(["report", "--dry-run"])
    with pytest.raises(SystemExit):
        cli.main(["apply", "--uninstall"])


def test_docs_describe_the_wizard():
    readme = (ROOT / "README.md").read_text()
    for needle in ("## Setup wizard", "## Install on another machine", "tokopt setup", "--dry-run", "--uninstall"):
        assert needle in readme, needle
    sec = (ROOT / "SECURITY.md").read_text()
    assert "tokopt setup" in sec and "UserPromptSubmit" in sec
    assert "tokopt setup" in (ROOT / "CHANGELOG.md").read_text()


def real_resolution(monkeypatch):
    monkeypatch.setattr(wiz, "_console_script", REAL_CONSOLE_SCRIPT)  # exercise the real PATH search; HOME stays private


def plant(d: Path, name: str) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    f = d / name
    f.write_text("#!/bin/sh\necho pwned\n")
    f.chmod(0o755)
    return f


@pytest.mark.parametrize("path_of", [lambda cwd: ".", lambda cwd: ":/usr/bin", lambda cwd: "../" + cwd.name + ":/usr/bin",
                                     lambda cwd: "bin"])
def test_a_relative_or_empty_path_entry_never_becomes_the_hook_command(monkeypatch, tmp_path, path_of):
    real_resolution(monkeypatch)
    cwd = tmp_path / "repo"
    plant(cwd, "tokopt-nudge")
    plant(cwd / "bin", "tokopt-nudge")
    plant(cwd, "tokopt")
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("PATH", path_of(cwd))
    for cmd in (wiz.nudge_command(), wiz._tokopt_cmd()):
        if cmd is None:  # nothing safe on this machine (e.g. a group-writable interpreter dir): refusing is correct
            continue
        assert os.path.isabs(shlex.split(cmd)[0]), cmd
        assert str(cwd) not in cmd, cmd


@darwin_only
def test_a_console_script_in_a_group_or_world_writable_dir_is_refused(monkeypatch, tmp_path):
    real_resolution(monkeypatch)
    d = tmp_path / "bin"
    exe = plant(d, "tokopt-nudge")
    monkeypatch.setenv("PATH", str(d))
    d.chmod(0o755)
    assert wiz._console_script("tokopt-nudge") == os.path.realpath(exe)
    d.chmod(0o775)
    assert wiz._console_script("tokopt-nudge") is None
    d.chmod(0o757)
    assert wiz._console_script("tokopt-nudge") is None


def test_unsafe_interpreter_fallback_aborts_the_hook_step(monkeypatch, capsys):
    put_settings()
    monkeypatch.setattr(wiz, "_console_script", lambda name: None)
    monkeypatch.setattr(wiz, "_safe_exe", lambda p: None)
    original = settings().read_bytes()
    assert cli.main(["setup", "--yes"]) == 1
    assert settings().read_bytes() == original
    assert "no safe" in capsys.readouterr().out


def test_ask_int_survives_unicode_digits_and_absurd_lengths(monkeypatch):
    for bad in ("²", "٣", "9" * 5000):  # superscript, Arabic-Indic digit, longer than int()'s conversion limit
        answers = iter([bad, "12"])
        monkeypatch.setattr("builtins.input", lambda p="": next(answers))
        assert wiz._ask_int("x", 28, 7) == 12, bad


def test_prompts_mask_control_characters_in_paths(monkeypatch):
    prompts = []
    monkeypatch.setattr("builtins.input", lambda p="": prompts.append(p) or "n")
    wiz._confirm(type("A", (), {"yes": False})(), "Remove \x1b[31m /tmp/x\ny?", True)
    assert "\x1b" not in prompts[0] and "\n" not in prompts[0]


def test_symlinked_settings_json_is_followed_on_install_and_uninstall(tmp_path):
    real = tmp_path / "dotfiles-settings.json"
    real.write_text(FIXTURE.read_text())
    real.chmod(0o640)
    (h() / ".claude").mkdir()
    settings().symlink_to(real)
    assert cli.main(["setup", "--yes"]) == 0
    assert settings().is_symlink() and nudges(real) == [NUDGE]  # link kept, TARGET replaced atomically
    assert stat.S_IMODE(real.stat().st_mode) == 0o640
    bks = list((h() / ".local/state/tokopt/backups").rglob("*settings.json"))
    assert len(bks) == 1 and bks[0].read_text() == FIXTURE.read_text() and not bks[0].is_symlink()
    assert cli.main(["setup", "--uninstall", "--yes"]) == 0
    assert settings().is_symlink() and real.read_text() == FIXTURE.read_text()


def test_a_symlink_to_something_that_is_not_your_regular_file_is_refused(capsys):
    (h() / ".claude").mkdir()
    settings().symlink_to("/dev/null")
    assert cli.main(["setup", "--yes"]) == 1
    assert "NOT changed" in capsys.readouterr().out and settings().is_symlink()


def test_a_dangling_symlink_is_refused(capsys):
    (h() / ".claude").mkdir()
    settings().symlink_to(h() / "nowhere.json")
    assert cli.main(["setup", "--yes"]) == 1
    assert not (h() / "nowhere.json").exists()


def test_apply_failure_exit_code_reaches_the_wizard_exit_code(monkeypatch):
    put_settings()
    tty(monkeypatch, ["n", "n", "n", "n", "y"])
    monkeypatch.setattr(wiz, "_apply", lambda: 7)
    assert cli.main(["setup"]) == 7


def test_apply_success_leaves_exit_code_zero(monkeypatch):
    put_settings()
    tty(monkeypatch, ["n", "n", "n", "n", "y"])
    monkeypatch.setattr(wiz, "_apply", lambda: 0)
    assert cli.main(["setup"]) == 0


def test_docs_cover_the_audit_notes():
    sec = (ROOT / "SECURITY.md").read_text()
    for needle in ("pinned_exe", "symlink", "cannot park or stop", "never passes `--yes`", "tokopt/install-tokopt-launchd.sh"):
        assert needle in sec, needle
    assert "symlink" in (ROOT / "README.md").read_text().lower()


# ---------- trust chain of the hook program (re-audit F2): one implementation, the installer's ----------

INSTALLER_SH = ROOT / "tokopt/install-tokopt-launchd.sh"


def script(path: Path, body: str = "#!/bin/sh\nexit 0\n", mode: int = 0o755) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(mode)
    return path


def only_path(monkeypatch, *dirs: Path):
    monkeypatch.setattr(wiz, "_console_script", REAL_CONSOLE_SCRIPT)
    monkeypatch.setenv("PATH", os.pathsep.join(str(d) for d in dirs))


@darwin_only
def test_a_console_script_in_a_clean_chain_is_accepted_and_the_resolved_real_path_is_written(monkeypatch, tmp_path):
    real = script(tmp_path / "venv/bin/tokopt-nudge")
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin/tokopt-nudge").symlink_to(real)  # the usual pipx layout: ~/.local/bin/x -> venv/bin/x
    only_path(monkeypatch, tmp_path / "bin")
    assert wiz.nudge_command() == shlex.quote(os.path.realpath(real))  # the file that was checked is the file that runs


@darwin_only
def test_a_symlink_into_a_world_writable_directory_is_refused(monkeypatch, tmp_path):  # (a)
    danger = tmp_path / "danger"
    real = script(danger / "real-nudge")
    danger.chmod(0o777)
    (tmp_path / "safe").mkdir()
    (tmp_path / "safe/tokopt-nudge").symlink_to(real)
    only_path(monkeypatch, tmp_path / "safe")
    assert wiz._console_script("tokopt-nudge") is None


@darwin_only
def test_a_writable_ancestor_above_a_clean_bin_dir_is_refused(monkeypatch, tmp_path):  # (b)
    script(tmp_path / "open/bin/tokopt-nudge")
    (tmp_path / "open").chmod(0o777)
    only_path(monkeypatch, tmp_path / "open/bin")
    assert wiz._console_script("tokopt-nudge") is None


@darwin_only
@pytest.mark.parametrize("shebang", [
    "#!{bad}/python\n",                                  # interpreter in a world-writable directory
    "#!/usr/bin/env python3\n",                         # found through PATH at run time
    "#!/bin/sh\n'''exec' \"{bad}/python\" \"$0\" \"$@\"\n",  # the pip console-script form
    "#!/bin/sh\n'''exec' python3 \"$0\"\n",             # re-exec we cannot parse
])
def test_the_shebang_interpreter_gets_the_same_chain_check(monkeypatch, tmp_path, shebang):  # (c)
    bad = tmp_path / "bad"
    script(bad / "python")
    bad.chmod(0o777)
    script(tmp_path / "bin/tokopt-nudge", shebang.format(bad=bad) + "echo hi\n")
    only_path(monkeypatch, tmp_path / "bin")
    assert wiz._console_script("tokopt-nudge") is None


@darwin_only
def test_a_clean_pip_style_script_with_a_clean_interpreter_is_accepted(monkeypatch, tmp_path):
    py = script(tmp_path / "py/bin/python")
    script(tmp_path / "bin/tokopt-nudge", f"#!/bin/sh\n'''exec' \"{py}\" \"$0\" \"$@\"\n' '''\n")
    only_path(monkeypatch, tmp_path / "bin")
    assert wiz._console_script("tokopt-nudge") == os.path.realpath(tmp_path / "bin/tokopt-nudge")


@darwin_only
def test_a_file_owned_by_another_uid_is_refused(tmp_path):  # (d): the installer's ownership check, with the uid seam
    exe = script(tmp_path / "bin/tokopt-nudge")
    ok = f'source "{INSTALLER_SH}"; pinned_exe "{exe}"'
    run = lambda body: subprocess.run(["/bin/bash", "-c", body], capture_output=True, text=True)  # noqa: E731
    assert run(ok).returncode == 0
    r = run(f'source "{INSTALLER_SH}"; current_uid() {{ echo 4242; }}; pinned_exe "{exe}"')
    assert r.returncode != 0 and "not owned by you" in r.stderr


@darwin_only
def test_setup_uses_the_installers_check_not_a_copy_of_it(monkeypatch, tmp_path):
    exe = script(tmp_path / "bin/tokopt-nudge")
    seen = []
    real_run = subprocess.run
    monkeypatch.setattr(subprocess, "run", lambda cmd, *a, **k: seen.append(cmd) or real_run(cmd, *a, **k))
    assert wiz._safe_exe(str(exe)) == os.path.realpath(exe)
    assert any("pinned_exe" in " ".join(map(str, c)) for c in seen)  # the installer's function, sourced
    assert wiz._safe_exe("relative/tokopt-nudge") is None and wiz._safe_exe(str(tmp_path / "missing")) is None


@darwin_only
def test_interpreter_fallback_is_chain_checked_on_its_real_path(monkeypatch, tmp_path):
    real = script(tmp_path / "realpy/bin/python")
    (tmp_path / "link").mkdir()
    (tmp_path / "link/python").symlink_to(real)
    monkeypatch.setattr(wiz, "_console_script", lambda name: None)
    monkeypatch.setattr(wiz, "_can_import_nudge", lambda py: True)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "link/python"))
    assert wiz.nudge_command() == f"{shlex.quote(os.path.realpath(real))} -I -m tokopt.nudge"
    (tmp_path / "realpy").chmod(0o777)
    assert wiz.nudge_command() is None


# ---------- which hooks count as tokopt's (re-audit F3) ----------

@pytest.mark.parametrize("cmd", [
    "tokopt-nudge",                       # bare: resolved through PATH at run time, we cannot say it is gone
    "python -m tokopt.nudge",
    "python3 -I -m tokopt.nudge",
    "my-wrapper.sh -m tokopt.nudge",      # not a python interpreter
    "/no such/tokopt-nudge",              # unquoted: two words, argv[0] is /no
])
def test_hooks_that_are_not_provably_tokopts_are_never_stale(cmd):
    base = wiz.hook_add(FIXTURE.read_text(), cmd)
    assert wiz.stale_hooks(base, NUDGE) == []
    assert cmd in nudges_in(wiz.hook_add(base, NUDGE, replace_stale=True))  # kept, ours added next to it
    assert cmd in nudges_in(wiz.hook_remove(wiz.hook_add(base, NUDGE), NUDGE))  # and uninstall leaves it


def test_an_existing_tokopt_nudge_by_absolute_path_is_the_users(tmp_path):
    own = script(tmp_path / "tokopt-nudge")
    base = wiz.hook_add(FIXTURE.read_text(), str(own))
    assert wiz.stale_hooks(base, NUDGE) == []


@pytest.mark.parametrize("cmd", ["'/no such/tokopt-nudge'", "/gone/bin/tokopt-nudge", "/gone/python -I -m tokopt.nudge",
                                 "/gone/python3.12 -m tokopt.nudge"])
def test_a_missing_absolute_tokopt_program_is_stale_and_replaced_in_place(cmd):
    base = wiz.hook_add(FIXTURE.read_text(), cmd)
    assert wiz.stale_hooks(base, NUDGE) == [cmd]
    assert nudges_in(wiz.hook_add(base, NUDGE, replace_stale=True)) == [NUDGE]


def test_a_prior_interpreter_fallback_hook_is_replaced_by_the_console_script_without_a_duplicate(tmp_path):
    py = script(tmp_path / "bin/python")  # exists: not "missing", but the fallback form is tokopt's own
    old = f"{py} -I -m tokopt.nudge"
    base = wiz.hook_add(FIXTURE.read_text(), old)
    assert wiz.stale_hooks(base, NUDGE) == [old]
    assert nudges_in(wiz.hook_add(base, NUDGE, replace_stale=True)) == [NUDGE]
    assert wiz.hook_remove(base, NUDGE) == FIXTURE.read_text()


@darwin_only
def test_a_fallback_interpreter_that_cannot_import_tokopt_is_refused(monkeypatch, tmp_path, capsys):
    bad_py = script(tmp_path / "py/bin/python", "#!/bin/sh\nexit 1\n")
    monkeypatch.setattr(wiz, "_console_script", lambda name: None)
    monkeypatch.setattr(wiz, "_safe_exe", lambda p: str(bad_py))
    assert wiz.nudge_command() is None
    assert "cannot import tokopt" in " ".join(wiz.REFUSALS)
    good_py = script(tmp_path / "py2/bin/python", "#!/bin/sh\nexit 0\n")
    monkeypatch.setattr(wiz, "_safe_exe", lambda p: str(good_py))
    assert wiz.nudge_command() == f"{good_py} -I -m tokopt.nudge"
    put_settings()
    monkeypatch.setattr(wiz, "_safe_exe", lambda p: str(bad_py))
    original = settings().read_bytes()
    assert cli.main(["setup", "--yes"]) == 1 and settings().read_bytes() == original
    assert "cannot import tokopt" in capsys.readouterr().out


def test_user_written_hook_forms_survive_install_and_uninstall_untouched():
    cmds = ["tokopt-nudge", "python3 -m tokopt.nudge", "~/.local/bin/tokopt-nudge", "$HOME/.local/bin/tokopt-nudge",
            "FOO=1 /usr/bin/python3 -m tokopt.nudge", "uv run -m tokopt.nudge"]
    text = FIXTURE.read_text()
    for c in cmds:
        text = wiz.hook_add(text, c)
    assert wiz.stale_hooks(text, NUDGE) == []
    installed = wiz.hook_add(text, NUDGE, replace_stale=True)
    assert nudges_in(installed) == [*cmds, NUDGE]
    assert nudges_in(wiz.hook_remove(installed, NUDGE)) == cmds


def test_a_failed_launchd_install_exits_non_zero_even_when_everything_else_worked(monkeypatch):
    monkeypatch.setattr(wiz, "_run_installer", lambda *args: 3)
    assert cli.main(["setup", "--yes"]) == 1


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
@pytest.mark.parametrize("entry,env_root", [("scripts/install-tokopt-launchd.sh", True), ("tokopt/install-tokopt-launchd.sh", False)])
def test_installer_refuses_a_worktree_through_the_wrapper_and_by_default_repo_root(tmp_path, entry, env_root):
    """The wrapper forwards TOKOPT_REPO_ROOT; run directly, repo_root defaults to the script's own directory."""
    tree = tmp_path / ".claude/worktrees/x"
    shutil.copytree(ROOT / "scripts", tree / "scripts")
    shutil.copytree(ROOT / "tokopt", tree / "tokopt", ignore=shutil.ignore_patterns("__pycache__"))
    r = subprocess.run(["/bin/bash", str(tree / entry)], capture_output=True, text=True, cwd=tmp_path,
                       env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)})
    assert r.returncode == 1 and "worktree" in r.stderr, (r.returncode, r.stderr)
    assert "scripts/../" not in r.stderr  # usage/diagnostics show a normalised path


def test_the_current_fallback_command_is_ours_not_stale(tmp_path):
    py = script(tmp_path / "bin/python")
    cmd = f"{py} -I -m tokopt.nudge"
    base = wiz.hook_add(FIXTURE.read_text(), cmd)
    assert wiz.stale_hooks(base, cmd) == []
    with pytest.raises(cli.NoChange):
        wiz.hook_add(base, cmd)


def test_the_canonical_json_refusal_is_what_the_docs_say():
    with pytest.raises(ValueError, match="canonical 2-space JSON"):
        wiz.hook_add('{"theme":"x"}', NUDGE)
    assert "2-space" in (ROOT / "README.md").read_text() and "2-space" in (ROOT / "SECURITY.md").read_text()


def test_the_fixture_is_canonical_two_space_json():
    t = FIXTURE.read_text()
    assert json.dumps(json.loads(t), indent=2, ensure_ascii=False) + "\n" == t

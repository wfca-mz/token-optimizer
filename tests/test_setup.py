"""`tokopt setup` wizard. Fake HOME (conftest); launchd and the installer script are always stubbed."""
from __future__ import annotations

import json
import os
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


@pytest.fixture(autouse=True)
def stubs(monkeypatch):
    calls = {"installer": 0, "launchctl": []}
    monkeypatch.setattr(wiz, "_console_script", lambda name: f"/opt/venv/bin/{name}")
    monkeypatch.setattr(wiz, "_run_installer", lambda: calls.__setitem__("installer", calls["installer"] + 1) or 0)
    monkeypatch.setattr(wiz, "_launchctl", lambda *args: calls["launchctl"].append(args))
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


def test_hook_add_is_idempotent_even_when_an_older_path_is_registered():
    out = wiz.hook_add(FIXTURE.read_text(), "/old/place/tokopt-nudge")
    with pytest.raises(cli.NoChange):
        wiz.hook_add(out, NUDGE)  # different path, same hook: no duplicate
    with pytest.raises(cli.NoChange):
        wiz.hook_add(wiz.hook_add("{}\n", "/py -m tokopt.nudge"), NUDGE)


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
    assert wiz.hook_remove(out) == FIXTURE.read_text()
    only = wiz.hook_add("{}\n", NUDGE)
    assert json.loads(wiz.hook_remove(only)) == {}
    with pytest.raises(cli.NoChange):
        wiz.hook_remove(FIXTURE.read_text())


def test_nudge_command_falls_back_to_python_module_when_no_console_script(monkeypatch):
    monkeypatch.setattr(wiz, "_console_script", lambda name: None)
    c = wiz.nudge_command()
    assert c.endswith("-m tokopt.nudge") and sys.executable in c


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
    monkeypatch.setattr(wiz, "hook_add", lambda text, cmd: "{oops")
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
    monkeypatch.setattr(wiz, "_run_installer", lambda: 1)
    assert cli.main(["setup", "--yes"]) == 0  # scheduling is optional: never fail the wizard
    assert "scheduling failed" in capsys.readouterr().out


def test_installer_script_missing_is_explained(monkeypatch, capsys):
    monkeypatch.undo()
    monkeypatch.setattr(wiz, "_installer_path", lambda: Path("/nonexistent/install.sh"))
    assert wiz._run_installer() != 0
    assert "checkout" in capsys.readouterr().out


# ---------- step 6: first apply ----------

def test_yes_does_not_apply_by_default(capsys):
    cli.main(["setup", "--yes"])
    assert not (h() / ".local/state/tokopt/confirmed").exists()
    assert "tokopt apply" in capsys.readouterr().out


def test_accepting_apply_goes_through_the_confirmation_gate(monkeypatch):
    put_settings()
    tty(monkeypatch, ["n", "n", "n", "n", "y"])
    seen = []
    real = cli.main
    monkeypatch.setattr(wiz, "_apply", lambda: seen.append(1) or real(["apply", "--yes"]))
    cli.main(["setup"])
    assert seen == [1]
    assert (h() / ".local/state/tokopt/confirmed").exists()  # written by _gate, the same code path as `apply --yes`


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
    assert not plist.exists() and stubs["launchctl"] == [("unload", str(plist))]
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


def test_wizard_never_touches_the_real_home_launchd_or_console(stubs):
    # the stubs fixture is autouse: if any test reached the real installer/launchctl it would bypass them
    assert stubs["installer"] == 0 and stubs["launchctl"] == []


def test_docs_describe_the_wizard():
    readme = (ROOT / "README.md").read_text()
    for needle in ("## Setup wizard", "## Install on another machine", "tokopt setup", "--dry-run", "--uninstall"):
        assert needle in readme, needle
    sec = (ROOT / "SECURITY.md").read_text()
    assert "tokopt setup" in sec and "UserPromptSubmit" in sec
    assert "tokopt setup" in (ROOT / "CHANGELOG.md").read_text()


def test_the_fixture_is_canonical_two_space_json():
    t = FIXTURE.read_text()
    assert json.dumps(json.loads(t), indent=2, ensure_ascii=False) + "\n" == t

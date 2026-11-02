"""`tokopt setup`: one-command onboarding of a machine. Every step is shown before it acts and can be skipped.

    tokopt setup [--yes] [--dry-run] [--uninstall]

Builds on tokopt.cli (policy loader, atomic edits, backups, the apply confirmation gate) and the launchd installer
script; it owns only the wizard flow and the edit of the Claude `UserPromptSubmit` hook list.
"""
from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
import sys
import time
import tomllib
import uuid
from pathlib import Path

from tokopt import cli
from tokopt.cli import NoChange, _tilde, atomic_write, claude_dir, home, mask

EVENT = "UserPromptSubmit"
NUDGE_TIMEOUT = 5           # seconds; the hook is advisory and exits 0
STEPS = 6
LABEL = "com.tokopt.agent"
CAPS = {
    "claude": "park MCP servers and plugins; stop idle sessions (SIGTERM); context nudge hook",
    "codex": "park MCP servers; sessions are never stopped; no nudge hook (see step 4)",
    "hermes": "park MCP servers; sessions are never stopped",
    "gemini": "read-only: report only, no readable usage history, nothing is parked or stopped",
}


def say(msg: str = "") -> None:
    print("\n".join(cli._oneline(mask(ln)) for ln in msg.split("\n")))


def _is_tty() -> bool:
    return sys.stdin.isatty()


def _input(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except EOFError:
        return ""


def _confirm(a, question: str, default: bool) -> bool:
    """--yes accepts the step's default (never an unrequested `yes`); otherwise ask."""
    if a.yes:
        say(f"{question} -> {'yes' if default else 'no'} (--yes: default)")
        return default
    ans = _input(f"{question} [{'Y/n' if default else 'y/N'}] ").lower()
    return default if not ans else ans in ("y", "yes")


# ---------- hook edit (pure text -> text, same contract as the park edits) ----------

def _is_nudge(command: object) -> bool:
    try:
        t = shlex.split(command) if isinstance(command, str) else []
    except ValueError:
        return False
    return bool(t) and (Path(t[0]).name == "tokopt-nudge" or t[-2:] == ["-m", "tokopt.nudge"])


def _event_list(data: dict, create: bool) -> list | None:
    hooks = data.get("hooks")
    if hooks is None and create:
        hooks = data["hooks"] = {}
    if hooks is None:
        return None
    if not isinstance(hooks, dict):
        raise ValueError("settings.json `hooks` is not an object; refusing to edit it")
    lst = hooks.get(EVENT)
    if lst is None and create:
        lst = hooks[EVENT] = []
    if lst is not None and not isinstance(lst, list):
        raise ValueError(f"settings.json hooks.{EVENT} is not a list; refusing to edit it")
    return lst


def _entry_hooks(entry: object) -> list:
    inner = entry.get("hooks") if isinstance(entry, dict) else None
    return inner if isinstance(inner, list) else []


def _has_nudge(lst: list) -> bool:
    return any(isinstance(h, dict) and _is_nudge(h.get("command")) for e in lst for h in _entry_hooks(e))


def hook_add(text: str, command: str) -> str:
    data = cli._canonical_json(text)
    if not isinstance(data, dict):
        raise ValueError("settings.json is not a JSON object")
    lst = _event_list(data, create=True)
    assert lst is not None
    if _has_nudge(lst):
        raise NoChange("the nudge hook is already installed")
    lst.append({"hooks": [{"type": "command", "command": command, "timeout": NUDGE_TIMEOUT}]})
    return cli._dump_json(data, text)


def hook_remove(text: str) -> str:
    data = cli._canonical_json(text)
    if not isinstance(data, dict):
        raise ValueError("settings.json is not a JSON object")
    lst = _event_list(data, create=False)
    if lst is None or not _has_nudge(lst):
        raise NoChange("the nudge hook is not installed")
    for e in list(lst):
        keep = [h for h in _entry_hooks(e) if not (isinstance(h, dict) and _is_nudge(h.get("command")))]
        if len(keep) != len(_entry_hooks(e)):
            if keep:
                e["hooks"] = keep
            else:
                lst.remove(e)  # we emptied this entry: drop it, and only what we emptied
    if not lst:
        del data["hooks"][EVENT]
        if not data["hooks"]:
            del data["hooks"]
    return cli._dump_json(data, text)


def _console_script(name: str) -> str | None:
    """Absolute path of a console script: PATH first, then next to this interpreter (a venv that is not activated)."""
    if (p := shutil.which(name)):
        return p
    sib = Path(sys.executable).parent / name
    return str(sib) if sib.is_file() else None


def nudge_command() -> str:
    exe = _console_script("tokopt-nudge")
    return shlex.quote(exe) if exe else f"{shlex.quote(sys.executable)} -m tokopt.nudge"


def _edit_settings(path: Path, fn, create: bool) -> None:
    """Backup (0600), edit atomically (mode preserved), then re-read: the file on disk must be valid JSON."""
    def checked(text: str) -> str:
        out = fn(text)
        json.loads(out)  # valid after, BEFORE anything is written
        return out

    with cli.state_lock():
        if not path.exists():
            if not create:
                raise NoChange("settings.json does not exist")
            atomic_write(path, checked("{}\n").encode(), 0o600)
        else:
            text = cli._read_small(path).decode()
            json.loads(text)  # valid before; a broken file is the user's to fix
            _, _, bk = cli.edit_file(path, checked, cli.backup_writer(path, time.strftime("%Y%m%dT%H%M%S"), uuid.uuid4().hex[:8]))
            say(f"backup: {_tilde(str(bk))}")
    json.loads(path.read_text())  # the file on disk, not our buffer


# ---------- policy ----------

def _set_policy_values(text: str, new: dict) -> str:
    for k, v in new.items():
        pat = re.compile(rf"^{k}[ \t]*=.*$", re.M)
        line = f"{k} = {json.dumps(v)}"
        text = pat.sub(lambda _m, line=line: line, text, count=1) if pat.search(text) else text.rstrip("\n") + "\n" + line + "\n"
    try:
        raw = tomllib.loads(text)
        cli.validate_policy({**cli.DEFAULTS, **raw})
    except (ValueError, KeyError) as e:
        raise ValueError(f"cannot edit the policy automatically ({e}); edit ~/.config/tokopt/policy.toml yourself") from None
    if any(raw.get(k) != v for k, v in new.items()):
        raise ValueError("cannot edit the policy automatically (multi-line value?); edit ~/.config/tokopt/policy.toml yourself")
    return text


def _ask_int(label: str, cur: int, floor: int) -> int:
    for _ in range(3):
        s = _input(f"  {label} [{cur}] (integer >= {floor}): ")
        if not s:
            return cur
        if s.isdigit() and int(s) >= floor:
            return int(s)
        say(f"  must be an integer >= {floor}")
    say(f"  keeping {cur}")
    return cur


def _ask_list(label: str, cur: list[str]) -> list[str]:
    s = _input(f"  {label} {cur} (comma-separated, Enter keeps): ")
    return [x.strip() for x in s.split(",") if x.strip()] if s else cur


# ---------- system helpers (stubbed in tests: they touch launchd and the repo's installer) ----------

def _installer_path() -> Path:
    return Path(__file__).resolve().parent.parent / "scripts/install-tokopt-launchd.sh"


def _run_installer() -> int:
    p = _installer_path()
    if not p.is_file():
        say(f"The launchd installer ({p.name}) ships only in a source checkout, not in a pipx/pip install: clone the "
            "repository and run scripts/install-tokopt-launchd.sh from it.")
        return 1
    return subprocess.run(["/bin/bash", str(p)], check=False).returncode


def _launchctl(*args: str) -> None:
    try:
        subprocess.run(["/bin/launchctl", *args], check=False, capture_output=True)
    except OSError:
        pass  # best effort, like the installer's own --uninstall


def _apply() -> int:
    return cli.main(["apply", "--yes"])  # the same entry as `tokopt apply --yes`: lock, plan, confirmed marker


def _plist() -> Path:
    return home() / f"Library/LaunchAgents/{LABEL}.plist"


# ---------- steps ----------

def detect_agents() -> dict[str, bool]:
    """Same existence checks as the adapters (tests keep them in step); no scan, so nothing is written."""
    return {"claude": claude_dir().is_dir(), "codex": cli.codex_dir().is_dir(),
            "hermes": (home() / ".hermes").is_dir(), "gemini": (home() / ".gemini").is_dir()}


def _step1() -> dict[str, bool]:
    say(f"[1/{STEPS}] Detecting agents")
    found = detect_agents()
    for name, ok in found.items():
        say(f"  {name:7} {'found    ' if ok else 'not found'}  {CAPS[name] if ok else ''}".rstrip())
    return found


def _step2(a, path: Path) -> dict | None:
    say(f"\n[2/{STEPS}] Policy ({_tilde(str(path))})")
    if not path.exists() and a.dry_run:
        say("  dry run: would create it with the documented defaults")
        policy = dict(cli.DEFAULTS)
    else:
        try:
            policy = cli.load_policy()  # creates the file on first run
        except ValueError as e:
            say(f"tokopt: invalid policy file {_tilde(str(path))}: {e}")
            return None
    for k in cli.DEFAULTS:
        say(f"  {k} = {policy[k]}")
    if a.dry_run or a.yes or not _confirm(a, "Change these values?", False):
        return policy
    new = {"mcp_idle_days": _ask_int("park MCP servers idle for days", policy["mcp_idle_days"], 7),
           "skill_idle_days": _ask_int("park plugins idle for days", policy["skill_idle_days"], 7),
           "session_idle_hours": _ask_int("stop sessions idle for hours", policy["session_idle_hours"], 1),
           "protect": _ask_list("protect (never parked or stopped)", policy["protect"]),
           "cheap_agents": _ask_list("cheap_agents (your subagents that could use haiku)", policy["cheap_agents"])}
    new = {k: v for k, v in new.items() if v != policy[k]}
    if not new:
        return policy
    try:
        with cli.state_lock():
            cli.edit_file(path, lambda t: _set_policy_values(t, new),
                          cli.backup_writer(path, time.strftime("%Y%m%dT%H%M%S"), uuid.uuid4().hex[:8]))
        return cli.load_policy()
    except (ValueError, OSError, RuntimeError) as e:
        say(f"  policy not changed: {e}")
        return policy


def _step3(a, policy: dict) -> None:
    say(f"\n[3/{STEPS}] Report (read-only plan)")
    if a.dry_run:
        say("  dry run: would run `tokopt report` (it caches scans under ~/.local/state/tokopt, so it is skipped)")
    elif _confirm(a, "Run the report now?", True):
        now = time.time()
        say(cli.report(policy, now, cli.idle_sessions(policy, now)))


def _step4(a, found: dict[str, bool]) -> bool:
    """-> False when the hook step failed."""
    say(f"\n[4/{STEPS}] Context nudge hook")
    if found["codex"]:
        say("  Codex: not supported. Codex registers UserPromptSubmit hooks only through plugins (hooks JSON plus a "
            "`trusted_hash` in config.toml) and the nudge reads Claude transcripts, so tokopt does not touch Codex config.")
    if not found["claude"]:
        say("  Claude Code not found: skipped.")
        return True
    path, cmd = claude_dir() / "settings.json", nudge_command()
    say(f"  Adds one {EVENT} entry to {_tilde(str(path))}:\n    {cmd}\n"
        "  (JSON checked before and after; 0600 backup first under ~/.local/state/tokopt/backups; atomic write)")
    if a.dry_run:
        say("  dry run: not written")
        return True
    if not _confirm(a, "Install the nudge hook?", True):
        return True
    try:
        _edit_settings(path, lambda t: hook_add(t, cmd), create=True)
        assert _has_nudge(_event_list(json.loads(path.read_text()), False) or [])
    except NoChange:
        say("  already installed: nothing to do")
    except (ValueError, OSError, RuntimeError, AssertionError, cli.Busy) as e:
        say(f"  {_tilde(str(path))} NOT changed: {e or 'verification failed'}")
        return False
    else:
        say("  installed")
    return True


def _tokopt_cmd() -> str:
    exe = _console_script("tokopt")
    return shlex.quote(exe) if exe else f"{shlex.quote(sys.executable)} -m tokopt"


def _step5(a) -> None:
    say(f"\n[5/{STEPS}] Scheduling")
    if sys.platform != "darwin":
        say(f"  Scheduling is not supported on this OS. Run `{_tokopt_cmd()} apply` once by hand, then add to cron:\n"
            f"    30 3 * * * {_tokopt_cmd()} apply --scheduled")
        return
    say("  Installs a daily 03:30 `tokopt apply --scheduled` launchd job (scripts/install-tokopt-launchd.sh). "
        "It refuses to act until step 6 (or `tokopt apply`) has confirmed a plan.")
    if a.dry_run:
        say("  dry run: not installed")
    elif _confirm(a, "Install the launchd job?", True) and _run_installer() != 0:
        say("  scheduling failed (see above); everything else is set up. Fix it and re-run `tokopt setup`.")


def _step6(a) -> None:
    say(f"\n[6/{STEPS}] First apply")
    say("  Parks idle items and stops idle Claude sessions, with backups and a ledger (`tokopt restore --all` undoes it). "
        "Same path as `tokopt apply --yes`, which records the confirmation scheduled runs need.")
    if a.dry_run:
        say("  dry run: would ask")
    elif _confirm(a, "Run `tokopt apply` now?", False):
        _apply()
    else:
        say("  skipped. When you have read the plan: `tokopt apply`")


def _uninstall(a) -> int:
    say("[uninstall 1/2] Nudge hook")
    path = claude_dir() / "settings.json"
    rc = 0
    if not path.exists():
        say(f"  {_tilde(str(path))} does not exist: not installed")
    elif a.dry_run:
        say(f"  dry run: would remove the tokopt-nudge entry from {_tilde(str(path))} (backup first)")
    elif _confirm(a, f"Remove the tokopt-nudge entry from {_tilde(str(path))}?", True):
        try:
            _edit_settings(path, hook_remove, create=False)
            say("  removed")
        except NoChange:
            say("  not installed")
        except (ValueError, OSError, RuntimeError, cli.Busy) as e:
            say(f"  {_tilde(str(path))} NOT changed: {e}")
            rc = 1
    say("[uninstall 2/2] Scheduled job")
    if sys.platform != "darwin":
        say("  not supported on this OS: remove your cron line by hand")
    elif not _plist().exists():
        say("  not installed")
    elif a.dry_run:
        say(f"  dry run: would unload and delete {_tilde(str(_plist()))}")
    elif _confirm(a, "Remove the launchd job?", True):
        _launchctl("unload", str(_plist()))
        _plist().unlink(missing_ok=True)
        say(f"  removed {LABEL}")
    say("The ledger and backups under ~/.local/state/tokopt are kept; `tokopt restore --all` undoes parked items.")
    return rc


def run(a) -> int:
    if not (a.yes or a.dry_run or _is_tty()):
        say("tokopt: setup is interactive: run it in a terminal, or pass --yes (accept defaults) or --dry-run. Nothing changed.")
        return 2
    if a.dry_run:
        say("tokopt setup: dry run, nothing will be written.\n")
    if a.uninstall:
        return _uninstall(a)
    found = _step1()
    policy = _step2(a, home() / ".config/tokopt/policy.toml")
    if policy is None:
        return 2
    _step3(a, policy)
    ok = _step4(a, found)
    _step5(a)
    _step6(a)
    say("\nDone." if not a.dry_run else "\nDry run complete: nothing was written.")
    return 0 if ok else 1

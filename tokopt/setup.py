"""`tokopt setup`: one-command onboarding of a machine. Every step is shown before it acts and can be skipped.

    tokopt setup [--yes] [--dry-run] [--uninstall]

Builds on tokopt.cli (policy loader, atomic edits, backups, the apply confirmation gate) and the launchd installer
script; it owns only the wizard flow and the edit of the Claude `UserPromptSubmit` hook list.
"""
from __future__ import annotations

import importlib.resources
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import time
import tomllib
import uuid
from pathlib import Path
from typing import cast

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
        return input(cli._oneline(mask(prompt))).strip()  # prompts can carry paths: no terminal controls
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

def _tokens(command: object) -> list[str]:
    try:
        return shlex.split(command) if isinstance(command, str) else []
    except ValueError:
        return []


def _is_ours(command: object, ours: str) -> bool:
    """Exactly the command we would write now (same console script, same file after resolving symlinks)."""
    if command == ours:
        return True
    t, o = _tokens(command), _tokens(ours)
    return len(t) == 1 and len(o) == 1 and os.path.realpath(t[0]) == os.path.realpath(o[0])


def _is_stale(command: object, ours: str) -> bool:
    """A tokopt-shaped hook (`.../tokopt-nudge` or `<python> ... -m tokopt.nudge`) whose program no longer exists
    (an old venv/pipx path). Only this kind is ever replaced or removed besides our own: a tokopt-nudge that still
    exists is the user's to keep."""
    t = _tokens(command)
    shaped = (len(t) == 1 and Path(t[0]).name == "tokopt-nudge") or t[-2:] == ["-m", "tokopt.nudge"]
    return shaped and not _is_ours(command, ours) and not os.path.exists(t[0])


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


def _commands(lst: list):
    return [h for e in lst for h in _entry_hooks(e) if isinstance(h, dict)]


def _settings_obj(text: str) -> dict:
    data = cli._canonical_json(text)
    if not isinstance(data, dict):
        raise ValueError("settings.json is not a JSON object")
    return data


def stale_hooks(text: str, command: str) -> list[str]:
    """Commands of tokopt hooks registered in settings.json whose program is gone (see _is_stale)."""
    lst = _event_list(_settings_obj(text), create=False) or []
    return [h["command"] for h in _commands(lst) if _is_stale(h.get("command"), command)]


def _drop(lst: list, pred) -> None:
    """Remove the hooks matching pred from the entries of lst; an entry that becomes empty is dropped, any other
    hook sharing an entry stays."""
    for e in list(lst):
        keep = [h for h in _entry_hooks(e) if not (isinstance(h, dict) and pred(h))]
        if len(keep) != len(_entry_hooks(e)):
            if keep:
                e["hooks"] = keep
            else:
                lst.remove(e)


def hook_add(text: str, command: str, replace_stale: bool = False) -> str:
    """Append our hook. NoChange if it is already there. With replace_stale, the first stale tokopt hook is rewritten
    in place to `command` (and further stale ones dropped) instead of a new entry being added."""
    data = _settings_obj(text)
    lst = cast(list, _event_list(data, create=True))
    if any(_is_ours(h.get("command"), command) for h in _commands(lst)):
        raise NoChange("the nudge hook is already installed")
    stale = [h for h in _commands(lst) if _is_stale(h.get("command"), command)] if replace_stale else []
    if stale:
        keep = stale[0]
        keep["command"] = command
        _drop(lst, lambda h: h is not keep and h in stale)
    else:
        lst.append({"hooks": [{"type": "command", "command": command, "timeout": NUDGE_TIMEOUT}]})
    return cli._dump_json(data, text)


def hook_remove(text: str, command: str) -> str:
    """Remove our hook and stale tokopt hooks; any other hook, including a user's own tokopt-nudge, stays."""
    data = _settings_obj(text)
    lst = _event_list(data, create=False)
    mine = lambda h: _is_ours(h.get("command"), command) or _is_stale(h.get("command"), command)  # noqa: E731
    if lst is None or not any(mine(h) for h in _commands(lst)):
        raise NoChange("the nudge hook is not installed")
    _drop(lst, mine)
    if not lst:
        del data["hooks"][EVENT]
        if not data["hooks"]:
            del data["hooks"]
    return cli._dump_json(data, text)


def _safe_exe(p: str) -> str | None:
    """`p` as an absolute path if it is an executable regular file that neither it nor its directory lets group or
    others write. The hook runs in every project's cwd on every prompt, so it must never name a relative path or
    something a less-privileged user can swap."""
    if not os.path.isabs(p):
        return None
    try:
        f, d = os.stat(p), os.stat(os.path.dirname(p))
    except OSError:
        return None
    if not (stat.S_ISREG(f.st_mode) and os.access(p, os.X_OK)) or (f.st_mode | d.st_mode) & 0o022:
        return None
    return os.path.abspath(p)


def _console_script(name: str) -> str | None:
    """Safe absolute path of a console script: ABSOLUTE PATH entries only ('.', '' and relative entries mean the
    current directory, i.e. whatever repository the hook later runs in), then next to this interpreter."""
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if os.path.isabs(d) and (p := _safe_exe(os.path.join(d, name))):
            return p
    return _safe_exe(os.path.join(os.path.dirname(os.path.abspath(sys.executable)), name))


def nudge_command() -> str | None:
    """The exact hook command to register, or None when nothing safe exists. The interpreter fallback uses -I so a
    `tokopt/` directory in the prompt's cwd cannot shadow the package."""
    if (exe := _console_script("tokopt-nudge")):
        return shlex.quote(exe)
    py = _safe_exe(sys.executable)
    return f"{shlex.quote(py)} -I -m tokopt.nudge" if py else None


def _edit_settings(path: Path, fn, create: bool) -> None:
    """Backup (0600), edit atomically (mode preserved), then re-read: the file on disk must be valid JSON.
    Symlinks are followed like every agent config edit (dotfile managers link settings.json): the TARGET is replaced
    atomically and the link stays, but only if the target is a regular file owned by you."""
    def checked(text: str) -> str:
        out = fn(text)
        json.loads(out)  # valid after, BEFORE anything is written
        return out

    with cli.state_lock():
        if not path.exists():
            if path.is_symlink():
                raise OSError(f"{path} is a dangling symlink")
            if not create:
                raise NoChange("settings.json does not exist")
            atomic_write(path, checked("{}\n").encode(), 0o600)
        else:
            st = os.stat(path)
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
                raise OSError(f"{os.path.realpath(path)} is not a regular file owned by you; refusing to edit it")
            _, _, bk = cli.edit_file(path, checked, cli.backup_writer(path, time.strftime("%Y%m%dT%H%M%S"), uuid.uuid4().hex[:8]))
            say(f"backup: {_tilde(str(bk))}")
    json.loads(path.read_text())  # the end state on disk, not our buffer


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
        try:
            if s.isascii() and s.isdecimal() and int(s) >= floor:
                return int(s)
        except ValueError:  # a digit string longer than Python's int-conversion limit
            pass
        say(f"  must be an integer >= {floor}")
    say(f"  keeping {cur}")
    return cur


def _ask_list(label: str, cur: list[str]) -> list[str]:
    s = _input(f"  {label} {cur} (comma-separated, Enter keeps): ")
    return [x.strip() for x in s.split(",") if x.strip()] if s else cur


# ---------- system helpers (stubbed in tests: they touch launchd and the repo's installer) ----------

def _installer_path() -> Path:
    """The installer ships inside the package (package data), so it is found the same way in a checkout, a
    pipx venv and a wheel install."""
    return Path(str(importlib.resources.files("tokopt") / "install-tokopt-launchd.sh"))


def _run_installer() -> int:
    p = _installer_path()
    if not p.is_file():
        say(f"The launchd installer is missing from this installation ({p}); reinstall tokopt.")
        return 1
    return subprocess.run(["/bin/bash", str(p)], check=False).returncode


def _launchctl(*args: str) -> None:
    try:
        subprocess.run(["/bin/launchctl", *args], check=False, capture_output=True)
    except OSError:
        pass  # best effort, like the installer's own --uninstall


def _apply() -> int:
    """Plain `tokopt apply`: the gate prints the plan and asks, and only its `y` writes the confirmed marker."""
    return cli.main(["apply"])


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
    if cmd is None:
        say("  no safe tokopt-nudge program found (it must be an absolute path to an executable that neither it nor its "
            "directory lets group/others write): install tokopt with pipx, then re-run `tokopt setup`.")
        return False
    say(f"  Adds one {EVENT} entry to {_tilde(str(path))}:\n    {cmd}\n"
        "  (JSON checked before and after; 0600 backup first under ~/.local/state/tokopt/backups; atomic write)")
    stale = _stale_in(path, cmd)
    if stale:
        say("  A tokopt hook is already registered but its program no longer exists (moved or reinstalled):\n    "
            + "\n    ".join(stale))
    if a.dry_run:
        say("  dry run: not written")
        return True
    if not _confirm(a, "Replace the stale hook?" if stale else "Install the nudge hook?", True):
        return True
    try:
        _edit_settings(path, lambda t: hook_add(t, cmd, replace_stale=True), create=True)
    except NoChange:
        say("  already installed with this command: nothing to do")
    except (ValueError, OSError, RuntimeError) as e:  # cli.Busy is a RuntimeError
        say(f"  {_tilde(str(path))} NOT changed: {e}")
        return False
    else:
        say("  replaced" if stale else "  installed")
    return True


def _stale_in(path: Path, cmd: str) -> list[str]:
    try:
        return stale_hooks(path.read_text(), cmd)
    except (OSError, ValueError):
        return []  # unreadable or invalid: the edit step reports it


def _tokopt_cmd() -> str:
    """Text for the printed cron line only (never written to a config)."""
    exe = _console_script("tokopt")
    return shlex.quote(exe) if exe else f"{shlex.quote(sys.executable)} -I -m tokopt"


def _step5(a) -> None:
    say(f"\n[5/{STEPS}] Scheduling")
    if sys.platform != "darwin":
        say(f"  Scheduling is not supported on this OS. Run `{_tokopt_cmd()} apply` once by hand, then add to cron:\n"
            f"    30 3 * * * {_tokopt_cmd()} apply --scheduled")
        return
    say("  Installs a daily 03:30 `tokopt apply --scheduled` launchd job (the installer script shipped in the package). "
        "It refuses to act until step 6 (or `tokopt apply`) has confirmed a plan.")
    if a.dry_run:
        say("  dry run: not installed")
    elif _confirm(a, "Install the launchd job?", True) and _run_installer() != 0:
        say("  scheduling failed (see above); everything else is set up. Fix it and re-run `tokopt setup`.")


def _step6(a) -> int:
    """-> the exit code of `tokopt apply` (0 when it was not run)."""
    say(f"\n[6/{STEPS}] First apply")
    say("  Parks idle items and stops idle Claude sessions, with backups and a ledger (`tokopt restore --all` undoes it). "
        "This runs plain `tokopt apply`: it shows the plan and asks again; only your `y` records the confirmation "
        "scheduled runs need.")
    if a.dry_run:
        say("  dry run: would ask")
    elif _confirm(a, "Run `tokopt apply` now?", False):
        return _apply()
    else:
        say("  skipped. When you have read the plan: `tokopt apply`")
    return 0


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
            ours = nudge_command() or ""
            _edit_settings(path, lambda t: hook_remove(t, ours), create=False)
            say("  removed")
        except NoChange:
            say("  not installed")
        except (ValueError, OSError, RuntimeError) as e:
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
    rc = _step6(a)
    say("\nDone." if not a.dry_run else "\nDry run complete: nothing was written.")
    return rc or (0 if ok else 1)

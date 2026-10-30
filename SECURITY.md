# Security policy

## Reporting a vulnerability

Please report security problems privately, not in a public issue: use GitHub's "Report a vulnerability" button on the repository's Security tab (a private security advisory). Include the version (`tokopt --version`), what you expected and what happened. You will get an acknowledgement within a few days.

## What tokopt touches

tokopt runs locally as your user, needs no network and no API key, and has no dependencies besides the Python standard library.

- Reads: agent logs and config (`~/.claude/projects`, `~/.claude/sessions`, `~/.claude/settings.json`, `~/.claude.json`, `~/.claude/agents/<name>.md` for `cheap_agents`, `~/.claude/skills/*/SKILL.md`, `~/.claude/plugins/installed_plugins.json`, each enabled plugin's `skills/`, `commands/`, `agents/`, `.mcp.json` and `.claude-plugin/plugin.json` (third-party content: regular files only, at most 1 MiB, the file itself must not be a symlink; a symlinked parent directory is followed, but only a few booleans and description lengths are read from it), `~/.codex`, `~/.hermes`, `~/.gemini`, `~/.config/tokopt/policy.toml`) and the process table (`/bin/ps`, every uid: a child process of any uid, such as a background `sudo`, counts as work; only sessions of your own uid are ever candidates to stop).
- Writes: `report` writes only tokopt's own state and policy (`scan-*.json` caches, `first_seen.json`, and `policy.toml` on first run, created 0600). `apply` and `restore` also edit the agent config files above (Claude, Codex and Hermes only; Gemini is read-only) (one validated edit at a time, atomic replace) and write the ledger, the lock, the `confirmed` marker, 0600 backups (full copies of the edited config, which may therefore contain tokens) and `stopped/*.md` handoff notes written before a session is stopped, all under `~/.local/state/tokopt/`.
- Signals: SIGTERM (never SIGKILL) to idle Claude Code sessions of your own uid, after re-checking identity and idleness.
- Output and the ledger never contain config values; secret-looking text is masked.

## The context nudge hook

`tokopt-nudge` (a Claude `UserPromptSubmit` hook) reads the last 2 MiB of the file named by `transcript_path` in the hook JSON on stdin (at most 1 MiB, in bytes, of stdin), opened `O_NOFOLLOW`, regular files only, never blocking on a FIFO or device. It always exits 0, prints at most one line, and writes nothing.

## Dependencies

tokopt itself has none. Development pins (`requirements-dev.txt`: pytest, pyright) are unhashed; install them in a throwaway virtualenv.

## Known limits (out of scope)

- Session files: a process running as your own user can write a fake `~/.claude/sessions/<pid>.json` (status `idle`, a matching start time) and make tokopt consider a session idle. Same-user code can already do far worse; this is out of scope. The only effect is that tokopt may SIGTERM that same-user Claude session after re-checking identity, and a handoff note with the resume command is written first.
- Installer trust: the launchd installer accepts a group-writable directory only when its group is exactly `admin`, you own it and you are in `admin` (the stock Homebrew layout). This means other local administrators are trusted (they can already `sudo`). `staff`, any other group, root- or other-owned group-writable directories and world-writable paths are refused. If you do not accept that, install with `pipx`, or into a virtualenv that is not group-writable and whose bin directory on PATH holds the `tokopt` console script; the installer then pins that script's resolved path in the plist and the job runs the console script. `TOKOPT_PYTHON` is read only when no `tokopt` is on PATH, and only for an interpreter that has tokopt in its own site-packages (the installer resolves symlinks, so a venv's `bin/python` would become the base interpreter, which does not have tokopt); that fallback runs `python -I`, which ignores per-user site-packages.

- The state directory itself may be a symlink (it is followed, and its target is set to mode 0700); entries inside it are never followed.
- Same-user tampering: a process running as you can edit the state directory (ledger, scan cache, `first_seen.json`, `confirmed`, backups), the policy file or the launchd plist and change what tokopt does next. Ledger rows are only honoured for the known agent config paths and backups inside the state dir, and tokopt's own state files (ledger, lock, `first_seen.json`, `confirmed`, scan caches, backups, handoff notes) are never read or written through a symlink: a link planted in the state directory, in `backups/`, `stopped/` or `backups/<stamp>/` makes `apply`/`restore` refuse before changing anything, and elsewhere it is replaced rather than followed. Agent config files and `policy.toml` deliberately DO follow symlinks (dotfile managers link them). A same-user attacker is not otherwise defended against.
- ACLs and extended attributes: the installer judges writability by POSIX mode bits, owner and group only. macOS ACL entries (`ls -le`) that grant write access to other accounts are not inspected. Check the directories above your `tokopt`/Python with `ls -led` if that matters on a shared machine.
- Race window: the installer validates the resolved program path and every symlink hop's directory, then writes that resolved path to the plist; it cannot stop an administrator who can write those directories later. Unattended `--scheduled` runs refuse to run if the state directory cannot be locked.

tokopt never sends anything anywhere, and it never edits agent definition files.

# tokopt

Every enabled MCP server, plugin and skill description is injected into every request your coding agent makes, so unused ones are pure token cost. `tokopt` finds the ones you have not used in a while, parks them (reversibly), and stops Claude Code sessions that have been idle for hours.

Supports Claude Code, Codex, Hermes and Gemini. Agents without readable usage logs (for example Gemini) are reported as "no evidence: skipped" and never parked. Honours `CLAUDE_CONFIG_DIR` and `CODEX_HOME`. An adapter turns itself off when that agent is not installed. Python >= 3.11, standard library only.

## Install

```
pipx install .            # gives you the `tokopt` command
python -m tokopt report   # or run from a checkout, no install
```

## Use

```
tokopt                     # report: dry run, edits nothing
tokopt apply               # first time: prints the plan, needs --yes (or answer y)
tokopt apply --yes
tokopt restore <ledger-id> | --all
```

Only `restore` takes a positional argument; `apply` and `report` reject stray ones. Example report row: `terraform@x   claude  plugin  15d ago  ~100  KEEP: used 15d ago`. `~` means chars/4 of skill descriptions; `n/a` means the size is not measurable (MCP tool schemas), and tokopt never invents a number.

## What is automated and what is advisory

| Thing | Automated by `apply` | Advisory only (`report`) |
|---|---|---|
| MCP servers: Claude, Codex, Hermes | parked after the idle threshold, reversibly | |
| Claude plugins | parked (set `false`) | plugins with hooks/servers: skipped, never parked |
| Claude standalone skills | | listed, park them yourself |
| Gemini | | no readable history: "no evidence: skipped" |
| Idle Claude Code sessions | SIGTERM, after the checks below | |
| Codex / Gemini sessions | never stopped | |
| `cheap_agents` (`model: haiku`) | | listed; agent files are never edited |
| Context nudge hook | | prints one `/clear` hint |

Idle-session stop applies only to Claude Code, only to interactive CLI sessions whose own session file says status `idle` for longer than `session_idle_hours`. Channel, print/stream-json and SDK instances are never stopped.

**Realistic hit rate.** A session is stopped only if every child process is recognised as idle. Recognised: your own configured MCP servers as they appear in `ps` (`npx` shows as `npm exec`, `uvx` as `uv tool uvx`, a script behind an interpreter, `docker run ...` with the same arguments) and `caffeinate`. Anything else counts as work: a Bash-tool shell, a language server (LSP), and any MCP server whose config is a wrapper such as `sh -c "..."` (the live process is the inner program, which cannot be matched). On a machine with six `npm exec`/`uvx`/`docker` servers, a session that also runs one `sh -c`-configured server or a language server is never stopped, so expect the feature to fire on sessions with a plain server set, not on every idle one. `report` shows the stop list, so you can see what would happen. A session left with a long-running shell command is intentionally never stopped.

## How it decides

1. Usage evidence comes from the agents' own logs: Claude `~/.claude/projects/**/*.jsonl` (including subagent transcripts), Codex `~/.codex/sessions`, Hermes `state.db`. Counted as use: MCP tool calls (Codex namespaced calls included), MCP resource tools and `/mcp__server__prompt` commands, `Skill` calls, subagent types, slash commands (`/plugin:cmd` or a plugin's bare `/cmd`).
2. An item is parked only if the agent's oldest log is older than the idle threshold (default 28 days) AND the item was not used within it.
3. Never-used items age from the first time tokopt saw them (`first_seen`), not from install time, so nothing parks on its first run.
4. No evidence means skipped, never parked: agents with no readable history (Gemini), plugins with hooks (a `hooks/` directory or `hooks`, `lspServers`, `outputStyles`, `monitors` in `plugin.json`: no usage signal), agents whose scan found zero tool calls, and agents whose log scan was incomplete. A scan is incomplete when a log is a symlink, a FIFO or unreadable, or has a line over 8 MiB that may hold a tool call (it contains `"tool_use"`, `"function_call"`, `"McpToolCall"` or `<command-name>` and its first 64 KiB does not prove otherwise). For an incomplete scan `report` prints a WARNING line naming the log(s) (home shown as `~`), and while one exists nothing is parked for that agent. A line over 8 MiB that provably holds no tool call (a Codex `compacted` record, a `response_item` that is not a `function_call`, or a line with no marker at all) is streamed in 1 MiB chunks and skipped silently: no warning, the scan stays complete).
5. `protect` in the policy is never parked or stopped. It is a case-insensitive substring: of the item name for servers and plugins, of the agent name or the session's working directory for sessions.
6. `cheap_agents` is a plain list of your own subagent names (`~/.claude/agents/<name>.md`). `report` only lists which of them could take `model: haiku`; tokopt never edits agent files.

Policy lives in `~/.config/tokopt/policy.toml` (written, documented, on first run). Unknown keys (typos such as `session_idle_hour`) are rejected with the list of valid keys. State, ledger and backups live in `~/.local/state/tokopt/`.

## What parking does

| Agent | Park | Restore |
|---|---|---|
| Claude | `~/.claude.json` `mcpServers` entry moves to `mcpServersParked`; plugin set `false` in `settings.json` | move back / set `true` |
| Codex | `enabled = false` in `[mcp_servers.X]` | sets an explicit `enabled = true` (it does not remove the line) |
| Hermes | entry moves to `mcp_servers_parked:` | move back (semantic inverse); byte-exact when the file is untouched |
| Gemini | never parks (no readable history) | n/a |

Hermes servers with `enabled: false` are not candidates. Where the text parser cannot be sure what YAML would say (a duplicate `enabled:` key, anchors/aliases, flow mappings, quoted keys) the server is not listed by `report` and is never parked.

Restoring a `~/.claude.json` server appends it to the end of `mcpServers`, so the key order of that object can differ from before parking (the content is identical). Standalone `~/.claude/skills/*` are listed report-only.

## Safety model

- Dry run by default. The first `apply` on a machine prints the plan and requires `--yes` or an interactive `y`. The confirmation is tied to the policy (a hash), to the agent config paths (`CLAUDE_CONFIG_DIR`, `CODEX_HOME`) and to the agent/item kinds in the plan: change the policy or the paths, or park a kind of item never confirmed before, and `apply` asks again.
- Unattended `--scheduled` runs never prompt, and a scheduled run never stops sessions or parks anything unless a confirmation covering the current policy, paths and plan exists; otherwise they exit 2 with nothing changed. `apply` and `restore` hold a lock in the state dir, so a scheduled run and a manual one cannot interleave.
- Every park writes a 0600 backup and a ledger row. Backups are full copies of the agent config files (so they contain any tokens in them), kept in 0700 directories under `~/.local/state/tokopt/backups/`. A backup is deleted when its entry is restored, and backups older than 30 days are pruned on each `apply` (restore then falls back to the semantic inverse edit, which does not need the backup). Output and ledger never contain config values.
- Files are replaced atomically. If a config path is a symlink (dotfiles managers), the link is kept and its target is edited. Every write (park, restore, byte-exact or semantic) aborts if the file changed while being edited (a running agent rewrites its own config). After each park tokopt re-reads the file after about 1s; if the running agent overwrote it, or put the server back next to the parked copy, the row is recorded as `reverted-by-host` and a warning printed (quit the agent, re-run).
- Every text edit is re-parsed (TOML, JSON) and compared with the original apart from the one value changed, else nothing is written. JSON configs are only rewritten if they are canonical 2-space JSON; Hermes YAML only if it is a simple block layout.
- `restore` is byte-exact if the file is untouched since apply (and the backup checksum matches); otherwise, including when the backup is gone, it applies the inverse edit (JSON, plugins, TOML and Hermes YAML), so unrelated changes made since are kept. It refuses rather than guess, never drops a parked copy when the server also exists live, and exits non-zero if any row was refused. A row the host already reverted (`reverted-by-host`, or found live again at the start of the next `apply`) reports `nothing to restore (host already reverted)`, changes nothing and keeps its backup until it is pruned. Ledger rows pointing outside the known agent config files are refused.
- Sessions (Claude Code sessions only; Codex and Gemini are never stopped, there is no per-process activity signal for them): tokopt stops sessions first, then parks. Claude sessions are judged by Claude's own per-process file `~/.claude/sessions/<pid>.json`: only an interactive CLI session whose status is `idle` (not busy/shell) for longer than `session_idle_hours`, and whose recorded start time (UTC) matches the live process, is a candidate. Channel, print/stream-json and SDK instances are never stopped. A candidate with any child process is active unless the child is one of the MCP servers in your own Claude config (user, project, enabled plugins), matched token for token with the launcher rewrites undone (`npx` as `npm exec`, `uvx` as `uv tool uvx`, an interpreter before a script), or `caffeinate`; a path that merely contains "mcp" counts as work. Only sessions of your own uid are candidates, but children of any uid (a background `sudo`) count as active. A `${VAR}` in a configured server (for example `${A}/server.js`) stands for any non-empty, whitespace-free text, so it matches any `/server.js`, but the whole command line must still match token for token. Immediately before each SIGTERM (never SIGKILL) the process identity (command + start time) is re-checked after the activity check. A handoff note with the resume command is written first, and removed if the signal fails.

## Scheduling (macOS)

`pipx install .`, then `scripts/install-tokopt-launchd.sh` installs a daily 03:30 `tokopt apply --scheduled` job. launchd gets the program and arguments as separate plist strings (no shell); the installer uses the `tokopt` command on PATH, or an absolute Python >= 3.11 on which `-m tokopt --version` works. It requires an absolute path whose resolved file and every parent directory are owned by you (or root), are not world-writable and are not in Dropbox/iCloud/CloudStorage; the same checks apply to the interpreter in a script's `#!` line (`#!/usr/bin/env` is refused). A group-writable path is refused, except a directory you own whose group is exactly `admin` and that you are a member of (the stock Homebrew layout, 775 you:admin): other local administrators are then trusted, as they can already `sudo`. `staff` is never trusted. If you do not accept that, install with `pipx`, or into a virtualenv that is not group-writable and whose bin directory on PATH holds the `tokopt` console script; the installer then pins that script's resolved path in the plist and the job runs the console script. `TOKOPT_PYTHON` is read only when no `tokopt` is on PATH, and only for an interpreter that has tokopt in its own site-packages (the installer resolves symlinks, so a venv's `bin/python` would become the base interpreter, which does not have tokopt); that fallback runs `python -I`, which ignores per-user site-packages. If `CLAUDE_CONFIG_DIR` or `CODEX_HOME` is set when you install, the plist carries them (and a scheduled run whose paths differ from the confirmed ones refuses). `--uninstall` removes it. Run `tokopt apply` by hand once first; re-confirm after policy changes.

After upgrading Python (`brew upgrade`) or reinstalling pipx, re-run the installer: the job pins the resolved program path, which a Cellar or pipx-venv change removes, and the 03:30 run would then silently never happen. `scripts/install-tokopt-launchd.sh --status` and `tokopt report` both warn when the pinned program has vanished.

Log rotation: the job appends to `~/.local/state/tokopt/logs/tokopt.out.log` and `tokopt.err.log` (directory 0700) and nothing rotates them. They stay small (one short report a day); truncate or delete them whenever you like, e.g. `: > ~/.local/state/tokopt/logs/tokopt.out.log`.

## Context nudge (optional)

`tokopt-nudge` (always exits 0) is a Claude `UserPromptSubmit` hook that prints one `/clear` hint when the context exceeds 150k tokens. Add it under `hooks.UserPromptSubmit` in `~/.claude/settings.json`.

## Uninstall

`tokopt restore --all`, then `scripts/install-tokopt-launchd.sh --uninstall` if you scheduled it, then `pipx uninstall tokopt`. Optionally delete `~/.config/tokopt` and `~/.local/state/tokopt`.

## FAQ

**It parked something I need.** `tokopt restore <id>` (ids are in the apply output and `~/.local/state/tokopt/ledger.jsonl`); add the name to `protect`.

**I upgraded and my policy file has the old defaults.** `policy.toml` files written by older versions keep their old values (including old `protect` lists and `cheap_agents`); tokopt only writes defaults when the file does not exist. Review it, and note that invalid values and unknown keys are now rejected (idle days >= 7, hours >= 1).

**Nothing parks on the first run.** By design: never-used items age 28 days from first sight, and agents with under 28 days of logs are skipped.

**My agent put the server back.** Agents rewrite their config while running. Quit the agent, then `tokopt apply`.

**Does it need network or an API key?** No.

## Development and contributing

`pip install -r requirements-dev.txt` then `python -m pytest -q` (tests use a fake `HOME` and never touch the real one) and `pyright tokopt`. Contributing: open an issue first for anything larger than a fix; write the test first and watch it fail for the right reason; keep the tool stdlib-only; never commit personal paths, names or tokens (a test scans every tracked file). Security problems: see `SECURITY.md`, do not open a public issue.

## License

MIT

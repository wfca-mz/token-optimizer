# Changelog

## 0.1.0 (unreleased)

First shareable release. Changes from the pre-review import:

- Third-party plugin manifests (`plugin.json`, `.mcp.json`) are read as regular files, at most 1 MiB, the file itself must not be a symlink: a manifest linked to `/dev/zero`, a FIFO, a directory or a huge file makes the plugin `SKIP: unreadable plugin manifest` (never parked) instead of exhausting memory; symlinked `skills/`, `commands/`, `agents/` directories are not walked.
- Hardening: `scheduled_job_warning` cannot crash on any plist content (NUL path, FIFO, device link, directory, nested); common zero-width, format, bidi and tag characters are masked; plugin `SKILL.md` and session-file reads are regular-file only and capped at 1 MiB; an unusable state directory exits 2 cleanly and an unwritable `first_seen.json` no longer breaks `report`; dev pin pytest 9.0.3.
- Ledger rows start on a fresh line (a half-written row cannot hide the next one); `apply` stops editing configs once the ledger is not writable; the state-dir preflight runs after the lock and covers the state root, `backups/`, `stopped/` and `backups/<stamp>/` (a link there is refused up front; restore also names a planted backup link); a process row without a uid is not yours; `--check-job` stands alone.
- Fail closed before mutating: a symlink planted in the state dir (ledger, backups, `confirmed`, ...) makes `apply`/`restore` refuse up front; state files are read and written without following symlinks; a ledger write failure after a park or a SIGTERM is reported (backup named) instead of a false SKIPPED or a crash. `report` survives any plist at the scheduled-job path; names cannot forge output lines (newlines, tabs, C1 and bidi controls become `?`); the last pre-SIGTERM check also requires one of your own `claude` processes; Hermes entries the parser is unsure about are never candidates.
- Oversized lines whose first 64 KiB prove a record without tool calls (Codex `compacted`, non-`function_call` `response_item`) are skipped without scanning; the call markers are quoted (`"function_call"`, `"McpToolCall"`, `"tool_use"`, `<command-name>`) so `function_call_output` is not a call, and escaped text such as a quoted `mcp__` name inside a large tool result is not evidence. Hermes `enabled: false` servers are not candidates. A child appearing after selection is re-checked right before SIGTERM. Config `${VAR}` tokens match as anchored regexes; only the system `caffeinate -i -t N` counts idle. Installer: symlink chains are validated hop by hop, pip `exec` wrappers are followed.
- Oversized transcript lines (> 8 MiB) are streamed in bounded chunks: skipped silently when they hold no evidence marker, otherwise the scan is incomplete and the log is named in `report`.
- Evidence scan covers subagent transcripts (`<project>/<session>/subagents/*.jsonl`); plugin slash commands count as use.
- `first_seen` starts at the first observation by tokopt, never at plugin install time.
- Session stop re-verifies process identity and idleness immediately before SIGTERM; kill failures are reported, not fatal.
- Restore is a semantic inverse edit when the file changed since apply; byte-exact only when untouched. Parks that a running agent overwrites are recorded as `reverted-by-host`.
- Idle-child detection defaults to "active"; only known idle MCP server command lines are ignored. `codex app-server` / `mcp-server` are never stopped.
- First `apply` on a machine requires `--yes` (or an interactive y); `--scheduled` never prompts.
- Secret-bearing files are created 0600 / dirs 0700 with no wider window.
- Claude `procStart` (UTC) is compared with `ps` start time as epochs, so real sessions can now be recognised; `/bin/ps`, all uids are read (a child of any uid counts as work); only your own uid's claude sessions are candidates.
- Restore falls back to the semantic inverse when the backup was pruned or corrupt; never drops a parked copy; all writes share one race check; apply/restore hold a lock.
- Only Claude Code sessions are stopped; an idle child must match one of the agent's own configured MCP commands exactly.
- Hostile logs: bounded line reads, regular files only (no symlinks/FIFOs), iterative walk, per-adapter failure isolation, tolerant config shapes.
- Plugins declaring hooks/lspServers/outputStyles/monitors in `plugin.json` are never parked; MCP prompts, resource tools and Codex namespaced calls count as use.
- Policy rejects unknown keys; the confirmation is bound to the agent config paths; the installer validates the executable and its parents and forwards `CLAUDE_CONFIG_DIR`/`CODEX_HOME`.
- No vendor-specific adapters; `cheap_agents` is a user-supplied list reported on only. Added SECURITY.md, requirements-dev.txt, pip dependabot, Python 3.13 in CI.
- Packaged as `tokopt` (stdlib only, Python >= 3.11); no personal defaults.

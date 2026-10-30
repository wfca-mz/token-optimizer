"""tokopt: park idle MCP servers / plugins and stop idle agent sessions.

    tokopt [report]          dry-run table (default; never edits agent configs)
    tokopt apply [--yes]     stop idle sessions, then park, with backups and a ledger
    tokopt restore <id|--all>

Every enabled MCP/plugin description rides in every request, so unused ones
are pure token cost. Parking is reversible: backup file + ledger row, and
`restore` undoes exactly the parked entry (byte-exact when the file is
untouched since, a semantic inverse edit otherwise). Usage evidence comes from
agent logs; no evidence means "skipped", never a guess. Stdlib only.
"""
from __future__ import annotations

import argparse
import calendar
import copy
import errno
import fcntl
import hashlib
import json
import os
import plistlib
import re
import shlex
import signal
import sqlite3
import stat
import subprocess
import sys
import time
import tomllib
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

from tokopt import __version__

DEFAULT_POLICY = """\
# tokopt policy (created on first run; edit freely).

# Park an MCP server this many days after its last observed tool call (>= 7).
mcp_idle_days = 28
# Same, for plugins and skills (>= 7).
skill_idle_days = 28
# Stop an agent session (SIGTERM) idle this many hours (>= 1): the agent reports
# it idle, no running child process.
session_idle_hours = 8

# Never parked or stopped. Case-insensitive substring of the item name; for sessions it is
# matched against the agent name and the session's working directory.
# Example (uncomment and edit):
# protect = ["my-memory-server", "my-plugin"]
protect = []

# Names of your own Claude subagents (~/.claude/agents/<name>.md) that do simple work.
# `report` lists which of them could take `model: haiku`; tokopt never edits agent files.
# Example: cheap_agents = ["summarizer", "docs-writer"]
cheap_agents = []
"""
DEFAULTS = {"mcp_idle_days": 28, "skill_idle_days": 28, "session_idle_hours": 8, "protect": [], "cheap_agents": []}
DAY = 86400
VERIFY_DELAY = 1.0     # seconds to wait before checking a running agent did not undo a park
SCAN_VERSION = 7       # bump when evidence extraction changes: stale caches are ignored
MAX_LINE = 8 * 1024 * 1024   # transcript lines longer than this are never parsed (see _lines)
HEAD = 64 * 1024             # bytes of an oversized line inspected for its record type
CHUNK = 1024 * 1024          # read size when streaming an oversized line
MASK_CAP = 2_000_000
BACKUP_DAYS = 30
RUN_TIMEOUT = 15
PS = "/bin/ps"           # absolute: a scheduled job must not resolve ps through PATH
CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u2028\u2029\u200e\u200f\u202a-\u202e\u2066-\u2069\u061c\u200b-\u200d\u2060-\u2064\ufeff\U000e0000-\U000e007f\u00ad\u180e\u206a-\u206f\ufff9-\ufffb\u0600-\u0605\u06dd\u070f]")  # terminal controls, bidi, zero-width/format and tag characters; tab and newline stay (see _oneline)
BEARER_RE = re.compile(r"(?i)\b(bearer\s{1,8})[\w.~+/=-]+")
SECRET_RE = re.compile(r"(?i)((?:token|key|secret|password|authorization)[\w-]{0,64}\s{0,8}[:=]\s{0,8})\S+")
# Claude Code built-ins: a plugin command with the same bare name is not evidence the plugin was used.
BUILTIN_SLASH = frozenset("""add-dir agents bug clear compact config context cost doctor exit export fast feedback goal
help hooks ide init install-github-app login logout loop mcp memory model output-style permissions plugin
plugins pr-comments privacy-settings quit rate-limit-options release-notes reload-plugins remote-control
resume review rewind schedule security-review simplify skills status statusline tasks terminal-setup theme
todos upgrade usage usage-credits vim""".split())


def home() -> Path:
    return Path(os.environ["HOME"])


def state_dir() -> Path:
    return home() / ".local/state/tokopt"


def claude_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or home() / ".claude")


def claude_json() -> Path:
    return claude_dir() / ".claude.json" if os.environ.get("CLAUDE_CONFIG_DIR") else home() / ".claude.json"


def codex_dir() -> Path:
    return Path(os.environ.get("CODEX_HOME") or home() / ".codex")


def mask(s: str) -> str:
    if len(s) > MASK_CAP:
        s = s[:MASK_CAP] + "...[truncated]"
    return CTRL_RE.sub("?", SECRET_RE.sub(r"\1***", BEARER_RE.sub(r"\1***", s)))


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


# Claude keeps `-` in tool names; Codex maps every non [A-Za-z0-9_] character to `_`.
_NORM = {"claude": re.compile(r"[^A-Za-z0-9_-]"), "codex": re.compile(r"[^A-Za-z0-9_]")}


def norm(name: str, agent: str) -> str:
    return _NORM[agent].sub("_", name)


def _run(cmd: list[str], env: dict[str, str] | None = None) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=RUN_TIMEOUT, env=env).stdout
    except (OSError, subprocess.TimeoutExpired):  # tool missing or hung: no signal, not an error
        return ""


def _read_json(p: Path, default: Any = None) -> Any:
    """Read a JSON file, FOLLOWING symlinks on purpose: this is for the agents' own config files, which dotfile
    managers link. tokopt's own state goes through _read_state_json."""
    try:
        return json.loads(Path(p).read_text())
    except (OSError, ValueError):
        return default


def _state_bytes(p: Path) -> bytes:
    with _open_regular(Path(p)) as f:
        return f.read()


SMALL_FILE = 1 << 20


def _read_small(p: Path) -> bytes:
    """At most SMALL_FILE bytes of a regular file (no symlink, no FIFO/device); a bigger file is refused."""
    with _open_regular(Path(p)) as f:
        data = f.read(SMALL_FILE + 1)
    if len(data) > SMALL_FILE:
        raise OSError(errno.EFBIG, "file too large")
    return data


def _read_state_json(p: Path, default: Any = None) -> Any:
    """Read one of tokopt's OWN state files: never through a symlink, regular files only."""
    try:
        return json.loads(_state_bytes(p))
    except (OSError, ValueError):
        return default


def atomic_write(path: Path, data: bytes, mode: int = 0o600, follow: bool = True) -> None:
    """Write via an exclusively-created temp file never wider than `mode`, then rename.
    follow=True (agent config files, which dotfile managers symlink): a symlink at `path` is followed, the link is
    preserved and its target replaced. follow=False (tokopt's own state): a symlink at `path` is REPLACED, never
    written through."""
    path = Path(os.path.realpath(path)) if follow else Path(os.path.abspath(path))
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def private_dir(p: Path) -> None:
    """Create p (and parents) and force 0700 on p and every ancestor inside the state dir."""
    for d in reversed([p, *p.parents]):
        os.makedirs(d, mode=0o700, exist_ok=True)
    root = state_dir()
    for d in (p, *p.parents):
        if d == root or root in d.parents:
            os.chmod(d, 0o700)


def validate_policy(p: dict) -> dict:
    for k, floor in (("mcp_idle_days", 7), ("skill_idle_days", 7), ("session_idle_hours", 1)):
        v = p[k]
        if isinstance(v, bool) or not isinstance(v, int) or v < floor:
            raise ValueError(f"policy: {k} must be an integer >= {floor}, got {v!r}")
    for k in ("protect", "cheap_agents"):
        if not (isinstance(p[k], list) and all(isinstance(x, str) for x in p[k])):
            raise ValueError(f"policy: {k} must be a list of strings")
    return p


def policy_hash(p: dict) -> str:
    return sha(json.dumps({k: p[k] for k in sorted(DEFAULTS)}, sort_keys=True).encode())


def load_policy() -> dict:
    p = home() / ".config/tokopt/policy.toml"
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(p, DEFAULT_POLICY.encode(), 0o600)
    raw = tomllib.loads(p.read_text())
    unknown = sorted(set(raw) - set(DEFAULTS))
    if unknown:
        raise ValueError(f"policy: unknown key(s) {unknown}; valid keys: {sorted(DEFAULTS)}")
    return validate_policy({**DEFAULTS, **raw})


# ---------- text-level edits: (text, name) -> new text; ValueError if unsafe, NoChange if already done ----------

class NoChange(ValueError):
    """The edit would not alter the file (item already in the target state)."""


class BothPresent(ValueError):
    """The server is live AND still in the parked block (a host put it back next to the parked copy)."""


def _canonical_json(text: str) -> dict:
    data = json.loads(text)
    if json.dumps(data, indent=2, ensure_ascii=False) != text.rstrip("\n"):
        raise ValueError("file is not canonical 2-space JSON; refusing to re-serialise")
    return data


def _dump_json(data: dict, text: str) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + ("\n" if text.endswith("\n") else "")


def json_park(text: str, name: str) -> str:
    data = _canonical_json(text)
    if name in data.get("mcpServersParked", {}):
        raise ValueError(f"{name} already in mcpServersParked; refusing to overwrite it")
    entry = data["mcpServers"].pop(name)
    data.setdefault("mcpServersParked", {})[name] = entry
    return _dump_json(data, text)


def json_unpark(text: str, name: str) -> str:
    data = _canonical_json(text)
    parked = data.get("mcpServersParked", {})
    if name not in parked:
        if name in data.get("mcpServers", {}):
            raise NoChange(f"{name} is already active")
        raise ValueError(f"{name} is in neither mcpServers nor mcpServersParked")
    live = data.setdefault("mcpServers", {})
    if name in live:
        raise BothPresent(f"{name} is in both mcpServers and mcpServersParked; refusing to drop the parked copy")
    live[name] = parked.pop(name)
    if not parked:
        del data["mcpServersParked"]
    return _dump_json(data, text)


def _set_plugin(text: str, name: str, enabled: bool) -> str:
    old = json.loads(text)  # an unparseable file is never edited
    rx = re.compile(r'("%s"\s*:\s*)(true|false)' % re.escape(name))
    if len(rx.findall(text)) != 1:
        raise ValueError(f"enabledPlugins entry {name} not found exactly once")
    new = rx.sub(lambda m: m.group(1) + ("true" if enabled else "false"), text)
    if new == text:
        raise NoChange(f"{name} is already {'enabled' if enabled else 'disabled'}")
    want = copy.deepcopy(old)
    try:
        want["enabledPlugins"][name] = enabled
    except (KeyError, TypeError):
        raise ValueError(f"enabledPlugins[{name}] is not a top-level entry") from None
    if json.loads(new) != want:
        raise ValueError(f"edit of {name} would change more than enabledPlugins[{name}]; refusing")
    return new


def plugin_park(text: str, name: str) -> str:
    return _set_plugin(text, name, False)


def plugin_unpark(text: str, name: str) -> str:
    return _set_plugin(text, name, True)


def _set_toml_enabled(text: str, name: str, enabled: bool) -> str:
    old = tomllib.loads(text)  # an unparseable file is never edited
    lines = text.split("\n")
    hdr = re.compile(r'^\[mcp_servers\.("?)%s\1\]\s*(#.*)?$' % re.escape(name))
    want = f"enabled = {'true' if enabled else 'false'}"
    for i, ln in enumerate(lines):
        if not hdr.match(ln):
            continue
        j = i + 1
        while j < len(lines) and not lines[j].lstrip().startswith("["):
            if re.match(r"\s*enabled\s*=", lines[j]):
                if lines[j].strip() == want:
                    raise NoChange(f"{name} already has {want}")
                lines[j] = want
                break
            j += 1
        else:
            if enabled:
                raise NoChange(f"{name} has no enabled = false to undo")
            lines.insert(i + 1, want)
        new = "\n".join(lines)
        expect = copy.deepcopy(old)
        try:
            expect["mcp_servers"][name]["enabled"] = enabled
        except (KeyError, TypeError):
            raise ValueError(f"[mcp_servers.{name}] not found") from None
        if tomllib.loads(new) != expect:  # also raises if the edit produced invalid TOML
            raise ValueError(f"edit of [mcp_servers.{name}] would change more than `enabled`; refusing")
        return new
    raise ValueError(f"[mcp_servers.{name}] not found")


def toml_park(text: str, name: str) -> str:
    return _set_toml_enabled(text, name, False)


def toml_unpark(text: str, name: str) -> str:
    return _set_toml_enabled(text, name, True)


def _yaml_top(lines: list[str], key: str) -> tuple[int, int] | None:
    """(header index, end index) of a top-level `key:` block, which must be a simple block layout."""
    top = next((i for i, l in enumerate(lines) if re.match(r"%s:\s*(?:\{\}\s*)?$" % key, l)), None)
    if top is None:
        return None
    end = next((i for i in range(top + 1, len(lines)) if lines[i][:1] not in ("", " ", "#")), len(lines))
    for l in lines[top + 1:end]:
        s = l.strip()
        if "\t" in l or (s and not s.startswith("#") and re.search(r"(^|\s)(&|\*|<<)\S*", l)):
            raise ValueError(f"{key}: not a simple block layout (tabs/anchors/aliases); refusing to edit")
        if re.match(r"  \S", l) and not s.startswith("#") and not re.match(r"  [\w.-]+:\s*$", l):
            raise ValueError(f"{key}: not a simple block layout (inline entry {s[:30]!r}); refusing to edit")
    return top, end


def _yaml_chunk(lines: list[str], top: int, end: int, name: str) -> tuple[int, int] | None:
    start = next((i for i in range(top + 1, end) if re.match(r"  %s:\s*$" % re.escape(name), lines[i])), None)
    if start is None:
        return None
    stop = next((i for i in range(start + 1, end) if re.match(r"  \S", lines[i])), end)
    while stop > start + 1 and lines[stop - 1].strip() == "":
        stop -= 1
    return start, stop


def _has_entries(lines: list[str]) -> bool:
    return any(re.match(r"  \S", l) and not l.lstrip().startswith("#") for l in lines)


def yaml_park(text: str, name: str) -> str:
    """Move the `  name:` entry from `mcp_servers:` to `mcp_servers_parked:`, text-level."""
    lines = text.split("\n")
    blk = _yaml_top(lines, "mcp_servers")
    if blk is None:
        raise ValueError("mcp_servers: block not found")
    top, end = blk
    span = _yaml_chunk(lines, top, end, name)
    if span is None:
        raise ValueError(f"mcp_servers entry {name} not found")
    start, stop = span
    chunk = lines[start:stop]
    del lines[start:stop]
    if not _has_entries(lines[top + 1:end - len(chunk)]):
        lines[top] = "mcp_servers: {}"
    parked = _yaml_top(lines, "mcp_servers_parked")
    if parked is None:
        while lines and lines[-1] == "":
            lines.pop()
        lines += ["", "mcp_servers_parked:"] + chunk + [""]
    else:
        ptop, pend = parked
        if _yaml_chunk(lines, ptop, pend, name):
            raise ValueError(f"{name} already in mcp_servers_parked; refusing to overwrite it")
        while pend > ptop + 1 and lines[pend - 1].strip() == "":
            pend -= 1
        lines[pend:pend] = chunk
    return "\n".join(lines)


def yaml_unpark(text: str, name: str) -> str:
    lines = text.split("\n")
    parked = _yaml_top(lines, "mcp_servers_parked")
    span = _yaml_chunk(lines, *parked, name) if parked else None
    live = _yaml_top(lines, "mcp_servers")
    if parked is None or span is None:
        if live and _yaml_chunk(lines, *live, name):
            raise NoChange(f"{name} is already active")
        raise ValueError(f"{name} not found in mcp_servers_parked")
    if live and _yaml_chunk(lines, *live, name):
        raise BothPresent(f"{name} is in both mcp_servers and mcp_servers_parked; refusing to drop the parked copy")
    ptop, pend = parked
    start, stop = span
    chunk = lines[start:stop]
    del lines[start:stop]
    if not _has_entries(lines[ptop + 1:pend - len(chunk)]):
        del lines[ptop]
        if ptop > 0 and lines[ptop - 1].strip() == "":
            del lines[ptop - 1]
    live = _yaml_top(lines, "mcp_servers")
    if live is None:
        raise ValueError("mcp_servers: block not found; cannot restore into it")
    top, end = live
    if re.match(r"mcp_servers:\s*\{\}\s*$", lines[top]):
        lines[top] = "mcp_servers:"
    while end > top + 1 and lines[end - 1].strip() == "":
        end -= 1
    lines[end:end] = chunk
    return "\n".join(lines)


# kind -> (park, unpark)
EDITS: dict[str, tuple[Callable[[str, str], str], Callable[[str, str], str]]] = {
    "json": (json_park, json_unpark), "plugin": (plugin_park, plugin_unpark),
    "toml": (toml_park, toml_unpark), "yaml": (yaml_park, yaml_unpark)}


def edit_kind(fn: Callable | None) -> str | None:
    return next((k for k, (p, _) in EDITS.items() if p is fn), None)


# ---------- evidence ----------

@dataclass
class Item:
    agent: str
    kind: str                       # mcp | plugin | skill
    name: str
    path: Path | None = None        # file to edit; None = report-only
    edit: Callable[[str, str], str] | None = None
    last_used: float | None = None
    tokens: int | None = None
    note: str = ""                  # forces a non-park action (e.g. "no evidence: skipped")


@dataclass
class Scan:
    agent: str
    oldest: float | None            # oldest usable log mtime; None = no evidence source
    items: list[Item]
    blind: bool = False             # history exists but the scan found no tool calls at all
    unscanned: list[str] = field(default_factory=list)  # logs that could not be fully scanned: absence of evidence proves nothing

    @property
    def incomplete(self) -> bool:
        return bool(self.unscanned)


def _ts_of(obj: Any, fallback: float) -> float:
    try:
        return datetime.fromisoformat(obj["timestamp"].replace("Z", "+00:00")).timestamp()
    except (ValueError, KeyError, TypeError, AttributeError):
        return fallback


def _walk(o: Any) -> Iterator[dict]:
    """Every dict inside o. Iterative: hostile nesting must not hit the recursion limit."""
    todo = [o]
    while todo:
        x = todo.pop()
        if isinstance(x, dict):
            yield x
            todo.extend(x.values())
        elif isinstance(x, list):
            todo.extend(x)


_CMD_RE = re.compile(rb"<command-name>/?([^<]+)</command-name>")


def claude_keys(obj: Any, raw: bytes) -> list[str]:
    """Evidence keys of one transcript record. Only real tool_use blocks count, never inventory text."""
    keys = []
    for d in _walk(obj):
        n, inp = d.get("name"), d.get("input")
        if d.get("type") != "tool_use" or not isinstance(n, str):
            continue
        if n.startswith("mcp__"):
            keys.append("mcp:" + norm(n[5:].split("__", 1)[0], "claude"))
        elif isinstance(inp, dict) and n in ("ReadMcpResourceTool", "ListMcpResourcesTool"):
            if isinstance(inp.get("server"), str):
                keys.append("mcp:" + norm(inp["server"], "claude"))
        elif isinstance(inp, dict) and n == "Skill" and isinstance(inp.get("skill"), str):
            keys.append("use:" + inp["skill"])
        elif isinstance(inp, dict) and n in ("Task", "Agent") and isinstance(inp.get("subagent_type"), str):
            keys.append("use:" + inp["subagent_type"])
    for m in _CMD_RE.finditer(raw):
        cmd = m.group(1).decode(errors="replace")
        # an MCP prompt (/mcp__server__prompt) is use of that server
        keys.append("mcp:" + norm(cmd[5:].split("__", 1)[0], "claude") if cmd.startswith("mcp__") else "use:" + cmd)
    return keys


def codex_keys(obj: Any, raw: bytes) -> list[str]:
    keys = []
    for d in _walk(obj):
        t, n = d.get("type"), d.get("name")
        if t == "McpToolCall" and isinstance(d.get("server"), str):
            keys.append("mcp:" + norm(d["server"], "codex"))
        elif t == "function_call" and isinstance(n, str):
            # real rollouts: {"namespace": "mcp__<server>", "name": "<tool>"} (no trailing `__`); older ones
            # carry `mcp__<server>__` or a flat `mcp__<server>__<tool>` name. A non-mcp namespace (clock,
            # collaboration) says nothing, so fall back to the flat name.
            ns = d.get("namespace")
            full = ns if isinstance(ns, str) and ns.startswith("mcp__") else n
            srv = full[5:].removesuffix("__").split("__", 1)[0] if full.startswith("mcp__") else ""
            if srv:
                keys.append("mcp:" + norm(srv, "codex"))
    return keys


CLAUDE_SCAN = (claude_keys, (b'"tool_use"', b"<command-name>"))
CODEX_SCAN = (codex_keys, (b'"McpToolCall"', b'"function_call"'))  # quoted: `function_call_output` is not a call


def _open_regular(f: Path):
    """Open a log for reading without following a symlink or blocking on a FIFO/device; regular files only."""
    fd = os.open(f, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, "not a regular file")
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


_TOP_TYPE = re.compile(rb'"type"\s*:\s*"([^"\\]*)"')
_PAYLOAD_TYPE = re.compile(rb'"payload"\s*:\s*\{\s*"type"\s*:\s*"([^"\\]*)"')


def _typed_skip(head: bytes) -> bool:
    """True when the first bytes of a line PROVE it carries no tool call: a depth-1 `"type":"compacted"` record,
    or a `response_item` whose payload type is not `function_call` (messages, outputs, reasoning)."""
    m = _TOP_TYPE.search(head)
    if not m:
        return False
    pre = head[:m.start()]
    if pre.count(b"{") != 1 or b"[" in pre or b"}" in pre:  # the type key must sit directly in the top-level object
        return False
    if m.group(1) == b"compacted":
        return True
    p = _PAYLOAD_TYPE.search(head)
    return m.group(1) == b"response_item" and p is not None and p.group(1) != b"function_call"


def _lines(fh, over: list[bool], needles: tuple[bytes, ...] = ()) -> Iterator[bytes]:
    """Lines of at most MAX_LINE bytes. A longer line is streamed in CHUNK-sized reads (overlapping by the longest
    needle minus one byte) and never held whole: it is skipped silently when no evidence needle occurs anywhere in
    it, and noted in `over` (fail closed) when one does."""
    cut = max((len(n) for n in needles), default=1) - 1
    n = max(1, min(CHUNK, MAX_LINE))
    while True:
        chunk = fh.readline(n)
        if not chunk:
            return
        if chunk.endswith(b"\n"):
            yield chunk
            continue
        parts, size = [chunk], len(chunk)
        tail = chunk[len(chunk) - cut:] if cut else b""
        safe = _typed_skip(chunk[:HEAD])
        hit = not safe and any(nd in chunk for nd in needles)
        while not chunk.endswith(b"\n"):
            chunk = fh.readline(n)
            if not chunk:
                break
            size += len(chunk)
            window = tail + chunk
            hit = hit or (not safe and any(nd in window for nd in needles))
            tail = window[len(window) - cut:] if cut else b""
            if size <= MAX_LINE:
                parts.append(chunk)
            else:
                parts.clear()  # oversized: only the needle verdict is kept
        if size <= MAX_LINE:
            yield b"".join(parts)
        elif hit:
            over.append(True)


def scan_logs(agent: str, files: list[Path], spec) -> tuple[dict[str, float], float | None, list[str]]:
    """key -> last-used epoch over all log files; cached per (path,mtime,size).
    Third value: the logs that could not be fully scanned (symlink, FIFO, unreadable, a line over MAX_LINE):
    while any exists the evidence is incomplete."""
    extract, needles = spec
    cache_p = state_dir() / f"scan-{agent}.json"
    raw = _read_state_json(cache_p, {})
    cache = raw.get("files", {}) if isinstance(raw, dict) and raw.get("v") == SCAN_VERSION else {}
    out: dict[str, float] = {}
    fresh, oldest, bad = {}, None, []
    for f in files:
        try:
            st = f.lstat()
            if not stat.S_ISREG(st.st_mode):
                bad.append(str(f))
                continue
            sig = [st.st_mtime_ns, st.st_size]
            ent = cache.get(str(f))
            if not isinstance(ent, list) or len(ent) != 3 or ent[0] != sig:
                found: dict[str, float] = {}
                over: list[bool] = []
                with _open_regular(f) as fh:
                    for line in _lines(fh, over, needles):
                        if not any(n in line for n in needles):
                            continue
                        try:
                            obj = json.loads(line)
                        except (ValueError, RecursionError):  # malformed or absurdly nested: skip the line
                            continue
                        keys = extract(obj, line)
                        if keys:
                            ts = _ts_of(obj, st.st_mtime)
                            for k in keys:
                                found[k] = max(found.get(k, 0), ts)
                ent = [sig, found, bool(over)]
        except FileNotFoundError:  # vanished mid-scan: normal churn
            continue
        except OSError:  # symlink (ELOOP), special file, permissions: we could not read it
            bad.append(str(f))
            continue
        if ent[2]:  # a line over MAX_LINE was skipped: a tool call may have been on it
            bad.append(str(f))
        oldest = st.st_mtime if oldest is None else min(oldest, st.st_mtime)
        fresh[str(f)] = ent
        for k, v in ent[1].items():
            out[k] = max(out.get(k, 0), v)
    try:
        private_dir(state_dir())
        atomic_write(cache_p, json.dumps({"v": SCAN_VERSION, "files": fresh}).encode(), follow=False)
    except OSError:
        pass
    return out, oldest, bad


def _dict(x: Any) -> dict:
    """Config files are untrusted shapes: anything that is not a dict reads as empty."""
    return x if isinstance(x, dict) else {}


def _cfg_servers(path: Path, key: str) -> list[str]:
    return list(_dict(_dict(_read_json(path, {})).get(key)))


def _enabled_plugin_dirs(base: Path) -> list[tuple[str, Path]]:
    """(plugin key, install dir) of every enabled plugin, tolerating malformed settings/installed_plugins shapes."""
    enabled = [k for k, v in _dict(_dict(_read_json(base / "settings.json", {})).get("enabledPlugins")).items() if v is True]
    inst = _dict(_dict(_read_json(base / "plugins/installed_plugins.json", {})).get("plugins"))
    out = []
    for key in enabled:
        recs = inst.get(key)
        rec = recs[0] if isinstance(recs, list) and recs and isinstance(recs[0], dict) else {}
        ip = rec.get("installPath")
        out.append((key, Path(ip if isinstance(ip, str) and ip else "/nonexistent")))
    return out


def _plugin_names(d: Path) -> set[str]:
    """Names a user can invoke for a plugin without its `<plugin>:` prefix."""
    names = {f.stem for sub in ("commands", "agents") if _plain_dir(d / sub) for f in (d / sub).rglob("*.md")}
    return names | {x.name for x in (d / "skills").glob("*") if x.is_dir()} if _plain_dir(d / "skills") else names


# plugin.json keys that make a plugin do work without any skill/command call we could observe
INLINE_NO_SIGNAL = ("hooks", "lspServers", "outputStyles", "monitors")


def _read_plugin_json(p: Path) -> dict | None:
    """Third-party plugin content: at most 1 MiB, a regular file, never through a symlink (a manifest linked to
    /dev/zero must not eat the machine). Missing -> {} (nothing declared); unreadable, invalid or oversized -> None
    (unknown, which is never treated as 'empty'); a JSON value that is not an object -> {}."""
    try:
        data = json.loads(_read_small(p))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, RecursionError):
        return None
    return data if isinstance(data, dict) else {}


def _plain_dir(p: Path) -> bool:
    return p.is_dir() and not p.is_symlink()


def _skill_text(sk: Path) -> str:
    try:
        return _read_small(sk).decode(errors="replace")
    except OSError:  # symlink, device, too big, unreadable: no description measured
        return ""


def scan_claude() -> Scan | None:
    base = claude_dir()
    if not base.is_dir():
        return None
    root = base / "projects"
    files = sorted(root.rglob("*.jsonl")) if root.is_dir() else []  # includes <session>/subagents/*.jsonl
    ev, oldest, bad = scan_logs("claude", files, CLAUDE_SCAN)
    items: list[Item] = []
    cj = claude_json()
    for s in _cfg_servers(cj, "mcpServers"):
        items.append(Item("claude", "mcp", s, cj, json_park, ev.get(f"mcp:{norm(s, 'claude')}")))
    settings = base / "settings.json"
    for key, d in _enabled_plugin_dirs(base):
        p = key.split("@")[0]
        bare = {f"use:{n}" for n in _plugin_names(d) - BUILTIN_SLASH}
        used = [v for k, v in ev.items()
                if k.startswith((f"use:{p}:", f"mcp:plugin_{norm(p, 'claude')}_")) or k in bare]
        it = Item("claude", "plugin", key, settings, plugin_park, max(used) if used else None)
        manifest, mcpj = _read_plugin_json(d / ".claude-plugin/plugin.json"), _read_plugin_json(d / ".mcp.json")
        if manifest is None or mcpj is None:  # exists but cannot be read safely: no evidence about it, so no parking
            it.note = "unreadable plugin manifest (symlink, device, directory, invalid or over 1 MiB): skipped"
        elif (d / "hooks").exists() or any(manifest.get(k) for k in INLINE_NO_SIGNAL):
            it.note = "has hooks/servers (no usage signal): skipped"
        elif not any((d / x).exists() for x in ("skills", "commands", "agents", ".mcp.json")):
            it.note = "no usage signal: skipped"
        else:
            chars = 0
            for sk in (d / "skills").glob("*/SKILL.md") if _plain_dir(d / "skills") else []:
                m = re.search(r"(?m)^description:\s*(.+)$", _skill_text(sk))
                chars += len(m.group(1)) if m else 0
            it.tokens = chars // 4 or None
        items.append(it)
    sk_root = base / "skills"
    for d in sorted(sk_root.iterdir()) if sk_root.is_dir() else []:
        if (d / "SKILL.md").exists():
            items.append(Item("claude", "skill", d.name, last_used=ev.get(f"use:{d.name}"),
                              note="report-only (park manually)"))
    return Scan("claude", oldest, items, blind=not any(k.startswith("mcp:") for k in ev), unscanned=bad)


def scan_codex() -> Scan | None:
    base = codex_dir()
    if not base.is_dir():
        return None
    cfg = base / "config.toml"
    root = base / "sessions"
    files = sorted(root.rglob("*.jsonl")) if root.is_dir() else []
    ev, oldest, bad = scan_logs("codex", files, CODEX_SCAN)
    servers: dict = {}
    try:
        servers = tomllib.loads(cfg.read_text()).get("mcp_servers", {})
    except (OSError, ValueError):
        pass
    # servers already `enabled = false` are not candidates: nothing to park, nothing to save
    names = [s for s, v in servers.items() if not (isinstance(v, dict) and v.get("enabled") is False)]
    return Scan("codex", oldest, [Item("codex", "mcp", s, cfg, toml_park, ev.get(f"mcp:{norm(s, 'codex')}")) for s in names],
                blind=not any(k.startswith("mcp:") for k in ev), unscanned=bad)


def _yaml_block(p: Path) -> str:
    """Body of the top-level `mcp_servers:` block only; it ends at the next top-level key (same rule as _yaml_top),
    so parked entries and other blocks' 2-space keys are never read as live servers."""
    try:
        lines = p.read_text().split("\n")
    except OSError:
        return ""
    top = next((i for i, l in enumerate(lines) if re.match(r"mcp_servers:\s*$", l)), None)
    if top is None:
        return ""
    end = next((i for i in range(top + 1, len(lines)) if lines[i][:1] not in ("", " ", "#")), len(lines))
    return "\n".join(lines[top + 1:end])


_YAML_UNSURE = re.compile(r"(?:^|\s)[&*]\w|<<\s*:|^\s*[{\[]|\{[^}]*\benabled\b|[\"']enabled[\"']\s*:")


def _hermes_enabled(block: str) -> list[str]:
    """Server names of a `mcp_servers:` body that are certainly live: entries whose own flag is `enabled:
    false|no|off|0` (quoted or not) are dropped like Codex's. The flag is read at the entry's own child indent,
    comments are ignored. Where the text parser cannot be sure what YAML would say (a duplicate `enabled:` field,
    anchors/aliases/merge keys, flow mappings, quoted names) the server is treated as unknown and NOT listed, so it
    can never become a park candidate."""
    out: list[str] = []
    name, off, child, unsure, flags = None, False, None, False, 0
    for ln in [*block.split("\n"), "  _end:"]:
        if not ln.strip() or ln.lstrip().startswith("#"):
            continue
        indent = len(ln) - len(ln.lstrip(" "))
        if indent <= 2:
            if name and not off and not unsure and flags <= 1:
                out.append(name)
            m = re.match(r"  ([\w.-]+):\s*(#.*)?$", ln)
            name, off, child, unsure, flags = (m.group(1) if m else None), False, None, False, 0
        elif name:
            child = indent if child is None else child
            unsure = unsure or bool(_YAML_UNSURE.search(ln))
            if indent == child and re.match(r"\s*enabled\s*:", ln):
                flags += 1
                if re.match(r"\s*enabled:\s*[\"']?(false|no|off|0)[\"']?\s*(#.*)?$", ln, re.I):
                    off = True
    return out


def scan_hermes() -> Scan | None:
    """Optional adapter: only active when ~/.hermes exists."""
    if not (home() / ".hermes").is_dir():
        return None
    cfg = home() / ".hermes/config.yaml"
    names = _hermes_enabled(_yaml_block(cfg))
    oldest, last = None, {}
    try:
        con = sqlite3.connect(f"file:{home() / '.hermes/state.db'}?mode=ro", uri=True)
        oldest = (con.execute("select min(timestamp) from messages").fetchone() or [None])[0]
        for tool, ts in con.execute("select tool_name, max(timestamp) from messages where tool_name like 'mcp%' group by 1"):
            last[tool] = ts
        con.close()
    except sqlite3.Error:
        pass
    items = []
    for s in names:
        used = [v for k, v in last.items() if s.replace("-", "_") in k.replace("-", "_")]
        items.append(Item("hermes", "mcp", s, cfg, yaml_park, max(used) if used else None))
    return Scan("hermes", oldest, items, blind=not last)


def scan_gemini() -> Scan | None:
    if not (home() / ".gemini").is_dir():
        return None
    cfg = home() / ".gemini/settings.json"
    # ceiling: gemini keeps no tool-use history we can read -> oldest=None => "no evidence: skipped"
    return Scan("gemini", None, [Item("gemini", "mcp", s, cfg, json_park) for s in _cfg_servers(cfg, "mcpServers")])


ADAPTERS = [scan_claude, scan_codex, scan_hermes, scan_gemini]


# ---------- decision ----------

def first_seen(items: list[Item], now: float) -> dict[str, float]:
    """An item's age starts the first time tokopt observes it (never at install time)."""
    p = state_dir() / "first_seen.json"
    raw = _read_state_json(p, {})
    fs: dict[str, float] = {k: v for k, v in raw.items() if isinstance(v, (int, float)) and not isinstance(v, bool)} if isinstance(raw, dict) else {}
    new = fs != raw  # unusable content is replaced
    if new:
        print(f"tokopt: warning: {p} is not a JSON object of timestamps; unusable entries are treated as never seen "
              "(those items age from now) and the file is rewritten", file=sys.stderr)
    for it in items:
        k = f"{it.agent}:{it.kind}:{it.name}"
        if k not in fs:
            fs[k] = now
            new = True
    if new:
        try:
            private_dir(state_dir())
            atomic_write(p, json.dumps(fs).encode(), follow=False)
        except OSError as e:  # unwritable state dir: report must still work (items just age from now on every run)
            print(f"tokopt: warning: cannot write {p} ({e.strerror}); items age from now on every run", file=sys.stderr)
    return fs


def decide(it: Item, scan: Scan, policy: dict, now: float, seen: dict[str, float]) -> tuple[str, str]:
    """-> (action, reason); action in park|keep|skip."""
    if is_protected(policy, it.name):
        return "keep", "protected"
    if it.note:
        return "skip", it.note
    if scan.oldest is None:
        return "skip", "no evidence: skipped"
    if scan.incomplete:
        return "skip", "log scan incomplete (unreadable, non-regular or oversized-line log; see WARNING): skipped"
    idle = policy["skill_idle_days" if it.kind in ("skill", "plugin") else "mcp_idle_days"] * DAY
    if now - scan.oldest < idle:
        return "skip", "insufficient history"
    if scan.blind:
        return "skip", "scan found no tool calls: skipped"
    if it.last_used is not None:
        age = now - it.last_used
        return ("park", f"unused {int(age / DAY)}d") if age >= idle else ("keep", f"used {int(age / DAY)}d ago")
    age = now - seen[f"{it.agent}:{it.kind}:{it.name}"]
    return ("park", f"never seen used; first_seen {int(age / DAY)}d ago") if age >= idle \
        else ("keep", f"never seen used; aging {int(age / DAY)}d/{int(idle / DAY)}d")


def plan(policy: dict, now: float):
    scans = []
    for ad in ADAPTERS:
        try:
            sc = ad()
        except Exception as e:  # one bad file must not take the other agents' report down
            name = getattr(ad, "__name__", "adapter")
            sc = Scan(name, None, [Item(name, "adapter", name, note=f"adapter failed: {name} ({type(e).__name__})")])
        if sc is not None:
            scans.append(sc)
    seen = first_seen([i for s in scans for i in s.items], now)
    rows = []
    for s in scans:
        for it in s.items:
            rows.append((it, *decide(it, s, policy, now, seen)))
    return scans, rows


# ---------- sessions ----------

# Only Claude Code sessions are ever stopped: its per-pid session file is the one reliable idleness signal.
# (Codex/Gemini keep no per-process state, and a foreign process cannot be matched to CODEX_HOME.)
# Never targets: channel/SDK/print-mode (non-interactive) instances.
SKIP_ARGS = re.compile(r"--channels|--print\b|(?:^|\s)-p(?:\s|$)|stream-json")


def _cmd_tuple(spec: Any, root: str = "") -> tuple[str, ...] | None:
    """(command, *args) of one MCP server config; None when it is not a plain command (url servers, bad shapes)."""
    spec = _dict(spec)
    cmd, args = spec.get("command"), spec.get("args", [])
    if not isinstance(cmd, str) or not cmd or not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        return None
    return tuple(x.replace("${CLAUDE_PLUGIN_ROOT}", root) for x in (cmd, *args))


def configured_mcp_commands() -> set[tuple[str, ...]]:
    """Command lines of the MCP servers the agent itself is configured to launch (user, project, enabled plugins)."""
    cj = _dict(_read_json(claude_json(), {}))
    specs = list(_dict(cj.get("mcpServers")).values())
    for proj in _dict(cj.get("projects")).values():
        specs += list(_dict(_dict(proj).get("mcpServers")).values())
    out = {t for t in (_cmd_tuple(x) for x in specs) if t}
    for _, d in _enabled_plugin_dirs(claude_dir()):
        manifest = _read_plugin_json(d / ".claude-plugin/plugin.json") or {}
        mcp = _read_plugin_json(d / ".mcp.json") or {}
        servers = {**_dict(manifest.get("mcpServers")), **_dict(mcp.get("mcpServers", mcp))}
        out |= {t for t in (_cmd_tuple(x, str(d)) for x in servers.values()) if t}
    return out


# Interpreters a shebang script or `npm exec` runs under: they prefix the real command in `ps`.
_INTERP = re.compile(r"(?i)^(python[\d.]*|node|nodejs|bun|deno|ruby)$")
_PH = re.compile(r"\$\{[^}]*\}")


def _placeholder_match(lits: list[str], got: str) -> bool:
    """`lit0 ${..} lit1 ${..} ... litN` against `got`, with each placeholder a NON-EMPTY, whitespace-free gap.
    Linear time (ordered `find`, no regex backtracking): starts with lit0, ends with litN, the middle literals
    occur in order, leftmost placement."""
    if not got.startswith(lits[0]):
        return False
    pos = len(lits[0])
    for lit in lits[1:-1]:
        i = got.find(lit, pos + 1)
        if i < 0 or any(c.isspace() for c in got[pos:i]):
            return False
        pos = i + len(lit)
    end = len(got) - len(lits[-1])
    return end >= pos + 1 and got.endswith(lits[-1]) and not any(c.isspace() for c in got[pos:end])


def _tok_eq(cfg: str, got: str, exe: bool = False) -> bool:
    """One configured token vs one live token, anchored (full match).
    - `${VAR}` (the agent expands it before spawning) becomes a non-empty `\\S+` group; the literal text around it must
      match too, and a token whose literal part is nothing but `/` or `-` never matches.
    - Absolute paths: equal after normalisation. The executable (position 0) may be bare in config or live:
      same file name, any directory. Any other relative token must be equal as written."""
    if _PH.search(cfg):
        if not _PH.sub("", cfg).replace("/", "").replace("-", ""):
            return False
        return _placeholder_match(_PH.split(cfg), got)
    if cfg.startswith("/") and got.startswith("/"):
        return os.path.normpath(cfg) == os.path.normpath(got)
    if exe and ("/" not in cfg or cfg.startswith("/")) and ("/" not in got or got.startswith("/")):
        return os.path.basename(cfg) == os.path.basename(got)
    return cfg == got


def _launcher(t: list[str]) -> list[str]:
    """Undo the rewrites launchers show in `ps`: `npm exec X` is `npx X`, `uv tool uvx X` is `uvx X`; npx's -y is dropped."""
    b = os.path.basename(t[0])
    if b == "npm" and t[1:2] == ["exec"]:
        t = ["npx", *t[2:]]
    elif b == "uv" and t[1:3] in (["tool", "uvx"], ["tool", "run"]):
        t = ["uvx", *t[3:]]
    if os.path.basename(t[0]) == "npx":
        t = [t[0], *(x for x in t[1:] if x not in ("-y", "--yes"))]
    return t


def _covers(cfg: list[str], live: list[str]) -> bool:
    """The live command line IS the configured server (same tokens, same order), with an interpreter allowed in front."""
    if _tok_eq(cfg[0], live[0], exe=True):
        want, got = cfg[1:], live[1:]
    elif _INTERP.match(os.path.basename(live[0])):
        want, got = (cfg[1:], live[1:]) if _INTERP.match(os.path.basename(cfg[0])) else (cfg, live[1:])
    else:
        return False
    return len(want) == len(got) and all(_tok_eq(w, g, exe=(i == 0 and want is cfg)) for i, (w, g) in enumerate(zip(want, got)))


def is_idle_child(args: str, idle_cmds: Iterable[tuple[str, ...]]) -> bool:
    """A child is idle only if it is one of the agent's own configured MCP servers (anchored: every token must
    match, an interpreter may prefix it) or `caffeinate`, which Claude itself spawns. Everything else is work."""
    try:
        live = shlex.split(args)
    except ValueError:
        return False
    if not live:
        return False
    if live[0] in ("caffeinate", "/usr/bin/caffeinate"):  # only the exact shape Claude spawns; `caffeinate -i ./job` wraps work
        return len(live) == 4 and live[1:3] == ["-i", "-t"] and live[3].isdecimal()
    live = _launcher(live)
    return any(_covers(_launcher(list(c)), live) for c in idle_cmds if c)


def is_protected(policy: dict, *names: str) -> bool:
    """`protect`: case-insensitive substring of any of the given names (item name, agent, working directory)."""
    return any(p.lower() in n.lower() for p in policy.get("protect", []) for n in names)


def ps_rows() -> list[dict]:
    """Process table. Names come from `ps -c` (executable name only, may contain spaces); args are joined by pid."""
    args: dict[int, str] = {}
    for ln in _run([PS, "-axo", "pid=,args="]).splitlines():
        f = ln.split(None, 1)
        if f and f[0].isdigit():
            args[int(f[0])] = f[1] if len(f) > 1 else ""
    rows = []
    for ln in _run([PS, "-axco", "pid=,ppid=,uid=,stat=,pcpu=,comm="]).splitlines():
        f = ln.split(None, 5)
        try:
            pid, ppid, uid, cpu = int(f[0]), int(f[1]), int(f[2]), float(f[4])
        except (ValueError, IndexError):
            continue
        if len(f) == 6:  # every uid is kept: another user's child (a background sudo) is still work
            rows.append({"pid": pid, "ppid": ppid, "uid": uid, "stat": f[3], "cpu": cpu, "comm": f[5].strip(),
                         "args": args.get(pid, f[5].strip())})
    return rows


def proc_ident(pid: int) -> str:
    """Process identity (command + start time): differs if the pid is recycled."""
    return _run([PS, "-p", str(pid), "-o", "comm=,lstart="]).strip()


def _lstart_epoch(s: str) -> float | None:
    """`ps -o lstart=` text (read with TZ=UTC) -> UTC epoch."""
    try:
        return calendar.timegm(time.strptime(" ".join(s.split()), "%a %b %d %H:%M:%S %Y"))
    except ValueError:
        return None


def proc_start_utc(pid: int) -> float | None:
    """Process start as a UTC epoch. Claude records procStart in UTC; ps prints local time unless told otherwise."""
    return _lstart_epoch(_run([PS, "-p", str(pid), "-o", "lstart="], env={"TZ": "UTC", "LC_ALL": "C"}))


def _has_active_child(pid: int, byp: dict[int, dict], kids: dict[int, list[int]], idle_cmds: Iterable[tuple[str, ...]]) -> bool:
    for c in kids.get(pid, []):
        r = byp[c]
        if r["stat"].startswith("Z"):
            continue
        if r["stat"].startswith("R") or r["cpu"] >= 1.0 or not is_idle_child(r["args"], idle_cmds):
            return True
    return False


def _proc_graph(rows: list[dict]) -> tuple[dict[int, dict], dict[int, list[int]]]:
    byp = {r["pid"]: r for r in rows}
    kids: dict[int, list[int]] = {}
    for r in rows:
        kids.setdefault(r["ppid"], []).append(r["pid"])
    return byp, kids


def select_idle_sessions(rows: list[dict], activity: Callable[[dict], float | None], policy: dict,
                         now: float, self_pid: int, idle_cmds: Iterable[tuple[str, ...]] = (),
                         uid: int | None = None) -> list[dict]:
    """Claude processes of `uid` (default: ours) idle > threshold, minus protected ones, ones with an active child
    (a child of ANY uid counts) and the one running us. A row without a `uid` is not ours."""
    uid = os.getuid() if uid is None else uid
    byp, kids = _proc_graph(rows)
    ancestors, p = set(), self_pid
    while p in byp and p not in ancestors:
        ancestors.add(p)
        p = byp[p]["ppid"]
    out = []
    for r in rows:
        if os.path.basename(r["comm"]) != "claude" or r.get("uid") != uid or r["pid"] in ancestors \
                or SKIP_ARGS.search(r["args"]):
            continue
        rr = {**r, "agent": "claude"}
        last = activity(rr)  # also fills rr["session"] / rr["cwd"]
        if last is None or now - last < policy["session_idle_hours"] * 3600:
            continue
        if is_protected(policy, "claude", str(rr.get("cwd", "")), str(rr.get("session", ""))):
            continue
        if _has_active_child(r["pid"], byp, kids, idle_cmds):
            continue
        out.append({**rr, "idle_h": (now - last) / 3600})
    return out


def idle_sessions(policy: dict, now: float) -> list[dict]:
    out = select_idle_sessions(ps_rows(), real_activity, policy, now, os.getpid(), configured_mcp_commands())
    for s in out:
        s["ident"] = proc_ident(s["pid"])  # snapshot: re-checked right before any signal
    return out


def _claude_activity(r: dict) -> float | None:
    """From Claude's own per-pid map: only an interactive CLI session reporting `idle`, whose recorded
    start time matches the live process, can be idle. busy/shell/unknown => None (active)."""
    try:
        info = json.loads(_read_small(claude_dir() / "sessions" / f"{r['pid']}.json"))
    except (OSError, ValueError):
        info = None
    if not isinstance(info, dict) or info.get("kind") != "interactive" or info.get("entrypoint") != "cli" \
            or info.get("status") != "idle":
        return None
    want = _lstart_epoch(str(info.get("procStart", "")))
    if want is None or want != proc_start_utc(r["pid"]):
        return None  # stale file for a recycled pid, or an unparseable start time
    ts = info.get("statusUpdatedAt") or info.get("updatedAt")
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        return None
    r["session"], r["cwd"] = str(info.get("sessionId", "")), str(info.get("cwd", ""))
    return ts / 1000


def real_activity(r: dict) -> float | None:
    """Last activity epoch for a process, or None = cannot establish => treated as active.
    Claude: its per-pid session map. Anything else: unknown."""
    return _claude_activity(r) if r["agent"] == "claude" else None


def _oneline(s: str) -> str:
    """Single-line, control-free text: a name containing a newline must not forge an output line."""
    return CTRL_RE.sub("?", s.replace("\n", "?").replace("\t", "?"))


def _clean(s: str, cap: int = 200) -> str:
    return _oneline(s)[:cap]


def stop_session(s: dict, policy: dict, now: float) -> tuple[str, str]:
    """SIGTERM one idle session after re-verifying identity and idleness. -> (stopped|skipped|failed, message)."""
    pid, label = s["pid"], f"{s['agent']} pid {s['pid']}"
    gone = f"{label}: process identity changed or gone since selection; not signalled"
    if not s.get("ident") or proc_ident(pid) != s["ident"]:
        return "skipped", gone
    live = {"pid": pid, "agent": s["agent"]}
    last = real_activity(live)
    if last is None or now - last < policy["session_idle_hours"] * 3600:
        return "skipped", f"{label}: active again or activity unverifiable; not signalled"
    if is_protected(policy, s["agent"], str(live.get("cwd", "")), str(live.get("session", ""))):
        return "skipped", f"{label}: protected by policy; not signalled"
    if proc_ident(pid) != s["ident"]:  # the activity check is slow: re-verify right before signalling
        return "skipped", gone
    # the confirmation prompt can sit for ages: look at the process table again, right before signalling
    byp, kids = _proc_graph(ps_rows())
    if pid not in byp:  # empty/failed table (ps timed out) or the process is gone: unverifiable, so do not signal
        return "skipped", f"{label}: process table unavailable or process gone; not signalled"
    row = byp[pid]
    if os.path.basename(row["comm"]) != "claude" or row.get("uid") != os.getuid():
        return "skipped", f"{label}: pid {pid} is not one of your claude processes now; not signalled"
    if _has_active_child(pid, byp, kids, configured_mcp_commands()):
        return "skipped", f"{label}: has an active child process now; not signalled"
    idle_h = (now - last) / 3600
    sid = _clean(live.get("session") or s.get("session", ""))
    resume = f"claude --resume {shlex.quote(sid)}"
    note = state_dir() / "stopped" / f"{s['agent']}-{pid}.md"
    try:  # the handoff note exists before the signal: a stopped session must never be unresumable
        private_dir(note.parent)
        atomic_write(note, (f"{s['agent']} pid {pid} cwd {_clean(live.get('cwd') or s.get('cwd', '?'))} "
                            f"session {sid or '?'} idle {idle_h:.0f}h stopped by tokopt; resume: {resume}\n").encode(), follow=False)
    except OSError as e:
        return "failed", f"{label}: handoff note not written ({e}); not signalled"
    try:
        os.kill(pid, signal.SIGTERM)  # never SIGKILL
    except (ProcessLookupError, PermissionError) as e:
        note.unlink(missing_ok=True)
        return "failed", f"{label}: kill failed ({type(e).__name__}: {e})"
    try:
        ledger_add({"id": uuid.uuid4().hex[:8], "agent": s["agent"], "kind": "session", "item": str(pid),
                    "action": "stop", "before": None, "after": None, "reason": f"idle {idle_h:.0f}h",
                    "ts": datetime.now(timezone.utc).isoformat()})
    except OSError as e:  # the signal is already sent: report it, keep going
        return "stopped", f"stopped {label} (idle {idle_h:.0f}h); WARNING ledger row not written ({e})"
    return "stopped", f"stopped {label} (idle {idle_h:.0f}h)"


# ---------- apply / restore / report ----------

def ledger_add(row: dict) -> None:
    private_dir(state_dir())
    fd = os.open(state_dir() / "ledger.jsonl", os.O_APPEND | os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a") as f:
        f.write("\n" + json.dumps(row) + "\n")  # leading newline: after a half-written row this still starts a new line


def ledger_rows() -> list[dict]:
    p = state_dir() / "ledger.jsonl"
    rows = []
    try:
        text = _state_bytes(p).decode(errors="replace")
    except FileNotFoundError:
        text = ""
    except OSError as e:
        raise Busy(f"cannot read the ledger {p} ({e.strerror}); remove it if it is not a regular file") from None
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError("not an object")
            rows.append(row)
        except ValueError:
            print(f"tokopt: ledger line {n} is unreadable; skipped", file=sys.stderr)
    return rows


def known_configs() -> set[str]:
    return {str(claude_json()), str(claude_dir() / "settings.json"), str(codex_dir() / "config.toml"),
            str(home() / ".hermes/config.yaml"), str(home() / ".gemini/settings.json")}


def _sig(p: Path) -> tuple[int, int]:
    st = p.stat()
    return st.st_mtime_ns, st.st_size


def edit_bytes(path: Path, fn: Callable[[bytes], bytes], backup: Callable[[bytes], Path] | None = None):
    """Read, transform, atomically replace. Aborts (RuntimeError) if a live process touched the file meanwhile.
    Every write to an agent config goes through here (park, semantic restore and byte-exact restore)."""
    sig0 = _sig(path)
    before = path.read_bytes()
    after = fn(before)
    if after == before:
        raise NoChange(f"no change to {path.name}")
    if _sig(path) != sig0:
        raise RuntimeError(f"{path} changed during read; aborted")
    bk = backup(before) if backup else None
    if _sig(path) != sig0:
        if bk:
            bk.unlink(missing_ok=True)
        raise RuntimeError(f"{path} changed before write; aborted")
    atomic_write(path, after, stat.S_IMODE(path.stat().st_mode))
    return before, after, bk


def edit_file(path: Path, fn: Callable[[str], str], backup: Callable[[bytes], Path] | None = None):
    return edit_bytes(path, lambda b: fn(b.decode()).encode(), backup)


def _still_parked(kind: str | None, live: bytes, after: bytes, name: str) -> bool:
    if sha(live) == sha(after):
        return True
    un = EDITS.get(kind or "", (None, None))[1]
    if not un:
        return False
    try:
        return un(live.decode(), name) != live.decode()
    except ValueError:
        return False


def park(it: Item, reason: str, stamp: str) -> tuple[str, str]:
    """-> (status, message); status is `parked` or `reverted-by-host`. Raises RuntimeError/ValueError/OSError."""
    assert it.path and it.edit
    path, edit = it.path, it.edit
    rid = uuid.uuid4().hex[:8]

    def backup(before: bytes) -> Path:
        bdir = state_dir() / "backups" / stamp
        private_dir(bdir)
        bk = bdir / f"{rid}-{path.name}"
        atomic_write(bk, before, 0o600, follow=False)
        return bk

    before, after, bk = edit_file(path, lambda t: edit(t, it.name), backup)
    kind = edit_kind(edit)
    time.sleep(VERIFY_DELAY)  # a running agent that holds its own copy of the file may write it back
    try:
        live = path.read_bytes()
    except OSError:
        live = b""
    ok = _still_parked(kind, live, after, it.name)
    status = "parked" if ok else "reverted-by-host"
    try:
        ledger_add({"id": rid, "agent": it.agent, "kind": it.kind, "item": it.name, "action": "park", "edit": kind,
                    "status": status, "before": {"file": str(path), "sha256": sha(before)},
                    "after": {"file": str(path), "sha256": sha(after), "backup": str(bk)},
                    "reason": reason, "ts": datetime.now(timezone.utc).isoformat()})
    except OSError as e:  # the config is already edited: say so and name the backup, never a false SKIPPED
        return "unrecorded", (f"WARNING {it.agent} {it.kind} {it.name} was parked but the ledger row could not be written "
                              f"({e}); restore by hand from the backup: {bk}")
    if not ok:
        return status, (f"WARNING {it.agent} {it.kind} {it.name}: {path} was reverted by the running agent; "
                        f"quit {it.agent} and re-run apply [ledger {rid}]")
    return status, f"parked {it.agent} {it.kind} {it.name} ({reason}) [ledger {rid}]"


def _restore_one(r: dict) -> str:
    who = f"{r.get('id')} {r.get('item')}"
    try:
        p, bk = Path(r["after"]["file"]), Path(r["after"]["backup"])
        before_sha, after_sha = r["before"]["sha256"], r["after"]["sha256"]
    except (KeyError, TypeError):
        return f"REFUSED {who}: malformed ledger row"
    if bk.is_symlink():
        return f"REFUSED {who}: the backup {bk} is a planted symlink; remove it (never followed)"
    bdir = os.path.realpath(state_dir() / "backups")
    if str(p) not in known_configs() or r["before"].get("file") != str(p) \
            or not os.path.realpath(bk).startswith(bdir + os.sep):
        return f"REFUSED {who}: ledger row points outside the known agent configs / backup dir"
    try:
        cur = p.read_bytes()
    except OSError as e:
        return f"REFUSED {who}: cannot read {p} ({e})"
    data = None
    if sha(cur) == after_sha:  # untouched since apply: byte-exact restore, if the backup is still good
        try:
            data = _state_bytes(bk)
        except OSError:
            pass  # pruned (BACKUP_DAYS) or unreadable: the semantic inverse below does not need it
        if data is not None and sha(data) == before_sha:
            def exact(now: bytes) -> bytes:
                if sha(now) != after_sha:
                    raise RuntimeError(f"{p} changed before restore; aborted")
                return data  # type: ignore[return-value]
            try:
                edit_bytes(p, exact)
            except (RuntimeError, OSError) as e:
                return f"REFUSED {who}: {e} (backup: {bk})"
            return ""
    un = EDITS.get(r.get("edit") or "", (None, None))[1]
    if not un:
        return f"REFUSED {who}: {p} changed since apply and has no semantic inverse (backup: {bk})"
    try:
        edit_file(p, lambda t: un(t, r["item"]))
    except NoChange:
        return ""
    except (ValueError, RuntimeError, OSError) as e:
        return f"REFUSED {who}: {e} (backup: {bk})"
    return ""


def open_parks() -> list[dict]:
    """Park rows not yet restored, with their effective status (a later `status` row overrides the original)."""
    allrows = ledger_rows()
    done = {r.get("id") for r in allrows if r.get("action") == "restore"}
    status = {r.get("id"): r.get("status") for r in allrows if r.get("action") == "status"}
    return [{**r, "status": status.get(r.get("id"), r.get("status"))}
            for r in allrows if r.get("action") == "park" and r.get("id") not in done]


def _parked_state(r: dict) -> str:
    """Is a ledger row's server parked in the file right now? -> parked | live | both | unknown (never raises)."""
    try:
        p = Path(r["after"]["file"])
        un = EDITS[r["edit"]][1]
        if str(p) not in known_configs():
            return "unknown"
        un(p.read_text(), r["item"])
        return "parked"
    except NoChange:
        return "live"
    except BothPresent:
        return "both"
    except (KeyError, TypeError, ValueError, OSError):
        return "unknown"


def reconcile_ledger() -> list[str]:
    """Rows recorded as parked whose server is live again (the host undid the park later): mark them
    reverted-by-host so restore does not claim to restore them."""
    msgs = []
    for r in open_parks():
        if r.get("status") == "parked" and _parked_state(r) in ("live", "both"):
            try:
                ledger_add({"id": r["id"], "action": "status", "status": "reverted-by-host", "ts": datetime.now(timezone.utc).isoformat()})
            except OSError as e:
                msgs.append(f"ledger: cannot record that {r['item']} is live again ({e})")
                break
            msgs.append(f"ledger: {r['agent']} {r['kind']} {r['item']} is live again (the host undid the park); "
                        f"row {r['id']} marked reverted-by-host")
    return msgs


def restore(target: str) -> list[str]:
    rows = open_parks()
    rows = rows[::-1] if target == "--all" else [r for r in rows if r.get("id") == target]
    if not rows:
        return [f"nothing to restore for {target}"]
    msgs = []
    for r in rows:
        if r.get("status") == "reverted-by-host" and _parked_state(r) == "live":
            # the host already put it back: nothing to undo; the backup stays (evidence, pruned by age)
            try:
                ledger_add({**r, "action": "restore", "status": "nothing-to-restore", "ts": datetime.now(timezone.utc).isoformat()})
            except OSError as e:
                msgs.append(f"REFUSED {r['id']} {r['item']}: nothing to restore, but the ledger row could not be written ({e})")
                continue
            msgs.append(f"nothing to restore (host already reverted) {r['agent']} {r['kind']} {r['item']} [ledger {r['id']}]")
            continue
        err = _restore_one(r)
        if err:
            msgs.append(err)
            continue
        try:
            ledger_add({**r, "action": "restore", "ts": datetime.now(timezone.utc).isoformat()})
        except OSError as e:
            msgs.append(f"REFUSED {r['id']} {r['item']}: restored, but the ledger row could not be written ({e}); backup kept")
            continue
        Path(r["after"]["backup"]).unlink(missing_ok=True)  # full config copy: no longer needed
        msgs.append(f"restored {r['agent']} {r['kind']} {r['item']} [ledger {r['id']}]")
    return msgs


def prune_backups(now: float) -> None:
    """Backups are full copies of agent configs (tokens included): drop the ones older than BACKUP_DAYS."""
    root = state_dir() / "backups"
    for f in root.glob("*/*"):
        try:
            if f.stat().st_mtime < now - BACKUP_DAYS * DAY:
                f.unlink()
        except OSError:
            pass
    for d in root.glob("*"):
        try:
            d.rmdir()  # only succeeds when empty
        except OSError:
            pass


def _tilde(p: str) -> str:
    h = str(home())
    return "~" + p[len(h):] if p == h or p.startswith(h + os.sep) else p


def fmt_last(it: Item, now: float) -> str:
    return "never seen" if it.last_used is None else f"{int((now - it.last_used) / DAY)}d ago"


def cheap_agent_status(policy: dict) -> list[str]:
    """Listed agents whose file does not pin `model: haiku` yet. Report-only: agent files are never edited."""
    ad = claude_dir() / "agents"
    out = []
    for a in policy["cheap_agents"]:
        try:
            text = _read_small(ad / f"{a}.md").decode(errors="replace")
        except OSError:
            continue
        m = re.search(r"(?m)^model:\s*(\S+)", text)
        if not m or m.group(1).lower() != "haiku":
            out.append(f"{a} ({m.group(1) if m else 'unpinned'})")
    return out


def scheduled_job_warning() -> str:
    """The installed launchd job pins the resolved program path; an upgrade can remove it and the 03:30 run then
    silently never happens. Empty string when there is no job or its program is fine."""
    try:  # advisory helper: whatever is (or is not) at that path, it must never take report down
        with _open_regular(home() / "Library/LaunchAgents/com.tokopt.agent.plist") as f:
            prog = plistlib.load(f)["ProgramArguments"][0]
        if isinstance(prog, str) and not os.access(prog, os.X_OK):
            return (f"WARNING scheduled job: its program {prog} no longer exists or is not executable (Python or pipx "
                    "was upgraded or reinstalled): re-run scripts/install-tokopt-launchd.sh")
    except Exception:  # OSError, ExpatError, RecursionError, KeyError, ValueError (NUL byte), ...: no usable job
        pass
    return ""


def report(policy: dict, now: float, sessions: list[dict]) -> str:
    scans, rows = plan(policy, now)
    lines = [f"{'item':44} {'agent':7} {'kind':6} {'last used':11} {'tok/req':8} action"]
    for it, act, why in sorted(rows, key=lambda r: ({"park": 0, "keep": 1, "skip": 2}[r[1]], r[0].agent, r[0].name)):
        tok = f"~{it.tokens}" if it.tokens else "n/a"
        lines.append(f"{it.name[:44]:44} {it.agent:7} {it.kind:6} {fmt_last(it, now):11} {tok:8} {act.upper()}: {why}")
    if any(it.tokens for it, _, _ in rows):
        lines.append("tok/req: ~ = chars/4 of skill descriptions only; n/a = size not measurable (MCP tool schemas)")
    for s in scans:
        if s.oldest is not None:
            lines.append(f"history[{s.agent}]: oldest log {int((now - s.oldest) / DAY)}d")
    for s in scans:
        if s.unscanned:
            more = f" (+{len(s.unscanned) - 5} more)" if len(s.unscanned) > 5 else ""
            lines.append(f"WARNING log scan incomplete [{s.agent}], nothing is parked for {s.agent} until these are "
                         f"fixed or removed: {', '.join(_tilde(p) for p in s.unscanned[:5])}{more}")
    for s in sessions:
        lines.append(f"idle session: {s['agent']} pid {s['pid']} idle {s['idle_h']:.0f}h -> STOP on apply")
    if (cheap := cheap_agent_status(policy)):
        lines.append("could take `model: haiku` (edit the agent file yourself): " + ", ".join(cheap))
    if (w := scheduled_job_warning()):
        lines.append(w)
    return "\n".join(_oneline(mask(ln)) for ln in lines)


# ---------- CLI ----------

def _confirmed() -> Path:
    return state_dir() / "confirmed"


def _ask() -> bool:
    return sys.stdin.isatty() and input("Apply this plan? [y/N] ").strip().lower() == "y"


def _gate(a, policy: dict, now: float, scopes: set[str], sessions: list[dict]) -> bool:
    """Apply needs a confirmation covering this policy and these item kinds/agents. The first apply on a
    machine, a policy change, or a never-confirmed scope shows the plan and needs --yes (or a y).
    Scheduled runs never prompt: they refuse instead."""
    m = _read_state_json(_confirmed(), None)
    paths = sorted(known_configs())
    same = isinstance(m, dict) and m.get("policy") == policy_hash(policy) and m.get("paths") == paths
    known = set(m.get("scopes", [])) if same and isinstance(m, dict) else set()
    if same and scopes <= known:
        return True
    print(report(policy, now, sessions))
    why = "first apply on this machine" if m is None or not isinstance(m, dict) else \
        "policy or agent config paths changed since the last confirmation" if not same else f"new item kinds/agents: {sorted(scopes - known)}"
    if a.scheduled:
        print(f"tokopt: {why}; run `tokopt apply` once interactively to confirm. Nothing changed.")
        return False
    if not (a.yes or _ask()):
        print(f"tokopt: {why}: review the plan above, then re-run with --yes. Nothing changed.")
        return False
    private_dir(state_dir())
    atomic_write(_confirmed(), json.dumps({"policy": policy_hash(policy), "scopes": sorted(known | scopes), "paths": paths}).encode(), follow=False)
    return True


class Busy(RuntimeError):
    """Another apply/restore holds the state lock."""


def _preflight_state() -> None:
    """tokopt's state dir is ours: a symlink in it, in backups/ or stopped/, or in backups/<stamp>/ means someone
    planted it. Refuse BEFORE any config is edited or process signalled, not halfway through."""
    todo = [state_dir()]
    while todo:
        d = todo.pop()
        for e in os.scandir(d):
            if e.is_symlink():
                raise Busy(f"{e.path} is a symlink in tokopt's state directory; remove it (nothing was changed)")
            depth = len(Path(e.path).relative_to(state_dir()).parts)
            if e.is_dir(follow_symlinks=False) and (depth == 1 and e.name in ("backups", "stopped") or depth == 2 and d.name == "backups"):
                todo.append(Path(e.path))


@contextmanager
def state_lock(strict: bool = False) -> Iterator[None]:
    """Exclusive, non-blocking lock so a scheduled apply and a manual apply/restore never interleave."""
    try:
        private_dir(state_dir())
    except OSError as e:  # e.g. a dangling symlink where the state directory should be
        raise Busy(f"cannot use the state directory {state_dir()} ({e.strerror}); fix or remove it") from None
    try:
        fd = os.open(state_dir() / "lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    except OSError as e:  # e.g. a planted symlink (ELOOP)
        raise Busy(f"cannot open the lock file {state_dir() / 'lock'} ({e.strerror}); remove it if it is not a regular file") from None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Busy("another tokopt apply/restore is running (lock: ~/.local/state/tokopt/lock); try again later") from None
        except OSError as e:  # e.g. NFS or a FUSE mount without flock support
            if strict:  # unattended: no lock means a manual run could interleave, so refuse
                raise Busy(f"cannot lock {state_dir() / 'lock'} ({e.strerror}); refusing to run unattended without the lock") from None
            print(f"tokopt: warning: cannot lock {state_dir() / 'lock'} ({e.strerror}); continuing without the lock: "
                  "do not run two tokopt commands at the same time", file=sys.stderr)
        _preflight_state()  # after the lock is held: check and use are serialised
        yield
    finally:
        os.close(fd)  # closing releases the flock


def _restore_cmd(a) -> int:
    msgs = restore("--all" if a.all else a.target)
    print("\n".join(_oneline(mask(m)) for m in msgs))
    return int(any(m.startswith("REFUSED") for m in msgs))


def _apply_cmd(a, policy: dict, now: float) -> int:
    cands = [(it, why) for it, act, why in plan(policy, now)[1] if act == "park"]
    sessions = idle_sessions(policy, now)
    scopes = {f"{it.agent}:{it.kind}" for it, _ in cands} | ({"session:stop"} if sessions else set())
    if not _gate(a, policy, now, scopes, sessions):
        return 2
    prune_backups(now)
    for m in reconcile_ledger():
        print(_oneline(mask(m)))
    rc = 0
    # sessions first: a SIGTERMed agent may flush its config on the way out, which would undo a park
    for s in sessions:
        status, msg = stop_session(s, policy, time.time())
        rc |= status == "failed"
        print(_oneline(mask(msg)))
    stamp = time.strftime("%Y%m%dT%H%M%S")
    for it, why in cands:
        try:
            status, msg = park(it, why, stamp)
            rc |= status != "parked"
            print(_oneline(mask(msg)))
            if status == "unrecorded":  # no ledger means no restore: do not edit another config
                print("tokopt: the ledger is not writable; stopping here, no further configs were edited")
                return 1
        except (ValueError, RuntimeError, OSError, KeyError) as e:
            rc = 1
            print(_oneline(mask(f"SKIPPED {it.agent} {it.name}: {e}")))
    return rc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tokopt")
    ap.add_argument("cmd", nargs="?", choices=["report", "apply", "restore"])
    ap.add_argument("target", nargs="?")
    ap.add_argument("--all", action="store_true", help="restore: undo every parked item")
    ap.add_argument("--yes", "-y", action="store_true", help="apply: confirm the plan (first apply / changed policy)")
    ap.add_argument("--scheduled", action="store_true", help="apply: unattended run; refuses until a confirmed apply exists")
    ap.add_argument("--check-job", action="store_true", help="print a warning if the installed launchd job's program has vanished")
    ap.add_argument("--version", action="version", version=f"tokopt {__version__}")
    a = ap.parse_args(argv)
    if a.check_job:
        if a.cmd or a.target or a.all or a.yes or a.scheduled:
            ap.error("--check-job stands alone")
        if (w := scheduled_job_warning()):
            print(_oneline(mask(w)))
        return 0
    a.cmd = a.cmd or "report"
    if a.target and a.cmd != "restore":
        ap.error(f"unexpected argument {a.target!r} (only `restore` takes a ledger id)")
    if a.cmd == "restore" and not (a.target or a.all):
        ap.error("restore needs <ledger-id|--all>")
    try:
        if a.cmd == "restore":
            with state_lock():
                return _restore_cmd(a)
        try:
            policy = load_policy()
        except ValueError as e:
            print(f"tokopt: invalid policy file ~/.config/tokopt/policy.toml: {e}")
            return 2
        now = time.time()
        if a.cmd == "report":
            print(report(policy, now, idle_sessions(policy, now)))
            return 0
        with state_lock(strict=a.scheduled):
            return _apply_cmd(a, policy, now)
    except Busy as e:
        print(f"tokopt: {e}")
        return 2


if __name__ == "__main__":
    sys.exit(main())

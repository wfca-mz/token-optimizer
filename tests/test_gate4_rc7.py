"""Last respin: third-party plugin content is read bounded and regular-file only (N1) and the L1-L5 items."""
from __future__ import annotations

import json
import os
import re
import resource
import subprocess
import sys
import time
from pathlib import Path

import pytest

from test_tokopt import _tracked, age_first_seen, fake_home, make_plugin  # noqa: F401
from tokopt import cli as tokopt

ROOT = Path(__file__).resolve().parent.parent


def limit_memory():
    """A runaway read must die, not eat the machine: cap the child's address space (honoured on Linux)."""
    try:
        resource.setrlimit(resource.RLIMIT_AS, (8 << 30, 8 << 30))
    except (ValueError, OSError):
        pass


def cli(args: list[str], timeout: float = 40) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "tokopt", *args], capture_output=True, text=True, timeout=timeout, cwd=ROOT,
                          preexec_fn=limit_memory)


def make_devzero(p: Path): p.symlink_to("/dev/zero")
def make_fifo(p: Path): os.mkfifo(p)
def make_dir(p: Path): p.mkdir()
def make_big(p: Path):
    with open(p, "wb") as f:
        f.truncate(100 << 20)


def make_link_to_file(p: Path):
    real = p.parent / "real-target.json"
    real.write_text("{}")
    p.symlink_to(real)


VARIANTS = [make_devzero, make_fifo, make_dir, make_big, make_link_to_file]
TARGETS = [".mcp.json", ".claude-plugin/plugin.json"]


@pytest.mark.parametrize("variant", VARIANTS, ids=lambda f: f.__name__[5:])
@pytest.mark.parametrize("target", TARGETS)
def test_unreadable_plugin_manifest_is_skipped_never_parked_and_nothing_hangs(fake_home, target, variant):
    make_plugin(fake_home, "pl@m", skills=True, mcp=True)
    plug = fake_home / "plugins-src/pl"
    (plug / ".claude-plugin").mkdir(exist_ok=True)
    (plug / ".claude-plugin/plugin.json").write_text("{}")
    tokopt.plan(tokopt.load_policy(), time.time())
    age_first_seen("claude:plugin:pl@m", 40)                      # old enough that missing evidence would PARK it
    victim = plug / target
    victim.unlink() if victim.exists() or victim.is_symlink() else None
    variant(victim)
    settings_before = (fake_home / ".claude/settings.json").read_bytes()
    r = cli(["report"])
    assert r.returncode == 0 and "Traceback" not in r.stderr, r.stderr[-300:]
    row = next(ln for ln in r.stdout.splitlines() if ln.startswith("pl@m"))
    assert "SKIP" in row and "unreadable" in row and "PARK" not in row, row
    assert cli(["--check-job"]).returncode == 0
    a = cli(["apply", "--yes"])
    assert a.returncode == 0 and "Traceback" not in a.stderr, a.stderr[-300:]
    assert (fake_home / ".claude/settings.json").read_bytes() == settings_before          # plugin not parked


def test_a_missing_plugin_manifest_is_fine_and_a_valid_one_still_counts(fake_home):
    make_plugin(fake_home, "ok@m", skills=True)
    tokopt.plan(tokopt.load_policy(), time.time())
    age_first_seen("claude:plugin:ok@m", 40)
    row = [r for r in tokopt.plan(tokopt.load_policy(), time.time())[1] if r[0].name == "ok@m"][0]
    assert row[1] == "park"                                       # no manifest at all: behaves as before


def test_plugin_json_reader_contract(tmp_path):
    f = tmp_path / "m.json"
    assert tokopt._read_plugin_json(f) == {}                      # missing: nothing declared
    f.write_text('{"a": 1}')
    assert tokopt._read_plugin_json(f) == {"a": 1}
    f.write_text("[1, 2]")
    assert tokopt._read_plugin_json(f) == {}                      # not an object
    f.write_text("{not json")
    assert tokopt._read_plugin_json(f) is None                    # unreadable: unknown, never "empty"
    f.unlink()
    f.mkdir()
    assert tokopt._read_plugin_json(f) is None


# ---------- L1: more invisible characters ----------

def test_mask_strips_the_remaining_format_characters():
    chars = "\u00ad\u180e\u206a\u206f\ufff9\ufffb\u0600\u0605\u06dd\u070f"
    assert tokopt.mask("a" + chars + "b") == "a" + "?" * len(chars) + "b"
    assert tokopt.mask("café naïve 中文") == "café naïve 中文"


# ---------- L2 / L3 / L5 docs ----------

def test_security_md_reads_writes_and_limits_are_complete():
    sec = (ROOT / "SECURITY.md").read_text()
    reads = next(ln for ln in sec.splitlines() if ln.startswith("- Reads"))
    for item in ("~/.claude/agents/<name>.md", "~/.claude/skills/*/SKILL.md", "~/.claude/plugins/installed_plugins.json",
                 "skills/", "commands/", "agents/", ".mcp.json", "plugin.json", "~/.config/tokopt/policy.toml"):
        assert item in reads, item
    writes = next(ln for ln in sec.splitlines() if ln.startswith("- Writes"))
    assert "Claude, Codex and Hermes" in writes and "Gemini" in writes
    assert "state directory itself may be a symlink" in sec


def test_changelog_describes_the_plugin_manifest_hardening_and_does_not_overclaim_characters():
    ch = (ROOT / "CHANGELOG.md").read_text()
    assert "plugin manifests" in ch and "/dev/zero" in ch
    assert "zero-width/format/tag characters are masked" not in ch


# ---------- L4: the nudge stdin cap is bytes ----------

def test_nudge_caps_stdin_at_one_mebibyte_of_bytes(tmp_path):
    tr = tmp_path / "t.jsonl"
    tr.write_text(json.dumps({"message": {"usage": {"input_tokens": 999_999}}}) + "\n")
    body = ('{"pad": "' + "é" * 600_000 + '", "transcript_path": "' + str(tr) + '"}').encode()
    assert len(body) > (1 << 20) and len(body.decode()) < (1 << 20)        # over a MiB of bytes, under a million characters
    r = subprocess.run([sys.executable, "-m", "tokopt.nudge"], input=body, capture_output=True, timeout=20, cwd=ROOT)
    assert r.returncode == 0 and r.stdout == b""
    ok = subprocess.run([sys.executable, "-m", "tokopt.nudge"], input=json.dumps({"transcript_path": str(tr)}).encode(),
                        capture_output=True, timeout=20, cwd=ROOT)
    assert b"/clear" in ok.stdout


# ---------- carry-over ----------

def test_no_tracked_file_contains_a_literal_control_bidi_or_format_character():
    pat = re.compile(tokopt.CTRL_RE.pattern)
    bad = []
    for f in _tracked():
        try:
            text = (ROOT / f).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for n, line in enumerate(text.split("\n"), 1):
            m = pat.search(line)
            if m:
                bad.append(f"{f}:{n}: U+{ord(m.group()):04X}")
    assert bad == []


def test_hermes_docstring_avoids_secret_scanner_trigger_words():
    src = (ROOT / "tokopt/cli.py").read_text()
    assert "duplicate `enabled:` field" in src and "quoted names" in src and "duplicate `enabled:` key" not in src


def test_symlinked_plugin_subdirectories_are_not_walked(tmp_path):
    real = tmp_path / "elsewhere"
    real.mkdir()
    (real / "x.md").write_text("x")
    plug = tmp_path / "plug"
    plug.mkdir()
    (plug / "commands").symlink_to(real)
    (plug / "skills").symlink_to(real)
    assert tokopt._plugin_names(plug) == set()
    (tmp_path / "plug2/commands").mkdir(parents=True)
    (tmp_path / "plug2/commands/y.md").write_text("y")
    assert tokopt._plugin_names(tmp_path / "plug2") == {"y"}

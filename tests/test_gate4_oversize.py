"""Gate 4 follow-up: an oversized line is skippable only when provably not evidence (no needle anywhere in it)."""
from __future__ import annotations

import json
import time
import tracemalloc

import pytest

from test_tokopt import add_server, age_first_seen, fake_home, iso, rows_by_name, tool_line, write_log  # noqa: F401
from tokopt import cli as tokopt


def compacted_line(size: int) -> str:
    """Real head structure of a Codex `compacted` record (message / role=user / input_text), ~size bytes of prose."""
    text = ("lorem ipsum dolor sit amet " * (size // 27 + 1))[:size]
    return json.dumps({"timestamp": iso(30), "type": "compacted", "payload": {"message": "", "replacement_history": [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}]}})


def codex_home(fake_home, lines):
    (fake_home / ".codex").mkdir()
    (fake_home / ".codex/config.toml").write_text('[mcp_servers.fixture_srv]\ncommand = "x"\n')
    write_log(fake_home / ".codex/sessions/2026/03/04/rollout-a.jsonl", lines, 40)
    tokopt.plan(tokopt.load_policy(), time.time())
    age_first_seen("codex:mcp:fixture_srv", 40)


def used_call() -> str:
    return json.dumps({"timestamp": iso(1), "type": "response_item", "payload": {
        "type": "function_call", "name": "js", "namespace": "mcp__fixture_srv", "arguments": "{}"}})


def test_real_shape_10mb_compacted_record_is_skipped_and_other_evidence_still_counts(fake_home):
    codex_home(fake_home, [compacted_line(10_000_000), compacted_line(9_700_000), used_call()])
    act, why = rows_by_name()["fixture_srv"]
    assert act == "keep" and "used 1d ago" in why, (act, why)
    sc = tokopt.scan_codex()
    assert sc is not None and not sc.incomplete and sc.unscanned == []
    assert "WARNING log scan incomplete" not in tokopt.report(tokopt.load_policy(), time.time(), [])


def test_oversized_line_without_needles_does_not_make_the_scan_incomplete(fake_home, monkeypatch):
    add_server(fake_home, "big")
    monkeypatch.setattr(tokopt, "MAX_LINE", 1000)
    write_log(fake_home / ".claude/projects/-p/c.jsonl", ["z" * 5000, tool_line("big", 1)], 0)
    act, why = rows_by_name()["big"]
    assert act == "keep" and "used 1d" in why
    assert not tokopt.scan_claude().incomplete  # type: ignore[union-attr]


def scan_bad(fake_home, line: str) -> list[str]:
    f = fake_home / "x.jsonl"
    write_log(f, [line], 0)
    spec = tokopt.CODEX_SCAN if ('"function_call"' in line or '"McpToolCall"' in line) else tokopt.CLAUDE_SCAN
    return tokopt.scan_logs("t", [f], spec)[2]


@pytest.mark.parametrize("needle", ["\"function_call\"", "\"McpToolCall\"", "\"tool_use\"", "<command-name>"])
def test_oversized_line_containing_a_needle_stays_fail_closed(fake_home, monkeypatch, needle):
    monkeypatch.setattr(tokopt, "MAX_LINE", 1000)
    assert scan_bad(fake_home, "z" * 3000 + needle + "z" * 3000) == [str(fake_home / "x.jsonl")]
    assert scan_bad(fake_home, "z" * 3000 + "zz" + "z" * 3000) == []


def test_needle_straddling_a_chunk_boundary_is_still_found(fake_home, monkeypatch):
    monkeypatch.setattr(tokopt, "MAX_LINE", 3000)
    monkeypatch.setattr(tokopt, "CHUNK", 1000, raising=False)
    for off in range(986, 1000):  # `"function_call"` (15 bytes) crosses the first 1000-byte read at every split point
        assert scan_bad(fake_home, "x" * off + '"function_call"' + "y" * 6000), off


def test_forty_mb_line_streams_in_bounded_memory(fake_home):
    add_server(fake_home, "big")
    f = fake_home / ".claude/projects/-p/huge.jsonl"
    with open(f, "wb") as fh:
        fh.write(b'{"pad":"')
        for _ in range(40):
            fh.write(b"x" * (1 << 20))
        fh.write(b'"}\n' + tool_line("big", 1).encode() + b"\n")
    tracemalloc.start()
    try:
        act, why = rows_by_name()["big"]
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert act == "keep" and "used 1d" in why
    assert peak < 24 * (1 << 20), f"peak {peak >> 20} MiB"

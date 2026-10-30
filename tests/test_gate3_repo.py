"""Gate 3: public-repo naming, docs and security policy (items 14, 16, 17)."""
from __future__ import annotations

import re

from test_tokopt import ROOT, _tracked  # noqa: F401

VENDOR = re.compile("|".join(["anv" + "il", "pony" + "tail", r"\baa_", "agent" + "-memory", r"\brea\b"]), re.I)


def test_no_vendor_specific_names_in_tracked_files():
    hits = []
    for f in _tracked():
        p = ROOT / f
        if p.is_file() and (m := VENDOR.search(p.read_text(errors="replace"))):
            hits.append(f"{f}: {m.group(0)}")
    assert hits == []


def test_security_policy_says_how_to_report_privately_and_what_the_tool_touches():
    t = (ROOT / "SECURITY.md").read_text().lower()
    for phrase in ("report", "privately", "security advisor", "what tokopt touches", "~/.claude.json", "never sends"):
        assert phrase in t, phrase


def test_readme_documents_behaviours_the_gates_flagged():
    r = (ROOT / "README.md").read_text().lower()
    for phrase in ("explicit `enabled = true`", "key order", "protect", "no evidence: skipped", "gemini", "log rotation",
                   "contributing", "claude code sessions only", "could take `model: haiku`", "incomplete"):
        assert phrase in r, phrase
    assert "byte-exact only" not in r  # Hermes has a semantic inverse

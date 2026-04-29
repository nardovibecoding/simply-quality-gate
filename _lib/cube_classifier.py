"""Cube classifier — rule-based, project-axis domain routing for recall.

Classifies a user prompt into one of 6 cubes (or "general" fallback) so that
recall RRF weights can be biased per-domain. Pure function, no I/O.

Per CLAUDE.md HARD RULE "Rule-based > LLM for local classifiers".
Per ship.md "Heuristic validation gate" — held-out validation required
before phase-close (see ~/.ship/recall-cube-classifier/experiments/).

Usage:
    from _lib.cube_classifier import classify
    cube = classify("fix the kalshi wedge")  # -> "pm-bot"
"""
from __future__ import annotations

import re
from typing import Final

# Word-boundary regexes per cube. Every keyword wrapped in \b...\b to avoid
# substring false-positives (e.g. "hel" matching "hello").
# Tokens chosen to be project-distinctive (no generic "bot", "data", "code").
CUBE_KEYWORDS: Final[dict[str, list[str]]] = {
    "pm-bot": [
        r"\bkalshi\b", r"\bpolymarket\b", r"\bmanifold\b",
        r"\bhel\b", r"\blondon\b", r"\bpm[- ]?bot\b",
        r"\bsignal[- ]?trace\b", r"\btrade[- ]?journal\b",
        r"\bbasket[- ]?atomicity\b", r"\bfast[- ]?loop\b",
        r"\bclob[- ]?stream\b", r"\bwhale[- ]?scan\b",
        r"\bprediction[- ]?market", r"\borderhash\b",
        r"\bkmm\b", r"\bsy[- ]?replies\b",
    ],
    "vibe-island": [
        r"\bvibe[- ]?island\b", r"\bvibeisland\b",
        r"\bswiftui\b", r"\bxctest\b", r"\bsnapshot[- ]?test",
        r"\bmac[- ]?app\b", r"\bdashboard[- ]?mac\b",
        r"\b\.app\b", r"\blaunchagent\b",
        r"\bnard[- ]?cli\b", r"\bnardostick\b",
    ],
    "dagou": [
        r"\bdagou\b", r"\bkol\b", r"\bbsc\b", r"\bbnb\b",
        r"\bs5\b", r"\bwallet[- ]?harvester\b",
        r"\bspoofer[- ]?discover", r"\bbuild[- ]?unified[- ]?profile\b",
    ],
    "codex": [
        r"\bcodex\b", r"\bcodex[- ]?hooks\b",
        r"\bcodex[- ]?migration\b", r"\bcodex[- ]?prep\b",
        r"\bcodex[- ]?cutover\b", r"\bgithooks\b",
    ],
    "claude-harness": [
        r"\bclaude[- ]?code\b", r"\bclaude\.md\b", r"\bCLAUDE\.md\b",
        r"\bsemantic[- ]?router\b", r"\bsettings\.json\b",
        r"\bskill[- ]?loader\b", r"\bbigd\b", r"\bdaemons?\b",
        r"\b/ship\b", r"\b/debug\b", r"\b/recall\b", r"\b/snap\b",
        r"\bUserPromptSubmit\b", r"\bPreToolUse\b", r"\bPostToolUse\b",
        r"\bevidence[- ]?guard\b", r"\bship\.md\b",
    ],
}

# Compiled once at import time (regex objects keyed by cube)
_COMPILED: dict[str, list[re.Pattern[str]]] = {
    cube: [re.compile(p, re.IGNORECASE) for p in patterns]
    for cube, patterns in CUBE_KEYWORDS.items()
}

MIN_SCORE_TO_WIN = 1  # need ≥1 hit to escape "general"


def score(prompt: str) -> dict[str, int]:
    """Return per-cube hit counts for the given prompt."""
    if not prompt:
        return {cube: 0 for cube in CUBE_KEYWORDS}
    return {
        cube: sum(1 for p in patterns if p.search(prompt))
        for cube, patterns in _COMPILED.items()
    }


def classify(prompt: str) -> str:
    """Classify prompt into a single cube, fallback "general".

    Tie-breaker: highest score wins; ties resolved by CUBE_KEYWORDS dict order
    (pm-bot > vibe-island > dagou > codex > claude-harness). Empty/weak → general.
    """
    scores = score(prompt)
    best_cube = max(scores, key=lambda c: scores[c])
    if scores[best_cube] < MIN_SCORE_TO_WIN:
        return "general"
    return best_cube


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        q = " ".join(sys.argv[1:])
        print(f"cube={classify(q)}  scores={score(q)}")
    else:
        # Tautological author-tested smoke (per ship.md ban: explicitly labeled)
        # held-out validation runs in S4
        cases = [
            ("fix the kalshi wedge", "pm-bot"),
            ("snapshot test for the vibe-island app", "vibe-island"),
            ("dagou whale scan results", "dagou"),
            ("codex migration p0 manifest", "codex"),
            ("update CLAUDE.md routing", "claude-harness"),
            ("what's the weather", "general"),
        ]
        ok = sum(classify(q) == want for q, want in cases)
        print(f"author-tested {ok}/{len(cases)} (TAUTOLOGICAL — held-out S4 pending)")

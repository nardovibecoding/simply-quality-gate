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

# Per-cube keyword tiers. ANCHOR = project-name or sole-owner token (weight 2),
# CONTEXT = supporting tokens (weight 1). Anchor matches dominate ties.
# Word-boundaries on every regex to avoid substring FPs ("hel" matching "hello").
CUBE_ANCHORS: Final[dict[str, list[str]]] = {
    "pm-bot":         [r"\bkalshi\b", r"\bpolymarket\b", r"\bmanifold\b", r"\bpm[- ]?bot\b",
                       r"\bprediction[- ]?market", r"\bpm[- ]session\b", r"\bpm[- ]pipeline\b",
                       r"\bpm[- ]eval\b", r"\bpm[- ]strategy", r"\bpm[- ]strategies\b",
                       r"\bpm[- ]dashboard\b", r"\bpm[- ]orderbook\b", r"\bpm[- ]epoch\b",
                       r"\bpm[- ]launchd\b", r"\bpm[- ]exchange\b", r"\bpm[- ]whale\b"],
    "vibe-island":    [r"\bvibe[- ]?island\b", r"\bvibeisland\b"],
    "dagou":          [r"\bdagou\b"],
    "codex":          [r"\bcodex\b"],
    "claude-harness": [r"\bclaude[- ]?code\b", r"\bclaude\.md\b", r"\bsemantic[- ]?router\b",
                       r"\bsettings\.json\b", r"\bship\.md\b", r"\bbigd\b",
                       r"\b/ship\b", r"\b/debug\b", r"\b/recall\b", r"\b/snap\b", r"\b/daemons\b"],
}

CUBE_CONTEXT: Final[dict[str, list[str]]] = {
    "pm-bot": [
        r"\bhel\b", r"\blondon\b", r"\bsignal[- ]?trace\b", r"\btrade[- ]?journal\b",
        r"\bbasket[- ]?atomicity\b", r"\bfast[- ]?loop\b", r"\bclob[- ]?stream\b",
        r"\bwhale[- ]?scan\b", r"\borderhash\b", r"\bkmm\b", r"\bsy[- ]?replies\b",
    ],
    "vibe-island": [
        r"\bswiftui\b", r"\bxctest\b", r"\bsnapshot[- ]?test", r"\bmac[- ]?app\b",
        r"\bdashboard[- ]?mac\b", r"\b\.app\b", r"\blaunchagent\b",
        r"\bnard[- ]?cli\b", r"\bnardostick\b",
    ],
    "dagou": [
        r"\bkol\b", r"\bbsc\b", r"\bbnb\b", r"\bs5\b", r"\bwallet[- ]?harvester\b",
        r"\bspoofer[- ]?discover", r"\bbuild[- ]?unified[- ]?profile\b",
    ],
    "codex": [
        r"\bcodex[- ]?hooks\b", r"\bcodex[- ]?migration\b", r"\bcodex[- ]?prep\b",
        r"\bcodex[- ]?cutover\b", r"\bgithooks\b",
    ],
    "claude-harness": [
        r"\bskill[- ]?loader\b", r"\bUserPromptSubmit\b", r"\bPreToolUse\b",
        r"\bPostToolUse\b", r"\bevidence[- ]?guard\b",
        r"\blaunchagent[- ]?dup\b", r"\bcontext[- ]?50[- ]?check\b",
    ],
}

ANCHOR_WEIGHT: Final[int] = 2
CONTEXT_WEIGHT: Final[int] = 1
MIN_SCORE_TO_WIN: Final[int] = 1  # ≥1 context hit OR ≥1 anchor hit escapes "general"

_ANCHOR_RE: dict[str, list[re.Pattern[str]]] = {
    c: [re.compile(p, re.IGNORECASE) for p in pats] for c, pats in CUBE_ANCHORS.items()
}
_CONTEXT_RE: dict[str, list[re.Pattern[str]]] = {
    c: [re.compile(p, re.IGNORECASE) for p in pats] for c, pats in CUBE_CONTEXT.items()
}

# Public alias kept for back-compat with anyone reading CUBE_KEYWORDS
CUBE_KEYWORDS: Final[dict[str, list[str]]] = {
    c: CUBE_ANCHORS[c] + CUBE_CONTEXT[c] for c in CUBE_ANCHORS
}


def score(prompt: str) -> dict[str, int]:
    """Per-cube weighted hit count. Anchor=2, context=1."""
    if not prompt:
        return {cube: 0 for cube in CUBE_ANCHORS}
    out: dict[str, int] = {}
    for cube in CUBE_ANCHORS:
        a = sum(ANCHOR_WEIGHT for p in _ANCHOR_RE[cube] if p.search(prompt))
        c = sum(CONTEXT_WEIGHT for p in _CONTEXT_RE[cube] if p.search(prompt))
        out[cube] = a + c
    return out


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

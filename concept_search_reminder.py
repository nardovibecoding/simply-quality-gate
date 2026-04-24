#!/usr/bin/env python3
# @bigd-hook-meta
# name: concept_search_reminder
# fires_on: UserPromptSubmit
# relevant_intents: []
# irrelevant_intents: []
# cost_score: 1
# always_fire: true
"""
concept_search_reminder.py -- UserPromptSubmit hook (additive).

Trigger: user references prior context I might struggle to find.
Action: inject reminder to search by convention + concept synonyms,
NOT by the literal label the user just used.

Lesson source: ~/NardoWorld/lessons/literal-vs-concept-search-2026-04-23.md
Two recurrences in one session (2026-04-23) — L4 layer hunt + phase4_scope.md hunt.
"""

from __future__ import annotations

import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from _semantic_router import should_fire

TRIGGER_PHRASES = [
    "you told me",
    "i told you",
    "we discussed",
    "yesterday you",
    "yesterday we",
    "remember when",
    "we have a doc",
    "i remember you said",
    "you said yesterday",
    "remember i said",
    "remember you",
    "i remmebner",  # bernard's typo
    "i remmeber",
    "i rmemeber",
    "u told me",
    "u said",
]

REMINDER = """\
[concept-search reminder] User referenced prior context. Before grepping the literal label they used:
1. ls the likely directory for actual naming convention (`ls ~/NardoWorld/meta/`, `ls ~/.claude/skills/`, etc.)
2. Generate ≥3 concept synonyms — what would the original doc/conversation have called this? The user's current vocabulary may differ.
3. Stop after 2 failed greps and ask for a specific phrase, OR check hub_nodes.json / graph_index.json.
4. Never claim "no evidence" after only literal-label searches.
Lesson: ~/NardoWorld/lessons/literal-vs-concept-search-2026-04-23.md
"""


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read())
    except Exception:
        sys.exit(0)

    prompt = (payload.get("prompt") or "").lower()
    if not any(phrase in prompt for phrase in TRIGGER_PHRASES):
        sys.exit(0)

    output = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": REMINDER,
        }
    }
    print(json.dumps(output))
    sys.exit(0)


if __name__ == "__main__":
    _raw_stdin = sys.stdin.read()
    try:
        _prompt = json.loads(_raw_stdin).get("prompt", "")
    except Exception:
        _prompt = ""
    sys.stdin = io.StringIO(_raw_stdin)
    if not should_fire(__file__, _prompt):
        print("{}")
        sys.exit(0)
    main()

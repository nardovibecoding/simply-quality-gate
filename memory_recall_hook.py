#!/usr/bin/env python3
# @bigd-hook-meta
# name: memory_recall_hook
# fires_on: UserPromptSubmit
# relevant_intents: []
# irrelevant_intents: []
# cost_score: 1
# always_fire: true
"""UserPromptSubmit hook: remind Claude to check memory for recall-type questions."""
import io
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(__file__))
from _semantic_router import should_fire

RECALL_PATTERNS = re.compile(
    r"("
    r"are we using|do we (have|use)|did we|have we (tried|used|installed|set up)"
    r"|remember when|do you remember|check.*(memory|if we)"
    r"|already (have|using|installed|set up|tried)"
    r"|we using.+already|we have.+already"
    r"|using this already|have this already"
    r"|didn.t we|wasn.t that|weren.t we"
    r"|last time we|before we|previously"
    r")",
    re.IGNORECASE,
)


def main():
    try:
        hook_input = json.load(sys.stdin)
        prompt = hook_input.get("prompt", "")
    except (json.JSONDecodeError, EOFError):
        print("{}")
        return

    if RECALL_PATTERNS.search(prompt):
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": (
                    "\u26a0\ufe0f RECALL QUESTION DETECTED. "
                    "Check ALL memory dirs BEFORE answering:\n"
                    "  1. Project: ~/.claude/projects/-Users-bernard-polymarket-bot/memory/\n"
                    "  2. Home: ~/.claude/projects/-Users-bernard/memory/\n"
                    "Do NOT say 'not using' or 'don't have' without checking memory first."
                )
            }
        }))
    else:
        print("{}")


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

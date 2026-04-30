#!/usr/bin/env python3
# @bigd-hook-meta
# name: gmail_humanizer
# fires_on: PostToolUse
# relevant_intents: [telegram, docx]
# irrelevant_intents: [bigd, pm, git, code, vps, sync, memory, debug]
# cost_score: 1
# always_fire: false
"""PostToolUse hook: remind to run content-humanizer after creating Gmail drafts."""
import io
import json
import os
import sys


def main():
    try:
        input_data = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        print("{}")
        return

    tool_name = input_data.get("tool_name", "")

    if tool_name != "mcp__claude_ai_Gmail__gmail_create_draft":
        print("{}")
        return

    print(json.dumps({
        "systemMessage": (
            "📝 **Gmail draft created.** Run /tweet humanize on the draft body before sending. "
            "Remove AI patterns: delve, crucial, leverage, navigate, robust. "
            "Add personality and real voice."
        )
    }))


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(__file__))
    _raw = sys.stdin.read()
    try:
        _prompt = json.loads(_raw).get("prompt", "") if _raw else ""
    except Exception:
        _prompt = ""
    from _semantic_router import should_fire
    if not should_fire(__file__, _prompt):
        print("{}")
        sys.exit(0)
    sys.stdin = io.StringIO(_raw)
    main()

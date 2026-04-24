#!/usr/bin/env python3
# @bigd-hook-meta
# name: memory_scan_status
# fires_on: UserPromptSubmit
# relevant_intents: [memory, meta, bigd]
# irrelevant_intents: [docx, x_tweet, git, telegram, vps, pm]
# cost_score: 1
# always_fire: false
"""UserPromptSubmit hook: surface memory_write_scan hits in daily status.

Reads ~/.claude/logs/memory_write_scan.log. If any hits in last 7 days,
emits a compact additionalContext line so you don't forget to review.
"""
import io
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from _semantic_router import should_fire

LOG_FILE = Path.home() / ".claude" / "logs" / "memory_write_scan.log"
WINDOW_DAYS = 7


def main():
    # Consume stdin to keep hook well-behaved even if we emit nothing.
    try:
        json.load(sys.stdin)
    except Exception:
        pass

    if not LOG_FILE.exists():
        print("{}")
        return

    cutoff = datetime.now() - timedelta(days=WINDOW_DAYS)
    recent_hits = 0
    recent_files = set()

    try:
        for line in LOG_FILE.read_text().splitlines():
            # Format: "ISO_TS | tool | path | CATEGORY | snippet"
            parts = line.split(" | ", 4)
            if len(parts) < 4:
                continue
            try:
                ts = datetime.fromisoformat(parts[0])
            except ValueError:
                continue
            if ts < cutoff:
                continue
            recent_hits += 1
            recent_files.add(parts[2])
    except OSError:
        print("{}")
        return

    if recent_hits == 0:
        print("{}")
        return

    msg = (
        f"⚠️ Memory scan: {recent_hits} injection pattern hit(s) across "
        f"{len(recent_files)} file(s) in last {WINDOW_DAYS}d. "
        f"Review: ~/.claude/logs/memory_write_scan.log"
    )
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": msg,
        }
    }))


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

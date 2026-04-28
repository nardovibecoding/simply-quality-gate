#!/usr/bin/env python3
# @bigd-hook-meta
# name: pivot_detect
# fires_on: UserPromptSubmit
# always_fire: true
# cost_score: 1
"""Pivot-detection hook — runs on every UserPromptSubmit, logs HIGH-tier pivots.

Logs to ~/.claude/state/pivot-log.jsonl. /s --pivot or manual review of this
log feeds survival guide §0.0 snapshot updates.

Silent on LOG/LOW; logs MED+HIGH; never blocks.
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _safe_hook import safe_run

sys.path.insert(0, str(Path.home() / ".claude" / "scripts" / "pivot"))
from detect import detect

LOG_PATH = Path.home() / ".claude" / "state" / "pivot-log.jsonl"
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)


def main() -> None:
    payload = json.load(sys.stdin)
    prompt = payload.get("prompt", "")
    if not prompt:
        print("{}")
        return

    result = detect(prompt)
    tier = result.get("tier", "LOG")
    if tier in ("HIGH", "MED"):
        entry = {
            "ts": int(time.time()),
            "tier": tier,
            "matches": result.get("matches", []),
            "reasoning": result.get("reasoning", ""),
            "session_id": payload.get("session_id", ""),
            "prompt_excerpt": prompt[:200],
        }
        with open(LOG_PATH, "a") as f:
            f.write(json.dumps(entry) + "\n")

    print("{}")


if __name__ == "__main__":
    safe_run(main, "pivot_detect")

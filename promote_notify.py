#!/usr/bin/env python3
"""UserPromptSubmit hook: show GREEN notification when promote_check found promotion candidates.

Reads ~/.claude/promote_pending.json. Injects additionalContext with green-coded notification.
User confirms by saying 'approve promotions' or 'skip promotions'.
"""
import json
import sys
from datetime import datetime
from pathlib import Path

PENDING_FILE = Path.home() / ".claude/promote_pending.json"
PROMOTE_SCRIPT = Path.home() / "llm-wiki-stack/promote/promote_lessons.py"
MAX_AGE_HOURS = 48  # stale if >48h old


def main():
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        data = {}

    if not PENDING_FILE.exists():
        sys.stdout.write("{}\n")
        return

    try:
        pending = json.loads(PENDING_FILE.read_text())
    except Exception:
        sys.stdout.write("{}\n")
        return

    # Check age
    ts_str = pending.get("ts", "")
    if ts_str:
        try:
            age_h = (datetime.now() - datetime.fromisoformat(ts_str)).total_seconds() / 3600
            if age_h > MAX_AGE_HOURS:
                PENDING_FILE.unlink(missing_ok=True)
                sys.stdout.write("{}\n")
                return
        except Exception:
            pass

    # Check user prompt for approval/skip
    prompt = data.get("prompt", "").lower()
    if "approve promotion" in prompt or "yes promotion" in prompt or "confirm promotion" in prompt:
        # Claude will handle running --apply; just pass context
        promoted = pending.get("promoted", [])
        lines = [f"🟢 PROMOTE APPROVED — running promote_lessons.py --apply --write-rules"]
        lines.append(f"Lessons to promote ({len(promoted)}):")
        for r in promoted:
            lines.append(f"  • {r.get('title','?')} → \"{r.get('rule_text','?')}\"")
        lines.append(f"\nScript: python3 {PROMOTE_SCRIPT} --apply --write-rules")
        lines.append("Run it now, then commit build_system_prompt.py.")
        sys.stdout.write(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": "\n".join(lines)
            }
        }) + "\n")
        PENDING_FILE.unlink(missing_ok=True)
        return

    if "skip promotion" in prompt or "no promotion" in prompt or "reject promotion" in prompt:
        PENDING_FILE.unlink(missing_ok=True)
        sys.stdout.write(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": "🟢 Promote skipped. Pending file cleared."
            }
        }) + "\n")
        return

    # Show notification
    promoted = pending.get("promoted", [])
    total = pending.get("total", len(promoted))
    ts = pending.get("ts", "?")[:16].replace("T", " ")

    titles = "\n".join(f"  • {r.get('title','?')}" for r in promoted[:4])
    if len(promoted) > 4:
        titles += f"\n  ...+{len(promoted)-4} more"

    msg = (
        f"🟢🟢🟢 PROMOTE LESSONS READY ({ts})\n"
        f"{len(promoted)}/{total} lessons passed committee vote:\n{titles}\n"
        f"Say 'approve promotions' to write to system prompt, or 'skip promotions' to dismiss."
    )

    sys.stdout.write(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": msg
        }
    }) + "\n")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# @bigd-hook-meta
# name: state_change_reminder
# fires_on: UserPromptSubmit
# relevant_intents: []
# irrelevant_intents: []
# cost_score: 1
# always_fire: true
"""UserPromptSubmit hook: if state-change marker exists, inject reminder.

Forces Claude to update LATEST STATE block of affected memory/wiki files
before responding to user. Clears marker after consumption (Claude must
act in same turn).
"""
import json
import sys
from pathlib import Path

MARKER = Path("/tmp/state_change_pending.json")


def main():
    if not MARKER.exists():
        return 0

    try:
        entries = json.loads(MARKER.read_text())
        if not isinstance(entries, list) or not entries:
            MARKER.unlink(missing_ok=True)
            return 0
    except Exception:
        MARKER.unlink(missing_ok=True)
        return 0

    files = sorted({e.get("file", "") for e in entries if e.get("file")})
    if not files:
        MARKER.unlink(missing_ok=True)
        return 0

    files_list = "\n".join(f"  - {f}" for f in files)
    msg = (
        "STATE-CHANGE ENFORCEMENT:\n"
        f"Files edited that impact live state:\n{files_list}\n"
        "Before responding to user, update the LATEST STATE block of the "
        "corresponding memory/wiki file (glob by system name in "
        "~/NardoWorld/ and ~/.claude/projects/-Users-bernard/memory/).\n"
        "Spec: ~/NardoWorld/meta/latest_state_spec.md\n"
        "Rules: tag frontmatter has_state: true, state_updated: today, "
        "move old state to ## State history as dated delta. "
        "Include runnable verify: cmd."
    )

    out = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": msg,
        }
    }
    print(json.dumps(out))

    # Clear marker — Claude is expected to act this turn.
    MARKER.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

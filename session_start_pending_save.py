#!/usr/bin/env python3
"""SessionStart/UserPromptSubmit: surface pending deferred-saves.

Reads /tmp/pending_saves/*.json written by session_end_logger.sh.
On first user prompt of a session, injects additionalContext nudging
Claude to spawn a background Agent that saves each pending session.

Uses /tmp/claude_pending_save_injected as a per-session done marker so
the nudge fires exactly once per session.
"""
import json
import sys
from pathlib import Path

QUEUE = Path("/tmp/pending_saves")
DONE_MARKER = Path("/tmp/claude_pending_save_injected")


def main():
    try:
        event = json.load(sys.stdin)
    except Exception:
        event = {}

    if DONE_MARKER.exists():
        print("{}")
        return

    if not QUEUE.exists():
        print("{}")
        return

    markers = sorted(QUEUE.glob("*.json"))
    if not markers:
        print("{}")
        return

    # Build summary of pending saves
    lines = []
    for m in markers[:10]:
        try:
            d = json.loads(m.read_text())
            sid = d.get("session_id", "?")[:8]
            reason = d.get("reason", "?")
            tp = d.get("transcript_path", "")
            lines.append(f"- session={sid} reason={reason} transcript={tp} marker={m}")
        except Exception:
            continue

    if not lines:
        print("{}")
        return

    nudge = (
        f"DEFERRED SAVE QUEUE — {len(lines)} prior session(s) ended without /s.\n"
        f"{chr(10).join(lines)}\n\n"
        "BEFORE responding to the user, spawn ONE background Agent (model=haiku, "
        "run_in_background=true) with this prompt:\n\n"
        "\"For each pending marker in /tmp/pending_saves/*.json: read transcript JSONL, "
        "infer 1-3 word topic slug, check if /Users/bernard/.claude/projects/-Users-bernard/memory/convo_YYYY-MM-DD_<slug>.md "
        "already exists (skip if yes), else write convo file using the structured template "
        "(Active Task / Resolved / Pending User Asks / Remaining Work / State Deltas / Pivots), "
        "3KB cap. Delete marker after save. Report N saved, N skipped.\"\n\n"
        "After spawning, also: touch /tmp/claude_pending_save_injected (prevents re-nudge this session). "
        "Then respond to the user normally."
    )

    out = {
        "hookSpecificOutput": {
            "hookEventName": event.get("hook_event_name", "UserPromptSubmit"),
            "additionalContext": nudge,
        }
    }
    print(json.dumps(out))


if __name__ == "__main__":
    main()

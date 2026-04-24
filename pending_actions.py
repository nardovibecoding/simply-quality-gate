#!/usr/bin/env python3
# @bigd-hook-meta
# name: pending_actions
# fires_on: UserPromptSubmit
# relevant_intents: []
# irrelevant_intents: []
# cost_score: 1
# always_fire: true
"""UserPromptSubmit hook: remind about pending plans/actions/questions from Claude.

Claude writes to /tmp/claude_pending_actions.json to register items.
This hook reads the file, expires old items, and injects a reminder.

File format:
[
  {"type": "plan|action|question", "summary": "...", "created": "ISO8601", "status": "pending"},
  ...
]

Claude can also mark items done by writing status="done" or removing them.
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

PENDING_FILE = Path("/tmp/claude_pending_actions.json")
EXPIRE_HOURS = 6


def load_actions() -> list:
    if not PENDING_FILE.exists():
        return []
    try:
        data = json.loads(PENDING_FILE.read_text())
        if not isinstance(data, list):
            return []
        return data
    except (json.JSONDecodeError, OSError):
        return []


def save_actions(actions: list) -> None:
    try:
        PENDING_FILE.write_text(json.dumps(actions, indent=2))
    except OSError:
        pass


STOPWORDS = {
    "the", "and", "for", "with", "from", "into", "that", "this", "then",
    "all", "run", "add", "use", "get", "set", "fix", "new", "not", "but",
    "build", "make", "check", "update", "fetch", "merge", "cache", "market",
    "markets", "strategies", "strategy", "scan", "data", "file", "code",
}


def auto_clear_completed(actions: list) -> list:
    """Check recent convo summaries for evidence that pending items were completed.

    Uses 4+ char non-stopwords and requires 60%+ match ratio to avoid false positives.
    Checks last 2 days of summaries to handle cross-midnight items.
    """
    from pathlib import Path
    memory_dir = Path.home() / ".claude/projects/-Users-bernard/memory"
    if not memory_dir.exists():
        return actions
    # Check today + yesterday to handle cross-midnight
    dates = [
        datetime.now().strftime("%Y-%m-%d"),
        (datetime.now() - __import__('datetime').timedelta(days=1)).strftime("%Y-%m-%d"),
    ]
    corpus = ""
    for day in dates:
        for f in sorted(memory_dir.glob(f"convo_{day}_*.md"), key=lambda p: p.stat().st_mtime, reverse=True)[:10]:
            try:
                corpus += f.read_text()[:2000].lower() + "\n"
            except OSError:
                continue
    if not corpus:
        return actions
    updated = []
    for item in actions:
        summary = item.get("summary", "").lower()
        # Extract meaningful words: 4+ chars, not stopwords
        words = [w for w in summary.split() if len(w) >= 4 and w not in STOPWORDS]
        if len(words) < 2:
            # Too few distinctive words to match reliably — keep item
            updated.append(item)
            continue
        matches = sum(1 for w in words if w in corpus)
        ratio = matches / len(words)
        if ratio >= 0.6:
            item["status"] = "done"
        updated.append(item)
    return updated


def main():
    try:
        json.load(sys.stdin)  # consume hook input (prompt etc.)
    except (json.JSONDecodeError, EOFError):
        pass

    actions = load_actions()
    actions = auto_clear_completed(actions)
    now = datetime.now(timezone.utc)

    # Filter: keep only pending items that haven't expired
    kept = []
    changed = False
    for item in actions:
        if not isinstance(item, dict):
            changed = True
            continue
        status = item.get("status", "pending")
        if status != "pending":
            changed = True
            continue  # drop done/cancelled items
        created_str = item.get("created", "")
        if created_str:
            try:
                created = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                age_hours = (now - created).total_seconds() / 3600
                if age_hours > EXPIRE_HOURS:
                    changed = True
                    continue  # expired
            except ValueError:
                pass  # keep if unparseable
        kept.append(item)

    if changed:
        save_actions(kept)

    if not kept:
        print("{}")
        return

    # Format reminder
    type_icons = {"plan": "📋", "action": "⚡", "question": "❓"}
    parts = []
    for i, item in enumerate(kept[:5], 1):
        icon = type_icons.get(item.get("type", "action"), "📌")
        summary = item.get("summary", "?")
        parts.append(f"({i}) {icon} {summary}")

    reminder = "Pending: " + " ".join(parts)
    if len(kept) > 5:
        reminder += f" ... +{len(kept) - 5} more"

    output = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": reminder
        }
    }
    print(json.dumps(output))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""UserPromptSubmit hook: inject context after /clear or session start.

Fires when context usage is very low (<8%). Injects:
1. Today's librarian-log entries
2. Latest convo summary from memory
3. Pending unanswered questions

Uses a done marker to avoid re-injecting every message. Resets when context
goes above 15% (mid-session), so it fires again after next /clear.
"""
import json
import sys
import time
from datetime import datetime
from pathlib import Path

STATUSLINE_JSON = Path("/tmp/claude_statusline.json")
LIBRARIAN_LOG = Path.home() / "NardoWorld/meta/librarian-log.md"
MEMORY_DIR = Path.home() / ".claude/projects/-Users-bernard/memory"
PENDING_FILE = Path("/tmp/claude_pending_questions.json")
DONE_MARKER = Path("/tmp/claude_continuity_done")
TRACKER_FILE = Path("/tmp/claude_agent_tracker.json")
LOW_THRESHOLD = 8
RESET_THRESHOLD = 15


def get_today_entries(log_path: Path) -> str:
    if not log_path.exists():
        return ""
    today = datetime.now().strftime("%Y-%m-%d")
    lines = log_path.read_text().splitlines()
    entries = []
    capturing = False
    for line in lines:
        if line.startswith("## ") and today in line:
            capturing = True
            entries.append(line)
        elif line.startswith("## ") and capturing:
            break
        elif capturing:
            entries.append(line)
    return "\n".join(entries).strip()


def get_convo_summaries() -> str:
    if not MEMORY_DIR.exists():
        return ""
    today = datetime.now().strftime("%Y-%m-%d")
    files = sorted(MEMORY_DIR.glob(f"convo_{today}_*.md"), key=lambda f: f.stat().st_mtime)
    if not files:
        return ""
    parts = []
    total = 0
    for f in files:
        try:
            text = f.read_text().strip()
            if text.startswith("---"):
                chunks = text.split("---", 2)
                if len(chunks) >= 3:
                    text = chunks[2].strip()
            if total + len(text) > 4000:
                text = text[:max(200, 4000 - total)] + "\n... (truncated)"
            parts.append(f"[{f.stem}]\n{text}")
            total += len(text)
            if total >= 4000:
                break
        except OSError:
            continue
    if not parts:
        return ""
    return "Today's session summaries:\n\n" + "\n\n---\n\n".join(parts)


def get_pending_questions() -> str:
    if not PENDING_FILE.exists():
        return ""
    try:
        pending = json.loads(PENDING_FILE.read_text())
        old = [q for q in pending if q.get("turns", 0) >= 2]
        if not old:
            return ""
        bullets = "\n".join(f"  - {q['text']}" for q in old[:3])
        return f"Unanswered questions carrying over:\n{bullets}"
    except (json.JSONDecodeError, OSError):
        return ""


def get_pending_memory_items() -> str:
    """Read pending items from pending_actions.json (single source of truth).

    Previously grepped all memory files for 'pending' keyword — produced stale
    false positives from point-in-time convo summaries. Now uses the structured
    pending_actions.json that hooks actively maintain.
    """
    actions_file = Path("/tmp/claude_pending_actions.json")
    if not actions_file.exists():
        return ""
    try:
        data = json.loads(actions_file.read_text())
        active = [a for a in data if a.get("status") == "pending"]
        if not active:
            return ""
        bullets = "\n".join(f"  - {a['summary']}" for a in active[:5])
        return f"Pending actions:\n{bullets}"
    except (json.JSONDecodeError, OSError):
        return ""


def get_active_agents() -> str:
    """Get running background agents from tracker."""
    try:
        if not TRACKER_FILE.exists():
            return ""
        data = json.loads(TRACKER_FILE.read_text())
        active = [
            a for a in data.get("agents", [])
            if a.get("status") == "running"
            and time.time() - a.get("started", 0) < 7200
        ]
        if not active:
            return ""
        descs = ", ".join(a["description"] for a in active)
        return f"Agents still running: {descs}"
    except Exception:
        return ""


def main():
    try:
        json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        pass

    if not STATUSLINE_JSON.exists():
        print("{}")
        return

    try:
        data = json.loads(STATUSLINE_JSON.read_text())
        ctx_pct = float(data.get("context_window", {}).get("used_percentage", 0))
    except (ValueError, OSError, KeyError):
        print("{}")
        return

    if ctx_pct >= RESET_THRESHOLD:
        DONE_MARKER.unlink(missing_ok=True)
        print("{}")
        return

    if ctx_pct < LOW_THRESHOLD and DONE_MARKER.exists():
        print("{}")
        return

    if ctx_pct < LOW_THRESHOLD:
        sections = []

        today_log = get_today_entries(LIBRARIAN_LOG)
        if today_log:
            if len(today_log) > 800:
                today_log = today_log[:800] + "\n... (truncated)"
            sections.append("Today's filed work (librarian-log):\n" + today_log)

        convo = get_convo_summaries()
        if convo:
            sections.append(convo)

        pending = get_pending_questions()
        if pending:
            sections.append(pending)

        pending_mem = get_pending_memory_items()
        if pending_mem:
            sections.append(pending_mem)

        active_agents = get_active_agents()
        if active_agents:
            sections.append(active_agents)

        if not sections:
            DONE_MARKER.write_text("no-entries")
            print("{}")
            return

        DONE_MARKER.write_text("injected")
        context = "SESSION CONTINUITY (post-/clear):\n\n" + "\n\n---\n\n".join(sections)
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": context
            }
        }))
        return

    print("{}")


if __name__ == "__main__":
    print("{}")

#!/usr/bin/env python3
"""PostToolUse hook: detect state-impacting file edits, write marker.

Matches Edit/Write/NotebookEdit on files that represent LIVE state:
  - *.env, .env.*
  - *config*, *settings*
  - crontab files
  - systemd unit files (*.service, *.timer)
  - strategy/bot Python files under known dirs
Writes /tmp/state_change_pending.json with {ts, file, tool}.
UserPromptSubmit hook reads this to force memory update.
"""
import json
import re
import sys
import time
from pathlib import Path

MARKER = Path("/tmp/state_change_pending.json")

STATE_FILE_PATTERNS = [
    re.compile(r"\.env(\.|$)"),
    re.compile(r"(^|/)[^/]*config[^/]*\.(py|js|ts|json|yaml|yml|toml|ini)$", re.IGNORECASE),
    re.compile(r"(^|/)[^/]*settings[^/]*\.(py|js|ts|json|yaml|yml|toml|ini)$", re.IGNORECASE),
    re.compile(r"crontab"),
    re.compile(r"\.service$"),
    re.compile(r"\.timer$"),
    re.compile(r"/strategies?/[^/]+\.py$", re.IGNORECASE),
    re.compile(r"/strats?/[^/]+\.py$", re.IGNORECASE),
    re.compile(r"/hooks/[^/]+\.(py|sh)$", re.IGNORECASE),
]

EXCLUDE_PATTERNS = [
    re.compile(r"\.md$"),
    re.compile(r"/lessons/"),
    re.compile(r"/memory/convo_"),
    re.compile(r"/NardoWorld/"),
]


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0

    tool = payload.get("tool_name", "")
    if tool not in ("Edit", "Write", "NotebookEdit"):
        return 0

    tool_input = payload.get("tool_input", {}) or {}
    file_path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
    if not file_path:
        return 0

    if any(p.search(file_path) for p in EXCLUDE_PATTERNS):
        return 0
    if not any(p.search(file_path) for p in STATE_FILE_PATTERNS):
        return 0

    existing = []
    if MARKER.exists():
        try:
            existing = json.loads(MARKER.read_text())
            if not isinstance(existing, list):
                existing = []
        except Exception:
            existing = []

    existing.append({
        "ts": int(time.time()),
        "file": file_path,
        "tool": tool,
    })
    MARKER.write_text(json.dumps(existing, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""UserPromptSubmit hook: show VPS pipeline health in additionalContext.

Reads /tmp/vps_health.json (pulled from VPS by cron every 5min).
Only injects when there are RED or YELLOW items.
"""
import json
import sys
import time
from pathlib import Path

HEALTH_FILE = Path("/tmp/vps_health.json")
MAX_AGE_SEC = 600  # ignore if older than 10min


def main():
    try:
        json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        pass

    if not HEALTH_FILE.exists():
        print("{}")
        return

    try:
        age = time.time() - HEALTH_FILE.stat().st_mtime
        if age > MAX_AGE_SEC:
            print("{}")
            return

        data = json.loads(HEALTH_FILE.read_text())
        summary = data.get("summary", {})
        red = summary.get("red", 0)
        yellow = summary.get("yellow", 0)
        green = summary.get("green", 0)

        if red == 0 and yellow == 0:
            print("{}")
            return

        lines = [f"VPS Health: {red}🔴 {yellow}⚠️ {green}✅"]
        for p in data.get("pipelines", []):
            if p["status"] in ("RED", "YELLOW"):
                icon = "🔴" if p["status"] == "RED" else "⚠️"
                lines.append(f"  {icon} {p['name']}: {p['detail'][:60]}")

        context = "\n".join(lines)
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": context,
            }
        }))
    except Exception:
        print("{}")


if __name__ == "__main__":
    main()

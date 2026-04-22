#!/usr/bin/env python3
"""UserPromptSubmit hook: inject /bus active reminder so sessions don't forget.

Reads ~/.cache/claude_bus_active.json — if present + fresh (<1h old), inject
reminder with bus name, active peers, and auto-announce trigger list.
"""

import json
import os
import sys
import time

MARKER = os.path.expanduser("~/.cache/claude_bus_active.json")
REGISTRY = "/tmp/claude_bus_registry.jsonl"
MAX_AGE_SEC = 3600


def _peers(my_name):
    if not os.path.exists(REGISTRY):
        return []
    cutoff = time.time() - 60
    latest = {}
    try:
        with open(REGISTRY) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                n = r.get("name")
                t = r.get("ts", 0)
                if n and t > latest.get(n, 0):
                    latest[n] = t
    except OSError:
        return []
    return sorted(n for n, t in latest.items() if t >= cutoff and n != my_name)


def main():
    if not os.path.exists(MARKER):
        sys.exit(0)
    try:
        with open(MARKER) as f:
            m = json.load(f)
    except (OSError, json.JSONDecodeError):
        sys.exit(0)
    joined_ts = m.get("joined_ts", 0)
    if time.time() - joined_ts > MAX_AGE_SEC:
        sys.exit(0)
    name = m.get("name", "?")
    peers = _peers(name)
    peer_str = ", ".join(peers) if peers else "none (alone)"
    ctx = (
        f"📡 /bus ACTIVE as {name}. Peers: {peer_str}. "
        f"Auto-announce on: milestone shipped, Agent completion, pivot, "
        f"blocker, starting work on shared surface. Use `jq ... >> /tmp/claude_bus.jsonl`."
    )
    print(json.dumps({"additionalContext": ctx}))


if __name__ == "__main__":
    main()

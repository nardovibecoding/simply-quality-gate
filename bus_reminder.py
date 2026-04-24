#!/usr/bin/env python3
# @bigd-hook-meta
# name: bus_reminder
# fires_on: UserPromptSubmit
# relevant_intents: [meta, bigd, code]
# irrelevant_intents: [docx, x_tweet, telegram, git, pm, vps, sync]
# cost_score: 1
# always_fire: false
"""UserPromptSubmit hook: inject /bus active reminder so sessions don't forget.

Reads ~/.cache/claude_bus_active.json — if present + fresh (<1h old), inject
reminder with bus name, active peers, and auto-announce trigger list.
"""

import io
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))
from telemetry import log_fire, log_fire_done
from _semantic_router import should_fire

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
    _t0 = log_fire(__file__)
    _errored = False
    try:
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
        out = json.dumps({"additionalContext": ctx})
        log_fire_done(__file__, _t0, errored=False, output_size_bytes=len(out))
        print(out)
    except SystemExit:
        log_fire_done(__file__, _t0, errored=False, output_size_bytes=0)
        raise
    except Exception as e:
        _errored = True
        log_fire_done(__file__, _t0, errored=True, output_size_bytes=0)
        print(f"[bus_reminder] error: {e}", file=sys.stderr)
        sys.exit(0)


if __name__ == "__main__":
    _raw_stdin = sys.stdin.read()
    try:
        _prompt = json.loads(_raw_stdin).get("prompt", "")
    except Exception:
        _prompt = ""
    sys.stdin = io.StringIO(_raw_stdin)
    if not should_fire(__file__, _prompt):
        print(json.dumps({}))
        sys.exit(0)
    main()

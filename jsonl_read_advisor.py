#!/usr/bin/env python3
# @bigd-hook-meta
# name: jsonl_read_advisor
# fires_on: PreToolUse
# always_fire: false
# cost_score: 1
# Copyright (c) 2026 Nardo (nardovibecoding). AGPL-3.0 — see LICENSE
"""PreToolUse hook on Read: warn when reading large jsonl files; suggest jq projection.

Token-saving discipline. Reading a 100k-row trade-journal = full token cost.
jq-via-Bash with timestamp/kind filter returns 5 rows for ~5% of the cost.

Warn-only. Logs to ledger. Ratchet to block after 7d if ledger clean.
"""
import json
import os
import sys
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).parent))
from hook_base import run_hook

# Size threshold: warn if file >= 50KB
SIZE_THRESHOLD_BYTES = 50_000

# Known-large dirs that ALWAYS warrant projection regardless of current size
LARGE_DIRS = (
    str(Path.home() / "NardoWorld/meta/ssot"),
    str(Path.home() / ".claude/scripts/state"),
    str(Path.home() / "prediction-markets/data"),
    str(Path.home() / "NardoWorld/atoms"),
    str(Path.home() / "NardoWorld/sessions"),
)

LEDGER = Path.home() / ".claude/scripts/state/jsonl-read-advisor.jsonl"


def _check(tool_name, tool_input, _input_data):
    if tool_name != "Read":
        return False
    fp = tool_input.get("file_path", "")
    if not fp.endswith(".jsonl"):
        return False
    return True


def _action(_tool_name, tool_input, _input_data):
    fp = tool_input.get("file_path", "")
    p = Path(fp)
    try:
        size = p.stat().st_size if p.exists() else 0
    except Exception:
        size = 0

    in_large_dir = any(fp.startswith(d) for d in LARGE_DIRS)
    if size < SIZE_THRESHOLD_BYTES and not in_large_dir:
        return None

    # Log entry
    try:
        LEDGER.parent.mkdir(parents=True, exist_ok=True)
        with LEDGER.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": datetime.now(timezone.utc).isoformat(),
                "file": fp,
                "size": size,
                "in_large_dir": in_large_dir,
            }) + "\n")
    except Exception:
        pass

    size_kb = size // 1024
    suggest = (
        f"jq -c 'select(.kind==\"X\" and .ts > \"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M')}\")' {fp} | tail -20"
    )
    return (
        f"⚠️ Large jsonl Read ({size_kb} KB): {fp}\n"
        f"Token-saving alternative — pipe through jq projection via Bash:\n"
        f"  {suggest}\n"
        f"Or use offset/limit if you need a specific row range. "
        f"Full Read is fine for small jsonl (<50KB). Logged to {LEDGER.name}."
    )


if __name__ == "__main__":
    run_hook(_check, _action, hook_name="jsonl_read_advisor")

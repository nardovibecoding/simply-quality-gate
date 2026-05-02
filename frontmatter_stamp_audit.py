#!/usr/bin/env python3
"""Frontmatter stamp audit (Slice A — WARN-ONLY for first 7 days, then promote to block).

Fires on PreToolUse Write/Edit when path is under recall corpora:
  - ~/.claude/projects/-Users-bernard/memory/convo_*.md
  - ~/NardoWorld/atoms/*.md
  - ~/NardoWorld/lessons/*.md

Checks the staged file body for the 6 forward-only Slice A fields:
  writer, runtime, source_kind, session_id, originSessionId, date

Action: log missing fields to ~/.claude/scripts/state/frontmatter-audit.jsonl. Never blocks.

Promotion plan: after 7 days of zero-violation runs (or all violations bypass-marked),
flip MODE to "block" + add the bypass marker check (mirror comment_code_audit.py).
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import sys
import time

LOG = pathlib.Path.home() / ".claude" / "scripts" / "state" / "frontmatter-audit.jsonl"
HOME = pathlib.Path.home()
WATCH_DIRS = [
    HOME / ".claude" / "projects" / "-Users-bernard" / "memory",
    HOME / "NardoWorld" / "atoms",
    HOME / "NardoWorld" / "lessons",
]
REQUIRED_FIELDS = ["writer", "runtime", "source_kind",
                   "session_id", "originSessionId", "date"]
FM_BLOCK = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
MODE = "warn-only"  # flip to "block" after 7-day clean run


def is_watched(path: str) -> bool:
    if not path:
        return False
    p = pathlib.Path(path).resolve()
    return any(str(p).startswith(str(d.resolve()) + os.sep) for d in WATCH_DIRS) and p.suffix == ".md"


def parse_fields(content: str) -> dict[str, str]:
    m = FM_BLOCK.match(content)
    if not m:
        return {}
    fields: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            fields[k.strip()] = v.strip()
    return fields


def audit(_path: str, content: str) -> list[str]:
    fields = parse_fields(content)
    return [f for f in REQUIRED_FIELDS if f not in fields or not fields[f]]


def log_audit(path: str, missing: list[str], decision: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "path": path,
        "missing": missing,
        "mode": MODE,
        "decision": decision,
    }
    with open(LOG, "a") as fh:
        fh.write(json.dumps(rec) + "\n")


def main() -> None:
    try:
        data = json.loads(sys.stdin.read())
    except Exception:
        sys.exit(0)
    if data.get("tool_name") not in ("Write", "Edit"):
        sys.exit(0)
    tool_input = data.get("tool_input") or {}
    path = tool_input.get("file_path", "") or ""
    if not is_watched(path):
        sys.exit(0)
    # Edit: post-edit content reconstruction is hard from PreToolUse; only audit Write.
    # For Edit, we audit the existing file's frontmatter (best-effort proxy).
    if data.get("tool_name") == "Write":
        content = tool_input.get("content", "") or ""
    else:
        try:
            content = pathlib.Path(path).read_text()
        except Exception:
            sys.exit(0)
    missing = audit(path, content)
    if not missing:
        sys.exit(0)
    log_audit(path, missing, decision="warn")
    if MODE == "block":
        # Promotion path; not active in Slice A.
        msg = (f"frontmatter-stamp-audit: {len(missing)} required field(s) missing on {path}: "
               f"{', '.join(missing)}. Use ~/.claude/scripts/stamp_frontmatter.py to emit. "
               f"Bypass: add `[skip-frontmatter-audit=<reason>]` to commit subject.")
        print(json.dumps({"decision": "block", "reason": msg}))
    sys.exit(0)


if __name__ == "__main__":
    main()

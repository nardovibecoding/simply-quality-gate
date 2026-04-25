#!/usr/bin/env python3
# Copyright (c) 2026 Nardo. AGPL-3.0
"""PreToolUse hook: block dot-vs-dash LaunchAgent collisions.

Triggered on Write to ~/Library/LaunchAgents/com.bernard.*.plist*.

Canonicalises the target's stem (strip prefix `com.bernard.`, strip `.plist`
plus any `.disabled.*` / `.bak.*` suffix, then collapse `.` and `-` to `_`).
If any existing file in the same dir produces the same canonical stem and the
literal filenames differ (dot-vs-dash, case), block with exit 2 so Claude Code
refuses the Write.

Motivated by 2026-04-25 round-5 ship subagent that wrote
`com.bernard.bigd.lint.plist` (DOT, every 30min) while
`com.bernard.bigd-lint.plist` (DASH, daily 14:00) already existed.
Result: 48x firehose, 1200+ duplicate inbox briefs in 24h.

Hook protocol: read PreToolUse JSON from stdin, exit 0 to allow, exit 2 to
block. stderr is surfaced to the model.
"""
import json
import os
import re
import sys
from pathlib import Path

LAUNCHAGENTS_DIR = Path(os.path.expanduser("~/Library/LaunchAgents"))
TARGET_PREFIX = "com.bernard."
# match com.bernard.<stem>.plist OR .plist.disabled.* OR .plist.bak.* etc.
TARGET_RE = re.compile(
    r"^com\.bernard\.(?P<stem>.+?)\.plist(?:\.disabled.*|\.bak.*)?$",
    re.IGNORECASE,
)


def canonical_stem(filename: str) -> str | None:
    """Return canonical stem for a com.bernard.*.plist* filename, or None."""
    m = TARGET_RE.match(filename)
    if not m:
        return None
    stem = m.group("stem")
    # collapse `.` and `-` to single delimiter so dot-vs-dash collide
    return re.sub(r"[.\-]+", "_", stem).lower()


def check_collision(target_path: str) -> tuple[str, str, str] | None:
    """Return (target_basename, existing_basename, canonical_stem) on collision, else None."""
    target_p = Path(target_path)
    target_name = target_p.name
    target_stem = canonical_stem(target_name)
    if target_stem is None:
        return None
    # Only enforce in LaunchAgents dir
    try:
        if target_p.parent.resolve() != LAUNCHAGENTS_DIR.resolve():
            return None
    except (OSError, RuntimeError):
        return None
    if not LAUNCHAGENTS_DIR.is_dir():
        return None
    # Exact overwrite path: if the literal filename exists, allow.
    # User is editing in place, not creating a colliding twin.
    if (LAUNCHAGENTS_DIR / target_name).exists():
        return None
    for existing in LAUNCHAGENTS_DIR.iterdir():
        ename = existing.name
        if ename == target_name:
            # exact overwrite — allow
            continue
        estem = canonical_stem(ename)
        if estem is None:
            continue
        if estem == target_stem:
            return (target_name, ename, target_stem)
    return None


def main():
    raw = sys.stdin.read()
    if not raw.strip():
        sys.exit(0)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        sys.exit(0)
    if data.get("tool_name") != "Write":
        sys.exit(0)
    fp = (data.get("tool_input") or {}).get("file_path", "")
    if not fp:
        sys.exit(0)
    collision = check_collision(fp)
    if collision is None:
        sys.exit(0)
    target, existing, stem = collision
    msg = (
        f"LaunchAgent dup-guard: refusing to write `{target}` — "
        f"collides with existing `{existing}`.\n"
        f"Both normalize to canonical stem `{stem}`. "
        f"macOS treats them as separate units but they likely shadow each other's intent.\n"
        f"If this is intentional (e.g. replacement), first delete or rename "
        f"the existing one and retry.\n"
        f"To bypass for genuine cases: rename target so canonical stem differs.\n"
    )
    print(msg, file=sys.stderr)
    sys.exit(2)


if __name__ == "__main__":
    main()

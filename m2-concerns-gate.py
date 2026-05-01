#!/usr/bin/env python3
# Hook M2: every project with .ship/ must declare concerns thresholds.
# Created: 2026-05-01 (jz-mode tooling upgrade)
# Trigger: PreToolUse Bash on `git commit`. If cwd's git-root contains
# `.ship/` BUT lacks `.ship/_meta/concerns.md`, block the commit.
#
# Bypass: include `[skip-concerns-check=<reason>]` in commit message.
# Logged to ~/.claude/scripts/state/concern-skips.jsonl.
#
# Source: rules/disciplines/M2-domain-concerns.md
# Parallel to: m1-domain-invariants-gate.py (same shape, different file).
import json
import os
import pathlib
import re
import sys
import time


def find_git_root(start: pathlib.Path) -> pathlib.Path | None:
    p = start.resolve()
    while p != p.parent:
        if (p / ".git").exists():
            return p
        p = p.parent
    return None


def extract_commit_message(cmd: str) -> str:
    m = re.search(r"-m\s+(['\"])(.+?)\1", cmd, re.DOTALL)
    if m:
        return m.group(2)
    m = re.search(r"--message[= ]+(['\"])(.+?)\1", cmd, re.DOTALL)
    if m:
        return m.group(2)
    return ""


def log_skip(slug_root: pathlib.Path, reason: str) -> None:
    state_dir = pathlib.Path.home() / ".claude" / "scripts" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "project": str(slug_root),
        "reason": reason,
    }
    with open(state_dir / "concern-skips.jsonl", "a") as f:
        f.write(json.dumps(rec) + "\n")


def main() -> None:
    try:
        data = json.loads(sys.stdin.read())
    except Exception:
        sys.exit(0)

    if data.get("tool_name") != "Bash":
        sys.exit(0)

    cmd = (data.get("tool_input") or {}).get("command", "") or ""
    if not re.search(r"\bgit\s+commit\b", cmd):
        sys.exit(0)

    cwd = pathlib.Path((data.get("tool_input") or {}).get("cwd") or os.getcwd())
    root = find_git_root(cwd)
    if not root:
        sys.exit(0)

    ship_dir = root / ".ship"
    if not ship_dir.is_dir():
        sys.exit(0)

    concerns_file = ship_dir / "_meta" / "concerns.md"
    if concerns_file.exists() and concerns_file.stat().st_size > 0:
        sys.exit(0)

    msg = extract_commit_message(cmd)
    skip_match = re.search(r"\[skip-concerns-check=([^\]]+)\]", msg)
    if skip_match:
        log_skip(root, skip_match.group(1))
        sys.exit(0)

    print(
        json.dumps(
            {
                "decision": "block",
                "reason": (
                    f"M2 meta-rule: project at {root} uses /ship "
                    f"(.ship/ exists) but lacks "
                    f"{concerns_file.relative_to(root)}.\n\n"
                    "Every /ship-using project must declare its concerns "
                    "thresholds per "
                    "~/.claude/rules/disciplines/M2-domain-concerns.md.\n\n"
                    "Fix: create `.ship/_meta/concerns.md` with C1-C7 entries. "
                    "Reference pm-bot at "
                    "~/prediction-markets/.ship/_meta/concerns.md.\n\n"
                    "Bypass: add `[skip-concerns-check=<reason>]` to commit "
                    "message subject."
                ),
            }
        )
    )
    sys.exit(0)


if __name__ == "__main__":
    main()

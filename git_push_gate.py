#!/usr/bin/env python3
# @bigd-hook-meta
# name: git_push_gate
# fires_on: PreToolUse
# relevant_intents: [git, sync]
# irrelevant_intents: [bigd, telegram, docx, x_tweet, debug]
# cost_score: 1
# always_fire: false
"""PreToolUse(Bash) hook: intercept `git push` commands targeting github.com
remotes. Block the raw push and instruct the model to use gated_push.py
instead, which scans the diff for credential leaks first.

Non-github remotes (hel:/london: bare) pass through unchanged.
Bash subprocesses spawned by hooks/scripts (L1's Popen, gated_push's own
child push) are NOT Claude tool calls, so they don't fire this hook —
the gate runs once at the Claude-Bash boundary, not recursively.
"""
import json
import re
import subprocess
import sys
from pathlib import Path

GATE = Path.home() / ".claude" / "scripts" / "gated_push.py"


def remote_url(cwd: str, remote: str = "origin") -> str:
    rc = subprocess.run(
        ["git", "-C", cwd, "remote", "get-url", remote],
        capture_output=True,
        text=True,
        timeout=3,
    )
    return rc.stdout.strip()


def parse_push_cwd(cmd: str) -> str:
    """Extract the working directory the push will run in.
    Handles: `git -C <path> push`, `cd <path> && git push`, plain `git push`.
    Returns "" if not parseable.
    """
    m = re.search(r"git\s+-C\s+(\S+)\s+.*push", cmd)
    if m:
        return m.group(1).strip("'\"")
    m = re.search(r"cd\s+(\S+).*?git\s+push", cmd)
    if m:
        return m.group(1).strip("'\"")
    return ""


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0

    cmd = payload.get("tool_input", {}).get("command", "") or ""
    if "git push" not in cmd:
        return 0
    if str(GATE) in cmd:
        # Already routed through the gate — let it through.
        return 0

    cwd = parse_push_cwd(cmd)
    if not cwd:
        # Plain `git push` with no -C / cd parseable — allow, but warn.
        # Stricter mode would block here; opting for permissive default
        # to avoid breaking interactive sessions.
        return 0

    cwd_path = str(Path(cwd).expanduser())
    url = remote_url(cwd_path)
    if "github.com" not in url:
        return 0  # hel:/london:/local — passthrough

    # Determine current branch for the helpful command suggestion.
    branch_rc = subprocess.run(
        ["git", "-C", cwd_path, "symbolic-ref", "--short", "HEAD"],
        capture_output=True,
        text=True,
        timeout=3,
    )
    branch = branch_rc.stdout.strip() or "main"

    suggestion = f"python3 {GATE} {cwd_path} {branch}"
    output = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                f"Direct `git push` to github.com remote ({url}) blocked. "
                f"Use the gated_push helper instead, which scans the diff "
                f"for credential leaks before pushing:\n  {suggestion}"
            ),
        }
    }
    print(json.dumps(output))
    return 0


if __name__ == "__main__":
    sys.exit(main())

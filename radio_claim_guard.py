#!/usr/bin/env python3
"""
radio_claim_guard.py — PreToolUse hook for /radio file-claim enforcement.
Blocks Edit, Write, NotebookEdit, MultiEdit on paths claimed by another session.

Protocol (Claude Code hook stdin):
  JSON object with keys: tool_name, tool_input (dict with file_path or similar).

Output (blocking):
  {"decision": "block", "reason": "..."}  → printed to stdout, exit 0
  (no output, exit 0) → allow

Bypass:
  RADIO_CLAIM_BYPASS=1 → skip check, log warn to stderr, allow.

Fast-exit:
  If ~/.claude/bus/claims/ is empty or missing → exit 0 immediately (zero overhead).
"""

import json
import os
import sys
import hashlib
import subprocess

_bus_dir = os.environ.get("BUS_DIR", os.path.expanduser("~/.claude/bus"))
CLAIMS_DIR = os.path.join(_bus_dir, "claims")

# Tools whose file_path param we intercept.
WATCHED_TOOLS = {"Edit", "Write", "NotebookEdit", "MultiEdit"}

# Map tool name → list of param keys that hold a file path.
TOOL_PATH_KEYS = {
    "Edit": ["file_path"],
    "Write": ["file_path"],
    "NotebookEdit": ["notebook_path"],
    "MultiEdit": ["file_path"],
}


def sha256_of_string(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def resolve_own_session_id() -> str | None:
    """
    Walk parent processes looking for 'claude' binary, same logic as _lock_lib.sh.
    BUS_FORCE_SID env override for tests.
    """
    force = os.environ.get("BUS_FORCE_SID", "")
    if force:
        return force.strip()

    pid = os.getppid()
    for _ in range(16):
        try:
            r = subprocess.run(
                ["ps", "-p", str(pid), "-o", "ppid=,command="],
                capture_output=True, text=True, timeout=2
            )
            if r.returncode != 0 or not r.stdout.strip():
                break
            line = r.stdout.strip()
            parts = line.split(None, 1)
            ppid = int(parts[0])
            cmd = parts[1] if len(parts) > 1 else ""
            argv0 = cmd.split()[0] if cmd.strip() else ""
            basename = os.path.basename(argv0)
            if basename == "claude":
                return str(pid)
            if ppid <= 1:
                break
            pid = ppid
        except Exception:
            break
    return None


def is_pid_alive(pid_str: str) -> bool:
    try:
        pid = int(pid_str)
        os.kill(pid, 0)
        return True
    except (ValueError, ProcessLookupError, PermissionError):
        return False


def check_claim(abs_path: str, own_sid: str | None) -> dict | None:
    """
    Returns blocking dict if path is claimed by another live session.
    Returns None if safe to proceed.
    """
    sha = sha256_of_string(abs_path)
    claim_file = os.path.join(CLAIMS_DIR, sha)

    if not os.path.isfile(claim_file):
        return None  # Not claimed.

    try:
        with open(claim_file) as f:
            claim = json.load(f)
    except Exception:
        # Corrupted claim → treat as no claim; log warn.
        print(f"[radio-claim] WARN: corrupted claim file for {abs_path}, treating as unclaimed", file=sys.stderr)
        return None

    claim_sid = str(claim.get("session_id", ""))
    claim_name = claim.get("name", "?")
    claim_ts = claim.get("ts", "?")

    # Own claim → allow.
    # Also allow when own_sid is unknown (fail-open: can't verify, don't block).
    if not own_sid or claim_sid == own_sid:
        return None

    # Dead session → sweep and allow.
    if not is_pid_alive(claim_sid):
        try:
            os.remove(claim_file)
            print(f"[radio-claim] swept stale claim (dead PID {claim_sid}): {abs_path}", file=sys.stderr)
        except Exception:
            pass
        return None

    # Live foreign claim → block.
    return {
        "decision": "block",
        "reason": (
            f"Path claimed by {claim_name} (session {claim_sid}) at {claim_ts}. "
            f"Coordinate via /radio or set RADIO_CLAIM_BYPASS=1 if emergency."
        ),
    }


def main() -> None:
    # Fast-exit: no claims dir or empty.
    if not os.path.isdir(CLAIMS_DIR) or not os.listdir(CLAIMS_DIR):
        sys.exit(0)

    # BYPASS mode.
    if os.environ.get("RADIO_CLAIM_BYPASS", "") == "1":
        raw = sys.stdin.read()
        try:
            data = json.loads(raw)
            path = ""
            tool = data.get("tool_name", "")
            for key in TOOL_PATH_KEYS.get(tool, ["file_path"]):
                path = data.get("tool_input", {}).get(key, "")
                if path:
                    break
        except Exception:
            path = "(unknown)"
        print(f"[radio-claim] BYPASS active — skipping check for: {path}", file=sys.stderr)
        sys.exit(0)

    raw = sys.stdin.read()
    try:
        data = json.loads(raw)
    except Exception:
        sys.exit(0)  # Can't parse → allow.

    tool_name = data.get("tool_name", "")
    if tool_name not in WATCHED_TOOLS:
        sys.exit(0)

    tool_input = data.get("tool_input", {})
    path_keys = TOOL_PATH_KEYS.get(tool_name, ["file_path"])
    raw_path = ""
    for k in path_keys:
        raw_path = tool_input.get(k, "")
        if raw_path:
            break

    if not raw_path:
        sys.exit(0)

    # Resolve absolute path.
    try:
        abs_path = os.path.realpath(raw_path)
    except Exception:
        abs_path = raw_path

    own_sid = resolve_own_session_id()

    result = check_claim(abs_path, own_sid)
    if result:
        # Emit block decision.
        print(json.dumps(result))
        print(
            f"[radio-claim] BLOCKED {tool_name} on {abs_path} — claimed by {result.get('reason', '')}",
            file=sys.stderr
        )
        sys.exit(0)  # Hook protocol: exit 0 with JSON output = block

    sys.exit(0)


if __name__ == "__main__":
    main()

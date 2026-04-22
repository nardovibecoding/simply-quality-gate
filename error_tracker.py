#!/usr/bin/env python3
"""PostToolUse hook: track consecutive Bash errors → inject lesson reminder after 2 in a row."""
import hashlib
import json
import sys
import time
from pathlib import Path

STATE_FILE = Path("/tmp/claude_error_tracker.json")
THRESHOLD = 2


def fingerprint(command: str, output: str) -> str:
    """Stable hash: command binary + first error keyword found."""
    cmd_bin = command.strip().split()[0] if command.strip() else ""
    error_sig = ""
    for line in output.splitlines():
        lo = line.lower()
        if any(k in lo for k in ("exit code", "error:", "failed", "refused", "permission denied")):
            error_sig = line.strip()[:120]
            break
    return hashlib.md5(f"{cmd_bin}|{error_sig}".encode()).hexdigest()[:10]


def main():
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        sys.stdout.write("{}\n")
        return

    if data.get("tool_name") != "Bash":
        sys.stdout.write("{}\n")
        return

    command = data.get("tool_input", {}).get("command", "")
    resp = data.get("tool_response", {})
    output = ""
    if isinstance(resp, dict):
        output = str(resp.get("output", "")) + str(resp.get("stderr", ""))
    elif isinstance(resp, str):
        output = resp

    is_error = any(k in output for k in (
        "Error:", "exit code", "Exit code", "FAILED", "failed:", "Connection refused",
        "Permission denied", "No such file"
    ))

    if not is_error:
        STATE_FILE.unlink(missing_ok=True)
        sys.stdout.write("{}\n")
        return

    state = {}
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text())
        except Exception:
            state = {}

    fp = fingerprint(command, output)
    if state.get("fp") == fp:
        state["count"] = state.get("count", 1) + 1
    else:
        state = {"fp": fp, "count": 1, "cmd": command[:200], "ts": time.time()}

    STATE_FILE.write_text(json.dumps(state))

    if state["count"] >= THRESHOLD:
        STATE_FILE.unlink(missing_ok=True)
        msg = (
            f"SAME ERROR {state['count']}x IN A ROW on: `{command[:80]}`. "
            "STOP retrying minor variations. Diagnose root cause. "
            "After fixing, save a lesson to memory."
        )
        sys.stdout.write(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": msg
            }
        }) + "\n")
        return

    sys.stdout.write("{}\n")


if __name__ == "__main__":
    main()

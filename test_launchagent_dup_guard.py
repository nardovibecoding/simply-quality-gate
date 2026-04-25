#!/usr/bin/env python3
"""Test harness for launchagent_dup_guard.py.

Pipes mock PreToolUse JSON to the hook for 3 fault-injection cases:
  a) Collision (DOT target, DASH exists)         -> MUST block (exit 2)
  b) Exact overwrite                             -> MUST allow (exit 0)
  c) New unique name                             -> MUST allow (exit 0)

Pre-req: at least one existing com.bernard.bigd-lint.plist in LaunchAgents.
Prints PASS/FAIL per case + summary.
"""
import json
import os
import subprocess
import sys

HOOK = "/Users/bernard/.claude/hooks/launchagent_dup_guard.py"
LA = os.path.expanduser("~/Library/LaunchAgents")


def run(case_name, file_path, expected_exit):
    payload = json.dumps({
        "tool_name": "Write",
        "tool_input": {"file_path": file_path, "content": "<plist/>"},
    })
    proc = subprocess.run(
        ["python3", HOOK],
        input=payload,
        capture_output=True,
        text=True,
        timeout=10,
    )
    ok = proc.returncode == expected_exit
    status = "PASS" if ok else "FAIL"
    print(f"[{status}] {case_name}: exit={proc.returncode} expected={expected_exit}")
    if proc.stderr.strip():
        print(f"       stderr: {proc.stderr.strip().splitlines()[0][:120]}")
    return ok


def main():
    # Verify pre-req: existing dash file
    dash_existing = os.path.join(LA, "com.bernard.bigd-lint.plist")
    if not os.path.exists(dash_existing):
        print(f"PRECHECK FAIL: {dash_existing} not present, can't test collision")
        sys.exit(1)

    results = []
    # a) collision: DOT target collides with existing DASH
    results.append(run(
        "a) DOT-vs-DASH collision",
        os.path.join(LA, "com.bernard.bigd.lint.plist"),
        2,
    ))
    # b) exact overwrite of existing file
    results.append(run(
        "b) exact overwrite (DASH==DASH)",
        dash_existing,
        0,
    ))
    # c) novel non-colliding name
    results.append(run(
        "c) novel unique name",
        os.path.join(LA, "com.bernard.zzz-test-newthing.plist"),
        0,
    ))

    passed = sum(results)
    print(f"\n{passed}/{len(results)} passed")
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()

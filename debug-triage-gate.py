#!/usr/bin/env python3
# Hook E6: /debug verdict must include F-family triage.
# Created: 2026-05-01
# Trigger: PreToolUse Write on /debug verdict artifacts
# (~/NardoWorld/realize-debt.md or **/debug-*.md or files whose
# content starts with "# /debug" or "# debug verdict").
# Reject if content lacks an `F-family:` line referencing F1-F16.
#
# Bypass: `<!-- skip-debug-triage: <reason> -->` in content.
# Logged to ~/.claude/scripts/state/debug-triage-skips.jsonl.
#
# Source: CLAUDE.md "/debug primitives map symptoms to F-family
# before generic debug verbs"
import json
import pathlib
import re
import sys
import time


F_PAT = re.compile(r"(?im)^[\s>\-*]*F[-_ ]?famil(?:y|ies)\s*:\s*F\d+(?:\.\d+)?")
# Concern axis (C1-C7) — accepted as alternative triage marker per
# concerns-taxonomy.md (jz-mode added 2026-05-01).
C_PAT = re.compile(r"(?im)^[\s>\-*]*Concern\s*:\s*C[1-7]\b")
DEBUG_FILE_PAT = re.compile(
    r"(realize-debt\.md|/debug[-_/][^/]*\.md|/debug-verdict)$"
)
DEBUG_HEADER_PAT = re.compile(
    r"(?im)\A\s*#\s*(?:/?debug\b|debug verdict|debug report)"
)


def log_skip(path: str, reason: str) -> None:
    state_dir = pathlib.Path.home() / ".claude" / "scripts" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "path": path,
        "reason": reason,
    }
    with open(state_dir / "debug-triage-skips.jsonl", "a") as f:
        f.write(json.dumps(rec) + "\n")


def main() -> None:
    try:
        data = json.loads(sys.stdin.read())
    except Exception:
        sys.exit(0)

    if data.get("tool_name") != "Write":
        sys.exit(0)

    tin = data.get("tool_input") or {}
    fp = tin.get("file_path", "") or ""
    content = tin.get("content", "") or ""

    is_debug = bool(DEBUG_FILE_PAT.search(fp)) or bool(
        DEBUG_HEADER_PAT.search(content[:200])
    )
    if not is_debug:
        sys.exit(0)

    skip_match = re.search(
        r"<!--\s*skip-debug-triage[:=]\s*([^>]+?)\s*-->", content
    )
    if skip_match:
        log_skip(fp, skip_match.group(1).strip())
        sys.exit(0)

    if F_PAT.search(content):
        sys.exit(0)

    print(
        json.dumps(
            {
                "decision": "block",
                "reason": (
                    f"/debug triage gate: {fp} looks like a /debug verdict "
                    "but contains no `F-family:` line referencing the "
                    "invariant taxonomy.\n\nFix: add a line like "
                    "`F-family: F1.1` (or F2.x / F4.5 / etc.) near the top, "
                    "per CLAUDE.md '/debug primitives map symptoms to "
                    "F-family before generic debug verbs'. See "
                    "~/.claude/rules/invariant-taxonomy.md for codes.\n\n"
                    "Bypass: add `<!-- skip-debug-triage: <reason> -->` "
                    "to the content."
                ),
            }
        )
    )
    sys.exit(0)


if __name__ == "__main__":
    main()

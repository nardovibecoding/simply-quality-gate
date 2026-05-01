#!/usr/bin/env python3
# Hook E2: Discipline-Impact section gate.
# Created: 2026-05-01
# Trigger: PreToolUse Write on .ship/<slug>/01-spec.md or 02-plan.md.
# Reject if the staged content lacks §Discipline Impact section
# with all 4 mandatory sub-fields populated.
#
# Mandatory sub-fields (per phases/common/discipline-impact.md):
#   - lens: [F1.x, ...]
#   - applicable_DIs: [DI.N, ...]   (or explicit empty justification)
#   - disciplines: { D-code: ... }
#   - gaps: [...] (or empty)
#
# Bypass: include HTML comment `<!-- skip-discipline-impact: <reason> -->`
# anywhere in the file. Logged to
# ~/.claude/scripts/state/discipline-impact-skips.jsonl.
#
# Source: rules/ship.md §Discipline Impact section
import json
import pathlib
import re
import sys
import time


PHASE_PAT = re.compile(r"\.ship/[^/]+/(?:goals/)?(?:01-spec|02-plan)\.md$")


def log_skip(path: str, reason: str) -> None:
    state_dir = pathlib.Path.home() / ".claude" / "scripts" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "path": path,
        "reason": reason,
    }
    with open(state_dir / "discipline-impact-skips.jsonl", "a") as f:
        f.write(json.dumps(rec) + "\n")


def has_section(content: str) -> bool:
    return bool(re.search(r"(?im)^#{1,6}\s*.*?Discipline\s*Impact", content))


def has_field(content: str, field: str) -> bool:
    # match `field:` followed by non-empty value (yaml-like or fenced)
    pat = re.compile(
        rf"(?im)^[\s\-*]*`?{re.escape(field)}`?\s*:\s*\S"
    )
    return bool(pat.search(content))


def main() -> None:
    try:
        data = json.loads(sys.stdin.read())
    except Exception:
        sys.exit(0)

    if data.get("tool_name") != "Write":
        sys.exit(0)

    tin = data.get("tool_input") or {}
    fp = tin.get("file_path", "") or ""
    if not PHASE_PAT.search(fp):
        sys.exit(0)

    content = tin.get("content", "") or ""

    skip_match = re.search(
        r"<!--\s*skip-discipline-impact[:=]\s*([^>]+?)\s*-->", content
    )
    if skip_match:
        log_skip(fp, skip_match.group(1).strip())
        sys.exit(0)

    missing = []
    if not has_section(content):
        missing.append("§Discipline Impact section header")
    for f in ("lens", "applicable_DIs", "applicable_concerns", "disciplines", "gaps"):
        if not has_field(content, f):
            missing.append(f"sub-field `{f}:` (with non-empty value)")

    if not missing:
        sys.exit(0)

    print(
        json.dumps(
            {
                "decision": "block",
                "reason": (
                    f"Discipline Impact gate: {fp} is a /ship Phase 1 SPEC "
                    f"or Phase 2 PLAN artifact, but missing:\n  - "
                    + "\n  - ".join(missing)
                    + "\n\nFix: add a §Discipline Impact section per "
                    "~/.claude/skills/ship/phases/common/discipline-impact.md "
                    "with all 4 sub-fields (lens, applicable_DIs, "
                    "disciplines, gaps).\n\nBypass intentionally: add "
                    "`<!-- skip-discipline-impact: <reason> -->` in the "
                    "file content."
                ),
            }
        )
    )
    sys.exit(0)


if __name__ == "__main__":
    main()

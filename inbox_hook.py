#!/usr/bin/env python3
"""UserPromptSubmit hook: inject inbox briefs into additionalContext.

Tier delivery schedule (HKT = UTC+8):
- critical/  : always (every prompt)
- daily/     : 10:00-12:00 HKT only
- weekly/    : Sunday 20:00-22:00 HKT only

Validation: hand-rolled required-field check against _schema.json required fields.
On malformed brief: stderr warning, skip — never crash.
Budget: <200ms with 50 briefs queued.
"""

import glob
import json
import os
import sys
from datetime import datetime, timezone, timedelta

INBOX_ROOT = os.path.expanduser("~/inbox")
SCHEMA_REQUIRED = ["id", "tier", "source_daemon", "host", "title", "body", "created", "actions"]
ACTION_REQUIRED = ["code", "label", "command"]

HKT = timezone(timedelta(hours=8))


def _hkt_now():
    return datetime.now(tz=HKT)


def _in_daily_window(now):
    """10:00-12:00 HKT."""
    return now.hour == 10 or (now.hour == 11) or (now.hour == 12 and now.minute == 0)


def _in_weekly_window(now):
    """Sunday 20:00-22:00 HKT. weekday() == 6 = Sunday."""
    return now.weekday() == 6 and (now.hour == 20 or now.hour == 21 or (now.hour == 22 and now.minute == 0))


def _validate_brief(data, path):
    """Return True if all required fields present and actions[] valid. Warn on stderr otherwise."""
    for field in SCHEMA_REQUIRED:
        if field not in data:
            print(f"[inbox_hook] WARN: skipping {path} — missing field '{field}'", file=sys.stderr)
            return False
    if not isinstance(data["actions"], list) or len(data["actions"]) < 1:
        print(f"[inbox_hook] WARN: skipping {path} — actions must be non-empty list", file=sys.stderr)
        return False
    for action in data["actions"]:
        for af in ACTION_REQUIRED:
            if af not in action:
                print(f"[inbox_hook] WARN: skipping {path} — action missing field '{af}'", file=sys.stderr)
                return False
    return True


def _load_briefs(subdir):
    """Load and validate all JSON briefs in a subdir. Return list of dicts."""
    pattern = os.path.join(INBOX_ROOT, subdir, "*.json")
    briefs = []
    for path in sorted(glob.glob(pattern)):
        try:
            with open(path, "r") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            print(f"[inbox_hook] WARN: cannot read {path} — {e}", file=sys.stderr)
            continue
        if _validate_brief(data, path):
            briefs.append(data)
    return briefs


def _format_brief(brief, idx):
    """Format a single brief for additionalContext injection."""
    lines = [
        f"[INBOX #{idx}] [{brief['tier'].upper()}] {brief['title']}",
        f"  Source: {brief['source_daemon']} @ {brief['host']} | ID: {brief['id']}",
        f"  {brief['body']}",
        "  Actions:",
    ]
    for action in brief["actions"]:
        lines.append(f"    [{action['code']}] {action['label']}")
    return "\n".join(lines)


def main():
    # Consume stdin (required for hook protocol; ignore content for this hook)
    try:
        json.load(sys.stdin)
    except Exception:
        pass

    now = _hkt_now()

    all_briefs = []

    # Always: critical
    all_briefs.extend(_load_briefs("critical"))

    # Daily window: 10:00-12:00 HKT
    if _in_daily_window(now):
        all_briefs.extend(_load_briefs("daily"))

    # Weekly window: Sunday 20:00-22:00 HKT
    if _in_weekly_window(now):
        all_briefs.extend(_load_briefs("weekly"))

    if not all_briefs:
        print(json.dumps({}))
        return

    lines = [
        "<inbox-briefs>",
        "[System note: Big SystemD inbox briefs — pending items for Bernard's approval. "
        "Each brief has reply codes; Bernard types e.g. '1' to approve, '2' to defer, '3' to skip.]",
        "",
        f"Pending briefs ({len(all_briefs)}):",
    ]
    for i, brief in enumerate(all_briefs, 1):
        lines.append("")
        lines.append(_format_brief(brief, i))

    lines.append("")
    lines.append("</inbox-briefs>")

    context = "\n".join(lines)
    print(json.dumps({"additionalContext": context}))


if __name__ == "__main__":
    main()

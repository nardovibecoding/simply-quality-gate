#!/usr/bin/env python3
# @bigd-hook-meta
# name: inbox_ack
# fires_on: UserPromptSubmit
# relevant_intents: [bigd, meta]
# irrelevant_intents: [git, pm, telegram, docx, x_tweet, code, vps, sync]
# cost_score: 2
# always_fire: false
"""UserPromptSubmit hook: parse Bernard's reply codes and write approval files.

Runs AFTER inbox_hook.py (registered second in settings.json) so brief context
is already loaded when this fires.

Reply code grammar (case-insensitive):
  Single digit:  "1", "2", "3"
  Range:         "1-3 yes", "1 3 yes"
  Word:          "approve", "defer", "skip", "yes", "no"
  Combined:      "approve all", "yes 1 3 5", "defer 2"
  Scoped:        "ack <brief_id> 1" → targets specific brief

Recognition gate (R2 guard): prompt must contain EITHER:
  - A keyword prefix: approve/defer/skip/ack/yes/no
  - A digit followed by "yes", "approve", "defer", "skip", or "ack"
  - "ack <id>" reference
  Bare "1" alone does NOT trigger. Prevents false positives mid-task.

Outputs:
  ~/inbox/_approvals/<brief_id>.json  — approval record
  ~/inbox/archive/<id>.json           — brief moved here after ack

Race protection: fcntl.flock on ~/inbox/_approvals/.ack_write.lock
"""

import fcntl
import glob
import io
import json
import os
import re
import shutil
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(__file__))
from telemetry import log_fire, log_fire_done
from _semantic_router import should_fire

INBOX_ROOT = os.path.expanduser("~/inbox")
APPROVALS_DIR = os.path.join(INBOX_ROOT, "_approvals")
ARCHIVE_DIR = os.path.join(INBOX_ROOT, "archive")
LOCK_PATH = os.path.join(APPROVALS_DIR, ".ack_write.lock")

SCHEMA_REQUIRED = ["id", "tier", "source_daemon", "host", "title", "body", "created", "actions"]
ACTION_REQUIRED = ["code", "label", "command"]

HKT = timezone(timedelta(hours=8))

# Keywords that make a prompt recognizable as an ack
ACK_KEYWORDS = {"approve", "defer", "skip", "ack", "yes", "no"}

# Word-to-code normalization
WORD_MAP = {
    "approve": "approve",
    "yes":     "approve",
    "defer":   "defer",
    "later":   "defer",
    "skip":    "skip",
    "no":      "skip",
}


def _validate_brief(data):
    for field in SCHEMA_REQUIRED:
        if field not in data:
            return False
    if not isinstance(data["actions"], list) or len(data["actions"]) < 1:
        return False
    for action in data["actions"]:
        for af in ACTION_REQUIRED:
            if af not in action:
                return False
    return True


def _load_all_briefs():
    """Load all briefs across tiers; return list of (path, data)."""
    result = []
    for subdir in ("critical", "daily", "weekly"):
        pattern = os.path.join(INBOX_ROOT, subdir, "*.json")
        for path in sorted(glob.glob(pattern)):
            try:
                with open(path, "r") as f:
                    data = json.load(f)
            except (OSError, json.JSONDecodeError):
                continue
            if _validate_brief(data):
                result.append((path, data))
    return result


def _load_brief_by_id(brief_id):
    """Find a specific brief by id. Return (path, data) or (None, None)."""
    for subdir in ("critical", "daily", "weekly"):
        pattern = os.path.join(INBOX_ROOT, subdir, "*.json")
        for path in sorted(glob.glob(pattern)):
            try:
                with open(path, "r") as f:
                    data = json.load(f)
            except (OSError, json.JSONDecodeError):
                continue
            if data.get("id") == brief_id and _validate_brief(data):
                return path, data
    return None, None


def _oldest_brief(all_briefs):
    """Return (path, data) for oldest brief across tiers: critical first, then daily, then weekly."""
    tier_order = {"critical": 0, "daily": 1, "weekly": 2}
    if not all_briefs:
        return None, None
    # Sort by tier priority then created timestamp
    def sort_key(item):
        _, data = item
        return (tier_order.get(data.get("tier", "weekly"), 9), data.get("created", ""))
    sorted_briefs = sorted(all_briefs, key=sort_key)
    return sorted_briefs[0]


def _parse_prompt(prompt):
    """
    Parse prompt for ack intent.

    Returns dict with keys:
      recognized: bool
      codes: list of strings (e.g. ["1", "2"] or ["approve", "defer"])
      brief_id: str or None (if scoped)
      all_flag: bool (if "all" keyword present)

    Returns None if prompt is not recognized as an ack.
    """
    # Preserve original tokens for case-sensitive brief_id extraction
    orig_tokens = re.split(r"[\s,]+", prompt.strip())
    orig_tokens = [t for t in orig_tokens if t]

    text = prompt.strip().lower()
    tokens = re.split(r"[\s,]+", text)
    tokens = [t for t in tokens if t]

    if not tokens:
        return None

    # Check recognition gate first
    has_ack_keyword = any(t in ACK_KEYWORDS for t in tokens)

    # Check for digit + keyword pattern: "1 yes", "2 later", etc.
    # Also allow "ack <brief_id>" pattern
    has_digit_keyword = False
    for i, t in enumerate(tokens):
        if re.match(r"^[1-5]$", t):
            # Look for a qualifying keyword anywhere in the tokens
            rest = tokens[:i] + tokens[i+1:]
            if any(r in ACK_KEYWORDS for r in rest):
                has_digit_keyword = True
                break

    if not has_ack_keyword and not has_digit_keyword:
        return None

    # Extract scoped brief_id: "ack <brief_id>" pattern
    # Use orig_tokens for case-preserving ID; lowercased tokens for position
    brief_id = None
    ack_idx = None
    for i, t in enumerate(tokens):
        if t == "ack" and i + 1 < len(tokens):
            candidate_lower = tokens[i + 1]
            candidate_orig = orig_tokens[i + 1] if i + 1 < len(orig_tokens) else candidate_lower
            # Brief IDs match pattern: word_word_date_rand or arbitrary id
            # Accept if it doesn't look like a plain code
            if not re.match(r"^[1-5]$", candidate_lower) and candidate_lower not in WORD_MAP:
                brief_id = candidate_orig  # preserve original case for ID lookup
                ack_idx = i
                break

    # Remove "ack <id>" from tokens for code parsing
    clean_tokens = list(tokens)
    if ack_idx is not None:
        clean_tokens = clean_tokens[:ack_idx] + clean_tokens[ack_idx + 2:]

    # Check for "all" flag
    all_flag = "all" in clean_tokens
    if all_flag:
        clean_tokens = [t for t in clean_tokens if t != "all"]

    # Extract codes from remaining tokens
    codes = []

    # Expand ranges like "1-3"
    expanded = []
    for t in clean_tokens:
        range_match = re.match(r"^([1-5])-([1-5])$", t)
        if range_match:
            lo, hi = int(range_match.group(1)), int(range_match.group(2))
            expanded.extend([str(n) for n in range(lo, hi + 1)])
        else:
            expanded.append(t)

    for t in expanded:
        if re.match(r"^[1-5]$", t):
            codes.append(t)
        elif t in WORD_MAP:
            codes.append(WORD_MAP[t])

    # Deduplicate preserving order
    seen = set()
    unique_codes = []
    for c in codes:
        if c not in seen:
            seen.add(c)
            unique_codes.append(c)

    if not unique_codes and not all_flag:
        return None

    return {
        "recognized": True,
        "codes": unique_codes,
        "brief_id": brief_id,
        "all_flag": all_flag,
    }


def _write_approval(brief, code, prompt_snippet):
    """Write approval JSON with filelock. Return True on success."""
    os.makedirs(APPROVALS_DIR, exist_ok=True)
    os.makedirs(ARCHIVE_DIR, exist_ok=True)

    approval = {
        "brief_id": brief["id"],
        "code": code,
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        "user_prompt_snippet": prompt_snippet[:200],
    }

    approval_path = os.path.join(APPROVALS_DIR, f"{brief['id']}.json")

    # Filelock: exclusive lock on sentinel file
    lock_fd = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        # Another process holds lock — wait up to 2s
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
        except Exception as e:
            print(f"[inbox_ack] WARN: could not acquire lock: {e}", file=sys.stderr)
            lock_fd.close()
            return False

    try:
        with open(approval_path, "w") as f:
            json.dump(approval, f, indent=2)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()

    return True


def _archive_brief(brief_path):
    """Move brief file to archive/. Return True on success."""
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    filename = os.path.basename(brief_path)
    dest = os.path.join(ARCHIVE_DIR, filename)
    try:
        shutil.move(brief_path, dest)
        return True
    except Exception as e:
        print(f"[inbox_ack] WARN: could not archive {brief_path}: {e}", file=sys.stderr)
        return False


def _resolve_code_to_action(brief, code):
    """Find action in brief matching code. Return action dict or None."""
    for action in brief.get("actions", []):
        if action.get("code") == code:
            return action
    return None


def main():
    _t0 = log_fire(__file__)
    try:
        # Read stdin JSON (UserPromptSubmit payload)
        try:
            payload = json.load(sys.stdin)
        except Exception:
            log_fire_done(__file__, _t0, errored=False, output_size_bytes=2)
            print(json.dumps({}))
            return

        prompt = payload.get("prompt", "")
        if not prompt:
            log_fire_done(__file__, _t0, errored=False, output_size_bytes=2)
            print(json.dumps({}))
            return

        parsed = _parse_prompt(prompt)

        if parsed is None:
            # Not an ack — pass through silently
            log_fire_done(__file__, _t0, errored=False, output_size_bytes=2)
            print(json.dumps({}))
            return

        # Load briefs
        all_briefs = _load_all_briefs()

        # Resolve target brief(s)
        if parsed["brief_id"]:
            # Scoped to a specific brief
            brief_path, brief = _load_brief_by_id(parsed["brief_id"])
            if brief is None:
                print(f"[inbox_ack] WARN: brief_id '{parsed['brief_id']}' not found", file=sys.stderr)
                log_fire_done(__file__, _t0, errored=False, output_size_bytes=2)
                print(json.dumps({}))
                return
            targets = [(brief_path, brief)]
        elif parsed["all_flag"]:
            targets = all_briefs
        else:
            # Apply to oldest brief (critical > daily > weekly)
            oldest_path, oldest = _oldest_brief(all_briefs)
            if oldest is None:
                log_fire_done(__file__, _t0, errored=False, output_size_bytes=2)
                print(json.dumps({}))
                return
            targets = [(oldest_path, oldest)]

        prompt_snippet = prompt[:200]
        acked = []

        for brief_path, brief in targets:
            # Determine which code to apply
            codes = parsed["codes"]

            if not codes:
                # "all" with no digit code: apply first action code for each brief
                first_action = brief["actions"][0] if brief["actions"] else None
                if first_action:
                    codes = [first_action["code"]]

            for code in codes:
                action = _resolve_code_to_action(brief, code)
                if action is None:
                    # Normalize word codes to digit if possible
                    # (e.g. "approve" might map to action code "1" in the brief)
                    # Try matching label keywords
                    word_to_try = None
                    if code == "approve":
                        word_to_try = "1"
                    elif code == "defer":
                        word_to_try = "2"
                    elif code == "skip":
                        word_to_try = "3"
                    if word_to_try:
                        action = _resolve_code_to_action(brief, word_to_try)

                if action is None:
                    print(f"[inbox_ack] WARN: unrecognized code '{code}' for brief '{brief['id']}'", file=sys.stderr)
                    continue

                ok = _write_approval(brief, action["code"], prompt_snippet)
                if ok:
                    _archive_brief(brief_path)
                    acked.append({"brief_id": brief["id"], "code": action["code"], "label": action["label"]})
                    # Only one code per brief (first match wins)
                    break

        if acked:
            # Inject context so Claude sees the ack result
            lines = ["<inbox-ack>"]
            lines.append(f"Acknowledged {len(acked)} brief(s):")
            for a in acked:
                lines.append(f"  [{a['code']}] {a['brief_id']} — {a['label']}")
            lines.append("</inbox-ack>")
            context = "\n".join(lines)
            out = json.dumps({"additionalContext": context})
            log_fire_done(__file__, _t0, errored=False, output_size_bytes=len(out))
            print(out)
        else:
            log_fire_done(__file__, _t0, errored=False, output_size_bytes=2)
            print(json.dumps({}))
    except Exception as e:
        log_fire_done(__file__, _t0, errored=True, output_size_bytes=0)
        print(f"[inbox_ack] error: {e}", file=sys.stderr)
        print(json.dumps({}))


if __name__ == "__main__":
    _raw_stdin = sys.stdin.read()
    try:
        _hook_input = json.loads(_raw_stdin)
        _prompt = _hook_input.get("prompt", "")
    except Exception:
        _hook_input = {}
        _prompt = ""
    sys.stdin = io.StringIO(_raw_stdin)
    if not should_fire(__file__, _prompt):
        print(json.dumps({}))
        sys.exit(0)
    main()

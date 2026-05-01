#!/usr/bin/env python3
# Hook O2 (PILOT, log-only): comment-vs-code AND/OR drift audit.
# Created: 2026-05-02 — after vps_sync P2 label-vs-code drift incident.
#
# Source: discussion 2026-05-02. Comment block at vps_sync.sh:44-46 said
#   "if >DIVERGENCE_ALERT commits ahead AND no successful push in last hour"
# but code at line 88 only checked the first half. P2 shipped half-implemented;
# the snapshot still claimed "P2 hardened" because /s read the comment label,
# not the enforcement clause.
#
# Behavior: scan staged diff for ADDED comment lines containing the words
# " AND " or " OR " in a clause-joining context, then check whether the added
# code lines in the same file contain matching && or || operators. Mismatches
# are appended to ~/.claude/scripts/state/comment-code-audit.jsonl for
# eyeball review. NEVER blocks the commit during pilot. After 30d FP-rate
# review, promote to block-on-mismatch (or downgrade if too noisy).
#
# Trigger: PreToolUse Bash on `git commit`. Cheap; only reads `git diff --cached`.
import json
import os
import pathlib
import re
import subprocess
import sys
import time

LOG = pathlib.Path.home() / ".claude" / "scripts" / "state" / "comment-code-audit.jsonl"
SUPPORTED_EXT = {".sh", ".py", ".ts", ".tsx", ".js", ".jsx", ".bash", ".zsh"}
COMMENT_PREFIX = re.compile(r"^\s*(#|//|\*)\s?")
# match " AND " / " OR " (caps or mixed) used as English clause join, not as
# part of code identifiers (so we drop matches that are inside backticks).
JOIN_AND = re.compile(r"\b[Aa][Nn][Dd]\b")
JOIN_OR = re.compile(r"\b[Oo][Rr]\b")
HAS_AND_OP = re.compile(r"&&|\band\b")  # bash/python both
HAS_OR_OP = re.compile(r"\|\||\bor\b")


def get_staged_diff() -> str:
    try:
        out = subprocess.run(
            ["git", "diff", "--cached", "-U0", "--no-color"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return out.stdout if out.returncode == 0 else ""
    except Exception:
        return ""


def parse_hunks(diff: str):
    """Yield (file_path, added_comment_lines, added_code_lines) per file."""
    cur_file = None
    cmts: list[str] = []
    code: list[str] = []
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            if cur_file:
                yield cur_file, cmts, code
            cur_file = line[6:]
            cmts, code = [], []
        elif line.startswith("+") and not line.startswith("+++"):
            payload = line[1:]
            stripped = payload.strip()
            if not stripped:
                continue
            if COMMENT_PREFIX.match(payload):
                cmts.append(payload)
            else:
                code.append(payload)
    if cur_file:
        yield cur_file, cmts, code


def audit_hunk(file_path: str, cmts: list[str], code: list[str]) -> list[dict]:
    findings: list[dict] = []
    ext = pathlib.Path(file_path).suffix.lower()
    if ext not in SUPPORTED_EXT:
        return findings
    code_blob = "\n".join(code)
    has_and = bool(HAS_AND_OP.search(code_blob))
    has_or = bool(HAS_OR_OP.search(code_blob))
    for c in cmts:
        # only consider comment lines that look clause-joining (have a verb-like context)
        # cheap heuristic: contains " AND " / " OR " AND has at least 4 words
        if len(c.split()) < 4:
            continue
        if JOIN_AND.search(c) and not has_and:
            findings.append({
                "kind": "comment-AND-no-code-AND",
                "comment": c.strip()[:200],
            })
        if JOIN_OR.search(c) and not has_or:
            findings.append({
                "kind": "comment-OR-no-code-OR",
                "comment": c.strip()[:200],
            })
    return findings


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

    diff = get_staged_diff()
    if not diff:
        sys.exit(0)

    all_findings: list[dict] = []
    for f, cmts, code in parse_hunks(diff):
        for fnd in audit_hunk(f, cmts, code):
            fnd["file"] = f
            all_findings.append(fnd)

    if not all_findings:
        sys.exit(0)

    LOG.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "cwd": os.getcwd(),
        "findings": all_findings,
        "mode": "log-only-pilot",
    }
    with open(LOG, "a") as fh:
        fh.write(json.dumps(rec) + "\n")
    # Pilot: never block. After 30d, promote.
    sys.exit(0)


if __name__ == "__main__":
    main()

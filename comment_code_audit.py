#!/usr/bin/env python3
# Hook D17: comment-vs-code AND/OR drift audit (PROMOTED 2026-05-02 from log-only pilot to block-on-mismatch).
# Created: 2026-05-02 — after vps_sync P2 label-vs-code drift incident.
# Promoted: 2026-05-02 16:35 HKT after H11 word-count gate eliminated 100% of FP shapes
# in 14h backtest (8 hits → 2 TP / 6 FP under H0; 2 TP / 0 FP under H11).
#
# Source: discussion 2026-05-02. Comment block at vps_sync.sh:44-46 said
#   "if >DIVERGENCE_ALERT commits ahead AND no successful push in last hour"
# but code at line 88 only checked the first half. P2 shipped half-implemented;
# the snapshot still claimed "P2 hardened" because /s read the comment label,
# not the enforcement clause.
#
# H11 detection rule (each side of AND/OR conjunction needs ≥3 substantive words):
#  - Aggregate consecutive added comment lines into a BLOCK (handles AND-at-EOL multi-line).
#  - Find first " AND " / " OR " conjunction in block (case-insensitive, word-bounded).
#  - Split block at conjunction position; count substantive words on each side.
#  - Both sides must have ≥3 words → boolean clause-pair shape (not English verb-list).
#  - Then check: code-block in same file diff lacks `&&` / `||` (or `and` / `or` keywords).
#
# Bypass: include `[skip-comment-audit=<reason>]` in commit message subject. Logged.
#
# Trigger: PreToolUse Bash on `git commit`. Cheap; only reads `git diff --cached`.
# Cross-references:
#  - rules/disciplines/comment-vs-code-drift.md (D17 — same logic at /ship LAND via RC-11)
#  - rules/disciplines/_index.md (D17 row)
#  - skills/ship/phases/common/realization-checks.md (RC-9 — LAND-time twin)
import argparse
import json
import os
import pathlib
import re
import subprocess
import sys
import time

LOG = pathlib.Path.home() / ".claude" / "scripts" / "state" / "comment-code-audit.jsonl"
SKIP_LOG = pathlib.Path.home() / ".claude" / "scripts" / "state" / "comment-code-audit-skips.jsonl"
SUPPORTED_EXT = {".sh", ".py", ".ts", ".tsx", ".js", ".jsx", ".bash", ".zsh"}
COMMENT_PREFIX = re.compile(r"^\s*(#|//|\*)\s?")
# AND/OR matchers — must be space-padded (rejects hyphenated compounds like "fire-and-forget").
# Use lookarounds for whitespace so the match doesn't consume separators.
JOIN_AND = re.compile(r"(?<=\s)(?:AND|And|and)(?=\s)")
JOIN_OR = re.compile(r"(?<=\s)(?:OR|Or|or)(?=\s)")
HAS_AND_OP = re.compile(r"&&|\band\b")  # bash/python boolean operators
HAS_OR_OP = re.compile(r"\|\||\bor\b")
WORD = re.compile(r"\b\w+\b")
# Metadata-block suppression: detector docstrings + module headers have ≥3 lines like
# "key: value" or "key-name: value". Skip those blocks (they're documentation, not clause-pairs).
META_KV_LINE = re.compile(r"^\s*[A-Za-z][\w-]*:\s+\S")
MIN_SIDE_WORDS = 3   # H11 threshold — both sides of AND/OR conjunction must have ≥3 words
MIN_META_KV_LINES = 3  # block with ≥this many KV lines is metadata, skip


def words_count(s: str) -> int:
    """Count substantive words (alphanumeric tokens, excluding pure-punctuation)."""
    return len(WORD.findall(s))


def strip_comment_marker(line: str) -> str:
    """Strip leading whitespace + #/// marker from a comment line."""
    return COMMENT_PREFIX.sub("", line, count=1).rstrip()


def get_diff(range_spec: str | None = None) -> str:
    """Return unified diff. range_spec=None → staged (`--cached`); else `git diff <range>`."""
    args = ["git", "diff", "-U0", "--no-color"]
    if range_spec is None:
        args.append("--cached")
    else:
        args.append(range_spec)
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=5)
        return out.stdout if out.returncode == 0 else ""
    except Exception:
        return ""


def parse_hunks(diff: str):
    """Yield (file_path, comment_blocks, added_code_lines) per file.

    A 'comment_block' is a list of consecutive added comment lines (joined
    later into one logical paragraph for clause-pair detection).
    """
    cur_file = None
    blocks: list[list[str]] = []
    cur_block: list[str] = []
    code: list[str] = []

    def flush_block():
        if cur_block:
            blocks.append(cur_block.copy())
            cur_block.clear()

    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            flush_block()
            if cur_file:
                yield cur_file, blocks, code
            cur_file = line[6:]
            blocks = []
            cur_block = []
            code = []
        elif line.startswith("@@ "):
            # hunk header — break comment-block continuity
            flush_block()
        elif line.startswith("+") and not line.startswith("+++"):
            payload = line[1:]
            if not payload.strip():
                # blank line breaks comment-block continuity
                flush_block()
                continue
            if COMMENT_PREFIX.match(payload):
                cur_block.append(payload)
            else:
                flush_block()
                code.append(payload)
        else:
            # context / removal lines — break block continuity
            flush_block()
    flush_block()
    if cur_file:
        yield cur_file, blocks, code


def audit_block_text(block_text: str, code_blob: str) -> list[dict]:
    """Return findings for a single comment block joined into one paragraph.

    H11: each side of the first AND/OR conjunction must have ≥MIN_SIDE_WORDS words.
    """
    findings: list[dict] = []
    has_and_op = bool(HAS_AND_OP.search(code_blob))
    has_or_op = bool(HAS_OR_OP.search(code_blob))

    if len(block_text.split()) < 6:
        return findings  # too short for any meaningful clause-pair

    for kind, pattern, has_op in [
        ("comment-AND-no-code-AND", JOIN_AND, has_and_op),
        ("comment-OR-no-code-OR", JOIN_OR, has_or_op),
    ]:
        if has_op:
            continue
        m = pattern.search(block_text)
        if not m:
            continue
        left = block_text[:m.start()]
        right = block_text[m.end():]
        if words_count(left) >= MIN_SIDE_WORDS and words_count(right) >= MIN_SIDE_WORDS:
            findings.append({
                "kind": kind,
                "block": block_text[:300],
                "left_words": words_count(left),
                "right_words": words_count(right),
            })
    return findings


def is_metadata_block(block_lines: list[str]) -> bool:
    """Detector metadata / module-header docstrings have ≥3 KV-shaped lines.
    Example pattern that triggers suppression:
        # detector: foo_scan
        # emits_types: [a, b]
        # covers: [F1, F10]
        # severity: HIGH
    """
    kv_count = 0
    for line in block_lines:
        stripped = strip_comment_marker(line)
        if META_KV_LINE.match(stripped):
            kv_count += 1
            if kv_count >= MIN_META_KV_LINES:
                return True
    return False


def audit_file(file_path: str, blocks: list[list[str]], code: list[str]) -> list[dict]:
    findings: list[dict] = []
    ext = pathlib.Path(file_path).suffix.lower()
    if ext not in SUPPORTED_EXT:
        return findings
    code_blob = "\n".join(code)
    for block in blocks:
        if is_metadata_block(block):
            continue  # detector docstring / module header — not a clause-pair claim
        # join multi-line comment block into one paragraph
        block_text = " ".join(strip_comment_marker(line) for line in block).strip()
        for fnd in audit_block_text(block_text, code_blob):
            fnd["file"] = file_path
            findings.append(fnd)
    return findings


def extract_commit_message(cmd: str) -> str:
    m = re.search(r"-m\s+(['\"])(.+?)\1", cmd, re.DOTALL)
    if m:
        return m.group(2)
    m = re.search(r"--message[= ]+(['\"])(.+?)\1", cmd, re.DOTALL)
    if m:
        return m.group(2)
    return ""


def log_skip(reason: str, findings: list[dict]) -> None:
    SKIP_LOG.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "reason": reason,
        "cwd": os.getcwd(),
        "findings_count": len(findings),
    }
    with open(SKIP_LOG, "a") as fh:
        fh.write(json.dumps(rec) + "\n")


def log_block(findings: list[dict], decision: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "cwd": os.getcwd(),
        "decision": decision,
        "findings": findings,
        "mode": "block-on-mismatch",
    }
    with open(LOG, "a") as fh:
        fh.write(json.dumps(rec) + "\n")


def run_cli(range_spec: str, strict: bool) -> None:
    """CLI mode for /ship RC-9 LAND-time twin. Exits nonzero on findings when --strict."""
    diff = get_diff(range_spec)
    if not diff:
        print(f"comment_code_audit: empty diff for range '{range_spec}'", file=sys.stderr)
        sys.exit(0)
    findings: list[dict] = []
    for f, blocks, code in parse_hunks(diff):
        findings.extend(audit_file(f, blocks, code))
    if not findings:
        print(f"comment_code_audit: 0 findings on {range_spec}", file=sys.stderr)
        sys.exit(0)
    print(
        f"comment_code_audit: {len(findings)} D17 finding(s) on {range_spec}",
        file=sys.stderr,
    )
    for fnd in findings[:10]:
        print(
            f"  • {fnd['file']}: {fnd['kind']} "
            f"({fnd['left_words']}+{fnd['right_words']} words)",
            file=sys.stderr,
        )
        print(f"    {fnd['block'][:160]}", file=sys.stderr)
    if len(findings) > 10:
        print(f"  ... +{len(findings) - 10} more", file=sys.stderr)
    log_block(findings, decision="cli-strict" if strict else "cli-report")
    sys.exit(1 if strict else 0)


def main() -> None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--diff", dest="range_spec", default=None,
                        help="Git range (e.g. 'main..HEAD'). CLI mode for /ship RC-9.")
    parser.add_argument("--strict", action="store_true",
                        help="Exit nonzero on findings (block phase close).")
    args, _ = parser.parse_known_args()
    if args.range_spec is not None:
        run_cli(args.range_spec, args.strict)
        return

    try:
        data = json.loads(sys.stdin.read())
    except Exception:
        sys.exit(0)
    if data.get("tool_name") != "Bash":
        sys.exit(0)
    cmd = (data.get("tool_input") or {}).get("command", "") or ""
    if not re.search(r"\bgit\s+commit\b", cmd):
        sys.exit(0)

    diff = get_diff(None)
    if not diff:
        sys.exit(0)

    all_findings: list[dict] = []
    for f, blocks, code in parse_hunks(diff):
        all_findings.extend(audit_file(f, blocks, code))

    if not all_findings:
        sys.exit(0)

    msg = extract_commit_message(cmd)
    skip_match = re.search(r"\[skip-comment-audit=([^\]]+)\]", msg)
    if skip_match:
        log_skip(skip_match.group(1), all_findings)
        sys.exit(0)

    # Block the commit. Output JSON decision per Claude hook contract.
    log_block(all_findings, decision="block")
    fingerprints = []
    for f in all_findings[:5]:
        fingerprints.append(f"  • {f['file']}: {f['kind']}")
        fingerprints.append(f"    block: {f['block'][:140]}...")
    extra = f"\n  ... +{len(all_findings) - 5} more" if len(all_findings) > 5 else ""
    reason = (
        f"D17 comment-vs-code drift detected: comment uses 'AND'/'OR' clause-pair "
        f"({all_findings[0]['left_words']}+{all_findings[0]['right_words']} words) "
        f"but code in same file lacks matching boolean operator.\n\n"
        + "\n".join(fingerprints) + extra +
        "\n\nFix: implement the missing clause OR rephrase the comment to verb-list shape.\n"
        "Bypass: add `[skip-comment-audit=<reason>]` to commit message subject "
        "(logged to ~/.claude/scripts/state/comment-code-audit-skips.jsonl).\n"
        "Source: rules/disciplines/comment-vs-code-drift.md (D17)."
    )
    print(json.dumps({"decision": "block", "reason": reason}))
    sys.exit(0)


if __name__ == "__main__":
    main()

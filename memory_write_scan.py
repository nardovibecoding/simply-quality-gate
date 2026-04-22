#!/usr/bin/env python3
"""PostToolUse hook: scan Write/Edit to memory+wiki files for prompt-injection patterns.

Reuses PROMPT_INJECTION_PATTERNS from skill-security-auditor (CRITICAL only).
Non-blocking: logs hits to ~/.claude/logs/memory_write_scan.log.
Daily visibility provided by memory_scan_status.py (UserPromptSubmit hook).
"""
import json
import re
import sys
from datetime import datetime
from pathlib import Path

LOG_FILE = Path.home() / ".claude" / "logs" / "memory_write_scan.log"

MEMORY_ROOTS = [
    Path.home() / ".claude" / "projects" / "-Users-bernard" / "memory",
    Path.home() / ".claude" / "projects" / "-Users-bernard-polymarket-bot" / "memory",
    Path.home() / "NardoWorld",
]

# Skip filenames that are legitimately about injection (defensive docs).
SKIP_NAME_TOKENS = ("lesson", "security", "injection", "attack", "audit", "threat", "red-team", "prompt-inject")

# CRITICAL patterns only — reused from skill-security-auditor.
PATTERNS = [
    (re.compile(r"(?i)ignore\s+(?:all\s+)?(?:previous|prior|above)\s+instructions"), "PROMPT-OVERRIDE"),
    (re.compile(r"(?i)you\s+are\s+now\s+(?:a|an|the)\s+"), "ROLE-HIJACK"),
    (re.compile(r"(?i)(?:disregard|forget|override)\s+(?:your|all|any)\s+(?:instructions|rules|guidelines|constraints|safety)"), "OVERRIDE-RULES"),
    (re.compile(r"(?i)(?:pretend|act\s+as\s+if|imagine)\s+you\s+(?:have\s+no|don'?t\s+have\s+any)\s+(?:restrictions|limits|rules|safety)"), "SAFETY-BYPASS"),
]

# Strip markdown code fences + inline code + blockquotes before scanning.
_FENCE_RE = re.compile(r"```[\s\S]*?```", re.MULTILINE)
_INLINE_RE = re.compile(r"`[^`\n]*`")
_QUOTE_RE = re.compile(r"^>.*$", re.MULTILINE)


def _is_memory_path(p: str) -> bool:
    try:
        rp = Path(p).resolve()
    except Exception:
        return False
    for root in MEMORY_ROOTS:
        try:
            rp.relative_to(root.resolve())
            return True
        except ValueError:
            continue
    return False


def _skip_by_name(p: str) -> bool:
    name = Path(p).name.lower()
    return any(tok in name for tok in SKIP_NAME_TOKENS)


def _strip_code_and_quotes(text: str) -> str:
    text = _FENCE_RE.sub("", text)
    text = _INLINE_RE.sub("", text)
    text = _QUOTE_RE.sub("", text)
    return text


def _scan(text: str):
    stripped = _strip_code_and_quotes(text)
    hits = []
    for pat, cat in PATTERNS:
        m = pat.search(stripped)
        if m:
            hits.append((cat, m.group(0)[:120], m.start()))
    if not hits:
        return []
    # Educational/doc heuristic: if hits span >1000 chars apart, likely a doc
    # discussing multiple patterns, not a real attack (which is usually one
    # concentrated string). Suppress unless all hits are within 500-char span.
    if len(hits) >= 3:
        positions = [h[2] for h in hits]
        if max(positions) - min(positions) > 500:
            return []
    return [(cat, snip) for cat, snip, _ in hits]


def _log(tool: str, path: str, hits):
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().isoformat(timespec="seconds")
    with LOG_FILE.open("a") as f:
        for cat, snippet in hits:
            f.write(f"{ts} | {tool} | {path} | {cat} | {snippet}\n")


def main():
    try:
        event = json.load(sys.stdin)
    except Exception:
        return

    tool = event.get("tool_name", "")
    if tool not in ("Write", "Edit"):
        return

    inp = event.get("tool_input", {})
    path = inp.get("file_path", "")
    if not path or not _is_memory_path(path):
        return
    if _skip_by_name(path):
        return

    content = inp.get("content") if tool == "Write" else inp.get("new_string")
    if not content or not isinstance(content, str):
        return

    hits = _scan(content)
    if not hits:
        return

    _log(tool, path, hits)


if __name__ == "__main__":
    main()

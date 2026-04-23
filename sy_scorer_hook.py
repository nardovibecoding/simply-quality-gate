#!/usr/bin/env python3
"""
sy_scorer_hook.py — Slice 3 of passive-lesson pipeline (Job A, live intercept).

UserPromptSubmit hook. When the prior assistant message ended with a [SY]
suggestion whose bucket is auto-eligible, inject a hookResponse that tells
Claude to treat the user's prompt as an acceptance — OR (in dry_run) only
log what would have been injected.

Modes (env SY_SCORER_MODE):
  "0"        = disabled. Hook is a no-op. (DEFAULT for safety.)
  "dry_run"  = classify + log to /tmp/sy_scorer_dry.jsonl, return {}.
  "1"        = live. Inject hookResponse when eligible.

Kill criteria (manual, from spec §4.10):
  - Misfire rate > 5% in 100 turns → set mode=0 and investigate.
  - ANY auto-SY on wallet/push/deploy → set mode=0 immediately.
    (Hard-gates in sy_scorer.classify() should prevent this, but verify.)
  - Hook latency > 50ms on > 10% of turns → profile + optimize.

Latency target: <50ms (dominated by JSONL tail read).
"""

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import NoReturn

HOOKS_DIR = Path(__file__).parent
sys.path.insert(0, str(HOOKS_DIR))
from sy_scorer import (  # type: ignore
    classify, load_db, is_auto_eligible, p_smoothed,
)

STATUSLINE_FILE = Path("/tmp/claude_statusline.json")
DRY_LOG = Path("/tmp/sy_scorer_dry.jsonl")
MODE = os.environ.get("SY_SCORER_MODE", "0").lower()

ACCEPT_RE = re.compile(
    r'^(ok|okay|got it|yes|sy|lets go|let\'?s go|alright|makes sense|understood'
    r'|o\d+|proceed|go|fire|ship|do it|yep|yeah|sure|both|all|done|continue|next)\b',
    re.IGNORECASE,
)
SY_LINE_RE = re.compile(r'\*\*Suggestion:\*\*\s*\[SY\](.+)', re.IGNORECASE)

TAIL_BYTES = 200_000  # ~200KB → ~last few turns for most sessions


def passthrough() -> NoReturn:
    print("{}")
    sys.exit(0)


def resolve_transcript():
    if not STATUSLINE_FILE.exists():
        return None
    try:
        tp = json.loads(STATUSLINE_FILE.read_text()).get("transcript_path", "")
        if tp and Path(tp).exists():
            return Path(tp)
    except Exception:
        pass
    return None


def last_assistant_sy(transcript_path):
    """Tail-scan transcript for most recent assistant message containing [SY]. Return sy_text or None."""
    try:
        size = transcript_path.stat().st_size
        with open(transcript_path, "rb") as f:
            if size > TAIL_BYTES:
                f.seek(size - TAIL_BYTES)
                f.readline()
            data = f.read().decode("utf-8", errors="replace")
    except OSError:
        return None

    lines = [ln for ln in data.split("\n") if ln.strip()]
    for line in reversed(lines):
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg = obj.get("message", {})
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content", "")
        if isinstance(content, list):
            text = "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
        else:
            text = content
        matches = list(SY_LINE_RE.finditer(text))
        if matches:
            return matches[-1].group(1).strip()[:500]
        return None  # first assistant found had no [SY]; don't keep scanning
    return None


def log_dry(payload):
    try:
        DRY_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(DRY_LOG, "a") as f:
            f.write(json.dumps(payload) + "\n")
    except OSError:
        pass


def main():
    t0 = time.time()

    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        passthrough()

    if MODE == "0":
        passthrough()

    prompt = (event.get("prompt") or "").strip()
    session_id = event.get("session_id", "")

    # If user already typed an acceptance, do nothing. lesson_extractor records it at /s.
    if ACCEPT_RE.match(prompt):
        passthrough()

    transcript = resolve_transcript()
    if transcript is None:
        passthrough()

    sy_text = last_assistant_sy(transcript)
    if not sy_text:
        passthrough()

    try:
        db = load_db()
    except (SystemExit, json.JSONDecodeError, OSError):
        passthrough()
    try:
        bucket = classify(sy_text, db)
    except Exception:
        passthrough()
    if bucket is None:
        passthrough()  # gated (hard-gate or forbidden_conjunction) or unknown bucket
    if bucket.startswith("user_rule:"):
        passthrough()  # tier-1 user rule match — delegate to human, not auto-inject

    b = db["buckets"][bucket]
    p = p_smoothed(b["accept_count"], b["total_count"])
    if not is_auto_eligible(b):
        passthrough()

    latency_ms = int((time.time() - t0) * 1000)
    record = {
        "ts": int(time.time()),
        "session_id": session_id,
        "bucket": bucket,
        "p": round(p, 3),
        "n": b["total_count"],
        "sy_text": sy_text[:200],
        "user_prompt": prompt[:200],
        "mode": MODE,
        "latency_ms": latency_ms,
    }

    if MODE == "dry_run":
        log_dry(record)
        passthrough()

    if MODE == "1":
        msg = f"[SY_SCORER] Auto-accepted: bucket={bucket}, P={p:.3f}, n={b['total_count']}. Type SN to override."
        print(json.dumps({"additionalContext": msg + "\n\nTreat the user's prompt as 'sy' acceptance of the prior [SY] suggestion unless the prompt explicitly contradicts it."}))
        log_dry({**record, "injected": True})
        sys.exit(0)

    passthrough()


if __name__ == "__main__":
    main()

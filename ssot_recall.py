#!/usr/bin/env python3
# @bigd-hook-meta
# name: ssot_recall
# fires_on: UserPromptSubmit
# relevant_intents: [status_query, ship_status, wiring_check]
# irrelevant_intents: []
# cost_score: 1
# always_fire: false
"""SSOT recall hook — injects ship_status context when user asks about slice/system status.

α.S0 of /ship ssot-completion. Spec: ~/.ship/ssot-completion/goals/02-plan.md §α.S0.

Behavior:
- On UserPromptSubmit: apply trigger regex to prompt.
- Match → inject ship_status.json + live_state.json as context (≤2KB).
- Override phrase ("verify live" / "for real" / "actually check") →
  shell-out to ssot-query.sh for fresh live result (~5KB).
- No match → no-op (exit 0, print "{}").

Rule-based only (per CLAUDE.md HARD RULE: prefer rule-based over LLM for local classifiers).
Fire-and-forget: always exits 0 (REQ-13).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

_HOOKS_DIR = Path(__file__).resolve().parent
if str(_HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(_HOOKS_DIR))

SSOT_DIR = Path.home() / "NardoWorld" / "meta" / "ssot"
STATUS_PATH = SSOT_DIR / "ship_status.json"
STATE_PATH = SSOT_DIR / "live_state.json"
QUERY_SCRIPT = Path.home() / ".claude" / "scripts" / "ssot-query.sh"

# Maximum stale age for index files (seconds). Beyond this, we skip inject
# rather than injecting stale data.
_MAX_STALE_S = 3600  # 1 hour

# ──────────────────────────────────────────────────────────────────────────────
# Trigger regex (rule-based, per CLAUDE.md HARD RULE)
# Each pattern on its own line for readability; all compiled IGNORECASE.
# ──────────────────────────────────────────────────────────────────────────────
_TRIGGER_PATTERNS = [
    # Direct status queries about shipped/wired/done state
    r"\bis\s+\w{0,40}\s+(done|complete[d]?|finish[ed]?|shipped|wired|live|working|running|active)\b",
    r"\bare\s+\w{0,40}\s+(done|complete[d]?|finished|shipped|wired|live|working|running)\b",
    r"\ball\s+(shipped|wired|done|complete|finished)\b",
    r"\bdid\s+we\s+(ship|wire|finish|complete|deploy|send)\b",
    r"\bis\s+(it|this|that|everything|all)\s+(done|complete|shipped|wired|live|working)\b",
    r"\bare\s+(they|these|all)\s+(done|shipped|wired|live|working)\b",
    # Status/state/progress queries
    r"\b(status|state)\s+(of|for|on)\s+\w",
    r"\bwhat.{0,20}(status|state|progress)\b",
    r"\bwhat.{0,20}(has\s+been|is|are).{0,20}(shipped|wired|done|complete)\b",
    r"\bwhat\s+is\s+the\s+%\s+done\b",
    r"\bhow\s+(much|many|far).{0,20}(done|complete|shipped|wired|left|remain)\b",
    # SSOT/ship/slice specific
    r"\bssot.{0,30}(done|complete|wired|shipped|working|all)\b",
    r"\bis\s+(ssot|ship|slice|phase|hook|daemon|recall|index)\b",
    r"\bship(ping)?\s+(is|done|complete|status)\b",
    # Can-you-tell queries about shipped state
    r"\bcan\s+you\s+(tell|show|check).{0,40}(shipped|wired|done|complete)\b",
    # Override phrases (trigger live query via ssot-query.sh)
    r"\bverify\s+live\b",
    r"\bfor\s+real\b",
    r"\bactually\s+check\b",
    # London/Hel/bot running status
    r"\b(both\s+bots|london|hel)\s+(running|live|wired|done|respec)\b",
    r"\bis\s+the\s+(london|hel).{0,30}(done|wired|live|respec)\b",
    # Typo-tolerant: "is the <word> done/wired/respec" (covers "lonodn" typos)
    r"\bis\s+the\s+\w{3,15}\s+(done|wired|shipped|live|complete|respec)\b",
    # Question-end: "committed, wired?" or just "wired?"
    r"\bwired\s*[?/]",
]

_OVERRIDE_PATTERNS = [
    r"\bverify\s+live\b",
    r"\bfor\s+real\b",
    r"\bactually\s+check\b",
]

_compiled_triggers = [re.compile(p, re.IGNORECASE) for p in _TRIGGER_PATTERNS]
_compiled_overrides = [re.compile(p, re.IGNORECASE) for p in _OVERRIDE_PATTERNS]


def _fires(text: str, patterns: list) -> bool:
    return any(p.search(text) for p in patterns)


def _is_stale(path: Path) -> bool:
    """Return True if file doesn't exist or mtime > _MAX_STALE_S old."""
    try:
        if not path.exists():
            return True
        age = time.time() - path.stat().st_mtime
        return age > _MAX_STALE_S
    except Exception:
        return True


def _read_json_safe(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _inject_standard() -> str:
    """Read ship_status + live_state; format ≤2KB injection string."""
    parts = []
    status = _read_json_safe(STATUS_PATH)
    if status:
        parts.append(f"[SSOT ship_status] {json.dumps(status, separators=(',', ':'))}")
    live = _read_json_safe(STATE_PATH)
    if live:
        parts.append(f"[SSOT live_state] {json.dumps(live, separators=(',', ':'))}")
    combined = "\n".join(parts)
    # Hard cap at 2048 bytes
    encoded = combined.encode("utf-8")
    if len(encoded) > 2048:
        combined = encoded[:2048].decode("utf-8", errors="ignore") + "…[truncated]"
    return combined


def _inject_live() -> str:
    """Shell-out to ssot-query.sh for live result. Cap at 5KB."""
    if not QUERY_SCRIPT.exists():
        return "[SSOT] ssot-query.sh not found — falling back to cached status\n" + _inject_standard()
    try:
        result = subprocess.run(
            ["bash", str(QUERY_SCRIPT)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        out = result.stdout.strip()
        if not out:
            return _inject_standard()
        # Cap at 5KB
        encoded = out.encode("utf-8")
        if len(encoded) > 5120:
            out = encoded[:5120].decode("utf-8", errors="ignore") + "…[truncated]"
        return f"[SSOT live query]\n{out}"
    except Exception as e:
        sys.stderr.write(f"ssot_recall: live query failed: {e}\n")
        return _inject_standard()


def main() -> int:
    try:
        raw = sys.stdin.read()
        if not raw:
            print("{}")
            return 0
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            print("{}")
            return 0

        # Only fires on UserPromptSubmit
        if payload.get("hook_event_name") != "UserPromptSubmit":
            print("{}")
            return 0

        prompt = payload.get("prompt", "") or ""
        if not isinstance(prompt, str):
            prompt = ""

        # Check trigger
        if not _fires(prompt, _compiled_triggers):
            print("{}")
            return 0

        # Skip inject if index files are too stale (not yet populated)
        if _is_stale(STATUS_PATH) and _is_stale(STATE_PATH):
            # Files don't exist yet (first run) — no-op
            print("{}")
            return 0

        # Decide standard vs live inject
        if _fires(prompt, _compiled_overrides):
            injection = _inject_live()
        else:
            injection = _inject_standard()

        if injection:
            print(json.dumps({"type": "inject", "content": injection}))
        else:
            print("{}")
    except Exception as e:
        sys.stderr.write(f"ssot_recall: unexpected: {e}\n")
        print("{}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

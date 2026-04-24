#!/usr/bin/env python3
"""
_semantic_router.py — FP-11 semantic hook metadata + router.

Usage (inside any hook that wants routing):

    import sys, os, json
    sys.path.insert(0, os.path.dirname(__file__))
    from _semantic_router import should_fire, classify_prompt

    hook_input = json.load(sys.stdin)
    prompt = hook_input.get("prompt", "")
    if not should_fire(__file__, prompt):
        print("{}")
        sys.exit(0)

Router reads the @bigd-hook-meta YAML block from the hook file itself.
Rule-based keyword classifier — NO LLM. Per CLAUDE.md hard rule.

Log: ~/.claude/hooks/.router_log.jsonl
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Intent -> keyword map (rule-based, no LLM)
# ---------------------------------------------------------------------------
# Each intent has a list of keyword patterns. Any match = intent is active.
# Patterns are lowercased substring checks (fast). Regex only when needed.

INTENT_KEYWORDS: dict[str, list[str]] = {
    "bigd": [
        "bigd", "big-d", "bigsystemd", "inbox", "brief", "briefs",
        "triage", "pipeline", "daemon", "bundle", "approval queue",
        "approve", "defer", "skip", "ack",
    ],
    "pm": [
        "kalshi", "polymarket", "manifold", "prediction market",
        "pm bot", "hel", "london", "vultr", "pm-london",
        "trading", "trade", "bet", "market maker", "mm ", "fill",
        "position", "portfolio", "odds", "orderbook",
    ],
    "telegram": [
        "telegram", "tg ", "bot message", "telegram bot", "tg bot",
        "speak_hook", "memo display", "story memo",
    ],
    "docx": [
        "word doc", "docx", ".docx", "word document", "microsoft word",
        "write a doc", "export to word",
    ],
    "git": [
        "git ", "commit", "push", "pull request", "branch", "merge",
        "rebase", "stash", "clone", "repo", "repository", "github",
        "gitignore", "git log", "git diff",
    ],
    "code": [
        "fix the", "debug", "refactor", "implement", "add feature",
        "typescript", "python", "javascript", "function", "class",
        "import", "module", "error in", "bug in", "crash", "exception",
        "test", "unit test", "lint", "tsc", "compile", "build",
    ],
    "meta": [
        "claude.md", "hook", "agent", "skill ", "memory", "nardoworld",
        "rules", "settings.json", "strict-execute", "strict-plan",
        "/ship", "ship phase", "subagent",
    ],
    "debug": [
        "debug", "error:", "traceback", "exception", "stack trace",
        "not working", "broken", "fails", "failure", "crash",
        "502", "503", "500 error", "timeout", "connection refused",
    ],
    "x_tweet": [
        "tweet", "x thread", "post on x", "twitter", "@nardovibecoding",
        "280 chars", "post this",
    ],
    "vps": [
        "vps", "ssh", "server", "hel ", "london ", "vultr", "systemd",
        "launchctl", "service", "deploy", "rsync",
    ],
    "memory": [
        "remember", "recall", "memory", "nardoworld", "wiki",
        "hub node", "graph", "article", "note", "lesson",
    ],
    "sync": [
        "sync", "rsync", "git pull", "git push", "vps sync",
        "pm sync", "deploy",
    ],
}

# ---------------------------------------------------------------------------
# Parser: extract @bigd-hook-meta block from hook file
# ---------------------------------------------------------------------------

_META_CACHE: dict[str, dict] = {}


def _parse_hook_meta(hook_path: str) -> dict:
    """
    Parse the @bigd-hook-meta YAML comment block from a hook file.
    Returns dict with keys: name, fires_on, relevant_intents,
    irrelevant_intents, cost_score, always_fire.
    Returns {} if no meta block found (back-compat: always fire).
    """
    if hook_path in _META_CACHE:
        return _META_CACHE[hook_path]

    try:
        text = Path(hook_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        _META_CACHE[hook_path] = {}
        return {}

    # Find the meta block: lines starting with # that include @bigd-hook-meta
    lines = text.splitlines()
    meta_start = -1
    for i, line in enumerate(lines[:30]):  # only scan first 30 lines
        if "@bigd-hook-meta" in line:
            meta_start = i
            break

    if meta_start == -1:
        _META_CACHE[hook_path] = {}
        return {}

    # Collect comment lines after meta_start until non-comment line
    meta_lines = []
    for line in lines[meta_start + 1 : meta_start + 20]:
        stripped = line.strip()
        if stripped.startswith("#"):
            meta_lines.append(stripped[1:].strip())
        else:
            break

    # Parse simple key: value and key: [a, b, c] YAML subset
    meta: dict = {}
    for line in meta_lines:
        if ":" not in line:
            continue
        key, _, raw_val = line.partition(":")
        key = key.strip()
        val = raw_val.split("#")[0].strip()  # strip inline comments

        if val.startswith("[") and val.endswith("]"):
            # List: [a, b, c]
            items = [x.strip().strip('"').strip("'") for x in val[1:-1].split(",")]
            meta[key] = [x for x in items if x]
        elif val.lower() == "true":
            meta[key] = True
        elif val.lower() == "false":
            meta[key] = False
        else:
            try:
                meta[key] = int(val)
            except ValueError:
                meta[key] = val

    _META_CACHE[hook_path] = meta
    return meta


# ---------------------------------------------------------------------------
# Classifier: prompt -> set of intent tags
# ---------------------------------------------------------------------------

def classify_prompt(prompt: str) -> set[str]:
    """
    Classify a prompt into a set of intent tags using keyword lookup.
    Returns set of matching intents. Returns {"general"} if nothing matches.
    Rule-based only — no LLM.
    """
    if not prompt:
        return {"general"}

    prompt_lower = prompt.lower()
    matched: set[str] = set()

    for intent, keywords in INTENT_KEYWORDS.items():
        for kw in keywords:
            if kw in prompt_lower:
                matched.add(intent)
                break  # one match per intent is enough

    return matched if matched else {"general"}


# ---------------------------------------------------------------------------
# Router decision
# ---------------------------------------------------------------------------

def should_fire(hook_path: str, prompt: str) -> bool:
    """
    Decide whether this hook should fire for the given prompt.

    Rules:
    1. No meta block -> always fire (back-compat).
    2. always_fire: true -> always fire.
    3. relevant_intents present -> fire if ANY classified intent is in the list.
    4. irrelevant_intents present -> skip if ALL classified intents are in that list.
    5. Neither list -> always fire.

    Logs decision to .router_log.jsonl.
    """
    meta = _parse_hook_meta(hook_path)
    hook_name = os.path.basename(hook_path)

    if not meta:
        # No metadata -> back-compat, always fire (no log to keep noise down)
        return True

    if meta.get("always_fire", False):
        _log_decision(hook_name, prompt, set(), "fire", "always_fire=true")
        return True

    intents = classify_prompt(prompt)
    relevant = set(meta.get("relevant_intents", []))
    irrelevant = set(meta.get("irrelevant_intents", []))

    if relevant:
        # Fire if ANY classified intent overlaps with relevant_intents
        if intents & relevant:
            _log_decision(hook_name, prompt, intents, "fire",
                          f"matched relevant: {intents & relevant}")
            return True
        # No relevant match -> skip (unless no irrelevant_intents either)
        _log_decision(hook_name, prompt, intents, "skip",
                      f"no relevant match (need {relevant}, got {intents})")
        return False

    if irrelevant:
        # Skip only if ALL intents are in the irrelevant set
        if intents and intents.issubset(irrelevant):
            _log_decision(hook_name, prompt, intents, "skip",
                          f"all intents in irrelevant: {intents}")
            return False
        _log_decision(hook_name, prompt, intents, "fire",
                      f"not all intents irrelevant (got {intents})")
        return True

    # No relevant or irrelevant list -> always fire
    return True


# ---------------------------------------------------------------------------
# Log
# ---------------------------------------------------------------------------

LOG_PATH = Path(__file__).parent / ".router_log.jsonl"
_LOG_FD: Optional[int] = None


def _log_decision(hook_name: str, prompt: str, intents: set,
                  decision: str, reason: str) -> None:
    """Append one log row to .router_log.jsonl. Silent on failure."""
    global _LOG_FD
    try:
        row = {
            "ts": time.time(),
            "hook": hook_name,
            "prompt_head": prompt[:80],
            "intents": sorted(intents),
            "decision": decision,
            "reason": reason,
        }
        line = json.dumps(row) + "\n"
        if _LOG_FD is None:
            _LOG_FD = os.open(str(LOG_PATH), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        os.write(_LOG_FD, line.encode("utf-8"))
    except Exception:
        pass  # always silent


# ---------------------------------------------------------------------------
# Standalone: print classification for a prompt (for testing)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    prompt_arg = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else ""
    result = classify_prompt(prompt_arg)
    print(f"prompt: {prompt_arg!r}")
    print(f"intents: {sorted(result)}")

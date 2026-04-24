#!/usr/bin/env python3
# @bigd-hook-meta
# name: credential_detect
# fires_on: UserPromptSubmit
# relevant_intents: []
# irrelevant_intents: []
# cost_score: 2
# always_fire: true
"""UserPromptSubmit hook: detect credentials in user messages, inject reminder to file to SSoT.

Detection tiers:
  A) Known-prefix API keys (high confidence, zero false positives)
  B) Explicit label + value patterns (API_KEY=xxx, KEY: xxx, TOKEN=xxx)
  C) Credential keyword + high-entropy 30+ char token near the keyword

NEVER auto-files. Only injects context — Claude decides service name + confirms with user.
"""
import json
import math
import re
import sys

# Tier A: known prefixes. Prefix → service name.
KNOWN_PREFIXES = [
    ("sk-ant-",   "anthropic"),
    ("sk-proj-",  "openai"),
    ("sk-",       "openai-or-generic"),
    ("ghp_",      "github"),
    ("gho_",      "github"),
    ("ghu_",      "github"),
    ("ghs_",      "github"),
    ("xoxb-",     "slack-bot"),
    ("xoxp-",     "slack-user"),
    ("xapp-",     "slack-app"),
    ("AIza",      "google"),
    ("AKIA",      "aws-access-key"),
    ("pk_live_",  "stripe-pub"),
    ("sk_live_",  "stripe-secret"),
    ("pk_test_",  "stripe-pub-test"),
    ("sk_test_",  "stripe-secret-test"),
    ("glpat-",    "gitlab"),
    ("hf_",       "huggingface"),
    ("gsk_",      "groq"),
]

# Tier B: label = value
LABEL_PATTERNS = [
    r'\b([A-Z][A-Z0-9_]{2,}_?(?:KEY|TOKEN|SECRET|PASSWORD|PASS|PWD|API))\s*[=:]\s*["\']?([A-Za-z0-9_\-\.]{16,})',
    r'\b(api[_\s-]?key|access[_\s-]?token|bearer[_\s-]?token|secret[_\s-]?key)\s*[=:]\s*["\']?([A-Za-z0-9_\-\.]{16,})',
]

# Tier C: credential keyword proximity
CRED_KEYWORDS = re.compile(r'\b(api[_\s-]?key|token|secret|credential|password|creds?)\b', re.IGNORECASE)
TOKEN_CANDIDATE = re.compile(r'\b[A-Za-z0-9_\-]{30,}\b')

# Tier D: standalone UUID (Helius, many services use UUID as API key)
UUID_PATTERN = re.compile(r'\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b', re.IGNORECASE)

# Claude-internal path markers — UUIDs inside these are session/task IDs, never credentials.
INTERNAL_PATH_MARKERS = ("/tasks/", "claude-501", "/-Users-bernard/", "claude_statusline",
                        ".claude/projects/", "/sessions/", "output_file")


def _current_session_id() -> str | None:
    """Read current session ID from statusline JSON so we never flag our own UUID."""
    try:
        import os
        with open("/tmp/claude_statusline.json") as f:
            sl = json.load(f)
        tp = sl.get("transcript_path", "")
        # transcript_path like .../-Users-bernard/<session-uuid>.jsonl
        base = os.path.basename(tp).replace(".jsonl", "")
        if UUID_PATTERN.fullmatch(base):
            return base.lower()
    except Exception:
        pass
    return None


def _in_internal_path(text: str, start: int, end: int) -> bool:
    """Check if UUID match sits inside a Claude-internal path (not a credential)."""
    window = text[max(0, start - 60):min(len(text), end + 60)]
    return any(m in window for m in INTERNAL_PATH_MARKERS)


def shannon_entropy(s: str) -> float:
    if not s:
        return 0.0
    freq = {}
    for c in s:
        freq[c] = freq.get(c, 0) + 1
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in freq.values())


def looks_like_wallet(s: str) -> bool:
    if s.startswith('0x') and len(s) == 42 and all(c in '0123456789abcdefABCDEF' for c in s[2:]):
        return True
    if len(s) in (43, 44) and s.isalnum() and not s.isupper() and not s.islower():
        return False
    return False


def detect(text: str) -> list[dict]:
    hits: list[dict] = []
    seen_values: set[str] = set()

    for prefix, service in KNOWN_PREFIXES:
        for m in re.finditer(re.escape(prefix) + r'[A-Za-z0-9_\-]{16,}', text):
            v = m.group(0)
            if v in seen_values:
                continue
            seen_values.add(v)
            hits.append({"tier": "A", "service": service, "preview": v[:10] + "…", "length": len(v)})

    for pat in LABEL_PATTERNS:
        for m in re.finditer(pat, text, re.IGNORECASE):
            label, value = m.group(1), m.group(2)
            if value in seen_values or looks_like_wallet(value):
                continue
            seen_values.add(value)
            hits.append({"tier": "B", "service": label.lower(), "preview": value[:6] + "…", "length": len(value)})

    _session_id = _current_session_id()
    for m in UUID_PATTERN.finditer(text):
        value = m.group(0)
        if value in seen_values:
            continue
        # Skip current session UUID — never a credential, just Claude plumbing.
        if _session_id and value.lower() == _session_id:
            continue
        # Skip UUIDs embedded in Claude-internal paths (task files, session dirs).
        if _in_internal_path(text, m.start(), m.end()):
            continue
        seen_values.add(value)
        hits.append({"tier": "D", "service": "uuid-key (helius/similar)", "preview": value[:8] + "…", "length": len(value)})

    if CRED_KEYWORDS.search(text):
        for m in TOKEN_CANDIDATE.finditer(text):
            value = m.group(0)
            if value in seen_values or looks_like_wallet(value):
                continue
            if shannon_entropy(value) < 4.0:
                continue
            kw_near = False
            start = max(0, m.start() - 80)
            end = min(len(text), m.end() + 80)
            if CRED_KEYWORDS.search(text[start:end]):
                kw_near = True
            if not kw_near:
                continue
            seen_values.add(value)
            hits.append({"tier": "C", "service": "unknown", "preview": value[:6] + "…", "length": len(value)})

    return hits


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        print("{}")
        return

    prompt = data.get("prompt", "") or ""
    if not prompt:
        print("{}")
        return

    hits = detect(prompt)
    if not hits:
        print("{}")
        return

    lines = ["⚠️ CREDENTIAL DETECTED in user message:"]
    for h in hits:
        lines.append(f"  • [{h['tier']}] {h['service']} — {h['preview']} (len {h['length']})")
    lines.append("")
    lines.append("MANDATORY before any /s, summary, or /clear:")
    lines.append("  1. Confirm service name with user if ambiguous")
    lines.append("  2. Append to ~/telegram-claude-bot/.env (single source of truth)")
    lines.append("  3. Update ~/.claude/projects/-Users-bernard/memory/reference_api_keys_locations.md")
    lines.append("  4. NEVER paraphrase / summarize raw key into convo logs")

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": "\n".join(lines),
        }
    }))


if __name__ == "__main__":
    main()

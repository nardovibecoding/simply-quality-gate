"""SSOT redactor — strip secrets from free-form string fields before append.

Per spec REQ-12 + Plan S2 carry resolution C3.

Patterns covered:
- Eth privkey/addr hex   `0x[a-fA-F0-9]{40,}`
- Kalshi API key prefix  `kxa-[a-zA-Z0-9_]+`
- Bearer auth header     `Bearer [A-Za-z0-9._-]+`
- Base64 secrets         `[A-Za-z0-9+/]{60,}={0,2}`  (60-char min per Plan S2 to reduce UUID FPs)
- PEM blocks             `-----BEGIN ...-----...-----END ...-----`

Public API:
    redact(text: str) -> str
    redact_field(value, key: str = "") -> Any
        - applies to strings only
        - >256 char strings always run through redactor
        - keys named "message", "error_class" always redacted regardless of length
        - other types (dict/list) recursed; ints/bools/None passed through
"""
from __future__ import annotations

import re
from typing import Any

# Order matters: PEM first (multiline), then long base64, then specific token shapes.
_PEM_RE = re.compile(
    r"-----BEGIN [A-Z ]+-----[\s\S]*?-----END [A-Z ]+-----",
    re.MULTILINE,
)
_ETH_HEX_RE = re.compile(r"0x[a-fA-F0-9]{40,}")
_KALSHI_KEY_RE = re.compile(r"kxa-[a-zA-Z0-9_]+")
_BEARER_RE = re.compile(r"Bearer [A-Za-z0-9._-]+")
# Base64: 60+ chars of [A-Za-z0-9+/] with optional `=` padding.
# Tightened from spec's 40-char to 60-char per Plan S2 carry resolution to reduce
# UUID-like / hex-string false positives. Matches surrounding context-friendly
# (no leading-anchor / trailing-anchor) so embedded secrets in log lines are caught.
_BASE64_RE = re.compile(r"[A-Za-z0-9+/]{60,}={0,2}")

# Field keys that always trigger redaction even for short strings.
_ALWAYS_REDACT_KEYS = {"message", "error_class", "error", "stderr", "stdout", "output", "command", "content"}

_MAX_UNREDACTED = 256  # Strings longer than this run redactor unconditionally.


def redact(text: str) -> str:
    """Run all redaction patterns over text, return scrubbed copy."""
    if not isinstance(text, str) or not text:
        return text
    out = _PEM_RE.sub("[REDACTED:PEM]", text)
    out = _ETH_HEX_RE.sub("[REDACTED:ETH_HEX]", out)
    out = _KALSHI_KEY_RE.sub("[REDACTED:KALSHI_KEY]", out)
    out = _BEARER_RE.sub("[REDACTED:BEARER]", out)
    out = _BASE64_RE.sub("[REDACTED:B64]", out)
    return out


def redact_field(value: Any, key: str = "") -> Any:
    """Recursively redact strings in nested dict/list. Non-strings pass through."""
    if isinstance(value, str):
        if key in _ALWAYS_REDACT_KEYS or len(value) > _MAX_UNREDACTED:
            return redact(value)
        # Short string in unmonitored key: still apply token-shape redactors
        # (cheap, catches stray privkeys in any string).
        return redact(value)
    if isinstance(value, dict):
        return {k: redact_field(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_field(v, key) for v in value]
    return value

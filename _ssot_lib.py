"""SSOT writer library — append-only event log to ~/NardoWorld/meta/ssot/ssot.jsonl.

Shared core for both `ssot_writer.py` (mac Claude hook) and the future
`~/NardoWorld/scripts/ssot_writer.py` (hel/london bot writer, S3-S5).

Spec: ~/.ship/ssot-log/goals/01-spec.md (REQ-01..REQ-16)
Plan: ~/.ship/ssot-log/goals/02-plan.md (Slice S2)

Schema: §3 of spec — 12 top-level fields + per-kind metadata.

Design properties:
- stdlib only (no python-ulid; inline ULID generator)
- flock(LOCK_EX|LOCK_NB), 5ms × 10 retries, 50ms cap (REQ-07)
- fire-and-forget: writer crash NEVER aborts caller (REQ-13). Always exit 0.
- Torn-line repair on next append (REQ-15)
- Redactor (REQ-12) applied before serialise

Correlation: env var SSOT_CORRELATION_ID propagates via ssh/exec subprocesses.
WebFetch / Edit / Write don't cross hosts → no correlation needed; documented limitation.

schema_version: 1
"""
from __future__ import annotations

import errno
import fcntl
import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

from _ssot_redactor import redact_field

SSOT_DIR = Path.home() / "NardoWorld" / "meta" / "ssot"
SSOT_FILE = SSOT_DIR / "ssot.jsonl"
SSOT_LOCK = SSOT_DIR / "ssot.lock"

LOCK_RETRY_INTERVAL_S = 0.005  # 5ms
LOCK_MAX_RETRIES = 10  # 5ms × 10 = 50ms cap (REQ-07)

SCHEMA_VERSION = 1

# ──────────────────────────────────────────────────────────────────────────────
# ULID generation (Crockford-base32, 26 chars, time-ordered)
# ──────────────────────────────────────────────────────────────────────────────
_CROCKFORD_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

# Within-process monotonicity: if new ts <= last ts, increment-from-last.
_last_ulid_ms = 0
_last_ulid_rand = 0


def _crockford_encode(num: int, length: int) -> str:
    out = []
    for _ in range(length):
        out.append(_CROCKFORD_ALPHABET[num & 0x1F])
        num >>= 5
    return "".join(reversed(out))


def generate_ulid() -> str:
    """26-char Crockford-base32 ULID. Within-process monotonic.

    Layout: 48-bit ms timestamp (10 chars) + 80-bit randomness (16 chars).
    """
    global _last_ulid_ms, _last_ulid_rand
    now_ms = int(time.time() * 1000)
    if now_ms <= _last_ulid_ms:
        # Clock didn't advance OR went backward (NTP step) — increment last_rand.
        now_ms = _last_ulid_ms
        rand = (_last_ulid_rand + 1) & ((1 << 80) - 1)
    else:
        rand = int.from_bytes(secrets.token_bytes(10), "big")
    _last_ulid_ms = now_ms
    _last_ulid_rand = rand
    ts_part = _crockford_encode(now_ms, 10)
    rand_part = _crockford_encode(rand, 16)
    return ts_part + rand_part


# ──────────────────────────────────────────────────────────────────────────────
# Host detection (mac/hel/london)
# ──────────────────────────────────────────────────────────────────────────────
def detect_host() -> str:
    """Return 'mac' | 'hel' | 'london' based on hostname."""
    h = os.environ.get("HOSTNAME") or ""
    if not h:
        try:
            h = subprocess.check_output(["hostname"], timeout=1).decode().strip()
        except Exception:
            h = ""
    h = h.split(".")[0].lower()
    if "mac" in h or "local" in h or "bernard" in h:
        return "mac"
    if "hel" in h or "claude" in h:
        return "hel"
    if "london" in h or "pm" == h:
        return "london"
    return "mac"  # default; mac is the only host this lib runs on in S2


# ──────────────────────────────────────────────────────────────────────────────
# Git state probe (cheap, capped)
# ──────────────────────────────────────────────────────────────────────────────
def _git_state(cwd: str) -> str | None:
    if not cwd or not os.path.isdir(cwd):
        return None
    try:
        sha = subprocess.check_output(
            ["git", "-C", cwd, "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL, timeout=0.5,
        ).decode().strip()
        if not sha:
            return None
        # Dirty check — `--quiet` exits 1 if dirty; cheap.
        dirty = subprocess.call(
            ["git", "-C", cwd, "diff", "--quiet"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=0.5,
        )
        return f"{sha}{'*' if dirty else ''}"
    except Exception:
        return None


# ──────────────────────────────────────────────────────────────────────────────
# Torn-line repair (REQ-15)
# ──────────────────────────────────────────────────────────────────────────────
def _repair_torn_line(path: Path) -> None:
    """If file lacks trailing newline OR is empty, repair before append.

    REQ-15: file empty OR no newline → truncate to 0; else seek to last newline + truncate.
    """
    try:
        if not path.exists():
            return
        size = path.stat().st_size
        if size == 0:
            return  # already clean
        with open(path, "rb+") as f:
            f.seek(-1, os.SEEK_END)
            last = f.read(1)
            if last == b"\n":
                return  # clean
            # No trailing newline — find last newline before EOF + truncate after it.
            chunk = 4096
            pos = max(0, size - chunk)
            f.seek(pos)
            buf = f.read()
            idx = buf.rfind(b"\n")
            if idx == -1:
                # No newline anywhere → file is one torn line; truncate to 0.
                f.seek(0)
                f.truncate()
            else:
                # Truncate to after the last newline.
                f.seek(pos + idx + 1)
                f.truncate()
    except Exception as e:
        # Best-effort; failure is non-fatal (REQ-13 fire-and-forget).
        sys.stderr.write(f"ssot:_repair_torn_line: {e}\n")


# ──────────────────────────────────────────────────────────────────────────────
# Lock + append
# ──────────────────────────────────────────────────────────────────────────────
def _acquire_lock(lock_fd: int) -> bool:
    """flock LOCK_EX|LOCK_NB with 5ms × 10 retry budget. Return True on success."""
    for _ in range(LOCK_MAX_RETRIES):
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError as e:
            if e.errno not in (errno.EAGAIN, errno.EWOULDBLOCK):
                return False
            time.sleep(LOCK_RETRY_INTERVAL_S)
    return False


def _release_lock(lock_fd: int) -> None:
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
    except Exception:
        pass


def append_event(event: dict) -> bool:
    """Append one event. Returns True on success, False on lock-timeout/error.

    REQ-08: never block tool >50ms. REQ-13: fire-and-forget; caller MUST exit 0.
    """
    try:
        SSOT_DIR.mkdir(parents=True, exist_ok=True)
        # Open lock file (separate from data file so flock doesn't fight rotate).
        lock_fd = os.open(str(SSOT_LOCK), os.O_CREAT | os.O_WRONLY, 0o644)
        try:
            if not _acquire_lock(lock_fd):
                sys.stderr.write("ssot:lock-timeout — event dropped\n")
                return False
            _repair_torn_line(SSOT_FILE)
            # Append one NDJSON line.
            line = json.dumps(event, separators=(",", ":"), ensure_ascii=False) + "\n"
            with open(SSOT_FILE, "a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
            return True
        finally:
            _release_lock(lock_fd)
            os.close(lock_fd)
    except Exception as e:
        sys.stderr.write(f"ssot:append_event: {e}\n")
        return False


# ──────────────────────────────────────────────────────────────────────────────
# Event factory
# ──────────────────────────────────────────────────────────────────────────────
def _now_iso_ms() -> str:
    """ISO 8601 UTC with ms precision: 2026-04-30T08:15:23.456Z"""
    t = time.time()
    ms = int((t - int(t)) * 1000)
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + f".{ms:03d}Z"


def build_event(
    kind: str,
    actor: str,
    subject: str,
    *,
    session_id: str | None = None,
    parent_event_id: str | None = None,
    cwd: str | None = None,
    outcome: str = "ok",
    metadata: dict | None = None,
) -> dict:
    """Construct a fully-populated event dict with redaction applied to metadata."""
    cwd = cwd or os.getcwd()
    md = redact_field(metadata or {}, key="metadata")
    md.setdefault("schema_version", SCHEMA_VERSION)
    return {
        "ts": _now_iso_ms(),
        "host": detect_host(),
        "session_id": session_id or os.environ.get("CLAUDE_SESSION_ID", "system"),
        "event_id": f"evt_{generate_ulid()}",
        "parent_event_id": parent_event_id,
        "correlation_id": os.environ.get("SSOT_CORRELATION_ID"),
        "kind": kind,
        "actor": actor,
        "subject": subject,
        "cwd": cwd,
        "git_state": _git_state(cwd),
        "outcome": outcome,
        "metadata": md,
    }

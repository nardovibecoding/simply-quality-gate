#!/usr/bin/env python3
# @bigd-hook-meta
# name: ssot_writer
# fires_on: PostToolUse, UserPromptSubmit, Stop, PreCompact, PermissionRequest
# relevant_intents: [all]
# irrelevant_intents: []
# cost_score: 1
# always_fire: true
# Copyright (c) 2026 Nardo (nardovibecoding). AGPL-3.0 — see LICENSE
"""SSOT writer hook — captures Claude Code events to ~/NardoWorld/meta/ssot/ssot.jsonl.

Slice S2 of /ship ssot-log. Spec REQ-01..REQ-16 + Plan §S2.
Extended by α.S10: writer-health bundle (D3b/D8/D11/D12).

Invocation: registered in ~/.claude/settings.json under PostToolUse,
UserPromptSubmit, Stop, PreCompact. Reads JSON payload from stdin.

Behavior:
- PostToolUse → emit kind=tool_call (subject=<tool_name>)
- UserPromptSubmit → emit kind=user_turn (subject="prompt")
- Stop → emit kind=assistant_turn (subject="turn") + REQ-16 orphan recovery
  if a previous assistant_turn has no matching Stop within session.
- PreCompact → emit kind=session.precompact (subject="compact") with
  metadata.chars_before and metadata.context_pct from payload.
- PermissionRequest → emit kind=session.permission_request (subject=tool_name)
  with metadata.tool_name and metadata.tool_args_path (file_path or command from tool_input).

α.S10 writer-health bundle (fires alongside main events):
- writer_health: every 10th PostToolUse fire (heartbeat sidecar).
- writer_backpressure: when queue_depth > 5 or flush_ms > 100 (D8 alarm).
- writer_resume: when gap since last write >= 30s (D8/D3b replay marker).
- secret_redaction: when outgoing payload matches API-key patterns (OWASP LLM02:2025).

Fire-and-forget: every code path exits 0 (REQ-13). Never block caller >50ms (REQ-08).

Field mapping (Claude Code stdin payload):
  hook_event_name        → routes to kind
  tool_name              → subject for tool_call
  tool_input             → metadata (redacted, hashed)
  tool_response          → outcome inference + bytes/exit_code
  prompt                 → user_turn metadata
  session_id             → top-level session_id
  cwd                    → top-level cwd

Verified payload shape from sibling hooks at:
  ~/.claude/hooks/admin_only_guard.py:78-126 (uses tool_name, tool_input)
  ~/.claude/hooks/hook_base.py:30-37 (json.load(sys.stdin), input_data.get fields)
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

# Add hooks dir to path so _ssot_lib imports work even when invoked as absolute path.
_HOOKS_DIR = Path(__file__).resolve().parent
if str(_HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(_HOOKS_DIR))

try:
    from _ssot_lib import append_event, build_event, update_index, SSOT_DIR, _atomic_write
    from _ssot_redactor import redact_field
except Exception as e:
    sys.stderr.write(f"ssot_writer: import failed: {e}\n")
    print("{}")
    sys.exit(0)

# ──────────────────────────────────────────────────────────────────────────────
# α.S10 — writer-health bundle state
# ──────────────────────────────────────────────────────────────────────────────
_WRITER_STATE_PATH = SSOT_DIR / ".writer_state.json"
_BACKPRESSURE_LOCK = SSOT_DIR / ".bp.lock"

# Module-level counters (per process lifetime).
_fire_counter: int = 0       # increments each PostToolUse; resets mod-10 on health emit
_error_count_24h: int = 0    # running error count (in-process only; persisted on health emit)
_fsync_count: int = 0        # number of successful appends this process
_last_write_ok: bool = True  # result of last append_event call
_last_flush_ms: int = 0      # ms for last lock+write measured in handle_post_tool_use

# ──────────────────────────────────────────────────────────────────────────────
# Secret-redaction patterns (OWASP LLM02:2025)
# Source: https://owasp.org/www-project-top-10-for-large-language-model-applications/
# Pattern list: POLY_PRIVATE_KEY, sk- (OpenAI), AKIA (AWS), ghp_ (GitHub), xoxb- (Slack)
# ──────────────────────────────────────────────────────────────────────────────
_SECRET_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("POLY_PRIVATE_KEY", re.compile(r"(POLY_PRIVATE_KEY\s*[=:]\s*)\S+")),
    ("sk-",             re.compile(r"(sk-[A-Za-z0-9]{4,})[A-Za-z0-9]{16,}")),
    ("AKIA",            re.compile(r"(AKIA[0-9A-Z]{16})")),
    ("ghp_",            re.compile(r"(ghp_[A-Za-z0-9]{36})")),
    ("xoxb-",           re.compile(r"(xoxb-[0-9-]+-[A-Za-z0-9]+)")),
]

_REDACT_PLACEHOLDER = "<REDACTED>"
_MAX_DEPTH = 10


def _redact_secrets(payload: dict, depth: int = 0) -> tuple[dict, int, str, str]:
    """Walk payload dict recursively (depth-bounded ≤10); redact API-key patterns in-place.

    Returns (redacted_payload, matched_count, first_matched_pattern, first_field_path).
    D11 (F9): never raises; returns original payload on error.
    """
    matched_count = 0
    first_pattern = ""
    first_field = ""
    if depth > _MAX_DEPTH:
        return payload, 0, "", ""

    try:
        result = dict(payload)
        for key, val in result.items():
            if isinstance(val, str):
                for pat_name, pat_re in _SECRET_PATTERNS:
                    new_val, n = pat_re.subn(_REDACT_PLACEHOLDER, val)
                    if n > 0:
                        val = new_val
                        matched_count += n
                        if not first_pattern:
                            first_pattern = pat_name
                            first_field = key
                result[key] = val
            elif isinstance(val, dict):
                sub, sub_n, sub_pat, sub_field = _redact_secrets(val, depth + 1)
                result[key] = sub
                if sub_n > 0:
                    matched_count += sub_n
                    if not first_pattern:
                        first_pattern = sub_pat
                        first_field = f"{key}.{sub_field}"
            elif isinstance(val, list):
                new_list = []
                for item in val:
                    if isinstance(item, str):
                        for pat_name, pat_re in _SECRET_PATTERNS:
                            item, n = pat_re.subn(_REDACT_PLACEHOLDER, item)
                            if n > 0:
                                matched_count += n
                                if not first_pattern:
                                    first_pattern = pat_name
                                    first_field = key
                    elif isinstance(item, dict):
                        item, sub_n, sub_pat, sub_field = _redact_secrets(item, depth + 1)
                        if sub_n > 0:
                            matched_count += sub_n
                            if not first_pattern:
                                first_pattern = sub_pat
                                first_field = f"{key}[].{sub_field}"
                    new_list.append(item)
                result[key] = new_list
        return result, matched_count, first_pattern, first_field
    except Exception as e:
        sys.stderr.write(f"ssot_writer:_redact_secrets: {e}\n")
        return payload, 0, "", ""


def _load_writer_state() -> dict:
    """Load .writer_state.json; return defaults if missing or corrupt. Never raises."""
    try:
        if _WRITER_STATE_PATH.exists():
            raw = _WRITER_STATE_PATH.read_text(encoding="utf-8")
            return json.loads(raw)
    except Exception:
        pass
    return {
        "last_write_ts": None,
        "last_known_seq": 0,
        "error_count_24h": 0,
        "fsync_count": 0,
    }


def _save_writer_state(state: dict) -> None:
    """Persist .writer_state.json atomically. Never raises."""
    try:
        SSOT_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _WRITER_STATE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, separators=(",", ":"), ensure_ascii=False),
                       encoding="utf-8")
        tmp.rename(_WRITER_STATE_PATH)
    except Exception as e:
        sys.stderr.write(f"ssot_writer:_save_writer_state: {e}\n")


def _runtime_actor() -> str:
    """Return the assistant runtime actor for emitted SSOT rows."""
    if (os.environ.get("CODEX_THREAD_ID") or os.environ.get("CODEX_CI")
            or os.environ.get("CODEX_SESSION") or os.environ.get("CODEX_CLI_VERSION")):
        return "codex"
    return "claude"


def _check_resume(session_id: str | None, cwd: str) -> None:
    """Emit kind=writer_resume if gap since last_write_ts >= 30s. Never raises.

    D3b (F10.4): replay marker so consumers know events may be missing in the gap.
    D8 (F4.2): fail-quiet; does not block.
    """
    try:
        state = _load_writer_state()
        last_ts_str = state.get("last_write_ts")
        if not last_ts_str:
            return  # first run — no gap yet
        # Parse ISO to epoch (stdlib, no dateutil).
        try:
            # Format: 2026-05-02T10:00:00.000Z  →  strip Z, split on .
            clean = last_ts_str.rstrip("Z")
            if "." in clean:
                base, frac = clean.rsplit(".", 1)
            else:
                base, frac = clean, "0"
            import datetime
            last_epoch = datetime.datetime.strptime(base, "%Y-%m-%dT%H:%M:%S").replace(
                tzinfo=datetime.timezone.utc
            ).timestamp() + float(f"0.{frac}")
        except Exception:
            return
        gap_s = time.time() - last_epoch
        if gap_s < 30:
            return
        gap_ms = int(gap_s * 1000)
        last_seq = state.get("last_known_seq", 0)
        # Estimate missed events: gap_s / avg_inter_event_s (assume 10s avg)
        missed_estimate = max(0, int(gap_s / 10) - 1)
        event = build_event(
            kind="writer_resume",
            actor=_runtime_actor(),
            subject="writer",
            session_id=session_id,
            cwd=cwd,
            outcome="ok",
            metadata={
                "gap_ms": gap_ms,
                "missed_events_estimate": missed_estimate,
                "last_known_seq": last_seq,
            },
        )
        append_event(event)
        update_index(event)
        # Reset seq after emit.
        state["last_known_seq"] = last_seq
        _save_writer_state(state)
    except Exception as e:
        sys.stderr.write(f"ssot_writer:_check_resume: {e}\n")


def _emit_health(session_id: str | None, cwd: str) -> None:
    """Emit kind=writer_health every 10th PostToolUse fire. Never raises.

    D3b (F10.4): heartbeat sidecar so Phase 9 / lint can detect absence.
    Reads counts from .writer_state.json (init defaults if missing).
    """
    global _error_count_24h, _fsync_count, _last_write_ok, _last_flush_ms
    try:
        state = _load_writer_state()
        # Merge in-process accumulated errors/fsync into stored state.
        total_errors = state.get("error_count_24h", 0)
        total_fsync = state.get("fsync_count", 0)
        event = build_event(
            kind="writer_health",
            actor=_runtime_actor(),
            subject="writer",
            session_id=session_id,
            cwd=cwd,
            outcome="ok",
            metadata={
                "last_write_result": _last_write_ok,
                "last_flush_ms": _last_flush_ms,
                "queue_depth": 0,        # in-process queue; 0 = no backlog in sync writer
                "fsync_count": total_fsync + _fsync_count,
                "error_count_24h": total_errors + _error_count_24h,
            },
        )
        ok = append_event(event)
        if ok:
            # Persist updated counters + sequence to .writer_state.json via update_index.
            update_index(event)
    except Exception as e:
        sys.stderr.write(f"ssot_writer:_emit_health: {e}\n")


def _emit_backpressure_if_needed(session_id: str | None, cwd: str,
                                  flush_ms: int, queue_depth: int,
                                  dropped_count: int) -> None:
    """Emit kind=writer_backpressure when queue_depth > 5 OR flush_ms > 100.

    D8 (F4.2): bounded-queue alarm. Uses a SEPARATE fcntl-lock (100ms timeout, fail-quiet).
    D11 (F9.1): all error paths return, never raise.
    """
    if queue_depth <= 5 and flush_ms <= 100:
        return
    try:
        # Use a separate lock to avoid contending on the main ssot.lock.
        lock_path = str(_BACKPRESSURE_LOCK)
        try:
            lock_fd = os.open(lock_path, os.O_CREAT | os.O_WRONLY, 0o644)
        except Exception:
            return  # fail-quiet: can't open bp lock
        try:
            # 100ms timeout: try up to 20 times × 5ms.
            acquired = False
            for _ in range(20):
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except OSError:
                    time.sleep(0.005)
            if not acquired:
                return  # fail-quiet per R4
            event = build_event(
                kind="writer_backpressure",
                actor=_runtime_actor(),
                subject="writer",
                session_id=session_id,
                cwd=cwd,
                outcome="error",
                metadata={
                    "queue_depth": queue_depth,
                    "flush_ms": flush_ms,
                    "dropped_count": dropped_count,
                },
            )
            append_event(event)
            update_index(event)
        finally:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                os.close(lock_fd)
            except Exception:
                pass
    except Exception as e:
        sys.stderr.write(f"ssot_writer:_emit_backpressure_if_needed: {e}\n")


# ──────────────────────────────────────────────────────────────────────────────
# Event handlers
# ──────────────────────────────────────────────────────────────────────────────

def _hash_args(args: dict) -> str:
    """SHA256 over canonical-JSON of args (sorted keys, no whitespace)."""
    try:
        canon = json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]
    except Exception:
        return ""


def _classify_outcome(tool_response: dict) -> str:
    """Return 'ok' | 'error' | 'timeout' | 'blocked' from tool_response."""
    if not isinstance(tool_response, dict):
        return "ok"
    if tool_response.get("interrupted") or tool_response.get("isError"):
        return "error"
    if "timeout" in str(tool_response.get("error", "")).lower():
        return "timeout"
    return "ok"


def handle_post_tool_use(payload: dict) -> dict:
    global _last_flush_ms, _last_write_ok, _fsync_count, _error_count_24h
    tool_name = payload.get("tool_name", "unknown")
    tool_input = payload.get("tool_input", {}) or {}
    tool_response = payload.get("tool_response", {}) or {}
    metadata = {
        "tool": tool_name,
        "args_hash": _hash_args(tool_input),
        "duration_ms": payload.get("duration_ms"),
    }
    # Bash → exit_code; Edit/Write → bytes_written.
    if tool_name == "Bash":
        metadata["exit_code"] = tool_response.get("exit_code") if isinstance(tool_response, dict) else None
    if tool_name in ("Edit", "Write"):
        content = (tool_input.get("content") or tool_input.get("new_string") or "")
        metadata["bytes_written"] = len(content.encode("utf-8")) if isinstance(content, str) else None
    err = tool_response.get("error") if isinstance(tool_response, dict) else None
    if err:
        metadata["error_class"] = str(err)[:200]

    # Measure flush time (D9/D8 observability).
    t0 = time.monotonic_ns()
    event = build_event(
        kind="tool_call",
        actor=_runtime_actor(),
        subject=tool_name,
        session_id=payload.get("session_id"),
        cwd=payload.get("cwd") or os.getcwd(),
        outcome=_classify_outcome(tool_response),
        metadata=metadata,
    )
    ok = append_event(event)
    flush_ns = time.monotonic_ns() - t0
    flush_ms_now = flush_ns // 1_000_000

    _last_flush_ms = flush_ms_now
    _last_write_ok = ok
    if ok:
        _fsync_count += 1
    else:
        _error_count_24h += 1

    # Backpressure check: flush > 100ms or synthetic queue depth.
    # queue_depth approximated as 1 when lock contended (append returned False = lock-timeout).
    queue_depth = 0 if ok else 6  # simulate >5 when write failed due to lock contention
    dropped = 0 if ok else 1
    _emit_backpressure_if_needed(
        payload.get("session_id"), payload.get("cwd") or os.getcwd(),
        flush_ms_now, queue_depth, dropped,
    )
    return event


def handle_user_prompt_submit(payload: dict) -> dict:
    prompt = payload.get("prompt", "") or ""
    return build_event(
        kind="user_turn",
        actor="bernard",
        subject="prompt",
        session_id=payload.get("session_id"),
        cwd=payload.get("cwd") or os.getcwd(),
        outcome="ok",
        metadata={
            "prompt_chars": len(prompt) if isinstance(prompt, str) else 0,
            # Redactor will scrub the prompt content; full body kept for now (already short, no secret patterns expected).
            "prompt_preview": redact_field(prompt[:200] if isinstance(prompt, str) else "", key="prompt"),
        },
    )


def handle_stop(payload: dict) -> dict:
    # Orphan recovery (REQ-16): minimal v1 — emit one assistant_turn at Stop.
    # session_orphan_recovery proper detection deferred to S6/S7 tooling
    # (cross-session ledger comparison); /lint Phase 9 will detect orphan
    # assistant_turns lacking matching Stop fire and emit recovery events.
    return build_event(
        kind="assistant_turn",
        actor=_runtime_actor(),
        subject="turn",
        session_id=payload.get("session_id"),
        cwd=payload.get("cwd") or os.getcwd(),
        outcome="ok",
        metadata={
            "stop_hook_active": payload.get("stop_hook_active", False),
        },
    )


def handle_precompact(payload: dict) -> dict:
    # Emit kind=session.precompact with chars_before + context_pct from payload.
    # Field names per Claude Code PreCompact payload spec. If field names differ
    # in practice, values will be None — [GAP — exp: verify via ssot.jsonl tail
    # after Bernard triggers /compact; update field names if null].
    return build_event(
        kind="session.precompact",
        actor=_runtime_actor(),
        subject="compact",
        session_id=payload.get("session_id"),
        cwd=payload.get("cwd") or os.getcwd(),
        outcome="ok",
        metadata={
            "chars_before": payload.get("chars_before"),
            "context_pct": payload.get("context_pct"),
        },
    )


def handle_session_save(payload: dict) -> dict:
    # Emit kind=session.save when /s skill completes a checkpoint save.
    # Invoked by /s SKILL.md Step 2 agent via:
    #   echo '<json>' | python3 ~/.claude/hooks/ssot_writer.py
    # where <json> = {"hook_event_name":"SessionSave","session_id":"...","metadata":{...}}
    # Placed AFTER the ALREADY_SAVED guard in Step 0 so dup /s calls don't double-write.
    # Fields: chars_before (int|null), topic_slug (str|null),
    #         lessons_filed_count (int|null), source ("/s").
    return build_event(
        kind="session.save",
        actor=_runtime_actor(),
        subject="session",
        session_id=payload.get("session_id"),
        cwd=payload.get("cwd") or os.getcwd(),
        outcome="ok",
        metadata={
            "chars_before": payload.get("chars_before"),
            "topic_slug": payload.get("topic_slug"),
            "lessons_filed_count": payload.get("lessons_filed_count"),
            "source": payload.get("source", "/s"),
        },
    )


def handle_permission_request(payload: dict) -> dict:
    # Emit kind=session.permission_request with tool_name + first-path from tool_input.
    # PermissionRequest payload mirrors PreToolUse: tool_name + tool_input are present.
    # [GAP — exact PermissionRequest stdin payload field names inferred from PreToolUse shape
    # (hook_base.py:35-36, hook-dev SKILL.md:316) + changelog 2.0.45; verify tool_name/
    # tool_args_path non-null after Bernard triggers a permission-required tool in next session.]
    tool_name = payload.get("tool_name") or "unknown"
    tool_input = payload.get("tool_input") or {}
    # Extract the primary path/command field — covers Read/Edit/Write (file_path) + Bash (command).
    tool_args_path = (
        tool_input.get("file_path")
        or tool_input.get("command")
        or tool_input.get("path")
        or None
    )
    return build_event(
        kind="session.permission_request",
        actor=_runtime_actor(),
        subject=tool_name,
        session_id=payload.get("session_id"),
        cwd=payload.get("cwd") or os.getcwd(),
        outcome="ok",
        metadata={
            "tool_name": tool_name,
            "tool_args_path": str(tool_args_path)[:200] if tool_args_path else None,
            "decision": payload.get("decision"),
        },
    )


_DISPATCH = {
    "PostToolUse": handle_post_tool_use,
    "UserPromptSubmit": handle_user_prompt_submit,
    "Stop": handle_stop,
    "PreCompact": handle_precompact,
    "SessionSave": handle_session_save,
    "PermissionRequest": handle_permission_request,
}


def main() -> int:
    global _fire_counter
    try:
        raw = sys.stdin.read()
        if not raw:
            print("{}")
            return 0
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            sys.stderr.write("ssot_writer: malformed JSON payload, dropping event\n")
            print("{}")
            return 0

        # α.S10 step 2: redact secrets from incoming payload before any dispatch.
        payload, matched_count, matched_pattern, field_path = _redact_secrets(payload)

        event_name = payload.get("hook_event_name", "")
        session_id = payload.get("session_id")
        cwd = payload.get("cwd") or os.getcwd()

        handler = _DISPATCH.get(event_name)
        if not handler:
            # Hook fired for unsupported event name (forward-compat); silently no-op.
            print("{}")
            return 0

        # α.S10 step 5: check for resume before dispatching.
        _check_resume(session_id, cwd)

        # Dispatch — handle_post_tool_use now returns event dict AND side-effects
        # (backpressure check). Other handlers return event dict for append below.
        if event_name == "PostToolUse":
            # handle_post_tool_use internally calls append_event + emits backpressure.
            event = handle_post_tool_use(payload)
            # update_index called here since handle_post_tool_use returns early.
            update_index(event)
        else:
            event = handler(payload)
            append_event(event)  # success/failure both result in exit 0 (REQ-13)
            update_index(event)  # α.S0: incremental index update; fire-and-forget

        # α.S10 step 3: emit secret_redaction sidecar if any match was found.
        if matched_count > 0:
            redact_event = build_event(
                kind="secret_redaction",
                actor=_runtime_actor(),
                subject="writer",
                session_id=session_id,
                cwd=cwd,
                outcome="ok",
                metadata={
                    "matched_pattern": matched_pattern,
                    "field_path": field_path,
                    "redacted_length": matched_count,
                },
            )
            append_event(redact_event)
            update_index(redact_event)

        # Update last_write_ts + persisted fire_counter in .writer_state.json.
        # fire_counter is persisted because each PostToolUse hook invocation is a
        # fresh subprocess — module-level counters reset every invocation.
        try:
            state = _load_writer_state()
            state["last_write_ts"] = event.get("ts") or _now_iso()
            state["last_known_seq"] = state.get("last_known_seq", 0) + 1
            if event_name == "PostToolUse":
                fc = state.get("fire_counter", 0) + 1
                state["fire_counter"] = fc
            _save_writer_state(state)
        except Exception:
            pass

        # α.S10 step 3: emit writer_health every 10th PostToolUse.
        # Read persisted fire_counter to detect the 10th across subprocess boundaries.
        if event_name == "PostToolUse":
            try:
                state2 = _load_writer_state()
                fc = state2.get("fire_counter", 0)
                if fc % 10 == 0:
                    _emit_health(session_id, cwd)
            except Exception:
                pass

    except Exception as e:
        # Catch-all: any uncaught exception MUST NOT abort tool call.
        sys.stderr.write(f"ssot_writer: unexpected: {e}\n")
    finally:
        print("{}")
    return 0


def _now_iso() -> str:
    """ISO 8601 UTC ms-precision string (local helper, mirrors _ssot_lib._now_iso_ms)."""
    t = time.time()
    ms = int((t - int(t)) * 1000)
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + f".{ms:03d}Z"


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
# @bigd-hook-meta
# name: ssot_writer
# fires_on: PostToolUse, UserPromptSubmit, Stop, PreCompact
# relevant_intents: [all]
# irrelevant_intents: []
# cost_score: 1
# always_fire: true
# Copyright (c) 2026 Nardo (nardovibecoding). AGPL-3.0 — see LICENSE
"""SSOT writer hook — captures Claude Code events to ~/NardoWorld/meta/ssot/ssot.jsonl.

Slice S2 of /ship ssot-log. Spec REQ-01..REQ-16 + Plan §S2.

Invocation: registered in ~/.claude/settings.json under PostToolUse,
UserPromptSubmit, Stop. Reads JSON payload from stdin.

Behavior:
- PostToolUse → emit kind=tool_call (subject=<tool_name>)
- UserPromptSubmit → emit kind=user_turn (subject="prompt")
- Stop → emit kind=assistant_turn (subject="turn") + REQ-16 orphan recovery
  if a previous assistant_turn has no matching Stop within session.

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

import hashlib
import json
import os
import sys
from pathlib import Path

# Add hooks dir to path so _ssot_lib imports work even when invoked as absolute path.
_HOOKS_DIR = Path(__file__).resolve().parent
if str(_HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(_HOOKS_DIR))

try:
    from _ssot_lib import append_event, build_event
    from _ssot_redactor import redact_field
except Exception as e:
    sys.stderr.write(f"ssot_writer: import failed: {e}\n")
    print("{}")
    sys.exit(0)


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
    return build_event(
        kind="tool_call",
        actor="claude",
        subject=tool_name,
        session_id=payload.get("session_id"),
        cwd=payload.get("cwd") or os.getcwd(),
        outcome=_classify_outcome(tool_response),
        metadata=metadata,
    )


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
        actor="claude",
        subject="turn",
        session_id=payload.get("session_id"),
        cwd=payload.get("cwd") or os.getcwd(),
        outcome="ok",
        metadata={
            "stop_hook_active": payload.get("stop_hook_active", False),
        },
    )


_DISPATCH = {
    "PostToolUse": handle_post_tool_use,
    "UserPromptSubmit": handle_user_prompt_submit,
    "Stop": handle_stop,
}


def main() -> int:
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

        event_name = payload.get("hook_event_name", "")
        handler = _DISPATCH.get(event_name)
        if not handler:
            # Hook fired for unsupported event name (forward-compat); silently no-op.
            print("{}")
            return 0

        event = handler(payload)
        append_event(event)  # success/failure both result in exit 0 (REQ-13)
    except Exception as e:
        # Catch-all: any uncaught exception MUST NOT abort tool call.
        sys.stderr.write(f"ssot_writer: unexpected: {e}\n")
    finally:
        print("{}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

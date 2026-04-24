#!/usr/bin/env python3
"""UserPromptSubmit hook: inject inbox briefs into additionalContext.

Order of operations (P10.15):
1. Check ~/inbox/_summaries/ready/*.json — if any bundle present:
   a. Read latest bundle (by filename date, YYYY-MM-DD_bundle.json)
   b. Format into human-readable summary per daemon x host + cross-refs
   c. Include in additionalContext
   d. Call collector.py --consume <bundle_id> to move ready -> consumed
   e. Record bundle_id in session state so same session does not re-inject
2. Fallback: if NO bundle in ready/, use legacy critical/+daily/+weekly injection
3. Session delta (F6) stays: don't re-inject same bundle_id or same brief IDs

Tier delivery schedule (HKT = UTC+8) — applies only to legacy fallback path:
- critical/  : always (every prompt, delta-only after first inject per session)
- daily/     : 10:00-12:00 HKT only
- weekly/    : Sunday 20:00-22:00 HKT only

Session dedup (Fix 6):
- Tracks last-inject state per session via /tmp/claude_inbox_inject_<session_id>
- First prompt: inject all qualifying briefs, write state file with brief IDs + last_seen timestamps
- Subsequent prompts: inject DELTA only (briefs with last_seen > prev inject time, or unseen IDs)
- Session ID sourced from CLAUDE_SESSION_ID env var (falls back to pid-based key)

Validation: hand-rolled required-field check against _schema.json required fields.
On malformed brief: stderr warning, skip — never crash.
Budget: <200ms with 50 briefs queued.
"""

import glob
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(__file__))
from telemetry import log_fire, log_fire_done

INBOX_ROOT = os.path.expanduser("~/inbox")
BUNDLE_READY_DIR = os.path.expanduser("~/inbox/_summaries/ready")
COLLECTOR_PATH = os.path.expanduser("~/NardoWorld/scripts/bigd/_lib/collector.py")
SCHEMA_REQUIRED = ["id", "tier", "source_daemon", "host", "title", "body", "created", "actions"]
ACTION_REQUIRED = ["code", "label", "command"]

# Bundle size limit: if bundle JSON > 50KB, truncate middle sections
BUNDLE_SIZE_LIMIT_BYTES = 50 * 1024

HKT = timezone(timedelta(hours=8))

# Session state dir for dedup tracking
_TMP_DIR = "/tmp"

# Daemons in display order
_DAEMON_ORDER = ["lint", "security", "performance", "gaps", "upgrade"]
# Hosts in display order
_HOST_ORDER = ["mac", "hel", "london"]


def _hkt_now():
    return datetime.now(tz=HKT)


def _in_daily_window(now):
    """10:00-12:00 HKT."""
    return now.hour == 10 or (now.hour == 11) or (now.hour == 12 and now.minute == 0)


def _in_weekly_window(now):
    """Sunday 20:00-22:00 HKT. weekday() == 6 = Sunday."""
    return now.weekday() == 6 and (now.hour == 20 or now.hour == 21 or (now.hour == 22 and now.minute == 0))


def _validate_brief(data, path):
    """Return True if all required fields present and actions[] valid. Warn on stderr otherwise."""
    for field in SCHEMA_REQUIRED:
        if field not in data:
            print(f"[inbox_hook] WARN: skipping {path} — missing field '{field}'", file=sys.stderr)
            return False
    if not isinstance(data["actions"], list) or len(data["actions"]) < 1:
        print(f"[inbox_hook] WARN: skipping {path} — actions must be non-empty list", file=sys.stderr)
        return False
    for action in data["actions"]:
        for af in ACTION_REQUIRED:
            if af not in action:
                print(f"[inbox_hook] WARN: skipping {path} — action missing field '{af}'", file=sys.stderr)
                return False
    return True


def _load_briefs(subdir):
    """Load and validate all JSON briefs in a subdir. Return list of (path, dict) tuples."""
    pattern = os.path.join(INBOX_ROOT, subdir, "*.json")
    briefs = []
    for path in sorted(glob.glob(pattern)):
        try:
            with open(path, "r") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            print(f"[inbox_hook] WARN: cannot read {path} — {e}", file=sys.stderr)
            continue
        if _validate_brief(data, path):
            briefs.append((path, data))
    return briefs


def _session_id():
    """Get a stable session key. Uses CLAUDE_SESSION_ID env var, falls back to parent PID."""
    sid = os.environ.get("CLAUDE_SESSION_ID", "")
    if sid:
        return sid[:32]  # truncate for safety
    # Fall back to parent PID (stable within a session)
    return str(os.getppid())


def _state_path(session_id: str) -> str:
    return os.path.join(_TMP_DIR, f"claude_inbox_inject_{session_id}.json")


def _load_state(session_id: str) -> dict:
    """Load session inject state. Returns {inject_ts: float, seen_ids: set, bundle_ids: set}."""
    path = _state_path(session_id)
    try:
        with open(path) as f:
            raw = json.load(f)
        return {
            "inject_ts": float(raw.get("inject_ts", 0)),
            "seen_ids": set(raw.get("seen_ids", [])),
            "bundle_ids": set(raw.get("bundle_ids", [])),
        }
    except (OSError, json.JSONDecodeError, ValueError):
        return {"inject_ts": 0.0, "seen_ids": set(), "bundle_ids": set()}


def _save_state(session_id: str, inject_ts: float, seen_ids: set, bundle_ids: set) -> None:
    """Persist session inject state to /tmp."""
    path = _state_path(session_id)
    try:
        with open(path, "w") as f:
            json.dump({
                "inject_ts": inject_ts,
                "seen_ids": list(seen_ids),
                "bundle_ids": list(bundle_ids),
            }, f)
    except OSError as e:
        print(f"[inbox_hook] WARN: cannot save state to {path}: {e}", file=sys.stderr)


def _brief_last_seen(brief: dict) -> float:
    """Return last_seen as epoch float, or created timestamp, or 0."""
    for field in ("last_seen", "created"):
        val = brief.get(field, "")
        if val:
            try:
                # Try ISO8601 with Z suffix
                ts = datetime.fromisoformat(val.replace("Z", "+00:00"))
                return ts.timestamp()
            except (ValueError, AttributeError):
                pass
    return 0.0


def _is_delta(brief: dict, state: dict) -> bool:
    """
    Return True if this brief should be included in a delta inject.
    True if: brief ID not seen before OR last_seen > last inject time.
    """
    brief_id = brief.get("id", "")
    if brief_id not in state["seen_ids"]:
        return True
    last_seen_ts = _brief_last_seen(brief)
    return last_seen_ts > state["inject_ts"]


def _format_brief(brief, idx):
    """Format a single brief for additionalContext injection."""
    lines = [
        f"[INBOX #{idx}] [{brief['tier'].upper()}] {brief['title']}",
        f"  Source: {brief['source_daemon']} @ {brief['host']} | ID: {brief['id']}",
        f"  {brief['body']}",
        "  Actions:",
    ]
    for action in brief["actions"]:
        lines.append(f"    [{action['code']}] {action['label']}")
    # Show recurrence if > 1
    rc = brief.get("recurrence_count", 1)
    if rc > 1:
        first = brief.get("first_seen", "")
        lines.append(f"  [Recurrence: #{rc} | first_seen: {first}]")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Bundle injection (P10.15)
# ---------------------------------------------------------------------------

def _latest_ready_bundle():
    """
    Return (bundle_id, bundle_dict) of the latest bundle in ready/ by filename date.
    Returns (None, None) if ready/ is empty or all files are malformed.
    """
    pattern = os.path.join(BUNDLE_READY_DIR, "*_bundle.json")
    paths = sorted(glob.glob(pattern))  # lexicographic = date order
    if not paths:
        return None, None
    # Take the last (latest date)
    path = paths[-1]
    try:
        raw = open(path, "rb").read()
        bundle = json.loads(raw)
    except (OSError, json.JSONDecodeError) as e:
        print(f"[inbox_hook] WARN: cannot read bundle {path} — {e}", file=sys.stderr)
        return None, None

    bundle_id = bundle.get("bundle_id")
    if not bundle_id:
        print(f"[inbox_hook] WARN: bundle {path} missing bundle_id", file=sys.stderr)
        return None, None

    # Validate required top-level bundle fields
    _BUNDLE_REQUIRED = ["bundle_id", "date", "assembled_at", "summaries_count", "summaries"]
    for field in _BUNDLE_REQUIRED:
        if field not in bundle:
            print(f"[inbox_hook] WARN: bundle {path} missing field '{field}', falling back to legacy", file=sys.stderr)
            return None, None
    if not isinstance(bundle.get("summaries"), dict):
        print(f"[inbox_hook] WARN: bundle {path} 'summaries' not a dict, falling back to legacy", file=sys.stderr)
        return None, None

    return bundle_id, bundle


def _consume_bundle(bundle_id: str) -> None:
    """
    Fire collector.py --consume <bundle_id> as a background process (non-blocking).
    Fails gracefully: logs warning, does NOT propagate.
    Worst case: bundle stays in ready/ and re-injects next session (acceptable).
    Non-blocking keeps hook under 200ms budget (subprocess import is ~400ms blocking).
    """
    try:
        subprocess.Popen(
            ["python3", COLLECTOR_PATH, "--consume", bundle_id],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        print(f"[inbox_hook] bundle {bundle_id} consume dispatched (async)", file=sys.stderr)
    except Exception as e:
        print(f"[inbox_hook] WARN: consume dispatch failed for {bundle_id}: {e}", file=sys.stderr)


def _format_bundle(bundle: dict) -> str:
    """
    Render bundle as human-readable additionalContext block.
    If raw JSON > BUNDLE_SIZE_LIMIT_BYTES, truncate middle sections.
    """
    bundle_id    = bundle.get("bundle_id", "?")
    date_str     = bundle.get("date", "?")
    assembled_at = bundle.get("assembled_at", "?")
    count        = bundle.get("summaries_count", "?")
    summaries    = bundle.get("summaries", {})
    cross_refs   = bundle.get("cross_refs", {})

    truncated = False
    raw_size = len(json.dumps(bundle).encode())
    if raw_size > BUNDLE_SIZE_LIMIT_BYTES:
        truncated = True

    lines = [
        f"=== BIGD DAILY BUNDLE {date_str} ===",
        f"(bundle_id: {bundle_id}, {count} daemon summaries, assembled {assembled_at})",
        "",
        f"## {len(_DAEMON_ORDER)} DAEMONS (folded across {len(_HOST_ORDER)} hosts)",
    ]

    # Per daemon section
    daemon_sections_written = 0
    for daemon in _DAEMON_ORDER:
        host_lines = []
        top_actions_all = []

        for host in _HOST_ORDER:
            key = f"{daemon}@{host}"
            s = summaries.get(key)
            if s is None:
                host_lines.append(f"  - {host.capitalize()}: missing")
                continue

            land = s.get("ship_phases", {}).get("land", {})
            new_c      = land.get("findings_new", 0)
            resolved_c = land.get("findings_resolved_since_last", 0)
            recurring_c = land.get("findings_recurring", 0)
            health     = s.get("self_report", {}).get("daemon_health", "?")
            host_lines.append(
                f"  - {host.capitalize()}: {new_c} new, {resolved_c} resolved, "
                f"{recurring_c} recurring (health: {health})"
            )

            # Collect proposed action titles for top-actions across hosts
            for pa in s.get("proposed_actions", []):
                approval = " [APPROVAL REQUIRED]" if pa.get("approval_required") else ""
                top_actions_all.append(f"{pa.get('title','?')} [{pa.get('risk','?')} risk]{approval}")

        # If truncated, skip middle daemon sections (keep first 2 and last 1)
        if truncated and 1 < daemon_sections_written < len(_DAEMON_ORDER) - 1:
            if daemon_sections_written == 2:
                lines.append(f"\n  (... {len(_DAEMON_ORDER) - 3} more daemon sections, see ~/inbox/critical/)")
            daemon_sections_written += 1
            continue

        lines.append(f"\n### {daemon.upper()}")
        lines.extend(host_lines)
        if top_actions_all:
            lines.append("  Top proposed actions:")
            for a in top_actions_all[:3]:
                lines.append(f"    - {a}")
        daemon_sections_written += 1

    # Cross-refs
    lines.append("\n## CROSS-DAEMON CONFLICTS")
    conflicts = cross_refs.get("action_conflicts", [])
    if conflicts:
        for c in conflicts:
            lines.append(f"  - {c}")
    else:
        lines.append("  (none)")

    lines.append("\n## CROSS-DAEMON CLUSTER CANDIDATES")
    clusters = cross_refs.get("cluster_candidates", [])
    if clusters:
        for c in clusters:
            lines.append(f"  - {c}")
    else:
        lines.append("  (none)")

    # Holy-mode verdict section: approval_required=true across all summaries
    approval_actions = []
    for key, s in summaries.items():
        for pa in s.get("proposed_actions", []):
            if pa.get("approval_required"):
                approval_actions.append(
                    f"  - [{key}] {pa.get('title','?')} (risk: {pa.get('risk','?')}, "
                    f"blast: {pa.get('blast_radius_score','?')})"
                )

    lines.append("\n## CLAUDE HOLY-MODE VERDICT NEEDED")
    if approval_actions:
        for a in approval_actions:
            lines.append(a)
    else:
        lines.append("  (no approval-required actions this bundle)")

    lines.append("\n=== END BUNDLE ===")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    _t0 = log_fire(__file__)
    try:
        # Consume stdin (required for hook protocol; ignore content for this hook)
        try:
            json.load(sys.stdin)
        except Exception:
            pass

        now = _hkt_now()
        session_id = _session_id()
        state = _load_state(session_id)
        is_first_inject = state["inject_ts"] == 0.0

        # ------------------------------------------------------------------
        # PATH A: Bundle injection (P10.15)
        # ------------------------------------------------------------------
        bundle_id, bundle = _latest_ready_bundle()

        if bundle_id is not None:
            # Check session dedup: if this bundle was already injected this session, skip
            if bundle_id in state["bundle_ids"]:
                # Already injected this session — emit empty
                print(f"[inbox_hook] bundle {bundle_id} already injected this session, skipping", file=sys.stderr)
                _save_state(session_id, time.time(), state["seen_ids"], state["bundle_ids"])
                log_fire_done(__file__, _t0, errored=False, output_size_bytes=2)
                print(json.dumps({}))
                return

            # Format bundle
            try:
                bundle_text = _format_bundle(bundle)
            except Exception as e:
                print(f"[inbox_hook] WARN: bundle format failed ({e}), falling back to legacy", file=sys.stderr)
                bundle_id = None  # fall through to legacy path

            if bundle_id is not None:
                context = bundle_text
                out = json.dumps({"additionalContext": context})

                # Consume: move ready -> consumed (graceful fail)
                _consume_bundle(bundle_id)

                # Update session state: record bundle_id as seen
                new_bundle_ids = state["bundle_ids"] | {bundle_id}
                _save_state(session_id, time.time(), state["seen_ids"], new_bundle_ids)

                log_fire_done(__file__, _t0, errored=False, output_size_bytes=len(out))
                print(out)
                return

        # ------------------------------------------------------------------
        # PATH B: Legacy injection (critical/+daily/+weekly briefs)
        # Reached when: no bundle in ready/, OR bundle was malformed
        # ------------------------------------------------------------------

        # Collect candidate briefs from qualifying tiers
        candidates: list[tuple[str, dict]] = []

        # Always: critical
        candidates.extend(_load_briefs("critical"))

        # Daily window: 10:00-12:00 HKT
        if _in_daily_window(now):
            candidates.extend(_load_briefs("daily"))

        # Weekly window: Sunday 20:00-22:00 HKT
        if _in_weekly_window(now):
            candidates.extend(_load_briefs("weekly"))

        if not candidates:
            _save_state(session_id, time.time(), state["seen_ids"], state["bundle_ids"])
            log_fire_done(__file__, _t0, errored=False, output_size_bytes=2)
            print(json.dumps({}))
            return

        # Apply delta filter: first inject gets all, subsequent get only new/updated
        if is_first_inject:
            selected = candidates
            inject_label = f"all ({len(selected)} briefs, first inject this session)"
        else:
            selected = [(p, b) for p, b in candidates if _is_delta(b, state)]
            if not selected:
                # No new briefs since last inject — emit empty
                _save_state(session_id, time.time(), state["seen_ids"], state["bundle_ids"])
                log_fire_done(__file__, _t0, errored=False, output_size_bytes=2)
                print(json.dumps({}))
                return
            inject_label = f"delta ({len(selected)} new/updated since last inject)"

        lines = [
            "<inbox-briefs>",
            "[System note: Big SystemD inbox briefs — pending items for Bernard's approval. "
            "Each brief has reply codes; Bernard types e.g. '1' to approve, '2' to defer, '3' to skip.]",
            "",
            f"Pending briefs ({inject_label}):",
        ]
        for i, (path, brief) in enumerate(selected, 1):
            lines.append("")
            lines.append(_format_brief(brief, i))

        lines.append("")
        lines.append("</inbox-briefs>")

        context = "\n".join(lines)
        out = json.dumps({"additionalContext": context})

        # Update session state: record inject timestamp + seen IDs
        new_seen = state["seen_ids"] | {b.get("id", "") for _, b in candidates}
        _save_state(session_id, time.time(), new_seen, state["bundle_ids"])

        log_fire_done(__file__, _t0, errored=False, output_size_bytes=len(out))
        print(out)
    except Exception as e:
        log_fire_done(__file__, _t0, errored=True, output_size_bytes=0)
        print(f"[inbox_hook] error: {e}", file=sys.stderr)
        print(json.dumps({}))


if __name__ == "__main__":
    main()

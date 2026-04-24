#!/usr/bin/env python3
# @bigd-hook-meta
# name: inbox_hook
# fires_on: UserPromptSubmit
# relevant_intents: [bigd, meta]
# irrelevant_intents: [git, pm, telegram, docx, x_tweet, code, vps, sync]
# cost_score: 3
# always_fire: false
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

import io
sys.path.insert(0, os.path.dirname(__file__))
from telemetry import log_fire, log_fire_done
from _semantic_router import should_fire

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


def _brief_priority(brief: dict) -> int:
    """Map tier to sort priority integer. Lower = higher priority."""
    return {"critical": 0, "daily": 1, "weekly": 2}.get(brief.get("tier", ""), 3)


def _format_brief(brief, idx, also_reported_by: list = None):
    """Format a single brief for additionalContext injection."""
    lines = [
        f"[INBOX #{idx}] [{brief['tier'].upper()}] {brief['title']}",
        f"  Source: {brief['source_daemon']} @ {brief['host']} | ID: {brief['id']}",
        f"  {brief['body']}",
        "  Actions:",
    ]
    for action in brief["actions"]:
        cmd = action.get("command", "").strip()
        if cmd:
            cmd_preview = cmd[:100] + ("..." if len(cmd) > 100 else "")
            lines.append(f"    [{action['code']}] {action['label']}")
            lines.append(f"         cmd: {cmd_preview}")
        else:
            lines.append(f"    [{action['code']}] {action['label']}  (noop: archive-only)")
    # Show recurrence if > 1
    rc = brief.get("recurrence_count", 1)
    if rc > 1:
        first = brief.get("first_seen", "")
        lines.append(f"  [Recurrence: #{rc} | first_seen: {first}]")
    # Cross-host dedup note
    if also_reported_by:
        lines.append(f"  (also reported by: {', '.join(also_reported_by)})")
    return "\n".join(lines)


def _dedup_briefs(briefs_with_path: list) -> list:
    """
    Deduplicate briefs across hosts.
    Two briefs are duplicates only when they share the same 'id' field.
    (Cross-host: same finding written to multiple hosts with the same ID.)
    Secondarily: if both 'id' and 'message_hash' match (schema extension, optional).
    Keep the one with earliest 'created'. Attach 'also_reported_by' list to winner.
    Returns list of (path, brief, also_reported_by_list).

    NOTE: (source_daemon, title) secondary key is intentionally omitted.
    Many briefs share daemon+title patterns (e.g. "Issue found on london: ...")
    but are distinct findings with unique IDs. Dedup by title would false-positive.
    """
    # Build index: id -> list of (path, brief)
    by_id: dict[str, list] = {}

    for path, brief in briefs_with_path:
        bid = brief.get("id", "")
        if bid:
            by_id.setdefault(bid, []).append((path, brief))

    # Also check message_hash if present (optional schema extension)
    by_hash: dict[str, list] = {}
    for path, brief in briefs_with_path:
        mhash = brief.get("message_hash", "")
        if mhash:
            by_hash.setdefault(mhash, []).append((path, brief))

    processed_ids: set = set()
    result = []

    for path, brief in briefs_with_path:
        bid = brief.get("id", "")
        if bid in processed_ids:
            continue

        # Find all duplicates: same id OR same message_hash (if present)
        dupes_by_id = by_id.get(bid, [])
        mhash = brief.get("message_hash", "")
        dupes_by_hash = by_hash.get(mhash, []) if mhash else []

        all_dupes = {id(b): (p, b) for p, b in dupes_by_id + dupes_by_hash}.values()
        all_dupes = list(all_dupes)

        # Mark all IDs in this cluster as processed
        for dp, db in all_dupes:
            processed_ids.add(db.get("id", ""))

        if len(all_dupes) == 1:
            result.append((path, brief, []))
            continue

        # Pick winner: earliest 'created'
        def _created_ts(item):
            p, b = item
            val = b.get("created", "")
            try:
                return datetime.fromisoformat(val.replace("Z", "+00:00")).timestamp()
            except (ValueError, AttributeError):
                return 0.0

        all_dupes_sorted = sorted(all_dupes, key=_created_ts)
        win_path, win_brief = all_dupes_sorted[0]
        others = [b.get("host", "?") for p, b in all_dupes_sorted[1:] if b.get("host") != win_brief.get("host")]
        # Deduplicate host names in also_reported_by
        seen_hosts: set = set()
        unique_others = []
        for h in others:
            if h not in seen_hosts:
                seen_hosts.add(h)
                unique_others.append(h)
        result.append((win_path, win_brief, unique_others))

    return result


def _format_host_grouped(selected: list, inject_label: str) -> str:
    """
    Render selected briefs grouped by host (mac / hel / london).
    All 3 host sections always shown, even if 0 briefs.
    Deduplicates across hosts. Sorts within host by priority (P0->P3) then mtime desc.
    Appends a SUMMARY line with total + priority tallies across all hosts.
    """
    # Dedup first
    deduped = _dedup_briefs(selected)

    # Group by host
    host_groups: dict[str, list] = {h: [] for h in _HOST_ORDER}
    for path, brief, also_by in deduped:
        host = brief.get("host", "")
        # Normalise host aliases (e.g. "pm-london" -> "london")
        _alias_map = {"pm-london": "london", "neuro": "hel", "vps": "hel"}
        host = _alias_map.get(host, host)
        if host in host_groups:
            host_groups[host].append((path, brief, also_by))
        else:
            # Unknown host: put in "mac" as fallback with a note (don't silently drop)
            host_groups["mac"].append((path, brief, also_by))

    # Sort within each host: priority asc, then mtime desc
    for host in _HOST_ORDER:
        def _sort_key(item):
            path, brief, _ = item
            pri = _brief_priority(brief)
            try:
                mtime = -os.path.getmtime(path)
            except OSError:
                mtime = 0.0
            return (pri, mtime)
        host_groups[host].sort(key=_sort_key)

    # Priority tallies across all hosts
    priority_counts = {0: 0, 1: 0, 2: 0, 3: 0}
    total_briefs = 0
    total_actions = 0
    for host in _HOST_ORDER:
        for path, brief, _ in host_groups[host]:
            total_briefs += 1
            total_actions += len(brief.get("actions", []))
            priority_counts[_brief_priority(brief)] += 1

    lines = [
        "<inbox-briefs>",
        "[System note: Big SystemD inbox briefs — pending items for Bernard's approval. "
        "Each brief has reply codes; Bernard types e.g. '1' to approve, '2' to defer, '3' to skip.]",
        f"[{inject_label}]",
        "",
    ]

    global_idx = 1
    for host in _HOST_ORDER:
        entries = host_groups[host]
        host_action_count = sum(len(b.get("actions", [])) for _, b, _ in entries)
        lines.append(
            f"## {host.upper()} (host={host}) — {len(entries)} brief{'s' if len(entries) != 1 else ''}, "
            f"{host_action_count} proposed action{'s' if host_action_count != 1 else ''}"
        )
        if not entries:
            lines.append("  (no issues)")
        else:
            for path, brief, also_by in entries:
                lines.append("")
                lines.append(_format_brief(brief, global_idx, also_reported_by=also_by))
                global_idx += 1
        lines.append("")

    lines.append(
        f"## SUMMARY — Total {total_briefs} brief{'s' if total_briefs != 1 else ''} across {len(_HOST_ORDER)} hosts, "
        f"priorities: P0={priority_counts[0]} P1={priority_counts[1]} P2={priority_counts[2]} P3={priority_counts[3]}"
    )
    lines.append("</inbox-briefs>")
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


def _infer_category(daemon_key: str, title: str) -> str:
    """
    Rule-based category inference (no LLM). daemon_key = 'bigd-security@mac' or 'security@mac'.
    Title-based bug override checked first on title.
    Returns one of: Bugs, Security, Performance, Hygiene, Drift, Upgrade, Other.
    """
    import re
    title_lower = title.lower()
    if re.search(r"\b(bug|error|revert|broken)\b", title_lower):
        return "Bugs"
    # Normalise daemon_key: strip host suffix if present
    daemon_part = daemon_key.split("@")[0]  # e.g. "security" or "bigd-security"
    daemon_part = daemon_part.replace("bigd-", "")  # normalise to bare name
    mapping = {
        "security":    "Security",
        "performance": "Performance",
        "lint":        "Hygiene",
        "gaps":        "Drift",
        "upgrade":     "Upgrade",
    }
    return mapping.get(daemon_part, "Other")


# Category display order and emoji labels
_CATEGORY_EMOJI = {
    "Bugs":        "🐛 Bugs",
    "Security":    "🔒 Security",
    "Performance": "⚡ Performance",
    "Hygiene":     "🧹 Hygiene",
    "Drift":       "♻️ Drift",
    "Upgrade":     "🔧 Upgrade",
    "Other":       "📌 Other",
}
_CATEGORY_ORDER = ["Bugs", "Security", "Performance", "Hygiene", "Drift", "Upgrade", "Other"]


def _format_bundle_digest(bundle: dict) -> str:
    """
    Build human-readable approval queue grouped by category with numbered action codes.
    Includes directive telling Claude to present the queue to user.
    """
    date_str   = bundle.get("date", "?")
    summaries  = bundle.get("summaries", {})

    # Collect all proposed_actions across all daemon@host keys with global numbering
    # Each entry: (global_num, daemon_key, pa_dict, category)
    all_actions: list[tuple[int, str, dict, str]] = []
    num = 1
    for daemon_key, s in summaries.items():
        for pa in s.get("proposed_actions", []):
            title = pa.get("title", "")
            category = _infer_category(daemon_key, title)
            all_actions.append((num, daemon_key, pa, category))
            num += 1

    total_actions = len(all_actions)
    total_findings = sum(
        len(s.get("proposed_actions", [])) for s in summaries.values()
    )
    daemon_count = len(summaries)

    # Group by category
    by_category: dict[str, list[tuple[int, str, dict]]] = {c: [] for c in _CATEGORY_ORDER}
    for n, dk, pa, cat in all_actions:
        by_category[cat].append((n, dk, pa))

    bundle_id = bundle.get("bundle_id", "")
    directive = (
        "[DIRECTIVE to Claude: On next response, present the approval queue below to user. "
        "Ask them to reply with action numbers, 'approve all', or 'defer all'. "
        f"IMPORTANT: Before presenting the queue, emit a verdict JSON for bundle_id={bundle_id!r} "
        "via bigd._lib.verdict.write_verdict() -- call write_verdict(bundle_id, decisions) where "
        "decisions maps each action id to APPROVED/DEFERRED/SKIPPED based on user input. "
        "Present each escalated action as a numbered approval item to Bernard.]"
    )

    lines = [
        "<bigd-daily-bundle>",
        directive,
        "",
        f"## \U0001f4ec Daily BigD Report {date_str}",
        f"{total_findings} findings, {total_actions} actions across {daemon_count} daemons.",
        "",
    ]

    for cat in _CATEGORY_ORDER:
        entries = by_category.get(cat, [])
        if not entries:
            continue
        label = _CATEGORY_EMOJI.get(cat, cat)
        lines.append(f"### {label} ({len(entries)} actions)")
        for n, dk, pa in entries:
            risk    = pa.get("risk", "?")
            title   = pa.get("title", "?")
            pa_id   = pa.get("id", "?")
            # Phase 3: show concrete command text under each action
            actions_list = pa.get("actions", [])
            cmd_parts: list[str] = []
            for act in actions_list:
                cmd = (act.get("command") or "").strip()
                if cmd:
                    cmd_parts.append(f"[{act.get('code','?')}] {cmd[:100]}")
                else:
                    cmd_parts.append(f"[{act.get('code','?')}] (noop: archive-only)")
            lines.append(f"{n}. [risk={risk}] {title}  (id: {pa_id})")
            for cp in cmd_parts:
                lines.append(f"       {cp}")
        lines.append("")

    lines += [
        "## Action codes to reply with:",
        "- `1 2 5` — approve selected",
        "- `approve all` — approve every action",
        "- `defer all` — mark for tomorrow",
        "- `skip` — no action",
        "- `1-5 defer` — defer range",
        "</bigd-daily-bundle>",
    ]

    return "\n".join(lines)


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
                # Build category-grouped approval queue digest (FP-9/10)
                try:
                    digest_text = _format_bundle_digest(bundle)
                except Exception as de:
                    print(f"[inbox_hook] WARN: digest format failed ({de}), using raw bundle", file=sys.stderr)
                    digest_text = bundle_text
                context = digest_text
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

        # Force-refresh: scan critical/ for briefs newer than last inject
        # that are not yet in seen_ids. These bypass session dedup so
        # genuinely new briefs written after last inject always surface.
        _critical_dir = os.path.join(INBOX_ROOT, "critical")
        _force_refresh_count = 0
        if os.path.isdir(_critical_dir):
            for _fname in os.listdir(_critical_dir):
                if not _fname.endswith(".json"):
                    continue
                _fpath = os.path.join(_critical_dir, _fname)
                try:
                    _mtime = os.path.getmtime(_fpath)
                except OSError:
                    continue
                _brief_id = _fname[:-5]  # strip .json
                if _mtime > state["inject_ts"] and _brief_id not in state["seen_ids"]:
                    _force_refresh_count += 1
            if _force_refresh_count:
                print(
                    f"[inbox_hook] force-refresh: {_force_refresh_count} new brief(s) detected by mtime",
                    file=sys.stderr,
                )
                # Reset inject_ts to 0 so the delta filter treats this session as first-inject
                # for these new briefs; existing seen_ids still suppress already-seen ones.
                state["inject_ts"] = 0.0

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

        context = _format_host_grouped(selected, inject_label)
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
    # Router check: read stdin once, check intent, re-feed for main()
    _raw_stdin = sys.stdin.read()
    try:
        _hook_input = json.loads(_raw_stdin)
        _prompt = _hook_input.get("prompt", "")
    except Exception:
        _hook_input = {}
        _prompt = ""
    sys.stdin = io.StringIO(_raw_stdin)  # re-feed for json.load() in main()
    if not should_fire(__file__, _prompt):
        print(json.dumps({}))
        sys.exit(0)
    main()

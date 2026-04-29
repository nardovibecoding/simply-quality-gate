#!/usr/bin/env python3
# @bigd-hook-meta
# name: inbox_hook
# fires_on: UserPromptSubmit
# relevant_intents: [bigd, meta]
# irrelevant_intents: [git, pm, telegram, docx, x_tweet, code, vps, sync]
# cost_score: 3
# always_fire: false
"""UserPromptSubmit hook: inject inbox briefs into additionalContext.

Order of operations (P10.15 + FP-18):
1. Check ~/inbox/_summaries/ready/*.json — if any bundles present:
   a. Read up to 3 bundles (by filename date, YYYY-MM-DD_bundle.json), newest first
   b. Skip bundles already injected this session (by bundle_id)
   c. Format each new bundle into human-readable digest
   d. Concatenate all new bundle digests into one additionalContext block
   e. Call collector.py --consume <bundle_id> for each consumed bundle
   f. Record all bundle_ids in session state so same session does not re-inject
2. Fallback: if NO new bundle in ready/, use legacy critical/+daily/+weekly injection
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


# ---------------------------------------------------------------------------
# PATH C: Daily force-window inject (15:00 HKT)
# Bypasses intent gate (handled in __main__) and session dedup.
# Fires once per day max — first UserPromptSubmit at/after 15:00 HKT.
# ---------------------------------------------------------------------------
FORCE_WINDOW_HOUR = 15  # HKT
FORCE_WINDOW_CRITICAL_TOP_N = 10  # cap critical briefs in daily push


# PATH C auto-inject DISABLED 2026-04-26: token-waste concern. The 15:00 HKT
# panel was loading ~13KB into whichever session submitted first; if the model
# dropped it (which happened today), tokens were charged for nothing. Use the
# /bigd skill to summon the panel on-demand instead — it prints inline in the
# current session so it can't be silently dropped, and costs tokens only when
# explicitly requested.
_FORCE_WINDOW_AUTO_INJECT_ENABLED = False


def _is_force_window(now_hkt) -> bool:
    """True if current HKT time is at/after FORCE_WINDOW_HOUR today."""
    if not _FORCE_WINDOW_AUTO_INJECT_ENABLED:
        return False
    return FORCE_WINDOW_HOUR <= now_hkt.hour < 24


def _force_window_already_fired_today(state: dict, today_str: str) -> bool:
    return state.get("force_window_last_fire_date") == today_str


# Cross-session global state for PATH C (per-session state would re-fire in
# every new session). Stores last fire date only.
_FORCE_WINDOW_GLOBAL_PATH = "/tmp/claude_inbox_force_window_global.json"


def _load_force_window_global() -> dict:
    try:
        with open(_FORCE_WINDOW_GLOBAL_PATH) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _save_force_window_global(today_str: str) -> None:
    try:
        with open(_FORCE_WINDOW_GLOBAL_PATH, "w") as f:
            json.dump({"force_window_last_fire_date": today_str}, f)
    except OSError as e:
        print(
            f"[inbox_hook] WARN: cannot save force-window global state: {e}",
            file=sys.stderr,
        )


def _build_force_window_panel(today_str: str) -> str | None:
    """Build the daily-push panel: top-N critical briefs + today's bundle digest if exists.
    Returns None if neither critical briefs nor bundle available (nothing to inject).
    """
    parts = []

    # Critical briefs: newest N by mtime
    critical_briefs = _load_briefs("critical")
    if critical_briefs:
        # Sort newest-first by mtime, cap at N
        def _mtime_desc(item):
            p, _ = item
            try:
                return -os.path.getmtime(p)
            except OSError:
                return 0.0
        critical_briefs.sort(key=_mtime_desc)
        top_critical = critical_briefs[:FORCE_WINDOW_CRITICAL_TOP_N]
        crit_label = (
            f"daily-push critical: top {len(top_critical)} of {len(critical_briefs)} in critical/ "
            f"(newest by mtime)"
        )
        parts.append(_format_host_grouped(top_critical, crit_label))

    # Bundle: today's consumed bundle if exists
    consumed_path = os.path.expanduser(
        f"~/inbox/_summaries/consumed/{today_str}_bundle.json"
    )
    bundle = None
    if os.path.exists(consumed_path):
        try:
            with open(consumed_path) as f:
                bundle = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            print(
                f"[inbox_hook] PATH C: cannot read consumed bundle {consumed_path}: {e}",
                file=sys.stderr,
            )
    if bundle is not None:
        try:
            parts.append(_format_bundle_digest(bundle))
        except Exception as e:
            print(f"[inbox_hook] PATH C: bundle digest failed ({e})", file=sys.stderr)

    if not parts:
        return None

    header = (
        "**ACTION REQUIRED — surface this panel verbatim to the user "
        "BEFORE answering their prompt. This is the once-daily 15:00 HKT "
        "inbox push and only fires into one session per day; if you skip it, "
        "the user never sees it.**\n\n"
        f"[INBOX DAILY PUSH — {FORCE_WINDOW_HOUR:02d}:00 HKT — "
        f"{len(parts)} section(s) for {today_str}]\n"
        "(this is the once-daily force-inject; bypasses intent gate and session dedup)"
    )
    sep = "\n\n" + ("=" * 60) + "\n\n"
    return header + sep + sep.join(parts)


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
    """Load session inject state.
    Returns {inject_ts, seen_ids, bundle_ids, critical_shown_ids, bundle_ts}.

    F5 schema migration: bundle_ids may load as either:
      - legacy list[str] -> dict[str, float] with ts=0.0 (eligible for TTL re-inject immediately)
      - new dict[str, float] -> kept as-is

    F4 schema addition: critical_shown_ids = set of brief IDs shown via PATH B
    critical-always sub-path (independent of bundle dedup).

    F2 schema addition: briefs_emitted_last_turn = bool (read by inbox_ack.py).
    """
    path = _state_path(session_id)
    try:
        with open(path) as f:
            raw = json.load(f)
        # Migrate bundle_ids: list -> dict
        raw_bundle_ids = raw.get("bundle_ids", [])
        if isinstance(raw_bundle_ids, list):
            bundle_ts = {bid: 0.0 for bid in raw_bundle_ids}
        elif isinstance(raw_bundle_ids, dict):
            bundle_ts = {str(k): float(v) for k, v in raw_bundle_ids.items()}
        else:
            bundle_ts = {}
        return {
            "inject_ts": float(raw.get("inject_ts", 0)),
            "seen_ids": set(raw.get("seen_ids", [])),
            "bundle_ids": bundle_ts,  # now dict[str, float]
            "critical_shown_ids": set(raw.get("critical_shown_ids", [])),
            "briefs_emitted_last_turn": bool(raw.get("briefs_emitted_last_turn", False)),
            "force_window_last_fire_date": raw.get("force_window_last_fire_date", ""),
        }
    except (OSError, json.JSONDecodeError, ValueError):
        return {
            "inject_ts": 0.0,
            "seen_ids": set(),
            "bundle_ids": {},
            "critical_shown_ids": set(),
            "briefs_emitted_last_turn": False,
            "force_window_last_fire_date": "",
        }


# F5: TTL after which a bundle is eligible for re-injection in same session
BUNDLE_TTL_SEC = 7200  # 2 hours


def _save_state(
    session_id: str,
    inject_ts: float,
    seen_ids: set,
    bundle_ids,  # dict[str, float] (new) or set/list (legacy callers)
    critical_shown_ids: set | None = None,
    briefs_emitted_last_turn: bool = False,
) -> None:
    """Persist session inject state to /tmp.
    Tolerates legacy callers passing bundle_ids as set/list (converts to dict).
    """
    path = _state_path(session_id)
    if isinstance(bundle_ids, (set, list)):
        bundle_ids = {bid: time.time() for bid in bundle_ids}
    if critical_shown_ids is None:
        critical_shown_ids = set()
    try:
        with open(path, "w") as f:
            json.dump({
                "inject_ts": inject_ts,
                "seen_ids": list(seen_ids),
                "bundle_ids": dict(bundle_ids),
                "critical_shown_ids": list(critical_shown_ids),
                "briefs_emitted_last_turn": bool(briefs_emitted_last_turn),
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


def _format_brief(brief, idx, also_reported_by: list | None = None):
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

def _ready_bundles(max_count: int = 3) -> list:
    """
    Return list of (bundle_id, bundle_dict) for up to max_count bundles in ready/,
    sorted newest-first by filename date. Skips malformed files with a warning.
    Returns empty list if ready/ is empty or all files are malformed.
    FP-18: replaces single-bundle _latest_ready_bundle().
    """
    pattern = os.path.join(BUNDLE_READY_DIR, "*_bundle.json")
    paths = sorted(glob.glob(pattern), reverse=True)  # newest first
    if not paths:
        return []

    _BUNDLE_REQUIRED = ["bundle_id", "date", "assembled_at", "summaries_count", "summaries"]
    results = []
    for path in paths:
        if len(results) >= max_count:
            break
        try:
            raw = open(path, "rb").read()
            bundle = json.loads(raw)
        except (OSError, json.JSONDecodeError) as e:
            print(f"[inbox_hook] WARN: cannot read bundle {path} — {e}", file=sys.stderr)
            continue

        bundle_id = bundle.get("bundle_id")
        if not bundle_id:
            print(f"[inbox_hook] WARN: bundle {path} missing bundle_id", file=sys.stderr)
            continue

        valid = True
        for field in _BUNDLE_REQUIRED:
            if field not in bundle:
                print(f"[inbox_hook] WARN: bundle {path} missing field '{field}', skipping", file=sys.stderr)
                valid = False
                break
        if not valid:
            continue
        if not isinstance(bundle.get("summaries"), dict):
            print(f"[inbox_hook] WARN: bundle {path} 'summaries' not a dict, skipping", file=sys.stderr)
            continue

        results.append((bundle_id, bundle))

    return results


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
        "For carry-forward items (numbered after today's actions), set decision.provenance = "
        "{from_carried_forward: true, carried_from: '<YYYY-MM-DD>'} in the verdict decision. "
        "RECURRING carry-forward items (tagged [REC]) are NOT numbered — skip them entirely. "
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

    # Per-host tables — one section per host (Mac / Hel / London) showing each
    # daemon's findings and escalated-action count. This is the "3 tables, 1
    # per system" view the user expects: each row a daemon, each table a host,
    # so you can scan host-by-host.
    if summaries:
        # Group keys by host: {host: [(daemon, summary_dict), ...]}
        per_host: dict[str, list[tuple[str, dict]]] = {}
        for daemon_key, s in summaries.items():
            if "@" in daemon_key:
                daemon, host = daemon_key.split("@", 1)
            else:
                daemon, host = daemon_key, "unknown"
            per_host.setdefault(host, []).append((daemon, s))

        host_order = ["mac", "hel", "london"]
        host_emoji = {"mac": "💻", "hel": "🌐", "london": "🌍"}
        for host in host_order:
            entries = per_host.get(host, [])
            if not entries:
                continue
            host_total_findings = sum(
                e[1].get("ship_phases", {}).get("land", {}).get("findings_total", 0)
                for e in entries
            )
            host_total_actions = sum(len(e[1].get("proposed_actions", [])) for e in entries)
            lines.append("")
            lines.append(f"### {host_emoji.get(host, '🖥')} {host.upper()} — "
                         f"{host_total_findings} findings, {host_total_actions} actions, "
                         f"{len(entries)} daemons")
            for daemon, s in sorted(entries):
                findings = s.get("ship_phases", {}).get("land", {}).get("findings_total", 0)
                actions  = len(s.get("proposed_actions", []))
                health   = s.get("self_report", {}).get("daemon_health", "?")
                health_emoji = {"green": "🟢", "yellow": "🟡", "red": "🔴"}.get(health, "⚪")
                lines.append(
                    f"  {health_emoji} {daemon:14s}  findings={findings:<4}  actions={actions}"
                )

        # Any host the loop missed (e.g. a future "tokyo")
        leftover = [h for h in per_host if h not in host_order]
        for host in leftover:
            entries = per_host[host]
            host_total_findings = sum(
                e[1].get("ship_phases", {}).get("land", {}).get("findings_total", 0)
                for e in entries
            )
            host_total_actions = sum(len(e[1].get("proposed_actions", [])) for e in entries)
            lines.append("")
            lines.append(f"### 🖥 {host.upper()} — "
                         f"{host_total_findings} findings, {host_total_actions} actions, "
                         f"{len(entries)} daemons")
            for daemon, s in sorted(entries):
                findings = s.get("ship_phases", {}).get("land", {}).get("findings_total", 0)
                actions  = len(s.get("proposed_actions", []))
                health   = s.get("self_report", {}).get("daemon_health", "?")
                health_emoji = {"green": "🟢", "yellow": "🟡", "red": "🔴"}.get(health, "⚪")
                lines.append(
                    f"  {health_emoji} {daemon:14s}  findings={findings:<4}  actions={actions}"
                )

    # CARRIED FORWARD section — unconsumed findings from prior days
    # Schema: cross_refs.carried_forward = {prior_date: [action, ...]}
    # Each action is a proposed_action dict annotated with:
    #   carried_from (str), carry_status ("CARRY_FORWARD" | "RECURRING")
    # Populated by collector.py _assemble_bundle [cited collector.py:335]
    # Numbering: carry-forward items with carry_status != "RECURRING" continue the
    # global num counter from today's actions. RECURRING items are display-only (no number).
    _sev_tag = {
        "CRITICAL": "[CRIT]",
        "HIGH":     "[HIGH]",
        "MEDIUM":   "[ MED]",
        "MED":      "[ MED]",
        "LOW":      "[ LOW]",
        "INFO":     "[INFO]",
    }
    cross_refs = bundle.get("cross_refs") or {}
    carried_forward: dict = cross_refs.get("carried_forward") or {}
    if carried_forward:
        n_days = len(carried_forward)
        n_findings = sum(len(v) for v in carried_forward.values())
        carry_new_count = cross_refs.get("carry_new_count") or 0
        carry_recurring_count = cross_refs.get("carry_recurring_count") or 0
        lines.append("")
        lines.append(
            f"### \U0001f4e6 CARRIED FORWARD ({n_days} day{'s' if n_days != 1 else ''}, "
            f"{n_findings} finding{'s' if n_findings != 1 else ''} — "
            f"{carry_new_count} new, {carry_recurring_count} recurring)"
        )
        for prior_date in sorted(carried_forward.keys()):
            day_actions = carried_forward[prior_date]
            lines.append(f"  \U0001f4c5 from {prior_date} ({len(day_actions)} finding{'s' if len(day_actions) != 1 else ''})")
            for act in day_actions:
                # severity — check 'risk' field (proposed_action schema uses 'risk')
                risk_raw = (act.get("risk") or "").upper()
                sev_label = _sev_tag.get(risk_raw, f"[{risk_raw[:4]:4s}]" if risk_raw else "[ ? ]")
                # daemon@host — from 'id' prefix or direct fields
                daemon_host = act.get("daemon_host", "")
                if not daemon_host:
                    # Try to reconstruct from id: e.g. "lint_mac_20260428_abc123"
                    act_id = act.get("id") or act.get("finding_id") or ""
                    parts = act_id.split("_")
                    if len(parts) >= 2:
                        daemon_host = f"{parts[0]}@{parts[1]}"
                    else:
                        daemon_host = "?@?"
                title = act.get("title") or "[no title]"
                fid = act.get("finding_id") or act.get("id") or "?"
                fid_short = fid[:12] if len(fid) > 12 else fid
                carry_status = act.get("carry_status", "")
                from_skipped = act.get("from_skipped_day", False)
                # RECURRING items: display-only, no approval number (already in today's queue)
                is_recurring = (carry_status == "RECURRING")
                status_tag = " [REC]" if is_recurring else ""
                if from_skipped:
                    status_tag += " [SKIP]"
                if is_recurring:
                    # No number — recurring items appear in today's numbered queue already
                    lines.append(
                        f"    {sev_label} {daemon_host:<18s}  {title[:60]:<60s}  …{fid_short}{status_tag}"
                    )
                else:
                    # Continue global numbering from today's actions
                    lines.append(
                        f"    {num}. {sev_label} {daemon_host:<18s}  {title[:60]:<60s}  …{fid_short}{status_tag}"
                    )
                    num += 1

    # OPEN SCRIBBLES section — aging memos from /memo store (memo-v2 S6)
    # Invokes ~/.claude/skills/memo/scripts/list_aging.py via subprocess.
    # Subprocess (not direct import) keeps hook lean + lets us swap memo
    # backend later. Degrades silently on any failure (missing script,
    # missing _index.jsonl, timeout) — never crash the digest render.
    try:
        import os as _os
        import subprocess as _sp
        _aging_script = _os.path.expanduser(
            "~/.claude/skills/memo/scripts/list_aging.py"
        )
        if _os.path.isfile(_aging_script):
            _proc = _sp.run(
                ["python3", _aging_script, "--with-total",
                 "--threshold", "7", "--limit", "10"],
                capture_output=True, text=True, timeout=2,
            )
            if _proc.returncode == 0 and _proc.stdout.strip():
                _payload = json.loads(_proc.stdout)
                _aging_rows = _payload.get("rows") or []
                _aging_total = int(_payload.get("total") or 0)
                if _aging_rows:
                    lines.append("")
                    lines.append(
                        f"## \U0001f4cc OPEN SCRIBBLES "
                        f"({_aging_total} unanswered, >7 days)"
                    )
                    for _row in _aging_rows:
                        _tag = (_row.get("primary_tag") or "").strip()
                        _tag_disp = f"[#{_tag}]" if _tag else "[?]"
                        # 8-char tag column right-padded
                        _tag_col = f"{_tag_disp:<8s}"
                        _age = int(_row.get("ts_age_days") or 0)
                        _preview = (_row.get("body_preview") or "").strip()
                        lines.append(
                            f"  {_tag_col}  {_age}d ago — {_preview}"
                        )
                    if _aging_total > len(_aging_rows):
                        _more = _aging_total - len(_aging_rows)
                        lines.append(
                            f"  … +{_more} more — "
                            f"run /memo --since 7d for full list"
                        )
    except Exception:
        # Silent degrade — never crash digest on memo plumbing issue
        pass

    lines += [
        "",
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
        # PATH C: Daily force-window inject (15:00 HKT, once per day max)
        # Runs BEFORE intent gate (handled in __main__) and BEFORE PATH A/B.
        # Uses cross-session global state file for once-per-day dedup.
        # When PATH C fires, it returns immediately — does NOT also run A/B.
        # ------------------------------------------------------------------
        today_str = now.strftime("%Y-%m-%d")
        global_state = _load_force_window_global()
        if (
            _is_force_window(now)
            and not _force_window_already_fired_today(global_state, today_str)
        ):
            panel = _build_force_window_panel(today_str)
            if panel is not None:
                _save_force_window_global(today_str)
                # Persist sentinel for inbox_ack — briefs were emitted
                _save_state(
                    session_id,
                    time.time(),
                    state["seen_ids"],
                    state["bundle_ids"],
                    critical_shown_ids=state["critical_shown_ids"],
                    briefs_emitted_last_turn=True,
                )
                out = json.dumps({"additionalContext": panel})
                print(
                    f"[inbox_hook] PATH C: daily force-window injected for {today_str}",
                    file=sys.stderr,
                )
                log_fire_done(__file__, _t0, errored=False, output_size_bytes=len(out))
                print(out)
                return
            else:
                print(
                    f"[inbox_hook] PATH C: window open for {today_str} but no content "
                    "(no critical briefs, no consumed bundle) — falling through",
                    file=sys.stderr,
                )

        # ------------------------------------------------------------------
        # PATH A: Bundle injection (P10.15 + FP-18: up to 3-bundle aggregation)
        # F5 hybrid: bundle is "fresh" if either never seen this session OR
        # last-injected > BUNDLE_TTL_SEC ago. Bundle digest filters out
        # acked actions via state["seen_ids"] (best-effort; per-action acked_action_ids
        # would require ack pipeline changes — out of scope for Phase 3).
        # ------------------------------------------------------------------
        ready = _ready_bundles(max_count=3)

        now_ts = time.time()
        # F5: bundle eligible if not seen OR last-seen older than TTL
        new_bundles = [
            (bid, b) for bid, b in ready
            if (bid not in state["bundle_ids"])
            or (now_ts - state["bundle_ids"].get(bid, 0.0) > BUNDLE_TTL_SEC)
        ]

        # F4 additive design: PATH A may emit a bundle digest;
        # PATH B (critical-always) may also emit a critical panel.
        # Both concatenate into additionalContext if both have content.
        path_a_context = None
        path_a_consumed_ids = []

        if new_bundles:
            digest_parts = []
            consumed_ids = []

            for bundle_id, bundle in new_bundles:
                # Format bundle
                try:
                    bundle_text = _format_bundle(bundle)
                except Exception as e:
                    print(f"[inbox_hook] WARN: bundle {bundle_id} format failed ({e}), skipping", file=sys.stderr)
                    continue

                # Build category-grouped approval queue digest (FP-9/10)
                try:
                    digest_text = _format_bundle_digest(bundle)
                except Exception as de:
                    print(f"[inbox_hook] WARN: bundle {bundle_id} digest failed ({de}), using raw", file=sys.stderr)
                    digest_text = bundle_text

                digest_parts.append(digest_text)
                consumed_ids.append(bundle_id)

            if consumed_ids:
                if len(digest_parts) == 1:
                    path_a_context = digest_parts[0]
                else:
                    sep = "\n\n" + ("=" * 60) + "\n\n"
                    path_a_context = sep.join(digest_parts)
                    path_a_context = f"[{len(digest_parts)} bundles aggregated — FP-18]\n\n" + path_a_context

                # Consume all: move ready -> consumed (graceful fail, non-blocking)
                for bid in consumed_ids:
                    _consume_bundle(bid)

                path_a_consumed_ids = consumed_ids

                # F5: record bundle injection timestamps (for TTL re-inject)
                for bid in consumed_ids:
                    state["bundle_ids"][bid] = now_ts

                print(f"[inbox_hook] PATH A: injected {len(consumed_ids)} bundle(s): {consumed_ids}", file=sys.stderr)

        elif ready:
            # All ready bundles already injected this session AND TTL not yet expired
            all_ids = [bid for bid, _ in ready]
            print(f"[inbox_hook] all ready bundles within TTL window: {all_ids}", file=sys.stderr)

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

        # F4: critical-always sub-path. Surface up to N=5 oldest critical
        # briefs not yet shown this session, even if PATH A bundle was injected.
        # Independent dedup via state["critical_shown_ids"].
        path_b_context = None
        path_b_critical_shown = []
        CRITICAL_PAGE_SIZE = 5
        critical_briefs = _load_briefs("critical")
        critical_unshown = [
            (p, b) for p, b in critical_briefs
            if b.get("id", "") not in state["critical_shown_ids"]
        ]
        # Sort oldest-first by mtime so user works through backlog
        def _crit_sort_key(item):
            p, _ = item
            try:
                return os.path.getmtime(p)
            except OSError:
                return 0.0
        critical_unshown.sort(key=_crit_sort_key)
        critical_page = critical_unshown[:CRITICAL_PAGE_SIZE]

        if critical_page:
            total_crit = len(critical_briefs)
            unshown_total = len(critical_unshown)
            page_label = (
                f"critical-page ({len(critical_page)} of {unshown_total} unshown; "
                f"{total_crit} total in critical/) — reply 'ack <id> 1' or 'more' for next page"
            )
            path_b_context = _format_host_grouped(critical_page, page_label)
            path_b_critical_shown = [b.get("id", "") for _, b in critical_page]
            print(
                f"[inbox_hook] PATH B critical-always: showing {len(critical_page)} of {unshown_total}",
                file=sys.stderr,
            )

        # Daily / weekly windows still use the legacy first-inject + delta logic
        # WITHOUT the critical/ duplication (already covered above).
        legacy_candidates: list[tuple[str, dict]] = []
        if _in_daily_window(now):
            legacy_candidates.extend(_load_briefs("daily"))
        if _in_weekly_window(now):
            legacy_candidates.extend(_load_briefs("weekly"))

        legacy_context = None
        if legacy_candidates:
            if is_first_inject:
                selected = legacy_candidates
                inject_label = f"all ({len(selected)} daily/weekly briefs, first inject this session)"
            else:
                selected = [(p, b) for p, b in legacy_candidates if _is_delta(b, state)]
                inject_label = f"delta ({len(selected)} daily/weekly new/updated since last inject)"
            if selected:
                legacy_context = _format_host_grouped(selected, inject_label)

        # ------------------------------------------------------------------
        # Combine PATH A + PATH B critical + legacy daily/weekly contexts
        # ------------------------------------------------------------------
        parts = [c for c in (path_a_context, path_b_context, legacy_context) if c]
        emitted_briefs = bool(parts)

        if emitted_briefs:
            sep = "\n\n" + ("=" * 60) + "\n\n"
            context = sep.join(parts)
            out = json.dumps({"additionalContext": context})
        else:
            out = json.dumps({})

        # Update session state. Sentinel for inbox_ack F2 gate.
        new_seen = state["seen_ids"] | {b.get("id", "") for _, b in legacy_candidates}
        new_critical_shown = state["critical_shown_ids"] | set(path_b_critical_shown)
        _save_state(
            session_id,
            time.time(),
            new_seen,
            state["bundle_ids"],
            critical_shown_ids=new_critical_shown,
            briefs_emitted_last_turn=emitted_briefs,
        )

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
    # PATH C bypass: if force-window is open and hasn't fired today, run main()
    # even when intent gate would otherwise block. main() will run PATH C and return
    # before touching PATH A/B.
    _now_hkt = datetime.now(tz=HKT)
    _today_str = _now_hkt.strftime("%Y-%m-%d")
    _global = _load_force_window_global()
    _force_window_pending = (
        _is_force_window(_now_hkt)
        and not _force_window_already_fired_today(_global, _today_str)
    )
    if not _force_window_pending and not should_fire(__file__, _prompt):
        print(json.dumps({}))
        sys.exit(0)
    main()

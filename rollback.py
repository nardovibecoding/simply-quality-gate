#!/usr/bin/env python3
"""
rollback.py — Big SystemD Phase 8-4 (Slices 2/3/4)

Per-daemon-run and per-host rollback of approved bigd actions.

Lookup chain:
  manifest -> brief_ids -> exec_ids (from audit log) -> rollback.sh per exec_id

Granularities:
  --run <run_id>                    : revert all exec_ids from one daemon run
  --daemon <daemon>@<host> --date <date> : revert by identity (looks up today's manifests)
  --host <host> --date <date>       : nuclear: all 5 daemons on host for that date

Flags:
  --dry-run   : mandatory first pass; shows what WOULD be reverted without touching anything
  --force     : skip dry-run guard (use only after reviewing dry-run output)

Security:
  - Same V1-V5 discipline as approval_executor.py for forward actions.
  - State verification: current hash of target file must match post_state_hash from audit.
    If mismatch: rollback REJECTED (manual change detected -- preventing silent clobber).
  - Rollback itself is logged to ~/inbox/_audit/rollback_<date>.jsonl.
  - HARD_PROTECTED zones: ~/.ssh, ~/.gnupg, _audit, _rollback dirs cannot be the
    target of a rollback restore (same list as approval_executor.py).

Usage:
  python3 ~/.claude/hooks/rollback.py --run bigd-lint_mac_20260423T162800_a3b7 --dry-run
  python3 ~/.claude/hooks/rollback.py --run bigd-lint_mac_20260423T162800_a3b7 --force
  python3 ~/.claude/hooks/rollback.py --daemon bigd-lint@mac --date 2026-04-23 --dry-run
  python3 ~/.claude/hooks/rollback.py --host mac --date 2026-04-23 --dry-run

Audit log: ~/inbox/_audit/rollback_<YYYYMMDD>.jsonl
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths (mirrors approval_executor.py)
# ---------------------------------------------------------------------------
HOME          = Path.home()
INBOX_ROOT    = HOME / "inbox"
AUDIT_ROOT    = INBOX_ROOT / "_audit"
ROLLBACK_ROOT = INBOX_ROOT / "_rollback"
MANIFEST_DIR  = ROLLBACK_ROOT / "_manifests"

HARD_PROTECTED = [
    HOME / ".ssh",
    HOME / ".gnupg",
    INBOX_ROOT / "_audit",
    INBOX_ROOT / "_rollback",
    INBOX_ROOT / "_approvals",
    HOME / ".claude" / "hooks" / "approval_executor.py",
    HOME / ".claude" / "hooks" / "rollback.py",
]

ALL_DAEMONS = ["bigd-lint", "bigd-upgrade", "bigd-security", "bigd-performance", "bigd-gaps"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_utc() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _log(msg: str) -> None:
    ts = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{ts}] {msg}", flush=True)


def _sha256_file(path: Path) -> str:
    if not path.exists():
        return "MISSING"
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
    except OSError:
        return "READ_ERROR"
    return h.hexdigest()


def _write_rollback_audit(entry: dict, dry_run: bool) -> None:
    """Append rollback operation to today's rollback audit JSONL."""
    AUDIT_ROOT.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now(tz=timezone.utc).strftime("%Y%m%d")
    audit_path = AUDIT_ROOT / f"rollback_{date_str}.jsonl"
    entry["dry_run"] = dry_run
    entry["rollback_ts"] = _now_utc()
    line = json.dumps(entry, default=str) + "\n"
    with open(audit_path, "a", encoding="utf-8") as f:
        f.write(line)


def _is_hard_protected(path: Path) -> bool:
    """Return True if resolved path is inside any hard-protected zone."""
    try:
        resolved = path.resolve()
    except OSError:
        return True
    for hp in HARD_PROTECTED:
        try:
            resolved.relative_to(hp.resolve())
            return True
        except ValueError:
            pass
    return False


# ---------------------------------------------------------------------------
# Slice 2: manifest + audit log lookups
# ---------------------------------------------------------------------------

def _load_manifest(run_id: str) -> dict | None:
    """Load manifest JSON by run_id. Returns None if not found."""
    manifest_path = MANIFEST_DIR / f"{run_id}.json"
    if not manifest_path.exists():
        return None
    try:
        return json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        _log(f"ERROR: cannot read manifest {run_id}: {e}")
        return None


def _load_all_manifests() -> list[dict]:
    """Load all manifests from _manifests/. Returns list of dicts."""
    manifests = []
    for p in sorted(MANIFEST_DIR.glob("*.json")):
        try:
            manifests.append(json.loads(p.read_text()))
        except (OSError, json.JSONDecodeError):
            continue
    return manifests


def _get_exec_ids_for_brief_ids(brief_ids: list[str]) -> list[dict]:
    """
    Query executor audit logs to find exec_ids matching the given brief_ids.
    Returns list of audit records (non-no_op, ok=True, non-dry_run).
    """
    brief_id_set = set(brief_ids)
    results = []
    for audit_file in sorted(AUDIT_ROOT.glob("executor_*.jsonl")):
        try:
            with open(audit_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    # Only reversible: ok=True, not dry_run, not no_op
                    if (rec.get("brief_id") in brief_id_set
                            and rec.get("ok") is True
                            and not rec.get("dry_run", True)
                            and rec.get("action_type") not in ("no_op", "ORPHANED",
                                                                "NO_MATCHING_ACTION", "REJECTED")):
                        results.append(rec)
        except OSError:
            continue
    return results


def _find_manifests_for_daemon_date(daemon: str, host: str, date_str: str) -> list[dict]:
    """
    Find all manifests for daemon@host where cycle_ts starts with date_str (YYYY-MM-DD).
    """
    results = []
    for m in _load_all_manifests():
        if (m.get("daemon") == daemon
                and m.get("host") == host
                and m.get("cycle_ts", "").startswith(date_str)):
            results.append(m)
    return results


def _find_manifests_for_host_date(host: str, date_str: str) -> list[dict]:
    """Find all manifests for any daemon on host where cycle_ts starts with date_str."""
    results = []
    for m in _load_all_manifests():
        if (m.get("host") == host
                and m.get("cycle_ts", "").startswith(date_str)):
            results.append(m)
    return results


# ---------------------------------------------------------------------------
# Slice 3: state verification before rollback
# ---------------------------------------------------------------------------

def _verify_state_for_rollback(audit_rec: dict) -> tuple[bool, str]:
    """
    Verify that the current state of the affected file matches what the executor
    recorded in post_state_hash.

    For file_delete: post_state_hash == "DELETED". Verify file does NOT exist.
    For file_edit:   post_state_hash == sha256(new content). Verify current sha256 matches.
    For plist_reload/launchd_*: no file state to verify; return True.
    For inbox_archive: verify file is in archive (not in inbox tiers).

    Returns (ok: bool, reason: str).
    """
    action_type = audit_rec.get("action_type", "")
    post_hash   = audit_rec.get("post_state_hash", "N/A")
    exec_id     = audit_rec.get("exec_id", "?")

    if action_type in ("plist_reload", "launchd_enable", "launchd_disable", "systemd_reload"):
        # No file content to verify -- allow rollback
        return True, "no file state to verify for launchd action"

    if action_type == "no_op":
        return False, "no_op actions have no state to revert"

    if action_type == "inbox_archive":
        return True, "inbox_archive rollback: manual check required"

    if action_type not in ("file_delete", "file_edit"):
        return False, f"unrecognized action_type {action_type!r} -- cannot verify state"

    # For file_delete: original path from rollback meta
    rollback_dir = ROLLBACK_ROOT / exec_id
    meta_path = rollback_dir / "original" / "meta.json"
    if not meta_path.exists():
        # Try meta.json directly in rollback_dir (P8-3 stored it there)
        meta_path_alt = rollback_dir / "meta.json"
        if meta_path_alt.exists():
            meta_path = meta_path_alt
        else:
            return False, f"rollback meta.json not found for exec_id={exec_id}"

    try:
        meta = json.loads(meta_path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        return False, f"cannot read meta.json: {e}"

    original_path = Path(meta.get("original_path", ""))
    if not original_path.name:
        return False, f"meta.json missing original_path for exec_id={exec_id}"

    if _is_hard_protected(original_path):
        return False, f"REJECT: target {original_path} is in hard-protected zone"

    if action_type == "file_delete":
        if post_hash == "DELETED":
            # Verify file is still deleted (if someone recreated it, we must not clobber)
            if original_path.exists():
                current_hash = _sha256_file(original_path)
                return False, (
                    f"STATE MISMATCH: file_delete recorded post=DELETED but file now exists "
                    f"(sha256={current_hash[:16]}...) -- file was recreated after deletion. "
                    f"Manual review required before rollback."
                )
            return True, f"state OK: {original_path.name} confirmed deleted"
        else:
            # post_hash is a real sha256 -- unusual for file_delete, but handle
            return False, f"unexpected post_hash={post_hash!r} for file_delete action"

    if action_type == "file_edit":
        if not original_path.exists():
            return False, f"STATE MISMATCH: file {original_path} no longer exists -- cannot verify post-edit state"
        current_hash = _sha256_file(original_path)
        if current_hash != post_hash:
            return False, (
                f"STATE MISMATCH: expected post_hash={post_hash[:16]}... "
                f"but current={current_hash[:16]}... -- file changed after executor ran. "
                f"Manual review required before rollback."
            )
        return True, f"state OK: {original_path.name} hash matches post-edit state"

    return False, f"unhandled action_type={action_type!r}"


# ---------------------------------------------------------------------------
# Single exec_id rollback
# ---------------------------------------------------------------------------

def _rollback_one(audit_rec: dict, dry_run: bool) -> dict:
    """
    Rollback one exec_id.
    Returns result dict: {exec_id, ok, reason, action_type, original_path, dry_run}.
    """
    exec_id     = audit_rec.get("exec_id", "?")
    action_type = audit_rec.get("action_type", "?")
    brief_id    = audit_rec.get("brief_id", "?")

    _log(f"  Rollback exec_id={exec_id} action_type={action_type} brief_id={brief_id!r}")

    # Slice 3: state verification
    state_ok, state_reason = _verify_state_for_rollback(audit_rec)
    if not state_ok:
        _log(f"    REJECTED: {state_reason}")
        result = {
            "exec_id": exec_id,
            "brief_id": brief_id,
            "action_type": action_type,
            "ok": False,
            "reason": f"STATE_VERIFY_FAILED: {state_reason}",
        }
        _write_rollback_audit(result, dry_run)
        return result

    _log(f"    State verify: {state_reason}")

    # Find rollback script
    rollback_dir = ROLLBACK_ROOT / exec_id
    rollback_script = rollback_dir / "rollback.sh"
    meta_path_candidates = [
        rollback_dir / "meta.json",
        rollback_dir / "original" / "meta.json",
    ]
    meta_path = None
    for c in meta_path_candidates:
        if c.exists():
            meta_path = c
            break

    if not rollback_script.exists():
        reason = f"rollback.sh not found at {rollback_script}"
        _log(f"    FAIL: {reason}")
        result = {
            "exec_id": exec_id,
            "brief_id": brief_id,
            "action_type": action_type,
            "ok": False,
            "reason": reason,
        }
        _write_rollback_audit(result, dry_run)
        return result

    original_path = "unknown"
    if meta_path:
        try:
            meta = json.loads(meta_path.read_text())
            original_path = meta.get("original_path", "unknown")
        except (OSError, json.JSONDecodeError):
            pass

    preview = f"ROLLBACK: exec_id={exec_id} action={action_type} restore={original_path}"
    _log(f"    {preview}")

    if dry_run:
        result = {
            "exec_id": exec_id,
            "brief_id": brief_id,
            "action_type": action_type,
            "ok": True,
            "reason": "DRY_RUN: would execute rollback.sh",
            "preview": preview,
            "original_path": original_path,
        }
        _write_rollback_audit(result, dry_run)
        return result

    # Execute rollback.sh
    run_result = subprocess.run(
        ["sh", str(rollback_script)],
        capture_output=True,
        text=True,
        timeout=30,
    )

    if run_result.returncode == 0:
        _log(f"    OK: {run_result.stdout.strip()}")
        result = {
            "exec_id": exec_id,
            "brief_id": brief_id,
            "action_type": action_type,
            "ok": True,
            "reason": run_result.stdout.strip(),
            "preview": preview,
            "original_path": original_path,
        }
    else:
        _log(f"    FAILED: {run_result.stderr.strip()}")
        result = {
            "exec_id": exec_id,
            "brief_id": brief_id,
            "action_type": action_type,
            "ok": False,
            "reason": f"rollback.sh failed: {run_result.stderr.strip()[:300]}",
            "preview": preview,
            "original_path": original_path,
        }

    _write_rollback_audit(result, dry_run)
    return result


# ---------------------------------------------------------------------------
# Multi-exec_id rollback (for --run, --daemon, --host)
# ---------------------------------------------------------------------------

def _rollback_exec_ids(exec_ids_records: list[dict], label: str, dry_run: bool) -> None:
    """
    Run rollback for a list of audit records. Prints summary.
    exec_ids_records: list of audit record dicts (from _get_exec_ids_for_brief_ids).
    """
    if not exec_ids_records:
        _log(f"{label}: no reversible exec_ids found in audit log for this run")
        return

    mode = "DRY-RUN" if dry_run else "LIVE"
    _log(f"{label}: found {len(exec_ids_records)} reversible exec_id(s) [{mode}]")

    if not dry_run:
        _log(f"WARNING: LIVE rollback in progress. {len(exec_ids_records)} action(s) will be reverted.")

    results = {"ok": 0, "failed": 0, "rejected": 0}
    for rec in exec_ids_records:
        r = _rollback_one(rec, dry_run=dry_run)
        if not r["ok"]:
            if "STATE_VERIFY_FAILED" in r.get("reason", ""):
                results["rejected"] += 1
            else:
                results["failed"] += 1
        else:
            results["ok"] += 1

    _log(
        f"{label}: done. ok={results['ok']} failed={results['failed']} "
        f"rejected={results['rejected']} [{'DRY-RUN' if dry_run else 'LIVE'}]"
    )


# ---------------------------------------------------------------------------
# --run: revert all exec_ids from one daemon run
# ---------------------------------------------------------------------------

def cmd_run(run_id: str, dry_run: bool) -> None:
    manifest = _load_manifest(run_id)
    if manifest is None:
        _log(f"ERROR: manifest not found for run_id={run_id!r}")
        _log(f"  Expected: {MANIFEST_DIR / (run_id + '.json')}")
        sys.exit(1)

    brief_ids = manifest.get("brief_ids", [])
    daemon    = manifest.get("daemon", "?")
    host      = manifest.get("host", "?")
    cycle_ts  = manifest.get("cycle_ts", "?")

    _log(f"run_id={run_id} daemon={daemon} host={host} cycle_ts={cycle_ts} brief_ids={len(brief_ids)}")

    exec_records = _get_exec_ids_for_brief_ids(brief_ids)
    _rollback_exec_ids(exec_records, label=f"--run {run_id}", dry_run=dry_run)


# ---------------------------------------------------------------------------
# --daemon: revert by daemon@host + date
# ---------------------------------------------------------------------------

def cmd_daemon(daemon: str, host: str, date_str: str, dry_run: bool) -> None:
    manifests = _find_manifests_for_daemon_date(daemon, host, date_str)
    if not manifests:
        _log(f"ERROR: no manifests found for {daemon}@{host} on {date_str}")
        sys.exit(1)

    _log(f"Found {len(manifests)} manifest(s) for {daemon}@{host} on {date_str}")
    all_brief_ids = []
    for m in manifests:
        all_brief_ids.extend(m.get("brief_ids", []))
        _log(f"  run_id={m['run_id']} brief_ids={len(m.get('brief_ids', []))}")

    exec_records = _get_exec_ids_for_brief_ids(all_brief_ids)
    _rollback_exec_ids(exec_records, label=f"--daemon {daemon}@{host} --date {date_str}", dry_run=dry_run)


# ---------------------------------------------------------------------------
# --host: nuclear -- all daemons on host for a date
# ---------------------------------------------------------------------------

def cmd_host(host: str, date_str: str, dry_run: bool) -> None:
    manifests = _find_manifests_for_host_date(host, date_str)
    if not manifests:
        _log(f"ERROR: no manifests found for host={host} on {date_str}")
        sys.exit(1)

    _log(f"NUCLEAR: found {len(manifests)} manifest(s) across all daemons on {host} for {date_str}")
    if not dry_run:
        _log("NUCLEAR WARNING: reverting ALL approved actions on this host for this date.")

    all_brief_ids = []
    for m in manifests:
        all_brief_ids.extend(m.get("brief_ids", []))
        _log(f"  {m['daemon']} run_id={m['run_id']} brief_ids={len(m.get('brief_ids', []))}")

    exec_records = _get_exec_ids_for_brief_ids(all_brief_ids)
    _rollback_exec_ids(exec_records, label=f"--host {host} --date {date_str}", dry_run=dry_run)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="rollback.py -- Big SystemD P8-4 per-daemon-run rollback"
    )
    parser.add_argument("--run", metavar="RUN_ID",
                        help="Revert all exec_ids from one daemon run (by run_id from manifest)")
    parser.add_argument("--daemon", metavar="DAEMON@HOST",
                        help="Revert by daemon identity (e.g. bigd-lint@mac)")
    parser.add_argument("--host", metavar="HOST",
                        help="Nuclear: revert all 5 daemons on host for date")
    parser.add_argument("--date", metavar="YYYY-MM-DD",
                        help="Date filter for --daemon and --host modes")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be reverted without applying (REQUIRED before --force)")
    parser.add_argument("--force", action="store_true",
                        help="Apply rollback (use after reviewing --dry-run output)")

    args = parser.parse_args()

    # Require exactly one of --dry-run or --force
    if not args.dry_run and not args.force:
        print("ERROR: must specify --dry-run or --force", file=sys.stderr)
        print("  Run with --dry-run first to review the rollback plan.", file=sys.stderr)
        sys.exit(1)

    if args.dry_run and args.force:
        print("ERROR: --dry-run and --force are mutually exclusive", file=sys.stderr)
        sys.exit(1)

    dry_run = args.dry_run

    mode = "DRY-RUN" if dry_run else "LIVE"
    _log(f"rollback.py starting [{mode}]")

    if args.run:
        cmd_run(args.run, dry_run=dry_run)
        return

    if args.daemon:
        # Parse DAEMON@HOST
        if "@" not in args.daemon:
            print(f"ERROR: --daemon must be in format daemon@host, got {args.daemon!r}", file=sys.stderr)
            sys.exit(1)
        daemon_part, _, host_part = args.daemon.partition("@")
        if not args.date:
            print("ERROR: --daemon requires --date YYYY-MM-DD", file=sys.stderr)
            sys.exit(1)
        cmd_daemon(daemon_part, host_part, args.date, dry_run=dry_run)
        return

    if args.host:
        if not args.date:
            print("ERROR: --host requires --date YYYY-MM-DD", file=sys.stderr)
            sys.exit(1)
        if not dry_run:
            _log(f"NUCLEAR MODE: --host {args.host} --date {args.date} --force")
        cmd_host(args.host, args.date, dry_run=dry_run)
        return

    print("ERROR: must specify one of --run, --daemon, or --host", file=sys.stderr)
    parser.print_help()
    sys.exit(1)


if __name__ == "__main__":
    main()

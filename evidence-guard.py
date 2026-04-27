#!/usr/bin/env python3
"""
evidence-guard.py — unified "claim ≠ evidence" hook.

Three scopes share one verifier cache (~/.cache/claude-evidence/verifier.iso):
  bot-liveness    — block mtime/ls reads of BOT data files used to infer
                    bot alive/dead. Generalized from bot-liveness-guard.py.
  mtime-trap      — block mtime/ls reads of high-risk state/lock/watchdog/
                    ship-slug files used to infer writer activity.
  completion-claim — block completion-flavored Bash (commit, install, deploy,
                    disable-rename) when no verification command (test, smoke,
                    is-active, build, run-and-check-output) ran in last 10 min.

Modes (sys.argv[1]):
  pretool     — PreToolUse(Bash|Write|Edit): block per-scope rules above.
  posttool    — PostToolUse(Bash|Write|Edit): mark verifier-used when a
                content-reading or state-querying command is observed.
  userprompt  — UserPromptSubmit: emit reminder when prompt mentions
                liveness/completion keywords on a high-risk target.

Replaces (planned migration): bot-liveness-guard.py + mtime-trap-guard.py.
Lessons: 04-26 kalshi liveness misdiagnosis + 04-26 bot-data + 04-27 watchdog
state file + 04-27 SKILL.md frontmatter "shipped" claim missed T4.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

CACHE_DIR = Path.home() / ".cache" / "claude-evidence"
VERIFIER_TTL = timedelta(minutes=10)

BOTS = {
    "kalshi-bot": {"host": "hel"},
    "pm-bot":     {"host": "london"},
}
BOT_DATA_FILE_RE = re.compile(
    r"(signal-trace|trade-journal|signal_history|eval-history|portfolio\.json|"
    r"kalshi_cancels|clob_cancels|mm-roundtrips|virtual-fills|brier_tracking|"
    r"kmm-orders|signal_status|scanner\.log)",
    re.IGNORECASE,
)
BOT_LIVENESS_VERIFIER_RE = re.compile(
    r"systemctl\s+(?:is-active|status|show)\s+\S*(?:pm-bot|kalshi-bot)|"
    r"journalctl\s+[^|;&]*-u\s+(?:pm-bot|kalshi-bot)"
)

HIGH_RISK_PATH_RE = re.compile(
    r"("
    r"\.watchdog[_\-][^/\s]+\.json|"
    r"[^/\s]*[_\-]state\.json|"
    r"\.ship/[^/\s]+/(experiments|state)/|"
    r"scheduled_tasks\.json|"
    r"/tmp/[^/\s]+\.(?:lock|pid|state|log)|"
    r"[^/\s]+\.lock\b|"
    r"[^/\s]+\.pid\b|"
    r"[^/\s]*progress\.(?:json|md|txt)|"
    r"\.cache/[^/\s]+/[^/\s]+\.iso"
    r")",
    re.IGNORECASE,
)

MTIME_VERB_RE = re.compile(
    r"\bstat\s+-c\s*['\"]?[^'\"\s]*%[yY]|"
    r"\bls\s+-[a-zA-Z]*l[a-zA-Z]*t?\b|"
    r"\bls\s+-[a-zA-Z]*t\b|"
    r"\bfind\b[^|;&]*-(?:mtime|mmin|cmin|ctime|amin|atime)\b|"
    r"\bdate\s+-r\s"
)

VERIFIER_VERB_RE = re.compile(
    r"\btail\s+-?\d*|"
    r"\bhead\s+-?\d*|"
    r"\bcat\s+/|"
    r"\bwc\s+-l\b|"
    r"\blsof\b|"
    r"\bfuser\b|"
    r"\bsystemctl\s+(?:is-active|status|show)|"
    r"\bjournalctl\s|"
    r"\bps\s+-|"
    r"/proc/\d+/|"
    r"\binotifywait\b|"
    r"\bjq\b|"
    r"\bawk\b|"
    r"\bgrep\b|"
    r"\bsed\b|"
    r"\bsha\d+sum\b|"
    r"\bmd5sum\b|"
    r"\bdiff\b|"
    r"\bcurl\s|"
    r"\bnpm\s+(?:test|run\s+test)|"
    r"\bpytest\b|"
    r"\bbun\s+test|"
    r"\bnpm\s+(?:run\s+)?build|"
    r"\btsc\b|"
    r"\bbash\s+-n\b|"
    r"\bpython3?\s+-c\b|"
    r"\bpython3?\s+\S+\.py\b"
)

COMPLETION_ACTION_RE = re.compile(
    r"\bgit\s+commit\b|"
    r"\bgit\s+push\b|"
    r"\bsudo\s+install\s+-m\b|"
    r"\bsystemctl\s+(?:enable|restart|start)\b|"
    r"\bnpm\s+publish|"
    r"\bgh\s+(?:release|pr\s+create|pr\s+merge)|"
    r"\.disabled\s*$|"
    r"\bmv\s+\S+\.md\s+\S+\.disabled|"
    r"\bmv\s+\S+\.disabled\s+\S+\.md"
)

ACTIVITY_KEYWORDS = re.compile(
    r"\b(stale|fresh|active|inactive|alive|dead|writing|frozen|wedged|silent|"
    r"recent|last\s+(?:write|update|run|fired|touched|modified)|"
    r"shipped|wired|done|ready|complete|verified|deployed|live)\b",
    re.IGNORECASE,
)

LIVENESS_KEYWORDS = re.compile(
    r"\b(wedged|stuck|dead|alive|silent|frozen|hung|down|offline|crashed|"
    r"broken|stale|not\s+running|not\s+firing|last\s+scan|0\s+emits|wedge)\b",
    re.IGNORECASE,
)
BOT_NAME_PATTERN = re.compile(
    r"\b(kalshi[-_]?bot|pm[-_]?bot|polymarket|kalshi|hel\b|london\b|"
    r"prediction[-_]?market)\b",
    re.IGNORECASE,
)


def _safe_load_input() -> dict:
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return {}
        return json.loads(raw)
    except Exception:
        return {}


def _verifier_recently_used() -> bool:
    marker = CACHE_DIR / "verifier.iso"
    if not marker.exists():
        return False
    try:
        ts = marker.read_text().strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt) < VERIFIER_TTL
    except Exception:
        return False


def _mark_verifier() -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        (CACHE_DIR / "verifier.iso").write_text(
            datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        )
    except Exception:
        pass


def _bot_protocol_satisfied(unit: str) -> bool:
    marker = CACHE_DIR / f"{unit}-checked-at.iso"
    if not marker.exists():
        return False
    try:
        ts = marker.read_text().strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt) < timedelta(minutes=30)
    except Exception:
        return False


def _mark_bot_protocol(unit: str) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        (CACHE_DIR / f"{unit}-checked-at.iso").write_text(
            datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        )
    except Exception:
        pass


def handle_pretool(payload: dict) -> int:
    tool_input = payload.get("tool_input") or {}
    cmd = tool_input.get("command") or tool_input.get("file_path") or ""
    if not cmd:
        return 0

    if BOT_DATA_FILE_RE.search(cmd) and MTIME_VERB_RE.search(cmd) and not BOT_LIVENESS_VERIFIER_RE.search(cmd):
        if not any(_bot_protocol_satisfied(u) for u in BOTS):
            print(
                "\n⛔ evidence-guard BLOCK [bot-liveness]\n"
                "Reading bot data file mtime to infer liveness. Run 3-step protocol first:\n"
                "  ssh <host> 'systemctl is-active <unit>'\n"
                "  ssh <host> 'journalctl -u <unit> --since \"5 min ago\" | tail -20'\n"
                "  ssh <host> 'systemctl show <unit> -p MainPID -p ActiveEnterTimestamp'\n"
                "Bot registry: kalshi-bot @ hel | pm-bot @ london\n",
                file=sys.stderr,
            )
            return 2

    if MTIME_VERB_RE.search(cmd) and not VERIFIER_VERB_RE.search(cmd) and HIGH_RISK_PATH_RE.search(cmd):
        if not _verifier_recently_used():
            print(
                "\n⛔ evidence-guard BLOCK [mtime-trap]\n"
                "Reading mtime/ls on a high-risk file (state.json, lock, ship slug, watchdog).\n"
                "Mtime can lie. Bundle ONE of: tail -1 <file> | lsof <file> | systemctl is-active <unit> | cat <file> | jq .\n",
                file=sys.stderr,
            )
            return 2

    if COMPLETION_ACTION_RE.search(cmd) and not _verifier_recently_used():
        if not VERIFIER_VERB_RE.search(cmd):
            print(
                "\n⛔ evidence-guard BLOCK [completion-claim]\n"
                "About to run a completion action (commit/install/restart/publish/disable-rename)\n"
                "but no verification command (test/smoke/is-active/build/curl/grep) ran in the last 10 min.\n"
                "Iron Law: NO COMPLETION CLAIMS WITHOUT FRESH VERIFICATION EVIDENCE.\n"
                "Bundle a verifier into this command OR run a smoke test first.\n",
                file=sys.stderr,
            )
            return 2

    return 0


def handle_posttool(payload: dict) -> int:
    tool_input = payload.get("tool_input") or {}
    cmd = tool_input.get("command") or ""
    if not cmd:
        return 0
    if VERIFIER_VERB_RE.search(cmd):
        _mark_verifier()
    bot_re = re.compile(r"systemctl\s+(?:is-active|status|show)\s+(\S+)|journalctl\s+[^|;&]*-u\s+(\S+)")
    for m in bot_re.finditer(cmd):
        unit_raw = m.group(1) or m.group(2) or ""
        unit_base = unit_raw.replace(".service", "")
        if unit_base in BOTS:
            _mark_bot_protocol(unit_base)
    return 0


def handle_userprompt(payload: dict) -> int:
    prompt = payload.get("prompt") or ""
    if not prompt:
        return 0
    if LIVENESS_KEYWORDS.search(prompt) and BOT_NAME_PATTERN.search(prompt):
        print(
            "[evidence-guard reminder] bot liveness — verify with the 3-step protocol "
            "(systemctl is-active + journalctl --since + systemctl show MainPID) "
            "before claiming alive/dead/wedged."
        )
        return 0
    if ACTIVITY_KEYWORDS.search(prompt) and HIGH_RISK_PATH_RE.search(prompt):
        print(
            "[evidence-guard reminder] mtime ≠ activity. Verify with tail -1, lsof, "
            "or systemctl is-active before claiming the writer is fresh/stale."
        )
        return 0
    return 0


def main() -> int:
    try:
        mode = sys.argv[1] if len(sys.argv) > 1 else ""
        payload = _safe_load_input()
        if mode == "pretool":
            return handle_pretool(payload)
        if mode == "posttool":
            return handle_posttool(payload)
        if mode == "userprompt":
            return handle_userprompt(payload)
        return 0
    except Exception as e:
        try:
            print(f"evidence-guard: uncaught error: {e}", file=sys.stderr)
        except Exception:
            pass
        return 0


if __name__ == "__main__":
    sys.exit(main())

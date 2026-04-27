#!/usr/bin/env python3
"""
mtime-trap-guard.py — Block/warn when mtime is treated as evidence of activity.

The "mtime trap" pattern: assistant runs `stat -c %y` / `ls -la` / `find -mmin`
on a state file, daemon-state file, lock, pid, log, or progress sidecar — and
then claims the writer is "stale", "alive", "writing", "frozen", "active",
or "wedged" based on the timestamp. mtime can lie:
  - rotation (file moved + new file created keeps old mtime)
  - atomic write via rename (mtime jumps without writer touching the inode)
  - silent write failure (writer is alive but save_state() raises)
  - mmap without msync (writer is in the process of writing but mtime stale)
  - touch by an unrelated tool (logrotate, backup script)

Modes (sys.argv[1]):
  pretool   — PreToolUse(Bash): block when mtime verb hits a high-risk file
              path AND no companion content/process verifier is in the same
              shell command.
  posttool  — PostToolUse(Bash): mark the verifier-satisfied flag when a
              command queries content (`tail/head/cat/wc -l`), open handles
              (`lsof`), or process state (`systemctl/journalctl/ps`).
  userprompt — UserPromptSubmit: emit reminder when prompt mentions the
              relevant keywords on a high-risk file.

Source / lessons:
  - 2026-04-26 kalshi liveness misdiagnosis (lesson_bot_liveness_misdiagnosis_20260426.md)
  - 2026-04-26 bot-data file-mtime drift
  - 2026-04-27 watchdog state file (.watchdog_pm_bot_state.json mtime
                stale 2 days while watchdog still actively scanning)
Rule: ~/.claude/CLAUDE.md §Mtime-trap (HARD RULE)
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---- High-risk path patterns ---------------------------------------------
# Files whose mtime is most often misread as "is the writer alive?".
HIGH_RISK_PATH_RE = re.compile(
    r'('
    r'\.watchdog[_\-][^/\s]+\.json'           # watchdog_pm_bot_state.json
    r'|'
    r'[^/\s]*[_\-]state\.json'                # *_state.json
    r'|'
    r'\.ship/[^/\s]+/(experiments|state)/'    # ship slug state dirs
    r'|'
    r'scheduled_tasks\.json'                  # claude scheduled tasks
    r'|'
    r'/tmp/[^/\s]+\.(?:lock|pid|state|log)'   # tmp lock/pid/state/log
    r'|'
    r'[^/\s]+\.lock\b'                        # *.lock
    r'|'
    r'[^/\s]+\.pid\b'                         # *.pid
    r'|'
    r'signal[_\-]trace\.jsonl'                # bot data files (also covered by bot-liveness-guard)
    r'|'
    r'trade[_\-]journal\.jsonl'
    r'|'
    r'eval[_\-]history\.jsonl'
    r'|'
    r'portfolio\.json'
    r'|'
    r'[^/\s]*progress\.(?:json|md|txt)'       # progress sidecars
    r'|'
    r'\.cache/[^/\s]+/[^/\s]+\.iso'           # iso-time markers (incl this hook's own)
    r')',
    re.IGNORECASE,
)

# Mtime-inference verbs: things whose output is "this file's mtime is X".
MTIME_VERB_RE = re.compile(
    r'('
    r'\bstat\s+-c\s*[\'"]?%[yY]'              # stat -c '%y' / -c %Y
    r'|'
    r'\bstat\s+-c\s*[\'"]?[^\'"\s]*%[yY]'     # stat -c '%Y %n' etc.
    r'|'
    r'\bstat\s+(?:-[a-zA-Z]*\s+)*[^\s|;]*\b(?!--printf=[^%]*[^yY])'  # plain stat <file>
    r'|'
    r'\bls\s+-[a-zA-Z]*l[a-zA-Z]*t?\b'        # ls -la / ls -lat / ls -lt
    r'|'
    r'\bls\s+-[a-zA-Z]*t\b'                   # ls -t / ls -lat
    r'|'
    r'\bfind\b[^|;&]*-(?:mtime|mmin|cmin|ctime|amin|atime)\b'  # find -mtime/mmin/etc
    r'|'
    r'\bdate\s+-r\s'                          # date -r <file> (mtime as date)
    r')',
)

# Companion verifier verbs: presence of any of these in the SAME command means
# we're not just reading mtime — we're cross-checking the actual data.
VERIFIER_VERB_RE = re.compile(
    r'('
    r'\btail\s+-?\d*'                         # tail -1 / tail -100 etc.
    r'|'
    r'\bhead\s+-?\d*'                         # head
    r'|'
    r'\bcat\s+'                               # cat
    r'|'
    r'\bwc\s+-l\b'                            # wc -l (line count is content-based)
    r'|'
    r'\blsof\b'                               # open handles
    r'|'
    r'\bfuser\b'                              # process holding file open
    r'|'
    r'\bsystemctl\s+(?:is-active|status|show)' # service state
    r'|'
    r'\bjournalctl\s'                         # log query
    r'|'
    r'\bps\s+-'                               # process listing
    r'|'
    r'/proc/\d+/'                             # /proc/<pid>/
    r'|'
    r'\binotifywait\b'                        # filesystem event watcher
    r'|'
    r'\bjq\b'                                 # jq parses content
    r'|'
    r'\bawk\b'                                # awk reads content
    r'|'
    r'\bgrep\b'                               # grep reads content
    r'|'
    r'\bsed\b'                                # sed reads content
    r'|'
    r'\bsha\d+sum\b'                          # checksum reads content
    r'|'
    r'\bmd5sum\b'
    r'|'
    r'\bdiff\b'                               # diff reads content
    r')',
)

# Liveness/activity language used as a verbal trigger from user prompts.
ACTIVITY_KEYWORDS = re.compile(
    r'\b(stale|fresh|active|inactive|alive|dead|writing|frozen|wedged|silent|'
    r'recent|last\s+(?:write|update|run|fired|touched|modified))\b',
    re.IGNORECASE,
)

CACHE_DIR = Path.home() / '.cache' / 'claude-mtime-trap'
VERIFIER_TTL = timedelta(minutes=10)


def _safe_load_input() -> dict:
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return {}
        return json.loads(raw)
    except Exception:
        return {}


def _verifier_recently_used() -> bool:
    """Has a content/process verifier been used in the last 10 min anywhere?"""
    marker = CACHE_DIR / 'verifier-checked-at.iso'
    if not marker.exists():
        return False
    try:
        ts = marker.read_text().strip().replace('Z', '+00:00')
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt) < VERIFIER_TTL
    except Exception:
        return False


def _mark_verifier() -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        marker = CACHE_DIR / 'verifier-checked-at.iso'
        marker.write_text(datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'))
    except Exception:
        pass


def handle_pretool(payload: dict) -> int:
    cmd = (payload.get('tool_input') or {}).get('command') or ''
    if not cmd:
        return 0
    if not MTIME_VERB_RE.search(cmd):
        return 0
    # If the same command also runs a content/process verifier, allow.
    if VERIFIER_VERB_RE.search(cmd):
        return 0
    # If a high-risk path is targeted in this command, hard-warn.
    if not HIGH_RISK_PATH_RE.search(cmd):
        return 0
    # If a verifier was used in the last 10min, allow but soft-remind.
    if _verifier_recently_used():
        # Soft reminder via stdout (becomes context). Don't block.
        print(
            '[mtime-trap-guard] mtime check on a high-risk path. Verifier '
            'was used recently — proceed but do not infer activity from '
            'mtime alone in your next claim.',
        )
        return 0
    msg = (
        '\n'
        '⛔ mtime-trap-guard BLOCK\n'
        'You are about to read mtime/ls-time on a high-risk file (state.json, '
        'lock/pid, ship slug, watchdog state, bot data). Mtime can lie:\n'
        '  - rotation, atomic-rename, silent save_state failure, mmap-without-msync\n'
        '  - touched by logrotate/backup, written by a different writer than expected\n'
        'To prove a writer is alive, do ONE of:\n'
        '  (a) tail -1 <file>   — read the writer\'s own embedded ISO timestamp\n'
        '  (b) lsof <file>      — list current open handles on the file\n'
        '  (c) systemctl is-active <unit> + journalctl -u <unit> --since "5min ago"\n'
        '  (d) cat <file> | jq .  — inspect actual content for liveness markers\n'
        'Bundle one of (a)-(d) into the SAME shell call OR run any verifier first.\n'
        'Lesson: ~/NardoWorld/lessons/lesson_bot_liveness_misdiagnosis_20260426.md\n'
        'Rule: ~/.claude/CLAUDE.md §Mtime-trap (HARD RULE)\n'
    )
    print(msg, file=sys.stderr)
    return 2


def handle_posttool(payload: dict) -> int:
    cmd = (payload.get('tool_input') or {}).get('command') or ''
    if not cmd:
        return 0
    if VERIFIER_VERB_RE.search(cmd):
        _mark_verifier()
    return 0


def handle_userprompt(payload: dict) -> int:
    prompt = payload.get('prompt') or ''
    if not prompt:
        return 0
    if not ACTIVITY_KEYWORDS.search(prompt):
        return 0
    if not HIGH_RISK_PATH_RE.search(prompt):
        return 0
    reminder = (
        '[mtime-trap-guard reminder] You are about to discuss activity '
        '("stale/fresh/alive/...") of a high-risk file. mtime ≠ activity. '
        'Verify with ONE of: tail -1 <file>, lsof <file>, systemctl is-active '
        '<unit> + journalctl --since.\n'
        'Rule: ~/.claude/CLAUDE.md §Mtime-trap (HARD RULE)'
    )
    print(reminder)
    return 0


def main() -> int:
    try:
        mode = sys.argv[1] if len(sys.argv) > 1 else ''
        payload = _safe_load_input()
        if mode == 'pretool':
            return handle_pretool(payload)
        if mode == 'posttool':
            return handle_posttool(payload)
        if mode == 'userprompt':
            return handle_userprompt(payload)
        return 0
    except Exception as e:
        try:
            print(f'mtime-trap-guard: uncaught error: {e}', file=sys.stderr)
        except Exception:
            pass
        return 0


if __name__ == '__main__':
    sys.exit(main())

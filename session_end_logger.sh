#!/bin/bash
# SessionEnd: log + queue deferred save.
# Reads hook input JSON from stdin (session_id, reason, transcript_path).
INPUT=$(cat 2>/dev/null || echo '{}')

python3 - "$INPUT" << 'PYEOF'
import json, sys, os, time
from pathlib import Path

try:
    d = json.loads(sys.argv[1])
except Exception:
    d = {}

sid = d.get("session_id", "unknown")
reason = d.get("reason", "?")
transcript = d.get("transcript_path", "")
cwd = d.get("cwd") or os.getcwd()

# Log every fire for observability
log = Path("/tmp/session_end_fired.log")
with log.open("a") as f:
    f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} SessionEnd reason={reason} session={sid[:8]} cwd={cwd}\n")

# Skip queue if: no transcript, admin_bot/edwin SDK subprocess, already-handled reason
if not transcript or not Path(transcript).exists():
    sys.exit(0)
if "telegram-claude-bot" in cwd or "admin_bot" in cwd:
    sys.exit(0)
if Path(transcript).stat().st_size < 5000:
    sys.exit(0)

# Queue for next SessionStart to pick up
q = Path("/tmp/pending_saves")
q.mkdir(exist_ok=True)
marker = q / f"{sid}.json"
marker.write_text(json.dumps({
    "session_id": sid,
    "reason": reason,
    "transcript_path": transcript,
    "cwd": cwd,
    "ended_at": time.strftime('%Y-%m-%dT%H:%M:%S%z'),
}))
PYEOF

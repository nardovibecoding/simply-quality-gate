#!/usr/bin/env python3
# @bigd-hook-meta
# name: memory_auto_commit
# fires_on: Stop
# always_fire: true
# cost_score: 2
# Copyright (c) 2026 Nardo (nardovibecoding). AGPL-3.0 — see LICENSE
"""Stop hook: git commit + push memory to self-hosted bare repos at session end.

Replaces rsync (migrated 2026-04-23). Pushes to dual remote (Hel + London via SSH).
Fire-and-forget: session stop returns immediately, git push runs in background.
"""
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# Skip during convos (auto-clear flow)
_tty = os.environ.get("CLAUDE_TTY_ID", "").strip()
if Path(f"/tmp/claude_ctx_exit_pending_{_tty}").exists() if _tty else Path("/tmp/claude_ctx_exit_pending").exists():
    print("{}")
    sys.exit(0)

MEMORY_SRC = Path.home() / ".claude" / "projects" / f"-Users-{Path.home().name}" / "memory"


def main():
    try:
        json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        pass

    if not MEMORY_SRC.exists() or not (MEMORY_SRC / ".git").exists():
        print("{}")
        return

    # Skip if no changes
    status = subprocess.run(
        ["git", "-C", str(MEMORY_SRC), "status", "--porcelain"],
        capture_output=True, text=True, timeout=5,
    )
    if status.returncode != 0 or not status.stdout.strip():
        print("{}")
        return

    # Fire-and-forget: pull --rebase first (pick up VPS writes), then commit + push to both remotes.
    # Push goes through gated_push.py so the L3 breaker tracks outcomes and the privacy gate
    # scans github.com pushes (no-op for memory's hel:/london: ssh-bare remotes).
    LOG = Path("/tmp/memory_auto_commit.log")
    ts = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    GATE = Path.home() / ".claude" / "scripts" / "gated_push.py"
    script = (
        f"cd {MEMORY_SRC} && "
        f"git pull --rebase origin main 2>&1 ; "
        f"git add -A && "
        f"git commit -m 'session-end: {ts}' --allow-empty-message 2>&1 ; "
        f"python3 {GATE} {MEMORY_SRC} main 2>&1 || "
        f"( git pull --rebase origin main 2>&1 && python3 {GATE} {MEMORY_SRC} main 2>&1 )"
    )
    with open(LOG, "a") as f:
        f.write(f"\n--- {ts} ---\n")
        subprocess.Popen(
            ["bash", "-c", script],
            stdout=f, stderr=f,
            start_new_session=True,
        )
    print(json.dumps({"systemMessage": "Memory git push started (bg, dual remote)."}))


if __name__ == "__main__":
    import sys as _sys
    from pathlib import Path as _Path
    _sys.path.insert(0, str(_Path(__file__).parent))
    from _safe_hook import safe_run
    safe_run(main, "memory_auto_commit")

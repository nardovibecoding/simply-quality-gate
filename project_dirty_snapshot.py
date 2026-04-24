#!/usr/bin/env python3
# @bigd-hook-meta
# name: project_dirty_snapshot
# fires_on: Stop
# relevant_intents: []
# irrelevant_intents: []
# cost_score: 2
# always_fire: true
"""
Stop hook — snapshot uncommitted work in known project repos at session end.

Prevents the "files drift without commit" pattern where agents edit + deploy
via rsync but never git commit. Creates a WIP commit so state is reversible.

Does NOT push. User controls when to publish.
"""
import subprocess
import sys
from pathlib import Path

REPOS = [
    Path.home() / "prediction-markets",
    Path.home() / "telegram-claude-bot",
]


def run(args, cwd):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=10)


def snapshot(repo: Path) -> str | None:
    if not (repo / ".git").is_dir():
        return None
    # /ship LOCK gate: skip snapshot if a /ship slice is in progress. The slice
    # agent commits its own files explicitly; a WIP snapshot would mix unrelated
    # in-progress edits into the slice commit.
    # Added 2026-04-24 after S0 new files landed in a WIP snapshot instead of
    # the S0 refactor commit.
    ship_dir = repo / ".ship"
    if ship_dir.is_dir():
        for lock in ship_dir.glob("*/LOCK"):
            if lock.is_file():
                return f"{repo.name}: skip (/ship slice active: {lock.parent.name})"
    status = run(["git", "status", "--porcelain"], cwd=repo)
    if status.returncode != 0 or not status.stdout.strip():
        return None
    # Check for unstaged edits to tracked files only; skip untracked and ignored
    tracked_changes = [
        ln for ln in status.stdout.splitlines()
        if ln and not ln.startswith("??")
    ]
    if not tracked_changes:
        return None
    # Stage tracked changes only (avoid accidental new file commits like .env)
    run(["git", "add", "-u"], cwd=repo)
    # Pull remote first (rebase + autostash configured per-repo) to prevent divergence.
    # Stop hook never pushes — just keeps local branch in sync with origin before committing.
    run(["git", "pull", "--rebase", "--autostash", "origin", "main"], cwd=repo)
    # Amend guard: verify something is actually staged
    diff_cached = run(["git", "diff", "--cached", "--name-only"], cwd=repo)
    if not diff_cached.stdout.strip():
        return None
    msg = f"WIP: session snapshot [Stop hook] — {len(tracked_changes)} file(s)"
    commit = run(["git", "commit", "-m", msg], cwd=repo)
    if commit.returncode != 0:
        return f"{repo.name}: commit failed ({commit.stderr.strip()[:80]})"
    head = run(["git", "rev-parse", "--short", "HEAD"], cwd=repo).stdout.strip()
    return f"{repo.name}: {head} ({len(tracked_changes)} file(s))"


def main():
    results = []
    for repo in REPOS:
        try:
            r = snapshot(repo)
            if r:
                results.append(r)
        except Exception as e:
            results.append(f"{repo.name}: error — {str(e)[:80]}")
    if results:
        print("[project-snapshot] " + "; ".join(results), file=sys.stderr)


if __name__ == "__main__":
    import sys as _sys
    from pathlib import Path as _Path
    _sys.path.insert(0, str(_Path(__file__).parent))
    from _safe_hook import safe_run
    safe_run(main, "project_dirty_snapshot")

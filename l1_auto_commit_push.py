#!/usr/bin/env python3
# @bigd-hook-meta
# name: l1_auto_commit_push
# fires_on: PostToolUse
# relevant_intents: [git, sync, code, memory]
# irrelevant_intents: [bigd, telegram, docx, x_tweet, debug]
# cost_score: 1
# always_fire: false
"""L1 sync layer: auto-commit + push the just-edited file to its repo.

Fires after Write / Edit / NotebookEdit. If the changed file lives inside one
of the 5 always-synced repos, runs `git add <file> && git commit && git push`
asynchronously (fire-and-forget). Never blocks the tool.

Scope: 5 repos only (matches L2 sync_mac_vps.sh).
Granularity: just the changed file (narrow blast radius).
Push: yes, immediate (per Bernard 2026-04-27).

L1 owns per-write commits; L2 (sync_mac_vps.sh) still handles batch pushes
for files outside this hook's path (e.g. files written without a tool, or
backlog dirty state). L3 circuit-breaker (TBD) will pause L1 on repeated
push failures.
"""
import json
import subprocess
import sys
import time
from pathlib import Path

HOME = Path.home()

REPOS = {
    "memory": HOME / ".claude" / "projects" / "-Users-bernard" / "memory",
    "nardoworld": HOME / "NardoWorld",
    "telegram-claude-bot": HOME / "telegram-claude-bot",
    "claude-skills": HOME / ".claude" / "skills",
    "claude-quality-gate": HOME / ".claude" / "hooks",
}

LOG = Path("/tmp/l1_auto_commit_push.log")


def log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    try:
        with LOG.open("a") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass


def find_repo(file_path: Path):
    try:
        resolved = file_path.resolve()
    except Exception:
        return None, None
    for label, root in REPOS.items():
        try:
            resolved.relative_to(root.resolve())
            return label, root
        except ValueError:
            continue
    return None, None


def run(cmd, cwd):
    return subprocess.run(
        cmd, cwd=str(cwd), capture_output=True, text=True, timeout=10
    )


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception as e:
        log(f"stdin parse failed: {e}")
        return 0

    tool_input = payload.get("tool_input", {}) or {}
    file_path_str = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
    if not file_path_str:
        return 0

    file_path = Path(file_path_str)
    if not file_path.exists():
        return 0

    label, repo_root = find_repo(file_path)
    if not repo_root:
        return 0

    # Refuse to act on a repo that's mid-rebase / merge / cherry-pick / revert.
    # The L2 sync script (sync_mac_vps.sh) has the same guard; mirroring here so
    # auto-commits don't corrupt an in-progress operation.
    git_dir = repo_root / ".git"
    if (
        (git_dir / "rebase-merge").is_dir()
        or (git_dir / "rebase-apply").is_dir()
        or (git_dir / "MERGE_HEAD").is_file()
        or (git_dir / "CHERRY_PICK_HEAD").is_file()
        or (git_dir / "REVERT_HEAD").is_file()
    ):
        log(f"[{label}] SKIP rebase/merge/cherry-pick/revert in progress")
        return 0

    try:
        rel = file_path.resolve().relative_to(repo_root.resolve())
    except Exception:
        return 0

    # Skip git-internal + ignored files
    if str(rel).startswith(".git/"):
        return 0
    check = run(["git", "check-ignore", "-q", str(rel)], repo_root)
    if check.returncode == 0:
        return 0

    # Stage just this file. If nothing changed, exit clean.
    add = run(["git", "add", "--", str(rel)], repo_root)
    if add.returncode != 0:
        log(f"[{label}] git add failed: {add.stderr.strip()}")
        return 0

    diff = run(["git", "diff", "--cached", "--quiet"], repo_root)
    if diff.returncode == 0:
        return 0

    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    msg = f"l1-auto: {rel} @ {ts}"
    commit = run(["git", "commit", "-m", msg, "--", str(rel)], repo_root)
    if commit.returncode != 0:
        log(f"[{label}] commit failed: {commit.stderr.strip() or commit.stdout.strip()}")
        return 0

    log(f"[{label}] committed {rel}")

    # Async push: fire-and-forget; don't block tool.
    push_log = f"/tmp/l1_push_{label}.log"
    try:
        subprocess.Popen(
            f"git push origin main >> {push_log} 2>&1",
            shell=True,
            cwd=str(repo_root),
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        log(f"[{label}] push spawn failed: {e}")

    return 0


if __name__ == "__main__":
    sys.exit(main())

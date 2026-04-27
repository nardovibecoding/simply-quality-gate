#!/bin/bash
# Mac→VPS sync for repos without GitHub remotes + memory + wiki
# Called from: cron (every 5 min) or post-commit hook
# Only syncs if there are actual changes to push.
#
# What syncs:
#   1. prediction-markets (git bundle — no GitHub remote)
#   2. memory (~/.claude/projects/-Users-bernard/memory/)
#   3. NardoWorld wiki

set -euo pipefail

VPS="vps"
LOG="/tmp/vps_sync.log"
LOCK="/tmp/vps_sync.lock"
BUNDLE="/tmp/pm-sync-bundle.git"

log() { echo "[$(date '+%H:%M:%S')] $*" >> "$LOG"; }

# Prevent concurrent runs
if [ -f "$LOCK" ]; then
  pid=$(cat "$LOCK" 2>/dev/null)
  if kill -0 "$pid" 2>/dev/null; then
    exit 0
  fi
fi
echo $$ > "$LOCK"
trap 'rm -f "$LOCK"' EXIT

# Quick connectivity check
if ! ssh -o ConnectTimeout=5 "$VPS" true 2>/dev/null; then
  log "VPS unreachable, skipping"
  exit 0
fi

# prediction-markets sync moved to deploy-pm-hel.sh + deploy-pm-lon.sh (2026-04-24).

# Zombie-rebase guard: bail out if a rebase is mid-flight from a prior run.
# Past bug: pull --rebase silently paused on conflicts (stderr to /dev/null), then
# subsequent commits stacked on top of paused HEAD. Detect + alert + abort.
sync_git_repo() {
  local repo_dir="$1" label="$2"
  cd "$repo_dir" || return 1
  if [ -d .git/rebase-merge ] || [ -d .git/rebase-apply ]; then
    log "$label: ZOMBIE REBASE detected (stopped at $(cat .git/rebase-merge/stopped-sha 2>/dev/null || echo unknown)). Auto-aborting and skipping this cycle."
    git rebase --abort 2>/dev/null
    return 1
  fi
  # Stash any unstaged changes so pull --rebase doesn't fail on dirty tree
  local stashed=0
  if ! git diff --quiet || ! git diff --cached --quiet; then
    git stash push -u -m "vps_sync auto-stash $(date +%FT%T)" >/dev/null 2>&1 && stashed=1
  fi
  # Pull only when origin is configured. Local-only repos skip the pull-rebase
  # entirely (no upstream to reconcile with).
  local has_origin=0
  git remote get-url origin >/dev/null 2>&1 && has_origin=1
  if [ "$has_origin" = "1" ]; then
    local pull_err
    pull_err=$(git pull --rebase origin main 2>&1)
    if [ $? -ne 0 ] || echo "$pull_err" | grep -qi 'conflict\|could not apply'; then
      log "$label: pull --rebase failed/conflicted, aborting + skipping. Error: $(echo "$pull_err" | tail -1)"
      git rebase --abort 2>/dev/null
      [ "$stashed" = "1" ] && git stash pop >/dev/null 2>&1
      return 1
    fi
  fi
  [ "$stashed" = "1" ] && git stash pop >/dev/null 2>&1
  git add -A
  # On unborn-branch repos (zero commits), git commit refuses with "no changes
  # added to commit" only when index is also empty. With files staged, it works.
  git commit -m "mac-periodic: $(date +%FT%T)" --allow-empty-message 2>/dev/null
  # Only push when origin is configured. Local-only repos (e.g. ~/vibe-island
  # 2.0, ~/.claude) must still get periodic commits but have no upstream.
  if git remote get-url origin >/dev/null 2>&1; then
    git push origin main 2>/dev/null
  fi
}

# ─── 2. Memory (git push/pull to self-hosted bare repo, migrated 2026-04-23) ───
MEMORY_DIR="$HOME/.claude/projects/-Users-bernard/memory"
if [ -d "$MEMORY_DIR/.git" ]; then
  sync_git_repo "$MEMORY_DIR" "Memory" && log "Memory: git synced" || log "Memory: git sync failed"
fi

# ─── 3. NardoWorld wiki (git push/pull to self-hosted bare repo, migrated 2026-04-23) ───
WIKI_DIR="$HOME/NardoWorld"
if [ -d "$WIKI_DIR/.git" ]; then
  sync_git_repo "$WIKI_DIR" "Wiki" && log "Wiki: git synced" || log "Wiki: git sync failed"
fi

# ─── 3.5 Local-only Mac repos ───────────────────────────────────────
# Each leaf-level local repo gets its own gitwatch launchd daemon (real-time,
# fswatch-driven, leaf-only by design). Add a daemon per repo at:
#   ~/Library/LaunchAgents/com.bernard.gitwatch-<reponame>.plist
# This script does not touch them — gitwatch handles its own lifecycle.
# Currently watching: vibe-island (com.bernard.gitwatch-vibe-island)

# ─── 4. Claude scripts (rsync) ───────────────────────────────────────
SCRIPTS_DIR="$HOME/.claude/scripts/"
if [ -d "$SCRIPTS_DIR" ]; then
  rsync -az --exclude='__pycache__' \
    "$SCRIPTS_DIR" \
    "$VPS:~/.claude/scripts/" \
    2>/dev/null && log "Scripts: synced" || log "Scripts: rsync failed"
fi

# ─── 5. Skills git pull with rebase fallback (fixes ff-only silent abort) ───
SKILLS_DIR="$HOME/.claude/skills"
if [ -d "$SKILLS_DIR/.git" ]; then
  git -C "$SKILLS_DIR" pull --rebase origin main 2>/dev/null \
    || git -C "$SKILLS_DIR" pull --no-rebase origin main 2>/dev/null \
    && log "Skills: pulled" \
    || log "Skills: pull failed — manual resolve needed"
fi

log "Sync complete"

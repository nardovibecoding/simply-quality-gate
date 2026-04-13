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

# ─── 1. prediction-markets (git bundle) ──────────────────────────────
PM_DIR="$HOME/prediction-markets"
if [ -d "$PM_DIR/.git" ]; then
  LOCAL_HEAD=$(cd "$PM_DIR" && git rev-parse HEAD)
  REMOTE_HEAD=$(ssh "$VPS" 'cd ~/prediction-markets && git rev-parse HEAD 2>/dev/null' || echo "none")

  if [ "$LOCAL_HEAD" != "$REMOTE_HEAD" ]; then
    log "PM: local=$LOCAL_HEAD VPS=$REMOTE_HEAD — syncing"

    if [ "$REMOTE_HEAD" = "none" ]; then
      # Full bundle
      (cd "$PM_DIR" && git bundle create "$BUNDLE" HEAD --all 2>/dev/null)
    else
      # Incremental bundle
      (cd "$PM_DIR" && git bundle create "$BUNDLE" "$REMOTE_HEAD..HEAD" 2>/dev/null)
    fi

    scp -q "$BUNDLE" "$VPS:/tmp/pm-sync-bundle.git"
    ssh "$VPS" 'cd ~/prediction-markets && \
      git fetch /tmp/pm-sync-bundle.git HEAD:incoming 2>/dev/null && \
      git stash -q 2>/dev/null; \
      git merge incoming --ff-only 2>/dev/null && \
      git branch -d incoming 2>/dev/null && \
      git stash pop -q 2>/dev/null; \
      rm -f /tmp/pm-sync-bundle.git' 2>/dev/null

    # Rebuild if TS files changed
    CHANGED=$(cd "$PM_DIR" && git diff --name-only "$REMOTE_HEAD..HEAD" -- '*.ts' 2>/dev/null | head -1)
    if [ -n "$CHANGED" ]; then
      log "PM: TS files changed, rebuilding on VPS"
      ssh "$VPS" 'cd ~/prediction-markets && npm run build 2>/dev/null' &
    fi

    log "PM: synced to $LOCAL_HEAD"
    rm -f "$BUNDLE"
  fi
fi

# ─── 2. Memory (rsync) ───────────────────────────────────────────────
MEMORY_DIR="$HOME/.claude/projects/-Users-bernard/memory/"
if [ -d "$MEMORY_DIR" ]; then
  rsync -az --delete --exclude='.git' \
    "$MEMORY_DIR" \
    "$VPS:~/.claude/projects/-Users-bernard/memory/" \
    2>/dev/null && log "Memory: synced" || log "Memory: rsync failed"
fi

# ─── 3. NardoWorld wiki (rsync) ──────────────────────────────────────
WIKI_DIR="$HOME/NardoWorld/"
if [ -d "$WIKI_DIR" ]; then
  rsync -az --delete --exclude='.git' --exclude='node_modules' \
    "$WIKI_DIR" \
    "$VPS:~/NardoWorld/" \
    2>/dev/null && log "Wiki: synced" || log "Wiki: rsync failed"
fi

# ─── 4. Claude scripts (rsync) ───────────────────────────────────────
SCRIPTS_DIR="$HOME/.claude/scripts/"
if [ -d "$SCRIPTS_DIR" ]; then
  rsync -az --exclude='__pycache__' \
    "$SCRIPTS_DIR" \
    "$VPS:~/.claude/scripts/" \
    2>/dev/null && log "Scripts: synced" || log "Scripts: rsync failed"
fi

log "Sync complete"

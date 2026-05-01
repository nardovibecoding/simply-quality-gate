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

# Sync helper. Three perm-fix layers added 2026-05-01 (post-detector-loss incident):
#
# P1 — Use merge (--no-rebase), not rebase, for any repo with >50 local commits
#       ahead of origin. Replaying 5000+ commits onto origin's tip is the actual
#       breakage shape; merge is one commit and never has the "replay-conflict
#       on commit #N out of 5361" pause-state.
# P2 — Divergence guardrail: if >DIVERGENCE_ALERT commits ahead of origin AND
#       no successful push in last hour, log loud alert and bail out. This means
#       the breaker / push gate is jammed; manual intervention needed.
# P3 — Stuck-rebase auto-recovery: hard-abort + verify clean working tree
#       before continuing. Existing zombie guard only logged + skipped, leaving
#       the working tree in mid-rebase state across cycles.
DIVERGENCE_PREFER_MERGE=50    # P1 threshold: >this commits ahead → merge mode
DIVERGENCE_ALERT=200          # P2 threshold: >this + push stuck → alert + bail
PUSH_STUCK_SECS=3600          # P2: a push older than this counts as "stuck"
LAST_PUSH_FILE="/tmp/vps_sync.last_push"

sync_git_repo() {
  local repo_dir="$1" label="$2"
  cd "$repo_dir" || return 1

  # P3: hard-recover from any prior stuck rebase / merge / cherry-pick.
  if [ -d .git/rebase-merge ] || [ -d .git/rebase-apply ]; then
    log "$label: P3 — stuck rebase detected, hard-aborting"
    git rebase --abort 2>/dev/null || true
    # Reset any leftover MERGE_HEAD / CHERRY_PICK_HEAD too
    [ -f .git/MERGE_HEAD ]        && git merge --abort 2>/dev/null
    [ -f .git/CHERRY_PICK_HEAD ]  && git cherry-pick --abort 2>/dev/null
    # Verify clean working tree state (per HEAD); if dirty, bail
    if [ -d .git/rebase-merge ] || [ -d .git/rebase-apply ]; then
      log "$label: P3 — abort failed, working tree still mid-rebase, MANUAL FIX NEEDED"
      return 1
    fi
  fi

  # Stash any unstaged changes so pull doesn't fail on dirty tree
  local stashed=0
  if ! git diff --quiet || ! git diff --cached --quiet; then
    git stash push -u -m "vps_sync auto-stash $(date +%FT%T)" >/dev/null 2>&1 && stashed=1
  fi

  local has_origin=0
  git remote get-url origin >/dev/null 2>&1 && has_origin=1
  if [ "$has_origin" = "1" ]; then
    # Fetch first so divergence numbers are accurate
    git fetch origin main 2>/dev/null

    # P2: divergence guardrail
    local ahead behind
    ahead=$(git rev-list --count origin/main..HEAD 2>/dev/null || echo 0)
    behind=$(git rev-list --count HEAD..origin/main 2>/dev/null || echo 0)

    if [ "$ahead" -gt "$DIVERGENCE_ALERT" ]; then
      # P2: only bail when push is ALSO stuck (no successful push in last hour).
      # Comment block (lines 44-46) said AND; prior code only checked ahead.
      local last_push_age=999999
      if [ -f "$LAST_PUSH_FILE" ]; then
        local now=$(date +%s) lp=$(stat -f %m "$LAST_PUSH_FILE" 2>/dev/null || stat -c %Y "$LAST_PUSH_FILE" 2>/dev/null || echo 0)
        last_push_age=$((now - lp))
      fi
      if [ "$last_push_age" -gt "$PUSH_STUCK_SECS" ]; then
        log "$label: P2 ALERT — ahead=$ahead (>$DIVERGENCE_ALERT) AND last push ${last_push_age}s ago (>$PUSH_STUCK_SECS). Push gate jammed. Bailing. Manual: check ~/.claude/scripts/sync_breaker.py status."
        [ "$stashed" = "1" ] && git stash pop >/dev/null 2>&1
        return 1
      else
        log "$label: P2 — ahead=$ahead (>$DIVERGENCE_ALERT) but last push ${last_push_age}s ago (≤$PUSH_STUCK_SECS), proceeding"
      fi
    fi

    # P1: pick pull strategy by divergence
    local pull_strategy="--rebase"
    if [ "$ahead" -gt "$DIVERGENCE_PREFER_MERGE" ]; then
      pull_strategy="--no-rebase"
      log "$label: P1 — local ahead by $ahead commits (>$DIVERGENCE_PREFER_MERGE), using merge instead of rebase"
    fi

    if [ "$behind" -gt 0 ]; then
      local pull_err
      pull_err=$(git pull $pull_strategy origin main 2>&1)
      if [ $? -ne 0 ] || echo "$pull_err" | grep -qi 'conflict\|could not apply'; then
        log "$label: pull failed/conflicted (strategy=$pull_strategy), hard-recovering. Error: $(echo "$pull_err" | tail -1)"
        # Try every recovery path (only one will apply per state)
        git rebase --abort 2>/dev/null || true
        git merge --abort 2>/dev/null || true
        [ "$stashed" = "1" ] && git stash pop >/dev/null 2>&1
        return 1
      fi
    fi
  fi

  [ "$stashed" = "1" ] && git stash pop >/dev/null 2>&1
  git add -A
  git commit -m "mac-periodic: $(date +%FT%T)" --allow-empty-message 2>/dev/null

  if git remote get-url origin >/dev/null 2>&1; then
    python3 "$HOME/.claude/scripts/gated_push.py" "$repo_dir" main 2>/dev/null
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

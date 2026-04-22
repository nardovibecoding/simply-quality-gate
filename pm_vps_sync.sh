#!/bin/bash
# PM VPS Sync Hook — debounced git push + Hel pull/build/restart
# Triggered on PostToolUse Edit for files in ~/prediction-markets
# Flow: local WIP commit → push to Hel bare → Hel pulls → rebuild → restart bot

TOUCHFILE="/tmp/pm_git_sync_pending"
LOCKFILE="/tmp/pm_git_sync.lock"
LOGFILE="/tmp/pm_vps_sync.log"
WATCHER_PID_FILE="/tmp/pm_git_sync_watcher.pid"
DEBOUNCE=60
PM_LOCAL="$HOME/prediction-markets"
PM_REMOTE="/home/bernard/prediction-markets"
BARE_URL="ssh://vps/home/bernard/prediction-markets.git"
SSH="ssh -o ControlMaster=no -o ControlPath=none -o ConnectTimeout=5 vps"

INPUT=$(cat)
FILE=$(echo "$INPUT" | jq -r '.tool_input.file_path // .tool_input.command // ""' 2>/dev/null)
if [[ "$FILE" != *"prediction-markets"* ]]; then
  exit 0
fi

touch "$TOUCHFILE"

if [[ -f "$WATCHER_PID_FILE" ]] && kill -0 "$(cat "$WATCHER_PID_FILE")" 2>/dev/null; then
  exit 0
fi

(
  echo $$ > "$WATCHER_PID_FILE"

  while true; do
    sleep "$DEBOUNCE"

    if [[ ! -f "$TOUCHFILE" ]]; then
      rm -f "$WATCHER_PID_FILE"
      exit 0
    fi

    AGE=$(( $(date +%s) - $(stat -f %m "$TOUCHFILE" 2>/dev/null || echo 0) ))
    if [[ $AGE -lt $DEBOUNCE ]]; then
      continue
    fi

    rm -f "$TOUCHFILE"

    if [[ -f "$LOCKFILE" ]]; then
      AGE_LOCK=$(( $(date +%s) - $(stat -f %m "$LOCKFILE" 2>/dev/null || echo 0) ))
      if [[ $AGE_LOCK -lt 180 ]]; then
        rm -f "$WATCHER_PID_FILE"
        exit 0
      fi
    fi

    touch "$LOCKFILE"

    if ! $SSH 'echo ok' &>/dev/null; then
      echo "$(date): VPS unreachable" >> "$LOGFILE"
      rm -f "$LOCKFILE" "$WATCHER_PID_FILE"
      exit 0
    fi

    # Local: WIP commit + push to Hel bare
    cd "$PM_LOCAL" || { rm -f "$LOCKFILE" "$WATCHER_PID_FILE"; exit 0; }
    if [[ -n "$(git status --porcelain)" ]]; then
      git add -A 2>/dev/null
      git commit -m "WIP: auto-sync [pm_vps_sync]" --quiet 2>/dev/null
    fi
    PUSH_OUT=$(git push "$BARE_URL" main 2>&1)
    PUSH_EXIT=$?
    echo "$(date): push $PUSH_EXIT — $(echo "$PUSH_OUT" | tail -1)" >> "$LOGFILE"

    # A: auto-rebase on non-fast-forward reject
    if [ $PUSH_EXIT -ne 0 ] && echo "$PUSH_OUT" | grep -q "non-fast-forward\|rejected"; then
      echo "$(date): REBASE attempt — fetching remote" >> "$LOGFILE"
      git fetch "$BARE_URL" main 2>> "$LOGFILE"
      if git rebase FETCH_HEAD >> "$LOGFILE" 2>&1; then
        RETRY_OUT=$(git push "$BARE_URL" main 2>&1)
        RETRY_EXIT=$?
        echo "$(date): REBASE+retry push $RETRY_EXIT — $(echo "$RETRY_OUT" | tail -1)" >> "$LOGFILE"
        [ $RETRY_EXIT -ne 0 ] && echo "$(date): SYNC_ERROR rebase-retry-failed" >> "$LOGFILE"
      else
        git rebase --abort 2>> "$LOGFILE"
        echo "$(date): SYNC_ERROR rebase-conflict manual-merge-needed" >> "$LOGFILE"
      fi
    fi

    # Remote build+restart steps (shared template):
    # - Clean tsbuildinfo + dist before build (prevents stale incremental cache bugs)
    # - Parse-gate critical data files before restart
    # - Kill orphan node processes before restart (prevents double-instance OOM)
    BUILD_RESTART='
      cd %REMOTE% && git fetch origin main --quiet && git reset --hard origin/main --quiet
      COUNT=$(git rev-list --count HEAD)
      HASH=$(git rev-parse --short HEAD)
      DEPLOY_TS=$(date +%s)
      mkdir -p data packages/bot/data
      echo "{\"count\":${COUNT},\"hash\":\"${HASH}\",\"deployedAt\":${DEPLOY_TS}}" > data/version.json
      cp data/version.json packages/bot/data/version.json 2>/dev/null; true
      find packages -name "*.tsbuildinfo" -delete 2>/dev/null
      rm -rf packages/sdk/dist packages/bot/dist 2>/dev/null
      cd packages/sdk && npm run build 2>&1 | tee /tmp/pm_sdk_build.log | tail -3
      SDK_EXIT=${PIPESTATUS[0]}
      [ $SDK_EXIT -ne 0 ] && { echo "[SYNC_ERROR] SDK build failed (exit $SDK_EXIT) — aborting restart"; cat /tmp/pm_sdk_build.log | tail -20; exit 1; }
      cd ../bot && npm run build 2>&1 | tee /tmp/pm_bot_build.log | tail -3
      BOT_EXIT=${PIPESTATUS[0]}
      [ $BOT_EXIT -ne 0 ] && { echo "[SYNC_ERROR] bot build failed (exit $BOT_EXIT) — aborting restart"; cat /tmp/pm_bot_build.log | tail -20; exit 1; }
      [ ! -f dist/main.js ] && { echo "[SYNC_ERROR] dist/main.js missing after build — aborting restart"; exit 1; }
      cd %REMOTE%
      for f in data/whale_wallets.json data/whale_enriched_db.json; do
        [ -f "$f" ] && ! jq -e . "$f" >/dev/null 2>&1 && { echo "[parse-gate] $f corrupt — aborting restart"; exit 1; }
      done
      pkill -f "node.*prediction-markets.*main.js" 2>/dev/null; sleep 1
      %RESTART_CMD%
    '
    HEL_CMD="${BUILD_RESTART//%REMOTE%/$PM_REMOTE}"
    HEL_CMD="${HEL_CMD//%RESTART_CMD%/sudo systemctl restart pm-bot}"
    $SSH "$HEL_CMD" >> "$LOGFILE" 2>&1

    VULTR_SSH="ssh -o ConnectTimeout=5 root@78.141.205.30"
    VULTR_CMD="${BUILD_RESTART//%REMOTE%//root/prediction-markets}"
    VULTR_CMD="${VULTR_CMD//%RESTART_CMD%/systemctl restart pm-bot}"
    $VULTR_SSH "$VULTR_CMD" >> "$LOGFILE" 2>&1

    echo "$(date): debounced git-sync + rebuilt + restarted (waited ${DEBOUNCE}s)" >> "$LOGFILE"
    rm -f "$LOCKFILE" "$WATCHER_PID_FILE"
    exit 0
  done
) &>/dev/null &

exit 0

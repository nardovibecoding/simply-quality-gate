#!/bin/bash
# pm_sync_status — surface recent pm_vps_sync errors to Claude.
# Used by both PostToolUse (after Edit/Bash) and UserPromptSubmit.
# Outputs additionalContext JSON if recent SYNC_ERROR found in log; else silent.

LOGFILE="/tmp/pm_vps_sync.log"
WINDOW_SEC=300  # look back 5 minutes

[ ! -f "$LOGFILE" ] && exit 0

# Grab last 50 log lines, filter for SYNC_ERROR entries with timestamp in window
NOW=$(date +%s)
CUTOFF=$((NOW - WINDOW_SEC))

RECENT_ERRORS=$(tail -50 "$LOGFILE" 2>/dev/null | awk -v cutoff="$CUTOFF" '
  /SYNC_ERROR/ {
    # Parse BSD date header: "Sat Apr 18 02:30:05 +0800 2026"
    cmd = "date -j -f \"%a %b %e %T %z %Y\" \"" substr($0, 1, index($0, ":") + 20) "\" +%s 2>/dev/null"
    cmd | getline ts
    close(cmd)
    if (ts + 0 >= cutoff + 0) print $0
  }
' 2>/dev/null)

if [ -z "$RECENT_ERRORS" ]; then
  exit 0
fi

# Output as additionalContext (works for both PostToolUse and UserPromptSubmit)
MSG="⚠ PM sync error(s) in last ${WINDOW_SEC}s:\n$(echo "$RECENT_ERRORS" | tail -3)\nCheck /tmp/pm_vps_sync.log — may need manual git rebase or merge."
# Emit JSON with additionalContext field; keys shared by both hook types.
printf '{"hookSpecificOutput":{"hookEventName":"UserPromptSubmit","additionalContext":%s}}\n' "$(printf '%s' "$MSG" | jq -Rs .)"
exit 0

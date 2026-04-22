#!/bin/bash
# Post-rsync hook: build + restart bot on VPS after code sync
# Triggered by PostToolUse on Bash matching rsync*prediction-markets

CMD=$(jq -r '.tool_input.command // ""' 2>/dev/null)

# Only run for rsync to PM VPS
echo "$CMD" | grep -q 'rsync' || exit 0
echo "$CMD" | grep -q 'prediction-markets' || exit 0
echo "$CMD" | grep -q '157.180' || exit 0

# Build on VPS
ssh bernard@157.180.28.14 "cd ~/prediction-markets && npm run build" >/dev/null 2>&1

# Restart bot
ssh bernard@157.180.28.14 "pkill -f 'node.*main.js'" 2>/dev/null
sleep 2
ssh bernard@157.180.28.14 "cd ~/prediction-markets/packages/bot && nohup /usr/bin/node --max-old-space-size=1536 --expose-gc dist/main.js >> /tmp/pm-bot.log 2>&1 &" 2>/dev/null

echo '{"hookSpecificOutput":{"hookEventName":"PostToolUse","additionalContext":"[hook] VPS build + bot restart complete"}}'

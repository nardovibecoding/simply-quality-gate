#!/bin/bash
# Pre-rsync hook: generate version.json for prediction-markets VPS deploy
# Triggered by PreToolUse on Bash matching rsync*prediction-markets

CMD=$(jq -r '.tool_input.command // ""' 2>/dev/null)

# Only run for rsync to PM VPS
echo "$CMD" | grep -q 'rsync' || exit 0
echo "$CMD" | grep -q 'prediction-markets' || exit 0
echo "$CMD" | grep -q '157.180' || exit 0

cd /Users/bernard/prediction-markets || exit 0

# Generate version.json locally
HASH=$(git rev-parse --short HEAD 2>/dev/null || echo "unknown")
COUNT=$(git rev-list --count HEAD 2>/dev/null || echo "0")
echo "{\"count\":$COUNT,\"hash\":\"$HASH\"}" > data/version.json

# Push version to VPS + build + restart (runs AFTER rsync completes via PostToolUse)
# Note: this is PreToolUse so rsync hasn't run yet. SCP version now, build+restart happens in post hook.
scp -q data/version.json bernard@157.180.28.14:~/prediction-markets/data/version.json 2>/dev/null

echo '{"hookSpecificOutput":{"hookEventName":"PreToolUse","additionalContext":"[hook] version.json auto-generated for PM deploy"}}'

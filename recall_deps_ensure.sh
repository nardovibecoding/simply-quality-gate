#!/usr/bin/env bash
# @bigd-hook-meta
# name: recall_deps_ensure
# fires_on: SessionStart
# always_fire: true
# cost_score: 0
#
# Idempotent guard: ensure ~/.claude/skills/recall/ has its node_modules
# populated before search.mjs is invoked at any later prompt. Without this,
# a fresh clone, /lint sweep, or `rm -rf node_modules` quietly breaks the
# vector layer (transformers import) and recall silently falls back to BM25
# only — which loses the per-cube weight effect we just shipped.
#
# Cost: stat one directory; npm install only when missing.

set -uo pipefail
RECALL_DIR="${HOME}/.claude/skills/recall"
LOG="/tmp/claude_recall_deps_ensure.log"
SENTINEL="${RECALL_DIR}/node_modules/@huggingface/transformers"

# Fast path: nothing to do
if [ -d "${SENTINEL}" ]; then
  exit 0
fi

# Slow path: install (only fires when missing)
{
  echo "[$(date -u +%FT%TZ)] recall_deps_ensure: installing missing deps"
  cd "${RECALL_DIR}" || { echo "  ERR: recall dir missing"; exit 0; }
  npm install --silent --no-audit --no-fund 2>&1 | tail -10
  echo "[$(date -u +%FT%TZ)] recall_deps_ensure: done"
} >> "${LOG}" 2>&1
exit 0

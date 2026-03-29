# quality-gate

43 production Claude Code hooks for automated code quality, safety enforcement, and workflow automation — built and battle-tested while vibe-coding daily.

## What this is

Claude Code hooks are shell commands that fire at specific events in your coding session:
- **PostToolUse** — after every file edit
- **PreToolUse** — before running commands
- **Stop** — when Claude finishes a response
- **UserPromptSubmit** — when you send a message

These hooks add a persistent enforcement layer that runs silently in the background and only speaks up when something needs attention.

## Hooks

### Code Quality (PostToolUse: Edit/Write)

| Hook | What it does |
|------|-------------|
| `auto_test_after_edit.py` | Syntax + lint + mypy + tests after every edit. Silent on pass, loud on failure. Also checks: debug code left in, hardcoded secrets, untested functions, new TODOs, large edits (>150 lines) |
| `auto_review_before_done.py` | Stop hook: reads test results from edit log, checks caller impact, schema migrations, config/docs drift. Blocks if tests fail, informs otherwise |
| `hardcoded_model_guard.py` | Prevents model names being hardcoded outside the single config file |
| `async_safety_guard.py` | Catches common async pitfalls (missing await, sync calls in async context) |
| `resource_leak_guard.py` | Detects unclosed file handles, DB connections, HTTP sessions |
| `temp_file_guard.py` | Warns when temp files are created but not cleaned up |
| `unicode_grep_warn.py` | Catches grep calls that will silently fail on Unicode content |

### Security Guards (PostToolUse: Edit/Write)

| Hook | What it does |
|------|-------------|
| `tg_security_guard.py` | Telegram-specific: prevents leaking chat IDs, tokens, user data |
| `tg_api_guard.py` | Blocks direct Telegram API calls that bypass the bot abstraction layer |
| `admin_only_guard.py` | Enforces admin-only access patterns on sensitive operations |
| `guard_safety.py` | PreToolUse: validates Bash commands before execution |
| `reasoning_leak_canary.py` | Detects when Claude's internal reasoning leaks into output |

### Automation (PostToolUse: Edit/Write)

| Hook | What it does |
|------|-------------|
| `auto_dependency_grep.py` | After editing source-of-truth files, greps for downstream references that may need updating |
| `auto_pip_install.py` | Detects new imports and auto-installs missing packages |
| `auto_bot_restart.py` | Restarts the relevant bot process after code changes |
| `auto_skill_sync.py` | Syncs skills directory after skill file edits |
| `auto_restart_process.py` | Generic process restart via VPS SSH after target file changes |
| `auto_license.py` | Ensures license headers on new files |
| `auto_copyright_header.py` | Adds copyright headers to new source files |

### Automation (PostToolUse: Bash)

| Hook | What it does |
|------|-------------|
| `auto_vps_sync.py` | Triggers VPS git pull after pushing |
| `auto_repo_check.py` | Validates repo state after git operations |

### Context & Memory (Stop / UserPromptSubmit)

| Hook | What it does |
|------|-------------|
| `auto_context_checkpoint.py` | UserPromptSubmit: injects checkpoint prompts at 20/40/60/80% context usage |
| `auto_memory_index.py` | Warns when a new memory file is written but not added to MEMORY.md index |
| `memory_auto_commit.py` | Auto-commits changed memory files at session end |
| `auto_content_remind.py` | Stop: reminds about content posting schedules |

### Publishing & Deployment

| Hook | What it does |
|------|-------------|
| `auto_pre_publish.py` | Pre-publish checklist: README has WHAT+WHO, platform badges, license |
| `auto_hook_deploy.py` | Validates hooks before deploying to production |
| `pre_commit_validate.py` | Pre-commit checks: no secrets, no debug code, tests pass |
| `verify_infra.py` | Verifies infrastructure is healthy before deployments |

### Utilities

| Hook | What it does |
|------|-------------|
| `hook_base.py` | Base class / shared runner for all hooks |
| `test_helpers.py` | AST-based test coverage detection used by quality hooks |
| `vps_config.py` | Shared VPS config loaded from `.env` |
| `file_lock.py` / `file_unlock.py` | File locking to prevent concurrent edits |
| `api_key_lookup.py` | Resolves API key locations from memory |
| `cookie_health.py` | Checks browser cookie freshness |
| `cron_log_monitor.py` | Monitors cron job logs for failures |
| `mcp_server_restart.py` | Restarts MCP servers after config changes |
| `gmail_humanizer.py` | Applies content humanizer to Gmail drafts |
| `reddit_api_block.py` | Blocks direct Reddit API calls (use MCP instead) |
| `revert_memory_chain.py` | Safely reverts a chain of memory file changes |
| `skill_disable_not_delete.py` | Prevents skills from being deleted instead of disabled |
| `tg_qr_document.py` | Formats Telegram QR code responses correctly |

## Installation

1. Clone into `~/.claude/hooks/`:
```bash
git clone https://github.com/nardovibecoding/quality-gate ~/.claude/hooks
```

2. Configure your `~/.claude/settings.json` — see [`settings.example.json`](settings.example.json)

3. Set environment variables in your project `.env`:
```bash
VPS_HOST=your-vps-ip
VPS_USER=your-vps-username
VPS_CLIPBOARD_PORT=8888   # optional
```

4. Hooks load automatically at session start. After any hook edit, run `/clear` or start a new session to pick up changes.

## How the quality pipeline works

```
Edit file
   │
   ▼
PostToolUse: auto_test_after_edit.py
   ├── syntax check (py_compile)
   ├── lint (ruff) — errors only
   ├── type check (mypy) — errors only
   ├── debug code? (print/pdb/breakpoint)
   ├── secrets? (API keys, tokens)
   ├── untested functions?
   ├── new TODO/FIXME?
   ├── large edit? (>150 lines)
   └── run tests → log pass/fail to /tmp/claude_edits_{session_id}.json
          │
          ▼ (only on failure)
       Print error to Claude

Claude finishes response
   │
   ▼
Stop: auto_review_before_done.py
   ├── read edit log (latest per file, no cross-session bleed)
   ├── any test failures? → block (exit 2)
   ├── missing test files? → block (exit 2)
   ├── caller impact? → inform (exit 0)
   ├── schema migration needed? → inform (exit 0)
   └── config/docs drift? → inform (exit 0)
```

## Writing your own hook

Every hook follows the same pattern via `hook_base.py`:

```python
from hook_base import run_hook

def check(tool_name, tool_input, input_data) -> bool:
    """Return True if this hook should fire for this tool call."""
    return tool_name in ("Edit", "Write")

def action(tool_name, tool_input, input_data) -> str | None:
    """Return a warning message, or None to stay silent."""
    file_path = tool_input.get("file_path", "")
    # ... your logic ...
    return "⚠️  Something looks wrong" if problem else None

if __name__ == "__main__":
    run_hook(check, action, "my_hook_name")
```

Then add it to `~/.claude/settings.json`:
```json
{
  "hooks": {
    "PostToolUse": [{
      "matcher": "Edit|Write",
      "hooks": [{"type": "command", "command": "python3 ~/.claude/hooks/my_hook.py", "timeout": 5000}]
    }]
  }
}
```

## Key design principles

- **Silent on pass** — hooks only output when there's something actionable. Zero noise = zero ignored warnings.
- **Per-session edit logs** — edit state is scoped to `session_id` so multiple concurrent sessions never interfere.
- **Last-write-wins deduplication** — if a file is edited 3 times in a session, only the final test result counts.
- **Informational vs blocking** — test failures block; style warnings inform. Claude is never stopped by noise.
- **Exit codes matter** — `exit 2` blocks Claude from finishing the turn; `exit 0` allows it even with output.

## Requirements

- Python 3.11+
- Claude Code CLI
- Optional: `ruff` (lint), `mypy` (types), `pytest` (tests — falls back to `unittest`)

## License

AGPL-3.0 — see [LICENSE](LICENSE)

# simply-quality-gate

```bash
claude plugins install nardovibecoding/simply-quality-gate
```

---

<div align="center">

**10 hooks that enforce code quality automatically — tests, patterns, resource leaks, license headers.**

[![hooks](https://img.shields.io/badge/hooks-10-orange?style=for-the-badge)](.)
[![license](https://img.shields.io/badge/license-AGPL--3.0-red?style=for-the-badge)](LICENSE)
[![platform](https://img.shields.io/badge/platform-macOS%20%2B%20Linux-lightgrey?style=for-the-badge)](#)

</div>

Code quality degrades gradually — skipped tests, leaked resources, inconsistent patterns. These hooks enforce your standards automatically on every Claude Code operation, silently in the background.

No VPS required. No MCP server. Just hooks.

---

## Hooks

| Hook | Event | What it does |
|------|-------|-------------|
| `auto_test_after_edit.py` | PostToolUse: Edit/Write | Syntax + lint + mypy + tests after every edit. Silent on pass, loud on failure. Flags debug code, hardcoded secrets, untested functions, large edits |
| `auto_review_before_done.py` | Stop | Reads test results, checks caller impact, schema migrations, config drift. Blocks if tests fail |
| `hardcoded_model_guard.py` | PostToolUse: Edit/Write | Prevents model names being hardcoded outside the single config file |
| `async_safety_guard.py` | PostToolUse: Edit/Write | Catches async pitfalls — missing await, sync calls in async context |
| `resource_leak_guard.py` | PostToolUse: Edit/Write | Detects unclosed file handles, DB connections, HTTP sessions |
| `temp_file_guard.py` | PostToolUse: Edit/Write | Warns when /tmp files are created but not cleaned up |
| `unicode_grep_warn.py` | PostToolUse: Edit/Write | Catches grep calls that silently fail on Unicode/CJK content |
| `pre_commit_validate.py` | PostToolUse: Bash | Validates Python syntax after git commit |
| `auto_copyright_header.py` | PreToolUse: Edit/Write | Ensures copyright header on new source files |
| `auto_license.py` | PostToolUse: Edit/Write | Auto-setup license on new repos after `gh repo create` |

---

## Install

```bash
claude plugins install nardovibecoding/simply-quality-gate
```

Or manually — clone and add to `~/.claude/settings.json`:

```json
{
  "plugins": ["~/simply-quality-gate"]
}
```

---

## Hook Dispatcher System

For high-frequency sessions, the dispatcher system reduces latency by ~75% and spawns ~79% fewer processes per tool call.

**Architecture:**

| File | Role |
|------|------|
| `hook_daemon.py` | Background process — pre-loads all hook modules into memory, listens on a Unix socket (`/tmp/claude_hook_daemon.sock`) |
| `hook_client.sh` | Lightweight caller — sends events to the daemon via `nc`, falls back to direct dispatcher if daemon is not running |
| `dispatcher_pre.py` | Routes PreToolUse events to the correct hooks based on `tool_name` |
| `dispatcher_post.py` | Routes PostToolUse events to the correct hooks based on `tool_name` |

**How it works:**

Without the dispatcher, Claude Code spawns a new `python3` process for every hook on every tool call. With the dispatcher, one Python process handles all routing in-process, and the daemon eliminates startup cost entirely by keeping modules loaded.

**Usage:**

```bash
# Start the daemon (once per session)
python3 hook_daemon.py &

# Use hook_client.sh as your hook command in settings.json
# PreToolUse:  echo $event | hook_client.sh pre
# PostToolUse: echo $event | hook_client.sh post
```

The daemon auto-detects pre vs post based on the `_event` field. If the daemon is down, `hook_client.sh` falls back to the Python dispatchers transparently.

**Customizing routing:**

Edit the `ROUTING` dict in `dispatcher_pre.py` / `dispatcher_post.py` (or `PRE_ROUTING` / `POST_ROUTING` in `hook_daemon.py`) to map tool names to your hook scripts.

---

## Related

- [claude-sec-ops-guard](https://github.com/nardovibecoding/claude-sec-ops-guard) — 27 hooks + 28 MCP tools for security enforcement and ops automation

---

## License

AGPL-3.0 — Copyright (c) 2026 Nardo (nardovibecoding)

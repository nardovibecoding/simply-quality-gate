#!/usr/bin/env python3
"""London config guard — PreToolUse hook (Edit/Write).

Blocks edits to config.london.json that would ENABLE any streaming/discovery
flag. Hard rule: London = consumer. Hel = producer.

Also blocks edits that set consumerMode=false on london config.
Also blocks edits to London systemd service file that add streaming envs.

Prints a block reason + exits with code 2 to veto the tool call.
"""
import json
import os
import re
import sys

# 2026-04-21: Rule rescinded. Original premise (London bandwidth cap) was based
# on misreading total RX+TX as billable. Vultr bills outbound only; London uses
# ~4.5 GB/day outbound (13.5% of 1 TB cap). Inbound is free, so streaming on
# London has no cost impact. London now runs full Poly stack locally.
FORBIDDEN_ENABLE_PATTERNS = []

TARGET_PATHS = [
    "config/config.london.json",
    "config.london.json",
    "pm-bot-london.service",
    "etc/systemd/system/pm-bot.service",
]


def is_london_target(path: str) -> bool:
    if not path:
        return False
    pl = path.lower()
    return any(t in pl for t in TARGET_PATHS) or "london" in pl


def extract_content(tool_name: str, tool_input: dict) -> str:
    """Extract the new content that would be written/edited."""
    if tool_name == "Write":
        return tool_input.get("content", "") or ""
    if tool_name == "Edit":
        return tool_input.get("new_string", "") or ""
    if tool_name == "NotebookEdit":
        return tool_input.get("new_source", "") or ""
    return ""


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        print("{}")
        return

    tool_name = data.get("tool_name", "")
    tool_input = data.get("tool_input", {}) or {}

    if tool_name not in ("Write", "Edit", "NotebookEdit"):
        print("{}")
        return

    path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
    if not is_london_target(path):
        print("{}")
        return

    content = extract_content(tool_name, tool_input)
    if not content:
        print("{}")
        return

    for pat in FORBIDDEN_ENABLE_PATTERNS:
        m = re.search(pat, content, re.IGNORECASE | re.DOTALL)
        if m:
            reason = (
                f"BLOCKED: edit to {os.path.basename(path)} would enable streaming "
                f"on London (matched pattern: {pat!r}, near: {m.group(0)[:80]!r}).\n"
                f"Hard rule: London = consumer. Move this work to Hel "
                f"(config.default.json or config.hel.json). London config must stay "
                f"consumerMode=true with walletHarvester/clobStream/adversarial disabled."
            )
            # Exit 2 = block with reason to stderr per Claude Code hook spec
            sys.stderr.write(reason + "\n")
            sys.exit(2)

    print("{}")


if __name__ == "__main__":
    main()

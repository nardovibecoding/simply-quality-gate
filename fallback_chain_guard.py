#!/usr/bin/env python3
# Copyright (c) 2026 Nardo. AGPL-3.0 — see LICENSE
"""PreToolUse hook: warn on `elif [ -d PATH ]` fallback chains in deploy/build/infra scripts.

Catches the pattern that caused the 2026-04-23 pm-london 41-commit drift bomb:
  if [ -d /home/bernard/... ]; then REPO=...
  elif [ -d /root/... ]; then REPO=...   # ← silently picks abandoned twin

See: memory/lesson_systemd_truth_drift_20260423.md
Rule:   memory/feedback_no_fallback_chains.md

Writes a warning (systemMessage); does NOT block. Human decides.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from hook_base import run_hook

# Deploy/build/infra script paths — where fallback chains are dangerous
_TARGET_DIR_SUBSTR = (
    "/prediction-markets/",
    "/infra/",
    "/scripts/",
    "/etc/systemd/",
    "deploy",
)

_TARGET_EXTS = (".sh", ".bash")

# The dangerous pattern: `elif [ -d ... ]` or `elif test -d ...`
_FALLBACK_PATTERNS = [
    re.compile(r"elif\s+\[\s+-d\s+"),
    re.compile(r"elif\s+test\s+-d\s+"),
]


def check(tool_name, tool_input, _input_data):
    if tool_name not in ("Edit", "Write"):
        return False
    fp = tool_input.get("file_path", "")
    if not fp:
        return False
    # Only deploy/build/infra scripts
    if not any(sub in fp for sub in _TARGET_DIR_SUBSTR):
        return False
    # Only shell scripts
    fname = Path(fp).name
    if not fname.endswith(_TARGET_EXTS) and "build" not in fname and "deploy" not in fname:
        return False
    return True


def action(tool_name, tool_input, _input_data):
    if tool_name == "Write":
        content = tool_input.get("content", "")
    else:
        content = tool_input.get("new_string", "")

    if not content:
        return None

    hits = []
    for i, line in enumerate(content.splitlines(), 1):
        for pattern in _FALLBACK_PATTERNS:
            if pattern.search(line):
                hits.append(f"  line ~{i}: `{line.strip()[:100]}`")
                break

    if not hits:
        return None

    fp = tool_input.get("file_path", "")
    return (
        f"FALLBACK CHAIN GUARD: `elif [ -d ... ]` detected in `{Path(fp).name}`.\n"
        "This pattern caused the 2026-04-23 pm-london 41-commit drift (see lesson_systemd_truth_drift_20260423.md).\n"
        "Use deterministic hostname case-switch instead:\n"
        "  case \"$(hostname)\" in\n"
        "    host1) REPO=/path/one ;;\n"
        "    host2) REPO=/path/two ;;\n"
        "    *) echo \"unknown host: $(hostname)\"; exit 1 ;;\n"
        "  esac\n"
        "Failing loud on unknown hosts > silently picking abandoned twins.\n"
        + "\n".join(hits[:5])
    )


if __name__ == "__main__":
    run_hook(check, action, "fallback_chain_guard")

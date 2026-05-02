#!/usr/bin/env python3
"""Auto-stamp canonical frontmatter on memory + NardoWorld writes (PostToolUse).

Fires on Write/Edit AFTER tool succeeds. If file is in watched dirs and missing any
of the 6 required fields, computes defaults via stamp_frontmatter.py logic and
edits the file in place with atomic write.

Pairs with PreToolUse `frontmatter_stamp_audit.py` (warn-only tripwire — tells you
which writer paths still emit non-canonical FM). Together: tripwire surfaces drift,
auto-stamp fixes it.

Idempotent — files already canonical pass silently.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import re
import sys
import tempfile
import time

LOG = pathlib.Path.home() / ".claude" / "scripts" / "state" / "frontmatter-autostamp.jsonl"
HOME = pathlib.Path.home()
WATCH_DIRS = [
    HOME / ".claude" / "projects" / "-Users-bernard" / "memory",
    HOME / "NardoWorld" / "atoms",
    HOME / "NardoWorld" / "lessons",
]
REQUIRED_FIELDS = ["date", "writer", "runtime", "source_kind",
                   "session_id", "originSessionId", "status"]
FM_BLOCK = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
CANONICAL_RUNTIME = "claude-opus-4-7"
STATUSLINE = pathlib.Path("/tmp/claude_statusline.json")


def is_watched(path: str) -> bool:
    if not path:
        return False
    p = pathlib.Path(path).resolve()
    return any(str(p).startswith(str(d.resolve()) + os.sep) for d in WATCH_DIRS) and p.suffix == ".md"


def parse_fm(content: str) -> tuple[dict[str, str], str, str]:
    m = FM_BLOCK.match(content)
    if not m:
        return {}, "", content
    fields: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" in line and not line.lstrip().startswith("#"):
            k, v = line.split(":", 1)
            fields[k.strip()] = v.strip()
    return fields, content[: m.end()], content[m.end():]


def detect_session_id() -> str:
    codex_thread_id = os.environ.get("CODEX_THREAD_ID", "")
    if codex_thread_id:
        return codex_thread_id
    try:
        return json.loads(STATUSLINE.read_text()).get("session_id", "") or ""
    except Exception:
        return ""


def detect_writer_runtime() -> tuple[str, str]:
    if (os.environ.get("CODEX_SESSION") or os.environ.get("CODEX_CLI_VERSION")
            or os.environ.get("CODEX_THREAD_ID") or os.environ.get("CODEX_CI")):
        v = os.environ.get("CODEX_CLI_VERSION", "unknown")
        return "codex", f"codex-cli-{v}"
    return "claude-code", os.environ.get("CLAUDE_RUNTIME_OVERRIDE") or CANONICAL_RUNTIME


def derive_defaults(path: pathlib.Path, fields: dict[str, str]) -> dict[str, str]:
    """Compute defaults for missing required fields. Never overwrites present ones."""
    today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    sid = detect_session_id() or "unknown"
    writer, runtime = detect_writer_runtime()

    # If file is in atoms/lessons, default writer to librarian (paths derive scope)
    p_str = str(path.resolve())
    if "/NardoWorld/atoms/" in p_str or "/NardoWorld/lessons/" in p_str:
        if not fields.get("writer"):
            writer = "librarian"

    # Date from filename if convo_YYYY-MM-DD pattern
    fn_date = re.match(r"^(?:convo|conv)_(\d{4}-\d{2}-\d{2})", path.name)
    derived_date = fn_date.group(1) if fn_date else today

    return {
        "date": derived_date,
        "writer": writer,
        "runtime": runtime,
        "source_kind": "visible-context",
        "session_id": sid,
        "originSessionId": sid,
        "status": "active",
    }


def render_with_added(fm_text: str, body: str, additions: dict[str, str]) -> str:
    """Insert additions into existing FM block (or build one if missing)."""
    if not fm_text:
        lines = ["---"]
        for k in REQUIRED_FIELDS:
            if k in additions:
                lines.append(f"{k}: {additions[k]}")
        lines.append("---")
        lines.append("")
        return "\n".join(lines) + body
    closing_idx = fm_text.rfind("---\n")
    head = fm_text[:closing_idx]
    extra = "".join(f"{k}: {v}\n" for k, v in additions.items())
    return head + extra + "---\n" + body


def atomic_write(path: pathlib.Path, content: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".fm-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(content)
        os.replace(tmp, path)
    except Exception:
        try: os.unlink(tmp)
        except Exception: pass
        raise


def log_stamp(path: str, additions: dict[str, str]) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "path": path,
        "added": list(additions.keys()),
        "values": additions,
    }
    with open(LOG, "a") as fh:
        fh.write(json.dumps(rec) + "\n")


def main() -> None:
    try:
        data = json.loads(sys.stdin.read())
    except Exception:
        sys.exit(0)
    if data.get("tool_name") not in ("Write", "Edit", "MultiEdit"):
        sys.exit(0)
    tool_input = data.get("tool_input") or {}
    path_str = tool_input.get("file_path", "") or ""
    if not is_watched(path_str):
        sys.exit(0)
    path = pathlib.Path(path_str)
    if not path.exists():
        sys.exit(0)

    try:
        content = path.read_text()
    except Exception:
        sys.exit(0)

    fields, fm_text, body = parse_fm(content)
    defaults = derive_defaults(path, fields)
    additions = {k: v for k, v in defaults.items()
                 if k in REQUIRED_FIELDS and (k not in fields or not fields[k])}
    if not additions:
        sys.exit(0)

    new_content = render_with_added(fm_text, body, additions)
    if new_content == content:
        sys.exit(0)
    try:
        atomic_write(path, new_content)
        log_stamp(path_str, additions)
    except Exception as e:
        # Fail open: log to stderr but don't fail the original write
        print(f"frontmatter_auto_stamp: {e}", file=sys.stderr)
    sys.exit(0)


if __name__ == "__main__":
    main()

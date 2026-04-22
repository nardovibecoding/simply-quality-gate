#!/usr/bin/env python3
"""UserPromptSubmit hook: surface pending tweet ideas in structured groups.

Reads ~/.tweet_ideas_pending.jsonl + legacy ~/.tweet_ideas_pending.json.
Groups: HOT (<2h), FRESH (<24h). STALE items (>24h) dropped silently.
"""
import os
import json
import subprocess
import sys
from datetime import datetime, timezone

PENDING_JSONL = os.path.expanduser("~/.tweet_ideas_pending.jsonl")
PENDING_JSON = os.path.expanduser("~/.tweet_ideas_pending.json")

DUSTY_PINK = "\033[38;2;200;140;160m"
BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"

HOT_HOURS = 2
FRESH_HOURS = 24
MAX_SHOWN = 7


def _parse_ts(item):
    for k in ("ts", "timestamp", "created_at", "fetched_at"):
        v = item.get(k)
        if not v:
            continue
        try:
            if isinstance(v, (int, float)):
                return datetime.fromtimestamp(v, tz=timezone.utc)
            return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        except Exception:
            continue
    return None


def _age_hours(item):
    ts = _parse_ts(item)
    if not ts:
        return None
    return (datetime.now(timezone.utc) - ts).total_seconds() / 3600


def _fmt_item(idx, item):
    title = (item.get("title") or "").strip()
    if len(title) > 110:
        title = title[:107] + "..."
    source = (item.get("source") or "").strip()
    url = (item.get("url") or "").strip()
    src_tag = f"[{source}] " if source else ""
    lines = [f" {idx}. {src_tag}{title}"]
    if url:
        lines.append(f"    {DIM}→ {url}{RESET}")
    return "\n".join(lines)


def main():
    user_prompt = ""
    try:
        hook_input = json.load(sys.stdin)
        user_prompt = hook_input.get("prompt", "")
    except (json.JSONDecodeError, EOFError):
        pass

    all_items = []
    if os.path.exists(PENDING_JSONL):
        try:
            with open(PENDING_JSONL) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    data = json.loads(line)
                    all_items.extend(data.get("items", []))
            os.remove(PENDING_JSONL)
        except Exception:
            pass

    if os.path.exists(PENDING_JSON):
        try:
            with open(PENDING_JSON) as f:
                data = json.load(f)
            all_items.extend(data.get("items", []))
            os.remove(PENDING_JSON)
        except Exception:
            pass

    if not all_items:
        print("{}")
        return

    hot, fresh = [], []
    for it in all_items:
        age = _age_hours(it)
        if age is None or age < HOT_HOURS:
            hot.append(it)
        elif age < FRESH_HOURS:
            fresh.append(it)

    hot = hot[:MAX_SHOWN]
    fresh = fresh[: max(0, MAX_SHOWN - len(hot))]

    if not hot and not fresh:
        print("{}")
        return

    total = len(hot) + len(fresh)
    header = f"{BOLD}📰 {total} idea(s){RESET}"
    if len(hot):
        header += f" · {len(hot)} hot"
    lines = [header]

    idx = 1
    if hot:
        lines.append(f"\n{BOLD}🔥 HOT{RESET}")
        for it in hot:
            lines.append(_fmt_item(idx, it))
            idx += 1
    if fresh:
        lines.append(f"\n{BOLD}📌 FRESH{RESET}")
        for it in fresh:
            lines.append(_fmt_item(idx, it))
            idx += 1

    if user_prompt:
        try:
            subprocess.run(["pbcopy"], input=user_prompt.encode(), check=True)
        except Exception:
            pass

    msg = "\n".join(lines)
    print(f"{DUSTY_PINK}{msg}{RESET}", file=sys.stderr)
    sys.exit(2)


if __name__ == "__main__":
    main()

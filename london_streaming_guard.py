#!/usr/bin/env python3
# @bigd-hook-meta
# name: london_streaming_guard
# fires_on: UserPromptSubmit
# relevant_intents: [pm, vps, sync, code]
# irrelevant_intents: [docx, x_tweet, telegram, git, bigd, memory]
# cost_score: 1
# always_fire: false
"""London streaming guard — UserPromptSubmit hook.

Hard rule: London (vultr, 78.141.205.30) is a CONSUMER box only.
All streaming/polling/discovery/RPC-heavy work runs on Hel (157.180.28.14).
Hel rsyncs data files to London. London reads + trades.

Injects a reminder into additionalContext when the prompt mentions
London+streaming keywords, so Claude doesn't propose adding streams
to London.

Also fires on prompts that look like they could ADD streaming to London.
"""
import io
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(__file__))
from _semantic_router import should_fire

LONDON_TOKENS = re.compile(
    r"\b(london|vultr|78\.141\.205\.30|pm-london)\b",
    re.IGNORECASE,
)

STREAMING_TOKENS = re.compile(
    r"\b(stream|streaming|websocket|ws\b|wss|polling|poll\b|"
    r"subscribe|discover|harvester|goldsky|subgraph|rpc|"
    r"scrape|scraper|scanner|crawl|ingest|fetch\b|feed\b|"
    r"leaderboard|activity.?scan|balance.?refresh|"
    r"wallet.?harvester|wallet.?discover|refresh_balances|"
    r"build_unified_profile|discover_whales|spoofer_discover)\b",
    re.IGNORECASE,
)

WARNING = (
    "⚠️ LONDON = CONSUMER ONLY. Hard rule: never add streaming, polling, "
    "discovery, wallet-harvester, Goldsky calls, subgraph queries, or "
    "scrapers to London (vultr, 78.141.205.30, pm-london). All such work "
    "belongs on Hel (vps, 157.180.28.14). Hel rsyncs data files to London. "
    "London only reads local files + trades. Current London RX: ~48GB/day, "
    "hitting Vultr bandwidth cap. If asked to add any stream/poll/discovery "
    "to London, refuse and redirect to Hel."
)


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        print("{}")
        return

    prompt = data.get("prompt", "") or ""

    if not prompt:
        print("{}")
        return

    london_hit = LONDON_TOKENS.search(prompt)
    streaming_hit = STREAMING_TOKENS.search(prompt)

    if london_hit and streaming_hit:
        print(json.dumps({"additionalContext": WARNING}))
        return

    print("{}")


if __name__ == "__main__":
    _raw_stdin = sys.stdin.read()
    try:
        _prompt = json.loads(_raw_stdin).get("prompt", "")
    except Exception:
        _prompt = ""
    sys.stdin = io.StringIO(_raw_stdin)
    if not should_fire(__file__, _prompt):
        print("{}")
        sys.exit(0)
    main()

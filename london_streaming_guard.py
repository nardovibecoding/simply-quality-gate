#!/usr/bin/env python3
"""London streaming guard — UserPromptSubmit hook.

Hard rule: London (vultr, 78.141.205.30) is a CONSUMER box only.
All streaming/polling/discovery/RPC-heavy work runs on Hel (157.180.28.14).
Hel rsyncs data files to London. London reads + trades.

Injects a reminder into additionalContext when the prompt mentions
London+streaming keywords, so Claude doesn't propose adding streams
to London.

Also fires on prompts that look like they could ADD streaming to London.
"""
import json
import re
import sys

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
    main()

#!/usr/bin/env python3
"""
sy_classifier.py — deterministic hard-rule classifier for [SY] suggestions.

Three-tier output:
  AUTO_GO       — empirically safe to act without asking (data-validated)
  ALWAYS_ASK    — empirically clarify-prone OR safety-flagged
  ASK_NORMALLY  — default

Rules derived from ~/NardoWorld/meta/sy_pairs.jsonl (471 decided pairs).
Promotion criteria:
  AUTO_GO_RULES:    n>=3, accept_rate==100%, no rejects
  ALWAYS_ASK_RULES: n>=5, clarify+reject rate >= 70% (baseline 53%)

NEVER probabilistic — if a rule doesn't have empirical backing, it doesn't fire.

Usage:
  python3 sy_classifier.py "O1 — orthogonal, 5 min"
  python3 sy_classifier.py --eval
  python3 sy_classifier.py --json '{"sy_text": "..."}'
"""

import json
import re
import sys
from pathlib import Path

PAIRS_FILE = Path.home() / "NardoWorld" / "meta" / "sy_pairs.jsonl"

# Safety hard-blocks — never auto-go, override everything else.
HARD_BLOCK_RULES = [
    ("wallet/secrets",   re.compile(r'\b(wallet|private[\s_]?key|\.env|credential|secret|api[\s_]?key)\b', re.I)),
    ("force/destructive", re.compile(r'\b(force|--force|reset.*hard|hard reset|rm\s+-rf|drop\s+table|truncate|wipe)\b', re.I)),
    ("prod/deploy",      re.compile(r'\b(deploy.*prod|prod.*deploy)\b', re.I)),
    ("claude.md",        re.compile(r'\bCLAUDE\.md\b', re.I)),
    ("systemctl_stop",   re.compile(r'\bsystemctl\s+(stop|restart)\b', re.I)),
]

# Empirically validated AUTO_GO rules.
# Format: (label, regex, source_n, source_accept_rate)
AUTO_GO_RULES = [
    ("orthogonal_marker", re.compile(r'\borthogonal\b', re.I), 4, 1.00),
]

# Empirically validated ALWAYS_ASK rules — Bernard reliably wants to discuss.
# Format: (label, regex, source_n, source_clarify_reject_rate)
ALWAYS_ASK_RULES = [
    ("restart_op",   re.compile(r'\brestart\b', re.I),  10, 0.90),
    ("design_topic", re.compile(r'\bdesign\b', re.I),   10, 0.80),
    ("daemon_topic", re.compile(r'\bdaemon\b', re.I),    7, 0.71),
]


def classify(sy_text):
    """
    Return dict:
      action: 'auto_go' | 'always_ask' | 'ask_normally'
      rule:   label of the firing rule (or None)
      reason: human-readable reason
      hard_blocked: bool
    """
    text = sy_text or ""

    # 1. Safety hard-blocks override everything
    for label, pat in HARD_BLOCK_RULES:
        if pat.search(text):
            return {
                "action": "always_ask",
                "rule": f"hard_block:{label}",
                "reason": f"Safety hard-block: {label}",
                "hard_blocked": True,
            }

    # 2. ALWAYS_ASK rules check before AUTO_GO so a SY mentioning both wins toward asking
    for label, pat, n, rate in ALWAYS_ASK_RULES:
        if pat.search(text):
            return {
                "action": "always_ask",
                "rule": label,
                "reason": f"{label}: {rate*100:.0f}% clarify/reject in n={n}",
                "hard_blocked": False,
            }

    # 3. AUTO_GO rules
    for label, pat, n, rate in AUTO_GO_RULES:
        if pat.search(text):
            return {
                "action": "auto_go",
                "rule": label,
                "reason": f"{label}: {rate*100:.0f}% accept in n={n}",
                "hard_blocked": False,
            }

    # 4. Default
    return {
        "action": "ask_normally",
        "rule": None,
        "reason": "No empirical rule matches",
        "hard_blocked": False,
    }


def evaluate():
    """Backtest hard rules against labeled pairs."""
    if not PAIRS_FILE.exists():
        print(f"sy_pairs.jsonl not found at {PAIRS_FILE}")
        sys.exit(1)
    pairs = [json.loads(l) for l in open(PAIRS_FILE) if l.strip()]
    decided = [p for p in pairs if p.get("signal") in ("accept", "clarify", "reject")]

    by_action = {"auto_go": [0,0,0], "always_ask": [0,0,0], "ask_normally": [0,0,0]}
    by_rule = {}
    for p in decided:
        result = classify(p.get("sy_text", ""))
        idx = {"accept": 0, "clarify": 1, "reject": 2}[p["signal"]]
        by_action[result["action"]][idx] += 1
        rule = result["rule"] or "default"
        by_rule.setdefault(rule, [0,0,0])[idx] += 1

    print(f"Backtest: {len(decided)} decided pairs\n")
    print(f"{'action':<16} {'accept':>7} {'clarify':>8} {'reject':>7} {'total':>6} {'A%':>5}")
    print("-" * 56)
    for act in ["auto_go", "always_ask", "ask_normally"]:
        a, c, r = by_action[act]
        n = a+c+r
        if n == 0:
            print(f"{act:<16} {0:>7} {0:>8} {0:>7} {0:>6}   --")
            continue
        print(f"{act:<16} {a:>7} {c:>8} {r:>7} {n:>6} {a/n*100:>4.0f}%")

    print(f"\n{'rule':<25} {'A':>4} {'C':>4} {'R':>4} {'n':>4}")
    print("-" * 47)
    for rule, (a,c,r) in sorted(by_rule.items(), key=lambda x: -sum(x[1])):
        print(f"{rule:<25} {a:>4} {c:>4} {r:>4} {a+c+r:>4}")

    a_auto, c_auto, r_auto = by_action["auto_go"]
    if a_auto + c_auto + r_auto:
        prec = a_auto / (a_auto + c_auto + r_auto) * 100
        print(f"\nauto_go precision: {a_auto}/{a_auto+c_auto+r_auto} = {prec:.0f}%")
    a_ask, c_ask, r_ask = by_action["always_ask"]
    if a_ask + c_ask + r_ask:
        ask_recall = (c_ask + r_ask) / (a_ask + c_ask + r_ask) * 100
        print(f"always_ask hit rate (% that were clarify/reject): {c_ask+r_ask}/{a_ask+c_ask+r_ask} = {ask_recall:.0f}%")


def main():
    args = sys.argv[1:]
    if not args:
        print("Usage: sy_classifier.py <sy_text> | --eval | --json <obj>")
        sys.exit(1)
    if args[0] == "--eval":
        evaluate()
        return
    if args[0] == "--json":
        obj = json.loads(args[1])
        print(json.dumps(classify(obj.get("sy_text", "")), indent=2))
        return
    text = " ".join(args)
    print(json.dumps(classify(text), indent=2))


if __name__ == "__main__":
    main()

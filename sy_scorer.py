#!/usr/bin/env python3
"""
sy_scorer.py — Slice 2 of passive-lesson pipeline (Job A, advisory).

Reads ~/NardoWorld/meta/sy_pairs.jsonl, classifies each sy_text into a bucket,
updates ~/.claude/hooks/sy_scorer_db.json with per-bucket accept/total counts,
reports per-bucket auto-eligibility (P >= P_THRESHOLD and n >= MIN_SAMPLE).

Advisory mode only. No hook registration (Slice 3). No live intercept.
Hard-gates + forbidden_conjunctions enforced in classify() now so Slice 3
can flip on without spec change.

Usage:
  python3 sy_scorer.py --rebuild   # recount db from sy_pairs.jsonl (idempotent)
  python3 sy_scorer.py --stats     # print per-bucket table
  python3 sy_scorer.py             # default: --rebuild + --stats
"""

import json
import re
import sys
import time
from pathlib import Path

NARDO_META = Path.home() / "NardoWorld" / "meta"
SY_PAIRS_FILE = NARDO_META / "sy_pairs.jsonl"
SCORER_DIR = Path.home() / ".claude" / "sy_scorer"
HOOKS_DIR = Path.home() / ".claude" / "hooks"
DB_FILE = HOOKS_DIR / "sy_scorer_db.json"
USER_RULES_FILE = SCORER_DIR / "user_rules.yaml"

P_THRESHOLD = 0.85
MIN_SAMPLE = 10
SCORER_VERSION = "1.0"

# Hard-gates — ALWAYS return None from classify regardless of bucket match.
HARD_GATES = [re.compile(p, re.IGNORECASE) for p in [
    r"wallet|private[\s_-]?key|funds|transfer|send\s+(usdc|eth|sol)",
    r"force[\s-]?push|push.*--force|--force.*push",
    r"rm\s+-rf|drop\s+table|truncate|wipe",
    r"reset.*hard|hard.*reset|git\s+reset",
    r"amend.*publish|published.*amend",
    r"deploy.*vps|vps.*deploy|ssh\s+prod|prod\s+ssh",
    r"push.*origin|origin.*push|git\s+push",
    r"new project|new repo|init.*project",
    r"CLAUDE\.md.*rule|change.*permission|security.*policy",
    r"tone|voice|naming|emoji|ui\s*copy|copy\s*ui",
]]


def load_db():
    if not DB_FILE.exists():
        sys.exit(f"sy_scorer_db.json missing at {DB_FILE}")
    return json.loads(DB_FILE.read_text())


def save_db(db):
    db["last_rebuilt_ts"] = int(time.time())
    DB_FILE.write_text(json.dumps(db, indent=2) + "\n")


def p_smoothed(accept, total):
    """Laplace smoothing. Zero data → 0.5 prior."""
    return (accept + 1) / (total + 2)


def is_hard_gated(text):
    return any(g.search(text) for g in HARD_GATES)


def classify(text, db):
    """Return bucket name or None. Enforces hard-gates + forbidden_conjunctions."""
    if is_hard_gated(text):
        return None
    tl = text.lower()
    for name, bucket in db["buckets"].items():
        if any(kw in tl for kw in bucket["keywords"]):
            forbidden = bucket.get("forbidden_conjunctions", [])
            if any(fc in tl for fc in forbidden):
                return None
            return name
    return None


def rebuild(db):
    """Recount accept/total for each bucket from sy_pairs.jsonl."""
    for b in db["buckets"].values():
        b["accept_count"] = 0
        b["total_count"] = 0

    if not SY_PAIRS_FILE.exists():
        return 0, 0

    pairs = 0
    classified = 0
    with open(SY_PAIRS_FILE) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                p = json.loads(line)
            except json.JSONDecodeError:
                continue
            pairs += 1
            sy_text = p.get("sy_text", "")
            signal = p.get("signal", "unknown")
            if signal not in ("accept", "reject"):
                continue
            bucket = classify(sy_text, db)
            if bucket is None:
                continue
            classified += 1
            db["buckets"][bucket]["total_count"] += 1
            if signal == "accept":
                db["buckets"][bucket]["accept_count"] += 1
    return pairs, classified


def is_auto_eligible(bucket):
    p = p_smoothed(bucket["accept_count"], bucket["total_count"])
    return bucket["total_count"] >= MIN_SAMPLE and p >= P_THRESHOLD


def print_stats(db):
    pairs_file_size = SY_PAIRS_FILE.stat().st_size if SY_PAIRS_FILE.exists() else 0
    last = db.get("last_rebuilt_ts", 0)
    last_str = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(last)) if last else "never"
    print(f"sy_scorer v{SCORER_VERSION}  rebuilt: {last_str}  source: sy_pairs.jsonl ({pairs_file_size} B)")
    print(f"Thresholds: P>={P_THRESHOLD}  n>={MIN_SAMPLE}")
    print()
    print(f"{'BUCKET':24s} {'n':>5s} {'accept':>7s} {'P':>6s} {'eligible':>9s}")
    print("-" * 60)
    rows = []
    for name, b in db["buckets"].items():
        p = p_smoothed(b["accept_count"], b["total_count"])
        elig = "YES" if is_auto_eligible(b) else ""
        rows.append((b["total_count"], name, b["accept_count"], p, elig))
    rows.sort(reverse=True)
    for n, name, acc, p, elig in rows:
        print(f"{name:24s} {n:5d} {acc:7d} {p:6.3f} {elig:>9s}")
    print()
    eligible = [name for name, b in db["buckets"].items() if is_auto_eligible(b)]
    print(f"Auto-eligible buckets: {len(eligible)} -> {eligible if eligible else '(none yet)'}")


def main():
    args = sys.argv[1:]
    do_rebuild = "--rebuild" in args or not args
    do_stats = "--stats" in args or not args

    db = load_db()

    if do_rebuild:
        pairs, classified = rebuild(db)
        save_db(db)
        if not do_stats:
            print(f"sy_scorer: rebuilt db from {pairs} pairs ({classified} classified into buckets)")

    if do_stats:
        print_stats(db)


if __name__ == "__main__":
    main()

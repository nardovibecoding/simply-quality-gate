#!/usr/bin/env python3
"""
shape_analyzer.py — Slice 5 of passive-lesson pipeline (Job B).

Reads ~/NardoWorld/meta/response_shape.jsonl, detects anti-patterns in the
assistant turn that PRECEDED each negative-signal user reply (pushback,
clarification, dismissal, time_pressure). Correlates (signal × anti_pattern)
and emits proposals to ~/NardoWorld/meta/shape_proposals.md when frequency
crosses N_SHAPE=50.

NEVER auto-edits CLAUDE.md. Output is append-only proposal text for Bernard
to manually review + paste.

Usage: python3 shape_analyzer.py           # analyze + write proposals
       python3 shape_analyzer.py --report  # print summary, no file write
"""

import json
import re
import sys
import time
from pathlib import Path
from collections import Counter, defaultdict

NARDO_META = Path.home() / "NardoWorld" / "meta"
SHAPE_FILE = NARDO_META / "response_shape.jsonl"
PROPOSALS_FILE = NARDO_META / "shape_proposals.md"
CUTOFF_FILE = NARDO_META / ".shape_analyzer_cutoff.json"
N_SHAPE = 50
ANALYZER_VERSION = "1.1"

# Negative signals = indicators Bernard was dissatisfied/interrupted.
# Positive signals (ack, satisfaction, elaboration) skipped — no rule to propose.
NEGATIVE_SIGNALS = {"pushback", "clarification", "dismissal", "time_pressure", "pivot"}

# Anti-pattern detectors operate on preceding_assistant_snippet (<=300 chars).
ANTI_PATTERNS = [
    ("header_with_few_items", re.compile(r"^#{1,6}\s+\S+.*?\n(?:[^\n]*\n){0,2}(?=\n|$)", re.MULTILINE)),
    ("for_completeness",      re.compile(r"\b(for completeness|additionally|moreover|furthermore|also worth)\b", re.IGNORECASE)),
    ("preamble_let_me",       re.compile(r"^(let me|i'?ll|allow me to|let'?s)\s+(explain|walk|break down|clarify)", re.IGNORECASE | re.MULTILINE)),
    ("bulleted_heavy",        re.compile(r"(?:^\s*[-*]\s+\S.*\n){4,}", re.MULTILINE)),
    ("numbered_list",         re.compile(r"(?:^\s*\d+[.)]\s+\S.*\n){3,}", re.MULTILINE)),
    ("table_small",           re.compile(r"\|[^\n]+\|\n\|[-:\s|]+\|\n(?:\|[^\n]+\|\n){1,2}", re.MULTILINE)),
    ("hedge_words",           re.compile(r"\b(might|maybe|perhaps|possibly|potentially|sort of|kind of)\b", re.IGNORECASE)),
    ("restating_user",        re.compile(r"\b(you asked|you want|you're asking|as you (said|mentioned|noted))\b", re.IGNORECASE)),
    ("speculative_expansion", re.compile(r"\b(here'?s everything|let'?s (also|go deeper)|while we'?re at it|tangent)\b", re.IGNORECASE)),
]


def load_shapes():
    if not SHAPE_FILE.exists():
        return []
    out = []
    with open(SHAPE_FILE) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def detect_antipatterns(text):
    if not text:
        return []
    hits = []
    for name, pat in ANTI_PATTERNS:
        if pat.search(text):
            hits.append(name)
    return hits


def analyze(shapes):
    signal_counts = Counter()
    corr = defaultdict(Counter)
    for s in shapes:
        sig = s.get("signal_type", "")
        signal_counts[sig] += 1
        if sig not in NEGATIVE_SIGNALS:
            continue
        for ap in detect_antipatterns(s.get("preceding_assistant_snippet", "")):
            corr[sig][ap] += 1
    return signal_counts, corr


def render_proposal(sig, ap, count, total_neg):
    pct = (count / total_neg * 100) if total_neg else 0
    return (
        f"### {sig} × {ap}  (n={count}, {pct:.1f}% of {sig})\n\n"
        f"**Observation:** `{ap}` precedes `{sig}` in {count} turns (≥ N_SHAPE={N_SHAPE}).\n\n"
        f"**Proposed CLAUDE.md rule (PASTE-READY, DO NOT AUTO-APPLY):**\n\n"
        f"> When a reply would contain `{ap}`, check if it serves the question directly. "
        f"If not, drop it — this pattern correlates with `{sig}` feedback ({count} observed cases).\n\n"
        f"**Signal:** `{sig}` = {sig_desc(sig)}\n"
        f"**Anti-pattern:** `{ap}` = {ap_desc(ap)}\n\n"
        f"---\n"
    )


def sig_desc(sig):
    return {
        "pushback":      "user disagreed / corrected you",
        "clarification": "user had to re-explain their question",
        "dismissal":     "user skipped / deferred",
        "time_pressure": "user signaled urgency (was reply too long?)",
        "pivot":         "user abandoned the thread",
    }.get(sig, "unknown")


def ap_desc(ap):
    return {
        "header_with_few_items":  "markdown header for only 1-2 items",
        "for_completeness":       "'for completeness' / 'additionally' expansion",
        "preamble_let_me":        "'let me explain' / 'I'll walk through' preamble",
        "bulleted_heavy":         "4+ consecutive bullet lines",
        "numbered_list":          "numbered list 3+ items",
        "table_small":            "table with ≤2 data rows",
        "hedge_words":            "hedge words (might / maybe / perhaps)",
        "restating_user":         "restating what user said back to them",
        "speculative_expansion":  "'here's everything' / 'let's go deeper' tangent",
    }.get(ap, "unknown")


def write_proposals(signal_counts, corr):
    total_shapes = sum(signal_counts.values())
    ready = []
    below = []
    for sig in NEGATIVE_SIGNALS:
        sig_total = signal_counts.get(sig, 0)
        for ap, count in sorted(corr[sig].items(), key=lambda x: -x[1]):
            if count >= N_SHAPE:
                ready.append(render_proposal(sig, ap, count, sig_total))
            else:
                below.append((sig, ap, count))

    header = (
        f"# Shape Proposals\n\n"
        f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}  \n"
        f"Source: `~/NardoWorld/meta/response_shape.jsonl` ({total_shapes} events)  \n"
        f"Analyzer: v{ANALYZER_VERSION}  \n"
        f"Gate: N_SHAPE={N_SHAPE} (frequency required before proposing a rule)\n\n"
        f"**⚠️  NEVER auto-applied. Bernard reviews + manually edits CLAUDE.md.**\n\n"
        f"## Signal counts\n\n"
    )
    for sig, c in signal_counts.most_common():
        flag = "NEG" if sig in NEGATIVE_SIGNALS else "pos"
        header += f"- `{sig}` ({flag}): {c}\n"
    header += f"\n## Proposals ≥ N_SHAPE (ready for review: {len(ready)})\n\n"

    body = "".join(ready) if ready else "_No correlations have crossed the N_SHAPE threshold yet._\n\n"

    below_section = f"## Pending (below N_SHAPE, {len(below)} candidates)\n\n"
    if below:
        below.sort(key=lambda x: -x[2])
        for sig, ap, c in below[:20]:
            below_section += f"- `{sig}` × `{ap}` — n={c}\n"
    else:
        below_section += "_none_\n"

    PROPOSALS_FILE.write_text(header + body + below_section)


def main():
    shapes = load_shapes()
    if not shapes:
        print("shape_analyzer: response_shape.jsonl empty — run lesson_extractor --full-rescan first")
        sys.exit(1)

    signal_counts, corr = analyze(shapes)

    if "--report" in sys.argv:
        print(f"Total shapes: {sum(signal_counts.values())}")
        print("\nSignal distribution:")
        for sig, c in signal_counts.most_common():
            print(f"  {c:5d} {sig}")
        print("\nNegative-signal × anti-pattern correlations:")
        for sig in NEGATIVE_SIGNALS:
            if not corr[sig]:
                continue
            print(f"\n  [{sig}]")
            for ap, c in sorted(corr[sig].items(), key=lambda x: -x[1]):
                flag = "✓" if c >= N_SHAPE else " "
                print(f"    {flag} {c:4d}  {ap}")
        return

    write_proposals(signal_counts, corr)
    total_neg = sum(v for k, v in signal_counts.items() if k in NEGATIVE_SIGNALS)
    ready = sum(1 for sig in NEGATIVE_SIGNALS for _, c in corr[sig].items() if c >= N_SHAPE)
    print(f"shape_analyzer: wrote {PROPOSALS_FILE} ({ready} proposals ready, {total_neg} negative signals analyzed)")


if __name__ == "__main__":
    main()

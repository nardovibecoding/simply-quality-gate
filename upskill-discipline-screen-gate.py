#!/usr/bin/env python3
# Hook E7: /upskill skill-install gate.
# Created: 2026-05-01
# Trigger: PreToolUse Bash on cp/mv/ln/install -d targeting
# ~/.claude/skills/<slug>/. Block unless
# ~/.claude/scripts/state/upskill-installs.jsonl has a recent (≤7d)
# entry for that slug with discipline_screen=PASS.
#
# Bypass: include `[skip-discipline-screen=<reason>]` anywhere in the
# command. Logged to
# ~/.claude/scripts/state/upskill-screen-skips.jsonl.
#
# Source: rules/disciplines/_index.md + /upskill SKILL.md:28
# (PASS-INSTALL requires ≥14/17 disciplines + no HIGH-$ violations)
import json
import os
import pathlib
import re
import sys
import time


INSTALL_PAT = re.compile(
    r"\b(cp|mv|ln\s+-s|install\s+-d|rsync)\b[^|;&]*"
    r"(?:~/?\.claude/skills/|"
    + re.escape(str(pathlib.Path.home() / ".claude" / "skills") + os.sep)
    + r")([\w.-]+)"
)


def log_skip(slug: str, reason: str) -> None:
    state_dir = pathlib.Path.home() / ".claude" / "scripts" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "slug": slug,
        "reason": reason,
    }
    with open(state_dir / "upskill-screen-skips.jsonl", "a") as f:
        f.write(json.dumps(rec) + "\n")


def slug_has_recent_pass(slug: str) -> bool:
    ledger = (
        pathlib.Path.home()
        / ".claude"
        / "scripts"
        / "state"
        / "upskill-installs.jsonl"
    )
    if not ledger.exists():
        return False
    cutoff = time.time() - 7 * 86400
    try:
        for line in ledger.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("slug") != slug:
                continue
            if rec.get("discipline_screen") not in ("PASS", "EXTRACT-only"):
                continue
            ts = rec.get("ts") or ""
            try:
                rec_time = time.mktime(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S"))
            except Exception:
                continue
            if rec_time >= cutoff:
                return True
    except Exception:
        return False
    return False


def main() -> None:
    try:
        data = json.loads(sys.stdin.read())
    except Exception:
        sys.exit(0)

    if data.get("tool_name") != "Bash":
        sys.exit(0)

    cmd = (data.get("tool_input") or {}).get("command", "") or ""
    m = INSTALL_PAT.search(cmd)
    if not m:
        sys.exit(0)

    slug = m.group(2)
    # ignore obvious non-slugs
    if slug in ("", ".", "..") or slug.startswith("_"):
        sys.exit(0)

    # Self-modifying meta-skill ops on existing dirs (e.g. moving a
    # config file inside a skill) — only fire on top-level slug install,
    # not internal shuffles. Heuristic: command must reference target
    # path with no further trailing /<file> after slug.
    skill_path = pathlib.Path.home() / ".claude" / "skills" / slug
    target_in_cmd = re.search(
        re.escape(slug) + r"(/[\w.-]+)", cmd
    )
    if target_in_cmd:
        # writing inside an existing skill dir, not an install
        sys.exit(0)

    skip_match = re.search(
        r"\[skip-discipline-screen=([^\]]+)\]", cmd
    )
    if skip_match:
        log_skip(slug, skip_match.group(1))
        sys.exit(0)

    if slug_has_recent_pass(slug):
        sys.exit(0)

    print(
        json.dumps(
            {
                "decision": "block",
                "reason": (
                    f"/upskill discipline-screen gate: command would install "
                    f"skill `{slug}` to {skill_path} but no recent "
                    "discipline-quality-screen PASS found in "
                    "~/.claude/scripts/state/upskill-installs.jsonl.\n\n"
                    "Fix: run `/upskill <repo-url>` first — it runs "
                    "scripts/extract.py which writes the screen verdict + "
                    "ledger entry per "
                    "~/.claude/skills/upskill/references/discipline-quality-screen.md "
                    "(17 yes/no checks against D1-D16; PASS-INSTALL needs "
                    "≥14/17 + no HIGH-$ violations).\n\nBypass intentionally: "
                    "add `[skip-discipline-screen=<reason>]` anywhere in the "
                    "command."
                ),
            }
        )
    )
    sys.exit(0)


if __name__ == "__main__":
    main()

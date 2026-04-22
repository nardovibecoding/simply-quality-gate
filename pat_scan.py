#!/usr/bin/env python3
"""Block git commands that would embed PATs in remote URLs."""
import json
import re
import sys

try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)

tool = data.get("tool_name", "")
inp = data.get("tool_input", {})

if tool != "Bash":
    sys.exit(0)

cmd = inp.get("command", "")

# Hard block: prediction-markets + on-chain-bots are private — never push/expose to any PUBLIC remote.
# EXCEPTION: self-hosted SSH remotes on user's own VPS are allowed.
# Allowed patterns: ssh://vps/..., ssh://neuro/..., ssh://<user>@157.180.28.14/..., ssh://<user>@78.141.205.30/...
SELF_HOSTED_REMOTE = re.compile(
    r"ssh://([\w-]+@)?(vps|neuro|157\.180\.28\.14|78\.141\.205\.30)[/:]",
    re.IGNORECASE,
)
def _cmd_targets_public_remote(command: str) -> bool:
    """True if the git command targets GitHub/GitLab/Bitbucket. Resolves
    bare remote names (e.g. `origin`) via `git remote get-url` so pushes
    from inside a private project's cwd aren't blanket-blocked when their
    configured remote is self-hosted ssh."""
    PUBLIC = ("github.com", "gitlab.com", "bitbucket.org")
    if any(h in command for h in PUBLIC):
        return True
    m = re.search(r"git\s+push\s+(\S+)", command)
    if not m:
        return False
    remote = m.group(1)
    if "://" in remote or remote.startswith("git@"):
        return any(h in remote for h in PUBLIC)
    import subprocess
    # Try cwd from hook payload first; fall back to known private project dirs.
    PRIVATE_DIRS = [
        inp.get("cwd") or data.get("cwd") or "",
        "/Users/bernard/prediction-markets",
        "/Users/bernard/on-chain-bots/dagou",
    ]
    for try_dir in PRIVATE_DIRS:
        if not try_dir:
            continue
        try:
            url = subprocess.check_output(
                ["git", "-C", try_dir, "remote", "get-url", remote],
                stderr=subprocess.DEVNULL, timeout=3,
            ).decode().strip()
            if url:
                # If resolved URL is self-hosted ssh, it's allowed.
                if SELF_HOSTED_REMOTE.search(url):
                    return False
                return any(h in url for h in PUBLIC)
        except Exception:
            continue
    return False


if re.search(r"git\s+(push|remote\s+set-url|remote\s+add|clone)", cmd):
    # Match project names in the git remote URL / cmd, but NOT in `cd ~/prediction-markets`
    # path prefixes — those are just cwd changes, not remote targets.
    # Strip leading cd commands before checking project name presence.
    cmd_no_cd = re.sub(r"^(cd\s+\S+\s*&&\s*)+", "", cmd.strip())
    if "prediction-markets" in cmd_no_cd or "prediction_markets" in cmd_no_cd or "on-chain-bots" in cmd_no_cd or "on_chain_bots" in cmd_no_cd:
        # Allow if the resolved remote URL is self-hosted (ssh://vps, ssh://neuro, etc.)
        # _cmd_targets_public_remote resolves bare remote names via `git remote get-url`
        # and returns True only when target is GitHub/GitLab/Bitbucket.
        if _cmd_targets_public_remote(cmd):
            print(json.dumps({
                "decision": "block",
                "reason": "prediction-markets / on-chain-bots are private. Never push to GitHub or any PUBLIC remote. (Self-hosted ssh://vps/... URLs are allowed.)"
            }))
            sys.exit(0)

# Also block gh repo create/delete for private repos
if re.search(r"gh\s+repo\s+(create|delete)", cmd) and ("prediction" in cmd or "on-chain" in cmd):
    print(json.dumps({
        "decision": "block",
        "reason": "prediction-markets / on-chain-bots are private. No GitHub repo allowed."
    }))
    sys.exit(0)

# Warn on broad kill commands that could hit Edwin or other services
if re.search(r"killall\s+(node|python)", cmd) or re.search(r"pkill\s+(-9\s+)?-f\s+['\"]?node['\"]?\s*$", cmd):
    print(json.dumps({
        "decision": "approve",
        "additionalContext": "⚠ Broad kill command — this will also kill Edwin (claude in tmux), admin_bot, legend bots. Use targeted kill: pkill -f 'node.*prediction-markets' or kill <specific-pid>"
    }))
    sys.exit(0)

# Only care about git remote set-url and git push with explicit URLs
if not re.search(r"git\s+(remote\s+set-url|push|clone)", cmd):
    sys.exit(0)

# Detect embedded credentials: https://user:TOKEN@host
pat_pattern = re.compile(r"https?://[^@\s]+:[^@\s]+@(github\.com|gitlab\.com|bitbucket\.org)", re.IGNORECASE)
match = pat_pattern.search(cmd)

if match:
    print(json.dumps({
        "decision": "block",
        "reason": (
            "PAT detected in git URL. Use a clean URL instead:\n"
            "  https://github.com/owner/repo.git\n"
            "Git will prompt for credentials separately, or use gh auth login."
        )
    }))
    sys.exit(0)

sys.exit(0)

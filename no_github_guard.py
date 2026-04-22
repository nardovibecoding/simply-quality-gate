#!/usr/bin/env python3
"""PreToolUse hook: permanently block `git push`, `git remote add`, and
`git clone` for projects that MUST NEVER reach GitHub or any public remote.

Contains trading logic, private wallet keys in .env, cartel intel, and
other Mac-local-forever artefacts. Self-hosted ssh://... remotes may be
added manually by the user if desired — this guard targets GitHub/public
remotes only.

DO NOT remove entries from this set unless the user explicitly says the
project is now safe for GitHub (usually because they scrubbed secrets +
forked to a new clean repo). "Create a GitHub repo" is NOT a remediation
path — these projects are intentionally off-GitHub.
"""
import json
import re
import sys
from pathlib import Path

NEVER_GITHUB_PROJECTS: set[str] = {
    "/Users/bernard/on-chain-bots/dagou",
    "/Users/bernard/on-chain-bots/hyperliquid",
    "/Users/bernard/on-chain-bots/hyperliquid-ts",
    "/Users/bernard/on-chain-bots/sniper",
    "/Users/bernard/on-chain-bots/shared",
    "/Users/bernard/prediction-markets",
    # Add more private projects here as they appear.
}


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError, ValueError):
        print(json.dumps({}))
        return

    tool = data.get("tool_name", "")
    if tool != "Bash":
        print(json.dumps({}))
        return

    cmd: str = data.get("tool_input", {}).get("command", "")
    cwd: str = data.get("cwd", "") or data.get("tool_input", {}).get("cwd", "")

    # Fire only on git push, git remote add, or git clone into a protected path.
    risky = re.search(r"\bgit\s+(push|remote\s+add|clone)\b", cmd)
    if not risky:
        print(json.dumps({}))
        return

    # Only block if the command targets a PUBLIC remote — GitHub/GitLab/Bitbucket.
    # Self-hosted ssh://... and user@host:... remotes are explicitly allowed
    # (honors the module docstring). Detect by scanning the command for known
    # public hostnames. If the push uses a bare remote name ("origin"), the URL
    # isn't in the command — fall back to checking the configured remote URL.
    PUBLIC_HOSTS = ("github.com", "gitlab.com", "bitbucket.org")
    cmd_public = any(h in cmd for h in PUBLIC_HOSTS)

    def remote_is_public(project_dir: str, cmd_text: str) -> bool:
        if cmd_public:
            return True
        # `git push <remote> ...` — the second token is the remote name.
        m = re.search(r"\bgit\s+push\s+(\S+)", cmd_text)
        remote_name = m.group(1) if m else "origin"
        if "://" in remote_name or remote_name.startswith("git@"):
            return any(h in remote_name for h in PUBLIC_HOSTS)
        import subprocess
        try:
            url = subprocess.check_output(
                ["git", "-C", project_dir, "remote", "get-url", remote_name],
                stderr=subprocess.DEVNULL, timeout=3,
            ).decode().strip()
        except Exception:
            return False
        return any(h in url for h in PUBLIC_HOSTS)

    for protected in NEVER_GITHUB_PROJECTS:
        hit = (
            (cwd and Path(cwd).resolve().as_posix().startswith(protected))
            or (protected in cmd)
        )
        if not hit:
            continue
        if not remote_is_public(protected, cmd):
            continue
        print(json.dumps({
            "decision": "block",
            "reason": (
                f"BLOCKED: `{protected}` is in NEVER_GITHUB_PROJECTS. "
                f"This project must NEVER reach GitHub/public remotes "
                f"(trading logic, wallet keys, cartel intel). "
                f"Self-hosted ssh remotes are fine if needed; use a "
                f"different command that doesn't target github.com."
            ),
        }))
        return

    print(json.dumps({}))


if __name__ == "__main__":
    main()

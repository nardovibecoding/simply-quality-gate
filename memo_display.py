#!/usr/bin/env python3
# @bigd-hook-meta
# name: memo_display
# fires_on: UserPromptSubmit
# relevant_intents: [telegram, bigd, meta]
# irrelevant_intents: [git, pm, docx, x_tweet, code, vps, sync, debug]
# cost_score: 2
# always_fire: false
"""UserPromptSubmit hook: display pending memos in terminal.

General memos: show once, auto-delete.
Story memos: show on first message, then every 10 turns, persist until user deletes.
"""
import io
import json
import os
import glob
import subprocess
import sys

sys.path.insert(0, os.path.dirname(__file__))
from _semantic_router import should_fire

BOT_REPO = os.path.expanduser("~/telegram-claude-bot")
MEMO_DIR = os.path.expanduser("~/telegram-claude-bot/memo/pending")
STORY_STATE = os.path.expanduser("~/telegram-claude-bot/memo/.story_state.json")
DONE_DIR = os.path.expanduser("~/telegram-claude-bot/memo/done")


def _pull_memos():
    """Quick git pull to sync VPS memos. Fail silently."""
    try:
        subprocess.run(
            ["git", "-C", BOT_REPO, "pull", "--ff-only", "--quiet", "origin", "main"],
            capture_output=True, timeout=4
        )
    except Exception:
        pass


def _load_story_state():
    if os.path.exists(STORY_STATE):
        with open(STORY_STATE) as f:
            return json.load(f)
    return {}


def _save_story_state(state):
    os.makedirs(os.path.dirname(STORY_STATE), exist_ok=True)
    with open(STORY_STATE, "w") as f:
        json.dump(state, f)


def _parse_memo(path):
    """Parse memo file, return (type, text) or None."""
    try:
        with open(path) as f:
            content = f.read()
    except Exception:
        return None

    memo_type = "general"
    text = content

    if content.startswith("---\n"):
        parts = content.split("---\n", 2)
        if len(parts) >= 3:
            frontmatter = parts[1]
            text = parts[2].strip()
            for line in frontmatter.splitlines():
                if line.startswith("type:"):
                    memo_type = line.split(":", 1)[1].strip()
    return memo_type, text


def _get_user_prompt():
    """Read user prompt from stdin (hook receives JSON with prompt field)."""
    import sys
    try:
        data = json.load(sys.stdin)
        return data.get("prompt", "").lower()
    except Exception:
        return ""


def _delete_all_story_memos():
    """Move all story memos to done/, clear story state."""
    os.makedirs(DONE_DIR, exist_ok=True)
    files = glob.glob(os.path.join(MEMO_DIR, "*.md"))
    deleted = 0
    for path in files:
        result = _parse_memo(path)
        if result and result[0] == "story":
            os.rename(path, os.path.join(DONE_DIR, os.path.basename(path)))
            deleted += 1
    # Clear story state
    if os.path.exists(STORY_STATE):
        os.remove(STORY_STATE)
    return deleted


def main():
    _pull_memos()
    if not os.path.isdir(MEMO_DIR):
        return

    # Check if user wants to delete story memos
    prompt = _get_user_prompt()
    if "delete story memo" in prompt or "delete storymemo" in prompt:
        n = _delete_all_story_memos()
        if n:
            print(json.dumps({"systemMessage": f"Deleted {n} story memo(s)."}))
        return

    files = sorted(glob.glob(os.path.join(MEMO_DIR, "*.md")))
    if not files:
        return

    story_state = _load_story_state()
    output_lines = []
    files_to_delete = []

    for path in files:
        result = _parse_memo(path)
        if result is None:
            continue

        memo_type, text = result
        basename = os.path.basename(path)

        if memo_type == "story":
            # Story memo: track turns, show on first + every 10 turns
            if basename not in story_state:
                story_state[basename] = {"turns": 0, "shown": False}

            state = story_state[basename]
            state["turns"] += 1
            show = not state["shown"] or state["turns"] % 10 == 0
            state["shown"] = True

            if show:
                output_lines.append(f"[STORY MEMO] {text}")
                output_lines.append(f"  (turn {state['turns']}, next reminder at turn {((state['turns'] // 10) + 1) * 10}. Say 'delete story memo' to dismiss)")
        else:
            # General memo: show once, mark for deletion
            output_lines.append(f"[MEMO] {text}")
            files_to_delete.append(path)

    # Save story state
    _save_story_state(story_state)

    # Delete general memos after showing + git rm so they don't come back
    os.makedirs(DONE_DIR, exist_ok=True)
    git_rm_paths = []
    for path in files_to_delete:
        try:
            rel = os.path.relpath(path, BOT_REPO)
            # git rm first (while file still exists)
            subprocess.run(
                ["git", "-C", BOT_REPO, "rm", "--cached", "--quiet", rel],
                capture_output=True, timeout=3
            )
            git_rm_paths.append(rel)
            # Then move to done/
            os.rename(path, os.path.join(DONE_DIR, os.path.basename(path)))
        except Exception:
            pass
    # Commit + push the removals
    if git_rm_paths:
        try:
            subprocess.run(
                ["git", "-C", BOT_REPO, "commit", "-m", "memo: shown and archived"],
                capture_output=True, timeout=5
            )
            subprocess.run(
                ["git", "-C", BOT_REPO, "push", "--quiet"],
                capture_output=True, timeout=10
            )
        except Exception:
            pass

    if output_lines:
        msg = "\n".join(output_lines)
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": msg
            }
        }))


if __name__ == "__main__":
    _raw_stdin = sys.stdin.read()
    try:
        _prompt = json.loads(_raw_stdin).get("prompt", "")
    except Exception:
        _prompt = ""
    sys.stdin = io.StringIO(_raw_stdin)
    if not should_fire(__file__, _prompt):
        print(json.dumps({}))
        sys.exit(0)
    main()

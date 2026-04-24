#!/usr/bin/env python3
# @bigd-hook-meta
# name: pending_questions
# fires_on: UserPromptSubmit
# relevant_intents: []
# irrelevant_intents: []
# cost_score: 1
# always_fire: true
"""UserPromptSubmit hook: remind about pending unanswered questions from Claude."""
import json
import re
import sys
from pathlib import Path

PENDING_FILE = Path("/tmp/claude_pending_questions.json")
STATUSLINE_FILE = Path("/tmp/claude_statusline.json")
MAX_PENDING = 5
REMIND_AFTER = 3  # turns before reminding
EXPIRE_TURNS = 15  # auto-expire after this many turns unanswered

# Patterns that indicate a decision/action question directed at user
QUESTION_PATTERNS = re.compile(
    r"(want me to|which|shall I|ready to|sound right\?|should I|"
    r"do you want|would you like|do you prefer|A or B|"
    r"proceed with|go ahead|confirm|ok to)",
    re.IGNORECASE,
)

# Patterns that are likely rhetorical (exclude)
RHETORICAL_PATTERNS = re.compile(
    r"(why would|how would|what if|isn't it|doesn't that|isn't that|"
    r"who would|what could|how can I help)",
    re.IGNORECASE,
)


def load_pending() -> list:
    if not PENDING_FILE.exists():
        return []
    try:
        return json.loads(PENDING_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return []


def save_pending(pending: list) -> None:
    PENDING_FILE.write_text(json.dumps(pending, indent=2))


def get_last_assistant_message(transcript_path: str) -> str:
    """Read transcript JSONL, return the last assistant message text."""
    p = Path(transcript_path)
    if not p.exists():
        return ""
    lines = p.read_text().splitlines()
    last_text = ""
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg = entry.get("message", {})
        if msg.get("role") == "assistant":
            content = msg.get("content", "")
            if isinstance(content, list):
                parts = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        parts.append(block.get("text", ""))
                last_text = "\n".join(parts)
            elif isinstance(content, str):
                last_text = content
            break
    return last_text


def extract_questions(text: str) -> list:
    """Extract candidate questions from assistant text."""
    questions = []
    for line in text.splitlines():
        line = line.strip()
        if not line.endswith("?"):
            continue
        if len(line) < 10:
            continue
        if RHETORICAL_PATTERNS.search(line):
            continue
        if QUESTION_PATTERNS.search(line):
            questions.append(line)
    return questions


def user_answers_question(prompt: str, question: str) -> bool:
    """Check if user's prompt seems to address a pending question."""
    # Extract significant words from question (4+ chars)
    keywords = [w.lower() for w in re.findall(r"\b\w{4,}\b", question)
                if w.lower() not in {"want", "shall", "would", "should", "which", "that",
                                     "this", "will", "with", "have", "your", "from",
                                     "them", "they", "what", "when", "where", "need"}]
    if not keywords:
        return False
    prompt_lower = prompt.lower()
    matches = sum(1 for kw in keywords if kw in prompt_lower)
    return matches >= max(1, len(keywords) // 3)


def main():
    try:
        hook_input = json.load(sys.stdin)
        prompt = hook_input.get("prompt", "")
    except (json.JSONDecodeError, EOFError):
        print("{}")
        return

    # Get transcript path
    transcript_path = None
    if STATUSLINE_FILE.exists():
        try:
            data = json.loads(STATUSLINE_FILE.read_text())
            transcript_path = data.get("transcript_path")
        except (json.JSONDecodeError, OSError):
            pass

    pending = load_pending()

    # Remove questions that user seems to be answering
    if prompt:
        pending = [q for q in pending if not user_answers_question(prompt, q["text"])]

    # Increment turn counters
    for q in pending:
        q["turns"] = q.get("turns", 0) + 1

    # Scan last assistant message for new questions
    if transcript_path:
        last_msg = get_last_assistant_message(transcript_path)
        if last_msg:
            new_questions = extract_questions(last_msg)
            existing_texts = {q["text"] for q in pending}
            for q_text in new_questions:
                if q_text not in existing_texts:
                    pending.append({"text": q_text, "turns": 0})
                    existing_texts.add(q_text)

    # Expire old questions (turn-based TTL)
    pending = [q for q in pending if q.get("turns", 0) < EXPIRE_TURNS]

    # Cap at MAX_PENDING, drop oldest
    if len(pending) > MAX_PENDING:
        pending = pending[-MAX_PENDING:]

    save_pending(pending)

    # Remind about questions that have been open >= REMIND_AFTER turns
    old_questions = [q for q in pending if q.get("turns", 0) >= REMIND_AFTER]
    if old_questions:
        remind = old_questions[:3]
        bullets = "\n".join(f"  - {q['text']}" for q in remind)
        output = {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": (
                    f"⏳ Still open ({len(remind)} unanswered question(s) from earlier):\n"
                    f"{bullets}\n"
                    "Address these if relevant, or ignore if superseded."
                )
            }
        }
        print(json.dumps(output))
    else:
        print("{}")


if __name__ == "__main__":
    main()

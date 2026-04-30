#!/usr/bin/env python3
"""
lesson_extractor.py — Slice 1 of passive-lesson pipeline.
Extracts [SY] suggestion + next user reply pairs from session JSONL.
Appends to ~/NardoWorld/meta/sy_pairs.jsonl (idempotent via session_id+turn_idx key).
Writes status to /tmp/s_extractor_last_run.json on every run (success or error).

Called from /s SKILL.md Step J (fire-and-forget, background subprocess).
Usage: python3 lesson_extractor.py <session_jsonl_path>
       python3 lesson_extractor.py --from-statusline   # reads path from /tmp/claude_statusline.json
       python3 lesson_extractor.py --full-rescan       # scan all jsonl files in projects dir

No LLM calls. No external deps beyond stdlib.
"""

import json
import re
import sys
import time
from pathlib import Path

# --- Paths ---
NARDO_META = Path.home() / "NardoWorld" / "meta"
SY_PAIRS_FILE = NARDO_META / "sy_pairs.jsonl"
SHAPE_FILE = NARDO_META / "response_shape.jsonl"
PUSHBACK_FILE = NARDO_META / "pushback_reasons.jsonl"
STATUS_FILE = Path("/tmp/s_extractor_last_run.json")
STATUSLINE_FILE = Path("/tmp/claude_statusline.json")
PROJECTS_DIR = Path.home() / ".claude" / "projects" / "-Users-bernard"

EXTRACTOR_VERSION = "1.0"
SCHEMA_VERSION = 1

# --- [SY] detection regex ---
# Matches: **Suggestion:** [SY] ... or standalone [SY] at start of footer line
SY_LINE_RE = re.compile(
    r'\*\*Suggestion:\*\*\s*\[SY\](.+?)$'
    r'|\[SY\]\s+([A-Z0-9][^\n]*)',
    re.MULTILINE
)

# Signals for classifying user reply
ACCEPT_RE = re.compile(
    r'^(ok|okay|got it|yes|sy|lets go|let\'?s go|alright|makes sense|understood'
    r'|o\d+|\d{1,2}$|proceed|go|fire|ship|do it|yep|yeah|sure|correct|right|confirmed'
    r'|both|all|done|continue|next)',
    re.IGNORECASE
)
REJECT_RE = re.compile(
    r'^(no[,\s]|nah|sn|not now|skip|maybe later|no thanks|ignore|stop'
    r'|don\'?t|disagree|that\'?s wrong|wrong|incorrect|actually|nope)',
    re.IGNORECASE
)
CLARIFY_RE = re.compile(
    r'\?|how do|why|what if|can we|but|wait|actually|i mean|you said|hold on',
    re.IGNORECASE
)

# Bucket classification — regex per bucket type
BUCKET_PATTERNS = {
    "scope-and-proceed": re.compile(
        r'scope|scoping|plan|phase|proceed|fire next|dispatch|architect|design|outline|spec',
        re.IGNORECASE
    ),
    "commit-only": re.compile(
        r'commit|save|checkpoint|git|push\s*to|record|preserve|snapshot',
        re.IGNORECASE
    ),
    "audit-spawn": re.compile(
        r'audit|check|review|verify|scan|inspect|examine|validate|lint|test',
        re.IGNORECASE
    ),
    "wait-for-bg": re.compile(
        r'wait|bg|background|slots? full|queue|pending|while waiting|hold|defer|async',
        re.IGNORECASE
    ),
    "memory-update": re.compile(
        r'memory|save|file|wiki|nardoworld|lesson|rollup|update.*doc|doc.*update',
        re.IGNORECASE
    ),
    "skill-invoke": re.compile(
        r'/s|/combo|/lint|/ship|/skill|skill|invoke|run.*skill|trigger',
        re.IGNORECASE
    ),
    "config-tweak-local": re.compile(
        r'config|setting|param|env|flag|toggle|enable|disable|switch|tweak',
        re.IGNORECASE
    ),
}

# Response-shape (Job B) signal patterns — per plan §3 Slice 4
SHAPE_PATTERNS = [
    ("pivot",         re.compile(r"actually,?\s+let'?s|change of plan|forget that|new direction|scrap that", re.IGNORECASE)),
    ("time_pressure", re.compile(r"\b(quick|fast|hurry|asap|urgent|need it now|running out)\b", re.IGNORECASE)),
    ("pushback",      re.compile(r"^(no[,\s]|nah|nope)|that'?s?\s+wrong|disagree|not quite|that doesn'?t", re.IGNORECASE)),
    ("clarification", re.compile(r"^(wait\b|actually\b|i mean\b|you said\b|but earlier|hold on)", re.IGNORECASE)),
    ("question",      re.compile(r"\?(?:\s|$)|\?$|^(how do|why did|what if|can we)\b", re.IGNORECASE)),
    ("dismissal",     re.compile(r"^(skip|not now|maybe later|no thanks|ignore)\b", re.IGNORECASE)),
    ("satisfaction",  re.compile(r"\b(perfect|exactly|that'?s? it|love it|nailed it)\b", re.IGNORECASE)),
    ("elaboration",   re.compile(r"^(also\b|additionally\b|and what about|one more|on top of)", re.IGNORECASE)),
    ("ack",           re.compile(r"^(ok|okay|got it|yes|sy|lets go|let'?s go|alright|makes sense|understood|o\d+|\d{1,2})\b", re.IGNORECASE)),
]

def classify_shape(user_text):
    """Return signal_type name or None. Order matters: specific before generic."""
    t = user_text.strip()
    if not t:
        return None
    for name, pat in SHAPE_PATTERNS:
        if pat.search(t):
            return name
    return None


# Pushback reasoning extraction patterns
# Each pattern captures the substring AFTER the trigger word as the reasoning.
REASONING_PATTERNS = [
    ("because",     re.compile(r'\bbecause\s+(.{5,200}?)(?:\.|$)', re.I | re.DOTALL)),
    ("need",        re.compile(r'\b(?:we|i)\s+need\s+(.{5,200}?)(?:\.|$)', re.I | re.DOTALL)),
    ("want",        re.compile(r'\b(?:i)\s+(?:want|prefer)\s+(.{5,200}?)(?:\.|$)', re.I | re.DOTALL)),
    ("first",       re.compile(r'\b(.{3,80}?)\s+first\b', re.I)),
    ("not_X_but_Y", re.compile(r'\b(?:no|not)\s+(.{3,80}?)\s+but\s+(.{5,200}?)(?:\.|$)', re.I | re.DOTALL)),
    ("ylyy_perm",   re.compile(r'\b(ylyy|perm fix|永久|一勞永逸)\b', re.I)),
]

# Principle tags — map reasoning content to recurring themes.
PRINCIPLE_TAGS = [
    ("permanence",     re.compile(r'\b(ylyy|perm fix|once|永久|一勞永逸|durable|recur)\b', re.I)),
    ("verify_first",   re.compile(r'\b(verify|check|confirm|prove|evidence|test)\b', re.I)),
    ("scope_concern",  re.compile(r'\b(scope|too (big|small)|out of scope|narrow|broaden)\b', re.I)),
    ("layman",         re.compile(r'\b(layman|eli5|simple|explain|what does|what is)\b', re.I)),
    ("priority",       re.compile(r'\b(blocker|urgent|first|priority|important|matters)\b', re.I)),
    ("timing",         re.compile(r'\b(later|now|tomorrow|today|tonight|morning|after|before)\b', re.I)),
    ("alternative",    re.compile(r'\b(actually|instead|rather|better|other (way|option))\b', re.I)),
    ("constraint",     re.compile(r'\b(can\'?t|cannot|must|have to|require|need)\b', re.I)),
    ("consolidation",  re.compile(r'\b(consolidate|single source|one place|ssot|unified)\b', re.I)),
    ("safety",         re.compile(r'\b(safe|risk|backup|preserve|don\'?t lose|protect)\b', re.I)),
]


def extract_reasoning(reply_text):
    """Pull reasoning fragments + principle tags from a user reply."""
    if not reply_text:
        return [], []
    fragments = []
    for label, pat in REASONING_PATTERNS:
        for m in pat.finditer(reply_text):
            captured = " | ".join(g.strip() for g in m.groups() if g)
            if captured:
                fragments.append({"type": label, "text": captured[:200]})
    tags = [name for name, pat in PRINCIPLE_TAGS if pat.search(reply_text)]
    return fragments, tags


# Hard-gate patterns — never auto-classify these (future Slice 2+ use)
HARD_GATES = [
    re.compile(p, re.IGNORECASE) for p in [
        r'wallet|private[\s_]?key|POLY_PRIVATE_KEY|funds|transfer|send.*usdc|send.*eth|\.env',
        r'credential[s]?|secret[s]?|api[\s_]?key|token.*write|auth.*token',
        r'force[\s-]?push|push.*\-\-force|\-\-force.*push',
        r'reset.*hard|hard.*reset|git.*reset',
        r'rm\s+-rf|rm\s+-r|drop\s+table|truncate|wipe|purge',
        r'delete.*all|remove.*all|mass.*delete',
        r'deploy.*vps|vps.*deploy|deploy.*prod|prod.*deploy',
        r'ssh.*prod|prod.*ssh|ssh.*hel|ssh.*london',
        r'push.*origin|origin.*push|git.*push',
        r'systemctl.*stop|systemctl.*restart|kill.*process|pkill',
        r'CLAUDE\.md.*change|change.*CLAUDE\.md|modify.*rules|rules.*modify',
        r'hook.*disable|disable.*hook|bypass.*hook',
    ]
]


def extract_text(content):
    """Extract plain text from message content (str or list of blocks)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "\n".join(parts)
    return ""


def is_human_text(text):
    """Return True if this is a real human message (not tool result / notification)."""
    t = text.strip()
    if not t:
        return False
    skip_prefixes = (
        "<task-notification", "<tool_result", "<function_results",
        "<local-command", "<command-name", "<command-message",
        "<local-command-stdout", "[Request interrupted",
        "<tool-use", "<function_calls>",
        "Base directory for this skill:",
        "<system-reminder",
    )
    return not any(t.startswith(p) for p in skip_prefixes)


def classify_bucket(sy_text):
    """Return bucket name (str) or 'unknown'."""
    for bucket, pat in BUCKET_PATTERNS.items():
        if pat.search(sy_text):
            return bucket
    return "unknown"


def classify_signal(user_text):
    """Return 'accept' | 'reject' | 'clarify' | 'unknown'."""
    t = user_text.strip()
    if not t:
        return "unknown"
    if ACCEPT_RE.match(t):
        return "accept"
    if REJECT_RE.match(t):
        return "reject"
    if CLARIFY_RE.search(t):
        return "clarify"
    return "unknown"


def is_hard_gated(sy_text):
    """Return True if any hard-gate pattern matches the [SY] text."""
    return any(p.search(sy_text) for p in HARD_GATES)


def load_existing_keys(filepath):
    """Load set of (session_id, turn_idx) already in sy_pairs.jsonl."""
    keys = set()
    if not filepath.exists():
        return keys
    with open(filepath) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                sid = obj.get("session_id", "")
                tidx = obj.get("turn_idx", -1)
                if sid and tidx >= 0:
                    keys.add((sid, tidx))
            except json.JSONDecodeError:
                pass
    return keys


def parse_jsonl(filepath):
    """Parse JSONL file; return list of dicts (skip malformed lines)."""
    turns = []
    with open(filepath, encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            try:
                turns.append((i, json.loads(line)))
            except json.JSONDecodeError:
                pass
    return turns


def extract_sy_pairs(jsonl_path, existing_keys):
    """
    Scan a session JSONL for [SY] assistant turns + next human reply.
    Return list of new sy_pair dicts (not already in existing_keys).
    """
    path = Path(jsonl_path)
    if not path.exists():
        return [], f"file not found: {jsonl_path}"

    try:
        turns = parse_jsonl(path)
    except Exception as e:
        return [], f"parse error: {e}"

    # Build index: line_idx -> (role, text, session_id, timestamp)
    parsed = []
    for line_idx, obj in turns:
        msg = obj.get("message", {})
        role = msg.get("role", "")
        tp = obj.get("type", "")
        content = msg.get("content", "")
        text = extract_text(content)
        session_id = obj.get("sessionId", "")
        ts_str = obj.get("timestamp", "")
        parsed.append({
            "line_idx": line_idx,
            "type": tp,
            "role": role,
            "text": text,
            "session_id": session_id,
            "ts_str": ts_str,
        })

    new_pairs = []
    n = len(parsed)

    for i, turn in enumerate(parsed):
        if turn["role"] != "assistant":
            continue
        text = turn["text"]
        if "[SY]" not in text:
            continue

        # Find [SY] lines in this assistant message
        sy_matches = list(SY_LINE_RE.finditer(text))
        if not sy_matches:
            continue

        # Use last [SY] line as the suggestion
        m = sy_matches[-1]
        sy_text = (m.group(1) or m.group(2) or "").strip()
        if not sy_text:
            sy_text = text[m.start():m.start()+200].strip()

        turn_idx = turn["line_idx"]
        session_id = turn["session_id"] or Path(jsonl_path).stem
        key = (session_id, turn_idx)

        if key in existing_keys:
            continue

        # Find next human text turn
        user_reply = ""
        for j in range(i + 1, min(i + 30, n)):
            nt = parsed[j]
            if nt["role"] == "user" and nt["type"] == "user":
                if is_human_text(nt["text"]):
                    user_reply = nt["text"].strip()[:300]
                    break

        # Parse timestamp
        ts_epoch = int(time.time())
        if turn["ts_str"]:
            try:
                import datetime
                dt = datetime.datetime.fromisoformat(
                    turn["ts_str"].replace("Z", "+00:00")
                )
                ts_epoch = int(dt.timestamp())
            except Exception:
                pass

        pair = {
            "schema_version": SCHEMA_VERSION,
            "ts": ts_epoch,
            "session_id": session_id,
            "turn_idx": turn_idx,
            "sy_text": sy_text[:300],
            "user_reply": user_reply,
            "signal": classify_signal(user_reply) if user_reply else "unknown",
            "bucket": classify_bucket(sy_text),
            "hard_gated": is_hard_gated(sy_text),
            "extractor_version": EXTRACTOR_VERSION,
        }
        new_pairs.append(pair)
        existing_keys.add(key)

    return new_pairs, None


def _atomic_append(path, content: str) -> None:
    """flock-protected append so parallel sessions never interleave writes."""
    import fcntl
    NARDO_META.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.write(content)
            f.flush()
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def append_pairs(pairs):
    """Append new pairs to SY_PAIRS_FILE (flock-safe)."""
    if not pairs:
        return
    buf = "".join(json.dumps(p) + "\n" for p in pairs)
    _atomic_append(SY_PAIRS_FILE, buf)


def append_shapes(shapes):
    if not shapes:
        return
    buf = "".join(json.dumps(s) + "\n" for s in shapes)
    _atomic_append(SHAPE_FILE, buf)


def load_existing_shape_keys(filepath):
    keys = set()
    if not filepath.exists():
        return keys
    with open(filepath) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                sid = obj.get("session_id", "")
                tidx = obj.get("turn_idx", -1)
                if sid and tidx >= 0:
                    keys.add((sid, tidx))
            except json.JSONDecodeError:
                pass
    return keys


def extract_response_shapes(jsonl_path, existing_keys):
    """Scan human turns, classify response shape, pair with preceding assistant snippet."""
    path = Path(jsonl_path)
    if not path.exists():
        return [], f"file not found: {jsonl_path}"
    try:
        turns = parse_jsonl(path)
    except Exception as e:
        return [], f"parse error: {e}"

    parsed = []
    for line_idx, obj in turns:
        msg = obj.get("message", {})
        parsed.append({
            "line_idx": line_idx,
            "type": obj.get("type", ""),
            "role": msg.get("role", ""),
            "text": extract_text(msg.get("content", "")),
            "session_id": obj.get("sessionId", ""),
            "ts_str": obj.get("timestamp", ""),
        })

    new_shapes = []
    for i, turn in enumerate(parsed):
        if turn["role"] != "user" or turn["type"] != "user":
            continue
        text = turn["text"]
        if not is_human_text(text):
            continue
        signal = classify_shape(text)
        if not signal:
            continue

        turn_idx = turn["line_idx"]
        session_id = turn["session_id"] or path.stem
        key = (session_id, turn_idx)
        if key in existing_keys:
            continue

        preceding = ""
        # Walk back up to 30 turns to skip tool-only assistant turns (tool_use blocks, no text).
        for j in range(i - 1, max(i - 30, -1), -1):
            pt = parsed[j]
            if pt["role"] == "assistant":
                txt = pt["text"].strip()
                if txt:
                    preceding = txt[:300]
                    break

        ts_epoch = int(time.time())
        if turn["ts_str"]:
            try:
                import datetime
                dt = datetime.datetime.fromisoformat(turn["ts_str"].replace("Z", "+00:00"))
                ts_epoch = int(dt.timestamp())
            except Exception:
                pass

        new_shapes.append({
            "schema_version": SCHEMA_VERSION,
            "ts": ts_epoch,
            "session_id": session_id,
            "turn_idx": turn_idx,
            "signal_type": signal,
            "snippet": text.strip()[:300],
            "preceding_assistant_snippet": preceding,
            "extractor_version": EXTRACTOR_VERSION,
        })
        existing_keys.add(key)

    return new_shapes, None


def write_status(session_id, pairs_extracted, error=None, files_scanned=1, shapes_extracted=0):
    """Write status file for observability (visible failure detection)."""
    status = {
        "ts": int(time.time()),
        "ts_human": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "session_id": session_id,
        "files_scanned": files_scanned,
        "pairs_extracted": pairs_extracted,
        "shapes_extracted": shapes_extracted,
        "sy_pairs_total": count_lines(SY_PAIRS_FILE),
        "response_shape_total": count_lines(SHAPE_FILE),
        "error": error,
        "status": "error" if error else "ok",
        "extractor_version": EXTRACTOR_VERSION,
    }
    STATUS_FILE.write_text(json.dumps(status, indent=2))


def count_lines(filepath):
    """Count lines in a file (returns 0 if not found)."""
    try:
        with open(filepath) as f:
            return sum(1 for _ in f)
    except FileNotFoundError:
        return 0


def resolve_jsonl_path():
    """Get current session JSONL path from statusline.json, fallback to newest file."""
    if STATUSLINE_FILE.exists():
        try:
            sl = json.loads(STATUSLINE_FILE.read_text())
            tp = sl.get("transcript_path", "")
            if tp and Path(tp).exists():
                return tp
        except Exception:
            pass
    # Fallback: newest jsonl in projects dir
    files = sorted(
        PROJECTS_DIR.glob("*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True
    )
    if files:
        return str(files[0])
    return None


def main():
    args = sys.argv[1:]

    if "--full-rescan" in args:
        existing_keys = load_existing_keys(SY_PAIRS_FILE)
        existing_shape_keys = load_existing_shape_keys(SHAPE_FILE)
        all_pairs = []
        all_shapes = []
        files = list(PROJECTS_DIR.glob("*.jsonl"))
        errors = []
        for fp in files:
            pairs, err = extract_sy_pairs(str(fp), existing_keys)
            if err:
                errors.append(f"{fp.name}: sy:{err}")
            else:
                all_pairs.extend(pairs)
            shapes, err = extract_response_shapes(str(fp), existing_shape_keys)
            if err:
                errors.append(f"{fp.name}: shape:{err}")
            else:
                all_shapes.extend(shapes)
        if all_pairs:
            append_pairs(all_pairs)
        if all_shapes:
            append_shapes(all_shapes)
        error_msg = "; ".join(errors) if errors else None
        write_status("full-rescan", len(all_pairs), error_msg, len(files), len(all_shapes))
        print(f"Full rescan: {len(files)} files, {len(all_pairs)} SY pairs, {len(all_shapes)} shapes")
        return

    if "--from-statusline" in args or not args:
        jsonl_path = resolve_jsonl_path()
    else:
        jsonl_path = args[0]

    if not jsonl_path:
        write_status("unknown", 0, "could not resolve jsonl path")
        sys.exit(1)

    session_id = Path(jsonl_path).stem
    existing_keys = load_existing_keys(SY_PAIRS_FILE)
    pairs, error = extract_sy_pairs(jsonl_path, existing_keys)

    if error:
        write_status(session_id, 0, error)
        sys.exit(1)

    if pairs:
        append_pairs(pairs)

    existing_shape_keys = load_existing_shape_keys(SHAPE_FILE)
    shapes, shape_err = extract_response_shapes(jsonl_path, existing_shape_keys)
    if shapes:
        append_shapes(shapes)

    write_status(session_id, len(pairs), shape_err, 1, len(shapes))
    print(f"lesson_extractor: {len(pairs)} new SY, {len(shapes)} new shape(s) from {Path(jsonl_path).name}")

    if pairs:
        try:
            import subprocess
            subprocess.Popen(
                [sys.executable, str(Path(__file__).parent / "sy_scorer.py"), "--rebuild"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass


if __name__ == "__main__":
    main()

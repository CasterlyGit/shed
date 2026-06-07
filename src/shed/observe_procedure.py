"""Autonomous passive procedure capture.

On the ``Stop`` hook (via ``reflect.py``), this module scans the session
transcript JSONL and asks: "did a coherent, successful, repeatable procedure
happen?" If yes, it distils a ``kind: skill`` proposal straight from the
existing trace — zero extra tokens, zero agent re-runs.

## Design principle (Tarun, 2026-05-30)

Everything is autonomous and free in steady state:

    PostToolUse already logs tool calls → Stop hook reads them → heuristic
    prefilter decides "procedure?" → distil from real trace → skill proposal
    in morning brief.

The ONLY gated action: if shed detects a captured skill's path looks expensive,
it *offers* to run ``shed learn`` (the convergence loop). That offer is the
single nudge the user ever sees.

## Heuristic

A session "contains a procedure" when:
1. A goal-stating user message exists (contains imperative verbs or task nouns).
2. A burst of ≥ MIN_TOOL_BURST related Bash/Edit/Write calls follows.
3. The session ends with a success signal (no unhandled error in the last
   assistant turn; or explicit success language).

Everything is local + deterministic — no LLM calls, no network.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

from shed.config import Config, current_mode, is_globally_disabled, load_config, proposals_dir
from shed.memory import now_iso, slugify

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

MIN_TOOL_BURST = 3   # minimum Bash/Edit/Write calls to qualify as a procedure
MAX_TASK_CHARS = 200  # cap the task description we extract for the skill title
MAX_SKILL_CHARS = 5200  # keep parity with learn.py's default

# Imperative / goal-stating language in user messages
_GOAL_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\b(build|deploy|run|fix|install|create|add|update|refactor|migrate|set up|setup|push|release|ship|test|start|stop|restart|configure|implement|generate|write|make)\b", re.I),
    re.compile(r"\b(let'?s|can you|please|could you|I need|I want|we need)\b", re.I),
]

# Success language in the last assistant turn
_SUCCESS_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\b(done|complete|success|finished|deployed|installed|passed|green|shipped|ready|worked|verified)\b", re.I),
    re.compile(r"\b(all \d+ tests|tests pass|suite pass|✓|✅)\b", re.I),
]

# Error / failure language — if dominant in the last assistant turn, skip
_FAILURE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\b(error|failed|exception|traceback|cannot|could not|not found|permission denied)\b", re.I),
]

# Tool names that constitute "doing work" (Bash + file-modifying tools)
_WORK_TOOLS: frozenset[str] = frozenset({
    "Bash", "Edit", "Write", "MultiEdit",
    "computer_use", "execute_bash", "execute_command",
})


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


@dataclass
class ProcedureSignal:
    """Evidence that a coherent procedure occurred in this session."""
    task: str            # extracted goal description
    tool_calls: list[str]  # names of work tools called
    success: bool        # success signal present in last assistant turn
    confidence: float    # 0..1


@dataclass
class ProcedureProposal:
    slug: str
    path: Path
    created_at: str
    task: str
    confidence: float


# ---------------------------------------------------------------------------
# Transcript parsing
# ---------------------------------------------------------------------------


def _parse_transcript(transcript_path: Path) -> tuple[list[dict], str, str]:
    """Return (tool_events, first_user_text, last_assistant_text).

    tool_events: list of {"tool": name, "ts": float} rows.
    first_user_text: the earliest non-hook user message text.
    last_assistant_text: the most recent assistant text block.
    """
    if not transcript_path or not transcript_path.exists():
        return [], "", ""

    try:
        lines = transcript_path.read_text(errors="ignore").splitlines()
    except Exception:
        return [], "", ""

    tool_events: list[dict] = []
    first_user_text = ""
    last_assistant_text = ""

    _hook_re = re.compile(
        r"^(stop|pretooluse|posttooluse|userpromptsubmit|sessionstart|"
        r"subagentstop|notification|precompact)\s+hook\b",
        re.I,
    )
    _hook_path_re = re.compile(r"^\[[^\]]*hooks?[^\]]*\]:", re.I)

    for line in lines:
        try:
            row = json.loads(line)
        except Exception:
            continue

        rtype = row.get("type", "")
        msg = row.get("message")

        # Tool-use rows: {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": ...}]}}
        if isinstance(msg, dict):
            role = msg.get("role", "")
            content = msg.get("content") or []
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "tool_use":
                        tname = block.get("name", "")
                        if tname:
                            tool_events.append({"tool": tname, "ts": time.time()})
                    if role == "assistant" and block.get("type") == "text":
                        txt = block.get("text", "")
                        if txt.strip():
                            last_assistant_text = txt  # keep last
                    if role == "user" and not first_user_text:
                        if block.get("type") == "text":
                            txt = block.get("text", "")
                            if txt.strip():
                                s = txt.lstrip()
                                if _hook_re.match(s):
                                    continue
                                first_line = s.split("\n", 1)[0]
                                if _hook_path_re.match(first_line):
                                    continue
                                first_user_text = txt.strip()

        # Some transcript formats are flat: {"role": "user"|"assistant", "content": ...}
        elif rtype in ("user", "assistant"):
            content = row.get("content") or []
            if isinstance(content, str):
                if rtype == "assistant":
                    last_assistant_text = content
                elif rtype == "user" and not first_user_text:
                    first_user_text = content
            elif isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "tool_use":
                        tname = block.get("name", "")
                        if tname:
                            tool_events.append({"tool": tname, "ts": time.time()})
                    if block.get("type") == "text":
                        txt = block.get("text", "")
                        if rtype == "assistant" and txt.strip():
                            last_assistant_text = txt
                        elif rtype == "user" and not first_user_text and txt.strip():
                            first_user_text = txt.strip()

    return tool_events, first_user_text, last_assistant_text


# ---------------------------------------------------------------------------
# Heuristic prefilter
# ---------------------------------------------------------------------------


def detect_procedure(
    transcript_path: Path,
    min_tool_burst: int = MIN_TOOL_BURST,
) -> ProcedureSignal | None:
    """Scan a transcript and return a ProcedureSignal if a procedure was detected.

    Returns None when the heuristic is not confident (fail-open: better to
    miss a skill than to spam the brief with noise).
    """
    tool_events, first_user_text, last_assistant_text = _parse_transcript(transcript_path)

    # Gate 1: enough work tools were called.
    work_calls = [e["tool"] for e in tool_events if e["tool"] in _WORK_TOOLS]
    if len(work_calls) < min_tool_burst:
        return None

    # Gate 2: goal-stating language in the first user message.
    if not first_user_text:
        return None
    goal_hits = sum(
        1 for p in _GOAL_PATTERNS if p.search(first_user_text)
    )
    if goal_hits == 0:
        return None

    # Gate 3: success signal in the last assistant turn (not dominated by errors).
    success = False
    if last_assistant_text:
        success_hits = sum(1 for p in _SUCCESS_PATTERNS if p.search(last_assistant_text))
        failure_hits = sum(1 for p in _FAILURE_PATTERNS if p.search(last_assistant_text))
        success = success_hits >= 1 and failure_hits <= success_hits

    # Confidence: more tools + explicit success → higher confidence.
    base_conf = min(0.5 + 0.05 * len(work_calls), 0.8)
    confidence = min(base_conf + (0.15 if success else 0.0), 0.95)

    # Extract a task description from the first user message (first line, capped).
    task = first_user_text.splitlines()[0].strip()[:MAX_TASK_CHARS]

    return ProcedureSignal(
        task=task,
        tool_calls=work_calls,
        success=success,
        confidence=confidence,
    )


# ---------------------------------------------------------------------------
# Skill distillation from trace
# ---------------------------------------------------------------------------


PROTECTED_OPEN = "<!-- shed:protected -->"
PROTECTED_CLOSE = "<!-- /shed:protected -->"


def _make_description(task: str) -> str:
    t = task.strip().rstrip(".")
    return f"Use when you need to: {t}. Captured by shed from a real session."


def _distill_tool_sequence(tool_calls: list[str]) -> str:
    """Summarise the tool call sequence into a compact readable pattern."""
    if not tool_calls:
        return "- (no tool calls recorded)"
    # Collapse consecutive duplicates into "×N" form.
    groups: list[tuple[str, int]] = []
    for name in tool_calls:
        if groups and groups[-1][0] == name:
            groups[-1] = (name, groups[-1][1] + 1)
        else:
            groups.append((name, 1))
    lines = [f"- {name}" + (f" ×{n}" if n > 1 else "") for name, n in groups]
    return "\n".join(lines)


def _skill_markdown_from_signal(
    signal: ProcedureSignal,
    slug: str,
    session_id: str = "",
    max_chars: int = MAX_SKILL_CHARS,
) -> str:
    """Build a SKILL.md body from a ProcedureSignal (no re-run, zero tokens)."""
    tool_seq = _distill_tool_sequence(signal.tool_calls)
    approach = (
        f"Observed tool sequence ({len(signal.tool_calls)} work-tool calls):\n\n"
        f"{tool_seq}\n\n"
        f"Task statement: {signal.task}"
    )

    return (
        f"---\n"
        f"name: {slug}\n"
        f"description: >-\n"
        f"  {_make_description(signal.task)}\n"
        f"source: shed-passive\n"
        f"status: observed\n"
        f"updated: {now_iso()}\n"
        f"session_id: {session_id}\n"
        f"confidence: {signal.confidence:.2f}\n"
        f"---\n\n"
        f"# {slug}\n\n"
        f"## Task\n\n{signal.task}\n\n"
        f"## Observed approach\n\n"
        f"{PROTECTED_OPEN}\n"
        f"Captured passively from a real session. "
        f"This is the tool sequence shed observed — not a hand-tuned convergence. "
        f"Refine with `shed learn` if you want a cost-optimised version.\n\n"
        f"{approach}\n"
        f"{PROTECTED_CLOSE}\n\n"
        f"## Notes\n\n"
        f"- Captured from live session (passive, no re-run).\n"
        f"- Success signal: {'yes' if signal.success else 'not detected'}.\n"
        f"- Tool calls observed: {len(signal.tool_calls)}.\n"
    )


def _write_procedure_proposal(
    signal: ProcedureSignal,
    slug: str,
    session_id: str = "",
    max_chars: int = MAX_SKILL_CHARS,
) -> Path:
    proposals_dir().mkdir(parents=True, exist_ok=True)

    # Write-time dedup: if a proposal with this slug already exists, skip.
    pdir = proposals_dir()
    existing = sorted(pdir.glob(f"skill-*-{slug}.md"))
    if existing:
        return existing[0]

    ts = time.strftime("%Y%m%d-%H%M%S")
    path = pdir / f"skill-{ts}-{slug}.md"

    skill_body = _skill_markdown_from_signal(signal, slug, session_id, max_chars)
    proposal = (
        f"---\n"
        f"kind: skill\n"
        f"name: {slug}\n"
        f"confidence: {signal.confidence:.2f}\n"
        f"source: shed-passive\n"
        f"---\n"
        f"<!-- shed skill proposal -->\n\n"
        f"## {slug}\n\n"
        f"Captured passively from a real session "
        f"({len(signal.tool_calls)} tool calls). "
        f"Accept to write `SKILL.md` live into your skills dir.\n\n"
        f"<!-- SKILL-BODY-START -->\n"
        f"{skill_body}"
        f"<!-- SKILL-BODY-END -->\n"
    )
    path.write_text(proposal)
    return path


# ---------------------------------------------------------------------------
# Repeat-gate (session counter per slug)
# ---------------------------------------------------------------------------


def _seen_count(slug: str) -> int:
    """How many times has this procedure slug been seen?

    Reads a lightweight JSONL counter in state_dir(). Fail-open → 0.
    """
    from shed.config import state_dir
    p = state_dir() / "procedure-seen.jsonl"
    if not p.exists():
        return 0
    count = 0
    try:
        for line in p.read_text(errors="ignore").splitlines():
            try:
                row = json.loads(line)
                if row.get("slug") == slug:
                    count += 1
            except Exception:
                continue
    except Exception:
        pass
    return count


def _record_seen(slug: str) -> None:
    """Append a seen-event for this slug."""
    from shed.config import state_dir
    state_dir().mkdir(parents=True, exist_ok=True)
    p = state_dir() / "procedure-seen.jsonl"
    row = {"ts": time.time(), "slug": slug}
    try:
        with p.open("a") as f:
            f.write(json.dumps(row) + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


def observe_procedure(
    transcript_path: Path | None,
    session_id: str = "",
    cfg: Config | None = None,
    min_tool_burst: int = MIN_TOOL_BURST,
    repeat_gate: int = 1,  # propose after seeing it N times (1 = every session)
) -> ProcedureProposal | None:
    """Main entrypoint called from ``reflect.py`` at Stop time.

    Scans the transcript, runs the heuristic prefilter, and emits a skill
    proposal if a coherent procedure is detected. Returns None (fail-open) on
    any error.

    ``repeat_gate``: only emit a proposal after the same procedure has been
    seen ≥ N sessions. Default 1 (every session) — set to 2+ for lower noise.
    """
    if cfg is None:
        try:
            from shed.config import load_config
            cfg = load_config()
        except Exception:
            cfg = Config()

    # Privacy / kill-switch checks — same contract as observe.py.
    if is_globally_disabled():
        return None
    if current_mode() == "private":
        return None

    if not transcript_path:
        return None

    try:
        signal = detect_procedure(Path(transcript_path), min_tool_burst=min_tool_burst)
    except Exception:
        return None

    if signal is None:
        return None

    slug = slugify(signal.task)
    if not slug:
        return None

    # Record this sighting and apply repeat gate.
    try:
        _record_seen(slug)
        seen = _seen_count(slug)
    except Exception:
        seen = 1

    if seen < repeat_gate:
        return None

    try:
        path = _write_procedure_proposal(signal, slug, session_id=session_id)
    except Exception:
        return None

    return ProcedureProposal(
        slug=slug,
        path=path,
        created_at=now_iso(),
        task=signal.task,
        confidence=signal.confidence,
    )

"""Optional Haiku judge for ambiguous corrections.

Default OFF. Activates only when:
1. ``observe.use_haiku_judge: true`` in config
2. ``ANTHROPIC_API_KEY`` is in the environment
3. Daily call budget hasn't been hit (default 100/day)

Cost guardrails:
- Per-call: ~500 tokens in, ~50 tokens out → ~$0.001 per call
- Daily cap: 100 calls = ~$0.10/day = ~$3/month upper bound
- The regex prefilter rejects ~90% of PostToolUse events before this fires

The judge sees only the trigger turn + 2 surrounding turns of context, never
the whole transcript. This keeps prompt size predictable and limits leakage.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date

from shed.config import state_dir

JUDGE_BUDGET_FILE = "haiku-judge-budget.json"
DEFAULT_DAILY_CAP = 100


@dataclass
class JudgeVerdict:
    is_correction: bool
    confidence: float  # 0..1
    rationale: str
    used_budget: bool  # True if a real API call was made


def _budget_path():
    return state_dir() / JUDGE_BUDGET_FILE


def _today() -> str:
    return date.today().isoformat()


def get_remaining_budget(daily_cap: int = DEFAULT_DAILY_CAP) -> int:
    p = _budget_path()
    if not p.exists():
        return daily_cap
    try:
        data = json.loads(p.read_text())
        if data.get("date") != _today():
            return daily_cap
        return max(0, daily_cap - int(data.get("used", 0)))
    except Exception:
        return daily_cap


def _decrement_budget(daily_cap: int = DEFAULT_DAILY_CAP) -> int:
    """Atomically increment usage. Returns remaining budget after decrement."""
    p = _budget_path()
    state_dir().mkdir(parents=True, exist_ok=True)
    today = _today()
    used = 0
    if p.exists():
        try:
            data = json.loads(p.read_text())
            if data.get("date") == today:
                used = int(data.get("used", 0))
        except Exception:
            pass
    used += 1
    p.write_text(json.dumps({"date": today, "used": used}))
    return max(0, daily_cap - used)


def judge(
    text: str,
    model: str = "claude-haiku-4-5",
    daily_cap: int = DEFAULT_DAILY_CAP,
) -> JudgeVerdict:
    """Ask Haiku whether ``text`` represents a durable correction.

    Returns a verdict. If the budget is exhausted or the API key is missing,
    returns a non-correction verdict with ``used_budget=False``.
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return JudgeVerdict(False, 0.0, "no API key", used_budget=False)
    if get_remaining_budget(daily_cap) <= 0:
        return JudgeVerdict(False, 0.0, "daily budget exhausted", used_budget=False)

    try:
        # Lazy import — anthropic SDK is optional
        from anthropic import Anthropic
    except ImportError:
        return JudgeVerdict(False, 0.0, "anthropic SDK not installed", used_budget=False)

    prompt = (
        "You are evaluating whether a user message represents a *durable correction* "
        "of an AI assistant's behavior — i.e., something the user wants the assistant "
        "to do differently in future sessions, not just this one.\n\n"
        f"User message:\n```\n{text[:1500]}\n```\n\n"
        "Reply with JSON only: "
        '{"is_correction": true/false, "confidence": 0.0-1.0, "rationale": "..."}\n\n'
        'A "durable correction" is a stable preference (e.g., "always use uv, never pip"). '
        "Off-the-cuff annoyance, one-off requests, or generic feedback is NOT a durable correction."
    )

    try:
        client = Anthropic()
        _decrement_budget(daily_cap)
        msg = client.messages.create(
            model=model,
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
            timeout=10.0,
        )
        body = msg.content[0].text if msg.content else "{}"
        # Find JSON in response.
        start = body.find("{")
        end = body.rfind("}")
        if start >= 0 and end > start:
            data = json.loads(body[start : end + 1])
            return JudgeVerdict(
                is_correction=bool(data.get("is_correction", False)),
                confidence=float(data.get("confidence", 0.5)),
                rationale=str(data.get("rationale", ""))[:200],
                used_budget=True,
            )
        return JudgeVerdict(False, 0.0, "judge response unparseable", used_budget=True)
    except Exception as e:
        return JudgeVerdict(False, 0.0, f"judge error: {e}", used_budget=False)

"""Regex-based category classifier (v0.1 — no model calls).

Maps a snippet of text (correction signal, lesson candidate) to one of the
allowlisted categories. If nothing matches we return ``None`` and the caller
drops the proposal — that is the privacy default.
"""

from __future__ import annotations

import re

CATEGORY_PATTERNS: dict[str, list[re.Pattern[str]]] = {
    "coding-preferences": [
        re.compile(r"\b(prefer|always|never|don'?t use)\b", re.I),
        re.compile(r"\b(tabs?|spaces?|2-space|4-space|snake_case|camelCase)\b", re.I),
        re.compile(r"\b(typed|type hints?|mypy|strict|lint(ing)?|formatter|format)\b", re.I),
    ],
    "tool-choices": [
        # Specific named CLIs are a strong signal.
        re.compile(
            r"\b(uv|poetry|pip|npm|pnpm|yarn|bun|cargo|rg|fzf|jq|gh)\b",
            re.I,
        ),
        re.compile(r"\b(switch to|stop using|drop)\b", re.I),
        re.compile(r"\b(use|prefer)\b.{0,40}\b(instead|over|rather than)\b", re.I),
    ],
    "workflow": [
        re.compile(r"\b(branch|commit|squash|rebase|PR|pull request|merge|push)\b", re.I),
        re.compile(r"\b(test|run tests|pytest|vitest|coverage|lint|ruff|format)\b", re.I),
        re.compile(r"\b(don'?t (push|commit|merge)|always (test|lint|format))\b", re.I),
    ],
    "project-facts": [
        re.compile(r"\b(repo|project|stack|deploy|hosted on|lives at|located at)\b", re.I),
        re.compile(r"\b(uses|built with|written in|runs on)\b", re.I),
    ],
}


def classify(text: str, allowlist: list[str]) -> str | None:
    """Return the highest-scoring allowlisted category, or None.

    A category "scores" by the number of pattern hits. Ties broken by
    allowlist order (i.e. user's preferred categories win).
    """
    if not text or not allowlist:
        return None

    scores: dict[str, int] = {}
    for cat in allowlist:
        patterns = CATEGORY_PATTERNS.get(cat, [])
        hits = sum(1 for p in patterns if p.search(text))
        if hits:
            scores[cat] = hits

    if not scores:
        return None

    best = max(scores.items(), key=lambda kv: (kv[1], -allowlist.index(kv[0])))
    return best[0]

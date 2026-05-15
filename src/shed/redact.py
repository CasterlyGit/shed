"""Deterministic PII redactor.

Belt-and-suspenders alongside the category allowlist. Drops any *line* that
matches a known PII pattern (emails outside the whitelist, phone numbers,
SSNs, credit cards, anything looking like an API key/token).

Pure regex, no model calls. Tested.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

# --- patterns ---------------------------------------------------------------

EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b")

# US-style + international, allowing parens and hyphens, 10–15 digits total.
PHONE_RE = re.compile(
    r"(?<!\w)(?:\+?\d{1,3}[\s.-]?)?(?:\(\d{3}\)|\d{3})[\s.-]?\d{3}[\s.-]?\d{4}(?!\w)"
)

SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")

# Major card networks; rough — Luhn would be heavier than we need here.
CC_RE = re.compile(r"\b(?:\d[ -]*?){13,19}\b")

# API-key-ish: long opaque tokens. Conservative to avoid eating normal hashes.
API_KEY_RE = re.compile(
    r"""(?ix)
    \b(
        sk-[A-Za-z0-9_\-]{20,}              |   # OpenAI/Anthropic style
        xox[baprs]-[A-Za-z0-9-]{10,}        |   # Slack
        ghp_[A-Za-z0-9]{20,}                |   # GitHub PAT
        github_pat_[A-Za-z0-9_]{20,}        |   # GitHub fine-grained PAT
        gho_[A-Za-z0-9]{20,}                |   # GitHub OAuth
        AIza[0-9A-Za-z\-_]{30,}             |   # Google API key
        AKIA[0-9A-Z]{14,}                   |   # AWS access key
        [A-Za-z0-9/+=]{40,}                     # generic base64-ish secret
    )\b
    """
)

# Lines like `password = "..."` / `api_key: ...` regardless of value shape.
SECRET_ASSIGN_RE = re.compile(
    r"(?i)\b(api[_\-]?key|secret|token|password|passwd|access[_\-]?key|auth)[^\n]{0,3}[:=]"
)

DEFAULT_EMAIL_WHITELIST = {"anthropic.com", "gmail.com"}


# --- core -------------------------------------------------------------------


def line_has_pii(line: str, email_whitelist: Iterable[str] = DEFAULT_EMAIL_WHITELIST) -> bool:
    """True if the line should be dropped from a write.

    Whitelisted email domains are exempt — Tarun explicitly works with these.
    """
    whitelist = {d.lower().lstrip("@") for d in email_whitelist}

    # Emails — drop if any email on the line is NOT whitelisted.
    emails = EMAIL_RE.findall(line)
    for e in emails:
        domain = e.split("@", 1)[1].lower()
        if domain not in whitelist:
            return True

    if SSN_RE.search(line):
        return True
    if PHONE_RE.search(line):
        return True
    if API_KEY_RE.search(line):
        return True
    if SECRET_ASSIGN_RE.search(line):
        return True

    # Credit cards — Luhn-check the candidate to avoid eating UUID-like things.
    for m in CC_RE.finditer(line):
        digits = re.sub(r"\D", "", m.group(0))
        if 13 <= len(digits) <= 19 and _luhn_ok(digits):
            return True

    return False


def _luhn_ok(digits: str) -> bool:
    total = 0
    parity = len(digits) % 2
    for i, ch in enumerate(digits):
        d = int(ch)
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def redact(text: str, email_whitelist: Iterable[str] | None = None) -> str:
    """Drop every line that matches a PII pattern.

    Returns the cleaned text. If a line is dropped a ``[shed: redacted]``
    marker takes its place so block structure is preserved.
    """
    wl = email_whitelist if email_whitelist is not None else DEFAULT_EMAIL_WHITELIST
    out: list[str] = []
    for line in text.splitlines():
        if line_has_pii(line, wl):
            out.append("[shed: redacted]")
        else:
            out.append(line)
    # Preserve trailing newline if input had one.
    suffix = "\n" if text.endswith("\n") else ""
    return "\n".join(out) + suffix

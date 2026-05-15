"""Memory file CRUD with YAML frontmatter.

A "memory" is a single ``.md`` file under one of the configured roots. The
frontmatter holds shed-managed metadata (slug, category, pinned, citation
counters); the body is whatever the user wrote.
"""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import frontmatter

from shed.config import archive_dir, memory_roots

SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    s = SLUG_RE.sub("-", text.lower()).strip("-")
    return s[:60] or "untitled"


def stable_id(path: Path) -> str:
    """Stable short id derived from absolute path. Survives renames poorly,
    survives filesystem moves perfectly — that's the right tradeoff for v0.1.
    """
    return hashlib.sha1(str(path.resolve()).encode()).hexdigest()[:10]


@dataclass
class Memory:
    path: Path
    slug: str
    title: str
    body: str
    category: str | None = None
    pinned: bool = False
    created_at: str = ""
    last_cited_at: str | None = None
    cite_count: int = 0
    extra: dict = field(default_factory=dict)

    @property
    def id(self) -> str:
        return stable_id(self.path)

    @property
    def text_for_embedding(self) -> str:
        return f"{self.title}\n\n{self.body}".strip()

    def to_post(self) -> frontmatter.Post:
        meta = {
            "slug": self.slug,
            "title": self.title,
            "category": self.category,
            "pinned": self.pinned,
            "created_at": self.created_at,
            "last_cited_at": self.last_cited_at,
            "cite_count": self.cite_count,
        }
        meta.update(self.extra)
        return frontmatter.Post(self.body, **{k: v for k, v in meta.items() if v is not None})


def _title_from_body(body: str, fallback: str) -> str:
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("#"):
            return line.lstrip("#").strip()
        if line:
            return line[:80]
    return fallback


def load(path: Path) -> Memory:
    post = frontmatter.load(path)
    body = post.content
    meta = post.metadata
    title = meta.get("title") or _title_from_body(body, path.stem)
    slug = meta.get("slug") or slugify(path.stem)
    return Memory(
        path=path,
        slug=slug,
        title=title,
        body=body,
        category=meta.get("category"),
        pinned=bool(meta.get("pinned", False)),
        created_at=_as_iso(meta.get("created_at")) or _isoformat(path.stat().st_ctime),
        last_cited_at=_as_iso(meta.get("last_cited_at")),
        cite_count=int(meta.get("cite_count", 0) or 0),
        extra={
            k: v
            for k, v in meta.items()
            if k
            not in {
                "slug",
                "title",
                "category",
                "pinned",
                "created_at",
                "last_cited_at",
                "cite_count",
            }
        },
    )


def _as_iso(value) -> str | None:
    """YAML may parse timestamps into datetime/date objects — coerce back."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.isoformat(timespec="seconds")
    # date or anything stringable
    return str(value)


def save(mem: Memory) -> None:
    mem.path.parent.mkdir(parents=True, exist_ok=True)
    text = frontmatter.dumps(mem.to_post())
    if not text.endswith("\n"):
        text += "\n"
    mem.path.write_text(text)


def discover(roots: Iterable[Path] | None = None) -> list[Memory]:
    """Walk every memory root and return loaded Memory objects."""
    roots = list(roots) if roots is not None else memory_roots()
    out: list[Memory] = []
    seen: set[Path] = set()
    for root in roots:
        if not root.exists():
            continue
        if root.is_file():
            candidates: Iterable[Path] = [root]
        else:
            candidates = root.rglob("*.md")
        for p in candidates:
            rp = p.resolve()
            if rp in seen:
                continue
            seen.add(rp)
            try:
                out.append(load(p))
            except Exception:
                # Malformed frontmatter shouldn't crash injection.
                continue
    return out


def record_citation(mem: Memory, when: float | None = None) -> None:
    when = when if when is not None else time.time()
    mem.cite_count += 1
    mem.last_cited_at = _isoformat(when)
    save(mem)


def archive(mem: Memory) -> Path:
    """Move a memory into the archive dir, preserving slug."""
    target = archive_dir() / f"{mem.id}-{mem.slug}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    save(mem)  # ensure latest metadata is on disk before move
    if mem.path.exists():
        mem.path.replace(target)
    mem.path = target
    return target


def _isoformat(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds")


def now_iso() -> str:
    return _isoformat(time.time())

"""Morning brief — walk through pending proposals.

Single-key actions (j/k navigate, y accept, n reject, e edit, s skip, p pin).
Implemented with Rich for the rendering + ``readchar`` if available, otherwise
falls back to ``input()`` + single letters. No textual dep.
"""

from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from shed.config import (
    changelog_path,
    current_mode,
    is_globally_disabled,
    memory_roots,
    proposals_dir,
    shed_home,
)
from shed.memory import Memory, now_iso, save, slugify

ACTIONS = "y/n/e/s/p/q"


@dataclass
class PendingProposal:
    path: Path
    title: str
    category: str | None
    body: str
    kind: str = "lesson"  # "lesson" | "permit"
    pattern: str | None = None  # for permits

    @classmethod
    def from_path(cls, path: Path) -> PendingProposal:
        text = path.read_text()
        kind = _extract_yaml(text, "kind") or ("permit" if path.name.startswith("permit-") else "lesson")
        if kind == "permit":
            pattern = _extract_yaml(text, "pattern")
            count = _extract_yaml(text, "count") or "?"
            title = f"permit: {pattern} (×{count})" if pattern else path.stem
            return cls(
                path=path, title=title, category="permission",
                body=text, kind="permit", pattern=pattern,
            )
        category = _extract(text, "category:")
        title = _first_heading(text) or path.stem
        return cls(path=path, title=title, category=category, body=text)


def list_pending() -> list[PendingProposal]:
    if not proposals_dir().exists():
        return []
    out: list[PendingProposal] = []
    for p in sorted(proposals_dir().glob("*.md")):
        try:
            out.append(PendingProposal.from_path(p))
        except Exception:
            continue
    return out


def render_brief(console: Console | None = None) -> str:
    """Render the brief as a string (used by both the CLI command and the
    SessionStart hook output). Returns the text we want Claude to see — the
    interactive walk-through is a separate function below.
    """
    console = console or Console(record=True, width=100)
    pending = list_pending()
    mode = current_mode()
    disabled = is_globally_disabled()

    header_bits = [f"shed mode: [bold]{mode}[/bold]"]
    if disabled:
        header_bits.append("[red]GLOBALLY DISABLED[/red]")
    header_bits.append(f"pending: {len(pending)}")
    console.print(Panel(" • ".join(header_bits), title="shed", border_style="magenta"))

    if not pending:
        console.print("[dim]No pending proposals. You're caught up.[/dim]")
        return console.export_text()

    permits = [p for p in pending if p.kind == "permit"]
    lessons = [p for p in pending if p.kind != "permit"]

    if permits:
        ptable = Table(title="Permission proposals", show_lines=False, border_style="cyan")
        ptable.add_column("#", style="cyan", width=3)
        ptable.add_column("pattern")
        ptable.add_column("file", style="dim")
        for i, pp in enumerate(permits[:10], 1):
            ptable.add_row(str(i), pp.pattern or pp.title[:60], pp.path.name)
        console.print(ptable)

    if lessons:
        table = Table(title="Lesson proposals", show_lines=False)
        table.add_column("#", style="cyan", width=3)
        table.add_column("category", style="yellow")
        table.add_column("title")
        table.add_column("file", style="dim")
        for i, pp in enumerate(lessons[:10], 1):
            table.add_row(str(i), pp.category or "?", pp.title[:60], pp.path.name)
        console.print(table)

    console.print(
        f"\nRun [bold]shed brief[/bold] to walk through them ({ACTIONS}).",
    )
    return console.export_text()


def walk_brief(console: Console | None = None) -> None:
    """Interactive walk. Single-key when readchar is available, else input()."""
    console = console or Console()
    pending = list_pending()
    if not pending:
        console.print("[dim]No pending proposals.[/dim]")
        return

    getch = _make_getch()

    i = 0
    while 0 <= i < len(pending):
        pp = pending[i]
        console.clear()
        console.print(
            Panel(
                pp.body,
                title=f"[{i + 1}/{len(pending)}] {pp.title}",
                subtitle=f"category: {pp.category or '?'}  •  {pp.path.name}",
                border_style="magenta",
            )
        )
        console.print(
            "[dim]actions:[/dim] [bold]y[/bold]es accept  "
            "[bold]n[/bold]o reject  [bold]e[/bold]dit  "
            "[bold]s[/bold]kip  [bold]p[/bold]in  [bold]q[/bold]uit"
        )
        ch = getch().lower().strip()
        if ch == "q":
            return
        if ch == "y":
            _accept(pp, console, pin=False)
            i += 1
        elif ch == "p":
            _accept(pp, console, pin=True)
            i += 1
        elif ch == "n":
            _reject(pp, console)
            i += 1
        elif ch == "e":
            _edit(pp, console)
            # re-render same item after edit
        elif ch == "s":
            i += 1
        else:
            console.print(f"[red]Unknown:[/red] {ch!r}")


# ---------------------------------------------------------------------------


def _accept(pp: PendingProposal, console: Console, pin: bool) -> None:
    _log_decision(pp, "accept")
    if pp.kind == "permit":
        from shed.permit import apply_proposal

        ok, msg = apply_proposal(pp.path)
        if ok:
            pp.path.unlink(missing_ok=True)
            _changelog(f"permit accepted: {pp.pattern} ({msg})")
            console.print(f"[green]permit applied[/green] {msg}")
        else:
            console.print(f"[red]permit failed:[/red] {msg}")
        return

    target_root = _accept_root()
    target_root.mkdir(parents=True, exist_ok=True)
    slug = slugify(pp.title)
    target = target_root / f"{slug}.md"
    # Strip the proposal frontmatter-ish lines, keep the body.
    body = _strip_proposal_meta(pp.body)
    mem = Memory(
        path=target,
        slug=slug,
        title=pp.title,
        body=body,
        category=pp.category,
        pinned=pin,
        created_at=now_iso(),
    )
    save(mem)
    pp.path.unlink(missing_ok=True)
    _changelog(f"accepted{' (pinned)' if pin else ''}: {slug} -> {target}")
    console.print(f"[green]accepted[/green] -> {target}")


def _reject(pp: PendingProposal, console: Console) -> None:
    _log_decision(pp, "reject")
    pp.path.unlink(missing_ok=True)
    _changelog(f"rejected: {pp.path.name}")
    console.print(f"[red]rejected[/red] {pp.path.name}")


def _log_decision(pp: PendingProposal, decision: str) -> None:
    """Feed the L5 self-tuning loop."""
    try:
        from shed.thresholds import log_feedback

        confidence = _extract_confidence(pp.body) or 0.5
        kind = pp.kind or "lesson"
        log_feedback(kind, confidence, decision)
    except Exception:
        pass


def _extract_confidence(body: str) -> float | None:
    for line in body.splitlines():
        if line.startswith("confidence:"):
            try:
                return float(line.split(":", 1)[1].strip())
            except Exception:
                return None
    return None


def _edit(pp: PendingProposal, console: Console) -> None:
    import os
    import subprocess

    editor = os.environ.get("EDITOR", "vi")
    subprocess.call([editor, str(pp.path)])
    pp.body = pp.path.read_text()


def _accept_root() -> Path:
    """Where accepted lessons land. First memory root, or ~/.shed/memory if
    none exist yet (still git-tracked under shed_home)."""
    roots = memory_roots()
    if roots:
        return roots[0]
    fallback = shed_home() / "memory"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def _changelog(msg: str) -> None:
    p = changelog_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    line = f"- {now_iso()} — {msg}\n"
    if p.exists():
        existing = p.read_text()
        p.write_text(existing + line)
    else:
        p.write_text("# shed changelog\n\n" + line)


def _extract(text: str, key: str) -> str | None:
    for line in text.splitlines():
        if line.startswith(key):
            return line[len(key):].strip()
    return None


def _extract_yaml(text: str, key: str) -> str | None:
    """Read a top-level YAML frontmatter key. Tolerant of files without
    frontmatter."""
    in_fm = False
    for line in text.splitlines():
        s = line.strip()
        if s == "---":
            if not in_fm:
                in_fm = True
                continue
            return None  # closed without finding key
        if in_fm and ":" in line:
            k, _, v = line.partition(":")
            if k.strip() == key:
                return v.strip()
    return None


def _first_heading(text: str) -> str | None:
    for line in text.splitlines():
        if line.startswith("##"):
            return line.lstrip("#").strip()
    return None


def _strip_proposal_meta(body: str) -> str:
    keep: list[str] = []
    skip = False
    for line in body.splitlines():
        if line.startswith("<!-- shed proposal"):
            skip = True
            continue
        if skip and line.strip().startswith("##"):
            skip = False
        if skip:
            continue
        keep.append(line)
    out = "\n".join(keep).strip()
    return out + "\n" if out else ""


def _make_getch():
    """Single-keypress reader. Falls back to input() if readchar/tty unavail."""
    if not sys.stdin.isatty() or shutil.which("stty") is None:
        return lambda: (input("? ") or "s")[:1]
    try:
        import termios
        import tty

        def getch() -> str:
            fd = sys.stdin.fileno()
            old = termios.tcgetattr(fd)
            try:
                tty.setraw(fd)
                ch = sys.stdin.read(1)
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
            return ch

        return getch
    except Exception:
        return lambda: (input("? ") or "s")[:1]

"""shed CLI — typer entrypoint."""

from __future__ import annotations

import json
import subprocess
import sys

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from shed import __version__
from shed.brief import render_brief, walk_brief
from shed.config import (
    current_mode,
    is_globally_disabled,
    load_config,
    set_mode,
    shed_home,
    state_dir,
    write_config,
)
from shed.doctor import run_doctor
from shed.evolve import run_evolve
from shed.hooks.install import install as install_hooks
from shed.inject import inject_for_prompt
from shed.memory import discover, save

app = typer.Typer(
    add_completion=True,
    no_args_is_help=False,
    help="shed — Claude Code that learns you.",
    invoke_without_command=True,
)
console = Console()


@app.callback()
def _root(
    ctx: typer.Context,
    version: bool = typer.Option(False, "--version", "-V", help="print version and exit"),
) -> None:
    if version:
        console.print(__version__)
        raise typer.Exit()
    if ctx.invoked_subcommand is None:
        console.print(ctx.get_help())
        raise typer.Exit()


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


@app.command()
def init(
    force: bool = typer.Option(False, "--force", help="overwrite config + allowlist"),
    download_model: bool = typer.Option(
        True, "--download-model/--no-download-model", help="pre-download ONNX embedder model"
    ),
) -> None:
    """Install hooks, create ~/.shed/, build initial embedding index."""
    if download_model:
        from shed.embeddings import download_onnx_model, onnx_model_files_present

        if not onnx_model_files_present():
            console.print("[dim]downloading ONNX embedder model (~33MB, one-time)…[/dim]")
            try:
                download_onnx_model(progress=True)
                console.print("[green]model downloaded[/green]")
            except Exception as e:
                console.print(f"[yellow]model download failed: {e}[/yellow]")
                console.print("[yellow]continuing with hash fallback — re-run with internet later[/yellow]")
    rep = install_hooks(force=force)
    console.print(
        Panel(
            f"[bold]shed installed.[/bold]\n\n"
            f"home: {rep.shed_home}\n"
            f"hooks: {len(rep.hooks_written)} written\n"
            f"settings: {rep.settings_patched}\n"
            f"git: {'initialized' if rep.git_initialized else 'already present'}\n"
            f"index: {rep.indexed} memories embedded\n\n"
            "[dim]Tip: open a fresh Claude Code session to pick up the hooks.[/dim]",
            border_style="magenta",
            title="shed init",
        )
    )


# ---------------------------------------------------------------------------
# why
# ---------------------------------------------------------------------------


@app.command()
def why(
    prompt: str = typer.Argument(None, help="prompt to explain (default: last)"),
    top_k: int = typer.Option(5, "--top-k", "-k", help="how many candidates to show"),
) -> None:
    """Explain which memories would be injected for a given prompt."""
    if prompt is None:
        # Default: show what was last picked.
        last = _last_injection()
        if last is None:
            console.print("[red]no prompt given and no prior injection logged[/red]")
            raise typer.Exit(1)
        console.print(
            Panel(last.get("prompt_preview", ""), title="last prompt", border_style="dim")
        )
        table = Table(title="last injection")
        table.add_column("rank")
        table.add_column("slug")
        table.add_column("score")
        table.add_column("chosen", style="green")
        for i, c in enumerate(last.get("candidates", []), 1):
            table.add_row(str(i), c["slug"], f"{c['score']:.3f}", "yes" if c["chosen"] else "")
        console.print(table)
        return

    res = inject_for_prompt(prompt, log=False)
    console.print(Panel(prompt, title="prompt", border_style="dim"))
    if res.skipped_reason:
        console.print(f"[yellow]skipped:[/yellow] {res.skipped_reason}")
        return
    table = Table(title=f"top {top_k} candidates  •  {res.elapsed_ms:.1f}ms")
    table.add_column("rank")
    table.add_column("slug")
    table.add_column("score")
    table.add_column("chosen", style="green")
    for i, c in enumerate(res.candidates[:top_k], 1):
        table.add_row(str(i), c.slug, f"{c.score:.3f}", "yes" if c.chosen else "")
    console.print(table)
    if res.block:
        console.print(Panel(res.block, title="<shed-context>", border_style="magenta"))


# ---------------------------------------------------------------------------
# dash
# ---------------------------------------------------------------------------


@app.command()
def dash() -> None:
    """Tiny TUI: hot/warm/cold memories, recent injections, citation hit-rate."""
    mems = discover()
    if not mems:
        console.print("[dim]no memories yet[/dim]")
        return

    by_cite = sorted(mems, key=lambda m: m.cite_count, reverse=True)
    hot = [m for m in by_cite if m.cite_count >= 3]
    warm = [m for m in by_cite if 1 <= m.cite_count < 3]
    cold = [m for m in by_cite if m.cite_count == 0]

    table = Table(title=f"shed dash — {len(mems)} memories")
    table.add_column("bucket")
    table.add_column("count")
    table.add_column("slugs", style="dim")
    for label, bucket in [("hot", hot), ("warm", warm), ("cold", cold)]:
        slugs = ", ".join(m.slug for m in bucket[:8])
        table.add_row(label, str(len(bucket)), slugs)
    console.print(table)

    # Recent injections + hit rate
    log = state_dir() / "injections.jsonl"
    if log.exists():
        rows: list[dict] = []
        for line in log.read_text().splitlines()[-200:]:
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
        hits = sum(1 for r in rows if r.get("chosen"))
        rate = (hits / len(rows) * 100) if rows else 0.0
        console.print(f"\nrecent injections: [bold]{len(rows)}[/bold]   hit-rate: [bold]{rate:.0f}%[/bold]")

        recent = Table(title="last 10 prompts")
        recent.add_column("prompt", style="dim")
        recent.add_column("chosen")
        for r in rows[-10:]:
            slugs = ", ".join(c["slug"] for c in r.get("chosen", [])) or "-"
            recent.add_row(r.get("prompt_preview", "")[:60], slugs)
        console.print(recent)


# ---------------------------------------------------------------------------
# brief
# ---------------------------------------------------------------------------


@app.command()
def brief(
    walk: bool = typer.Option(True, "--walk/--no-walk", help="interactive walk"),
) -> None:
    """Morning brief — pending proposals."""
    if walk and sys.stdin.isatty():
        walk_brief(console)
    else:
        console.print(render_brief(console))


# ---------------------------------------------------------------------------
# evolve
# ---------------------------------------------------------------------------


@app.command()
def evolve() -> None:
    """Run garbage collection: archive cold, propose merges, list hot."""
    rep = run_evolve()
    console.print(
        Panel(
            f"archived: {len(rep.archived)}\n"
            f"duplicates: {len(rep.duplicates)}\n"
            f"hot: {', '.join(rep.hot) or '-'}\n"
            f"proposals: {len(rep.proposals_written)}",
            title="shed evolve",
            border_style="magenta",
        )
    )


# ---------------------------------------------------------------------------
# inject (used by hook + manual)
# ---------------------------------------------------------------------------


@app.command()
def inject(
    prompt: str = typer.Argument(None, help="prompt to inject (default: read stdin)"),
) -> None:
    """Print the <shed-context> block for a prompt. Used by the hook."""
    if prompt is None:
        prompt = sys.stdin.read()
    res = inject_for_prompt(prompt)
    if res.block:
        sys.stdout.write(res.block)


# ---------------------------------------------------------------------------
# observe (used by hook + manual)
# ---------------------------------------------------------------------------


@app.command()
def observe(
    text: str = typer.Argument(None, help="text to scan (default: read stdin)"),
) -> None:
    """Scan text for a correction; queue a proposal if found."""
    from shed.observe import observe_text

    if text is None:
        text = sys.stdin.read()
    p = observe_text(text)
    if p:
        console.print(f"[green]queued[/green] {p.path}")
    else:
        console.print("[dim]no correction detected[/dim]")


# ---------------------------------------------------------------------------
# mute / pin
# ---------------------------------------------------------------------------


@app.command()
def mute(
    slug: str = typer.Argument(...),
    turns: int = typer.Option(0, "--turns", help="mute for N turns (0 = session)"),
    session: bool = typer.Option(False, "--session"),
) -> None:
    """Temporarily exclude a memory from injection."""
    target = shed_home() / "muted.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        data = json.loads(target.read_text())
    else:
        data = {}
    data[slug] = {"session": session or turns == 0, "turns": turns}
    target.write_text(json.dumps(data, indent=2))
    console.print(f"muted [bold]{slug}[/bold] ({'session' if session or turns == 0 else f'{turns} turns'})")


@app.command()
def pin(slug: str = typer.Argument(...)) -> None:
    """Pin a memory: never archive, always available."""
    matches = [m for m in discover() if m.slug == slug]
    if not matches:
        console.print(f"[red]no memory with slug {slug!r}[/red]")
        raise typer.Exit(1)
    for m in matches:
        m.pinned = True
        save(m)
        console.print(f"[green]pinned[/green] {m.path}")


# ---------------------------------------------------------------------------
# mode
# ---------------------------------------------------------------------------


@app.command()
def mode(
    value: str = typer.Argument(None, help="auto | explore | private (omit to show)"),
) -> None:
    """Show or set session mode."""
    if value is None:
        console.print(f"current mode: [bold]{current_mode()}[/bold]")
        if is_globally_disabled():
            console.print("[red]global kill switch is ON[/red]")
        return
    if value not in ("auto", "explore", "private"):
        console.print("[red]mode must be one of: auto, explore, private[/red]")
        raise typer.Exit(1)
    set_mode(value)  # type: ignore[arg-type]
    console.print(f"mode set: [bold]{value}[/bold]")


# ---------------------------------------------------------------------------
# sync
# ---------------------------------------------------------------------------


sync_app = typer.Typer(help="opt-in sync of ~/.shed/ to a private GitHub repo")
app.add_typer(sync_app, name="sync")


@sync_app.command("enable")
def sync_enable(
    remote: str = typer.Option(None, help="git remote URL"),
) -> None:
    cfg = load_config()
    if remote is None:
        remote = cfg.sync.remote
    cfg.sync.enabled = True
    cfg.sync.remote = remote
    write_config(cfg)
    sh = shed_home()
    if not (sh / ".git").exists():
        console.print("[red]~/.shed is not a git repo. Run `shed init` first.[/red]")
        raise typer.Exit(1)
    try:
        subprocess.run(
            ["git", "-C", str(sh), "remote", "add", "origin", remote],
            check=False,
        )
    except FileNotFoundError as e:
        console.print("[red]git not found[/red]")
        raise typer.Exit(1) from e
    console.print(f"[green]sync enabled[/green] -> {remote}")
    console.print("[dim]push with: git -C ~/.shed push -u origin main[/dim]")


@sync_app.command("disable")
def sync_disable() -> None:
    cfg = load_config()
    cfg.sync.enabled = False
    write_config(cfg)
    console.print("[yellow]sync disabled[/yellow] (remote left intact)")


@sync_app.command("status")
def sync_status() -> None:
    cfg = load_config()
    console.print(f"enabled: [bold]{cfg.sync.enabled}[/bold]")
    console.print(f"remote: {cfg.sync.remote}")


# ---------------------------------------------------------------------------
# statusline
# ---------------------------------------------------------------------------


statusline_app = typer.Typer(help="claude code statusline indicator")
app.add_typer(statusline_app, name="statusline")


@statusline_app.command("show")
def statusline_show() -> None:
    """Print the rendered statusline (live, not cached)."""
    from shed.statusline import render

    console.print(render())


@statusline_app.command("install")
def statusline_install() -> None:
    """Wire shed into ~/.claude/settings.json's statusLine field."""
    from shed.config import claude_home, load_config, write_config
    from shed.statusline import install_to_claude_settings, write_cache

    cfg = load_config()
    cfg.statusline.enabled = True
    write_config(cfg)
    write_cache()
    settings = claude_home() / "settings.json"
    changed = install_to_claude_settings(settings)
    if changed:
        console.print(f"[green]statusline installed[/green] → {settings}")
    else:
        console.print("[dim]statusline already wired[/dim]")


@statusline_app.command("uninstall")
def statusline_uninstall() -> None:
    """Remove shed from settings.json statusLine; restores backup if present."""
    from shed.config import claude_home, load_config, write_config
    from shed.statusline import uninstall_from_claude_settings

    cfg = load_config()
    cfg.statusline.enabled = False
    write_config(cfg)
    settings = claude_home() / "settings.json"
    changed = uninstall_from_claude_settings(settings)
    if changed:
        console.print("[yellow]statusline uninstalled[/yellow]")
    else:
        console.print("[dim]nothing to uninstall[/dim]")


@statusline_app.command("refresh")
def statusline_refresh() -> None:
    """Force-refresh the cached statusline string."""
    from shed.statusline import write_cache

    p = write_cache()
    console.print(f"[green]refreshed[/green] {p}")


# ---------------------------------------------------------------------------
# thresholds (L5)
# ---------------------------------------------------------------------------


thresholds_app = typer.Typer(help="self-tuning per-kind confidence thresholds")
app.add_typer(thresholds_app, name="thresholds")


@thresholds_app.command("show")
def thresholds_show() -> None:
    """Current per-kind tuned thresholds."""
    from shed.thresholds import load_thresholds

    t = load_thresholds()
    if not t:
        console.print("[dim]no tuned thresholds yet — using config defaults[/dim]")
        return
    table = Table(title="tuned thresholds (per-kind)")
    table.add_column("kind")
    table.add_column("threshold")
    for k, v in sorted(t.items()):
        table.add_row(k, f"{v:.2f}")
    console.print(table)


@thresholds_app.command("tune")
def thresholds_tune() -> None:
    """Recompute thresholds from accept/reject feedback log."""
    from shed.thresholds import tune_all

    reports = tune_all()
    if not reports:
        console.print("[dim]not enough feedback to tune (need 5+ decisions per kind)[/dim]")
        return
    table = Table(title="tuning reports")
    table.add_column("kind")
    table.add_column("new_threshold")
    table.add_column("accept_rate", style="green")
    table.add_column("n_decisions", style="dim")
    for r in reports:
        table.add_row(r.kind, f"{r.threshold:.2f}", f"{r.accept_rate:.2f}", str(r.accepts + r.rejects))
    console.print(table)


# ---------------------------------------------------------------------------
# quality (L1)
# ---------------------------------------------------------------------------


@app.command()
def quality(top: int = typer.Option(20, "--top", help="show top N memories by injection score")) -> None:
    """Show per-memory injection quality scores (L1 closed-loop)."""
    from shed.memory import discover
    from shed.quality import compute_scores

    scores = compute_scores()
    if not scores:
        console.print("[dim]no quality events logged yet — use Claude Code normally and shed will learn[/dim]")
        return
    by_id = {m.id: m for m in discover()}
    rows = sorted(scores.values(), key=lambda q: q.injection_score, reverse=True)
    table = Table(title=f"injection quality (top {top})")
    table.add_column("score", style="cyan")
    table.add_column("slug")
    table.add_column("injected", style="dim")
    table.add_column("cited", style="green")
    for q in rows[:top]:
        slug = by_id[q.memory_id].slug if q.memory_id in by_id else q.memory_id[:10]
        table.add_row(
            f"{q.injection_score:.2f}",
            slug,
            f"{q.injected_recent:.1f}",
            f"{q.cited_recent:.1f}",
        )
    console.print(table)


# ---------------------------------------------------------------------------
# permit
# ---------------------------------------------------------------------------


permit_app = typer.Typer(help="learn permission-prompt patterns (L3 loop)")
app.add_typer(permit_app, name="permit")


@permit_app.command("list")
def permit_list(top: int = typer.Option(20, "--top", help="how many patterns to show")) -> None:
    """Top permit patterns by approval count."""
    from shed.permit import is_blocked, load_approval_counts

    counts = load_approval_counts()
    if not counts:
        console.print("[dim]no approvals logged yet — use Claude Code normally and shed will learn[/dim]")
        return
    rows = sorted(counts.items(), key=lambda kv: kv[1]["count"], reverse=True)
    table = Table(title=f"approved permit patterns (top {top})")
    table.add_column("count", style="cyan")
    table.add_column("pattern")
    table.add_column("sessions", style="dim")
    table.add_column("status", style="yellow")
    for pat, entry in rows[:top]:
        status = "blocked" if is_blocked(pat) else "ok"
        n_sessions = len(entry.get("sessions", []))
        table.add_row(str(entry["count"]), pat, str(n_sessions), status)
    console.print(table)


@permit_app.command("suggest")
def permit_suggest(
    threshold: int = typer.Option(None, "--threshold", help="override default threshold"),
) -> None:
    """Show what would be proposed at current threshold."""
    from shed.permit import suggest

    rows = suggest(threshold=threshold)
    if not rows:
        cfg_t = threshold if threshold is not None else load_config().permits.threshold
        console.print(f"[dim]no candidates above threshold ({cfg_t})[/dim]")
        return
    table = Table(title=f"would propose (threshold={threshold or load_config().permits.threshold})")
    table.add_column("pattern")
    table.add_column("count")
    table.add_column("confidence")
    for r in rows:
        table.add_row(r["pattern"], str(r["count"]), f"{r['confidence']:.2f}")
    console.print(table)


@permit_app.command("log")
def permit_log(n: int = typer.Option(30, "-n", help="how many recent entries")) -> None:
    """Recent approved tool calls."""
    p = state_dir() / "permits-approved.jsonl"
    if not p.exists():
        console.print("[dim]no permit approvals logged yet[/dim]")
        return
    lines = p.read_text().splitlines()[-n:]
    table = Table(title=f"last {len(lines)} approvals")
    table.add_column("time", style="dim")
    table.add_column("tool")
    table.add_column("pattern")
    import datetime as _dt

    for line in lines:
        try:
            row = json.loads(line)
        except Exception:
            continue
        when = _dt.datetime.fromtimestamp(row["ts"]).strftime("%m-%d %H:%M:%S")
        table.add_row(when, row.get("tool_name", "?"), row.get("canonical", "?"))
    console.print(table)


@permit_app.command("threshold")
def permit_threshold(
    n: int = typer.Argument(None, help="new threshold value (omit to show)"),
) -> None:
    """Show or set the approval threshold."""
    cfg = load_config()
    if n is None:
        console.print(f"current threshold: [bold]{cfg.permits.threshold}[/bold]")
        return
    if n < 1:
        console.print("[red]threshold must be >= 1[/red]")
        raise typer.Exit(1)
    cfg.permits.threshold = n
    write_config(cfg)
    console.print(f"threshold set: [bold]{n}[/bold]")


@permit_app.command("scan")
def permit_scan() -> None:
    """Manually run the proposal generator (also runs at evolve)."""
    from shed.permit import generate_proposals

    written = generate_proposals()
    if not written:
        console.print("[dim]nothing new to propose[/dim]")
        return
    for p in written:
        console.print(f"[green]proposed[/green] {p.pattern} (×{p.count}) -> {p.path.name}")


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


@app.command()
def doctor() -> None:
    """Audit: hooks installed? embedder present? git initialized?"""
    score, _ = run_doctor(console)
    if score < 70:
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# undo / log
# ---------------------------------------------------------------------------


@app.command()
def undo(commit: str = typer.Argument("HEAD", help="commit to revert")) -> None:
    """git revert wrapper for any auto-applied change."""
    sh = shed_home()
    if not (sh / ".git").exists():
        console.print("[red]~/.shed is not a git repo[/red]")
        raise typer.Exit(1)
    rc = subprocess.call(
        ["git", "-C", str(sh), "revert", "--no-edit", commit],
    )
    raise typer.Exit(rc)


@app.command("log")
def log_(n: int = typer.Option(20, "-n", help="how many entries")) -> None:
    """Recent applied changes."""
    sh = shed_home()
    if not (sh / ".git").exists():
        console.print("[red]~/.shed is not a git repo[/red]")
        raise typer.Exit(1)
    subprocess.call(
        ["git", "-C", str(sh), "log", f"-n{n}", "--oneline", "--decorate"]
    )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _last_injection() -> dict | None:
    p = state_dir() / "injections.jsonl"
    if not p.exists():
        return None
    last = None
    for line in p.read_text().splitlines():
        try:
            last = json.loads(line)
        except Exception:
            continue
    return last


if __name__ == "__main__":
    app()

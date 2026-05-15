"""Evolve / GC tests."""

import shutil
from pathlib import Path

from shed.evolve import run_evolve
from shed.memory import discover

FIXTURES = Path(__file__).parent / "fixtures"


def _seed(memdir):
    shutil.copy2(FIXTURES / "coding_prefs.md", memdir / "coding_prefs.md")
    shutil.copy2(FIXTURES / "git_workflow.md", memdir / "git_workflow.md")
    shutil.copy2(FIXTURES / "cold_note.md", memdir / "cold_note.md")


def test_archive_cold_memory(memdir, shed_home_path):
    _seed(memdir)
    rep = run_evolve()
    assert "cold-note" in rep.archived
    # Hot memory survived.
    surviving = {m.slug for m in discover()}
    assert "coding-prefs" in surviving
    assert "cold-note" not in surviving
    # Archive received the file.
    archived = list((shed_home_path / "archive").glob("*.md"))
    assert any("cold-note" in p.name for p in archived)


def test_duplicate_detection(memdir, tmp_path):
    # Two memories that should look near-identical to the embedder.
    body = "Squash merge PRs and delete branches.\n"
    common_meta = (
        "category: workflow\n"
        "created_at: 2026-05-01T00:00:00+00:00\n"
        "cite_count: 1\n"
        "last_cited_at: 2026-05-13T00:00:00+00:00\n"
    )
    (memdir / "a.md").write_text(
        f"---\nslug: a\ntitle: Squash merge PRs\n{common_meta}---\n\n{body}"
    )
    (memdir / "b.md").write_text(
        f"---\nslug: b\ntitle: Squash merge PRs\n{common_meta}---\n\n{body}"
    )
    rep = run_evolve()
    pairs = {tuple(sorted([a, b])) for a, b, _ in rep.duplicates}
    assert ("a", "b") in pairs


def test_idempotent(memdir):
    _seed(memdir)
    run_evolve()
    rep2 = run_evolve()
    # Second run shouldn't re-archive anything.
    assert rep2.archived == []

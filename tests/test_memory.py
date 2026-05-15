"""Memory CRUD + discover tests."""

import shutil
from pathlib import Path

from shed.memory import Memory, archive, discover, load, now_iso, save, slugify

FIXTURES = Path(__file__).parent / "fixtures"


def _seed(memdir):
    shutil.copy2(FIXTURES / "coding_prefs.md", memdir / "coding_prefs.md")
    shutil.copy2(FIXTURES / "git_workflow.md", memdir / "git_workflow.md")


def test_slugify_handles_punctuation():
    assert slugify("Coding Prefs!!") == "coding-prefs"
    assert slugify("") == "untitled"


def test_load_and_round_trip(memdir):
    _seed(memdir)
    mem = load(memdir / "coding_prefs.md")
    assert mem.title == "Coding preferences"
    assert mem.category == "coding-preferences"
    assert mem.cite_count == 4

    mem.cite_count = 99
    save(mem)
    again = load(memdir / "coding_prefs.md")
    assert again.cite_count == 99


def test_discover_finds_all(memdir):
    _seed(memdir)
    mems = discover()
    slugs = {m.slug for m in mems}
    assert "coding-prefs" in slugs
    assert "git-workflow" in slugs


def test_archive_moves_file(memdir, shed_home_path):
    _seed(memdir)
    mem = load(memdir / "coding_prefs.md")
    target = archive(mem)
    assert target.exists()
    assert not (memdir / "coding_prefs.md").exists()
    assert target.parent == shed_home_path / "archive"


def test_create_save(memdir):
    p = memdir / "fresh.md"
    mem = Memory(
        path=p,
        slug="fresh",
        title="Fresh memory",
        body="body text",
        category="workflow",
        created_at=now_iso(),
    )
    save(mem)
    again = load(p)
    assert again.title == "Fresh memory"
    assert again.body.strip() == "body text"

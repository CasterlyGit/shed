"""brief.py handling of kind:skill proposals — accept writes a live SKILL.md."""

from __future__ import annotations

from pathlib import Path

from rich.console import Console

from shed.brief import PendingProposal, _accept, list_pending
from shed.config import proposals_dir  # noqa: F401  (used below)

SKILL_PROPOSAL = """---
kind: skill
name: fly-deploy
confidence: 0.85
---
<!-- shed skill proposal -->

## fly-deploy

Converged in 6 turns / $0.21.

<!-- SKILL-BODY-START -->
---
name: fly-deploy
source: shed-learn
---
# fly-deploy

## Task
Deploy the app to Fly.

## Converged approach
Run `fly deploy`.
<!-- SKILL-BODY-END -->
"""


def _write_proposal(name: str, text: str) -> Path:
    proposals_dir().mkdir(parents=True, exist_ok=True)
    p = proposals_dir() / name
    p.write_text(text)
    return p


def test_skill_proposal_is_recognized():
    p = _write_proposal("skill-20260530-120000-fly-deploy.md", SKILL_PROPOSAL)
    pending = list_pending()
    match = [pp for pp in pending if pp.path == p]
    assert match
    assert match[0].kind == "skill"
    assert match[0].pattern == "fly-deploy"
    assert match[0].category == "procedure"


def test_accepting_skill_writes_live_skill_md(tmp_path, monkeypatch):
    skills = tmp_path / "skills"
    monkeypatch.setenv("SHED_SKILLS_DIR", str(skills))

    p = _write_proposal("skill-20260530-120000-fly-deploy.md", SKILL_PROPOSAL)
    pp = PendingProposal.from_path(p)

    _accept(pp, Console(record=True), pin=False)

    target = skills / "fly-deploy" / "SKILL.md"
    assert target.exists()
    body = target.read_text()
    assert "name: fly-deploy" in body
    assert "fly deploy" in body
    assert "SKILL-BODY" not in body  # markers stripped
    # proposal consumed
    assert not p.exists()

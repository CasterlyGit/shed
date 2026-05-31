"""Correction detection + proposal tests."""

from shed.observe import detect, observe_text


def test_detect_obvious_correction():
    sig = detect("no, don't push to main")
    assert sig is not None
    assert sig.confidence > 0.5


def test_detect_returns_none_on_neutral_text():
    assert detect("ok thanks for the change") is None


def test_observe_writes_proposal_for_workflow_correction(shed_home_path):
    p = observe_text("don't push to main, run pytest first")
    assert p is not None
    assert p.category == "workflow"
    assert p.path.exists()
    assert p.path.parent == shed_home_path / "proposals"


def test_observe_drops_unclassifiable(shed_home_path):
    p = observe_text("no banana phone please")
    # Has a correction signal but no category match -> dropped.
    assert p is None


def test_observe_dedups_same_correction(shed_home_path):
    # The Stop hook re-observes the same last-user message every turn. The
    # same correction must not spawn a new proposal file each time.
    text = "don't push to main, run pytest first"
    p1 = observe_text(text)
    p2 = observe_text(text)
    assert p1 is not None and p2 is not None
    assert p1.path == p2.path
    n = len(list((shed_home_path / "proposals").glob("*.md")))
    assert n == 1, f"expected 1 proposal, found {n}"


def test_observe_redacts_pii_from_body(shed_home_path):
    p = observe_text("no, don't push, my number is 415-555-1212")
    assert p is not None
    assert "415-555-1212" not in p.path.read_text()
    assert "[shed: redacted]" in p.path.read_text()

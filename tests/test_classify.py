"""Classifier tests."""

from shed.classify import classify
from shed.config import DEFAULT_CATEGORIES


def test_coding_pref_classified():
    assert classify("prefer typer over click", DEFAULT_CATEGORIES) == "coding-preferences"


def test_workflow_classified():
    assert classify("don't push without running pytest", DEFAULT_CATEGORIES) == "workflow"


def test_tool_choice_classified():
    assert classify("use uv instead of pip", DEFAULT_CATEGORIES) == "tool-choices"


def test_unrelated_returns_none():
    assert classify("the sky is blue today", DEFAULT_CATEGORIES) is None


def test_dropped_when_category_not_in_allowlist():
    # Even if it'd match coding-preferences, an empty allowlist drops it.
    assert classify("prefer tabs over spaces", []) is None
    # Restricted allowlist that doesn't include coding-preferences
    assert classify("prefer tabs over spaces", ["project-facts"]) is None

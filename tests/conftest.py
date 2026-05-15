"""Test fixtures.

Every test runs against a tmp ``SHED_HOME`` + a tmp memory root so we never
touch the user's real ~/.shed or ~/.claude.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolate_env(tmp_path, monkeypatch):
    shed = tmp_path / "shed"
    claude = tmp_path / "claude"
    memdir = tmp_path / "memory"
    for d in (shed, claude, memdir):
        d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("SHED_HOME", str(shed))
    monkeypatch.setenv("SHED_CLAUDE_HOME", str(claude))
    monkeypatch.setenv("SHED_MEMORY_ROOTS", str(memdir))
    monkeypatch.setenv("SHED_EMBEDDER", "hash")
    yield


@pytest.fixture()
def memdir(tmp_path):
    return Path(os.environ["SHED_MEMORY_ROOTS"])


@pytest.fixture()
def shed_home_path():
    return Path(os.environ["SHED_HOME"])

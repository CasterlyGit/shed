"""Embedder backend selection + ONNX path tests.

The ONNX semantic test is marked ``slow`` because it downloads a model on
first run. Skip with ``pytest -m "not slow"``.
"""

from __future__ import annotations

import os

import numpy as np
import pytest

from shed.embeddings import (
    HashEmbedder,
    Index,
    ONNXEmbedder,
    download_onnx_model,
    get_embedder,
    onnx_model_files_present,
)


def test_hash_embedder_normalized():
    e = HashEmbedder()
    vecs = e.encode(["hello world", "another text"])
    norms = np.linalg.norm(vecs, axis=1)
    assert np.allclose(norms, 1.0, atol=1e-5)
    assert e.backend_name() == "hash"


def test_hash_embedder_handles_empty_text():
    e = HashEmbedder()
    vecs = e.encode([""])
    # All-zero input → zero vector, norm clamp keeps shape valid.
    assert vecs.shape == (1, 256)


def test_get_embedder_respects_force_hash(monkeypatch):
    monkeypatch.setenv("SHED_EMBEDDER", "hash")
    assert get_embedder().backend_name() == "hash"


def _write_memory(path, title, body):
    path.write_text(f"---\nslug: {path.stem}\ntitle: {title}\n---\n{body}\n")


def test_index_roundtrip_with_hash_embedder(memdir):
    from shed.memory import discover

    _write_memory(memdir / "a.md", "thing one", "content one")
    _write_memory(memdir / "b.md", "thing two", "content two")

    e = HashEmbedder()
    idx = Index(e)
    n = idx.upsert(discover())
    assert n == 2
    idx.save()

    # Reload and search.
    idx2 = Index(e)
    idx2.load()
    assert len(idx2) == 2
    results = idx2.search("content", top_k=2)
    assert len(results) == 2
    # Normalized cosine: scores in [-1, 1]
    assert all(-1.0 <= s <= 1.0 for _, _, s in results)


def test_index_handles_dim_change_on_reload(memdir, tmp_path):
    """If the embedder dim changes (e.g., user switched backends), index reloads cleanly."""
    from shed.memory import discover

    _write_memory(memdir / "x.md", "x", "x")

    # Save with one dim
    e1 = HashEmbedder(dim=128)
    idx = Index(e1)
    idx.upsert(discover())
    idx.save()

    # Reload with a different dim — should reset rather than crash.
    e2 = HashEmbedder(dim=256)
    idx2 = Index(e2)
    idx2.load()
    assert len(idx2) == 0  # cleared due to dim mismatch


# ---------------------------------------------------------------------------
# Slow tests — real ONNX download + inference. Skip with -m "not slow".
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_onnx_download_and_encode():
    if not onnx_model_files_present():
        download_onnx_model(progress=False)
    assert onnx_model_files_present()

    e = ONNXEmbedder()
    assert e.backend_name() == "onnx"
    assert e.dim == 384

    texts = [
        "Python testing with pytest",
        "graph algorithms for DSA practice",
        "JavaScript React components",
    ]
    vecs = e.encode(texts)
    assert vecs.shape == (3, 384)
    norms = np.linalg.norm(vecs, axis=1)
    assert np.allclose(norms, 1.0, atol=1e-3)


@pytest.mark.slow
def test_onnx_semantic_relevance():
    """ONNX embedder ranks semantically-related text correctly."""
    if not onnx_model_files_present():
        download_onnx_model(progress=False)
    e = ONNXEmbedder()

    # Memory corpus.
    docs = [
        "DSA tutoring guidance — graphs, BFS, DFS, dynamic programming",
        "hand gesture recognition with MediaPipe webcam input",
        "voice dictation push-to-talk Whisper integration",
        "git commit message style and PR conventions",
    ]
    queries_expected = [
        ("graph dsa practice", 0),
        ("hand gesture webcam", 1),
        ("voice dictation laptop", 2),
        ("how should I write commit messages", 3),
    ]

    doc_vecs = e.encode(docs)
    correct = 0
    for query, expected_idx in queries_expected:
        q = e.encode([query])[0]
        scores = doc_vecs @ q
        predicted = int(np.argmax(scores))
        if predicted == expected_idx:
            correct += 1
    # Accept 3/4 (one could legitimately be ambiguous).
    assert correct >= 3, f"semantic accuracy too low: {correct}/4"

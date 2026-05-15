"""Embedding index over memory files.

Default model: ``BAAI/bge-small-en-v1.5`` via sentence-transformers. We persist
the index to ``~/.shed/index/embeddings.parquet`` keyed by stable id + content
hash so we only re-embed memories that actually changed.

For test/CI use we expose a ``HashEmbedder`` fallback that requires no model
download — selected automatically when ``SHED_EMBEDDER=hash``.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterable
from pathlib import Path
from typing import Protocol

import numpy as np

from shed.config import index_dir
from shed.memory import Memory

_INDEX_FILE = "embeddings.parquet"


class Embedder(Protocol):
    dim: int

    def encode(self, texts: list[str]) -> np.ndarray: ...


# ---------------------------------------------------------------------------
# Hash embedder — deterministic, no deps, used in tests.
# ---------------------------------------------------------------------------


class HashEmbedder:
    """Bag-of-tokens hashed into a fixed-dim vector. Cosine works.

    Surprisingly OK for v0.1 demos and lets the test suite run with no
    network or model cache.
    """

    def __init__(self, dim: int = 256):
        self.dim = dim

    def encode(self, texts: list[str]) -> np.ndarray:
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for i, t in enumerate(texts):
            for tok in _tokens(t):
                h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
                out[i, h % self.dim] += 1.0
        # L2 normalize so dot product == cosine similarity.
        norms = np.linalg.norm(out, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return out / norms


def _tokens(text: str) -> list[str]:
    return [t for t in "".join(c.lower() if c.isalnum() else " " for c in text).split() if t]


# ---------------------------------------------------------------------------
# sentence-transformers wrapper.
# ---------------------------------------------------------------------------


class STEmbedder:
    def __init__(self, model_name: str = "BAAI/bge-small-en-v1.5"):
        from sentence_transformers import SentenceTransformer

        self._model = SentenceTransformer(model_name)
        self.dim = self._model.get_sentence_embedding_dimension()

    def encode(self, texts: list[str]) -> np.ndarray:
        vecs = self._model.encode(
            texts, normalize_embeddings=True, convert_to_numpy=True, show_progress_bar=False
        )
        return vecs.astype(np.float32)


def get_embedder(model_name: str = "BAAI/bge-small-en-v1.5") -> Embedder:
    """Pick an embedder. ``SHED_EMBEDDER=hash`` forces the hash fallback.

    Falls back to hash automatically if sentence-transformers isn't importable
    so ``shed why`` and the demo still work on a fresh machine.
    """
    forced = os.environ.get("SHED_EMBEDDER", "").lower()
    if forced == "hash":
        return HashEmbedder()
    if forced == "st":
        return STEmbedder(model_name)
    try:
        return STEmbedder(model_name)
    except Exception:
        return HashEmbedder()


# ---------------------------------------------------------------------------
# Index — content-addressed, parquet-backed.
# ---------------------------------------------------------------------------


def _content_hash(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()[:16]


class Index:
    """Tiny in-memory cosine index, persisted to parquet.

    Columns: id, slug, path, content_hash, vector (list[float]).
    """

    def __init__(self, embedder: Embedder, path: Path | None = None):
        self.embedder = embedder
        self.path = path or (index_dir() / _INDEX_FILE)
        self.ids: list[str] = []
        self.slugs: list[str] = []
        self.paths: list[str] = []
        self.hashes: list[str] = []
        self.vectors: np.ndarray = np.zeros((0, embedder.dim), dtype=np.float32)

    # ---- persistence ----

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            import pyarrow.parquet as pq
        except Exception:
            return
        table = pq.read_table(self.path)
        d = table.to_pydict()
        self.ids = list(d.get("id", []))
        self.slugs = list(d.get("slug", []))
        self.paths = list(d.get("path", []))
        self.hashes = list(d.get("content_hash", []))
        vecs = d.get("vector", [])
        if vecs:
            self.vectors = np.array(vecs, dtype=np.float32)
        else:
            self.vectors = np.zeros((0, self.embedder.dim), dtype=np.float32)

    def save(self) -> None:
        import pyarrow as pa
        import pyarrow.parquet as pq

        self.path.parent.mkdir(parents=True, exist_ok=True)
        table = pa.table(
            {
                "id": self.ids,
                "slug": self.slugs,
                "path": self.paths,
                "content_hash": self.hashes,
                "vector": [v.tolist() for v in self.vectors],
            }
        )
        pq.write_table(table, self.path)

    # ---- build / refresh ----

    def upsert(self, mems: Iterable[Memory]) -> int:
        """Re-embed any memory whose content hash changed; add new ones.

        Returns the number of (re-)embedded memories.
        """
        mems = list(mems)
        wanted: dict[str, Memory] = {m.id: m for m in mems}

        existing: dict[str, int] = {mid: i for i, mid in enumerate(self.ids)}

        # Drop entries no longer on disk.
        keep = [i for i, mid in enumerate(self.ids) if mid in wanted]
        if len(keep) != len(self.ids):
            self.ids = [self.ids[i] for i in keep]
            self.slugs = [self.slugs[i] for i in keep]
            self.paths = [self.paths[i] for i in keep]
            self.hashes = [self.hashes[i] for i in keep]
            self.vectors = self.vectors[keep] if len(keep) else np.zeros(
                (0, self.embedder.dim), dtype=np.float32
            )
            existing = {mid: i for i, mid in enumerate(self.ids)}

        # Decide which memories need (re-)embedding.
        to_embed: list[Memory] = []
        for m in mems:
            text = m.text_for_embedding
            h = _content_hash(text)
            idx = existing.get(m.id)
            if idx is None or self.hashes[idx] != h:
                to_embed.append(m)

        if not to_embed:
            return 0

        new_vecs = self.embedder.encode([m.text_for_embedding for m in to_embed])
        for m, vec in zip(to_embed, new_vecs, strict=True):
            text = m.text_for_embedding
            h = _content_hash(text)
            idx = existing.get(m.id)
            if idx is None:
                self.ids.append(m.id)
                self.slugs.append(m.slug)
                self.paths.append(str(m.path))
                self.hashes.append(h)
                self.vectors = (
                    np.vstack([self.vectors, vec[None, :]])
                    if self.vectors.size
                    else vec[None, :].copy()
                )
                existing[m.id] = len(self.ids) - 1
            else:
                self.slugs[idx] = m.slug
                self.paths[idx] = str(m.path)
                self.hashes[idx] = h
                self.vectors[idx] = vec
        return len(to_embed)

    # ---- query ----

    def search(self, query: str, top_k: int = 5) -> list[tuple[str, str, float]]:
        """Return ``[(id, slug, score)]`` ordered by cosine similarity desc."""
        if not self.ids:
            return []
        q = self.embedder.encode([query])[0]
        # Vectors are L2-normalized so dot == cosine.
        scores = self.vectors @ q
        order = np.argsort(-scores)[:top_k]
        return [(self.ids[i], self.slugs[i], float(scores[i])) for i in order]

    def __len__(self) -> int:
        return len(self.ids)

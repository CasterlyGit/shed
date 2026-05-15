"""Embedding index over memory files.

Three backends, picked in priority order:

1. **ONNX** (preferred) — onnxruntime + bge-small-en-v1.5 ONNX model.
   Real semantic embeddings, ~150ms/query on CPU, no torch dep.
   Works on Intel Mac, Apple Silicon, Linux.

2. **sentence-transformers** — kept for users with working torch >= 2.4.
   Detected by trying to import + run.

3. **HashEmbedder** — last-resort fallback. Bag-of-tokens, deterministic,
   no deps. Used in tests and when ONNX/torch are unavailable.

Persisted to ``~/.shed/index/embeddings.parquet`` keyed by stable id +
content hash so we only re-embed memories that actually changed.

Selection precedence:
- ``SHED_EMBEDDER=hash`` forces hash
- ``SHED_EMBEDDER=onnx`` forces onnx (errors if model not downloaded)
- ``SHED_EMBEDDER=st`` forces sentence-transformers
- otherwise: try onnx, then st, then hash
"""

from __future__ import annotations

import hashlib
import os
import urllib.error
import urllib.request
from collections.abc import Iterable
from pathlib import Path
from typing import Protocol

import numpy as np

from shed.config import index_dir
from shed.memory import Memory

_INDEX_FILE = "embeddings.parquet"

# bge-small-en-v1.5 ONNX export (Xenova mirror — well-maintained, stable URLs)
_ONNX_MODEL = "BAAI/bge-small-en-v1.5"
_ONNX_HF_REPO = "Xenova/bge-small-en-v1.5"
_ONNX_FILES = {
    "model": "onnx/model_quantized.onnx",  # ~33MB quantized
    "tokenizer": "tokenizer.json",
    "tokenizer_config": "tokenizer_config.json",
    "config": "config.json",
}
_HF_BASE = "https://huggingface.co"


class Embedder(Protocol):
    dim: int

    def encode(self, texts: list[str]) -> np.ndarray: ...

    def backend_name(self) -> str: ...


# ---------------------------------------------------------------------------
# Hash embedder — deterministic, no deps, fallback only.
# ---------------------------------------------------------------------------


class HashEmbedder:
    """Bag-of-tokens hashed into a fixed-dim vector. Cosine works.

    Last-resort fallback when no real embedder is available.
    """

    def __init__(self, dim: int = 256):
        self.dim = dim

    def encode(self, texts: list[str]) -> np.ndarray:
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for i, t in enumerate(texts):
            for tok in _tokens(t):
                h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
                out[i, h % self.dim] += 1.0
        norms = np.linalg.norm(out, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return out / norms

    def backend_name(self) -> str:
        return "hash"


def _tokens(text: str) -> list[str]:
    return [t for t in "".join(c.lower() if c.isalnum() else " " for c in text).split() if t]


# ---------------------------------------------------------------------------
# ONNX embedder — bge-small via onnxruntime + tokenizers. The default.
# ---------------------------------------------------------------------------


def _onnx_model_dir() -> Path:
    return Path.home() / ".cache" / "shed" / "models" / "bge-small-en-v1.5"


def onnx_model_files_present() -> bool:
    d = _onnx_model_dir()
    return all((d / fname).exists() for fname in _ONNX_FILES.values())


def _http_download(url: str, target: Path) -> None:
    """Download a URL to a file, following redirects (HF uses LFS redirects).

    Streams in chunks so large files don't hit memory.
    """
    req = urllib.request.Request(url, headers={"User-Agent": "shed/0.2"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        # urllib follows 301/302 by default but HF uses 307 → LFS host. Manually handle.
        if resp.status in (301, 302, 303, 307, 308):
            location = resp.headers.get("Location")
            if not location:
                raise RuntimeError(f"redirect with no Location header: {url}")
            return _http_download(location, target)
        tmp = target.with_suffix(target.suffix + ".part")
        with open(tmp, "wb") as f:
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                f.write(chunk)
        # Atomic rename so a crashed download never leaves a half-file as the real one.
        tmp.replace(target)


def download_onnx_model(progress: bool = True) -> Path:
    """Download the ONNX model files into ~/.cache/shed/models/.

    Uses urllib + chunked streaming (no extra deps). Returns the model directory.
    Raises on failure so caller can fall through.
    """
    d = _onnx_model_dir()
    d.mkdir(parents=True, exist_ok=True)
    for fname in _ONNX_FILES.values():
        target = d / fname
        if target.exists() and target.stat().st_size > 1024:
            # Existing non-trivially-sized file is good enough.
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        url = f"{_HF_BASE}/{_ONNX_HF_REPO}/resolve/main/{fname}?download=true"
        if progress:
            try:
                from rich.console import Console

                Console().print(f"[dim]downloading {fname}[/dim]")
            except Exception:
                pass
        try:
            _http_download(url, target)
        except (urllib.error.URLError, OSError) as e:
            # Clean up partial files so retry doesn't see them as "good enough"
            for stale in [target, target.with_suffix(target.suffix + ".part")]:
                if stale.exists():
                    stale.unlink()
            raise RuntimeError(f"failed to download {url}: {e}") from e
    return d


class ONNXEmbedder:
    """bge-small-en-v1.5 via onnxruntime (CPU). Fast + portable."""

    def __init__(self):
        if not onnx_model_files_present():
            download_onnx_model(progress=True)
        d = _onnx_model_dir()

        import onnxruntime as ort
        from tokenizers import Tokenizer

        sess_opts = ort.SessionOptions()
        sess_opts.intra_op_num_threads = max(1, (os.cpu_count() or 4) // 2)
        sess_opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self._session = ort.InferenceSession(
            str(d / _ONNX_FILES["model"]),
            sess_opts,
            providers=["CPUExecutionProvider"],
        )
        self._tokenizer = Tokenizer.from_file(str(d / _ONNX_FILES["tokenizer"]))
        # Pad on the right with [PAD] (id=0 for bge); truncate at 512.
        self._tokenizer.enable_padding(pad_id=0, pad_token="[PAD]", direction="right")
        self._tokenizer.enable_truncation(max_length=512)

        # bge-small embedding dim is 384.
        self.dim = 384

    def encode(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        # Add the bge query/passage prefix? bge-small expects no special prefix
        # for symmetric similarity in the small variant. Skip.
        encs = self._tokenizer.encode_batch(texts)
        input_ids = np.array([e.ids for e in encs], dtype=np.int64)
        attn_mask = np.array([e.attention_mask for e in encs], dtype=np.int64)
        token_type_ids = np.zeros_like(input_ids)

        # The Xenova export uses these standard input names.
        ort_inputs = {
            "input_ids": input_ids,
            "attention_mask": attn_mask,
            "token_type_ids": token_type_ids,
        }
        # Filter to inputs the model actually expects (some exports skip token_type_ids).
        expected = {i.name for i in self._session.get_inputs()}
        ort_inputs = {k: v for k, v in ort_inputs.items() if k in expected}

        outputs = self._session.run(None, ort_inputs)
        # Output is last_hidden_state [batch, seq, hidden]. Mean-pool with mask.
        last_hidden = outputs[0]
        mask = attn_mask.astype(np.float32)[..., None]
        summed = (last_hidden * mask).sum(axis=1)
        counts = mask.sum(axis=1).clip(min=1e-9)
        pooled = summed / counts

        # L2 normalize so dot product == cosine similarity.
        norms = np.linalg.norm(pooled, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return (pooled / norms).astype(np.float32)

    def backend_name(self) -> str:
        return "onnx"


# ---------------------------------------------------------------------------
# sentence-transformers wrapper — kept for systems with working torch.
# ---------------------------------------------------------------------------


class STEmbedder:
    def __init__(self, model_name: str = _ONNX_MODEL):
        from sentence_transformers import SentenceTransformer

        self._model = SentenceTransformer(model_name)
        self.dim = self._model.get_sentence_embedding_dimension()

    def encode(self, texts: list[str]) -> np.ndarray:
        vecs = self._model.encode(
            texts, normalize_embeddings=True, convert_to_numpy=True, show_progress_bar=False
        )
        return vecs.astype(np.float32)

    def backend_name(self) -> str:
        return "sentence-transformers"


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


def get_embedder(model_name: str = _ONNX_MODEL) -> Embedder:
    """Pick the best available embedder. See module docstring for selection rules."""
    forced = os.environ.get("SHED_EMBEDDER", "").lower()
    if forced == "hash":
        return HashEmbedder()
    if forced == "onnx":
        return ONNXEmbedder()
    if forced == "st":
        return STEmbedder(model_name)

    # Auto-select: prefer ONNX, then ST (only if torch is actually working), then hash.
    try:
        return ONNXEmbedder()
    except Exception:
        pass
    try:
        # Validate torch actually loads, not just sentence-transformers.
        import torch  # noqa: F401

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
            loaded = np.array(vecs, dtype=np.float32)
            # Re-embed if dim changed (switched embedder backends).
            if loaded.shape[1] != self.embedder.dim:
                self.ids, self.slugs, self.paths, self.hashes = [], [], [], []
                self.vectors = np.zeros((0, self.embedder.dim), dtype=np.float32)
            else:
                self.vectors = loaded
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
        scores = self.vectors @ q
        order = np.argsort(-scores)[:top_k]
        return [(self.ids[i], self.slugs[i], float(scores[i])) for i in order]

    def __len__(self) -> int:
        return len(self.ids)

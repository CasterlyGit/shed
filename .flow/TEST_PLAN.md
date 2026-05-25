# shed — Test Plan

## How to Run

```bash
# Full suite (hash embedder, no model download)
cd /path/to/shed
SHED_EMBEDDER=hash pytest --ignore=tests/test_embeddings.py -q

# With ONNX embedder (requires model download ~33MB)
pytest -q

# Single module
SHED_EMBEDDER=hash pytest tests/test_inject.py -v
```

All tests use isolated `SHED_HOME` + `SHED_MEMORY_ROOTS` via the `conftest.py` autouse fixture. No real `~/.shed` is touched.

## Test Categories

### Unit — `tests/test_classify.py`
- Maps free-text corrections to allowlisted categories
- Edge cases: empty string, ambiguous text, already-categorized input

### Unit — `tests/test_embeddings.py`
- `HashEmbedder.encode()`: shape, L2 norm, determinism
- `Index.upsert()` + `Index.search()`: returns correct top-k order
- Content-hash dedup: re-upsert same memory → no re-embed
- **Skipped in hash-only runs** (ONNX model download required for ONNX tests)

### Unit — `tests/test_inject.py`
- `inject_for_prompt()` returns `<shed-context>` block when memories present
- Returns `""` in private mode
- Returns `""` when `inject.enabled = false`
- Handles empty memory dir (fail-open)
- Handles corrupt parquet index (fail-open)
- `InjectionResult.elapsed_ms` is populated

### Unit — `tests/test_memory.py`
- `discover()` finds `.md` files in all configured memory roots
- `save()` writes correct frontmatter
- `Memory.text_for_embedding` combines title + body

### Unit — `tests/test_observe.py`
- Correction detection triggers on "actually, you should…" patterns
- PII redaction fires before proposal write
- No proposal written in private mode
- Non-correction text produces `None`

### Unit — `tests/test_quality.py`
- `log_injection()` appends to `quality.jsonl`
- `log_citations()` appends citation events
- `compute_scores()` applies exponential decay correctly
- `detect_citations_in_response()` finds 12-char substring matches

### Unit — `tests/test_reflect_pipeline.py`
- `reflect_from_stop_payload()` with valid transcript path: returns `(n_cited, proposal_or_None)`
- Missing `transcript_path` key: returns `(0, None)`
- Corrupt transcript JSONL: returns `(0, None)` (fail-open)
- Stop hook auto-writes stats row to `stats.jsonl`

### Unit — `tests/test_stats.py`
- `collect()` returns expected schema keys
- `write_stats()` appends valid JSON line
- `_proposal_stats()` reads `status: accepted` / `status: rejected` frontmatter lines

### Unit — `tests/test_thresholds.py`
- `log_feedback()` writes to `thresholds.jsonl`
- `tune()` computes 75th-percentile threshold from recent accepts
- Per-kind isolation: coding-preferences threshold doesn't bleed into workflow

### Unit — `tests/test_permit.py`
- `record_approval()` increments pattern count
- `apply_proposal()` writes permit entry to config
- Duplicate pattern: count increments, no duplicate entry

### Unit — `tests/test_redact.py`
- Email addresses redacted
- IP addresses redacted
- API keys / secrets redacted

### Unit — `tests/test_statusline.py`
- `render()` produces correct format: `[shed:●●●○ 3↓ ✓2]`
- `write_cache()` writes to `~/.shed/state/statusline.txt`

### Integration — `tests/test_evolve.py`
- Cold memory pruning removes memories not injected in 90d
- Dedup removes near-identical bodies

### Integration — `tests/test_install.py`
- `install.py` generates correctly templated hook scripts
- Generated scripts reference correct Python path

### Smoke — `scripts/smoke.sh`
- End-to-end: seed one memory → `shed inject` → assert exit 0, `<shed-context>` in output
- Runs with `SHED_EMBEDDER=hash` (no model download)
- Isolated temp `SHED_HOME`; cleans up on exit

## Coverage Goals

| Module | Target |
|---|---|
| `inject.py` | ≥ 90% |
| `observe.py` | ≥ 85% |
| `reflect.py` | ≥ 85% |
| `quality.py` | ≥ 90% |
| `thresholds.py` | ≥ 85% |
| `stats.py` | ≥ 85% |
| `brief.py` | ≥ 75% |
| `embeddings.py` (hash path) | ≥ 80% |

## Edge Cases

| Case | Expected Behavior |
|---|---|
| Empty memory dir | `inject_for_prompt()` returns `""` with `skipped_reason="no-memories"` |
| Corrupt JSONL in `quality.jsonl` | Line skipped; rest of file processed normally |
| Proposal with no `status:` line | Not counted as accepted or rejected in `_proposal_stats()` |
| Proposal with `status: accepted` | Counted as accepted, not as pending |
| `transcript_path` points to missing file | `reflect_from_stop_payload()` returns `(0, None)` |
| `SHED_MODE=private` set | Inject returns `""`, no writes to disk |
| `stats.write_stats()` fails | Stop hook completes normally (fail-open) |
| `Index` parquet file corrupt | `load()` swallows error; proceeds with empty index |
| `min_score = 1.0` (impossible threshold) | Returns `""` (no memories pass threshold) |

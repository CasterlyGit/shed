# Shed hash-fixture benchmarks

- Recorded: 2026-05-25
- Embedder: deterministic `hash` test fixture

Run `python scripts/bench.py` from an installed source checkout to reproduce
the same workload.

| Operation | Cold | Warm | p95 |
|---|---|---|---|
| embed query (hash) | 7.3ms | 0.1ms | 0.4ms |
| top-k retrieval (50 memories) | 0.1ms | 0.1ms | 13.1ms |
| top-k retrieval (200 memories) | 0.1ms | 0.2ms | 4.3ms |
| top-k retrieval (500 memories) | 0.2ms | 0.4ms | 4.8ms |
| full inject round-trip (200 memories) | 0.1ms | 0.2ms | 0.4ms |

> Scope: synthetic in-memory data only. Cold is the first call, warm is the
> median of 20 runs, and p95 is the 95th percentile. The original hardware and
> process environment were not recorded, so these values are historical
> evidence rather than cross-machine performance guarantees. They do not
> measure ONNX model loading, ONNX encoding, hook startup, or end-to-end prompt
> injection.

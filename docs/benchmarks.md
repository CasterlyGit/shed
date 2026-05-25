## shed benchmarks (2026-05-25)

embedder: `hash`

| Operation | Cold | Warm | p95 |
|---|---|---|---|
| embed query (hash) | 7.3ms | 0.1ms | 0.4ms |
| top-k retrieval (50 memories) | 0.1ms | 0.1ms | 13.1ms |
| top-k retrieval (200 memories) | 0.1ms | 0.2ms | 4.3ms |
| top-k retrieval (500 memories) | 0.2ms | 0.4ms | 4.8ms |
| full inject round-trip (200 memories) | 0.1ms | 0.2ms | 0.4ms |

> Measured on synthetic in-memory dataset. Cold = first call; Warm = median of 20 runs; p95 = 95th percentile.

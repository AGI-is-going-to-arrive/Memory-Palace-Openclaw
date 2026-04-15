# Benchmark Baseline Manifest

This manifest freezes the current public benchmark and contract surface for the
repository benchmark gate.

## MCP/API Contract Lock

Freeze Rule:

- Do not change both implementation code and benchmark gold set in the same
  review without explicitly re-baselining the benchmark assets.
- Source of truth for the frozen threshold contract is
  `backend/tests/benchmark_results.md`.
- Relevant implementation references include `backend/mcp_server.py` and the
  benchmark helpers under `backend/tests/benchmark/helpers/`.

## Threshold Contract v1

- Frozen source: `backend/tests/benchmark_results.md`
- Benchmark implementation reference: `backend/mcp_server.py`
- Review note: benchmark threshold changes must be intentional and traceable

### `search_memory` response must contain

- `degraded`
- `mode_applied`
- `mode_requested`
- `ok`
- `query`
- `query_effective`
- `results`

### `compact_context` response must contain

- `flushed`
- `gist_method`
- `ok`
- `quality`
- `reason`
- `session_id`
- `source_hash`

### `write_guard` decision must contain

- `action`
- `degrade_reasons`
- `degraded`
- `method`
- `reason`

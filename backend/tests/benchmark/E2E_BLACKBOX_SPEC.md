# OpenClaw Black-Box E2E Benchmark Specification

> Status: BASELINE ESTABLISHED — 2026-04-06
> Goal: Validate Memory Palace retrieval quality through end-to-end MCP tool calls, compared against a filesystem keyword baseline.

## 1. Scope

This benchmark evaluates Memory Palace **as experienced by an OpenClaw user** — through MCP tools only.
It does NOT call `sqlite_client.search_advanced()` or any internal API.

### What it tests

- MCP tool chain: `create_memory → search_memory → read_memory → update_memory → delete_memory → add_alias → compact_context`
- OpenClaw lifecycle patterns: auto-recall, auto-capture, session continuity
- Real retrieval quality through the MCP `search_memory` interface

### What it does NOT test

- Internal scoring components (vector/text/context weights)
- SQLite internals or migration correctness
- Frontend dashboard
- Plugin TypeScript code (covered by plugin unit tests)

## 2. Architecture

```
Test Harness (pytest)
  → MCP Client (mcp SDK, stdio transport)
    → Backend (FastAPI + FastMCP, in-process or subprocess)
      → SQLite (temp DB per test run)
```

### Key constraint

**No internal imports.** The harness communicates exclusively via MCP tool calls.
A separate `diagnostic_mode=true` flag may enable per-query internal metadata in responses,
but formal metrics are computed only from MCP return values.

## 3. Scenario Matrix

### 3.1 Scenario Categories

| ID | Category | OpenClaw Workflow | Description |
|---|---|---|---|
| SC01 | Store → Recall | Manual memory creation + search | Create memories via `create_memory`, retrieve via `search_memory` |
| SC02 | Auto-capture → Auto-recall | `agent_end` hook capture + `before_agent_start` recall | Simulate the lifecycle hook pattern |
| SC03 | Update → Recall | Update existing memory, verify latest content recalled | Test `update_memory` + `search_memory` consistency |
| SC04 | Cross-domain Search | Search across domains | Verify retrieval spans `personal://`, `project://`, `learning://` etc. |
| SC05 | Alias Recall | Add alias, search by alias path | `add_alias` + `search_memory` with alias-covered content |
| SC06 | Temporal Recall | "What did I save recently?" | Search with recency expectation (newest first) |
| SC07 | Conflict / Contradiction | Store contradictory facts | Verify `create_memory` guard detects conflicts |
| SC08 | Session Continuity | Multi-session recall | Create in session A, recall in session B (different session_id) |
| SC09 | Compact → Recall | `compact_context` then search | Verify gist quality survives compaction |
| SC10 | Delete → Verify Gone | Delete memory, confirm not in search | `delete_memory` + `search_memory` should not return deleted item |
| SC11 | Namespace Navigation | `read_memory("domain://path")` | Read by exact URI, verify content |
| SC12 | Scale Stress | 200+ memories, search accuracy | Bulk create then evaluate recall quality |

### 3.2 Per-Scenario Fixture Format

```jsonl
{
  "scenario_id": "SC01_001",
  "category": "SC01",
  "description": "Create personal dietary restriction, search by natural query",
  "setup": [
    {"tool": "create_memory", "args": {"domain": "personal", "parent_path": "", "title": "饮食禁忌", "content": "对花生和虾严重过敏..."}}
  ],
  "action": {"tool": "search_memory", "args": {"query": "我对什么食物过敏"}},
  "expected": {
    "must_contain_uri": "personal://饮食禁忌",
    "must_rank_within": 3,
    "content_substring": "花生"
  },
  "profile": "B"
}
```

## 4. Scoring Criteria

### 4.1 Per-Scenario Metrics

| Metric | Definition | Range |
|---|---|---|
| `hit` | Expected URI appears in top-k results | 0/1 |
| `rank` | Position of expected URI (1-indexed, 0 if miss) | 0..k |
| `mrr` | 1/rank if hit, else 0 | [0,1] |
| `content_match` | Expected substring found in returned content | 0/1 |
| `latency_ms` | Wall-clock time for the MCP tool call | ms |
| `guard_correct` | For SC07: write guard correctly triggered | 0/1 |
| `delete_verified` | For SC10: deleted item absent from search | 0/1 |

### 4.2 Aggregate Metrics

| Metric | Formula |
|---|---|
| HR@k | mean(hit) across all scenarios |
| MRR | mean(mrr) across all scenarios |
| Precision@1 | mean(rank==1) across scenarios where hit=1 |
| Guard Accuracy | mean(guard_correct) across SC07 scenarios |
| Delete Correctness | mean(delete_verified) across SC10 scenarios |
| P95 Latency | 95th percentile of latency_ms |

### 4.3 Profile Matrix

Each scenario runs on profiles B, C, D (A is keyword-only, not representative of real usage).

- **Profile B** — bootstrap baseline (zero-config, hash embedding, no external API)
- **Profile C** — provider-ready long-term recommended (API embedding)
- **Profile D** — full advanced-suite (API embedding + reranker, remote-API scenarios)

## 5. Comparison Baselines

### 5.1 Filesystem Keyword Baseline (fs_keyword_baseline) — HISTORICAL

The same scenario matrix is used for a filesystem keyword baseline.
This is NOT the actual OpenClaw native memory implementation — it models
file-based memory with keyword grep search.  Kept as historical reference.
Equivalences:

| MCP Tool | Filesystem Keyword Equivalent |
|---|---|
| `create_memory` | Write to `memory/*.md` file |
| `search_memory` | Grep/glob across `memory/*.md` + `USER.md` + `MEMORY.md` |
| `read_memory` | Read specific `memory/*.md` file |
| `update_memory` | Edit `memory/*.md` file content |
| `delete_memory` | Delete `memory/*.md` file |
| `add_alias` | Add entry to `MEMORY.md` index |
| `compact_context` | No native equivalent (advantage to MP) |

### 5.2 Memory-Core Replica Baseline (native_memory_core_replica)

A deterministic keyword-only replica approximating OpenClaw memory-core's
search semantics.  Uses SQLite FTS5/BM25 scoring + temporal-decay, matching
the default memory-core configuration (no embedding provider).

This is still NOT a "real OpenClaw native run" — it replicates the search
engine in isolation, without the full OpenClaw host/gateway/agent loop,
plugin routing, provider health, or session/hook behavior.

| MCP Tool | Memory-Core Replica Equivalent |
|---|---|
| `create_memory` | Write file + chunk + FTS5 index |
| `search_memory` | FTS5 BM25 search + temporal decay |
| `read_memory` | Direct file read |
| `update_memory` | Edit file + re-chunk + re-index |
| `delete_memory` | Delete file + remove from index |
| `add_alias` | Add index entry in MEMORY.md |
| `compact_context` | No native equivalent (advantage to MP) |

Harness: `test_e2e_native_memory_core.py`
Engine: `helpers/native_memory_core_replica.py`

## 6. Implementation Status

| Artifact | Status |
|---|---|
| `e2e_blackbox_scenarios.jsonl` | DONE — 18 scenarios across 12 categories |
| `test_e2e_blackbox_harness.py` | DONE — MCP stdio harness, profile B/C/D |
| `e2e_blackbox_report.json` | DONE — baseline established |
| `test_e2e_native_harness.py` | DONE — fs_keyword_baseline (historical reference, NOT actual OpenClaw native) |
| `e2e_native_report.json` | DONE — fs_keyword_baseline report (historical) |
| `e2e_comparison_report.json` | DONE — MP vs fs_keyword_baseline comparison (historical) |
| `test_e2e_native_memory_core.py` | DONE — memory-core replica baseline harness |
| `helpers/native_memory_core_replica.py` | DONE — FTS5/BM25 + temporal-decay replica engine |
| `helpers/e2e_eval.py` | DONE — shared eval helpers (ScenarioResult, eval_*, aggregate) |
| `e2e_native_memory_core_report.json` | DONE — memory-core replica baseline report |
| `e2e_mp_vs_native_core_comparison.json` | DONE — MP vs memory-core replica comparison |
| `test_e2e_real_native_calibration.py` | DONE — Tier 2 real memory-core calibration harness |
| `e2e_real_native_calibration.json` | DONE — Tier 2 calibration report (93.3% agreement) |
| `e2e_hard_corpus.jsonl` | DONE — 60 entries (40 target + 20 noise) |
| `e2e_hard_queries.jsonl` | FROZEN — 20 hard queries (zero keyword overlap) |
| `e2e_hard_holdout.jsonl` | DONE — 8 holdout queries for validation |
| `test_e2e_hard_benchmark.py` | DONE — hard-mode harness, all profiles + fs |
| `test_e2e_hard_holdout.py` | DONE — holdout validation harness |
| `test_factual_fallback_regression.py` | DONE — 3 regression tests for fallback boundary |

## 7. First Baseline Results (2026-04-06)

| Profile | HR | MRR | ContentMatch | P95 Latency | Misses |
|---|---|---|---|---|---|
| B | 0.722 | 0.418 | 0.444 | 26ms | SC06, SC08, SC01_005, SC01_006, SC12 |
| C | 0.833 | 0.582 | 0.556 | 213ms | SC04, SC06, SC12 |
| D | 0.889 | 0.892 | 0.611 | 422ms | SC04, SC06 |

Key findings:
- MRR is monotonically increasing: B(0.418) < C(0.582) < D(0.892)
- SC06 (temporal) fails on all profiles — consistent with white-box TR1 weakness
- SC04 (cross_domain multi-target) fails on C/D — needs investigation
- D has highest HR and dramatically better MRR (almost all hits at rank 1)
- SC12 (scale stress with 20 memories) passes only on D — reranker provides edge at scale

## 8. Codex Cross-Review Fixes (2026-04-06)

| Fix | Severity | What changed |
|---|---|---|
| `_eval_delete` None guard | Critical | Added explicit None check for `must_not_contain_uri` |
| Multi-URI MRR calculation | Critical | Changed from recall ratio to mean reciprocal rank |
| Guard response logging | Critical | Added `guard_target_uri` + full response on failure |

## 9. Hard-Mode Benchmark (2026-04-06) — FROZEN

### Design

60-entry corpus (40 target + 20 noise) + 20 queries with **zero keyword overlap**
(paraphrase, intent routing, cross-reference). This tests semantic/paraphrase
retrieval ability, not keyword matching. A separate 8-query holdout set validates
that results are not overfit to the eval set.

### Comparison baseline

The comparison target is a **filesystem keyword baseline** (MEMORY.md + memory/*.md
with CJK bigram grep). This is NOT the actual OpenClaw native memory implementation.
Results do not support claims of "MP superiority over native memory" — only that
MP-C/D outperforms keyword-based file search on zero-keyword-overlap queries.

### Strategy corrections applied

| Fix | Scope | What changed |
|---|---|---|
| "怎么了" intent reclassification | `sqlite_client.py` L232 | Moved from `_TEMPORAL_IMPLICIT_PATTERNS` to `_CAUSAL_IMPLICIT_PATTERNS`. Without explicit time anchor, "怎么了" triggers causal (vector=0.52) not temporal (recency=0.38). |
| factual_high_precision zero-text fallback | `sqlite_client.py` ~L7360 | When `strategy=factual_high_precision` and `max(text_score) < 0.01`, fall back to default hybrid weights (vector=0.70). Prevents vector signal suppression when text matching produces no signal. |

Both corrections are **global strategy fixes**, not single-query special cases.
The factual fallback activates on all 20 hard queries because the eval set is
designed with zero keyword overlap → text_score=0 across the board.

### Frozen results (eval set, 20 queries)

| System | HR | MRR |
|---|---|---|
| MP-D (api+reranker) | 1.000 | 0.950 |
| MP-C (api embed) | 0.950 | 0.800 |
| fs_keyword | 0.800 | 0.579 |
| MP-B (hash) | 0.300 | 0.103 |

B < C < D MRR monotonic: 0.103 < 0.800 < 0.950.

### Holdout validation (8 queries, not used in any tuning)

| System | HR | MRR |
|---|---|---|
| MP-D | 1.000 | 1.000 |
| MP-C | 1.000 | 0.833 |
| fs_keyword | 0.500 | 0.212 |
| MP-B | 0.250 | 0.035 |

### Conclusion boundary

In zero-keyword-overlap, high-semantic-paraphrase scenarios:
- MP-C/D significantly outperform filesystem keyword baseline
- MP-B (hash embedding) is not competitive in this regime
- The factual fallback is the dominant scoring path in this eval set (20/20 trigger)
- This does NOT prove MP is generally superior to OpenClaw native memory
- This DOES prove MP's semantic retrieval provides value when queries
  and content have no shared surface forms

## 10. Native Memory-Core Comparison (2026-04-06)

### 10.1 Benchmark Structure

| Tier | Role | Engine | Deterministic | Reports |
|---|---|---|---|---|
| **Tier 1** (primary) | Repeatable benchmark | `NativeMemoryCoreReplica` — FTS5/BM25 keyword-only | Yes | `e2e_native_memory_core_report.json`, `e2e_mp_vs_native_core_comparison.json` |
| **Tier 2** (calibration) | Real-engine spot-check | OpenClaw `memory-core` via isolated CLI | No (depends on provider) | `e2e_real_native_calibration.json` |

**The primary benchmark is the Tier 1 replica.**  Tier 2 is a calibration layer
that validates the replica's fidelity against the real engine.  Tier 2 does not
replace the primary benchmark and should not be cited as "actual OpenClaw native
benchmark" on its own.

### 10.2 Tier 2 Calibration Conditions

| Condition | Value |
|---|---|
| OpenClaw version | 2026.3.28 |
| Profile | `--profile bench-native` (isolated state under `~/.openclaw-bench-native/`) |
| Workspace | `~/.openclaw/workspace-bench-native/` (isolated, 25–30 scenario files) |
| Active memory plugin | `memory-core` (`plugins.slots.memory = "memory-core"`) |
| `memory-palace` status | **Disabled** (`plugins.entries` does not load MP) |
| Embedding provider | `ollama` → `qwen3-embedding:8b-q8_0-ctx8192` (1024-dim, local) |
| Vector search | **Enabled** (sqlite-vec native KNN) |
| FTS5 | **Enabled** |
| Search interface | `openclaw --profile bench-native memory search --query "..." --json` |
| Scenarios | 15 comparable (SC07/SC09/SC10 excluded: no native equivalent or CLI limitation) |

### 10.3 Tier 1 Results: MP vs NativeMemoryCoreReplica

| System | HR | MRR | Denominator | Notes |
|---|---|---|---|---|
| NativeMemoryCoreReplica | 0.938 | 0.875 | 16 comparable | Keyword-only, no vector |
| MP Profile B | 0.944 | 0.670 | 18 all | Hash embedding |
| MP Profile C | 0.944 | 0.922 | 18 all | API embedding |
| MP Profile D | 0.944 | 0.894 | 18 all | API embedding + reranker |

**Denominator note:** MP aggregate uses all 18 scenarios; replica uses 16 comparable
(SC07 conflict_guard and SC09 compact_recall have no native equivalent).

**C→D on this corpus:** MRR decreased from 0.922 to 0.894.  The reranker did not
show stable advantage on this 18-scenario small corpus.  This should be reported
as "reranker did not stably demonstrate advantage at this scale", not as a general
conclusion about reranker negative impact.

### 10.4 Tier 2 Calibration: Replica vs Real Native

| Metric | Replica (keyword-only) | Real Native (FTS5+vector) | Delta |
|---|---|---|---|
| HR | 0.938 (15/16) | 1.000 (15/15) | +0.062 |
| MRR | 0.875 | 0.967 | +0.092 |
| Agreement rate | — | — | **93.3% (14/15)** |

#### Agreement Audit (denominator = 15 comparable scenarios)

| Scenario | Category | Replica | Real Native | Agreement | Reason |
|---|---|---|---|---|---|
| SC01_001 | store_recall | HIT | HIT | Yes | — |
| SC01_002 | store_recall | HIT | HIT | Yes | — |
| SC01_003 | store_recall | HIT | HIT | Yes | — |
| SC01_004 | store_recall | HIT | HIT | Yes | — |
| SC01_005 | store_recall | HIT | HIT | Yes | — |
| SC01_006 | store_recall | HIT | HIT | Yes | — |
| SC02_001 | auto_capture_recall | HIT | HIT | Yes | — |
| SC02_002 | auto_capture_recall | HIT | HIT | Yes | — |
| SC03_001 | update_recall | HIT | HIT | Yes | — |
| SC04_001 | cross_domain | HIT | HIT | Yes | — |
| SC05_001 | alias_recall | HIT | HIT | Yes | — |
| SC06_001 | temporal_recall | HIT | HIT | Yes | Real native MRR=1.000 vs replica MRR=0.500 (vector ranking advantage) |
| **SC08_001** | **session_continuity** | **MISS** | **HIT** | **No** | **Vector search resolved CJK semantic gap that keyword-only FTS5 could not** |
| SC11_001 | namespace_read | HIT | HIT | Yes | — |
| SC12_001 | scale_stress | HIT | HIT | Yes | — |

**Disagreement classification:**
- 1 disagreement (SC08): **vector advantage** — the CJK query "我的运动习惯是什么"
  shares minimal keyword overlap with content "每天早上6点起床跑步5公里".
  Real memory-core's vector search (qwen3-embedding) resolves this semantic gap.
  The keyword-only replica cannot match this without embedding support.

### 10.5 Authoritative Conclusion Boundary

1. `NativeMemoryCoreReplica` is a **keyword-only approximation of memory-core
   search semantics**.  It replicates the FTS5/BM25 keyword path with temporal
   decay but does NOT include vector search, MMR re-ranking, or the full
   OpenClaw host/gateway/agent loop.

2. It has been calibrated against real OpenClaw memory-core in a limited scope
   (15 comparable scenarios, isolated profile, single embedding provider).
   The calibration shows **93.3% hit/miss agreement** with a single identified
   gap attributable to vector search.

3. **The replica should not be represented as a complete equivalent of real
   OpenClaw native memory behavior.**  It is a controlled benchmark baseline
   for measuring MP's relative retrieval positioning.

4. **Corrected positioning (accounting for Tier 2 calibration):**
   - Real native memory-core (with vector): HR≈1.000, MRR≈0.967
   - MP Profile C: HR=0.944, MRR=0.922
   - MP Profile D: HR=0.944, MRR=0.894
   - On this 18-scenario corpus, **real native memory-core with vector search
     outperforms MP on MRR**.  MP's differentiated value lies in capabilities
     that native memory-core does not provide: `compact_context`, `write_guard`,
     structured URI namespace, multi-stage scoring pipeline, and gist recall.

5. The C→D MRR decrease (0.922→0.894) is observed on this small corpus only.
   It should be described as "reranker did not stably demonstrate advantage
   at this scale", not as evidence of reranker negative impact in general.

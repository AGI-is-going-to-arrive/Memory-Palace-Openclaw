"""Memory-Native Benchmark — P1b — Layer B (Session-Merge Algorithm)

Validates the session-merge algorithm via search_memory_impl with full
dependency injection. This tests session-merge algorithm quality — NOT
the public MCP tool surface (which also depends on transport/context).

Gates validated:
7. search_memory_impl full chain — session_queue_count > 0,
   session_first_metrics.session_contributed_before_truncation > 0
8. (via Layer A) Hierarchy parent chain
5. (from P1a) Session cache injection + get_session_id patch

Approach A for VALID_DOMAINS: patch mcp_server.VALID_DOMAINS +
mcp_server_config.VALID_DOMAINS, then continue using mcp_server.parse_uri.

Spec: docs/MEMORY_NATIVE_BENCHMARK_SPEC.md v3.6.1 §2.2, §10.2
"""

import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

_BACKEND = Path(__file__).resolve().parent.parent.parent
_BENCH_DIR = Path(__file__).resolve().parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
if str(_BENCH_DIR) not in sys.path:
    sys.path.insert(0, str(_BENCH_DIR))

_FIXTURES = _BACKEND / "tests" / "fixtures"
_REPORTS = Path(__file__).resolve().parent

BENCH_DOMAINS = (
    "core,writer,game,notes,system,"
    "personal,project,writing,research,finance,learning"
)
BENCH_DOMAIN_LIST = [d.strip() for d in BENCH_DOMAINS.split(",")]


def _load_jsonl(name: str) -> List[Dict]:
    with open(_FIXTURES / name) as f:
        return [json.loads(line) for line in f if line.strip()]


def _split_uri(uri: str) -> Tuple[str, str]:
    d, p = uri.split("://", 1)
    return d, p


def _db_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path}"


@pytest.mark.asyncio
async def test_memory_native_layer_b(tmp_path, monkeypatch):
    """P1b: Validate session-merge algorithm via search_memory_impl.

    This test validates the full search_memory_impl pipeline with session
    injection, NOT just the injection mechanism.

    Conclusion boundary: this validates "session-merge algorithm quality",
    NOT "public MCP tool surface is fully covered".
    """
    # --- Approach A: patch VALID_DOMAINS at module level ---
    monkeypatch.setenv("VALID_DOMAINS", BENCH_DOMAINS)
    monkeypatch.setenv("RETRIEVAL_EMBEDDING_BACKEND", "hash")

    import mcp_server as ms
    import mcp_server_config as msc

    monkeypatch.setattr(msc, "VALID_DOMAINS", BENCH_DOMAIN_LIST)
    monkeypatch.setattr(ms, "VALID_DOMAINS", BENCH_DOMAIN_LIST)

    from db.sqlite_client import SQLiteClient
    from mcp_tool_search import search_memory_impl
    from runtime_state import runtime_state as rs

    # --- Step 1: Populate corpus ---
    db = tmp_path / "mn_p1b_session.db"
    client = SQLiteClient(_db_url(db))
    await client.init_db()

    corpus = _load_jsonl("memory_native_corpus_p1a.jsonl")
    # Use populate_corpus from Layer A (import the shared helper)
    from test_memory_native_benchmark import populate_corpus, register_aliases

    id_map = await populate_corpus(client, corpus)
    aliases = _load_jsonl("memory_native_alias_specs_p1a.jsonl")
    await register_aliases(client, aliases, id_map)

    # Create an unindexed session anchor memory.
    # index_now=False means it exists in DB (passes revalidation in
    # _revalidate_search_results) but has NO FTS/vector index entries,
    # so it will NEVER appear in global search results. This avoids
    # the URI collision problem in small-corpus benchmarks.
    anchor = await client.create_memory(
        parent_path="",
        content="[session test anchor — not indexed]",
        priority=99,
        title="session-anchor",
        domain="personal",
        index_now=False,
    )
    id_map["__session_anchor__"] = (anchor["id"], anchor["uri"])

    # --- Step 2: Inject session cache entries ---
    session_fixtures = _load_jsonl("memory_native_session_fixture_p1a.jsonl")
    queries = [
        q for q in _load_jsonl("memory_native_queries_p1a.jsonl")
        if q.get("layer") == "B"
    ]

    test_session_id = "bench_sess_p1b_layer_b"

    for sf in session_fixtures:
        mid = None
        if sf.get("fixture_id") and sf["fixture_id"] in id_map:
            mid = id_map[sf["fixture_id"]][0]
        await rs.session_cache.record_hit(
            session_id=test_session_id,
            uri=sf["uri"],
            memory_id=mid,
            snippet=sf["content"],
            priority=sf.get("priority"),
            source="benchmark_inject",
        )

    # --- Step 3: Patch get_session_id ---
    monkeypatch.setattr(ms, "get_session_id", lambda: test_session_id)
    assert ms.get_session_id() == test_session_id

    # --- Step 4: Call search_memory_impl with full DI ---
    gate_results: Dict[str, Any] = {
        "session_inject": True,
        "get_session_id_patch": True,
        "search_memory_impl_called": False,
        "session_merge_assertions": [],
        "per_query": [],
    }

    for q in queries:
        rel_fids = q.get("expected_memory_ids", [])
        rel_mids = {id_map[f][0] for f in rel_fids if f in id_map}

        t0 = time.perf_counter()
        result_json_str = await search_memory_impl(
            query=q["query"],
            mode="hybrid",
            max_results=10,
            candidate_multiplier=8,
            include_session=True,
            filters=q.get("filters"),
            scope_hint=None,
            verbose=True,
            # --- DI: override only what's needed ---
            to_json=ms._to_json,
            get_sqlite_client=lambda: client,
            runtime_state=rs,
            get_session_id=lambda: test_session_id,
            try_client_method_variants=ms._try_client_method_variants,
            merge_session_global_results=ms._merge_session_global_results,
            search_result_identity=ms._search_result_identity,
            safe_int=ms._safe_int,
            record_session_hit=ms._record_session_hit,
            record_flush_event=ms._record_flush_event,
            parse_uri=ms.parse_uri,
            make_uri=ms.make_uri,
            valid_domains=BENCH_DOMAIN_LIST,
            default_search_mode=ms.DEFAULT_SEARCH_MODE,
            allowed_search_modes=ms.ALLOWED_SEARCH_MODES,
            default_search_max_results=ms.DEFAULT_SEARCH_MAX_RESULTS,
            default_search_candidate_multiplier=ms.DEFAULT_SEARCH_CANDIDATE_MULTIPLIER,
            search_hard_max_results=ms.SEARCH_HARD_MAX_RESULTS,
            search_hard_max_candidate_multiplier=ms.SEARCH_HARD_MAX_CANDIDATE_MULTIPLIER,
            enable_session_first_search=True,
            intent_llm_enabled=False,
        )
        lat = (time.perf_counter() - t0) * 1000

        gate_results["search_memory_impl_called"] = True

        # Parse the JSON return to extract session merge metadata
        result_payload = json.loads(result_json_str)

        sqc = result_payload.get("session_queue_count", 0)
        sfm = result_payload.get("session_first_metrics", {})
        scbt = sfm.get("session_contributed_before_truncation", 0)
        sc = sfm.get("session_contributed", 0)

        # Check long-term hit
        results_list = result_payload.get("results", [])
        rmids = [r.get("memory_id") for r in results_list if r.get("memory_id")]
        lt_hit = any(m in rel_mids for m in rmids[:10])

        gate_results["per_query"].append({
            "case_id": q["case_id"],
            "taxonomy": q["taxonomy_code"],
            "session_queue_count": sqc,
            "session_contributed_before_truncation": scbt,
            "session_contributed": sc,
            "long_term_hit": lt_hit,
            "result_count": len(results_list),
            "latency_ms": round(lat, 1),
        })

        gate_results["session_merge_assertions"].append({
            "case_id": q["case_id"],
            "sqc_gt_0": sqc > 0,
            "scbt_gt_0": scbt > 0,
        })

    # --- Step 5: Write report ---
    report = {
        "benchmark": "memory_native_p1b",
        "layer": "B",
        "conclusion_boundary": (
            "Validates session-merge algorithm quality via search_memory_impl. "
            "Does NOT validate public MCP tool surface end-to-end."
        ),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "gates": gate_results,
    }
    with open(_REPORTS / "memory_native_p1b_session_report.json", "w") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    # --- Hard assertions (Gate 7) ---
    assert gate_results["search_memory_impl_called"], (
        "search_memory_impl was never called"
    )

    for pq in gate_results["per_query"]:
        # All queries: session_queue_count > 0 (proves session participated in merge)
        assert pq["session_queue_count"] > 0, (
            f"{pq['case_id']}: session_queue_count must be > 0"
        )

        if pq["taxonomy"] == "M1":
            # M1 (Session Boost): session entries MUST survive into merged results
            assert pq["session_contributed_before_truncation"] > 0, (
                f"{pq['case_id']}: session_contributed_before_truncation must be > 0 "
                f"(Session Boost case — session entry must appear in merged results)"
            )
        elif pq["taxonomy"] == "M2":
            # M2 (Long-Term Override): authoritative long-term memory MUST survive
            # despite session noise. session_contributed may be 0 (expected when
            # session URIs collide with global results in small corpus).
            assert pq["long_term_hit"], (
                f"{pq['case_id']}: long_term_hit must be true "
                f"(Long-Term Override — authoritative memory must survive)"
            )

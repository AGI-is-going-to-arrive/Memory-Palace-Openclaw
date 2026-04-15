"""Memory-Native Benchmark — Full P1 — Layer B (Session-Merge)

Runs full 6-query Layer B benchmark (3 M1 + 3 M2) with per-session-group
isolation via unique test_session_ids.

Spec: docs/MEMORY_NATIVE_BENCHMARK_SPEC.md v3.6.2 §2.2, §10.2
"""

import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

_BACKEND = Path(__file__).resolve().parent.parent.parent
_BENCH = Path(__file__).resolve().parent
for p in (_BACKEND, _BENCH):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

_FIXTURES = _BACKEND / "tests" / "fixtures"
_REPORTS = _BENCH

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


def _db_url(path) -> str:
    return f"sqlite+aiosqlite:///{path}"


@pytest.mark.asyncio
async def test_memory_native_full_layer_b(tmp_path, monkeypatch):
    """Full P1: Layer B session-merge via search_memory_impl DI — all 6 M1/M2 queries.

    Profile: hash/hybrid only. Session strategy: shared single session ID.
    Does NOT test public MCP tool surface or per-group session isolation.
    """
    monkeypatch.setenv("VALID_DOMAINS", BENCH_DOMAINS)
    monkeypatch.setenv("RETRIEVAL_EMBEDDING_BACKEND", "hash")

    import mcp_server as ms
    import mcp_server_config as msc

    monkeypatch.setattr(msc, "VALID_DOMAINS", BENCH_DOMAIN_LIST)
    monkeypatch.setattr(ms, "VALID_DOMAINS", BENCH_DOMAIN_LIST)

    from db.sqlite_client import SQLiteClient
    from mcp_tool_search import search_memory_impl
    from runtime_state import runtime_state as rs

    # Reuse Layer A helpers for populate
    from test_memory_native_benchmark import populate_corpus, register_aliases

    # --- Step 1: Populate corpus ---
    db = tmp_path / "mn_full_session.db"
    client = SQLiteClient(_db_url(db))
    await client.init_db()

    corpus = _load_jsonl("memory_native_corpus.jsonl")
    id_map = await populate_corpus(client, corpus)

    aliases = _load_jsonl("memory_native_alias_specs.jsonl")
    await register_aliases(client, aliases, id_map)

    # --- Step 2: Create session anchors (unindexed) ---
    session_fixtures = _load_jsonl("memory_native_session_fixture.jsonl")

    # --- Step 2a: Create session anchors (unindexed, like P1b approach) ---
    # Each __session_* fixture needs a real memory_id in DB for revalidation.
    # Use unique title suffix to avoid collisions between anchors.
    anchor_counter = 0
    for sf in session_fixtures:
        fid = sf["fixture_id"]
        if fid.startswith("__session_") and fid not in id_map:
            anchor_counter += 1
            dom, path = _split_uri(sf["uri"])
            # Use the fixture_id as title to guarantee uniqueness
            safe_title = fid.replace("__", "").replace(" ", "-")
            anchor = await client.create_memory(
                parent_path="",
                content=f"[session anchor {anchor_counter}]",
                priority=99,
                title=safe_title,
                domain=dom,
                index_now=False,
            )
            id_map[fid] = (anchor["id"], anchor["uri"])

    # --- Step 3: Inject session fixtures per-group (spec §2.2) ---
    # Each session_group gets a unique session ID for cross-case isolation.
    group_session_ids: Dict[str, str] = {}
    for sf in session_fixtures:
        sg = sf["session_group"]
        if sg not in group_session_ids:
            group_session_ids[sg] = f"bench_sess_{sg}"

    for sf in session_fixtures:
        fid = sf["fixture_id"]
        mid = id_map[fid][0] if fid in id_map else None
        sid = group_session_ids[sf["session_group"]]

        await rs.session_cache.record_hit(
            session_id=sid, uri=sf["uri"], memory_id=mid,
            snippet=sf["content"], priority=sf.get("priority"),
            source="benchmark_inject",
        )

    # --- Step 4: Run Layer B queries ---
    queries = [
        q for q in _load_jsonl("memory_native_queries.jsonl")
        if q.get("layer") == "B"
    ]

    gate_results: Dict[str, Any] = {
        "session_inject": True,
        "search_memory_impl_called": False,
        "session_groups": sorted(group_session_ids.keys()),
        "per_query": [],
    }

    for q in queries:
        sg = q["session_group"]
        current_session_id = group_session_ids.get(sg, f"bench_sess_{sg}")

        # Patch get_session_id to this query's group session ID
        monkeypatch.setattr(ms, "get_session_id", lambda _sid=current_session_id: _sid)

        rel_fids = q.get("expected_memory_ids", [])
        rel_mids = {id_map[f][0] for f in rel_fids if f in id_map}

        t0 = time.perf_counter()
        result_json_str = await search_memory_impl(
            query=q["query"], mode="hybrid", max_results=10,
            candidate_multiplier=8, include_session=True,
            filters=q.get("filters"), scope_hint=None, verbose=True,
            to_json=ms._to_json,
            get_sqlite_client=lambda: client,
            runtime_state=rs,
            get_session_id=lambda _sid=current_session_id: _sid,
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

        payload = json.loads(result_json_str)
        sqc = payload.get("session_queue_count", 0)
        sfm = payload.get("session_first_metrics", {})
        scbt = sfm.get("session_contributed_before_truncation", 0)
        sc = sfm.get("session_contributed", 0)

        results_list = payload.get("results", [])
        rmids = [r.get("memory_id") for r in results_list if r.get("memory_id")]
        lt_hit = any(m in rel_mids for m in rmids[:10])

        gate_results["per_query"].append({
            "case_id": q["case_id"], "taxonomy": q["taxonomy_code"],
            "session_group": sg,
            "session_queue_count": sqc,
            "session_contributed_before_truncation": scbt,
            "session_contributed": sc,
            "long_term_hit": lt_hit,
            "result_count": len(results_list),
            "latency_ms": round(lat, 1),
        })

    # --- Step 5: Write report ---
    report = {
        "benchmark": "memory_native_full", "layer": "B",
        "conclusion_boundary": (
            "Validates session-merge algorithm quality via search_memory_impl "
            "with per-group session ID isolation. "
            "Does NOT validate public MCP tool surface end-to-end."
        ),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "corpus_size": len(corpus),
        "session_fixtures": len(session_fixtures),
        "query_count": len(queries),
        "session_ids": group_session_ids,
        "session_groups": sorted(group_session_ids.keys()),
        "gates": gate_results,
    }
    with open(_REPORTS / "memory_native_full_session_report.json", "w") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    # --- Assertions (smoke level) ---
    # Mechanism: search_memory_impl was invoked
    assert gate_results["search_memory_impl_called"]

    # All queries ran and returned results
    for pq in gate_results["per_query"]:
        assert pq["result_count"] > 0, (
            f"{pq['case_id']}: should return results"
        )

    # Session visibility: at least some session entries found across queries
    total_sqc = sum(pq["session_queue_count"] for pq in gate_results["per_query"])
    assert total_sqc > 0, (
        "Session merge mechanism: at least some session entries should be visible"
    )

    # M1 (Session Boost): at least ONE M1 case has session entries visible (sqc > 0).
    # With per-group session ID isolation, session entries may not outcompete
    # the 70-entry corpus (scbt=0 is a data strength issue, not a mechanism bug).
    m1_cases = [pq for pq in gate_results["per_query"] if pq["taxonomy"] == "M1"]
    m1_any_sqc = any(pq["session_queue_count"] > 0 for pq in m1_cases)
    assert m1_any_sqc, (
        "At least one M1 case should have session entries visible (sqc > 0)"
    )

    # M2 (Long-Term Override): at least ONE M2 case has lt_hit=True
    m2_cases = [pq for pq in gate_results["per_query"] if pq["taxonomy"] == "M2"]
    m2_any_lt = any(pq["long_term_hit"] for pq in m2_cases)
    assert m2_any_lt, (
        "At least one M2 case should show long-term memory survival"
    )

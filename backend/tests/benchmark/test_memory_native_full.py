"""Memory-Native Benchmark — Full P1 — Layer A (Pure Retrieval)

Runs full 70-corpus / 48-query (Layer A) benchmark.
Profiles A/B always run. C/D run when embedding + reranker env vars are configured.

Spec: docs/MEMORY_NATIVE_BENCHMARK_SPEC.md v3.6.2 §10.2
"""

import asyncio
import copy
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

_BACKEND = Path(__file__).resolve().parent.parent.parent
_BENCH = Path(__file__).resolve().parent
for p in (_BACKEND, _BENCH):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from db.sqlite_client import SQLiteClient

# Reuse P1b helpers (avoids duplicating populate/metrics/profile logic)
from test_memory_native_benchmark import (
    BENCH_DOMAINS,
    _created_namespaces,
    _db_url,
    _detect_profiles_and_snapshot,
    _configure_profile,
    _ensure_parent_chain,
    _load_jsonl,
    _path_matches_prefix,
    _split_uri,
    compute_metrics,
    register_aliases,
)

_REPORTS = _BENCH
_LIVE_BENCHMARK_ENV_KEYS = (
    "OPENCLAW_ENABLE_LIVE_BENCHMARK",
    "RELEASE_GATE_ENABLE_LIVE_BENCHMARK",
)


def _live_benchmark_requested() -> bool:
    return any(
        str(os.environ.get(key, "")).strip().lower() in {"1", "true", "yes", "on"}
        for key in _LIVE_BENCHMARK_ENV_KEYS
    )


def _skip_unless_live_benchmark_requested() -> None:
    if _live_benchmark_requested():
        return
    pytest.skip(
        "Memory-native live benchmark rerun is disabled by default; "
        "set OPENCLAW_ENABLE_LIVE_BENCHMARK=1 to run.",
    )

# ---------------------------------------------------------------------------
# Extended populate — tracks version-group v2 timestamps for TF1 resolution
# ---------------------------------------------------------------------------


async def _populate_corpus_tracked(
    client: SQLiteClient, corpus: List[Dict],
) -> Tuple[Dict[str, Tuple[int, str]], Dict[str, str]]:
    """Write corpus, return (fixture_id->(mid,uri), version_group->v2_timestamp)."""
    _created_namespaces.clear()
    id_map: Dict[str, Tuple[int, str]] = {}
    vg_v2_ts: Dict[str, str] = {}

    async def _create(e: Dict) -> None:
        dom, parent = _split_uri(e["parent_uri"])
        await _ensure_parent_chain(client, dom, parent)
        r = await client.create_memory(
            parent_path=parent, content=e["content"],
            priority=e.get("priority", 5), title=e["title"],
            domain=dom, index_now=True,
        )
        id_map[e["fixture_id"]] = (r["id"], r["uri"])

    grouped: Dict[str, List[Dict]] = {}
    plain: List[Dict] = []
    for e in corpus:
        gk = e.get("version_group") or e.get("conflict_group")
        if gk:
            grouped.setdefault(gk, []).append(e)
        else:
            plain.append(e)

    for e in plain:
        await _create(e)

    for gk in sorted(grouped):
        entries = sorted(
            grouped[gk], key=lambda x: (x.get("version") or 0, x["fixture_id"])
        )
        for i, e in enumerate(entries):
            if i > 0:
                await asyncio.sleep(1.0)
            await _create(e)
            # After v2 write, record timestamp for TF1 sentinel resolution
            if e.get("version") == 2 and e.get("version_group"):
                vg_v2_ts[e["version_group"]] = (
                    datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                )

    return id_map, vg_v2_ts


async def _inject_temporal_separation(
    client: SQLiteClient,
    corpus: List[Dict],
    id_map: Dict[str, Tuple[int, str]],
    days_per_version_step: float = 3.0,
) -> Dict[str, str]:
    """Backdate version_group entries so recency scoring has real signal.

    Returns version_group -> v2 backdated ISO timestamp (for TF1 sentinel).
    E.g. with 3 versions and step=3: v1=-6d, v2=-3d, v3=now.
    """
    from sqlalchemy import text

    groups: Dict[str, List[Dict]] = {}
    for e in corpus:
        vg = e.get("version_group")
        if vg:
            groups.setdefault(vg, []).append(e)

    vg_v2_ts: Dict[str, str] = {}
    now = datetime.now(timezone.utc)

    async with client.async_session() as session:
        for vg, entries in groups.items():
            max_ver = max(e.get("version") or 0 for e in entries)
            for e in entries:
                ver = e.get("version") or 0
                fid = e["fixture_id"]
                if fid not in id_map:
                    continue
                mid = id_map[fid][0]
                offset_days = (max_ver - ver) * days_per_version_step
                backdated = now - timedelta(days=offset_days)
                backdated_str = backdated.strftime("%Y-%m-%d %H:%M:%S")

                await session.execute(
                    text("UPDATE memories SET created_at = :ts WHERE id = :mid"),
                    {"ts": backdated_str, "mid": mid},
                )
                await session.execute(
                    text("UPDATE paths SET created_at = :ts WHERE memory_id = :mid"),
                    {"ts": backdated_str, "mid": mid},
                )

                if ver == 2:
                    vg_v2_ts[vg] = backdated.strftime("%Y-%m-%dT%H:%M:%SZ")

        await session.commit()

    return vg_v2_ts


def _resolve_tf1_sentinels(
    queries: List[Dict], vg_ts: Dict[str, str],
) -> None:
    """In-place replace __RUNTIME: sentinel values with actual timestamps."""
    mapping = {
        "__RUNTIME:AFTER_RISK_V2__": vg_ts.get("risk_evolution", ""),
        "__RUNTIME:AFTER_DIET_V2__": vg_ts.get("diet_evolution", ""),
        "__RUNTIME:AFTER_JP_V2__": vg_ts.get("jp_progress", ""),
    }
    for q in queries:
        filt = q.get("filters")
        if not filt:
            continue
        ua = filt.get("updated_after", "")
        if isinstance(ua, str) and ua.startswith("__RUNTIME:"):
            filt["updated_after"] = mapping.get(ua, ua)


# ---------------------------------------------------------------------------
# Aggregation / report
# ---------------------------------------------------------------------------


def _aggregate(per_query: List[Dict]) -> Tuple[Dict, Dict]:
    by_tax: Dict[str, List[Dict]] = {}
    for pq in per_query:
        by_tax.setdefault(pq["taxonomy"], []).append(pq)

    agg = {}
    for t, pqs in by_tax.items():
        n = len(pqs)
        agg[t] = {
            "n": n,
            "hr_at_10": sum(p["hr_at_10"] for p in pqs) / n,
            "mrr": sum(p["mrr"] for p in pqs) / n,
            "ndcg_at_10": sum(p["ndcg_at_10"] for p in pqs) / n,
        }

    n_all = len(per_query)
    overall = (
        {"n": n_all,
         "hr_at_10": sum(p["hr_at_10"] for p in per_query) / n_all,
         "mrr": sum(p["mrr"] for p in per_query) / n_all,
         "ndcg_at_10": sum(p["ndcg_at_10"] for p in per_query) / n_all}
        if n_all else {"n": 0, "hr_at_10": 0, "mrr": 0, "ndcg_at_10": 0}
    )
    return overall, agg


def _write_md(all_results: Dict, meta: Dict, path: Path) -> None:
    md = ["# Memory-Native Benchmark Full P1 — Layer A\n"]
    md.append(f"- Corpus: {meta['corpus_size']}, Aliases: {meta['alias_count']}, "
              f"Queries: {meta['query_count']}")
    md.append(f"- Profiles: {', '.join(all_results.keys())}")
    md.append(f"- Generated: {meta['timestamp']}\n")

    for pk, pr in all_results.items():
        ov = pr["overall"]
        md.append(f"## Profile {pk}\n")
        md.append(f"**Overall** (N={ov['n']}): HR@10={ov['hr_at_10']:.3f}, "
                  f"MRR={ov['mrr']:.3f}, NDCG@10={ov['ndcg_at_10']:.3f}\n")
        md.append("| Taxonomy | N | HR@10 | MRR | NDCG@10 |")
        md.append("|---|---:|---:|---:|---:|")
        for t, td in sorted(pr["by_taxonomy"].items()):
            md.append(f"| {t} | {td['n']} | {td['hr_at_10']:.3f} | "
                      f"{td['mrr']:.3f} | {td['ndcg_at_10']:.3f} |")

        md.append("\n<details><summary>Per-query detail</summary>\n")
        md.append("| Case | Tax | HR@10 | MRR | R1 | ms |")
        md.append("|---|---|---:|---:|---|---:|")
        for pq in pr["per_query"]:
            r1 = "Y" if pq["rank_1_correct"] else "-"
            md.append(f"| {pq['case_id']} | {pq['taxonomy']} | "
                      f"{pq['hr_at_10']:.0f} | {pq['mrr']:.3f} | "
                      f"{r1} | {pq['latency_ms']:.0f} |")
        md.append("\n</details>\n")

    with open(path, "w") as f:
        f.write("\n".join(md) + "\n")


# ---------------------------------------------------------------------------
# Main test
# ---------------------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.asyncio
async def test_memory_native_full_layer_a(tmp_path, monkeypatch):
    """Full P1: Layer A — all four profiles (A/B/C/D) mandatory.

    Fail-fast if embedding or reranker env is missing. Profile D must
    demonstrate rerank_applied=true on at least one query.
    """
    _skip_unless_live_benchmark_requested()
    profiles_available, env_snap = _detect_profiles_and_snapshot()

    # Fail-fast: all four profiles are mandatory for authoritative results
    missing = [p for p in ("A", "B", "C", "D") if p not in profiles_available]
    if missing:
        env_hint = []
        if "C" in missing or "D" in missing:
            env_hint.append("RETRIEVAL_EMBEDDING_API_BASE")
        if "D" in missing:
            env_hint.append("RETRIEVAL_RERANKER_API_BASE + RETRIEVAL_RERANKER_ENABLED=true + RETRIEVAL_RERANKER_MODEL")
        pytest.fail(
            f"Benchmark requires all four profiles (A/B/C/D). "
            f"Missing: {missing}. Set env: {', '.join(env_hint)}"
        )

    profiles_to_run = ["A", "B", "C", "D"]

    monkeypatch.setenv("VALID_DOMAINS", BENCH_DOMAINS)

    corpus = _load_jsonl("memory_native_corpus.jsonl")
    aliases = _load_jsonl("memory_native_alias_specs.jsonl")
    all_queries = _load_jsonl("memory_native_queries.jsonl")
    layer_a = [q for q in all_queries if q.get("layer", "A") == "A"]

    all_results: Dict[str, Any] = {}

    for pk in profiles_to_run:
        mode = _configure_profile(pk, monkeypatch, env_snap)

        db = tmp_path / f"mn_full_{pk}.db"
        client = SQLiteClient(_db_url(db))
        await client.init_db()

        # Populate corpus
        id_map, _legacy_ts = await _populate_corpus_tracked(client, corpus)
        assert len(id_map) == len(corpus), (
            f"Expected {len(corpus)} entries, got {len(id_map)}"
        )

        await register_aliases(client, aliases, id_map)

        # Inject day-level temporal separation for version groups
        # (replaces sleep(1s) which gave zero recency signal)
        vg_ts = await _inject_temporal_separation(client, corpus, id_map)

        # Deep-copy queries and resolve TF1 sentinels
        queries = copy.deepcopy(layer_a)
        _resolve_tf1_sentinels(queries, vg_ts)

        per_query: List[Dict] = []
        for q in queries:
            rel_fids = q.get("expected_memory_ids", [])
            rel_mids = {id_map[f][0] for f in rel_fids if f in id_map}

            t0 = time.perf_counter()
            result = await client.search_advanced(
                query=q["query"], mode=mode, max_results=10,
                candidate_multiplier=8, filters=q.get("filters"),
                intent_profile={"intent": q.get("intent", "factual")},
            )
            lat = (time.perf_counter() - t0) * 1000

            rmids = [r["memory_id"] for r in result.get("results", [])
                     if r.get("memory_id") is not None]
            m = compute_metrics(rmids, rel_mids)

            r1_ok = False
            if q.get("expected_rank_1") and rmids:
                r1_mid = id_map.get(q["expected_rank_1"], (None,))[0]
                r1_ok = rmids[0] == r1_mid

            scope_prec = None
            filt = q.get("filters") or {}
            rlist = result.get("results", [])
            if rlist:
                if "domain" in filt:
                    ok = sum(1 for r in rlist
                             if r.get("uri", "").startswith(f"{filt['domain']}://"))
                    scope_prec = ok / len(rlist)
                elif "path_prefix" in filt:
                    ok = sum(1 for r in rlist
                             if _path_matches_prefix(r.get("uri", ""), filt["path_prefix"]))
                    scope_prec = ok / len(rlist)

            meta = result.get("metadata", {})
            top_k_details = []
            for r in rlist[:10]:
                detail = {
                    "uri": r.get("uri", ""),
                    "memory_id": r.get("memory_id"),
                    "score": round(float(r.get("score") or 0), 6),
                }
                scores = r.get("scores")
                if isinstance(scores, dict):
                    detail["scores"] = {
                        k: round(float(v or 0), 6) for k, v in scores.items()
                    }
                top_k_details.append(detail)

            per_query.append({
                "case_id": q["case_id"], "taxonomy": q["taxonomy_code"],
                "gap": q.get("gap_dimension", []), "lang": q.get("lang", ""),
                **m, "rank_1_correct": r1_ok, "scope_precision": scope_prec,
                "latency_ms": round(lat, 1), "result_count": len(rmids),
                "rerank_applied": meta.get("rerank_applied", False),
                "degraded": meta.get("degraded", False),
                "degrade_reasons": meta.get("degrade_reasons", []),
                "semantic_search_unavailable": meta.get(
                    "semantic_search_unavailable", None),
                "vector_engine_selected": meta.get(
                    "vector_engine_selected", None),
                "top_k": top_k_details,
            })

        overall, by_tax = _aggregate(per_query)
        all_results[pk] = {
            "overall": overall, "by_taxonomy": by_tax, "per_query": per_query,
        }

    # Write reports
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    meta = {
        "corpus_size": len(corpus), "alias_count": len(aliases),
        "query_count": len(layer_a), "timestamp": ts,
    }
    report = {
        "benchmark": "memory_native_full", "layer": "A", **meta,
        "profiles_available": profiles_available,
        "profiles_run": list(all_results.keys()),
        "profiles": all_results,
    }
    with open(_REPORTS / "memory_native_full_report.json", "w") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    _write_md(_REPORTS / "memory_native_full_report.md") if False else \
        _write_md(all_results, meta, _REPORTS / "memory_native_full_report.md")

    # Assertions
    assert len(all_results) == 4, (
        f"All four profiles (A/B/C/D) must run, got: {list(all_results.keys())}"
    )
    for pk, pr in all_results.items():
        assert pr["overall"]["n"] == len(layer_a), (
            f"Profile {pk}: expected {len(layer_a)} query results"
        )

    # Profile D validity: reranker must have actually fired on at least one query.
    if "D" in all_results:
        d_reranked = sum(
            1 for pq in all_results["D"]["per_query"] if pq.get("rerank_applied")
        )
        assert d_reranked > 0, (
            "Profile D: rerank_applied=true on zero queries — "
            "reranker was not actually invoked, check env var contract"
        )

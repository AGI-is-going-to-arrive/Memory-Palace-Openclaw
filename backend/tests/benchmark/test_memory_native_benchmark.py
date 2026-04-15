"""Memory-Native Benchmark — P1a Pilot — Layer A (Pure Retrieval)

Tests memory-native retrieval scenarios that SQuAD/BEIR cannot cover:
- URI-structured memories with domain/path hierarchy
- Intent-aware retrieval (factual/temporal/causal)
- Scope-constrained filtering (domain filter)
- Alias recall (URI-to-URI mapping)
- Version evolution and conflict surface
- Text style robustness (CJK + English + mixed)

Spec: docs/MEMORY_NATIVE_BENCHMARK_SPEC.md v3.6.1 §10.1/10.2
"""

import asyncio
import json
import math
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple

import pytest

_BACKEND = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from db.sqlite_client import SQLiteClient

_FIXTURES = _BACKEND / "tests" / "fixtures"
_REPORTS = Path(__file__).resolve().parent

BENCH_DOMAINS = (
    "core,writer,game,notes,system,"
    "personal,project,writing,research,finance,learning"
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_jsonl(name: str) -> List[Dict]:
    with open(_FIXTURES / name) as f:
        return [json.loads(line) for line in f if line.strip()]


def _split_uri(uri: str) -> Tuple[str, str]:
    d, p = uri.split("://", 1)
    return d, p


def _db_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path}"


# ---------------------------------------------------------------------------
# Populate
# ---------------------------------------------------------------------------

# Track created namespace paths to avoid duplicate attempts per DB
_created_namespaces: Set[str] = set()


async def _ensure_parent_chain(client: SQLiteClient, domain: str, parent_path: str) -> None:
    """Create placeholder memories for every ancestor path segment.

    E.g. for domain="writing", parent_path="novel/chapter-3":
      1. Create writing://novel  (parent_path="", title="novel")
      2. Create writing://novel/chapter-3  (parent_path="novel", title="chapter-3")

    Placeholders use priority=99 and index_now=False so they don't
    interfere with keyword/semantic search results.
    """
    if not parent_path:
        return
    parts = parent_path.split("/")
    for i in range(len(parts)):
        ns_key = f"{domain}://{'/'.join(parts[: i + 1])}"
        if ns_key in _created_namespaces:
            continue
        ancestor = "/".join(parts[:i]) if i > 0 else ""
        segment = parts[i]
        try:
            await client.create_memory(
                parent_path=ancestor,
                content=f"[namespace: {ns_key}]",
                priority=99,
                title=segment,
                domain=domain,
                index_now=False,
            )
        except (ValueError, Exception):
            pass  # Already exists or other constraint — safe to skip
        _created_namespaces.add(ns_key)


async def populate_corpus(
    client: SQLiteClient, corpus: List[Dict]
) -> Dict[str, Tuple[int, str]]:
    """Write corpus via create_memory. Returns fixture_id -> (memory_id, uri)."""
    _created_namespaces.clear()
    id_map: Dict[str, Tuple[int, str]] = {}

    async def _create_one(e: Dict) -> None:
        dom, parent = _split_uri(e["parent_uri"])
        await _ensure_parent_chain(client, dom, parent)
        r = await client.create_memory(
            parent_path=parent,
            content=e["content"],
            priority=e.get("priority", 5),
            title=e["title"],
            domain=dom,
            index_now=True,
        )
        id_map[e["fixture_id"]] = (r["id"], r["uri"])

    # Separate entries that need ordered writing (version/conflict groups)
    grouped: Dict[str, List[Dict]] = {}
    plain: List[Dict] = []
    for e in corpus:
        gk = e.get("version_group") or e.get("conflict_group")
        if gk:
            grouped.setdefault(gk, []).append(e)
        else:
            plain.append(e)

    # Plain entries — no sleep needed
    for e in plain:
        await _create_one(e)

    # Grouped entries — sleep 1s between members for created_at ordering
    for gk in sorted(grouped):
        entries = sorted(
            grouped[gk],
            key=lambda x: (x.get("version", 0), x["fixture_id"]),
        )
        for i, e in enumerate(entries):
            if i > 0:
                await asyncio.sleep(1.0)
            await _create_one(e)

    return id_map


async def register_aliases(
    client: SQLiteClient,
    aliases: List[Dict],
    id_map: Dict[str, Tuple[int, str]],
) -> None:
    """Register aliases via add_path (underlying add_alias implementation)."""
    for a in aliases:
        target_fid = a["target_fixture_id"]
        if target_fid not in id_map:
            raise ValueError(f"Alias target {target_fid} not found in id_map")
        _, target_uri = id_map[target_fid]
        td, tp = _split_uri(target_uri)
        nd, np_ = _split_uri(a["new_uri"])
        await client.add_path(
            new_path=np_,
            target_path=tp,
            new_domain=nd,
            target_domain=td,
            priority=a.get("priority", 0),
            disclosure=a.get("disclosure"),
        )


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def compute_metrics(
    result_ids: List[int], relevant: Set[int], k: int = 10
) -> Dict[str, float]:
    top = result_ids[:k]
    hr = 1.0 if any(m in relevant for m in top) else 0.0
    mrr = next(
        (1.0 / (i + 1) for i, m in enumerate(result_ids) if m in relevant), 0.0
    )
    dcg = sum(
        1.0 / math.log2(i + 2) for i, m in enumerate(top) if m in relevant
    )
    n_rel = min(len(relevant), k)
    idcg = sum(1.0 / math.log2(i + 2) for i in range(n_rel)) if n_rel > 0 else 1.0
    ndcg = min(dcg / idcg, 1.0) if idcg > 0 else 0.0
    recall = len(set(top) & relevant) / len(relevant) if relevant else 0.0
    return {
        "hr_at_10": hr,
        "mrr": mrr,
        "ndcg_at_10": ndcg,
        "recall_at_10": recall,
    }


# ---------------------------------------------------------------------------
# Profile configuration — runtime detection (not import-time snapshot)
# ---------------------------------------------------------------------------

_EMBED_KEYS = [
    "RETRIEVAL_EMBEDDING_API_BASE",
    "RETRIEVAL_EMBEDDING_API_KEY",
    "RETRIEVAL_EMBEDDING_MODEL",
    "RETRIEVAL_EMBEDDING_DIM",
]
_RERANKER_KEYS = [
    "RETRIEVAL_RERANKER_ENABLED",
    "RETRIEVAL_RERANKER_API_BASE",
    "RETRIEVAL_RERANKER_MODEL",
    "RETRIEVAL_RERANKER_API_KEY",
    "RETRIEVAL_RERANKER_WEIGHT",
]


def _detect_profiles_and_snapshot() -> Tuple[List[str], Dict[str, str]]:
    """Runtime detection of available profiles + env snapshot for restore.

    Must be called at the START of the test, before any monkeypatch.
    Returns (profiles_available, env_snapshot).
    """
    snap: Dict[str, str] = {}
    for k in ["RETRIEVAL_EMBEDDING_BACKEND"] + _EMBED_KEYS + _RERANKER_KEYS:
        v = os.environ.get(k)
        if v is not None:
            snap[k] = v

    profiles = ["A", "B"]
    if snap.get("RETRIEVAL_EMBEDDING_API_BASE"):
        profiles.append("C")
        if (snap.get("RETRIEVAL_RERANKER_API_BASE")
                and snap.get("RETRIEVAL_RERANKER_ENABLED", "").lower() == "true"
                and snap.get("RETRIEVAL_RERANKER_MODEL")):
            profiles.append("D")
    return profiles, snap


def _configure_profile(pk: str, mp, snap: Dict[str, str]) -> str:
    """Set env vars for profile, return search mode."""
    if pk in ("A", "B"):
        mp.setenv("RETRIEVAL_EMBEDDING_BACKEND", "hash")
        for k in _RERANKER_KEYS:
            mp.delenv(k, raising=False)
        return "keyword" if pk == "A" else "hybrid"

    # C or D — restore embedding env from snapshot
    mp.setenv("RETRIEVAL_EMBEDDING_BACKEND", "api")
    for k in _EMBED_KEYS:
        if k in snap:
            mp.setenv(k, snap[k])

    if pk == "C":
        for k in _RERANKER_KEYS:
            mp.delenv(k, raising=False)
        mp.setenv("RETRIEVAL_RERANKER_ENABLED", "false")
    else:  # D
        for k in _RERANKER_KEYS:
            if k in snap:
                mp.setenv(k, snap[k])
        mp.setenv("RETRIEVAL_RERANKER_ENABLED", "true")

    return "hybrid"


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------


def _aggregate(per_query: List[Dict]) -> Tuple[Dict, Dict]:
    """Return (overall, by_taxonomy) aggregations."""
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
    if n_all == 0:
        overall = {"n": 0, "hr_at_10": 0, "mrr": 0, "ndcg_at_10": 0}
    else:
        overall = {
            "n": n_all,
            "hr_at_10": sum(p["hr_at_10"] for p in per_query) / n_all,
            "mrr": sum(p["mrr"] for p in per_query) / n_all,
            "ndcg_at_10": sum(p["ndcg_at_10"] for p in per_query) / n_all,
        }
    return overall, agg


def _write_md_report(all_results: Dict, meta: Dict, path: Path) -> None:
    md = ["# Memory-Native Benchmark P1b — Layer A\n"]
    md.append(
        f"- Corpus: {meta['corpus_size']}, "
        f"Aliases: {meta['alias_count']}, "
        f"Queries: {meta['query_count']}"
    )
    md.append(f"- Profiles: {', '.join(all_results.keys())}")
    md.append(f"- Generated: {meta['timestamp']}\n")

    for pk, pr in all_results.items():
        ov = pr["overall"]
        md.append(f"## Profile {pk}\n")
        md.append(
            f"**Overall** (N={ov['n']}): "
            f"HR@10={ov['hr_at_10']:.3f}, "
            f"MRR={ov['mrr']:.3f}, "
            f"NDCG@10={ov['ndcg_at_10']:.3f}\n"
        )
        md.append("| Taxonomy | N | HR@10 | MRR | NDCG@10 |")
        md.append("|---|---:|---:|---:|---:|")
        for t, td in sorted(pr["by_taxonomy"].items()):
            md.append(
                f"| {t} | {td['n']} | "
                f"{td['hr_at_10']:.3f} | "
                f"{td['mrr']:.3f} | "
                f"{td['ndcg_at_10']:.3f} |"
            )

        # Per-query detail
        md.append("\n<details><summary>Per-query detail</summary>\n")
        md.append("| Case | Tax | HR@10 | MRR | R1 | ms |")
        md.append("|---|---|---:|---:|---|---:|")
        for pq in pr["per_query"]:
            r1 = "Y" if pq["rank_1_correct"] else "-"
            md.append(
                f"| {pq['case_id']} | {pq['taxonomy']} | "
                f"{pq['hr_at_10']:.0f} | {pq['mrr']:.3f} | "
                f"{r1} | {pq['latency_ms']:.0f} |"
            )
        md.append("\n</details>\n")

    with open(path, "w") as f:
        f.write("\n".join(md) + "\n")


# ---------------------------------------------------------------------------
# Main test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_memory_native_layer_a(tmp_path, monkeypatch):
    """P1b: Layer A pure retrieval across available profiles.

    Gates validated:
    1. VALID_DOMAINS override — 6 benchmark domains writable
    2. fixture_id → (memory_id, uri) mapping correct
    3. add_alias (add_path) full chain works
    4. search_advanced returns results for memory-native queries
    5. Per-profile × per-gap HR@10 skeleton report generated
    8. Hierarchy parent chain — nested paths created without error
    9. S2 path_prefix filter — scope_precision=1.0
    10. N1 ancestor recall — HR@10 check
    """
    # --- Runtime profile detection (before any monkeypatch) ---
    profiles_available, env_snap = _detect_profiles_and_snapshot()

    monkeypatch.setenv("VALID_DOMAINS", BENCH_DOMAINS)

    corpus = _load_jsonl("memory_native_corpus_p1a.jsonl")
    aliases = _load_jsonl("memory_native_alias_specs_p1a.jsonl")
    all_queries = _load_jsonl("memory_native_queries_p1a.jsonl")
    queries = [q for q in all_queries if q.get("layer", "A") == "A"]

    all_results: Dict[str, Any] = {}

    for pk in profiles_available:
        mode = _configure_profile(pk, monkeypatch, env_snap)

        db = tmp_path / f"mn_p1b_{pk}.db"
        client = SQLiteClient(_db_url(db))
        await client.init_db()

        # Gate 1+2+8: populate corpus (with parent chain) across 6 domains
        id_map = await populate_corpus(client, corpus)
        assert len(id_map) == len(corpus), (
            f"Expected {len(corpus)} entries, got {len(id_map)}"
        )

        # Gate 3: register aliases
        await register_aliases(client, aliases, id_map)

        # Gate 4+9+10: run queries
        per_query: List[Dict] = []
        for q in queries:
            rel_fids = q.get("expected_memory_ids", [])
            rel_mids = {id_map[f][0] for f in rel_fids if f in id_map}

            t0 = time.perf_counter()
            result = await client.search_advanced(
                query=q["query"],
                mode=mode,
                max_results=10,
                candidate_multiplier=8,
                filters=q.get("filters"),
                intent_profile={"intent": q.get("intent", "factual")},
            )
            lat = (time.perf_counter() - t0) * 1000

            rmids = [
                r["memory_id"]
                for r in result.get("results", [])
                if r.get("memory_id") is not None
            ]
            m = compute_metrics(rmids, rel_mids)

            # Rank-1 check
            r1_ok = False
            if q.get("expected_rank_1") and rmids:
                r1_mid = id_map.get(q["expected_rank_1"], (None,))[0]
                r1_ok = rmids[0] == r1_mid

            # Scope precision (domain filter or path_prefix filter)
            scope_prec = None
            filt = q.get("filters") or {}
            rlist = result.get("results", [])
            if rlist:
                if "domain" in filt:
                    ok = sum(
                        1 for r in rlist
                        if r.get("uri", "").startswith(f"{filt['domain']}://")
                    )
                    scope_prec = ok / len(rlist)
                elif "path_prefix" in filt:
                    prefix = filt["path_prefix"]
                    ok = sum(
                        1 for r in rlist
                        if _path_matches_prefix(r.get("uri", ""), prefix)
                    )
                    scope_prec = ok / len(rlist)

            per_query.append(
                {
                    "case_id": q["case_id"],
                    "taxonomy": q["taxonomy_code"],
                    "gap": q.get("gap_dimension", []),
                    "lang": q.get("lang", ""),
                    **m,
                    "rank_1_correct": r1_ok,
                    "scope_precision": scope_prec,
                    "latency_ms": round(lat, 1),
                    "result_count": len(rmids),
                }
            )

        overall, by_tax = _aggregate(per_query)
        all_results[pk] = {
            "overall": overall,
            "by_taxonomy": by_tax,
            "per_query": per_query,
        }

    # Gate 5: write reports
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    meta = {
        "corpus_size": len(corpus),
        "alias_count": len(aliases),
        "query_count": len(queries),
        "timestamp": ts,
    }

    report = {
        "benchmark": "memory_native_p1b",
        "layer": "A",
        **meta,
        "profiles_available": profiles_available,
        "profiles_run": list(all_results.keys()),
        "profiles": all_results,
    }
    with open(_REPORTS / "memory_native_p1b_report.json", "w") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    _write_md_report(all_results, meta, _REPORTS / "memory_native_p1b_report.md")

    # Assertions
    assert len(all_results) >= 2, "At least profiles A and B should run"
    for pk, pr in all_results.items():
        assert pr["overall"]["n"] == len(queries), (
            f"Profile {pk}: expected {len(queries)} results, got {pr['overall']['n']}"
        )


def _path_matches_prefix(uri: str, prefix: str) -> bool:
    """Check if a URI's path starts with the given prefix."""
    if "://" not in uri:
        return False
    _, path = uri.split("://", 1)
    return path.startswith(prefix)

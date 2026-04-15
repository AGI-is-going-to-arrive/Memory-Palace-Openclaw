#!/usr/bin/env python3
"""Memory-Native Benchmark — Parameter Sweep Runner

Three sweep groups, each varying ONE parameter on Profile C or D.
Results written to separate files — does NOT overwrite baseline artifacts.

Requires embedding + reranker env vars set externally (not hardcoded).

Usage:
    cd backend
    RETRIEVAL_EMBEDDING_API_BASE=... RETRIEVAL_EMBEDDING_API_KEY=... \
    RETRIEVAL_EMBEDDING_MODEL=... RETRIEVAL_EMBEDDING_DIM=1024 \
    RETRIEVAL_RERANKER_BASE_URL=... RETRIEVAL_RERANKER_MODEL=... \
    RETRIEVAL_RERANKER_API_KEY=... \
    python tests/benchmark/run_memory_native_sweeps.py
"""

import asyncio
import copy
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

_BACKEND = Path(__file__).resolve().parent.parent.parent
_BENCH = Path(__file__).resolve().parent
_FIXTURES = _BACKEND / "tests" / "fixtures"

for p in (_BACKEND, _BENCH):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from db.sqlite_client import SQLiteClient

from test_memory_native_benchmark import (
    BENCH_DOMAINS,
    _created_namespaces,
    _ensure_parent_chain,
    _path_matches_prefix,
    _split_uri,
    compute_metrics,
    register_aliases,
)
from test_memory_native_full import (
    _populate_corpus_tracked,
    _resolve_tf1_sentinels,
)

# ---------------------------------------------------------------------------
# Snapshot reranker env at import time (before any manipulation)
# ---------------------------------------------------------------------------
_RERANKER_SNAP: Dict[str, str] = {}
for _k in ["RETRIEVAL_RERANKER_BASE_URL", "RETRIEVAL_RERANKER_MODEL",
           "RETRIEVAL_RERANKER_API_KEY"]:
    _v = os.environ.get(_k)
    if _v is not None:
        _RERANKER_SNAP[_k] = _v


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_jsonl(name: str) -> List[Dict]:
    with open(_FIXTURES / name) as f:
        return [json.loads(line) for line in f if line.strip()]


def _db_url(path) -> str:
    return f"sqlite+aiosqlite:///{path}"


def _set_profile_c_env():
    os.environ["VALID_DOMAINS"] = BENCH_DOMAINS
    os.environ["RETRIEVAL_EMBEDDING_BACKEND"] = "api"
    for k in ["RETRIEVAL_RERANKER_BASE_URL", "RETRIEVAL_RERANKER_MODEL",
              "RETRIEVAL_RERANKER_API_KEY", "RETRIEVAL_RERANKER_WEIGHT"]:
        os.environ.pop(k, None)


def _set_profile_d_env():
    os.environ["VALID_DOMAINS"] = BENCH_DOMAINS
    os.environ["RETRIEVAL_EMBEDDING_BACKEND"] = "api"
    for k, v in _RERANKER_SNAP.items():
        os.environ[k] = v


async def _populate(db_path, corpus, aliases):
    client = SQLiteClient(_db_url(db_path))
    await client.init_db()
    id_map, vg_ts = await _populate_corpus_tracked(client, corpus)
    assert len(id_map) == len(corpus), f"Expected {len(corpus)}, got {len(id_map)}"
    await register_aliases(client, aliases, id_map)
    return client, id_map, vg_ts


async def _run_queries(client, queries, id_map, vg_ts, *,
                       mode="hybrid", candidate_multiplier=8, max_results=10):
    qs = copy.deepcopy(queries)
    _resolve_tf1_sentinels(qs, vg_ts)

    per_query: List[Dict] = []
    for q in qs:
        rel_fids = q.get("expected_memory_ids", [])
        rel_mids = {id_map[f][0] for f in rel_fids if f in id_map}

        t0 = time.perf_counter()
        result = await client.search_advanced(
            query=q["query"], mode=mode, max_results=max_results,
            candidate_multiplier=candidate_multiplier,
            filters=q.get("filters"),
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

        per_query.append({
            "case_id": q["case_id"], "taxonomy": q["taxonomy_code"],
            "gap": q.get("gap_dimension", []), "lang": q.get("lang", ""),
            **m, "rank_1_correct": r1_ok, "scope_precision": scope_prec,
            "latency_ms": round(lat, 1), "result_count": len(rmids),
        })
    return per_query


def _aggregate(per_query):
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


# ---------------------------------------------------------------------------
# Sweep A: RETRIEVAL_WEIGHT_RECENCY on Profile C
# ---------------------------------------------------------------------------

async def sweep_a(corpus, aliases, queries, tmp_dir):
    values = [0.06, 0.15, 0.25, 0.35]
    results = {}

    for val in values:
        label = f"recency_{val}"
        print(f"  [{label}] populating ...", end="", flush=True)

        _set_profile_c_env()
        os.environ["RETRIEVAL_WEIGHT_RECENCY"] = str(val)

        db_path = Path(tmp_dir) / f"sweep_a_{val}.db"
        client, id_map, vg_ts = await _populate(db_path, corpus, aliases)

        print(" querying ...", end="", flush=True)
        pq = await _run_queries(client, queries, id_map, vg_ts)
        overall, by_tax = _aggregate(pq)

        results[label] = {
            "param": "RETRIEVAL_WEIGHT_RECENCY", "value": val, "profile": "C",
            "overall": overall, "by_taxonomy": by_tax, "per_query": pq,
        }
        print(f" HR@10={overall['hr_at_10']:.3f} MRR={overall['mrr']:.3f}", flush=True)

    os.environ.pop("RETRIEVAL_WEIGHT_RECENCY", None)
    return results


# ---------------------------------------------------------------------------
# Sweep B: candidate_multiplier on Profile C (single populate)
# ---------------------------------------------------------------------------

async def sweep_b(corpus, aliases, queries, tmp_dir):
    values = [8, 12, 16, 24]
    results = {}

    _set_profile_c_env()
    os.environ.pop("RETRIEVAL_WEIGHT_RECENCY", None)

    print("  [candidate] populating once ...", end="", flush=True)
    db_path = Path(tmp_dir) / "sweep_b.db"
    client, id_map, vg_ts = await _populate(db_path, corpus, aliases)
    print(" done", flush=True)

    for val in values:
        label = f"candidate_{val}"
        print(f"  [{label}] querying ...", end="", flush=True)

        pq = await _run_queries(client, queries, id_map, vg_ts,
                                candidate_multiplier=val)
        overall, by_tax = _aggregate(pq)

        results[label] = {
            "param": "candidate_multiplier", "value": val, "profile": "C",
            "overall": overall, "by_taxonomy": by_tax, "per_query": pq,
        }
        print(f" HR@10={overall['hr_at_10']:.3f} MRR={overall['mrr']:.3f}", flush=True)

    return results


# ---------------------------------------------------------------------------
# Sweep C: RETRIEVAL_RERANKER_WEIGHT on Profile D
# ---------------------------------------------------------------------------

async def sweep_c(corpus, aliases, queries, tmp_dir):
    values = [0.25, 0.40, 0.55, 0.70]
    results = {}

    for val in values:
        label = f"reranker_{val}"
        print(f"  [{label}] populating ...", end="", flush=True)

        _set_profile_d_env()
        os.environ["RETRIEVAL_RERANKER_WEIGHT"] = str(val)
        os.environ.pop("RETRIEVAL_WEIGHT_RECENCY", None)

        db_path = Path(tmp_dir) / f"sweep_c_{val}.db"
        client, id_map, vg_ts = await _populate(db_path, corpus, aliases)

        print(" querying ...", end="", flush=True)
        pq = await _run_queries(client, queries, id_map, vg_ts)
        overall, by_tax = _aggregate(pq)

        results[label] = {
            "param": "RETRIEVAL_RERANKER_WEIGHT", "value": val, "profile": "D",
            "overall": overall, "by_taxonomy": by_tax, "per_query": pq,
        }
        print(f" HR@10={overall['hr_at_10']:.3f} MRR={overall['mrr']:.3f}", flush=True)

    return results


# ---------------------------------------------------------------------------
# Delta computation
# ---------------------------------------------------------------------------

def _add_deltas(sweep_results, baseline_overall, baseline_tax):
    for _, sr in sweep_results.items():
        for t, td in sr["by_taxonomy"].items():
            bt = baseline_tax.get(t, {})
            td["delta_hr"] = round(td["hr_at_10"] - bt.get("hr_at_10", 0), 4)
            td["delta_mrr"] = round(td["mrr"] - bt.get("mrr", 0), 4)
        sr["delta_overall_hr"] = round(
            sr["overall"]["hr_at_10"] - baseline_overall["hr_at_10"], 4)
        sr["delta_overall_mrr"] = round(
            sr["overall"]["mrr"] - baseline_overall["mrr"], 4)


# ---------------------------------------------------------------------------
# MD report
# ---------------------------------------------------------------------------

def _write_md(report, baseline, path):
    md = ["# Memory-Native Benchmark — Parameter Sweep Report\n"]
    md.append(f"- Baseline: {report['baseline_timestamp']}")
    md.append(f"- Sweep: {report['timestamp']}\n")

    sweep_meta = [
        ("sweep_a_recency", "Sweep A: RETRIEVAL_WEIGHT_RECENCY (Profile C)",
         ["TR1", "V1"], ["F1", "F2", "S1", "TF1"]),
        ("sweep_b_candidate", "Sweep B: candidate_multiplier (Profile C)",
         ["E1", "E2", "V2"], ["F1", "S1", "N2"]),
        ("sweep_c_reranker", "Sweep C: RETRIEVAL_RERANKER_WEIGHT (Profile D)",
         ["F1", "F2", "C1", "TX"], ["S1", "S2", "TF1", "N2"]),
    ]

    for key, title, targets, guards in sweep_meta:
        data = report[key]
        md.append(f"## {title}\n")

        # Overall
        md.append("### Overall\n")
        md.append("| Value | HR@10 | ΔHR | MRR | ΔMRR | NDCG@10 |")
        md.append("|---:|---:|---:|---:|---:|---:|")
        for sr in data.values():
            ov = sr["overall"]
            dhr = sr.get("delta_overall_hr", 0)
            dmrr = sr.get("delta_overall_mrr", 0)
            md.append(
                f"| {sr['value']} | {ov['hr_at_10']:.3f} | "
                f"{'+' if dhr>0 else ''}{dhr:.3f} | "
                f"{ov['mrr']:.3f} | {'+' if dmrr>0 else ''}{dmrr:.3f} | "
                f"{ov['ndcg_at_10']:.3f} |")

        # Target taxonomy
        md.append(f"\n### Target: {', '.join(targets)}\n")
        hdr = "| Value |" + "".join(f" {t} HR | {t} ΔHR |" for t in targets)
        sep = "|---:|" + "---:|---:|" * len(targets)
        md.append(hdr)
        md.append(sep)
        for sr in data.values():
            row = f"| {sr['value']} |"
            for t in targets:
                td = sr["by_taxonomy"].get(t, {})
                hr = td.get("hr_at_10", 0)
                d = td.get("delta_hr", 0)
                row += f" {hr:.3f} | {'+' if d>0 else ''}{d:.3f} |"
            md.append(row)

        # Guard taxonomy
        md.append(f"\n### Guard: {', '.join(guards)}\n")
        hdr = "| Value |" + "".join(f" {t} HR | {t} ΔHR |" for t in guards)
        sep = "|---:|" + "---:|---:|" * len(guards)
        md.append(hdr)
        md.append(sep)
        for sr in data.values():
            row = f"| {sr['value']} |"
            for t in guards:
                td = sr["by_taxonomy"].get(t, {})
                hr = td.get("hr_at_10", 0)
                d = td.get("delta_hr", 0)
                row += f" {hr:.3f} | {'+' if d>0 else ''}{d:.3f} |"
            md.append(row)

        md.append("")

    with open(path, "w") as f:
        f.write("\n".join(md) + "\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    if not os.environ.get("RETRIEVAL_EMBEDDING_API_BASE"):
        print("ERROR: RETRIEVAL_EMBEDDING_API_BASE not set.")
        sys.exit(1)
    if not _RERANKER_SNAP:
        print("ERROR: RETRIEVAL_RERANKER_BASE_URL not set (needed for Sweep C).")
        sys.exit(1)

    print("=== Memory-Native Benchmark Parameter Sweep ===")
    print(f"Started: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}")

    baseline_path = _BENCH / "memory_native_full_report.json"
    with open(baseline_path) as f:
        baseline = json.load(f)

    bl_c_ov = baseline["profiles"]["C"]["overall"]
    bl_c_tax = baseline["profiles"]["C"]["by_taxonomy"]
    bl_d_ov = baseline["profiles"]["D"]["overall"]
    bl_d_tax = baseline["profiles"]["D"]["by_taxonomy"]

    corpus = _load_jsonl("memory_native_corpus.jsonl")
    aliases = _load_jsonl("memory_native_alias_specs.jsonl")
    layer_a = [q for q in _load_jsonl("memory_native_queries.jsonl")
               if q.get("layer", "A") == "A"]

    t_total = time.time()

    with tempfile.TemporaryDirectory(prefix="mn_sweep_") as tmp_dir:
        print(f"\n--- Sweep A: RETRIEVAL_WEIGHT_RECENCY ---")
        t0 = time.time()
        sa = await sweep_a(corpus, aliases, layer_a, tmp_dir)
        print(f"  Done in {time.time()-t0:.0f}s\n")

        print(f"--- Sweep B: candidate_multiplier ---")
        t0 = time.time()
        sb = await sweep_b(corpus, aliases, layer_a, tmp_dir)
        print(f"  Done in {time.time()-t0:.0f}s\n")

        print(f"--- Sweep C: RETRIEVAL_RERANKER_WEIGHT ---")
        t0 = time.time()
        sc = await sweep_c(corpus, aliases, layer_a, tmp_dir)
        print(f"  Done in {time.time()-t0:.0f}s\n")

    _add_deltas(sa, bl_c_ov, bl_c_tax)
    _add_deltas(sb, bl_c_ov, bl_c_tax)
    _add_deltas(sc, bl_d_ov, bl_d_tax)

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    report = {
        "benchmark": "memory_native_sweep",
        "timestamp": ts,
        "baseline_timestamp": baseline["timestamp"],
        "sweep_a_recency": {k: {kk: vv for kk, vv in v.items() if kk != "per_query"}
                            for k, v in sa.items()},
        "sweep_b_candidate": {k: {kk: vv for kk, vv in v.items() if kk != "per_query"}
                              for k, v in sb.items()},
        "sweep_c_reranker": {k: {kk: vv for kk, vv in v.items() if kk != "per_query"}
                             for k, v in sc.items()},
    }

    json_path = _BENCH / "memory_native_sweeps_report.json"
    with open(json_path, "w") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    md_path = _BENCH / "memory_native_sweeps_report.md"
    _write_md(report, baseline, md_path)

    print(f"Total: {time.time()-t_total:.0f}s")
    print(f"JSON: {json_path}")
    print(f"MD:   {md_path}")
    print("=== Sweep Complete ===")


if __name__ == "__main__":
    asyncio.run(main())

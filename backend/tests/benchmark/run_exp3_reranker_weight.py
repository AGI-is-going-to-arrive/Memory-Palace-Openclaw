#!/usr/bin/env python3
"""Exp 3: Reranker weight sensitivity on Profile D, base = overfetch=6.

Three runs: weight 0.25 (baseline control), 0.35, 0.45.
All use same local embedding + reranker. Profile C included as unchanged reference.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND_ROOT))
os.chdir(str(BACKEND_ROOT))

HEARTBEAT_PATH = Path(__file__).parent / ".exp3_heartbeat.log"
RESULT_PATH = Path(__file__).parent / "exp3_reranker_weight_results.json"

EXPERIMENTS = [
    {
        "name": "reranker_0.25_baseline",
        "label": "Reranker weight=0.25 (baseline control, overfetch=6)",
        "env_overrides": {"RETRIEVAL_RERANKER_WEIGHT": "0.25"},
    },
    {
        "name": "reranker_0.35",
        "label": "Reranker weight=0.35 (overfetch=6)",
        "env_overrides": {"RETRIEVAL_RERANKER_WEIGHT": "0.35"},
    },
    {
        "name": "reranker_0.45",
        "label": "Reranker weight=0.45 (overfetch=6)",
        "env_overrides": {"RETRIEVAL_RERANKER_WEIGHT": "0.45"},
    },
]


def heartbeat(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with HEARTBEAT_PATH.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def extract_metrics(result: dict) -> dict:
    summary = {}
    for pk, pv in result.get("profiles", {}).items():
        summary[pk] = {}
        for row in pv.get("rows", []):
            ds = row["dataset"]
            q = row["quality"]
            lat = row["latency_ms"]
            deg = row["degradation"]
            summary[pk][ds] = {
                "hr_at_5": q["hr_at_5"],
                "hr_at_10": q["hr_at_10"],
                "mrr": q["mrr"],
                "ndcg_at_10": q["ndcg_at_10"],
                "recall_at_10": q["recall_at_10"],
                "p50_ms": lat["p50"],
                "p95_ms": lat["p95"],
                "p99_ms": lat["p99"],
                "degrade_rate": deg["degrade_rate"],
                "invalid_reasons": deg.get("invalid_reasons", []),
            }
    return summary


async def run_experiment(exp: dict) -> dict:
    from tests.benchmark.helpers.profile_abcd_real_runner import (
        build_profile_abcd_real_metrics,
    )

    heartbeat(f"--- {exp['label']} ---")

    prev_env = {}
    for key, value in exp["env_overrides"].items():
        prev_env[key] = os.environ.get(key)
        os.environ[key] = value

    t0 = time.monotonic()
    try:
        result = await build_profile_abcd_real_metrics(
            dataset_keys=["squad_v2_dev", "beir_nfcorpus"],
            sample_size=100,
            profile_keys=["profile_c", "profile_d"],
            max_results=10,
            candidate_multiplier=8,
            extra_distractors=200,
            first_relevant_only=True,
            seed=20260219,
        )
    finally:
        for key, old_value in prev_env.items():
            if old_value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old_value

    elapsed = time.monotonic() - t0
    heartbeat(f"  Completed in {elapsed:.1f}s")

    metrics = extract_metrics(result)
    for pk in ["profile_c", "profile_d"]:
        for ds in ["beir_nfcorpus", "squad_v2_dev"]:
            m = metrics.get(pk, {}).get(ds, {})
            if m:
                tag = "TARGET" if ds == "beir_nfcorpus" else "GUARD"
                heartbeat(
                    f"  [{tag}] {pk} | {ds}: "
                    f"HR@10={m['hr_at_10']:.3f} MRR={m['mrr']:.3f} "
                    f"NDCG={m['ndcg_at_10']:.3f} Recall={m['recall_at_10']:.3f} "
                    f"p95={m['p95_ms']:.1f}ms"
                )

    return {
        "name": exp["name"],
        "label": exp["label"],
        "env_overrides": exp["env_overrides"],
        "elapsed_s": round(elapsed, 1),
        "generated_at_utc": result["generated_at_utc"],
        "metrics": metrics,
    }


async def main():
    HEARTBEAT_PATH.write_text("", encoding="utf-8")
    heartbeat("Exp 3: Reranker weight sensitivity started")
    heartbeat(f"  3 runs x (C+D) x 2 datasets = 12 cells")
    heartbeat(f"  Estimated: ~40-50 min total (local embedding + reranker)")

    from tests.benchmark.helpers.benchmark_env import (
        build_real_retrieval_env,
        describe_real_retrieval_env,
    )

    base_env = build_real_retrieval_env()
    base_env["RETRIEVAL_SEMANTIC_OVERFETCH_FACTOR"] = "6"
    for key, value in base_env.items():
        os.environ[key] = value
    heartbeat(f"Base env: {describe_real_retrieval_env(base_env)}, overfetch=6")

    all_results = []
    for i, exp in enumerate(EXPERIMENTS):
        heartbeat(f"[{i+1}/3] Starting {exp['name']}...")
        try:
            result = await run_experiment(exp)
            all_results.append(result)
        except Exception as e:
            heartbeat(f"  FAILED: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
            all_results.append({"name": exp["name"], "error": str(e)})

    output = {
        "generated_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "base_config": "overfetch=6, local qwen3-embedding + qwen3-reranker",
        "experiments": all_results,
    }
    RESULT_PATH.write_text(
        json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    heartbeat(f"Results saved to: {RESULT_PATH}")
    heartbeat("=== EXP 3 COMPLETE ===")
    heartbeat("EXIT_CODE=0")


if __name__ == "__main__":
    asyncio.run(main())

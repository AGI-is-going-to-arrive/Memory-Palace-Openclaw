#!/usr/bin/env python3
"""Exp 4: candidate_multiplier sensitivity diagnostic.

Base = overfetch=6 (current best). Profile C only (isolate from reranker).
CM = 8 (baseline), 12, 16, 24.
Contract otherwise unchanged.
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

HEARTBEAT_PATH = Path(__file__).parent / ".exp4_heartbeat.log"
RESULT_PATH = Path(__file__).parent / "exp4_cm_diagnostic_results.json"

CM_VALUES = [8, 12, 16, 24]


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
                "corpus_doc_count": row.get("corpus_doc_count", 0),
                "candidate_pool_size_avg": (
                    row.get("entrypoint_stats", {}).get("candidate_pool_size_avg")
                ),
            }
    return summary


async def run_cm_experiment(cm: int) -> dict:
    from tests.benchmark.helpers.profile_abcd_real_runner import (
        build_profile_abcd_real_metrics,
    )

    heartbeat(f"--- CM={cm} (Profile C only, overfetch=6) ---")

    t0 = time.monotonic()
    result = await build_profile_abcd_real_metrics(
        dataset_keys=["squad_v2_dev", "beir_nfcorpus"],
        sample_size=100,
        profile_keys=["profile_c"],
        max_results=10,
        candidate_multiplier=cm,
        extra_distractors=200,
        first_relevant_only=True,
        seed=20260219,
    )
    elapsed = time.monotonic() - t0
    heartbeat(f"  Completed in {elapsed:.1f}s")

    metrics = extract_metrics(result)
    for ds in ["beir_nfcorpus", "squad_v2_dev"]:
        m = metrics.get("profile_c", {}).get(ds, {})
        if m:
            tag = "TARGET" if ds == "beir_nfcorpus" else "GUARD"
            pool = m.get("candidate_pool_size_avg", "?")
            heartbeat(
                f"  [{tag}] {ds}: "
                f"HR@10={m['hr_at_10']:.3f} MRR={m['mrr']:.3f} "
                f"NDCG={m['ndcg_at_10']:.3f} Recall={m['recall_at_10']:.3f} "
                f"p95={m['p95_ms']:.1f}ms pool_avg={pool}"
            )

    return {
        "cm": cm,
        "elapsed_s": round(elapsed, 1),
        "generated_at_utc": result["generated_at_utc"],
        "metrics": metrics,
    }


async def main():
    HEARTBEAT_PATH.write_text("", encoding="utf-8")
    heartbeat("Exp 4: CM diagnostic started")
    heartbeat(f"  CM values: {CM_VALUES}")
    heartbeat(f"  Profile C only, overfetch=6, both datasets")
    heartbeat(f"  Estimated: ~10-12 min per CM value, ~40-50 min total")

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
    for i, cm in enumerate(CM_VALUES):
        heartbeat(f"[{i+1}/{len(CM_VALUES)}] CM={cm}...")
        try:
            result = await run_cm_experiment(cm)
            all_results.append(result)
        except Exception as e:
            heartbeat(f"  FAILED: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
            all_results.append({"cm": cm, "error": str(e)})

    output = {
        "generated_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "base_config": "overfetch=6, Profile C only, local qwen3-embedding",
        "experiments": all_results,
    }
    RESULT_PATH.write_text(
        json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    heartbeat(f"Results saved to: {RESULT_PATH}")
    heartbeat("=== EXP 4 COMPLETE ===")
    heartbeat("EXIT_CODE=0")


if __name__ == "__main__":
    asyncio.run(main())

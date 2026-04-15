#!/usr/bin/env python3
"""One-shot real retrieval benchmark rerun for Phase 1.

Produces current project real benchmark artifacts for A/B/C/D profiles.
Progress is logged to a heartbeat file for monitoring.
"""
from __future__ import annotations

import asyncio
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND_ROOT))
os.chdir(str(BACKEND_ROOT))

HEARTBEAT_PATH = Path(__file__).parent / ".rerun_heartbeat.log"
RESULT_JSON_PATH = Path(__file__).parent / "profile_abcd_real_rerun_20260405.json"


def heartbeat(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with HEARTBEAT_PATH.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def setup_env() -> dict:
    """Configure C/D profile env vars from the caller environment."""
    from tests.benchmark.helpers.benchmark_env import build_real_retrieval_env

    env_config = build_real_retrieval_env()
    for key, value in env_config.items():
        os.environ[key] = value
    return env_config


async def run_profiles_ab() -> dict:
    """Run Profile A/B (offline, no API needed)."""
    from tests.benchmark.helpers.profile_abcd_real_runner import (
        build_profile_abcd_real_metrics,
    )

    heartbeat("Phase 1a: Starting Profile A/B (keyword + hash embedding)...")
    t0 = time.monotonic()

    result = await build_profile_abcd_real_metrics(
        dataset_keys=["squad_v2_dev", "beir_nfcorpus"],
        sample_size=100,
        profile_keys=["profile_a", "profile_b"],
        max_results=10,
        candidate_multiplier=8,
        extra_distractors=200,
        first_relevant_only=True,
        seed=20260219,
    )

    elapsed = time.monotonic() - t0
    heartbeat(f"Phase 1a: Profile A/B completed in {elapsed:.1f}s")

    for pk, pv in result["profiles"].items():
        for row in pv["rows"]:
            q = row["quality"]
            lat = row["latency_ms"]
            deg = row["degradation"]
            heartbeat(
                f"  {pk} | {row['dataset_label']}: "
                f"HR@10={q['hr_at_10']:.3f} MRR={q['mrr']:.3f} "
                f"NDCG@10={q['ndcg_at_10']:.3f} p95={lat['p95']:.1f}ms "
                f"degraded={deg['degrade_rate']:.0%}"
            )

    return result


async def run_profiles_cd() -> dict:
    """Run Profile C/D (needs embedding API + reranker)."""
    from tests.benchmark.helpers.profile_abcd_real_runner import (
        build_profile_abcd_real_metrics,
    )

    heartbeat("Phase 1b: Starting Profile C/D (api embedding + reranker)...")
    t0 = time.monotonic()

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

    elapsed = time.monotonic() - t0
    heartbeat(f"Phase 1b: Profile C/D completed in {elapsed:.1f}s")

    for pk, pv in result["profiles"].items():
        for row in pv["rows"]:
            q = row["quality"]
            lat = row["latency_ms"]
            deg = row["degradation"]
            heartbeat(
                f"  {pk} | {row['dataset_label']}: "
                f"HR@10={q['hr_at_10']:.3f} MRR={q['mrr']:.3f} "
                f"NDCG@10={q['ndcg_at_10']:.3f} p95={lat['p95']:.1f}ms "
                f"degraded={deg['degrade_rate']:.0%} "
                f"invalid_reasons={deg.get('invalid_reasons', [])}"
            )

    return result


async def run_full_abcd() -> dict:
    """Run all A/B/C/D in one call for proper cross-comparison."""
    from tests.benchmark.helpers.profile_abcd_real_runner import (
        build_profile_abcd_real_metrics,
        write_profile_abcd_real_artifacts,
    )

    heartbeat("Phase 1 FULL: Starting A/B/C/D combined run...")
    heartbeat(f"  dataset_scope: squad_v2_dev, beir_nfcorpus")
    heartbeat(f"  sample_size: 100")
    heartbeat(f"  max_results: 10, candidate_multiplier: 8")
    heartbeat(f"  extra_distractors: 200, first_relevant_only: True")
    heartbeat(f"  seed: 20260219")
    t0 = time.monotonic()

    result = await build_profile_abcd_real_metrics(
        dataset_keys=["squad_v2_dev", "beir_nfcorpus"],
        sample_size=100,
        profile_keys=["profile_a", "profile_b", "profile_c", "profile_d"],
        max_results=10,
        candidate_multiplier=8,
        extra_distractors=200,
        first_relevant_only=True,
        seed=20260219,
    )

    elapsed = time.monotonic() - t0
    heartbeat(f"Phase 1 FULL: All 4 profiles completed in {elapsed:.1f}s")

    # Print all results
    for pk in ["profile_a", "profile_b", "profile_c", "profile_d"]:
        pv = result["profiles"].get(pk)
        if not pv:
            heartbeat(f"  {pk}: SKIPPED")
            continue
        for row in pv["rows"]:
            q = row["quality"]
            lat = row["latency_ms"]
            deg = row["degradation"]
            heartbeat(
                f"  {pk} | {row['dataset_label']}: "
                f"HR@10={q['hr_at_10']:.3f} MRR={q['mrr']:.3f} "
                f"NDCG@10={q['ndcg_at_10']:.3f} Recall@10={q['recall_at_10']:.3f} "
                f"p95={lat['p95']:.1f}ms "
                f"degraded={deg['degrade_rate']:.0%}"
            )

    # Write standard artifacts
    artifacts = write_profile_abcd_real_artifacts(result)
    heartbeat(f"Artifacts written:")
    for label, path in artifacts.items():
        heartbeat(f"  {label}: {path}")

    # Write our own JSON
    RESULT_JSON_PATH.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    heartbeat(f"Full JSON saved to: {RESULT_JSON_PATH}")

    # Phase 6 gate summary
    gate = result.get("phase6", {}).get("gate", {})
    heartbeat(f"Phase 6 Gate: valid={gate.get('valid')}, "
              f"invalid_reasons={gate.get('invalid_reasons', [])}, "
              f"invalid_rate={gate.get('invalid_rate', 0):.2%}")

    heartbeat("=== Phase 1 COMPLETE ===")
    return result


async def main():
    parser = argparse.ArgumentParser(
        description="Run the Phase 1 real retrieval benchmark rerun for profiles A/B/C/D."
    )
    parser.parse_args()

    # Clear heartbeat log
    HEARTBEAT_PATH.write_text("", encoding="utf-8")
    heartbeat("Benchmark rerun started")

    env_config = setup_env()
    from tests.benchmark.helpers.benchmark_env import describe_real_retrieval_env

    heartbeat(f"Env configured: {describe_real_retrieval_env(env_config)}")

    try:
        result = await run_full_abcd()
        heartbeat(f"EXIT_CODE=0")
    except Exception as e:
        heartbeat(f"FATAL ERROR: {type(e).__name__}: {e}")
        heartbeat(f"EXIT_CODE=1")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())

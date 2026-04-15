"""CI Regression Gate — Memory-Native Benchmark Baseline Protection

Two modes:
1. Report validation (fast, no API needed):
   Reads the frozen JSON report and validates thresholds.
   Run: pytest test_ci_regression_gate.py -k report_gate

2. Full re-run (slow, maintainer-only, needs embedding+reranker API):
   Re-runs the full Layer A benchmark and validates thresholds.
   Run: OPENCLAW_ENABLE_LIVE_BENCHMARK=1 pytest test_ci_regression_gate.py -k rerun_gate -m slow

Thresholds (frozen 2026-04-06):
  - D: HR@10 >= 0.90, MRR >= 0.75, NDCG@10 >= 0.75
  - Monotonic: A < B < C < D on HR@10
  - D reranker: rerank_applied on >= 1 query

Spec: docs/MEMORY_NATIVE_BENCHMARK_HANDOFF_20260405.md §0
"""

import json
import os
from pathlib import Path

import pytest

_BENCH = Path(__file__).resolve().parent
_REPORT_PATH = _BENCH / "memory_native_full_report.json"

# ---------------------------------------------------------------------------
# Frozen thresholds (from 2026-04-06 baseline)
# ---------------------------------------------------------------------------

THRESHOLDS = {
    "D": {"hr_at_10": 0.90, "mrr": 0.75, "ndcg_at_10": 0.75},
    "C": {"hr_at_10": 0.55, "mrr": 0.35, "ndcg_at_10": 0.38},
    "B": {"hr_at_10": 0.48, "mrr": 0.24, "ndcg_at_10": 0.29},
}

# A < B < C < D must hold on HR@10
MONOTONIC_PROFILES = ["A", "B", "C", "D"]
_LIVE_BENCHMARK_ENV_KEYS = (
    "OPENCLAW_ENABLE_LIVE_BENCHMARK",
    "RELEASE_GATE_ENABLE_LIVE_BENCHMARK",
)


def _live_benchmark_requested() -> bool:
    return any(
        str(os.environ.get(key, "")).strip().lower() in {"1", "true", "yes", "on"}
        for key in _LIVE_BENCHMARK_ENV_KEYS
    )


def _load_report() -> dict:
    assert _REPORT_PATH.exists(), (
        f"Frozen report not found: {_REPORT_PATH}\n"
        "Run the full benchmark first or restore the frozen artifact."
    )
    with open(_REPORT_PATH) as f:
        return json.load(f)


def _get_profile_overall(report: dict, profile: str) -> dict:
    profiles = report.get("profiles", {})
    assert profile in profiles, (
        f"Profile {profile} not in report. Available: {list(profiles.keys())}"
    )
    return profiles[profile]["overall"]


# ---------------------------------------------------------------------------
# Mode 1: Report validation gate (fast)
# ---------------------------------------------------------------------------


class TestReportGate:
    """Validate the frozen JSON report against regression thresholds."""

    def test_report_exists_and_has_four_profiles(self):
        report = _load_report()
        profiles_run = report.get("profiles_run", [])
        assert set(MONOTONIC_PROFILES).issubset(set(profiles_run)), (
            f"Report must contain all four profiles. Got: {profiles_run}"
        )

    @pytest.mark.parametrize("profile,metric,threshold", [
        (p, m, v)
        for p, thresholds in THRESHOLDS.items()
        for m, v in thresholds.items()
    ])
    def test_profile_meets_threshold(self, profile, metric, threshold):
        report = _load_report()
        overall = _get_profile_overall(report, profile)
        actual = overall[metric]
        assert actual >= threshold, (
            f"REGRESSION: Profile {profile} {metric}={actual:.4f} "
            f"< threshold {threshold:.4f}"
        )

    def test_monotonic_progression(self):
        """A < B < C < D on HR@10 must hold."""
        report = _load_report()
        hr_values = {}
        for p in MONOTONIC_PROFILES:
            overall = _get_profile_overall(report, p)
            hr_values[p] = overall["hr_at_10"]

        for i in range(len(MONOTONIC_PROFILES) - 1):
            lo = MONOTONIC_PROFILES[i]
            hi = MONOTONIC_PROFILES[i + 1]
            assert hr_values[lo] < hr_values[hi], (
                f"REGRESSION: Monotonic violation {lo}({hr_values[lo]:.4f}) "
                f">= {hi}({hr_values[hi]:.4f}) on HR@10"
            )

    def test_d_reranker_fired(self):
        """Profile D must have rerank_applied=true on at least one query."""
        report = _load_report()
        d_data = report["profiles"]["D"]
        reranked = sum(
            1 for pq in d_data["per_query"] if pq.get("rerank_applied")
        )
        assert reranked > 0, (
            "REGRESSION: Profile D rerank_applied=true on zero queries"
        )

    def test_d_taxonomy_floor(self):
        """Key taxonomies on D must not regress below floor values."""
        floors = {
            "F1": 0.90, "F2": 0.90, "S1": 0.90, "N1": 0.90,
            "V1": 0.90, "E2": 0.90, "TF1": 0.90, "C2": 0.90,
            "TR1": 0.50,  # currently 0.667, allow some room
            "TX": 0.70,   # currently 0.800
        }
        report = _load_report()
        d_tax = report["profiles"]["D"]["by_taxonomy"]
        failures = []
        for tax, floor in floors.items():
            if tax in d_tax:
                actual = d_tax[tax]["hr_at_10"]
                if actual < floor:
                    failures.append(
                        f"{tax}: HR@10={actual:.3f} < floor {floor:.3f}"
                    )
        assert not failures, (
            f"REGRESSION: D taxonomy floor violations:\n"
            + "\n".join(f"  - {f}" for f in failures)
        )


# ---------------------------------------------------------------------------
# Mode 2: Full re-run gate (slow, needs API)
# ---------------------------------------------------------------------------


@pytest.mark.slow
class TestRerunGate:
    """Re-run Layer A benchmark and validate thresholds.

    Requires embedding + reranker env vars.
    Run with: OPENCLAW_ENABLE_LIVE_BENCHMARK=1 pytest test_ci_regression_gate.py -k rerun_gate -m slow
    """

    @pytest.mark.asyncio
    async def test_rerun_gate(self, tmp_path, monkeypatch):
        """Full re-run: populate, query, validate thresholds."""
        if not _live_benchmark_requested():
            pytest.skip(
                "Memory-native live benchmark rerun is disabled by default; "
                "set OPENCLAW_ENABLE_LIVE_BENCHMARK=1 to run."
            )
        import sys
        _BACKEND = Path(__file__).resolve().parent.parent.parent
        for p in (_BACKEND, _BENCH):
            if str(p) not in sys.path:
                sys.path.insert(0, str(p))

        from test_memory_native_full import (
            test_memory_native_full_layer_a,
        )

        # Run the full benchmark (writes report JSON as side effect)
        await test_memory_native_full_layer_a(tmp_path, monkeypatch)

        # Now validate the freshly written report
        report = _load_report()

        # Threshold checks
        for profile, thresholds in THRESHOLDS.items():
            overall = _get_profile_overall(report, profile)
            for metric, threshold in thresholds.items():
                actual = overall[metric]
                assert actual >= threshold, (
                    f"REGRESSION (re-run): Profile {profile} "
                    f"{metric}={actual:.4f} < {threshold:.4f}"
                )

        # Monotonic check
        hrs = [
            _get_profile_overall(report, p)["hr_at_10"]
            for p in MONOTONIC_PROFILES
        ]
        for i in range(len(hrs) - 1):
            assert hrs[i] < hrs[i + 1], (
                f"REGRESSION (re-run): Monotonic violation at "
                f"{MONOTONIC_PROFILES[i]}->{MONOTONIC_PROFILES[i+1]}"
            )

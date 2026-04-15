import json
import sys
from pathlib import Path
from typing import Any, Dict, Mapping, Sequence

import pytest

BENCHMARK_DIR = Path(__file__).resolve().parent
if str(BENCHMARK_DIR) not in sys.path:
    sys.path.insert(0, str(BENCHMARK_DIR))

from helpers.common import load_thresholds_v1
from helpers import profile_ab_runner as legacy_runner
from helpers.profile_ab_runner import (
    PROFILE_JSON_ARTIFACT,
    PROFILE_MARKDOWN_ARTIFACTS,
    PROFILE_CD_INVALID_GATE_REASONS,
    build_profile_ab_metrics,
    write_profile_ab_artifacts,
)


def _fake_real_payload(sample_size: int) -> Mapping[str, Any]:
    def _row(
        dataset: str,
        dataset_label: str,
        *,
        hr10: float,
        mrr: float,
        ndcg: float,
        recall: float,
        p95: float,
    ) -> Dict[str, Any]:
        return {
            "dataset": dataset,
            "dataset_label": dataset_label,
            "mode": "hybrid",
            "sample_size": sample_size,
            "query_count": sample_size,
            "quality": {
                "hr_at_5": max(0.0, hr10 - 0.05),
                "hr_at_10": hr10,
                "mrr": mrr,
                "ndcg_at_10": ndcg,
                "recall_at_10": recall,
            },
            "latency_ms": {"p50": round(p95 * 0.6, 1), "p95": p95, "p99": round(p95 * 1.2, 1)},
            "degradation": {
                "queries": sample_size,
                "degraded": 0,
                "degrade_rate": 0.0,
                "degrade_reasons": [],
                "invalid_reasons": [],
                "invalid_count": 0,
                "invalid_rate": 0.0,
                "request_failed_count": 0,
                "request_failed_rate": 0.0,
                "valid": True,
            },
        }

    datasets = [
        ("squad_v2_dev", "SQuAD v2 Dev"),
        ("beir_nfcorpus", "BEIR NFCorpus"),
    ]
    profile_a_rows = [
        _row(dataset, label, hr10=0.30 + idx * 0.05, mrr=0.22 + idx * 0.04, ndcg=0.28 + idx * 0.05, recall=0.34 + idx * 0.05, p95=7.0 + idx)
        for idx, (dataset, label) in enumerate(datasets)
    ]
    profile_b_rows = [
        _row(dataset, label, hr10=0.55 + idx * 0.08, mrr=0.42 + idx * 0.05, ndcg=0.50 + idx * 0.05, recall=0.60 + idx * 0.05, p95=9.0 + idx)
        for idx, (dataset, label) in enumerate(datasets)
    ]
    profile_c_rows = [
        _row(dataset, label, hr10=0.58 + idx * 0.08, mrr=0.44 + idx * 0.05, ndcg=0.53 + idx * 0.05, recall=0.63 + idx * 0.05, p95=10.0 + idx)
        for idx, (dataset, label) in enumerate(datasets)
    ]
    profile_d_rows = [
        _row(dataset, label, hr10=0.62 + idx * 0.08, mrr=0.48 + idx * 0.05, ndcg=0.57 + idx * 0.05, recall=0.67 + idx * 0.05, p95=11.0 + idx)
        for idx, (dataset, label) in enumerate(datasets)
    ]
    return {
        "generated_at_utc": "2026-03-09T10:00:00+00:00",
        "source": "backend/tests/benchmark/helpers/profile_abcd_real_runner.py",
        "real_run_strategy": {"candidate_multiplier": 8, "max_results": 10},
        "profiles": {
            "profile_a": {"profile": "profile_a", "mode": "keyword", "rows": profile_a_rows},
            "profile_b": {"profile": "profile_b", "mode": "hybrid", "rows": profile_b_rows},
            "profile_c": {"profile": "profile_c", "mode": "hybrid", "rows": profile_c_rows},
            "profile_d": {"profile": "profile_d", "mode": "hybrid", "rows": profile_d_rows},
        },
        "phase6": {"gate": {"valid": True, "invalid_reasons": []}},
    }


def _run_profile_ab(
    monkeypatch: pytest.MonkeyPatch,
    sample_size: int = 100,
    profile_cd_degrade_reasons_by_dataset: Mapping[str, Sequence[str]] | None = None,
) -> Mapping[str, Any]:
    async def _fake_build_profile_abcd_real_metrics(**kwargs):
        assert kwargs["sample_size"] == sample_size
        return _fake_real_payload(sample_size)

    monkeypatch.setattr(
        legacy_runner,
        "build_profile_abcd_real_metrics",
        _fake_build_profile_abcd_real_metrics,
    )
    payload = build_profile_ab_metrics(
        sample_size=sample_size,
        profile_cd_degrade_reasons_by_dataset=profile_cd_degrade_reasons_by_dataset,
    )
    write_profile_ab_artifacts(payload)
    return payload


def _assert_latency_and_degradation_contract(
    row: Dict[str, Any], *, p95_limit: float, degrade_rate_limit: float
) -> None:
    latency = row["latency_ms"]
    assert set(latency) == {"p50", "p95", "p99"}
    p50 = float(latency["p50"])
    p95 = float(latency["p95"])
    p99 = float(latency["p99"])
    assert 0.0 < p50 <= p95 <= p99
    assert p95 < p95_limit

    degradation = row["degradation"]
    queries = int(degradation["queries"])
    degraded = int(degradation["degraded"])
    rate = float(degradation["degrade_rate"])
    assert queries > 0
    assert 0 <= degraded <= queries
    assert 0.0 <= rate <= degrade_rate_limit


def _assert_latency_markdown_row(markdown: str, row: Dict[str, Any]) -> None:
    latency = row["latency_ms"]
    expected = (
        f"| {row['dataset_label']} | {latency['p50']:.1f} | {latency['p95']:.1f} | {latency['p99']:.1f} |"
    )
    assert expected in markdown


def _assert_degradation_markdown_row(markdown: str, row: Dict[str, Any]) -> None:
    degradation = row["degradation"]
    expected = (
        f"| {row['dataset_label']} | {degradation['queries']} | {degradation['degraded']} | "
        f"{degradation['degrade_rate'] * 100:.1f}% |"
    )
    assert expected in markdown


def test_profile_a_latency_report_within_thresholds_and_json_consistent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _run_profile_ab(monkeypatch, sample_size=100)
    thresholds = load_thresholds_v1()

    profile_a = payload["profiles"]["profile_a"]
    rows = profile_a["rows"]
    assert len(rows) == 2

    for row in rows:
        _assert_latency_and_degradation_contract(
            row,
            p95_limit=float(thresholds["profile_cd"]["p95_ms_lt"]),
            degrade_rate_limit=float(thresholds["global"]["degrade_rate_lt"]),
        )

    assert PROFILE_JSON_ARTIFACT.exists()
    json_payload = json.loads(PROFILE_JSON_ARTIFACT.read_text(encoding="utf-8"))
    assert json_payload["profiles"]["profile_a"]["mode"] == "keyword"

    markdown_path = PROFILE_MARKDOWN_ARTIFACTS["profile_a"]
    assert markdown_path.exists()
    markdown = markdown_path.read_text(encoding="utf-8")
    assert "## Latency (ms)" in markdown
    assert "## Degradation" in markdown
    for row in rows:
        _assert_latency_markdown_row(markdown, row)
        _assert_degradation_markdown_row(markdown, row)


def test_profile_b_latency_report_within_thresholds_and_non_negative_degradation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _run_profile_ab(monkeypatch, sample_size=100)
    thresholds = load_thresholds_v1()

    profile_a_rows = payload["profiles"]["profile_a"]["rows"]
    profile_b_rows = payload["profiles"]["profile_b"]["rows"]
    assert len(profile_b_rows) == 2

    by_dataset_a = {row["dataset"]: row for row in profile_a_rows}
    for row in profile_b_rows:
        _assert_latency_and_degradation_contract(
            row,
            p95_limit=float(thresholds["profile_cd"]["p95_ms_lt"]),
            degrade_rate_limit=float(thresholds["global"]["degrade_rate_lt"]),
        )
        assert row["latency_ms"]["p95"] >= by_dataset_a[row["dataset"]]["latency_ms"]["p95"]

    markdown_path = PROFILE_MARKDOWN_ARTIFACTS["profile_b"]
    assert markdown_path.exists()
    markdown = markdown_path.read_text(encoding="utf-8")
    assert "## Latency (ms)" in markdown
    assert "## Degradation" in markdown
    for row in profile_b_rows:
        _assert_latency_markdown_row(markdown, row)
        _assert_degradation_markdown_row(markdown, row)


def test_profile_cd_latency_report_within_thresholds_and_gate_valid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _run_profile_ab(monkeypatch, sample_size=100)
    thresholds = load_thresholds_v1()
    profile_cd_rows = payload["profiles"]["profile_cd"]["rows"]
    assert len(profile_cd_rows) == 2

    for row in profile_cd_rows:
        _assert_latency_and_degradation_contract(
            row,
            p95_limit=float(thresholds["profile_cd"]["p95_ms_lt"]),
            degrade_rate_limit=float(thresholds["global"]["degrade_rate_lt"]),
        )
        degradation = row["degradation"]
        assert degradation["valid"] is True
        assert degradation["invalid_reasons"] == []

    phase6_gate = payload["phase6"]["gate"]
    assert phase6_gate["valid"] is True
    assert phase6_gate["invalid_reasons"] == []

    markdown_path = PROFILE_MARKDOWN_ARTIFACTS["profile_cd"]
    assert markdown_path.exists()
    markdown = markdown_path.read_text(encoding="utf-8")
    assert "## Latency (ms)" in markdown
    assert "## Degradation" in markdown
    assert "## A/B/CD Comparison" in markdown
    for row in profile_cd_rows:
        _assert_latency_markdown_row(markdown, row)
        _assert_degradation_markdown_row(markdown, row)


def test_profile_cd_latency_report_marks_invalid_when_phase6_reasons_hit_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _run_profile_ab(
        monkeypatch,
        sample_size=100,
        profile_cd_degrade_reasons_by_dataset={
            "beir_nfcorpus": ["embedding_request_failed", "embedding_fallback_hash"],
            "squad_v2_dev": ["reranker_request_failed"],
        },
    )

    profile_cd_rows = payload["profiles"]["profile_cd"]["rows"]
    invalid_union: set[str] = set()
    for row in profile_cd_rows:
        invalid_reasons = set(row["degradation"]["invalid_reasons"])
        assert row["degradation"]["valid"] is False
        assert invalid_reasons
        invalid_union.update(invalid_reasons)

    assert invalid_union == PROFILE_CD_INVALID_GATE_REASONS
    phase6_gate = payload["phase6"]["gate"]
    assert phase6_gate["valid"] is False
    assert set(phase6_gate["invalid_reasons"]) == PROFILE_CD_INVALID_GATE_REASONS

    markdown = PROFILE_MARKDOWN_ARTIFACTS["profile_cd"].read_text(encoding="utf-8")
    assert "overall_valid: false" in markdown
    assert "INVALID" in markdown

    restored = _run_profile_ab(monkeypatch, sample_size=100)
    assert restored["phase6"]["gate"]["valid"] is True

"""Build legacy profile benchmark artifacts from real retrieval execution.

This wrapper keeps the historical `profile_ab_metrics.json` / markdown artifact
shape, but the metrics now come from `profile_abcd_real_runner` instead of a
hard-coded baseline table.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Set

from .common import BENCHMARK_DIR, DATASETS_DIR
from .profile_abcd_real_runner import build_profile_abcd_real_metrics

PROFILE_JSON_ARTIFACT = BENCHMARK_DIR / "profile_ab_metrics.json"
PROFILE_MARKDOWN_ARTIFACTS: Dict[str, Path] = {
    "profile_a": BENCHMARK_DIR / "benchmark_results_profile_a.md",
    "profile_b": BENCHMARK_DIR / "benchmark_results_profile_b.md",
    "profile_cd": BENCHMARK_DIR / "benchmark_results_profile_cd.md",
}
MEMORY_GOLD_SET_PATH = DATASETS_DIR.parent / "fixtures" / "memory_gold_set.jsonl"
PROFILE_CD_INVALID_GATE_REASONS = {
    "embedding_fallback_hash",
    "embedding_request_failed",
    "reranker_request_failed",
}
PROFILE_CD_REQUEST_FAILED_REASONS = {
    "embedding_request_failed",
    "reranker_request_failed",
}
LEGACY_DATASET_SCOPE = ("squad_v2_dev", "beir_nfcorpus")
LEGACY_SOURCE = (
    "backend/tests/benchmark/helpers/profile_ab_runner.py "
    "(delegates to backend/tests/benchmark/helpers/profile_abcd_real_runner.py)"
)

_PROFILE_MODES: Dict[str, str] = {
    "profile_a": "keyword",
    "profile_b": "hybrid",
    "profile_cd": "hybrid",
}

def _normalize_degrade_reasons(raw_reasons: Sequence[str] | None) -> list[str]:
    if not raw_reasons:
        return []
    normalized: list[str] = []
    for item in raw_reasons:
        value = str(item).strip()
        if value and value not in normalized:
            normalized.append(value)
    return normalized


def _clone_jsonable(payload: Mapping[str, Any]) -> Dict[str, Any]:
    return json.loads(json.dumps(payload, ensure_ascii=False))


def _apply_profile_cd_override(
    row: Mapping[str, Any],
    extra_reasons: Sequence[str] | None,
) -> Dict[str, Any]:
    updated = _clone_jsonable(row)
    degradation = updated.setdefault("degradation", {})
    queries = int(degradation.get("queries") or updated.get("query_count") or 0)

    merged_degrade_reasons = _normalize_degrade_reasons(
        list(degradation.get("degrade_reasons", [])) + list(extra_reasons or [])
    )
    invalid_reasons = sorted(
        {
            *[
                reason
                for reason in degradation.get("invalid_reasons", [])
                if isinstance(reason, str)
            ],
            *[
                reason
                for reason in merged_degrade_reasons
                if reason in PROFILE_CD_INVALID_GATE_REASONS
            ],
        }
    )
    request_failed_reasons = sorted(
        reason
        for reason in invalid_reasons
        if reason in PROFILE_CD_REQUEST_FAILED_REASONS
    )

    invalid_count = int(degradation.get("invalid_count") or 0)
    request_failed_count = int(degradation.get("request_failed_count") or 0)
    if invalid_reasons and invalid_count <= 0:
        invalid_count = 1
    if request_failed_reasons and request_failed_count <= 0:
        request_failed_count = 1

    degradation["degrade_reasons"] = merged_degrade_reasons
    degradation["invalid_reasons"] = invalid_reasons
    degradation["valid"] = len(invalid_reasons) == 0
    degradation["invalid_count"] = invalid_count
    degradation["invalid_rate"] = (
        round(float(invalid_count) / float(queries), 6) if queries > 0 else 0.0
    )
    degradation["request_failed_count"] = request_failed_count
    degradation["request_failed_rate"] = (
        round(float(request_failed_count) / float(queries), 6) if queries > 0 else 0.0
    )
    degradation["invalid_reason_counts"] = {
        reason: max(
            1,
            int((degradation.get("invalid_reason_counts") or {}).get(reason, 0) or 0),
        )
        for reason in invalid_reasons
    }
    degradation["request_failed_reason_counts"] = {
        reason: max(
            1,
            int((degradation.get("request_failed_reason_counts") or {}).get(reason, 0) or 0),
        )
        for reason in request_failed_reasons
    }
    return updated


def _build_phase6_gate(profile_cd_rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    gate_rows: list[Dict[str, Any]] = []
    invalid_union: Set[str] = set()
    for row in profile_cd_rows:
        degradation = row.get("degradation", {})
        invalid_reasons = _normalize_degrade_reasons(degradation.get("invalid_reasons"))
        invalid_union.update(invalid_reasons)
        gate_rows.append(
            {
                "dataset": row["dataset"],
                "dataset_label": row["dataset_label"],
                "valid": len(invalid_reasons) == 0,
                "invalid_reasons": invalid_reasons,
            }
        )

    return {
        "valid": len(invalid_union) == 0,
        "invalid_reasons": sorted(invalid_union),
        "rows": gate_rows,
    }


def _build_phase6_comparison(
    profiles: Mapping[str, Mapping[str, Any]]
) -> list[Dict[str, Any]]:
    rows_a = {
        row["dataset"]: row for row in profiles["profile_a"]["rows"]
    }
    rows_b = {
        row["dataset"]: row for row in profiles["profile_b"]["rows"]
    }
    rows_cd = {
        row["dataset"]: row for row in profiles["profile_cd"]["rows"]
    }

    comparison: list[Dict[str, Any]] = []
    for dataset_key in rows_cd:
        if dataset_key not in rows_a or dataset_key not in rows_b:
            continue
        row_a = rows_a[dataset_key]
        row_b = rows_b[dataset_key]
        row_cd = rows_cd[dataset_key]
        degradation_cd = row_cd["degradation"]
        invalid_reasons = _normalize_degrade_reasons(degradation_cd.get("invalid_reasons"))
        comparison.append(
            {
                "dataset": dataset_key,
                "dataset_label": row_cd["dataset_label"],
                "a_hr10": row_a["quality"]["hr_at_10"],
                "b_hr10": row_b["quality"]["hr_at_10"],
                "cd_hr10": row_cd["quality"]["hr_at_10"],
                "a_mrr": row_a["quality"]["mrr"],
                "b_mrr": row_b["quality"]["mrr"],
                "cd_mrr": row_cd["quality"]["mrr"],
                "a_p95": row_a["latency_ms"]["p95"],
                "b_p95": row_b["latency_ms"]["p95"],
                "cd_p95": row_cd["latency_ms"]["p95"],
                "valid": len(invalid_reasons) == 0,
                "invalid_reasons": invalid_reasons,
            }
        )
    return comparison


def build_profile_ab_metrics(
    sample_size: int = 100,
    profile_cd_degrade_reasons_by_dataset: Optional[Mapping[str, Sequence[str]]] = None,
    *,
    dataset_keys: Sequence[str] = LEGACY_DATASET_SCOPE,
    first_relevant_only: bool = True,
    extra_distractors: int = 200,
    max_results: int = 10,
    candidate_multiplier: int = 8,
    seed: int = 20260219,
    workdir: Optional[Path] = None,
) -> Dict[str, Any]:
    if int(sample_size) <= 0:
        raise AssertionError(f"sample_size must be > 0: {sample_size}")
    if not MEMORY_GOLD_SET_PATH.exists():
        raise AssertionError(f"missing memory gold set: {MEMORY_GOLD_SET_PATH}")

    profile_cd_degrade_reasons_by_dataset = profile_cd_degrade_reasons_by_dataset or {}
    real_payload = asyncio.run(
        build_profile_abcd_real_metrics(
            sample_size=int(sample_size),
            dataset_keys=tuple(dataset_keys),
            first_relevant_only=bool(first_relevant_only),
            extra_distractors=int(extra_distractors),
            max_results=int(max_results),
            candidate_multiplier=int(candidate_multiplier),
            seed=int(seed),
            workdir=workdir,
        )
    )

    real_profiles = real_payload["profiles"]
    profiles = {
        "profile_a": _clone_jsonable(real_profiles["profile_a"]),
        "profile_b": _clone_jsonable(real_profiles["profile_b"]),
        "profile_cd": _clone_jsonable(real_profiles["profile_d"]),
    }
    profiles["profile_a"]["profile"] = "profile_a"
    profiles["profile_b"]["profile"] = "profile_b"
    profiles["profile_cd"]["profile"] = "profile_cd"

    rows_cd = []
    for row in profiles["profile_cd"]["rows"]:
        rows_cd.append(
            _apply_profile_cd_override(
                row,
                profile_cd_degrade_reasons_by_dataset.get(str(row.get("dataset") or "")),
            )
        )
    profiles["profile_cd"]["rows"] = rows_cd

    phase6_gate = _build_phase6_gate(profiles["profile_cd"]["rows"])
    phase6_comparison = _build_phase6_comparison(profiles)
    return {
        "generated_at_utc": real_payload["generated_at_utc"],
        "source": LEGACY_SOURCE,
        "real_source": real_payload.get("source"),
        "memory_gold_set": "backend/tests/fixtures/memory_gold_set.jsonl",
        "sample_size": int(sample_size),
        "dataset_scope": list(dataset_keys),
        "real_run_strategy": real_payload.get("real_run_strategy", {}),
        "profiles": profiles,
        "phase6": {
            "gate": phase6_gate,
            "comparison_rows": phase6_comparison,
            "real_gate": real_payload.get("phase6", {}).get("gate"),
        },
    }


def _render_profile_markdown(
    profile_payload: Mapping[str, Any],
    generated_at_utc: str,
    payload_source: str,
    dataset_scope: Sequence[str],
    phase6_payload: Mapping[str, Any] | None = None,
) -> str:
    profile_key = str(profile_payload["profile"])
    mode = str(profile_payload["mode"])
    rows = list(profile_payload["rows"])

    lines = [
        f"# Benchmark Results - {profile_key}",
        "",
        f"> generated_at_utc: {generated_at_utc}",
        f"> mode: {mode}",
        "",
        "## Retrieval Quality",
        "",
        "| Dataset | HR@5 | HR@10 | MRR | NDCG@10 | Recall@10 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        quality = row["quality"]
        lines.append(
            "| {dataset} | {hr5:.3f} | {hr10:.3f} | {mrr:.3f} | {ndcg:.3f} | {recall:.3f} |".format(
                dataset=row["dataset_label"],
                hr5=quality["hr_at_5"],
                hr10=quality["hr_at_10"],
                mrr=quality["mrr"],
                ndcg=quality["ndcg_at_10"],
                recall=quality["recall_at_10"],
            )
        )

    lines.extend(
        [
            "",
            "## Latency (ms)",
            "",
            "| Dataset | p50 | p95 | p99 |",
            "|---|---:|---:|---:|",
        ]
    )
    for row in rows:
        latency = row["latency_ms"]
        lines.append(
            "| {dataset} | {p50:.1f} | {p95:.1f} | {p99:.1f} |".format(
                dataset=row["dataset_label"],
                p50=latency["p50"],
                p95=latency["p95"],
                p99=latency["p99"],
            )
        )

    lines.extend(
        [
            "",
            "## Degradation",
            "",
            "| Dataset | Queries | Degraded | Rate |",
            "|---|---:|---:|---:|",
        ]
    )
    for row in rows:
        degradation = row["degradation"]
        lines.append(
            "| {dataset} | {queries} | {degraded} | {rate:.1f}% |".format(
                dataset=row["dataset_label"],
                queries=degradation["queries"],
                degraded=degradation["degraded"],
                rate=degradation["degrade_rate"] * 100,
            )
        )

    lines.extend(
        [
            "",
            "## Contract",
            "",
            "- json_artifact: `backend/tests/benchmark/profile_ab_metrics.json`",
            f"- source: `{payload_source}`",
            "- memory_gold_set: `backend/tests/fixtures/memory_gold_set.jsonl`",
            f"- dataset_scope: `{', '.join(dataset_scope)}`",
            "",
        ]
    )

    if profile_key == "profile_cd" and isinstance(phase6_payload, Mapping):
        gate = phase6_payload.get("gate", {})
        gate_rows = gate.get("rows", [])
        comparison_rows = phase6_payload.get("comparison_rows", [])
        lines.extend(
            [
                "",
                "## Phase 6 Gate",
                "",
                (
                    f"- overall_valid: "
                    f"{'true' if bool(gate.get('valid')) else 'false'}"
                ),
                (
                    "- invalid_reasons: "
                    + ", ".join(gate.get("invalid_reasons", []))
                    if gate.get("invalid_reasons")
                    else "- invalid_reasons: (none)"
                ),
                "",
                "| Dataset | Valid | Invalid Reasons |",
                "|---|---|---|",
            ]
        )
        for row in gate_rows:
            reasons = row.get("invalid_reasons", [])
            rendered_reasons = ",".join(reasons) if reasons else "-"
            lines.append(
                f"| {row['dataset_label']} | "
                f"{'PASS' if row['valid'] else 'INVALID'} | {rendered_reasons} |"
            )

        lines.extend(
            [
                "",
                "## A/B/CD Comparison",
                "",
                "| Dataset | A HR@10 | B HR@10 | C/D HR@10 | A MRR | B MRR | C/D MRR | A p95 | B p95 | C/D p95 | C/D Gate |",
                "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
            ]
        )
        for row in comparison_rows:
            lines.append(
                "| {dataset} | {a_hr10:.3f} | {b_hr10:.3f} | {cd_hr10:.3f} | "
                "{a_mrr:.3f} | {b_mrr:.3f} | {cd_mrr:.3f} | {a_p95:.1f} | "
                "{b_p95:.1f} | {cd_p95:.1f} | {gate} |".format(
                    dataset=row["dataset_label"],
                    a_hr10=row["a_hr10"],
                    b_hr10=row["b_hr10"],
                    cd_hr10=row["cd_hr10"],
                    a_mrr=row["a_mrr"],
                    b_mrr=row["b_mrr"],
                    cd_mrr=row["cd_mrr"],
                    a_p95=row["a_p95"],
                    b_p95=row["b_p95"],
                    cd_p95=row["cd_p95"],
                    gate="PASS" if row["valid"] else "INVALID",
                )
            )
    return "\n".join(lines)


def write_profile_ab_artifacts(payload: Mapping[str, Any]) -> Dict[str, Path]:
    BENCHMARK_DIR.mkdir(parents=True, exist_ok=True)
    PROFILE_JSON_ARTIFACT.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    generated_at_utc = str(payload["generated_at_utc"])
    payload_source = str(payload.get("source") or LEGACY_SOURCE)
    dataset_scope = [
        str(item)
        for item in payload.get("dataset_scope", [])
        if str(item).strip()
    ]
    artifacts: Dict[str, Path] = {"json": PROFILE_JSON_ARTIFACT}
    profiles = payload["profiles"]
    phase6_payload = payload.get("phase6")
    for profile_key, artifact_path in PROFILE_MARKDOWN_ARTIFACTS.items():
        profile_payload = profiles[profile_key]
        artifact_path.write_text(
            _render_profile_markdown(
                profile_payload,
                generated_at_utc,
                payload_source,
                dataset_scope,
                phase6_payload=phase6_payload if profile_key == "profile_cd" else None,
            ),
            encoding="utf-8",
        )
        artifacts[profile_key] = artifact_path
    return artifacts

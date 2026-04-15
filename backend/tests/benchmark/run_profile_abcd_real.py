#!/usr/bin/env python3
"""Run real A/B/C/D benchmark and emit artifacts."""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

from helpers.profile_abcd_real_runner import (
    REAL_PROFILE_CD_MARKDOWN_ARTIFACT,
    REAL_PROFILE_DEFAULT_CANDIDATE_MULTIPLIER,
    REAL_PROFILE_DEFAULT_ENTRYPOINT,
    REAL_PROFILE_DEFAULT_MAX_RESULTS,
    REAL_PROFILE_FACTUAL_COMPARE_MARKDOWN_ARTIFACT,
    REAL_PROFILE_JSON_ARTIFACT,
    REAL_PROFILE_MARKDOWN_ARTIFACT,
    REAL_PROFILE_SUPPORTED_ENTRYPOINTS,
    build_profile_abcd_real_metrics,
    render_abcd_sota_analysis_markdown,
    render_factual_pool_cap_compare_markdown,
    write_profile_abcd_real_artifacts,
)


def _default_analysis_path() -> Path:
    return Path(__file__).resolve().parents[2] / "docs" / "benchmark_abcd_real_analysis_2026_02.md"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run real benchmark for profiles A/B/C/D. "
            "Profile C/D uses API embedding and optional reranker based on env."
        )
    )
    parser.add_argument(
        "--sample-size",
        type=int,
        default=30,
        help="Effective query count per dataset (<= bucket size 100/200/500). Default: 30",
    )
    parser.add_argument(
        "--datasets",
        type=str,
        default="squad_v2_dev,beir_nfcorpus",
        help="Comma-separated dataset keys. Supported: squad_v2_dev,beir_nfcorpus",
    )
    parser.add_argument(
        "--extra-distractors",
        type=int,
        default=200,
        help="Extra non-relevant corpus docs per dataset. Default: 200",
    )
    parser.add_argument(
        "--max-results",
        type=int,
        default=REAL_PROFILE_DEFAULT_MAX_RESULTS,
        help=(
            "search_advanced max_results passed to each query. "
            f"Default: {REAL_PROFILE_DEFAULT_MAX_RESULTS}"
        ),
    )
    parser.add_argument(
        "--candidate-multiplier",
        type=int,
        default=REAL_PROFILE_DEFAULT_CANDIDATE_MULTIPLIER,
        help=(
            "search_advanced candidate_multiplier passed to each query. "
            f"Default: {REAL_PROFILE_DEFAULT_CANDIDATE_MULTIPLIER}"
        ),
    )
    parser.add_argument(
        "--all-relevant",
        action="store_true",
        help="Use all relevant doc IDs from labels (default uses first relevant only).",
    )
    parser.add_argument(
        "--profiles",
        type=str,
        default="a,b,c,d",
        help=(
            "Comma-separated profiles to execute. "
            "Supported aliases: a,b,c,d or profile_a,...,profile_d. "
            "Default: a,b,c,d"
        ),
    )
    parser.add_argument(
        "--entrypoint",
        type=str,
        default=REAL_PROFILE_DEFAULT_ENTRYPOINT,
        choices=sorted(REAL_PROFILE_SUPPORTED_ENTRYPOINTS),
        help=(
            "Benchmark query entrypoint. "
            "sqlite_client keeps the direct search_advanced baseline; "
            "mcp_search_memory includes preprocess/intention/search_memory contract."
        ),
    )
    parser.add_argument(
        "--factual-pool-cap",
        type=int,
        default=None,
        help=(
            "Optional cap for factual intent candidate_multiplier. "
            "Default keeps current backend behavior; use 0 to disable factual pool shrink "
            "for comparison runs."
        ),
    )
    parser.add_argument(
        "--output-json",
        type=Path,
        default=REAL_PROFILE_JSON_ARTIFACT,
        help=f"Output JSON path. Default: {REAL_PROFILE_JSON_ARTIFACT}",
    )
    parser.add_argument(
        "--output-md",
        type=Path,
        default=REAL_PROFILE_MARKDOWN_ARTIFACT,
        help=f"Output full markdown path. Default: {REAL_PROFILE_MARKDOWN_ARTIFACT}",
    )
    parser.add_argument(
        "--output-cd-md",
        type=Path,
        default=REAL_PROFILE_CD_MARKDOWN_ARTIFACT,
        help=f"Output C/D markdown path. Default: {REAL_PROFILE_CD_MARKDOWN_ARTIFACT}",
    )
    parser.add_argument(
        "--analysis-output",
        type=Path,
        default=_default_analysis_path(),
        help="Output analysis markdown path.",
    )
    parser.add_argument(
        "--workdir",
        type=Path,
        default=None,
        help=(
            "Optional benchmark cache dir for per-profile sqlite files. "
            "Use this when default cache dir has filesystem constraints."
        ),
    )
    parser.add_argument(
        "--phase6-gate-mode",
        type=str,
        default=None,
        help=(
            "Optional phase6 gate mode override. "
            "Supported: strict, api_tolerant."
        ),
    )
    parser.add_argument(
        "--phase6-invalid-rate-threshold",
        type=float,
        default=None,
        help=(
            "Optional phase6 invalid rate threshold (0~1). "
            "Effective when gate mode is api_tolerant."
        ),
    )
    parser.add_argument(
        "--compare-factual-pool-cap",
        type=int,
        default=None,
        help=(
            "Optional second factual_pool_cap value for an explicit comparison run. "
            "When set, the command keeps the normal single-run outputs for the baseline "
            "and additionally writes a separate comparison markdown report."
        ),
    )
    parser.add_argument(
        "--compare-output",
        type=Path,
        default=REAL_PROFILE_FACTUAL_COMPARE_MARKDOWN_ARTIFACT,
        help=(
            "Comparison markdown output path used with --compare-factual-pool-cap. "
            f"Default: {REAL_PROFILE_FACTUAL_COMPARE_MARKDOWN_ARTIFACT}"
        ),
    )
    return parser.parse_args()


async def _run(args: argparse.Namespace) -> None:
    if args.phase6_gate_mode is not None:
        os.environ["BENCHMARK_PHASE6_GATE_MODE"] = str(args.phase6_gate_mode)
    if args.phase6_invalid_rate_threshold is not None:
        os.environ["BENCHMARK_PHASE6_INVALID_RATE_THRESHOLD"] = str(
            float(args.phase6_invalid_rate_threshold)
        )

    def _child_workdir(suffix: str) -> Path | None:
        if args.workdir is None:
            return None
        base = Path(args.workdir)
        return base / suffix

    dataset_keys = [item.strip() for item in args.datasets.split(",") if item.strip()]
    profile_keys = [item.strip() for item in args.profiles.split(",") if item.strip()]
    payload = await build_profile_abcd_real_metrics(
        sample_size=int(args.sample_size),
        dataset_keys=dataset_keys,
        profile_keys=profile_keys,
        factual_pool_cap=args.factual_pool_cap,
        first_relevant_only=not bool(args.all_relevant),
        extra_distractors=int(args.extra_distractors),
        max_results=int(args.max_results),
        candidate_multiplier=int(args.candidate_multiplier),
        entrypoint=str(args.entrypoint),
        workdir=_child_workdir("baseline"),
    )
    artifact_paths = write_profile_abcd_real_artifacts(
        payload,
        json_path=args.output_json,
        markdown_path=args.output_md,
        cd_markdown_path=args.output_cd_md,
    )
    analysis_markdown = render_abcd_sota_analysis_markdown(payload)
    args.analysis_output.parent.mkdir(parents=True, exist_ok=True)
    args.analysis_output.write_text(analysis_markdown, encoding="utf-8")

    print(f"[benchmark] generated json: {artifact_paths['json']}")
    print(f"[benchmark] generated md: {artifact_paths['markdown']}")
    print(f"[benchmark] generated cd md: {artifact_paths['cd_markdown']}")
    print(f"[benchmark] generated analysis: {args.analysis_output}")

    if args.compare_factual_pool_cap is not None:
        compare_payload = await build_profile_abcd_real_metrics(
            sample_size=int(args.sample_size),
            dataset_keys=dataset_keys,
            profile_keys=profile_keys,
            factual_pool_cap=int(args.compare_factual_pool_cap),
            first_relevant_only=not bool(args.all_relevant),
            extra_distractors=int(args.extra_distractors),
            max_results=int(args.max_results),
            candidate_multiplier=int(args.candidate_multiplier),
            entrypoint=str(args.entrypoint),
            workdir=_child_workdir(
                f"compare-factual-cap-{int(args.compare_factual_pool_cap)}"
            ),
        )
        compare_markdown = render_factual_pool_cap_compare_markdown(
            payload,
            compare_payload,
        )
        args.compare_output.parent.mkdir(parents=True, exist_ok=True)
        args.compare_output.write_text(compare_markdown, encoding="utf-8")
        print(f"[benchmark] generated factual compare md: {args.compare_output}")


def main() -> int:
    args = parse_args()
    asyncio.run(_run(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

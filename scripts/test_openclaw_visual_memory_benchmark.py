#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import openclaw_visual_memory_benchmark as bench


class VisualBenchmarkTests(unittest.TestCase):
    def test_parse_json_stdout_accepts_payload_from_stderr(self) -> None:
        payload = bench._parse_json_stdout(
            "",
            '[plugins] hook runner initialized\n{"ok":true,"path":"visual/test"}',
        )

        self.assertEqual(payload["ok"], True)
        self.assertEqual(payload["path"], "visual/test")

    def test_default_cases_expand_to_200_unique_and_balanced_cases(self) -> None:
        cases = bench.default_cases()

        self.assertEqual(len(cases), 200)
        self.assertEqual(len({case.case_id for case in cases}), 200)

        catalog = bench.summarize_case_catalog(cases)
        family_counts = catalog["family_counts"]
        coverage_counts = catalog["coverage_counts"]

        self.assertEqual(catalog["total_cases"], 200)
        self.assertEqual(set(family_counts), set(bench.FAMILY_BUILDERS))
        self.assertEqual(sum(family_counts.values()), 200)
        self.assertLessEqual(max(family_counts.values()) - min(family_counts.values()), 1)
        self.assertEqual(sum(catalog["complexity_counts"].values()), 200)
        self.assertEqual(set(catalog["complexity_counts"]), {"basic", "medium", "complex"})
        self.assertEqual(set(coverage_counts), set(bench.DEFAULT_REQUIRED_COVERAGE_KEYS))
        self.assertEqual(
            sum(coverage_counts.values()),
            family_counts["raw_media_mixed"] + family_counts["raw_media_presigned"],
        )

    def test_select_cases_preserves_family_diversity_under_limit(self) -> None:
        cases = bench.default_cases()
        selected = bench.select_cases(cases, 16)
        catalog = bench.summarize_case_catalog(selected)
        family_counts = catalog["family_counts"]

        self.assertEqual(len(selected), 16)
        self.assertEqual(set(family_counts), set(bench.FAMILY_BUILDERS))
        self.assertTrue(all(count >= 1 for count in family_counts.values()))
        self.assertLessEqual(max(family_counts.values()) - min(family_counts.values()), 1)

    def test_duplicate_new_case_queries_variant_specific_signal(self) -> None:
        case = bench.default_cases(10)
        duplicate_case = next(item for item in case if item.family == "duplicate_new")

        self.assertIn("duplicate variant", duplicate_case.query)
        self.assertIn("new-01", duplicate_case.query)

    def test_raw_media_ref_families_cover_data_url_blob_and_presigned_behaviors(self) -> None:
        jpeg_case = bench._build_raw_media_mixed_case(1)
        webp_case = bench._build_raw_media_mixed_case(2)
        blob_case = bench._build_raw_media_mixed_case(3)
        png_case = bench._build_raw_media_mixed_case(4)
        presigned_case = bench._build_raw_media_presigned_case(1)

        self.assertIn("data:image/jpeg;base64,", jpeg_case.store_args[1])
        self.assertIn("data:image/jpeg;sha256-", jpeg_case.expected_get_substrings[0])
        self.assertIn("data:image/jpeg;base64,", jpeg_case.forbidden_get_substrings)
        self.assertEqual(jpeg_case.coverage_key, "raw_media_data_jpeg")

        self.assertIn("data:image/webp;base64,", webp_case.store_args[1])
        self.assertIn("data:image/webp;sha256-", webp_case.expected_get_substrings[0])
        self.assertIn("data:image/webp;base64,", webp_case.forbidden_get_substrings)
        self.assertEqual(webp_case.coverage_key, "raw_media_data_webp")

        self.assertIn("data:image/png;base64,", png_case.store_args[1])
        self.assertIn("data:image/png;sha256-", png_case.expected_get_substrings[0])
        self.assertEqual(png_case.coverage_key, "raw_media_data_png")

        self.assertTrue(blob_case.store_args[1].startswith("blob:"))
        self.assertIn(blob_case.store_args[1], blob_case.expected_get_substrings[0])
        self.assertEqual(blob_case.forbidden_get_substrings, [])
        self.assertEqual(blob_case.coverage_key, "raw_media_blob")

        self.assertIn("X-Amz-Signature=", presigned_case.store_args[1])
        self.assertTrue(any("sha256-" in item for item in presigned_case.expected_get_substrings))
        self.assertTrue(any("X-Amz-Signature=" in item for item in presigned_case.forbidden_get_substrings))
        self.assertEqual(presigned_case.coverage_key, "raw_media_presigned")

    def test_compute_metrics_aggregates_rates_percentiles_and_breakdowns(self) -> None:
        results = [
            bench.VisualBenchmarkResult(
                case_id="ocr_exact_001",
                family="ocr_exact",
                complexity="basic",
                description="ocr",
                store_ok=True,
                search_hit_at_3=True,
                reciprocal_rank_at_3=1.0,
                get_contains_expected=True,
                store_latency_ms=100.0,
                search_latency_ms=80.0,
                get_latency_ms=60.0,
                stored_path="a",
                stored_uri="core://a",
                query="launch checklist",
                notes=[],
                coverage_key="raw_media_data_png",
            ),
            bench.VisualBenchmarkResult(
                case_id="visual_context_only_001",
                family="visual_context_only",
                complexity="medium",
                description="context",
                store_ok=True,
                search_hit_at_3=False,
                reciprocal_rank_at_3=0.0,
                get_contains_expected=True,
                store_latency_ms=140.0,
                search_latency_ms=90.0,
                get_latency_ms=70.0,
                stored_path="b",
                stored_uri="core://b",
                query="planning wall",
                notes=["search_miss_at_3"],
                coverage_key="raw_media_blob",
            ),
            bench.VisualBenchmarkResult(
                case_id="duplicate_new_001",
                family="duplicate_new",
                complexity="complex",
                description="duplicate",
                store_ok=False,
                search_hit_at_3=False,
                reciprocal_rank_at_3=0.0,
                get_contains_expected=False,
                store_latency_ms=200.0,
                search_latency_ms=100.0,
                get_latency_ms=0.0,
                stored_path=None,
                stored_uri=None,
                query="variant",
                notes=["store_failed"],
                coverage_key="raw_media_presigned",
            ),
        ]

        metrics = bench.compute_metrics(
            results,
            runtime_probe={
                "runtime_visual_probe": "message_preprocessed",
                "runtime_visual_harvest_success_rate": 0.667,
                "runtime_visual_harvest_cases": [
                    {"case_id": "message_preprocessed", "ok": True},
                    {"case_id": "before_prompt_build", "ok": True},
                    {"case_id": "agent_end", "ok": False},
                ],
            },
        )

        self.assertEqual(metrics["total_cases"], 3)
        self.assertEqual(metrics["store_success_rate"], 0.667)
        self.assertEqual(metrics["search_hit_at_3_rate"], 0.333)
        self.assertEqual(metrics["mrr_at_3"], 0.333)
        self.assertEqual(metrics["get_contains_expected_rate"], 0.667)
        self.assertEqual(metrics["duplicate_new_success_rate"], 0.0)
        self.assertEqual(metrics["visual_context_reuse_success_rate"], 1.0)
        self.assertEqual(metrics["store_p95_ms"], 200.0)
        self.assertEqual(metrics["search_p95_ms"], 100.0)
        self.assertEqual(metrics["get_p95_ms"], 70.0)
        self.assertEqual(metrics["runtime_visual_probe"], "message_preprocessed")
        self.assertEqual(metrics["runtime_visual_harvest_success_rate"], 0.667)
        self.assertEqual(metrics["family_summary"]["ocr_exact"]["cases"], 1)
        self.assertEqual(metrics["family_summary"]["visual_context_only"]["search_hit_at_3_rate"], 0.0)
        self.assertEqual(metrics["complexity_summary"]["complex"]["store_success_rate"], 0.0)
        self.assertEqual(metrics["coverage_summary"]["raw_media_data_png"]["cases"], 1)
        self.assertEqual(metrics["coverage_summary"]["raw_media_blob"]["cases"], 1)
        self.assertEqual(metrics["coverage_summary"]["raw_media_presigned"]["store_success_rate"], 0.0)

    def test_build_markdown_report_renders_catalog_breakdown_and_cases(self) -> None:
        results = [
            bench.VisualBenchmarkResult(
                case_id="ocr_exact_001",
                family="ocr_exact",
                complexity="basic",
                description="ocr",
                store_ok=True,
                search_hit_at_3=True,
                reciprocal_rank_at_3=1.0,
                get_contains_expected=True,
                store_latency_ms=100.0,
                search_latency_ms=80.0,
                get_latency_ms=60.0,
                stored_path="a",
                stored_uri="core://a",
                query="launch checklist",
                notes=[],
            )
        ]
        metrics = bench.compute_metrics(
            results,
            runtime_probe={
                "runtime_visual_probe": "tool_context_only",
                "runtime_visual_harvest_success_rate": 1.0,
                "runtime_visual_harvest_cases": [
                    {"case_id": "tool_context_only", "ok": True},
                ],
            },
        )

        report = bench.build_markdown_report(
            profile="d",
            results=results,
            metrics=metrics,
            case_catalog_size=200,
            executed_case_count=16,
            coverage_gate={
                "required_keys": ["raw_media_data_png"],
                "missing_keys": ["raw_media_data_png"],
                "failing_keys": {},
                "passed": False,
            },
        )

        self.assertIn("# OpenClaw Visual Memory Benchmark", report)
        self.assertIn("- profile: `d`", report)
        self.assertIn("- case_catalog_size: `200`", report)
        self.assertIn("## Family Coverage", report)
        self.assertIn("## Complexity Coverage", report)
        self.assertIn("## Raw Media Coverage", report)
        self.assertIn("## Required Coverage", report)
        self.assertIn("- raw_media_mixed_gate: `missing`", report)
        self.assertIn("- raw_media_presigned_gate: `missing`", report)
        self.assertIn("- missing_keys: `raw_media_data_png`", report)
        self.assertIn(
            "| ocr_exact_001 | ocr_exact | basic | true | true | 1.000 | true | - |",
            report,
        )
        self.assertIn("## Runtime Harvest Probe", report)

    def test_parse_profiles_supports_single_and_matrix_modes(self) -> None:
        self.assertEqual(bench.parse_profiles("a", None), ["a"])
        self.assertEqual(bench.parse_profiles("a", "a,b,c,d"), ["a", "b", "c", "d"])
        with self.assertRaises(ValueError):
            bench.parse_profiles("a", "a,z")

    def test_build_matrix_markdown_report_renders_total_counts(self) -> None:
        payload = {
            "profiles": [
                {
                    "profile": "a",
                    "metrics": {
                        "store_success_rate": 1.0,
                        "search_hit_at_3_rate": 1.0,
                        "mrr_at_3": 1.0,
                        "get_contains_expected_rate": 1.0,
                        "runtime_visual_probe": "message_preprocessed",
                        "runtime_visual_harvest_success_rate": 1.0,
                    },
                },
                {
                    "profile": "b",
                    "metrics": {
                        "store_success_rate": 1.0,
                        "search_hit_at_3_rate": 0.95,
                        "mrr_at_3": 0.97,
                        "get_contains_expected_rate": 1.0,
                        "runtime_visual_probe": "tool_context_only",
                        "runtime_visual_harvest_success_rate": 1.0,
                    },
                },
            ],
            "case_catalog_size": 200,
            "executed_case_count_per_profile": 200,
            "executed_case_count_total": 400,
            "family_summary": {"ocr_exact": 25},
            "complexity_summary": {"basic": 50},
            "coverage_summary": {"raw_media_data_png": 4},
            "coverage_gate": {
                "required_keys": ["raw_media_data_png"],
                "passed": True,
                "profiles": {
                    "a": {"passed": True, "missing_keys": [], "failing_keys": {}},
                    "b": {"passed": True, "missing_keys": [], "failing_keys": {}},
                },
            },
        }

        report = bench.build_matrix_markdown_report(payload)

        self.assertIn("# OpenClaw Visual Memory Benchmark Matrix", report)
        self.assertIn("- executed_case_count_per_profile: `200`", report)
        self.assertIn("- executed_case_count_total: `400`", report)
        self.assertIn("## Raw Media Coverage", report)
        self.assertIn("## Required Coverage", report)

    def test_metric_is_full_success_treats_missing_family_metrics_as_non_blocking(self) -> None:
        self.assertTrue(bench._metric_is_full_success({"duplicate_new_success_rate": None}, "duplicate_new_success_rate"))
        self.assertTrue(bench._metric_is_full_success({"duplicate_new_success_rate": 1.0}, "duplicate_new_success_rate"))
        self.assertFalse(bench._metric_is_full_success({"duplicate_new_success_rate": 0.5}, "duplicate_new_success_rate"))

    def test_build_coverage_gate_status_marks_missing_and_failing_coverage(self) -> None:
        metrics = {
            "coverage_summary": {
                "raw_media_data_png": {
                    "cases": 2,
                    "store_success_rate": 1.0,
                    "search_hit_at_3_rate": 1.0,
                    "get_contains_expected_rate": 1.0,
                },
                "raw_media_data_jpeg": {
                    "cases": 2,
                    "store_success_rate": 1.0,
                    "search_hit_at_3_rate": 0.5,
                    "get_contains_expected_rate": 1.0,
                },
            }
        }

        gate = bench.build_coverage_gate_status(
            metrics,
            ["raw_media_data_png", "raw_media_data_jpeg", "raw_media_blob"],
        )

        self.assertFalse(gate["passed"])
        self.assertEqual(gate["missing_keys"], ["raw_media_blob"])
        self.assertEqual(
            gate["failing_keys"]["raw_media_data_jpeg"]["search_hit_at_3_rate"],
            0.5,
        )

    def test_run_profile_benchmark_forwards_required_coverage(self) -> None:
        original_runner = bench.run_local_benchmark
        captured = {}
        try:
            sample_case = bench.select_cases(bench.default_cases(10), 1)

            def _runner(
                profile,
                model_env,
                cases,
                *,
                required_coverage=None,
                progress_callback=None,
                case_started_callback=None,
                stop_requested=None,
            ):
                captured["profile"] = profile
                captured["required_coverage"] = required_coverage
                captured["case_ids"] = [case.case_id for case in cases]
                return {
                    "profile": profile,
                    "status": "completed",
                    "metrics": {
                        "store_success_rate": 1.0,
                        "search_hit_at_3_rate": 1.0,
                        "get_contains_expected_rate": 1.0,
                        "duplicate_new_success_rate": 1.0,
                        "visual_context_reuse_success_rate": 1.0,
                        "runtime_visual_harvest_success_rate": 1.0,
                    },
                    "runtime_probe": None,
                    "results": [],
                }

            bench.run_local_benchmark = _runner
            payload = bench.run_profile_benchmark(
                "a",
                {},
                sample_case,
                required_coverage=["raw_media_data_png"],
            )
        finally:
            bench.run_local_benchmark = original_runner

        self.assertEqual(captured["profile"], "a")
        self.assertEqual(captured["required_coverage"], ["raw_media_data_png"])
        self.assertEqual(captured["case_ids"], [sample_case[0].case_id])
        self.assertEqual(payload["status"], "completed")

    def test_build_coverage_gate_status_is_non_blocking_without_required_keys(self) -> None:
        gate = bench.build_coverage_gate_status({"coverage_summary": {}}, [])

        self.assertTrue(gate["passed"])
        self.assertEqual(gate["missing_keys"], [])
        self.assertEqual(gate["failing_keys"], {})

    def test_render_family_gate_uses_family_summary_success_and_failure_rates(self) -> None:
        metrics = {
            "family_summary": {
                "raw_media_mixed": {
                    "cases": 2,
                    "store_success_rate": 1.0,
                    "search_hit_at_3_rate": 1.0,
                    "get_contains_expected_rate": 1.0,
                },
                "raw_media_presigned": {
                    "cases": 2,
                    "store_success_rate": 1.0,
                    "search_hit_at_3_rate": 0.5,
                    "get_contains_expected_rate": 1.0,
                },
            }
        }

        self.assertEqual(bench._render_family_gate(metrics, "raw_media_mixed"), "pass")
        self.assertEqual(
            bench._render_family_gate(metrics, "raw_media_presigned"),
            "fail(store=1.0, hit@3=0.5, get=1.0)",
        )

    def test_is_transient_lock_output_detects_sqlite_lock_signals(self) -> None:
        self.assertTrue(
            bench.is_transient_lock_output(
                "",
                "sqlite3.OperationalError: database is locked",
            )
        )
        self.assertTrue(
            bench.is_transient_lock_output(
                "Error executing tool read_memory: query-invoked autoflush",
                "",
            )
        )
        self.assertFalse(bench.is_transient_lock_output("ok", ""))

    def test_evaluate_case_fails_get_validation_when_forbidden_media_fragment_leaks(self) -> None:
        original_runner = bench._timed_run_with_lock_retry
        try:
            def _runner(cmd, *, env, cwd, timeout=300, max_attempts=4, base_sleep_seconds=0.4):
                if cmd[2] == "store-visual":
                    proc = type(
                        "Proc",
                        (),
                        {
                            "returncode": 0,
                            "stdout": json.dumps(
                                {
                                    "path": "memory-palace/core/visual/demo.md",
                                    "uri": "core://visual/demo",
                                }
                            ),
                            "stderr": "",
                        },
                    )()
                    return proc, 10.0
                if cmd[2] == "search":
                    proc = type(
                        "Proc",
                        (),
                        {
                            "returncode": 0,
                            "stdout": json.dumps(
                                {
                                    "results": [
                                        {"path": "memory-palace/core/visual/demo.md"},
                                    ]
                                }
                            ),
                            "stderr": "",
                        },
                    )()
                    return proc, 20.0
                if cmd[2] == "get":
                    proc = type(
                        "Proc",
                        (),
                        {
                            "returncode": 0,
                            "stdout": json.dumps(
                                {
                                    "text": "# Visual Memory\n- summary: presigned board\n- media_ref: https://signed.example?X-Amz-Signature=secret",
                                }
                            ),
                            "stderr": "",
                        },
                    )()
                    return proc, 30.0
                raise AssertionError(f"unexpected command: {cmd}")

            bench._timed_run_with_lock_retry = _runner

            case = bench.VisualBenchmarkCase(
                case_id="raw_media_presigned_001",
                family="raw_media_presigned",
                complexity="complex",
                description="presigned",
                query="presigned token",
                store_args=["--media-ref", "https://signed.example", "--summary", "presigned board"],
                expected_get_substrings=["presigned board"],
                forbidden_get_substrings=["X-Amz-Signature="],
            )
            result = bench.evaluate_case(case, env={}, cwd=Path.cwd())
        finally:
            bench._timed_run_with_lock_retry = original_runner

        self.assertFalse(result.get_contains_expected)
        self.assertIn("get_missing_expected_content", result.notes)

    def test_run_recorder_writes_partial_artifacts_during_progress(self) -> None:
        case_catalog = bench.default_cases(10)
        selected_cases = bench.select_cases(case_catalog, 8)
        sample_result = bench.VisualBenchmarkResult(
            case_id="ocr_exact_001",
            family="ocr_exact",
            complexity="basic",
            description="ocr",
            store_ok=True,
            search_hit_at_3=True,
            reciprocal_rank_at_3=1.0,
            get_contains_expected=True,
            store_latency_ms=100.0,
            search_latency_ms=80.0,
            get_latency_ms=60.0,
            stored_path="memory-palace/core/demo.md",
            stored_uri="core://demo",
            query="launch checklist",
            notes=[],
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            json_output = Path(tmp_dir) / "bench.json"
            markdown_output = Path(tmp_dir) / "bench.md"
            recorder = bench.VisualBenchmarkRunRecorder(
                profiles=["a", "b"],
                case_catalog=case_catalog,
                selected_cases=selected_cases,
                json_output=json_output,
                markdown_output=markdown_output,
            )
            recorder.mark_profile_started("a")
            recorder.record_case_result("a", sample_result)

            payload = json.loads(json_output.read_text(encoding="utf-8"))
            markdown = markdown_output.read_text(encoding="utf-8")

        self.assertEqual(payload["status"], "running")
        self.assertTrue(payload["partial"])
        self.assertEqual(payload["profiles"][0]["completed_case_count"], 1)
        self.assertEqual(payload["profiles"][0]["last_case_id"], "ocr_exact_001")
        self.assertIn("status: `running`", markdown)
        self.assertIn("| a | running | 1/8 |", markdown)

    def test_run_recorder_tracks_current_case_heartbeat(self) -> None:
        case_catalog = bench.default_cases(10)
        selected_cases = bench.select_cases(case_catalog, 8)
        with tempfile.TemporaryDirectory() as tmp_dir:
            json_output = Path(tmp_dir) / "bench.json"
            markdown_output = Path(tmp_dir) / "bench.md"
            recorder = bench.VisualBenchmarkRunRecorder(
                profiles=["a"],
                case_catalog=case_catalog,
                selected_cases=selected_cases,
                json_output=json_output,
                markdown_output=markdown_output,
            )
            recorder.mark_profile_started("a")
            recorder.mark_case_started("a", selected_cases[0])

            payload = json.loads(json_output.read_text(encoding="utf-8"))

        profile_payload = payload["profiles"][0]
        self.assertEqual(profile_payload["current_case_id"], selected_cases[0].case_id)
        self.assertEqual(profile_payload["current_case_family"], selected_cases[0].family)
        self.assertIsNotNone(profile_payload["current_case_started_at"])

    def test_run_recorder_can_resume_from_existing_payload(self) -> None:
        case_catalog = bench.default_cases(10)
        selected_cases = bench.select_cases(case_catalog, 4)
        sample_case = selected_cases[0]
        sample_result = bench.VisualBenchmarkResult(
            case_id=sample_case.case_id,
            family=sample_case.family,
            complexity=sample_case.complexity,
            description=sample_case.description,
            store_ok=True,
            search_hit_at_3=True,
            reciprocal_rank_at_3=1.0,
            get_contains_expected=True,
            store_latency_ms=10.0,
            search_latency_ms=10.0,
            get_latency_ms=10.0,
            stored_path="memory-palace/core/demo.md",
            stored_uri="core://demo",
            query=sample_case.query,
            notes=[],
            coverage_key=sample_case.coverage_key,
        )
        payload = {
            "started_at": "2026-03-16T00:00:00Z",
            "profiles": [
                {
                    "profile": "a",
                    "status": "running",
                    "started_at": "2026-03-16T00:00:00Z",
                    "finished_at": None,
                    "completed_case_count": 1,
                    "total_case_count": 4,
                    "last_case_id": sample_case.case_id,
                    "runtime_probe": None,
                    "results": [bench.asdict(sample_result)],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmp_dir:
            recorder = bench.VisualBenchmarkRunRecorder.from_payload(
                payload=payload,
                profiles=["a"],
                case_catalog=case_catalog,
                selected_cases=selected_cases,
                json_output=Path(tmp_dir) / "bench.json",
                markdown_output=Path(tmp_dir) / "bench.md",
            )

            completed_ids = recorder.completed_case_ids("a")
            existing_results = recorder.existing_result_objects("a")

        self.assertEqual(completed_ids, {sample_case.case_id})
        self.assertEqual(len(existing_results), 1)
        self.assertEqual(existing_results[0].case_id, sample_case.case_id)

    def test_write_text_atomic_supports_parallel_flushes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            target = Path(tmp_dir) / "artifact.json"

            def _write(value: str) -> None:
                bench._write_text_atomic(target, value)

            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(_write, f"payload-{index}") for index in range(2)]
                for future in futures:
                    future.result()

            self.assertIn(target.read_text(encoding="utf-8"), {"payload-0", "payload-1"})

    def test_run_local_benchmark_marks_interrupted_and_skips_runtime_probe(self) -> None:
        stop_requested = threading.Event()
        case_catalog = bench.default_cases(10)
        selected_cases = bench.select_cases(case_catalog, 2)

        original_build_profile_env = bench.smoke.build_profile_env
        original_seed_local_memory = bench.smoke.seed_local_memory
        original_build_openclaw_config = bench.smoke.build_openclaw_config
        original_evaluate_case = bench.evaluate_case
        original_probe_runtime_visual_harvest = bench.probe_runtime_visual_harvest

        try:
            bench.smoke.build_profile_env = lambda *_args, **_kwargs: {
                "DATABASE_URL": "sqlite+aiosqlite:////tmp/fake.db"
            }
            bench.smoke.seed_local_memory = lambda *_args, **_kwargs: None
            bench.smoke.build_openclaw_config = lambda *_args, **_kwargs: None

            def _evaluate_case(case, *, env, cwd):
                stop_requested.set()
                return bench.VisualBenchmarkResult(
                    case_id=case.case_id,
                    family=case.family,
                    complexity=case.complexity,
                    description=case.description,
                    store_ok=True,
                    search_hit_at_3=True,
                    reciprocal_rank_at_3=1.0,
                    get_contains_expected=True,
                    store_latency_ms=10.0,
                    search_latency_ms=10.0,
                    get_latency_ms=10.0,
                    stored_path="memory-palace/core/demo.md",
                    stored_uri="core://demo",
                    query=case.query,
                    notes=[],
                )

            bench.evaluate_case = _evaluate_case

            def _unexpected_runtime_probe(*, cwd):
                raise AssertionError("runtime probe should be skipped when interrupted")

            bench.probe_runtime_visual_harvest = _unexpected_runtime_probe

            payload = bench.run_local_benchmark(
                "a",
                {},
                selected_cases,
                stop_requested=stop_requested,
            )
        finally:
            bench.smoke.build_profile_env = original_build_profile_env
            bench.smoke.seed_local_memory = original_seed_local_memory
            bench.smoke.build_openclaw_config = original_build_openclaw_config
            bench.evaluate_case = original_evaluate_case
            bench.probe_runtime_visual_harvest = original_probe_runtime_visual_harvest

        self.assertEqual(payload["status"], "interrupted")
        self.assertTrue(payload["interrupted"])
        self.assertEqual(payload["executed_case_count"], 1)
        self.assertEqual(payload["runtime_probe"], None)
        self.assertEqual(len(payload["results"]), 1)

    def test_evaluate_case_retries_search_and_get_after_index_wait_for_deferred_index(self) -> None:
        original_runner = bench._timed_run_with_lock_retry
        try:
            call_names: list[str] = []

            def _runner(cmd, *, env, cwd, timeout=300, max_attempts=4, base_sleep_seconds=0.4):
                call_names.append(cmd[2])
                if cmd[2] == "store-visual":
                    proc = type(
                        "Proc",
                        (),
                        {
                            "returncode": 0,
                            "stdout": json.dumps(
                                {
                                    "path": "memory-palace/core/visual/demo.md",
                                    "uri": "core://visual/demo",
                                }
                            ),
                            "stderr": "",
                        },
                    )()
                    return proc, 10.0
                if cmd[2] == "search":
                    if call_names.count("search") == 1:
                        proc = type(
                            "Proc",
                            (),
                            {
                                "returncode": 0,
                                "stdout": json.dumps({"results": []}),
                                "stderr": "",
                            },
                        )()
                        return proc, 20.0
                    proc = type(
                        "Proc",
                        (),
                        {
                            "returncode": 0,
                            "stdout": json.dumps(
                                {
                                    "results": [
                                        {
                                            "path": "memory-palace/core/visual/demo.md",
                                        }
                                    ]
                                }
                            ),
                            "stderr": "",
                        },
                    )()
                    return proc, 21.0
                if cmd[2] == "get":
                    if call_names.count("get") == 1:
                        proc = type(
                            "Proc",
                            (),
                            {
                                "returncode": 0,
                                "stdout": json.dumps({"text": "partial"}),
                                "stderr": "",
                            },
                        )()
                        return proc, 30.0
                    proc = type(
                        "Proc",
                        (),
                        {
                            "returncode": 0,
                            "stdout": json.dumps(
                                {
                                    "text": "expected summary provenance_summary_source: direct",
                                }
                            ),
                            "stderr": "",
                        },
                    )()
                    return proc, 31.0
                if cmd[2] == "index":
                    proc = type(
                        "Proc",
                        (),
                        {
                            "returncode": 0,
                            "stdout": json.dumps({"result": {"ok": True}}),
                            "stderr": "",
                        },
                    )()
                    return proc, 40.0
                raise AssertionError(cmd)

            bench._timed_run_with_lock_retry = _runner
            case = bench.VisualBenchmarkCase(
                case_id="summary_overlap_022",
                family="summary_overlap",
                complexity="basic",
                description="summary overlap",
                query="shipping blockers bench-summary-overlap-022 notebook",
                store_args=["--media-ref", "file:/tmp/demo.png", "--summary", "expected summary"],
                expected_get_substrings=["expected summary", "provenance_summary_source: direct"],
            )
            result = bench.evaluate_case(
                case,
                env={"RUNTIME_INDEX_DEFER_ON_WRITE": "true"},
                cwd=Path("."),
            )
        finally:
            bench._timed_run_with_lock_retry = original_runner

        self.assertTrue(result.store_ok)
        self.assertTrue(result.search_hit_at_3)
        self.assertTrue(result.get_contains_expected)
        self.assertEqual(result.reciprocal_rank_at_3, 1.0)
        self.assertIn("search_recovered_after_index_wait", result.notes)
        self.assertIn("get_recovered_after_index_wait", result.notes)
        self.assertEqual(call_names.count("index"), 1)

    def test_run_local_benchmark_retries_failed_case_once(self) -> None:
        original_build_profile_env = bench.smoke.build_profile_env
        original_seed_local_memory = bench.smoke.seed_local_memory
        original_build_openclaw_config = bench.smoke.build_openclaw_config
        original_evaluate_case = bench.evaluate_case
        original_probe_runtime_visual_harvest = bench.probe_runtime_visual_harvest
        original_sleep = bench.time.sleep
        try:
            bench.smoke.build_profile_env = lambda platform, profile, target, model_env: {  # type: ignore[assignment]
                "DATABASE_URL": "sqlite+aiosqlite:////tmp/test.db"
            }
            bench.smoke.seed_local_memory = lambda database_url: None  # type: ignore[assignment]
            bench.smoke.build_openclaw_config = (  # type: ignore[assignment]
                lambda config_path, transport, stdio_env, workspace_dir=None: None
            )
            bench.time.sleep = lambda seconds: None  # type: ignore[assignment]

            call_counts: dict[str, int] = {}

            def _evaluate_case(case, *, env, cwd):
                count = call_counts.get(case.case_id, 0) + 1
                call_counts[case.case_id] = count
                if count == 1:
                    return bench.VisualBenchmarkResult(
                        case_id=case.case_id,
                        family=case.family,
                        complexity=case.complexity,
                        description=case.description,
                        store_ok=False,
                        search_hit_at_3=False,
                        reciprocal_rank_at_3=0.0,
                        get_contains_expected=False,
                        store_latency_ms=10.0,
                        search_latency_ms=11.0,
                        get_latency_ms=12.0,
                        stored_path=None,
                        stored_uri=None,
                        query=case.query,
                        notes=["store_failed:transient"],
                        coverage_key=case.coverage_key,
                    )
                return bench.VisualBenchmarkResult(
                    case_id=case.case_id,
                    family=case.family,
                    complexity=case.complexity,
                    description=case.description,
                    store_ok=True,
                    search_hit_at_3=True,
                    reciprocal_rank_at_3=1.0,
                    get_contains_expected=True,
                    store_latency_ms=13.0,
                    search_latency_ms=14.0,
                    get_latency_ms=15.0,
                    stored_path="memory-palace/core/demo.md",
                    stored_uri="core://demo",
                    query=case.query,
                    notes=[],
                    coverage_key=case.coverage_key,
                )

            bench.evaluate_case = _evaluate_case
            bench.probe_runtime_visual_harvest = lambda *, cwd: {  # type: ignore[assignment]
                "runtime_visual_probe": "message_preprocessed",
                "runtime_visual_harvest_success_rate": 1.0,
                "runtime_visual_harvest_cases": [],
            }

            case = bench.VisualBenchmarkCase(
                case_id="summary_overlap_123",
                family="summary_overlap",
                complexity="basic",
                description="retry whole case",
                query="summary overlap retry",
                store_args=["--media-ref", "file:/tmp/demo.png", "--summary", "demo"],
                expected_get_substrings=["demo"],
            )
            payload = bench.run_local_benchmark("a", {}, [case])
        finally:
            bench.smoke.build_profile_env = original_build_profile_env
            bench.smoke.seed_local_memory = original_seed_local_memory
            bench.smoke.build_openclaw_config = original_build_openclaw_config
            bench.evaluate_case = original_evaluate_case
            bench.probe_runtime_visual_harvest = original_probe_runtime_visual_harvest
            bench.time.sleep = original_sleep

        self.assertEqual(call_counts["summary_overlap_123"], 2)
        result = bench.VisualBenchmarkResult(**payload["results"][0])
        self.assertTrue(result.store_ok)
        self.assertTrue(result.search_hit_at_3)
        self.assertTrue(result.get_contains_expected)
        self.assertIn("case_retried_after_failure", result.notes)
        self.assertIn("case_recovered_after_full_retry", result.notes)

    def test_evaluate_case_recovers_store_failure_when_record_becomes_readable_after_index_wait(self) -> None:
        original_runner = bench._timed_run_with_lock_retry
        try:
            call_names: list[str] = []

            def _runner(cmd, *, env, cwd, timeout=300, max_attempts=4, base_sleep_seconds=0.4):
                call_names.append(cmd[2])
                if cmd[2] == "store-visual":
                    proc = type(
                        "Proc",
                        (),
                        {
                            "returncode": 1,
                            "stdout": json.dumps(
                                {
                                    "error": "Error: Memory at 'core://visual/demo' not found.",
                                    "path": "memory-palace/core/visual/demo.md",
                                    "uri": "core://visual/demo",
                                }
                            ),
                            "stderr": "",
                        },
                    )()
                    return proc, 10.0
                if cmd[2] == "search":
                    if call_names.count("search") == 1:
                        proc = type(
                            "Proc",
                            (),
                            {
                                "returncode": 0,
                                "stdout": json.dumps({"results": []}),
                                "stderr": "",
                            },
                        )()
                        return proc, 20.0
                    proc = type(
                        "Proc",
                        (),
                        {
                            "returncode": 0,
                            "stdout": json.dumps(
                                {
                                    "results": [
                                        {
                                            "path": "memory-palace/core/visual/demo.md",
                                        }
                                    ]
                                }
                            ),
                            "stderr": "",
                        },
                    )()
                    return proc, 21.0
                if cmd[2] == "get":
                    if call_names.count("get") == 1:
                        proc = type(
                            "Proc",
                            (),
                            {
                                "returncode": 1,
                                "stdout": "",
                                "stderr": "Error: Memory at 'core://visual/demo' not found.",
                            },
                        )()
                        return proc, 30.0
                    proc = type(
                        "Proc",
                        (),
                        {
                            "returncode": 0,
                            "stdout": json.dumps(
                                {
                                    "text": "expected summary provenance_summary_source: direct",
                                }
                            ),
                            "stderr": "",
                        },
                    )()
                    return proc, 31.0
                if cmd[2] == "index":
                    proc = type(
                        "Proc",
                        (),
                        {
                            "returncode": 0,
                            "stdout": json.dumps({"result": {"ok": True}}),
                            "stderr": "",
                        },
                    )()
                    return proc, 40.0
                raise AssertionError(cmd)

            bench._timed_run_with_lock_retry = _runner
            case = bench.VisualBenchmarkCase(
                case_id="summary_overlap_099",
                family="summary_overlap",
                complexity="basic",
                description="summary overlap recover store failure",
                query="shipping blockers bench-summary-overlap-099 tracker",
                store_args=["--media-ref", "file:/tmp/demo.png", "--summary", "expected summary"],
                expected_get_substrings=["expected summary", "provenance_summary_source: direct"],
            )
            result = bench.evaluate_case(
                case,
                env={"RUNTIME_INDEX_DEFER_ON_WRITE": "true"},
                cwd=Path("."),
            )
        finally:
            bench._timed_run_with_lock_retry = original_runner

        self.assertTrue(result.store_ok)
        self.assertTrue(result.search_hit_at_3)
        self.assertTrue(result.get_contains_expected)
        self.assertIn("store_recovered_after_index_wait", result.notes)
        self.assertNotIn("get_missing_expected_content", result.notes)
        self.assertFalse(any(note.startswith("store_failed:") for note in result.notes))

    def test_evaluate_case_rewrites_duplicate_new_query_to_actual_variant_label(self) -> None:
        original_runner = bench._timed_run_with_lock_retry
        try:
            search_queries: list[str] = []

            def _runner(cmd, *, env, cwd, timeout=300, max_attempts=4, base_sleep_seconds=0.4):
                if cmd[2] == "store-visual":
                    proc = type(
                        "Proc",
                        (),
                        {
                            "returncode": 0,
                            "stdout": json.dumps(
                                {
                                    "path": "memory-palace/core/visual/demo--new-02.md",
                                    "uri": "core://visual/demo--new-02",
                                }
                            ),
                            "stderr": "",
                        },
                    )()
                    return proc, 10.0
                if cmd[2] == "search":
                    search_queries.append(cmd[3])
                    proc = type(
                        "Proc",
                        (),
                        {
                            "returncode": 0,
                            "stdout": json.dumps(
                                {
                                    "results": [
                                        {
                                            "path": "memory-palace/core/visual/demo--new-02.md",
                                        }
                                    ]
                                }
                            ),
                            "stderr": "",
                        },
                    )()
                    return proc, 20.0
                if cmd[2] == "get":
                    proc = type(
                        "Proc",
                        (),
                        {
                            "returncode": 0,
                            "stdout": json.dumps(
                                {
                                    "text": "duplicate_variant: new-02 provenance_variant_uri:",
                                }
                            ),
                            "stderr": "",
                        },
                    )()
                    return proc, 30.0
                raise AssertionError(cmd)

            bench._timed_run_with_lock_retry = _runner
            case = bench.VisualBenchmarkCase(
                case_id="duplicate_new_016",
                family="duplicate_new",
                complexity="complex",
                description="duplicate",
                query="bench-duplicate-new-016 duplicate variant new-01",
                store_args=["--media-ref", "file:/tmp/demo.png", "--duplicate-policy", "new"],
                expected_get_substrings=["duplicate_variant:", "provenance_variant_uri:"],
            )
            result = bench.evaluate_case(case, env={}, cwd=Path("."))
        finally:
            bench._timed_run_with_lock_retry = original_runner

        self.assertEqual(search_queries, ["bench-duplicate-new-016 duplicate variant new-02"])
        self.assertEqual(result.query, "bench-duplicate-new-016 duplicate variant new-02")
        self.assertEqual(result.reciprocal_rank_at_3, 1.0)


if __name__ == "__main__":
    unittest.main()

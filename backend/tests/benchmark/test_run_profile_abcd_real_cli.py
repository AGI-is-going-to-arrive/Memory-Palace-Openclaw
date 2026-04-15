import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

BENCHMARK_DIR = Path(__file__).resolve().parent
if str(BENCHMARK_DIR) not in sys.path:
    sys.path.insert(0, str(BENCHMARK_DIR))

import run_profile_abcd_real as cli_runner  # noqa: E402


@pytest.mark.asyncio
async def test_run_profile_abcd_real_compare_mode_writes_separate_markdown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = []

    async def _fake_build_profile_abcd_real_metrics(**kwargs):
        calls.append(dict(kwargs))
        return {
            "generated_at_utc": "2026-03-10T12:00:00+00:00",
            "dataset_scope": ["beir_nfcorpus"],
            "profiles": {"profile_d": {"rows": []}},
            "real_run_strategy": {
                "entrypoint": kwargs["entrypoint"],
                "profiles": list(kwargs["profile_keys"]),
                "factual_pool_cap": kwargs.get("factual_pool_cap"),
            },
            "phase6": {"gate": {"valid": True}, "comparison_rows": []},
        }

    def _fake_write_profile_abcd_real_artifacts(payload, *, json_path, markdown_path, cd_markdown_path):
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text("json\n", encoding="utf-8")
        markdown_path.write_text("md\n", encoding="utf-8")
        cd_markdown_path.write_text("cd\n", encoding="utf-8")
        return {
            "json": json_path,
            "markdown": markdown_path,
            "cd_markdown": cd_markdown_path,
        }

    monkeypatch.setattr(
        cli_runner,
        "build_profile_abcd_real_metrics",
        _fake_build_profile_abcd_real_metrics,
    )
    monkeypatch.setattr(
        cli_runner,
        "write_profile_abcd_real_artifacts",
        _fake_write_profile_abcd_real_artifacts,
    )
    monkeypatch.setattr(
        cli_runner,
        "render_abcd_sota_analysis_markdown",
        lambda _payload: "analysis\n",
    )
    monkeypatch.setattr(
        cli_runner,
        "render_factual_pool_cap_compare_markdown",
        lambda _baseline, _compare: "compare\n",
    )

    args = SimpleNamespace(
        phase6_gate_mode=None,
        phase6_invalid_rate_threshold=None,
        sample_size=8,
        datasets="beir_nfcorpus",
        profiles="d",
        factual_pool_cap=None,
        all_relevant=True,
        extra_distractors=200,
        max_results=10,
        candidate_multiplier=8,
        entrypoint="mcp_search_memory",
        workdir=tmp_path / "workdir",
        output_json=tmp_path / "baseline.json",
        output_md=tmp_path / "baseline.md",
        output_cd_md=tmp_path / "baseline_cd.md",
        analysis_output=tmp_path / "analysis.md",
        compare_factual_pool_cap=0,
        compare_output=tmp_path / "compare.md",
    )

    await cli_runner._run(args)

    assert len(calls) == 2
    assert calls[0]["factual_pool_cap"] is None
    assert calls[0]["workdir"] == tmp_path / "workdir" / "baseline"
    assert calls[1]["factual_pool_cap"] == 0
    assert calls[1]["workdir"] == tmp_path / "workdir" / "compare-factual-cap-0"
    assert args.output_json.read_text(encoding="utf-8") == "json\n"
    assert args.output_md.read_text(encoding="utf-8") == "md\n"
    assert args.output_cd_md.read_text(encoding="utf-8") == "cd\n"
    assert args.analysis_output.read_text(encoding="utf-8") == "analysis\n"
    assert args.compare_output.read_text(encoding="utf-8") == "compare\n"

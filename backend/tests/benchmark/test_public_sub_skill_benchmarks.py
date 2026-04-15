"""Public sub-skill benchmarks: MASSIVE / XNLI / PAWS-X / LCSTS / WikiLingua.

These benchmarks validate generic sub-skill capabilities (intent classification,
contradiction detection, paraphrase detection, summarization) using publicly
available datasets.

IMPORTANT:
  - These are SUB-SKILL benchmarks only, not product decision benchmarks.
  - They do NOT directly replace product gold sets for write_guard ADD/UPDATE/NOOP
    or compact_context session-trace -> gist evaluation.
  - Public datasets must be pre-downloaded via prepare_public_sub_skill_datasets.py.
    If the local cache is missing, tests skip (no implicit network dependency).

Each capability skips independently based on its own data availability and
service health requirements.

Artifacts:
  - public_sub_skill_benchmarks_report.json
  - public_sub_skill_benchmarks_report.md
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import pytest

BENCHMARK_DIR = Path(__file__).resolve().parent
BACKEND_ROOT = BENCHMARK_DIR.parents[2]
DATASETS_DIR = BENCHMARK_DIR.parent / "datasets" / "sub_skill"

if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))
if str(BENCHMARK_DIR) not in sys.path:
    sys.path.insert(0, str(BENCHMARK_DIR))

from db.sqlite_client import SQLiteClient
from helpers.health_checks import check_llm_health

REPORT_JSON = BENCHMARK_DIR / "public_sub_skill_benchmarks_report.json"
REPORT_MD = BENCHMARK_DIR / "public_sub_skill_benchmarks_report.md"

# Module-level flag: ensures the report file is wiped exactly once per
# pytest session, so stale keys from previous runs never carry over.
_REPORT_CLEANED = False


# ---------------------------------------------------------------------------
# Data loading (from local cache only, no network)
# ---------------------------------------------------------------------------

def _load_cached_jsonl(name: str) -> List[Dict[str, Any]]:
    """Load a cached sub-skill dataset. Returns empty list if not available."""
    path = DATASETS_DIR / f"{name}.jsonl"
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if raw:
            rows.append(json.loads(raw))
    return rows


def _skip_if_no_data(name: str, data: List) -> None:
    if not data:
        pytest.skip(
            f"Public dataset '{name}' not prepared. "
            f"Run: python backend/tests/benchmark/helpers/prepare_public_sub_skill_datasets.py"
        )


# ---------------------------------------------------------------------------
# ROUGE-L (inline)
# ---------------------------------------------------------------------------

def _tokenize(text: str) -> List[str]:
    return re.findall(r"[a-z0-9\u4e00-\u9fff]+", text.lower())


def _lcs_length(a: List[str], b: List[str]) -> int:
    if not a or not b:
        return 0
    m, n = len(a), len(b)
    dp = [[0] * (n + 1) for _ in range(m + 1)]
    for i in range(1, m + 1):
        for j in range(1, n + 1):
            if a[i - 1] == b[j - 1]:
                dp[i][j] = dp[i - 1][j - 1] + 1
            else:
                dp[i][j] = max(dp[i - 1][j], dp[i][j - 1])
    return dp[m][n]


def _rouge_l_f1(reference: str, candidate: str) -> float:
    ref_tok = _tokenize(reference)
    cand_tok = _tokenize(candidate)
    if not ref_tok or not cand_tok:
        return 0.0
    lcs = _lcs_length(ref_tok, cand_tok)
    p = lcs / len(cand_tok)
    r = lcs / len(ref_tok)
    if (p + r) == 0:
        return 0.0
    return (2 * p * r) / (p + r)


# ---------------------------------------------------------------------------
# MASSIVE intent benchmark
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_massive_intent_sub_skill() -> None:
    """Evaluate keyword intent classifier on MASSIVE zh+en subset."""
    data = _load_cached_jsonl("massive_intent")
    _skip_if_no_data("massive_intent", data)

    client = SQLiteClient("sqlite+aiosqlite:///:memory:")

    cases = []
    try:
        for row in data:
            query = str(row["text"])
            expected = str(row["mp_intent"])
            normalized = client.preprocess_query(query)
            result = client.classify_intent(query, normalized.get("rewritten_query"))
            predicted = str(result.get("intent") or "")
            cases.append({
                "expected": expected,
                "predicted": predicted,
                "correct": predicted == expected,
            })
    finally:
        await client.close()

    total = len(cases)
    correct = sum(1 for c in cases if c["correct"])
    accuracy = correct / total if total > 0 else 0.0

    by_intent: Dict[str, Dict[str, int]] = {}
    for c in cases:
        key = c["expected"]
        if key not in by_intent:
            by_intent[key] = {"total": 0, "correct": 0}
        by_intent[key]["total"] += 1
        if c["correct"]:
            by_intent[key]["correct"] += 1

    result_data = {
        "total": total,
        "accuracy": round(accuracy, 4),
        "by_mp_intent": {
            k: {**v, "accuracy": round(v["correct"] / v["total"], 4) if v["total"] > 0 else 0}
            for k, v in sorted(by_intent.items())
        },
    }

    # Write partial report
    _write_partial_report("massive_intent", result_data)

    # Sub-skill benchmark: no hard assertion on accuracy floor
    # (MASSIVE intents don't map 1:1 to MP intents)
    assert total > 0


# ---------------------------------------------------------------------------
# XNLI contradiction benchmark
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_xnli_contradiction_sub_skill() -> None:
    """Evaluate contradiction detection sub-skill on XNLI zh+en subset.

    For each pair, the premise is seeded as an existing memory, then the
    hypothesis is submitted through write_guard so the LLM-assisted
    contradiction path can compare the two.  This exercises the real
    write_guard contradiction sub-capability, not the full ADD/UPDATE/NOOP
    decision pipeline.
    """
    data = _load_cached_jsonl("xnli_contradiction")
    _skip_if_no_data("xnli_contradiction", data)

    llm_health = await check_llm_health()
    if llm_health["status"] != "ok":
        pytest.skip("LLM service unavailable for contradiction detection")

    import os
    import tempfile
    from helpers.real_retrieval_harness import seed_memories

    cases = []
    for idx, row in enumerate(data):
        premise = str(row["premise"])
        hypothesis = str(row["hypothesis"])
        expected_label = str(row["label"])  # "contradiction" or "not_contradiction"
        is_contradiction = expected_label == "contradiction"

        # Each pair gets its own temp DB so the premise is the only
        # existing memory when write_guard compares against it.
        fd, db_path = tempfile.mkstemp(suffix=".db", prefix="xnli_")
        os.close(fd)
        db_url = f"sqlite+aiosqlite:///{db_path}"

        client = SQLiteClient(db_url)
        try:
            await seed_memories(client, [{
                "uri": f"core://xnli/{idx}",
                "content": premise,
                "domain": "core",
            }])

            decision = await client.write_guard(
                content=hypothesis,
                domain="core",
            )
            has_contradiction = bool(decision.get("contradiction", False))
        except Exception:
            has_contradiction = False
        finally:
            await client.close()
            try:
                Path(db_path).unlink(missing_ok=True)
            except Exception:
                pass

        cases.append({
            "expected_contradiction": is_contradiction,
            "predicted_contradiction": has_contradiction,
            "correct": is_contradiction == has_contradiction,
        })

    total = len(cases)
    correct = sum(1 for c in cases if c["correct"])
    accuracy = correct / total if total > 0 else 0.0

    _write_partial_report("xnli_contradiction", {
        "total": total,
        "accuracy": round(accuracy, 4),
    })

    assert total > 0


# ---------------------------------------------------------------------------
# PAWS-X paraphrase benchmark
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pawsx_paraphrase_sub_skill() -> None:
    """Heuristic baseline for paraphrase detection on PAWS-X zh+en subset.

    This is an EXPLICIT HEURISTIC BASELINE using Jaccard token overlap.
    It measures how well a simple lexical signal separates paraphrase from
    non-paraphrase pairs, providing a floor against which embedding-based
    or LLM-based duplicate detectors can be compared.

    It does NOT exercise a product sub-skill path directly.
    It does NOT map paraphrase=1 -> UPDATE or paraphrase=0 -> ADD.
    """
    data = _load_cached_jsonl("pawsx_paraphrase")
    _skip_if_no_data("pawsx_paraphrase", data)

    cases = []
    for row in data:
        sent1 = str(row["sentence1"])
        sent2 = str(row["sentence2"])
        is_paraphrase = bool(row.get("label", 0))

        # Jaccard token overlap — intentionally simple baseline
        tok1 = set(_tokenize(sent1))
        tok2 = set(_tokenize(sent2))
        if tok1 and tok2:
            jaccard = len(tok1 & tok2) / len(tok1 | tok2)
        else:
            jaccard = 0.0

        predicted_paraphrase = jaccard > 0.5

        cases.append({
            "expected": is_paraphrase,
            "predicted": predicted_paraphrase,
            "correct": is_paraphrase == predicted_paraphrase,
        })

    total = len(cases)
    correct = sum(1 for c in cases if c["correct"])
    accuracy = correct / total if total > 0 else 0.0

    _write_partial_report("pawsx_paraphrase", {
        "total": total,
        "accuracy": round(accuracy, 4),
        "method": "jaccard_token_overlap_heuristic_baseline",
    })

    assert total > 0


# ---------------------------------------------------------------------------
# LCSTS gist benchmark
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_lcsts_gist_sub_skill() -> None:
    """Evaluate gist quality on LCSTS zh subset."""
    data = _load_cached_jsonl("lcsts_gist")
    _skip_if_no_data("lcsts_gist", data)

    llm_health = await check_llm_health()

    client = SQLiteClient("sqlite+aiosqlite:///:memory:")
    cases = []
    try:
        for row in data:
            source = str(row["source"])
            reference = str(row["summary"])

            gist_text = ""
            if llm_health["status"] == "ok":
                try:
                    result = await client.generate_compact_gist(
                        summary=source, max_points=3, max_chars=280,
                    )
                    if result and result.get("gist_text"):
                        gist_text = str(result["gist_text"])
                except Exception:
                    pass

            if not gist_text:
                # Extractive fallback
                sentences = re.split(r'[.。!！?？;；]', source)
                sentences = [s.strip() for s in sentences if s.strip()]
                gist_text = "; ".join(sentences[:2])[:280]

            rouge = _rouge_l_f1(reference, gist_text)
            cases.append({"rouge_l": round(rouge, 4)})
    finally:
        await client.close()

    rouge_mean = sum(c["rouge_l"] for c in cases) / len(cases) if cases else 0.0

    _write_partial_report("lcsts_gist", {
        "total": len(cases),
        "rouge_l_mean": round(rouge_mean, 4),
    })

    assert len(cases) > 0


# ---------------------------------------------------------------------------
# WikiLingua gist benchmark
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_wikilingua_gist_sub_skill() -> None:
    """Evaluate gist quality on WikiLingua zh+en subset."""
    data = _load_cached_jsonl("wikilingua_gist")
    _skip_if_no_data("wikilingua_gist", data)

    llm_health = await check_llm_health()

    client = SQLiteClient("sqlite+aiosqlite:///:memory:")
    cases = []
    try:
        for row in data:
            source = str(row["source"])
            reference = str(row["summary"])

            gist_text = ""
            if llm_health["status"] == "ok":
                try:
                    result = await client.generate_compact_gist(
                        summary=source, max_points=3, max_chars=280,
                    )
                    if result and result.get("gist_text"):
                        gist_text = str(result["gist_text"])
                except Exception:
                    pass

            if not gist_text:
                sentences = re.split(r'[.。!！?？;；]', source)
                sentences = [s.strip() for s in sentences if s.strip()]
                gist_text = "; ".join(sentences[:2])[:280]

            rouge = _rouge_l_f1(reference, gist_text)
            cases.append({"rouge_l": round(rouge, 4)})
    finally:
        await client.close()

    rouge_mean = sum(c["rouge_l"] for c in cases) / len(cases) if cases else 0.0

    _write_partial_report("wikilingua_gist", {
        "total": len(cases),
        "rouge_l_mean": round(rouge_mean, 4),
    })

    assert len(cases) > 0


# ---------------------------------------------------------------------------
# Report helpers
# ---------------------------------------------------------------------------

def _write_partial_report(key: str, data: Dict[str, Any]) -> None:
    """Write a sub-skill result to the report file.

    On the first call per pytest session the report is wiped so that stale
    keys from earlier runs never carry over.
    """
    global _REPORT_CLEANED  # noqa: PLW0603
    if not _REPORT_CLEANED:
        # Wipe any leftover report from a previous run
        REPORT_JSON.unlink(missing_ok=True)
        REPORT_MD.unlink(missing_ok=True)
        _REPORT_CLEANED = True

    report: Dict[str, Any] = {}
    if REPORT_JSON.exists():
        try:
            report = json.loads(REPORT_JSON.read_text(encoding="utf-8"))
        except Exception:
            pass

    report["generated_at_utc"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    report.setdefault("note", "Sub-skill benchmarks only; not product decision benchmarks")
    report.setdefault("benchmarks", {})
    report["benchmarks"][key] = data

    REPORT_JSON.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    # Regenerate markdown
    lines = [
        "# Public Sub-Skill Benchmarks Report",
        "",
        f"> generated: {report['generated_at_utc']}",
        "> NOTE: Sub-skill benchmarks only; not product decision benchmarks",
        "",
    ]
    for bk, bv in report.get("benchmarks", {}).items():
        lines.append(f"## {bk}")
        lines.append("")
        for mk, mv in bv.items():
            if isinstance(mv, dict):
                lines.append(f"- {mk}:")
                for dk, dv in mv.items():
                    lines.append(f"  - {dk}: {dv}")
            else:
                lines.append(f"- {mk}: {mv}")
        lines.append("")

    REPORT_MD.write_text("\n".join(lines), encoding="utf-8")

    # Timestamped archival copy — never overwrite previously accepted results
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive_json = BENCHMARK_DIR / f"public_sub_skill_benchmarks_report.{ts}.json"
    archive_md = BENCHMARK_DIR / f"public_sub_skill_benchmarks_report.{ts}.md"
    archive_json.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    archive_md.write_text("\n".join(lines), encoding="utf-8")

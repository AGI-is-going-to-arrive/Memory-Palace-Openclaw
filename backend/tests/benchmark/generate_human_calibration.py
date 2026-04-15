#!/usr/bin/env python3
"""Generate blinded human calibration dataset for pairwise gist evaluation.

Produces a Markdown file with 15 cases. Each case shows:
  - Source text
  - Gist X and Gist Y (randomized: one is extractive, one is LLM)
  - Scoring slots for: overall, coverage, faithfulness, conciseness

Also produces a JSON answer key (mapping X/Y back to method).

Usage:
    WRITE_GUARD_LLM_API_BASE=... WRITE_GUARD_LLM_API_KEY=... WRITE_GUARD_LLM_MODEL=... \
    python backend/tests/benchmark/generate_human_calibration.py
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import re
import sys
from pathlib import Path
from typing import Any, Dict, List

BENCHMARK_DIR = Path(__file__).resolve().parent
FIXTURES_DIR = BENCHMARK_DIR.parent / "fixtures"
BACKEND_ROOT = BENCHMARK_DIR.parents[2]
# Ensure both backend root and benchmark dir are on sys.path
for p in [str(BACKEND_ROOT), str(BENCHMARK_DIR)]:
    if p not in sys.path:
        sys.path.insert(0, p)

from db.sqlite_client import SQLiteClient
from helpers.gist_routing_policy import route_gist
from helpers.health_checks import check_llm_health

random.seed(2026_04_06)
SAMPLE_SIZE = 15

OUTPUT_MD = BENCHMARK_DIR / "human_calibration_sheet.md"
OUTPUT_KEY = BENCHMARK_DIR / "human_calibration_answer_key.json"


def _load_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if raw:
            rows.append(json.loads(raw))
    return rows


def _extractive_bullets(text: str) -> str:
    lines = text.strip().split("\n")
    bullets = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("- "):
            bullets.append(stripped[2:][:90])
    if bullets:
        return "; ".join(bullets[:3])
    sentences = re.split(r'[.。!！?？;；]', text)
    sentences = [s.strip() for s in sentences if s.strip()]
    return "; ".join(sentences[:2])[:280]


def _stratified_sample(rows, n):
    """Stratified by length_bucket to ensure coverage."""
    from collections import defaultdict
    buckets = defaultdict(list)
    for r in rows:
        buckets[r.get("length_bucket", "medium")].append(r)
    per = max(1, n // len(buckets))
    result = []
    for vals in buckets.values():
        result.extend(random.sample(vals, min(per, len(vals))))
    remaining = [r for r in rows if r not in result]
    while len(result) < n and remaining:
        result.append(remaining.pop())
    return result[:n]


async def main():
    health = await check_llm_health()
    if health["status"] != "ok":
        print(f"LLM unavailable: {health.get('reason', '?')}", file=sys.stderr)
        sys.exit(1)

    all_gist = _load_jsonl(FIXTURES_DIR / "gist_product_gold_set.jsonl")
    subset = _stratified_sample(all_gist, SAMPLE_SIZE)

    print(f"Sampled {len(subset)} cases for human calibration", file=sys.stderr)

    # Need to set env for LLM gist
    # (caller sets WRITE_GUARD_LLM_* env vars)
    os.environ.setdefault("COMPACT_GIST_LLM_ENABLED", "true")
    os.environ.setdefault("WRITE_GUARD_LLM_ENABLED", "true")

    client = SQLiteClient("sqlite+aiosqlite:///:memory:")
    await client.init_db()

    cases = []
    answer_key = []
    rng = random.Random(42)

    try:
        for idx, row in enumerate(subset):
            source = str(row["source_content"])
            lb = row.get("length_bucket", "medium")
            fb = row.get("format_bucket", "prose")
            lang = row.get("lang", "en")

            # B-off gist (always extractive)
            gist_boff = _extractive_bullets(source)

            # Hybrid gist
            routed = route_gist(lb, fb, lang)
            if routed == "llm_gist":
                gist_hybrid = ""
                try:
                    result = await client.generate_compact_gist(
                        summary=source, max_points=3, max_chars=280,
                    )
                    if result and result.get("gist_text"):
                        gist_hybrid = str(result["gist_text"])
                except Exception:
                    pass
                if not gist_hybrid:
                    gist_hybrid = _extractive_bullets(source)
            else:
                gist_hybrid = _extractive_bullets(source)

            # Blind: randomly assign to X/Y
            boff_is_x = rng.random() < 0.5
            if boff_is_x:
                gist_x, gist_y = gist_boff, gist_hybrid
            else:
                gist_x, gist_y = gist_hybrid, gist_boff

            cases.append({
                "case_num": idx + 1,
                "id": row["id"],
                "source": source,
                "gist_x": gist_x,
                "gist_y": gist_y,
                "lang": lang,
                "length_bucket": lb,
                "format_bucket": fb,
            })

            answer_key.append({
                "case_num": idx + 1,
                "id": row["id"],
                "x_method": "B-off" if boff_is_x else "Hybrid",
                "y_method": "Hybrid" if boff_is_x else "B-off",
                "length_bucket": lb,
                "format_bucket": fb,
                "lang": lang,
                "routed_to": routed,
            })

            print(f"  [{idx+1}/{SAMPLE_SIZE}] {row['id']} ({lang}/{lb}/{fb})", file=sys.stderr)
    finally:
        await client.close()

    # Write Markdown calibration sheet
    lines = [
        "# Human Calibration Sheet — Gist Pairwise Evaluation",
        "",
        "> **Instructions**: For each case, read the Source Text, then compare Gist X and Gist Y.",
        "> For each dimension, write: **X** (X is better), **Y** (Y is better), or **tie**.",
        "> You do NOT know which gist is extractive vs LLM — judge purely on quality.",
        "",
        "| Dimension | Meaning |",
        "|---|---|",
        "| overall | Which gist is better overall? |",
        "| coverage | Which captures more key information from the source? |",
        "| faithfulness | Which is more factually consistent (no fabrications)? |",
        "| conciseness | Which is more appropriately brief? |",
        "",
        "---",
        "",
    ]

    for c in cases:
        lines.append(f"## Case {c['case_num']} ({c['lang']}, {c['length_bucket']}, {c['format_bucket']})")
        lines.append("")
        lines.append("**Source Text:**")
        lines.append("```")
        lines.append(c["source"])
        lines.append("```")
        lines.append("")
        lines.append("**Gist X:**")
        lines.append(f"> {c['gist_x']}")
        lines.append("")
        lines.append("**Gist Y:**")
        lines.append(f"> {c['gist_y']}")
        lines.append("")
        lines.append("| Dimension | Your judgment (X / Y / tie) |")
        lines.append("|---|---|")
        lines.append("| overall | |")
        lines.append("| coverage | |")
        lines.append("| faithfulness | |")
        lines.append("| conciseness | |")
        lines.append("")
        lines.append("---")
        lines.append("")

    OUTPUT_MD.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nCalibration sheet: {OUTPUT_MD}", file=sys.stderr)

    # Write answer key (DO NOT show to human judge)
    OUTPUT_KEY.write_text(
        json.dumps(answer_key, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Answer key: {OUTPUT_KEY}", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())

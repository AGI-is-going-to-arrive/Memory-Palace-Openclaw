#!/usr/bin/env python3
"""Run LLM pairwise judge on the 15 calibration cases for comparison with human."""
from __future__ import annotations

import json
import random
import re
import sys
from pathlib import Path

BENCHMARK_DIR = Path(__file__).resolve().parent

sys.path.insert(0, str(BENCHMARK_DIR))
sys.path.insert(0, str(BENCHMARK_DIR.parents[2]))

from helpers.pairwise_judge import DIMENSIONS, judge_pair

SHEET = BENCHMARK_DIR / "human_calibration_sheet.md"
KEY = BENCHMARK_DIR / "human_calibration_answer_key.json"
OUTPUT = BENCHMARK_DIR / "human_calibration_llm_judgments.json"


def parse_human_judgments(md_text: str):
    """Extract human judgments from the filled calibration sheet."""
    cases = []
    blocks = re.split(r"## Case (\d+)", md_text)
    for i in range(1, len(blocks), 2):
        case_num = int(blocks[i])
        body = blocks[i + 1]

        # Extract gists
        gist_x_m = re.search(r"\*\*Gist X:\*\*\s*\n>\s*(.*?)(?:\n\n|\n\*\*)", body, re.DOTALL)
        gist_y_m = re.search(r"\*\*Gist Y:\*\*\s*\n>\s*(.*?)(?:\n\n|\n\|)", body, re.DOTALL)
        source_m = re.search(r"```\n(.*?)\n```", body, re.DOTALL)

        gist_x = gist_x_m.group(1).strip() if gist_x_m else ""
        gist_y = gist_y_m.group(1).strip() if gist_y_m else ""
        source = source_m.group(1).strip() if source_m else ""

        # Extract human judgments from table
        judgments = {}
        for dim in DIMENSIONS:
            pat = rf"\|\s*{dim}\s*\|\s*(\w+)\s*\|"
            m = re.search(pat, body, re.IGNORECASE)
            if m:
                judgments[dim] = m.group(1).strip().upper()

        cases.append({
            "case_num": case_num,
            "source": source,
            "gist_x": gist_x,
            "gist_y": gist_y,
            "human": judgments,
            "identical": gist_x == gist_y,
        })
    return cases


def main():
    md_text = SHEET.read_text(encoding="utf-8")
    answer_key = json.loads(KEY.read_text(encoding="utf-8"))
    key_map = {k["case_num"]: k for k in answer_key}

    cases = parse_human_judgments(md_text)
    rng = random.Random(42)

    results = []
    for c in cases:
        cn = c["case_num"]
        ak = key_map[cn]
        print(f"[{cn}/15] {ak['id']} ({ak['lang']}/{ak['length_bucket']}/{ak['format_bucket']})"
              f" identical={c['identical']}", file=sys.stderr, flush=True)

        if c["identical"]:
            llm_judgment = {d: "TIE" for d in DIMENSIONS}
            llm_raw = None
        else:
            # Run LLM judge: A=gist_x, B=gist_y (same order as human sees)
            llm_raw = judge_pair(c["source"], c["gist_x"], c["gist_y"], rng=rng)
            if llm_raw:
                # Map A_WIN->X, B_WIN->Y
                llm_judgment = {}
                for d in DIMENSIONS:
                    v = llm_raw[d]
                    if v == "A_WIN":
                        llm_judgment[d] = "X"
                    elif v == "B_WIN":
                        llm_judgment[d] = "Y"
                    else:
                        llm_judgment[d] = "TIE"
            else:
                llm_judgment = {d: "SKIP" for d in DIMENSIONS}

        # Map both to method preference
        human_pref = {}
        llm_pref = {}
        for d in DIMENSIONS:
            hv = c["human"].get(d, "TIE")
            lv = llm_judgment.get(d, "TIE")

            # Map X/Y to method using answer key
            for src, pref_map in [(hv, human_pref), (lv, llm_pref)]:
                if src == "X":
                    pref_map[d] = ak["x_method"]
                elif src == "Y":
                    pref_map[d] = ak["y_method"]
                else:
                    pref_map[d] = "tie"

        results.append({
            "case_num": cn,
            "id": ak["id"],
            "length_bucket": ak["length_bucket"],
            "format_bucket": ak["format_bucket"],
            "lang": ak["lang"],
            "identical": c["identical"],
            "human_xy": c["human"],
            "llm_xy": llm_judgment,
            "human_method": human_pref,
            "llm_method": llm_pref,
        })

    OUTPUT.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\nResults: {OUTPUT}", file=sys.stderr)


if __name__ == "__main__":
    main()

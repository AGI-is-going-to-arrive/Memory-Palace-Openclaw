"""Blinded pairwise A/B judge for gist evaluation.

Given source_content and two candidate gists (A and B), the judge:
  1. Receives them in randomized order (blinded)
  2. Scores each on: overall, coverage, faithfulness, conciseness
  3. Returns: A_win / tie / B_win per dimension

The judge does NOT know which candidate is LLM vs extractive.
"""
from __future__ import annotations

import json
import random
import sys
import urllib.request
import os
from typing import Any, Dict, List, Optional

PAIRWISE_PROMPT = """\
You are comparing two short summaries (gists) of the same source text.
You do NOT know which method produced which summary.

Source text:
---
{source}
---

Summary X:
---
{gist_x}
---

Summary Y:
---
{gist_y}
---

For each dimension, decide which summary is better, or if they are tied.
Dimensions:
- overall: Which summary is better overall?
- coverage: Which summary captures more key information from the source?
- faithfulness: Which summary is more factually consistent with the source (no fabrications)?
- conciseness: Which summary is more appropriately brief without losing important content?

Reply with ONLY a JSON object, no other text:
{{"overall": "X" or "Y" or "tie", "coverage": "X" or "Y" or "tie", "faithfulness": "X" or "Y" or "tie", "conciseness": "X" or "Y" or "tie"}}"""

DIMENSIONS = ("overall", "coverage", "faithfulness", "conciseness")


def _call_llm(prompt: str, timeout_sec: float = 30.0) -> Optional[str]:
    api_base = os.environ.get("WRITE_GUARD_LLM_API_BASE", "").rstrip("/")
    api_key = os.environ.get("WRITE_GUARD_LLM_API_KEY", "")
    model = os.environ.get("WRITE_GUARD_LLM_MODEL", "")

    if not api_base or not model:
        return None

    if not api_base.startswith("http"):
        api_base = f"http://{api_base}"

    url = f"{api_base}/chat/completions"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 120,
        "temperature": 0.0,
    }

    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
        result = json.loads(resp.read().decode("utf-8"))

    choices = result.get("choices", [])
    if not choices:
        return None
    return choices[0].get("message", {}).get("content", "")


def _parse_pairwise(raw: str) -> Optional[Dict[str, str]]:
    """Parse judge output into {dim: "X"|"Y"|"tie"}."""
    raw = raw.strip()
    if raw.startswith("```"):
        lines = [l for l in raw.split("\n") if not l.strip().startswith("```")]
        raw = "\n".join(lines).strip()
    try:
        obj = json.loads(raw)
        result = {}
        for dim in DIMENSIONS:
            val = str(obj.get(dim, "tie")).strip().upper()
            if val in ("X", "Y", "TIE"):
                result[dim] = val
            else:
                result[dim] = "TIE"
        return result
    except (json.JSONDecodeError, KeyError, ValueError):
        return None


def judge_pair(
    source_content: str,
    gist_a: str,
    gist_b: str,
    rng: Optional[random.Random] = None,
) -> Optional[Dict[str, str]]:
    """Compare gist_a vs gist_b with blinded randomization.

    Returns dict with keys from DIMENSIONS, values: "A_WIN", "B_WIN", "TIE".
    gist_a and gist_b are randomly assigned to X/Y positions.
    """
    if rng is None:
        rng = random.Random()

    # Randomize: coin flip decides if A->X, B->Y or A->Y, B->X
    a_is_x = rng.random() < 0.5
    if a_is_x:
        gist_x, gist_y = gist_a, gist_b
    else:
        gist_x, gist_y = gist_b, gist_a

    prompt = PAIRWISE_PROMPT.format(
        source=source_content[:2000],
        gist_x=gist_x[:500],
        gist_y=gist_y[:500],
    )

    try:
        raw = _call_llm(prompt)
        if raw is None:
            return None
        parsed = _parse_pairwise(raw)
        if parsed is None:
            return None
    except Exception as exc:
        print(f"[pairwise_judge] LLM call failed: {exc}", file=sys.stderr, flush=True)
        return None

    # Map X/Y back to A/B
    result = {}
    for dim in DIMENSIONS:
        verdict = parsed[dim]
        if verdict == "TIE":
            result[dim] = "TIE"
        elif verdict == "X":
            result[dim] = "A_WIN" if a_is_x else "B_WIN"
        else:  # Y
            result[dim] = "B_WIN" if a_is_x else "A_WIN"
    return result


def aggregate_pairwise(
    results: List[Optional[Dict[str, str]]],
) -> Dict[str, Any]:
    """Aggregate pairwise results into win rates."""
    valid = [r for r in results if r is not None]
    if not valid:
        return {"judged_count": 0, "skipped_count": len(results)}

    agg: Dict[str, Any] = {
        "judged_count": len(valid),
        "skipped_count": len(results) - len(valid),
    }
    for dim in DIMENSIONS:
        a_wins = sum(1 for r in valid if r[dim] == "A_WIN")
        b_wins = sum(1 for r in valid if r[dim] == "B_WIN")
        ties = sum(1 for r in valid if r[dim] == "TIE")
        total = len(valid)
        agg[dim] = {
            "a_win": a_wins,
            "b_win": b_wins,
            "tie": ties,
            "a_win_rate": round(a_wins / total, 3) if total else 0,
            "b_win_rate": round(b_wins / total, 3) if total else 0,
            "tie_rate": round(ties / total, 3) if total else 0,
        }
    return agg

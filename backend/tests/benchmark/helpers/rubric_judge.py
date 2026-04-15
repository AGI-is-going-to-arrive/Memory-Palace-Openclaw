"""Reference-free rubric-based LLM judge for gist evaluation.

Scores a candidate gist on 4 dimensions (1-5 scale):
  - coverage: does the gist capture key information from the source?
  - faithfulness: is the gist factually consistent with the source?
  - conciseness: is the gist appropriately brief without filler?
  - language_match: does the gist use the expected language?

IMPORTANT: This is reference-free. The judge sees only:
  source_content + candidate_gist + expected_lang
It does NOT see reference_gist.

Skips gracefully when LLM is unavailable.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from typing import Any, Dict, List, Optional


RUBRIC_PROMPT = """\
You are evaluating a short summary (gist) of a source text.
Score the gist on four dimensions, each from 1 (worst) to 5 (best).

Dimensions:
- coverage: Does the gist capture the key information from the source? (1=misses everything, 5=captures all key points)
- faithfulness: Is every claim in the gist supported by the source? (1=fabrications, 5=fully faithful)
- conciseness: Is the gist appropriately brief? (1=too verbose or too terse to be useful, 5=perfectly concise)
- language_match: Does the gist use the expected language ({expected_lang})? (1=wrong language, 5=correct language throughout)

Source text:
---
{source}
---

Candidate gist:
---
{gist}
---

Expected language: {expected_lang}

Reply with ONLY a JSON object, no other text:
{{"coverage": <int>, "faithfulness": <int>, "conciseness": <int>, "language_match": <int>}}"""

DIMENSIONS = ("coverage", "faithfulness", "conciseness", "language_match")


def _call_llm(prompt: str, timeout_sec: float = 30.0) -> Optional[str]:
    """Call LLM via OpenAI-compatible chat/completions endpoint."""
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


def _parse_scores(raw: str) -> Optional[Dict[str, int]]:
    """Extract the 4-dimension score dict from LLM output."""
    # Try direct JSON parse
    raw = raw.strip()
    # Strip markdown code fences if present
    if raw.startswith("```"):
        lines = raw.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        raw = "\n".join(lines).strip()

    try:
        obj = json.loads(raw)
        scores = {}
        for dim in DIMENSIONS:
            val = int(obj[dim])
            scores[dim] = max(1, min(5, val))
        return scores
    except (json.JSONDecodeError, KeyError, ValueError, TypeError):
        return None


def judge_single(
    source_content: str,
    candidate_gist: str,
    expected_lang: str,
) -> Optional[Dict[str, int]]:
    """Score a single gist. Returns dict of 4 scores or None if LLM unavailable."""
    prompt = RUBRIC_PROMPT.format(
        source=source_content[:2000],
        gist=candidate_gist[:500],
        expected_lang=expected_lang,
    )
    try:
        raw = _call_llm(prompt)
        if raw is None:
            return None
        return _parse_scores(raw)
    except Exception as exc:
        print(
            f"[rubric_judge] LLM call failed: {exc}",
            file=sys.stderr, flush=True,
        )
        return None


def judge_batch(
    cases: List[Dict[str, str]],
) -> List[Optional[Dict[str, int]]]:
    """Score a batch of gists. Each case needs: source_content, candidate_gist, expected_lang."""
    results = []
    for case in cases:
        score = judge_single(
            source_content=case["source_content"],
            candidate_gist=case["candidate_gist"],
            expected_lang=case.get("expected_lang", "en"),
        )
        results.append(score)
    return results


def aggregate_scores(
    scores: List[Optional[Dict[str, int]]],
) -> Dict[str, Any]:
    """Compute per-dimension means from a list of score dicts."""
    valid = [s for s in scores if s is not None]
    if not valid:
        return {"judged_count": 0, "skipped_count": len(scores)}

    result: Dict[str, Any] = {
        "judged_count": len(valid),
        "skipped_count": len(scores) - len(valid),
    }
    for dim in DIMENSIONS:
        vals = [s[dim] for s in valid]
        result[f"{dim}_mean"] = round(sum(vals) / len(vals), 2)
    result["overall_mean"] = round(
        sum(result[f"{d}_mean"] for d in DIMENSIONS) / len(DIMENSIONS), 2
    )
    return result

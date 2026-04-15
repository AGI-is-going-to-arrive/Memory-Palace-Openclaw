"""Factual coverage checker for gist evaluation.

Two-step process:
  1. Extract 3-5 key facts from source_content (LLM call)
  2. For each gist candidate, check which facts are covered / missed / fabricated

Returns per-gist: covered_count, missed_count, fabricated_count.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from typing import Any, Dict, List, Optional

EXTRACT_FACTS_PROMPT = """\
Extract ALL key facts from the following text (typically 3 to 7). Each fact should \
be a single, verifiable statement. Include facts about: progress made, ongoing work, \
next steps/plans, risks/concerns, notes/status updates, team discussions, and decisions.
Do NOT skip any substantive claim in the text.

Text:
---
{source}
---

Reply with ONLY a JSON array of strings, no other text:
["fact 1", "fact 2", ...]"""

CHECK_COVERAGE_PROMPT = """\
Given these key facts extracted from a source text, evaluate whether a summary \
covers each fact, misses it, or adds unsupported/fabricated claims.

IMPORTANT paraphrase rule:
- A fact is COVERED if the summary conveys the same meaning, even with different wording.
- Rephrasing, compression, or combining multiple facts into one sentence counts as COVERED.
- Only mark a claim as FABRICATED if it introduces information that is COMPLETELY ABSENT \
from the source text — not just rephrased differently.
- If a claim is a reasonable inference from explicitly stated facts, it is NOT fabricated.

Key facts:
{facts_json}

Source text (for reference when judging fabrication):
---
{source}
---

Summary to evaluate:
---
{gist}
---

For each fact, mark "covered" or "missed".
Then list any claims in the summary that introduce genuinely new information not \
present anywhere in the source text (fabricated).

Reply with ONLY a JSON object, no other text:
{{"covered": ["fact text", ...], "missed": ["fact text", ...], "fabricated": ["claim text", ...]}}"""


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
        "max_tokens": 500,
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


def _strip_fences(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("```"):
        lines = [l for l in raw.split("\n") if not l.strip().startswith("```")]
        raw = "\n".join(lines).strip()
    return raw


def extract_facts(source_content: str) -> Optional[List[str]]:
    """Extract 3-5 key facts from source. Returns list of fact strings or None."""
    prompt = EXTRACT_FACTS_PROMPT.format(source=source_content[:2000])
    try:
        raw = _call_llm(prompt)
        if raw is None:
            return None
        raw = _strip_fences(raw)
        facts = json.loads(raw)
        if isinstance(facts, list) and all(isinstance(f, str) for f in facts):
            return facts[:5]
        return None
    except Exception as exc:
        print(f"[factual_coverage] extract_facts failed: {exc}", file=sys.stderr, flush=True)
        return None


def check_coverage(
    facts: List[str],
    gist: str,
    source: str = "",
) -> Optional[Dict[str, List[str]]]:
    """Check which facts are covered/missed/fabricated in a gist."""
    facts_json = json.dumps(facts, ensure_ascii=False)
    prompt = CHECK_COVERAGE_PROMPT.format(
        facts_json=facts_json, gist=gist[:500], source=source[:2000],
    )
    try:
        raw = _call_llm(prompt)
        if raw is None:
            return None
        raw = _strip_fences(raw)
        obj = json.loads(raw)
        return {
            "covered": obj.get("covered", []),
            "missed": obj.get("missed", []),
            "fabricated": obj.get("fabricated", []),
        }
    except Exception as exc:
        print(f"[factual_coverage] check_coverage failed: {exc}", file=sys.stderr, flush=True)
        return None


def evaluate_pair(
    source_content: str,
    gist_a: str,
    gist_b: str,
) -> Optional[Dict[str, Any]]:
    """Full pipeline: extract facts, then check coverage for both gists."""
    facts = extract_facts(source_content)
    if facts is None or len(facts) == 0:
        return None

    cov_a = check_coverage(facts, gist_a, source=source_content)
    cov_b = check_coverage(facts, gist_b, source=source_content)

    if cov_a is None or cov_b is None:
        return None

    return {
        "facts_count": len(facts),
        "a_covered": len(cov_a["covered"]),
        "a_missed": len(cov_a["missed"]),
        "a_fabricated": len(cov_a["fabricated"]),
        "b_covered": len(cov_b["covered"]),
        "b_missed": len(cov_b["missed"]),
        "b_fabricated": len(cov_b["fabricated"]),
    }


def aggregate_coverage(
    results: List[Optional[Dict[str, Any]]],
) -> Dict[str, Any]:
    """Aggregate factual coverage results."""
    valid = [r for r in results if r is not None]
    if not valid:
        return {"evaluated_count": 0, "skipped_count": len(results)}

    total_facts = sum(r["facts_count"] for r in valid)
    a_covered = sum(r["a_covered"] for r in valid)
    a_missed = sum(r["a_missed"] for r in valid)
    a_fabricated = sum(r["a_fabricated"] for r in valid)
    b_covered = sum(r["b_covered"] for r in valid)
    b_missed = sum(r["b_missed"] for r in valid)
    b_fabricated = sum(r["b_fabricated"] for r in valid)

    return {
        "evaluated_count": len(valid),
        "skipped_count": len(results) - len(valid),
        "total_facts": total_facts,
        "a_coverage_rate": round(a_covered / total_facts, 3) if total_facts else 0,
        "a_missed_rate": round(a_missed / total_facts, 3) if total_facts else 0,
        "a_fabricated_total": a_fabricated,
        "a_fabricated_rate": round(a_fabricated / len(valid), 3),
        "b_coverage_rate": round(b_covered / total_facts, 3) if total_facts else 0,
        "b_missed_rate": round(b_missed / total_facts, 3) if total_facts else 0,
        "b_fabricated_total": b_fabricated,
        "b_fabricated_rate": round(b_fabricated / len(valid), 3),
    }

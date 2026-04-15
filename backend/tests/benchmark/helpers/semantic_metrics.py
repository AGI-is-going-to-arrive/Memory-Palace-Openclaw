"""Semantic similarity metrics for gist evaluation.

Default provider: BERTScore (bert-score package).
Gracefully skips when the package or model is unavailable.
"""
from __future__ import annotations

import sys
from typing import Dict, List, Optional

_BERTSCORE_AVAILABLE: Optional[bool] = None


def _check_bertscore() -> bool:
    global _BERTSCORE_AVAILABLE
    if _BERTSCORE_AVAILABLE is None:
        try:
            import bert_score  # noqa: F401
            _BERTSCORE_AVAILABLE = True
        except ImportError:
            _BERTSCORE_AVAILABLE = False
            print(
                "[semantic_metrics] bert-score not installed; "
                "pip install -r requirements-benchmark.txt",
                file=sys.stderr, flush=True,
            )
    return _BERTSCORE_AVAILABLE


def compute_bertscore_batch(
    references: List[str],
    candidates: List[str],
    lang: str = "en",
) -> Optional[List[float]]:
    """Compute BERTScore F1 for each (reference, candidate) pair.

    Returns list of float F1 scores, or None if BERTScore is unavailable.
    Uses multilingual model for zh, English model for en.
    """
    if not _check_bertscore():
        return None
    if not references or not candidates:
        return None

    try:
        from bert_score import score as bert_score_fn

        model_type = (
            "bert-base-chinese" if lang == "zh"
            else "roberta-large"
        )

        _P, _R, F1 = bert_score_fn(
            candidates, references,
            model_type=model_type,
            verbose=False,
            batch_size=16,
        )
        return [round(f.item(), 4) for f in F1]
    except Exception as exc:
        print(
            f"[semantic_metrics] BERTScore failed: {exc}",
            file=sys.stderr, flush=True,
        )
        return None


def compute_bertscore_single(
    reference: str,
    candidate: str,
    lang: str = "en",
) -> Optional[float]:
    """Convenience wrapper for a single pair."""
    result = compute_bertscore_batch([reference], [candidate], lang=lang)
    if result and len(result) > 0:
        return result[0]
    return None

#!/usr/bin/env python3
"""Download and prepare public sub-skill benchmark datasets.

This script must be run EXPLICITLY before test_public_sub_skill_benchmarks.py.
It downloads subsets from HuggingFace and saves lightweight JSONL fixtures
to backend/tests/datasets/sub_skill/.

Datasets prepared:
  - massive_intent: MASSIVE (Amazon) zh-CN + en-US, mapped to 4 MP intents
  - xnli_contradiction: XNLI zh + en contradiction subset
  - pawsx_paraphrase: PAWS-X zh + en paraphrase pairs
  - lcsts_gist: LCSTS zh short text summaries
  - wikilingua_gist: WikiLingua zh + en how-to summaries

Usage:
    python backend/tests/benchmark/helpers/prepare_public_sub_skill_datasets.py [--all]
    python backend/tests/benchmark/helpers/prepare_public_sub_skill_datasets.py --dataset massive_intent
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

try:
    from datasets import load_dataset
except ImportError:
    print("ERROR: 'datasets' package not installed. Run: pip install datasets")
    sys.exit(1)

PROJECT_ROOT = Path(__file__).resolve().parents[4]
FIXTURES_DIR = PROJECT_ROOT / "backend" / "tests" / "fixtures"
OUTPUT_DIR = PROJECT_ROOT / "backend" / "tests" / "datasets" / "sub_skill"
MAPPING_PATH = FIXTURES_DIR / "massive_intent_mapping.json"

SEED = 2026_04_04
random.seed(SEED)

ALL_DATASETS = ["massive_intent", "xnli_contradiction", "pawsx_paraphrase",
                "lcsts_gist", "wikilingua_gist"]


def _write_jsonl(path: Path, rows: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"  {path.name}: {len(rows)} cases")


# ---------------------------------------------------------------------------
# MASSIVE intent
# ---------------------------------------------------------------------------

def prepare_massive_intent(n_per_lang: int = 200) -> None:
    """Download MASSIVE zh-CN + en-US, map to MP intents, sample."""
    print("\n[massive_intent] Loading from HuggingFace...")
    mapping = json.loads(MAPPING_PATH.read_text(encoding="utf-8"))["mapping"]

    # MASSIVE uses a single 'default' config with a 'locale' column
    ds = load_dataset("AmazonScience/massive", "default", split="test")
    # Resolve numeric intent IDs to label names via ClassLabel feature
    intent_feature = ds.features["intent"]
    intent_names = intent_feature.names if hasattr(intent_feature, "names") else None

    rows = []
    excluded_count = 0

    for lang_code in ("en-US", "zh-CN"):
        lang_tag = "en" if "en" in lang_code else "zh"
        subset = [item for item in ds if item.get("locale") == lang_code]

        by_mp: dict[str, list] = {"factual": [], "exploratory": [], "temporal": [], "causal": []}
        for item in subset:
            raw_intent = item.get("intent", "")
            if isinstance(raw_intent, int) and intent_names:
                massive_intent = intent_names[raw_intent]
            else:
                massive_intent = str(raw_intent)

            mp_intent = mapping.get(massive_intent, "exclude")
            if mp_intent == "exclude":
                excluded_count += 1
                continue
            by_mp.setdefault(mp_intent, []).append({
                "text": item["utt"],
                "mp_intent": mp_intent,
                "massive_intent": massive_intent,
                "lang": lang_tag,
            })

        per_intent = n_per_lang // 4
        for intent, pool in by_mp.items():
            random.shuffle(pool)
            rows.extend(pool[:per_intent])

    random.shuffle(rows)
    _write_jsonl(OUTPUT_DIR / "massive_intent.jsonl", rows)
    print(f"  Excluded MASSIVE intents: {excluded_count} occurrences")


# ---------------------------------------------------------------------------
# XNLI contradiction
# ---------------------------------------------------------------------------

def prepare_xnli_contradiction(n_per_lang: int = 100) -> None:
    """Download XNLI zh + en, extract contradiction pairs."""
    print("\n[xnli_contradiction] Loading from HuggingFace...")
    rows = []

    for lang_code in ("en", "zh"):
        ds = load_dataset("facebook/xnli", lang_code, split="test",
)
        contradictions = [item for item in ds if item.get("label") == 2]  # 2 = contradiction
        not_contradictions = [item for item in ds if item.get("label") != 2]

        random.shuffle(contradictions)
        random.shuffle(not_contradictions)

        # Half contradiction, half not
        half = n_per_lang // 2
        for item in contradictions[:half]:
            rows.append({
                "premise": item["premise"],
                "hypothesis": item["hypothesis"],
                "label": "contradiction",
                "lang": lang_code,
            })
        for item in not_contradictions[:half]:
            rows.append({
                "premise": item["premise"],
                "hypothesis": item["hypothesis"],
                "label": "not_contradiction",
                "lang": lang_code,
            })

    random.shuffle(rows)
    _write_jsonl(OUTPUT_DIR / "xnli_contradiction.jsonl", rows)


# ---------------------------------------------------------------------------
# PAWS-X paraphrase
# ---------------------------------------------------------------------------

def prepare_pawsx_paraphrase(n_per_lang: int = 100) -> None:
    """Download PAWS-X zh + en, extract paraphrase pairs."""
    print("\n[pawsx_paraphrase] Loading from HuggingFace...")
    rows = []

    for lang_code in ("en", "zh"):
        ds = load_dataset("google-research-datasets/paws-x", lang_code, split="test",
)
        paraphrases = [item for item in ds if item.get("label") == 1]
        non_paraphrases = [item for item in ds if item.get("label") == 0]

        random.shuffle(paraphrases)
        random.shuffle(non_paraphrases)

        half = n_per_lang // 2
        for item in paraphrases[:half]:
            rows.append({
                "sentence1": item["sentence1"],
                "sentence2": item["sentence2"],
                "label": 1,
                "lang": lang_code,
            })
        for item in non_paraphrases[:half]:
            rows.append({
                "sentence1": item["sentence1"],
                "sentence2": item["sentence2"],
                "label": 0,
                "lang": lang_code,
            })

    random.shuffle(rows)
    _write_jsonl(OUTPUT_DIR / "pawsx_paraphrase.jsonl", rows)


# ---------------------------------------------------------------------------
# LCSTS gist (Chinese)
# ---------------------------------------------------------------------------

def prepare_lcsts_gist(n: int = 100) -> None:
    """Download LCSTS zh short text summaries."""
    print("\n[lcsts_gist] Loading from HuggingFace...")
    # LCSTS test split has empty summaries; use train split and filter
    ds = load_dataset("hugcyp/LCSTS", split="train")

    pool = []
    for item in ds:
        source = (item.get("text") or "").strip()
        summary = (item.get("summary") or "").strip()
        if source and summary and len(source) > 20:
            pool.append({"source": source, "summary": summary, "lang": "zh"})
        if len(pool) >= n * 5:  # Stop early, we only need n
            break

    random.shuffle(pool)
    _write_jsonl(OUTPUT_DIR / "lcsts_gist.jsonl", pool[:n])


# ---------------------------------------------------------------------------
# WikiLingua gist (zh + en)
# ---------------------------------------------------------------------------

def prepare_wikilingua_gist(n: int = 100) -> None:
    """Download WikiLingua en how-to summaries.

    WikiLingua (esdurmus/wiki_lingua) uses a single default config with
    article.document (list of section texts) and article.summary (list of
    section summaries).  We flatten the first section of each article.
    """
    print("\n[wikilingua_gist] Loading from HuggingFace...")
    ds = load_dataset("esdurmus/wiki_lingua", split="train")

    pool = []
    for item in ds:
        article = item.get("article")
        if not isinstance(article, dict):
            continue
        texts = article.get("document", [])
        summaries = article.get("summary", [])
        if not texts or not summaries:
            continue
        source = " ".join(texts[0]) if isinstance(texts[0], list) else str(texts[0])
        summary = " ".join(summaries[0]) if isinstance(summaries[0], list) else str(summaries[0])

        if source.strip() and summary.strip() and len(source) > 30:
            pool.append({
                "source": source.strip()[:1000],
                "summary": summary.strip()[:300],
                "lang": "en",
            })
        if len(pool) >= n * 5:
            break

    random.shuffle(pool)
    _write_jsonl(OUTPUT_DIR / "wikilingua_gist.jsonl", pool[:n])


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

PREPARERS = {
    "massive_intent": prepare_massive_intent,
    "xnli_contradiction": prepare_xnli_contradiction,
    "pawsx_paraphrase": prepare_pawsx_paraphrase,
    "lcsts_gist": prepare_lcsts_gist,
    "wikilingua_gist": prepare_wikilingua_gist,
}


def main():
    parser = argparse.ArgumentParser(description="Prepare public sub-skill benchmark datasets")
    parser.add_argument("--dataset", choices=ALL_DATASETS, help="Prepare a specific dataset")
    parser.add_argument("--all", action="store_true", help="Prepare all datasets")
    args = parser.parse_args()

    if not args.dataset and not args.all:
        args.all = True

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    targets = ALL_DATASETS if args.all else [args.dataset]
    for name in targets:
        try:
            PREPARERS[name]()
        except Exception as exc:
            print(f"  ERROR preparing {name}: {exc}")

    print(f"\nDone. Datasets saved to: {OUTPUT_DIR}")


if __name__ == "__main__":
    main()

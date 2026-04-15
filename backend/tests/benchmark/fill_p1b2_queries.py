#!/usr/bin/env python3
"""Phase B2: Fill query placeholder text via LLM.

Reads target corpus entries to instruct LLM to avoid copying key phrases.

Usage:
    python fill_p1b2_queries.py fill    # Fill 42 placeholder queries
    python fill_p1b2_queries.py audit   # Run overlap audit on filled queries

LLM config via environment variables: LLM_ENDPOINT, LLM_API_KEY, LLM_MODEL
"""

import json
import math
import os
import re
import sys
import time
import urllib.request
from collections import Counter
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
FIXTURES_DIR = SCRIPT_DIR.parent / "fixtures"
CORPUS_FILE = FIXTURES_DIR / "memory_native_corpus.jsonl"
QUERIES_FILE = FIXTURES_DIR / "memory_native_queries.jsonl"

LLM_ENDPOINT = os.environ.get("LLM_ENDPOINT", "")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "")

INTENT_DESCRIPTIONS = {
    "factual": "直接事实查询，用户想找一个具体的记忆条目",
    "temporal": "时间相关查询，用户关注最新/最近/时间顺序",
    "causal": "因果追溯查询，用户想知道为什么做了某个决策",
    "exploratory": "探索性查询，用户想浏览某个主题的所有相关内容",
}

TAXONOMY_HINTS = {
    "F1": "直接询问一个具体事实",
    "F2": "询问个人偏好或风格规则",
    "TR1": "询问最近/最新更新的内容",
    "TR2": "询问某个主题的变化历程",
    "TF1": "带时间过滤条件的查询",
    "C1": "追问某个决策的原因",
    "C2": "询问两个事物之间的依赖关系",
    "E1": "浏览某个主题的所有笔记",
    "E2": "跨领域搜索相关信息",
    "S1": "在特定 domain 内搜索",
    "S2": "在特定路径前缀下搜索",
    "N1": "通过祖先路径召回子路径记忆",
    "N2": "通过别名召回记忆",
    "V1": "询问某个主题的最新版本",
    "V2": "查询应表面化矛盾的记忆",
    "V3": "查询应召回近重复的记忆",
    "M1": "session上下文应增强检索的查询",
    "M2": "长期记忆应压过session噪声的查询",
    "TX": "测试不同文本风格的检索鲁棒性",
}

LANG_INSTRUCTIONS = {
    "zh": "用中文写这条查询。",
    "en": "Write this query in English.",
    "mixed": "用中文写这条查询（可自然夹杂英文术语）。",
}


def _load_jsonl(path: Path) -> list:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _write_jsonl(path: Path, entries: list) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


def _call_llm(system_prompt: str, user_prompt: str, retries: int = 2) -> str:
    payload = json.dumps({
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.7,
        "max_tokens": 100,
    }).encode()
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(
                LLM_ENDPOINT, data=payload,
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {LLM_API_KEY}"},
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read())
            text = result["choices"][0]["message"]["content"].strip()
            # Strip quotes, labels, prefixes
            text = re.sub(r'^["\']|["\']$', '', text)
            text = re.sub(r'^(?:查询|Query|问题|Question)[：:]\s*', '', text)
            text = text.strip('"\'')
            # Take first line only (avoid multi-line)
            text = text.split("\n")[0].strip()
            return text
        except Exception as e:
            if attempt < retries:
                time.sleep(1)
            else:
                raise RuntimeError(f"LLM call failed: {e}")
    return ""


def _extract_hint(placeholder: str) -> str:
    m = re.match(r'\[QUERY_PLACEHOLDER[^\]]*\]\s*(.*)', placeholder)
    return m.group(1).strip() if m else placeholder


def _is_placeholder(query_text: str) -> bool:
    return query_text.startswith("[QUERY_PLACEHOLDER")


def _extract_keywords(text: str, min_len: int = 2) -> set:
    """Extract meaningful tokens from text for overlap checking."""
    # CJK + latin tokens
    tokens = set()
    # Chinese: bigrams
    cjk = re.findall(r'[\u4e00-\u9fff]+', text)
    for seg in cjk:
        for i in range(len(seg) - 1):
            tokens.add(seg[i:i+2])
    # English: words
    words = re.findall(r'[a-zA-Z]{2,}', text.lower())
    stopwords = {'the','a','an','is','are','was','were','be','been','being',
                 'have','has','had','do','does','did','will','would','could',
                 'should','may','might','can','shall','and','or','but','in',
                 'on','at','to','for','of','with','by','from','as','into',
                 'about','what','how','why','when','where','which','who',
                 'that','this','these','those','my','your','our','their'}
    tokens.update(w for w in words if w not in stopwords and len(w) >= min_len)
    return tokens


def compute_overlap(query_text: str, target_contents: list) -> float:
    """Compute max keyword overlap ratio between query and target contents."""
    q_tokens = _extract_keywords(query_text)
    if not q_tokens:
        return 0.0
    max_ratio = 0.0
    for content in target_contents:
        c_tokens = _extract_keywords(content)
        if not c_tokens:
            continue
        overlap = len(q_tokens & c_tokens)
        ratio = overlap / len(q_tokens) if q_tokens else 0.0
        max_ratio = max(max_ratio, ratio)
    return max_ratio


def fill_queries() -> int:
    corpus = _load_jsonl(CORPUS_FILE)
    corpus_map = {c["fixture_id"]: c for c in corpus}
    queries = _load_jsonl(QUERIES_FILE)
    filled = 0

    system_prompt = (
        "你是一个记忆检索系统的测试数据生成器。"
        "你的任务是生成自然的用户查询语句，用于测试记忆检索质量。"
        "关键规则：不要直接复制目标记忆中的关键短语，用你自己的话重新表述。"
    )

    for q in queries:
        if not _is_placeholder(q["query"]):
            continue

        hint = _extract_hint(q["query"])
        tax = q["taxonomy_code"]
        intent = q["intent"]
        lang = q["lang"]

        # Get target memory content (for anti-overlap instruction)
        target_contents = []
        key_phrases = []
        for eid in q["expected_memory_ids"]:
            if eid in corpus_map:
                content = corpus_map[eid]["content"]
                target_contents.append(content)
                # Extract a few key phrases to explicitly avoid
                phrases = re.findall(r'[\u4e00-\u9fff]{3,8}|[a-zA-Z]{4,}', content)
                key_phrases.extend(phrases[:5])

        avoid_str = "、".join(key_phrases[:8]) if key_phrases else "无"

        user_prompt = (
            f"请生成一条用户查询语句。\n\n"
            f"查询类型：{TAXONOMY_HINTS.get(tax, tax)}\n"
            f"意图：{INTENT_DESCRIPTIONS.get(intent, intent)}\n"
            f"语言：{LANG_INSTRUCTIONS.get(lang, '')}\n"
            f"提示：{hint}\n"
            f"禁止直接使用的关键词/短语：{avoid_str}\n\n"
            f"要求：\n"
            f"- 生成一句自然的用户查询（10-40字符）\n"
            f"- 不要复制目标记忆的原文短语\n"
            f"- 用自己的话重新表述\n"
            f"- 只输出查询语句本身，不加任何前缀或引号"
        )

        query_text = _call_llm(system_prompt, user_prompt)
        q["query"] = query_text
        filled += 1

        overlap = compute_overlap(query_text, target_contents)
        flag = " ⚠HIGH" if overlap > 0.5 else ""
        print(f"  [{filled:2d}] {q['case_id']} ({tax}/{lang}): "
              f"{query_text[:50]}{'...' if len(query_text)>50 else ''}"
              f"  [overlap={overlap:.2f}{flag}]")

    _write_jsonl(QUERIES_FILE, queries)
    return filled


def audit() -> None:
    corpus = _load_jsonl(CORPUS_FILE)
    corpus_map = {c["fixture_id"]: c for c in corpus}
    queries = _load_jsonl(QUERIES_FILE)

    print("=" * 60)
    print("Phase B2 Query Audit")
    print("=" * 60)

    # 1. Placeholder count
    ph_count = sum(1 for q in queries if _is_placeholder(q["query"]))
    print(f"\n[1] Remaining placeholders: {ph_count}/{len(queries)}")

    # 2. Overlap analysis
    print("\n[2] Keyword overlap analysis")
    overlaps = []
    high_overlap = []
    for q in queries:
        targets = [corpus_map[eid]["content"] for eid in q["expected_memory_ids"]
                   if eid in corpus_map]
        ov = compute_overlap(q["query"], targets)
        overlaps.append((q["case_id"], q["taxonomy_code"], ov))
        if ov > 0.5:
            high_overlap.append((q["case_id"], q["taxonomy_code"], ov, q["query"][:60]))

    avg_ov = sum(o for _, _, o in overlaps) / len(overlaps) if overlaps else 0
    max_ov = max(overlaps, key=lambda x: x[2]) if overlaps else ("", "", 0)
    print(f"  Average overlap: {avg_ov:.3f}")
    print(f"  Max overlap: {max_ov[0]} ({max_ov[1]}) = {max_ov[2]:.3f}")
    print(f"  High overlap (>0.5): {len(high_overlap)} cases")
    if high_overlap:
        for cid, tax, ov, txt in high_overlap:
            print(f"    {cid} ({tax}): overlap={ov:.2f} — {txt}")

    # 3. Taxonomy quota
    print("\n[3] Taxonomy × lang distribution")
    tax_lang = Counter()
    for q in queries:
        lang = q["lang"] if q["lang"] in ("zh", "en") else "zh"
        tax_lang[(q["taxonomy_code"], lang)] += 1

    taxes = sorted(set(t for t, _ in tax_lang))
    print(f"  {'Tax':<6} {'zh':>4} {'en':>4} {'Total':>6}")
    for tax in taxes:
        zh = tax_lang.get((tax, "zh"), 0)
        en = tax_lang.get((tax, "en"), 0)
        print(f"  {tax:<6} {zh:>4} {en:>4} {zh+en:>6}")

    # 4. Sample 5 high-risk cases: TF1, V1, N2, M2, TX
    print("\n[4] High-risk case samples (TF1/V1/N2/M2/TX)")
    sample_taxes = ["TF1", "V1", "N2", "M2", "TX"]
    for stax in sample_taxes:
        for q in queries:
            if q["taxonomy_code"] == stax and not _is_placeholder(q["query"]):
                targets = [corpus_map[eid]["content"][:80] for eid in q["expected_memory_ids"]
                           if eid in corpus_map]
                ov = compute_overlap(q["query"], [corpus_map[eid]["content"]
                     for eid in q["expected_memory_ids"] if eid in corpus_map])
                print(f"\n  {q['case_id']} ({stax}/{q['lang']}) overlap={ov:.2f}")
                print(f"    Query:  {q['query']}")
                print(f"    Target: {targets[0] if targets else 'N/A'}...")
                break

    # 5. Run validator
    print(f"\n{'='*60}")
    print("[5] Validator")
    import subprocess
    result = subprocess.run(
        [sys.executable, str(SCRIPT_DIR / "generate_p1_data.py"), "validate"],
        capture_output=True, text=True,
    )
    for line in result.stdout.strip().split("\n")[-5:]:
        print(f"  {line}")


def main():
    if len(sys.argv) < 2:
        print("Usage: fill_p1b2_queries.py {fill | audit}")
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "fill":
        if not all([LLM_ENDPOINT, LLM_API_KEY, LLM_MODEL]):
            print("ERROR: Set LLM_ENDPOINT, LLM_API_KEY, LLM_MODEL env vars.")
            sys.exit(1)
        print("=== Phase B2: Filling query text ===\n")
        n = fill_queries()
        print(f"\nFilled {n} queries. Running audit...\n")
        audit()
    elif cmd == "audit":
        audit()
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    main()

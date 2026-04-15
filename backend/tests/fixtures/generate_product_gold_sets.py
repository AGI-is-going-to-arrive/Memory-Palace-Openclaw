#!/usr/bin/env python3
"""Generate multi-scenario bilingual product gold sets for real ablation testing.

Unlike generate_gold_sets.py (coding-focused, mock-compatible), this generator
produces gold sets for REAL retrieval path testing across 7 user scenarios:

  1. coding      — programming / DevOps
  2. writing     — creative writing / blogging
  3. study       — learning notes / course material
  4. daily       — daily logs / personal records
  5. project     — project management / meeting notes
  6. gamedev     — game design / level design
  7. research    — academic research / literature review

Output files (separate from CI-fast fixtures):
  - intent_product_gold_set.jsonl        (200 cases, 100 CN + 100 EN)
  - write_guard_product_gold_set.jsonl   (200 cases, 100 CN + 100 EN)
  - gist_product_gold_set.jsonl          (100 cases, 50 CN + 50 EN)
"""
import json
import random
from pathlib import Path

FIXTURES_DIR = Path(__file__).parent
random.seed(2026_04_04)  # Reproducible

SCENARIOS = ["coding", "writing", "study", "daily", "project", "gamedev", "research"]

# =============================================================================
# Scenario-specific vocabulary
# =============================================================================

VOCAB = {
    "coding": {
        "subjects_en": ["API gateway", "CI pipeline", "database migration", "auth module",
                        "search index", "unit test suite", "deployment script", "config parser"],
        "subjects_zh": ["API 网关", "CI 流水线", "数据库迁移", "认证模块",
                        "搜索索引", "单元测试", "部署脚本", "配置解析器"],
        "actions_en": ["refactored", "deployed", "debugged", "benchmarked", "migrated", "optimized"],
        "actions_zh": ["重构", "部署", "调试", "基准测试", "迁移", "优化"],
    },
    "writing": {
        "subjects_en": ["blog post draft", "novel chapter outline", "newsletter copy",
                        "product description", "social media thread", "press release",
                        "essay on AI ethics", "technical tutorial"],
        "subjects_zh": ["博客草稿", "小说章节大纲", "电子报文案",
                        "产品描述", "社交媒体帖子", "新闻稿",
                        "AI 伦理论文", "技术教程"],
        "actions_en": ["drafted", "revised", "published", "proofread", "edited", "outlined"],
        "actions_zh": ["起草", "修改", "发布", "校对", "编辑", "拟定大纲"],
    },
    "study": {
        "subjects_en": ["linear algebra notes", "machine learning lecture", "statistics homework",
                        "physics lab report", "history essay", "language flashcards",
                        "algorithm practice", "reading list"],
        "subjects_zh": ["线性代数笔记", "机器学习课程", "统计作业",
                        "物理实验报告", "历史论文", "语言词卡",
                        "算法练习", "阅读清单"],
        "actions_en": ["reviewed", "summarized", "practiced", "memorized", "annotated", "studied"],
        "actions_zh": ["复习", "总结", "练习", "背诵", "批注", "学习"],
    },
    "daily": {
        "subjects_en": ["grocery list", "workout routine", "meal plan", "travel itinerary",
                        "budget tracker", "habit log", "book reading progress",
                        "home improvement tasks"],
        "subjects_zh": ["购物清单", "健身计划", "餐饮计划", "旅行行程",
                        "预算跟踪", "习惯日志", "读书进度",
                        "家装任务"],
        "actions_en": ["logged", "tracked", "planned", "updated", "completed", "scheduled"],
        "actions_zh": ["记录", "追踪", "规划", "更新", "完成", "安排"],
    },
    "project": {
        "subjects_en": ["sprint backlog", "stakeholder meeting notes", "risk register",
                        "roadmap update", "resource allocation", "milestone review",
                        "dependency chart", "status report"],
        "subjects_zh": ["冲刺待办", "利益相关者会议纪要", "风险登记表",
                        "路线图更新", "资源分配", "里程碑评审",
                        "依赖关系图", "状态报告"],
        "actions_en": ["planned", "reviewed", "assigned", "reported", "prioritized", "escalated"],
        "actions_zh": ["规划", "评审", "分配", "汇报", "排优先级", "升级处理"],
    },
    "gamedev": {
        "subjects_en": ["level design doc", "character balance sheet", "quest script",
                        "UI mockup", "sound design notes", "playtesting feedback",
                        "shader optimization", "narrative branching tree"],
        "subjects_zh": ["关卡设计文档", "角色平衡表", "任务脚本",
                        "UI 原型", "音效设计笔记", "试玩反馈",
                        "着色器优化", "剧情分支树"],
        "actions_en": ["designed", "balanced", "scripted", "prototyped", "tested", "iterated"],
        "actions_zh": ["设计", "平衡调整", "编写脚本", "制作原型", "测试", "迭代"],
    },
    "research": {
        "subjects_en": ["literature review matrix", "experiment protocol", "data analysis",
                        "conference paper draft", "citation database", "methodology notes",
                        "peer review response", "grant proposal"],
        "subjects_zh": ["文献综述矩阵", "实验方案", "数据分析",
                        "会议论文草稿", "引文数据库", "方法论笔记",
                        "同行评审回复", "基金申请书"],
        "actions_en": ["analyzed", "reviewed", "collected", "synthesized", "submitted", "revised"],
        "actions_zh": ["分析", "综述", "收集", "综合", "提交", "修改"],
    },
}

TIMEREFS_EN = ["yesterday", "last week", "two days ago", "this morning", "last month", "recently"]
TIMEREFS_ZH = ["昨天", "上周", "两天前", "今天早上", "上个月", "最近"]

# =============================================================================
# Intent Gold Set (200 cases: 50 per intent × 4 intents, 100 CN + 100 EN)
# =============================================================================

INTENT_TEMPLATES = {
    "factual": {
        "en": [
            "what is the current status of {subject}",
            "show me the {subject}",
            "how does {subject} work",
            "where is {subject} stored",
            "what are the details of {subject}",
            "list all items in {subject}",
            "how to configure {subject}",
        ],
        "zh": [
            "{subject}的当前状态是什么",
            "给我看{subject}",
            "{subject}是怎么工作的",
            "{subject}存放在哪里",
            "{subject}的详细信息",
            "列出{subject}中的所有条目",
            "如何配置{subject}",
        ],
    },
    "exploratory": {
        "en": [
            "compare {subject} with {subject2}",
            "what are the alternatives for {subject}",
            "suggest improvements for {subject}",
            "explore different approaches for {subject}",
            "pros and cons of {subject}",
            "brainstorm ideas about {subject}",
            "evaluate options for {subject}",
        ],
        "zh": [
            "对比{subject}和{subject2}",
            "{subject}有什么替代方案",
            "建议如何改进{subject}",
            "探索{subject}的不同方法",
            "{subject}的优缺点",
            "关于{subject}的头脑风暴",
            "评估{subject}的各种选择",
        ],
    },
    "temporal": {
        "en": [
            "when did we last update {subject}",
            "what changed {timeref} in {subject}",
            "show me the {subject} history",
            "latest changes to {subject}",
            "timeline of {subject} modifications",
            "{timeref} I was working on {subject}",
            "recent {subject} activity",
        ],
        "zh": [
            "我们上次更新{subject}是什么时候",
            "{timeref}{subject}有什么变化",
            "给我看{subject}的历史记录",
            "{subject}的最新变更",
            "{subject}修改的时间线",
            "{timeref}我在处理{subject}",
            "{subject}的近期活动",
        ],
    },
    "causal": {
        "en": [
            "why did {subject} fail",
            "what caused the issue with {subject}",
            "root cause of {subject} problem",
            "explain why {subject} is not working",
            "what went wrong with {subject}",
            "failure analysis for {subject}",
            "why is {subject} performing poorly",
        ],
        "zh": [
            "为什么{subject}失败了",
            "{subject}出问题的原因是什么",
            "{subject}问题的根本原因",
            "解释为什么{subject}不正常",
            "{subject}哪里出了问题",
            "{subject}的故障分析",
            "为什么{subject}表现不好",
        ],
    },
}


def _pick_subjects(scenario: str, lang: str):
    key = "subjects_zh" if lang == "zh" else "subjects_en"
    pool = VOCAB[scenario][key]
    s1, s2 = random.sample(pool, 2)
    return s1, s2


def generate_intent_product(n: int = 200):
    """Generate 200 intent cases: 50 per intent, 25 CN + 25 EN each."""
    rows = []
    intents = list(INTENT_TEMPLATES.keys())
    per_intent = n // len(intents)  # 50
    per_lang = per_intent // 2       # 25

    idx = 0
    for intent in intents:
        for lang in ("en", "zh"):
            templates = INTENT_TEMPLATES[intent][lang]
            for i in range(per_lang):
                idx += 1
                scenario = SCENARIOS[i % len(SCENARIOS)]
                s1, s2 = _pick_subjects(scenario, lang)
                t = templates[i % len(templates)]
                query = t.format(
                    subject=s1, subject2=s2,
                    timeref=random.choice(TIMEREFS_ZH if lang == "zh" else TIMEREFS_EN),
                )
                rows.append({
                    "id": f"intent-p-{idx:03d}",
                    "query": query,
                    "expected_intent": intent,
                    "scenario": scenario,
                    "lang": lang,
                })

    random.shuffle(rows)
    for i, row in enumerate(rows, 1):
        row["id"] = f"intent-p-{i:03d}"
    return rows


# =============================================================================
# Write Guard Gold Set (200 cases: ADD/UPDATE/NOOP, real retrieval format)
# =============================================================================

def _make_existing_memory(scenario: str, lang: str, idx: int,
                          subject: str, action: str) -> dict:
    """Generate a plausible existing memory with specific subject/action keywords.

    Using explicit subject/action ensures FTS keyword overlap with related
    new content, so search_advanced can actually find the target memory.
    """
    if lang == "zh":
        content = f"已{action}{subject}，取得了阶段性进展，并记录了关键决策"
    else:
        content = f"Worked on {subject}: {action} with key decisions documented"

    return {
        "uri": f"core://test/{scenario}/{idx}",
        "content": content,
        "domain": "core",
    }


def _make_distractor_memory(scenario: str, lang: str, idx: int) -> dict:
    """Generate a distractor memory: same domain, different subject."""
    key = "subjects_zh" if lang == "zh" else "subjects_en"
    subj = random.choice(VOCAB[scenario][key])
    act_key = "actions_zh" if lang == "zh" else "actions_en"
    act = random.choice(VOCAB[scenario][act_key])

    if lang == "zh":
        content = f"之前{act}了{subj}的相关内容"
    else:
        content = f"Earlier work on {subj}: {act} completed"

    return {
        "uri": f"core://test/{scenario}/{idx}",
        "content": content,
        "domain": "core",
    }


def _make_wg_case(scenario: str, lang: str, action: str, idx_base: int) -> dict:
    """Generate a write guard test case with 3-5 existing memories.

    Design constraints (informed by FTS/LIKE analysis):
    - FTS _build_safe_fts_query takes first 8 ASCII tokens, AND-joined
    - CJK excluded from FTS query (include_cjk=False), goes to LIKE fallback
    - LIKE _like_text_score checks full-query substring match → always 0 for different phrasing
    - Keyword-only (B-off) can reliably detect ADD; UPDATE/NOOP need semantic (C/D)

    Design for honest evaluation:
    - NOOP EN: content reuses existing memory's first 8 tokens → FTS matches → text_score ≈ 1.0
    - NOOP ZH: content starts with same key terms → LIKE may partially match
    - UPDATE EN: content starts with subject from existing + new info after token 8
    - UPDATE ZH: similar approach; accepted as hard for keyword-only
    - ADD: completely unrelated content → no FTS/LIKE match → ADD
    """
    key = "subjects_zh" if lang == "zh" else "subjects_en"
    act_key = "actions_zh" if lang == "zh" else "actions_en"

    subjects = VOCAB[scenario][key]
    actions = VOCAB[scenario][act_key]

    target_subj = random.choice(subjects)
    target_act = random.choice(actions)

    # 2-3 distractor memories (different subjects in same scenario)
    n_distractors = random.randint(2, 3)
    distractors = []
    for d in range(n_distractors):
        distractors.append(
            _make_distractor_memory(scenario, lang, idx_base + 100 + d)
        )

    if action == "ADD":
        # All existing memories are unrelated — pick a different scenario for target
        other_scenarios = [s for s in SCENARIOS if s != scenario]
        other_scen = random.choice(other_scenarios)
        other_key = "subjects_zh" if lang == "zh" else "subjects_en"
        other_subj = random.choice(VOCAB[other_scen][other_key])
        other_act_key = "actions_zh" if lang == "zh" else "actions_en"
        other_act = random.choice(VOCAB[other_scen][other_act_key])

        target = _make_existing_memory(other_scen, lang, idx_base, other_subj, other_act)
        existing_memories = [target] + distractors

        if lang == "zh":
            content = f"全新主题：关于{target_subj}的{target_act}方案初稿"
        else:
            content = f"Brand new topic: initial plan for {target_subj} — {target_act}"

    elif action == "UPDATE":
        # Existing: "Worked on {subject}: {action} with key decisions documented"
        # Content starts with subject tokens from existing for FTS overlap,
        # then adds genuinely new facts after token position 8.
        target = _make_existing_memory(scenario, lang, idx_base, target_subj, target_act)
        existing_memories = [target] + distractors

        new_act = random.choice([a for a in actions if a != target_act] or actions)
        new_subj = random.choice([s for s in subjects if s != target_subj] or subjects)
        if lang == "zh":
            # CJK: accepted as hard for keyword-only; real detection relies on semantic
            content = (
                f"已{target_act}{target_subj}，取得了阶段性进展，"
                f"但需要追加{new_act}{new_subj}的对接"
            )
        else:
            # EN: first 8 tokens overlap with existing:
            # existing = "Worked on {subj}: {act} with key decisions documented"
            # content = "Worked on {subj} {act} with key decisions plus {new_info}"
            content = (
                f"Worked on {target_subj} {target_act} with key decisions "
                f"plus {new_act} {new_subj} integration needed"
            )

    else:  # NOOP
        # Content preserves the existing memory's tokens for FTS matching.
        # For EN: nearly identical wording ensures first 8 tokens all match.
        # For ZH: uses same key terms to maximize LIKE overlap.
        target = _make_existing_memory(scenario, lang, idx_base, target_subj, target_act)
        existing_memories = [target] + distractors

        if lang == "zh":
            # Mirror the existing format closely
            content = f"已{target_act}{target_subj}，取得了阶段性进展，并记录了关键决策"
        else:
            # Mirror existing: "Worked on {subj}: {act} with key decisions documented"
            content = f"Worked on {target_subj}: {target_act} with key decisions documented"

    return {
        "content": content,
        "existing_memories": existing_memories,
        "expected_action": action,
        "scenario": scenario,
        "lang": lang,
    }


def generate_wg_product(n: int = 200):
    """Generate 200 write guard cases with 3-5 existing_memories each.

    Each case has 1 target memory (high relevance) + 2-3 distractors.
    Distribution: ADD 35%, UPDATE 35%, NOOP 30%.
    """
    rows = []
    counts = {"ADD": int(n * 0.35), "UPDATE": int(n * 0.35),
              "NOOP": n - int(n * 0.35) - int(n * 0.35)}

    idx = 0
    for action, count in counts.items():
        per_lang = count // 2
        for lang in ("en", "zh"):
            for i in range(per_lang):
                idx += 1
                scenario = SCENARIOS[idx % len(SCENARIOS)]
                case = _make_wg_case(scenario, lang, action, idx)
                case["id"] = f"wg-p-{idx:03d}"
                rows.append(case)

    random.shuffle(rows)
    for i, row in enumerate(rows, 1):
        row["id"] = f"wg-p-{i:03d}"
    return rows


# =============================================================================
# Gist Gold Set (100 cases: source_content + reference_gist)
# =============================================================================

LENGTH_BUCKETS = ["short", "medium", "long"]
FORMAT_BUCKETS = ["bullet", "prose", "mixed"]


def _make_source_content(scenario: str, lang: str,
                         length_bucket: str = "medium",
                         format_bucket: str = "prose") -> str:
    """Generate a realistic memory or session trace with controlled length and format.

    length_bucket:
      - short:  1-2 sentences (~30-80 chars)
      - medium: 3-5 sentences (~120-300 chars)
      - long:   6-10 sentences (~400-800 chars)

    format_bucket:
      - bullet: bullet-point list (- item\\n- item)
      - prose:  continuous paragraph sentences
      - mixed:  opening prose + bullet list + closing prose
    """
    key = "subjects_zh" if lang == "zh" else "subjects_en"
    subjects = random.sample(VOCAB[scenario][key], min(4, len(VOCAB[scenario][key])))
    act_key = "actions_zh" if lang == "zh" else "actions_en"
    acts = random.sample(VOCAB[scenario][act_key], min(4, len(VOCAB[scenario][act_key])))

    # --- Generate raw sentence pool based on length ---
    if lang == "zh":
        pool = [
            f"今天{acts[0]}了{subjects[0]}，取得了一些进展。",
            f"同时也在处理{subjects[1]}相关的问题。",
            f"下一步计划：继续{acts[1] if len(acts) > 1 else acts[0]}{subjects[2] if len(subjects) > 2 else subjects[0]}。",
            f"需要注意的风险：{subjects[1]}的进度可能受到影响。",
            f"备注：{acts[2] if len(acts) > 2 else acts[0]}工作已经完成了大部分。",
            f"与团队讨论了{subjects[0]}和{subjects[1]}之间的依赖关系。",
            f"决定优先处理{subjects[2] if len(subjects) > 2 else subjects[0]}的紧急问题。",
            f"已经完成了{acts[0]}阶段的验收测试。",
            f"后续需要{acts[1] if len(acts) > 1 else acts[0]}来确保质量。",
            f"总结：本阶段的核心交付物包括{subjects[0]}和{subjects[1]}。",
        ]
    else:
        pool = [
            f"Today I {acts[0]} {subjects[0]} and made some progress.",
            f"Also working on issues related to {subjects[1]}.",
            f"Next steps: continue to {acts[1] if len(acts) > 1 else acts[0]} {subjects[2] if len(subjects) > 2 else subjects[0]}.",
            f"Risk to watch: {subjects[1]} progress may be affected.",
            f"Note: {acts[2] if len(acts) > 2 else acts[0]} work is mostly done.",
            f"Discussed dependencies between {subjects[0]} and {subjects[1]} with the team.",
            f"Decided to prioritize the urgent issue on {subjects[2] if len(subjects) > 2 else subjects[0]}.",
            f"Completed the acceptance test for the {acts[0]} phase.",
            f"Follow-up: need to {acts[1] if len(acts) > 1 else acts[0]} to ensure quality.",
            f"Summary: core deliverables this phase include {subjects[0]} and {subjects[1]}.",
        ]

    if length_bucket == "short":
        sentences = pool[:2]
    elif length_bucket == "long":
        sentences = pool[:random.randint(7, 10)]
    else:  # medium
        sentences = pool[:random.randint(3, 5)]

    # --- Format the sentences ---
    if format_bucket == "bullet":
        return "\n".join(f"- {s.rstrip('.')}" for s in sentences)
    elif format_bucket == "mixed":
        # First sentence as prose, rest as bullets, last as prose
        if len(sentences) <= 2:
            return sentences[0] + "\n- " + sentences[-1].rstrip(".")
        opening = sentences[0]
        bullets = "\n".join(f"- {s.rstrip('.')}" for s in sentences[1:-1])
        closing = sentences[-1]
        return f"{opening}\n{bullets}\n{closing}"
    else:  # prose
        return " ".join(sentences)


def _make_reference_gist(source: str, lang: str,
                         length_bucket: str = "medium") -> str:
    """Generate a key-point summary reference gist (abstractive, not extractive).

    Adapts reference length to source length bucket:
      - short: 1 key point
      - medium: 2-3 key points
      - long: 3-4 key points (proportional, not exhaustive)
    """
    # Split on sentence boundaries (handle both bullet and prose)
    clean = source.replace("\n- ", ". ").replace("\n", ". ").replace("。", ".")
    sentences = [s.strip() for s in clean.split(".") if s.strip()]

    if lang == "zh":
        parts = []
        if len(sentences) >= 1:
            parts.append("进展：" + _rephrase_zh(sentences[0]))
        if length_bucket != "short" and len(sentences) >= 3:
            parts.append("计划：" + _rephrase_zh(sentences[2]))
        if length_bucket == "long" and len(sentences) >= 4:
            parts.append("风险：" + _rephrase_zh(sentences[3]))
        if length_bucket == "long" and len(sentences) >= 6:
            parts.append("总结：" + _rephrase_zh(sentences[min(5, len(sentences) - 1)]))
        gist = "；".join(parts) if parts else (sentences[0] if sentences else "工作推进中")
    else:
        parts = []
        if len(sentences) >= 1:
            parts.append("Progress: " + _rephrase_en(sentences[0]))
        if length_bucket != "short" and len(sentences) >= 3:
            parts.append("Next: " + _rephrase_en(sentences[2]))
        if length_bucket == "long" and len(sentences) >= 4:
            parts.append("Risk: " + _rephrase_en(sentences[3]))
        if length_bucket == "long" and len(sentences) >= 6:
            parts.append("Summary: " + _rephrase_en(sentences[min(5, len(sentences) - 1)]))
        gist = "; ".join(parts) if parts else (sentences[0] if sentences else "")

    max_len = {"short": 80, "medium": 150, "long": 250}[length_bucket]
    if len(gist) > max_len:
        gist = gist[:max_len - 3] + "..."
    return gist


def _rephrase_zh(sentence: str) -> str:
    """Lightly rephrase a Chinese sentence to avoid verbatim overlap."""
    # Remove common filler prefixes and rephrase structure
    s = sentence.strip().rstrip("。，")
    for prefix in ("今天", "同时也在处理", "下一步计划：继续", "需要注意的风险：", "备注："):
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    return s.strip() if s.strip() else sentence.strip()


def _rephrase_en(sentence: str) -> str:
    """Lightly rephrase an English sentence to avoid verbatim overlap."""
    s = sentence.strip().rstrip(".")
    for prefix in ("Today I ", "Also working on ", "Next steps: continue to ",
                    "Risk to watch: ", "Note: "):
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    return s.strip() if s.strip() else sentence.strip()


def generate_gist_product(n: int = 108):
    """Generate gist cases with distribution-aligned length/format buckets.

    Target: 3 length × 3 format × 2 lang = 18 cells, 6 cases per cell = 108.
    Each case has: source_content, reference_gist, length_bucket, format_bucket.
    """
    rows = []
    idx = 0
    per_cell = n // (len(LENGTH_BUCKETS) * len(FORMAT_BUCKETS) * 2)
    per_cell = max(per_cell, 1)

    for lang in ("en", "zh"):
        for lb in LENGTH_BUCKETS:
            for fb in FORMAT_BUCKETS:
                for i in range(per_cell):
                    idx += 1
                    scenario = SCENARIOS[idx % len(SCENARIOS)]
                    source = _make_source_content(
                        scenario, lang,
                        length_bucket=lb,
                        format_bucket=fb,
                    )
                    reference = _make_reference_gist(source, lang, length_bucket=lb)
                    rows.append({
                        "id": f"gist-p-{idx:03d}",
                        "source_content": source,
                        "reference_gist": reference,
                        "scenario": scenario,
                        "lang": lang,
                        "length_bucket": lb,
                        "format_bucket": fb,
                    })

    random.shuffle(rows)
    for i, row in enumerate(rows, 1):
        row["id"] = f"gist-p-{i:03d}"
    return rows


# =============================================================================
# Main
# =============================================================================

def main():
    intent_rows = generate_intent_product(200)
    wg_rows = generate_wg_product(200)
    gist_rows = generate_gist_product(100)

    def _write_jsonl(path, rows):
        with open(path, "w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(f"  {path.name}: {len(rows)} cases")

    print("Generating product gold sets:")
    _write_jsonl(FIXTURES_DIR / "intent_product_gold_set.jsonl", intent_rows)
    _write_jsonl(FIXTURES_DIR / "write_guard_product_gold_set.jsonl", wg_rows)
    _write_jsonl(FIXTURES_DIR / "gist_product_gold_set.jsonl", gist_rows)

    # Stats
    from collections import Counter
    print("\nIntent distribution:")
    ic = Counter(r["expected_intent"] for r in intent_rows)
    for k, v in sorted(ic.items()):
        print(f"  {k}: {v}")
    ilang = Counter(r["lang"] for r in intent_rows)
    print(f"  zh: {ilang['zh']}, en: {ilang['en']}")
    iscen = Counter(r["scenario"] for r in intent_rows)
    print("  Scenarios:", dict(sorted(iscen.items())))

    print("\nWrite Guard distribution:")
    wc = Counter(r["expected_action"] for r in wg_rows)
    for k, v in sorted(wc.items()):
        print(f"  {k}: {v}")
    wlang = Counter(r["lang"] for r in wg_rows)
    print(f"  zh: {wlang['zh']}, en: {wlang['en']}")

    print("\nGist distribution:")
    glang = Counter(r["lang"] for r in gist_rows)
    print(f"  zh: {glang.get('zh', 0)}, en: {glang.get('en', 0)}")
    glen = Counter(r["length_bucket"] for r in gist_rows)
    print(f"  length: {dict(sorted(glen.items()))}")
    gfmt = Counter(r["format_bucket"] for r in gist_rows)
    print(f"  format: {dict(sorted(gfmt.items()))}")
    gscen = Counter(r["scenario"] for r in gist_rows)
    print("  Scenarios:", dict(sorted(gscen.items())))


if __name__ == "__main__":
    main()

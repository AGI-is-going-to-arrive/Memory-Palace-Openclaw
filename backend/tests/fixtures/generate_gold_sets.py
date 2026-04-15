#!/usr/bin/env python3
"""Generate expanded gold sets for quality gate benchmarks.

Based on real OpenClaw plugin+skill usage patterns for EN/ZH coding users:
- Intent: 200 cases across 4 intents, EN+ZH, coding + memory scenarios
- Write Guard: 200 cases across ADD/UPDATE/NOOP with realistic score distributions
- Gist: 100 cases with varied rephrase distances

PersonaMem-inspired query types adapted for coding agent memory:
1. Fact recall (factual)
2. Preference tracking (factual/exploratory)
3. Timeline/history (temporal)
4. Root cause / debugging (causal)
5. Option comparison (exploratory)
6. Workflow continuity (temporal/factual)
7. Contradiction detection (causal)
"""
import json
import random
from pathlib import Path

FIXTURES_DIR = Path(__file__).parent
random.seed(42)  # Reproducible

# ============================================================================
# Intent Gold Set (200 cases)
# ============================================================================

INTENT_TEMPLATES = {
    "temporal": {
        "en": [
            "when did we {action} the {component}",
            "what happened {timeref} with the {component}",
            "show me the {component} history",
            "latest changes to {component}",
            "how long ago did we {action}",
            "timeline of {component} modifications",
            "{timeref} I was working on {component}",
            "recent {component} activity",
            "since {timeref} what changed in {component}",
            "before the {event} everything was fine",
            "after {action} the {component}, what happened",
            "show history of {component} changes",
        ],
        "zh": [
            "我们什么时候{action_zh}了{component_zh}",
            "{timeref_zh}{component_zh}有什么变化",
            "最近{component_zh}的变更记录",
            "上次{action_zh}是什么时候",
            "{component_zh}的历史改动",
            "之前{action_zh}{component_zh}后发生了什么",
            "昨天做了什么工作",
            "{timeref_zh}的{component_zh}状态",
        ],
    },
    "causal": {
        "en": [
            "why did the {component} fail",
            "what caused the {event}",
            "root cause of the {component} {event}",
            "explain why {component} is {state}",
            "reason for the {metric} on {component}",
            "because the {component} {event} we lost {outcome}",
            "debug: {component} {state} after {action}",
            "what went wrong with {component}",
            "why is {component} not working as expected",
            "failure analysis for {component}",
            "problems with {component} since {action}",
            "{component} crashed because of what",
        ],
        "zh": [
            "为什么{component_zh}{event_zh}了",
            "{component_zh}失败的原因是什么",
            "导致{event_zh}的根本原因",
            "{component_zh}出了什么问题",
            "分析{component_zh}{state_zh}的原因",
            "为什么{action_zh}之后{component_zh}不正常了",
            "{component_zh}故障排查",
            "什么导致了{event_zh}",
        ],
    },
    "exploratory": {
        "en": [
            "compare {component} with {component2}",
            "what are the tradeoffs between {option1} and {option2}",
            "suggest alternatives to {component}",
            "explore different approaches for {action}",
            "evaluate {component} vs {component2}",
            "brainstorm ideas for improving {component}",
            "review options for {action}",
            "pros and cons of {option1}",
            "what are the alternatives for {component}",
            "different strategies for {action}",
            "how could we approach {action} differently",
            "recommend a {component} configuration",
        ],
        "zh": [
            "对比{component_zh}和{component2_zh}",
            "{option1_zh}和{option2_zh}的优缺点",
            "有什么替代{component_zh}的方案",
            "探索{action_zh}的不同方法",
            "建议如何改进{component_zh}",
            "比较不同的{component_zh}策略",
            "评估{component_zh}的不同选择",
            "{component_zh}有什么可以优化的",
        ],
    },
    "factual": {
        "en": [
            "what is the current {component} configuration",
            "show me the {component} status",
            "how does {component} work",
            "list all {component} in the system",
            "current value of {setting}",
            "{component} documentation",
            "what is {component}",
            "show the {component} schema",
            "total number of {entity}",
            "default {setting} for {component}",
            "how to configure {component}",
            "where is {component} stored",
        ],
        "zh": [
            "当前{component_zh}的配置是什么",
            "{component_zh}的状态",
            "{component_zh}是怎么工作的",
            "列出所有{entity_zh}",
            "{setting_zh}的当前值",
            "{component_zh}文档",
            "数据库里有多少{entity_zh}",
            "如何配置{component_zh}",
        ],
    },
}

COMPONENTS = [
    "embedding provider", "reranker", "write guard", "search index",
    "sqlite database", "MCP server", "auto recall", "auto capture",
    "memory palace", "profile memory", "visual memory", "compact context",
    "intent classifier", "vitality decay", "WAL mode", "circuit breaker",
    "host bridge", "smart extraction", "reconcile pipeline", "onboarding flow",
]
COMPONENTS_ZH = [
    "embedding 服务", "reranker", "write guard", "搜索索引",
    "SQLite 数据库", "MCP 服务器", "自动召回", "自动捕获",
    "记忆宫殿", "profile 记忆", "视觉记忆", "压缩上下文",
    "意图分类器", "活性衰减", "WAL 模式", "断路器",
    "宿主桥", "智能提取", "协调管道", "onboarding 流程",
]
ACTIONS = ["configure", "deploy", "migrate", "update", "refactor", "benchmark", "debug", "test"]
ACTIONS_ZH = ["配置", "部署", "迁移", "更新", "重构", "测试", "调试", "优化"]
TIMEREFS = ["yesterday", "last week", "last Tuesday", "two days ago", "this morning", "recently"]
TIMEREFS_ZH = ["昨天", "上周", "上周二", "两天前", "今天早上", "最近"]
EVENTS = ["crash", "timeout", "regression", "outage", "spike", "failure", "lock contention"]
EVENTS_ZH = ["崩溃", "超时", "回归", "中断", "激增", "失败", "锁竞争"]
STATES = ["slow", "broken", "degraded", "blocking", "failing", "timing out"]
STATES_ZH = ["变慢", "坏了", "降级", "阻塞", "失败", "超时"]
METRICS = ["high latency", "low recall", "high error rate", "memory leak", "p95 spike"]
SETTINGS = ["RETRIEVAL_EMBEDDING_DIM", "SEARCH_DEFAULT_MODE", "RETRIEVAL_RERANKER_WEIGHT", "MCP_API_KEY", "chunk size"]
SETTINGS_ZH = ["embedding 维度", "搜索模式", "reranker 权重", "API 密钥", "chunk 大小"]
OPTIONS = ["Profile C", "Profile D", "hybrid search", "keyword search", "semantic search", "WAL mode", "delete mode"]
OPTIONS_ZH = ["Profile C", "Profile D", "混合搜索", "关键词搜索", "语义搜索", "WAL 模式", "delete 模式"]
ENTITIES = ["active memories", "chunks", "sessions", "agents", "embeddings"]
ENTITIES_ZH = ["活跃记忆", "分块", "会话", "agent", "embedding 缓存"]
OUTCOMES = ["data", "context", "progress", "accuracy", "performance"]

def _fill(template, lang="en"):
    c1, c2 = random.sample(COMPONENTS if lang == "en" else COMPONENTS_ZH, 2)
    o1, o2 = random.sample(OPTIONS if lang == "en" else OPTIONS_ZH, 2)
    return template.format(
        component=c1, component2=c2,
        component_zh=c1, component2_zh=c2,
        action=random.choice(ACTIONS if lang == "en" else ACTIONS_ZH),
        action_zh=random.choice(ACTIONS_ZH),
        timeref=random.choice(TIMEREFS if lang == "en" else TIMEREFS_ZH),
        timeref_zh=random.choice(TIMEREFS_ZH),
        event=random.choice(EVENTS if lang == "en" else EVENTS_ZH),
        event_zh=random.choice(EVENTS_ZH),
        state=random.choice(STATES if lang == "en" else STATES_ZH),
        state_zh=random.choice(STATES_ZH),
        metric=random.choice(METRICS),
        setting=random.choice(SETTINGS if lang == "en" else SETTINGS_ZH),
        setting_zh=random.choice(SETTINGS_ZH),
        option1=o1, option2=o2,
        option1_zh=o1, option2_zh=o2,
        entity=random.choice(ENTITIES if lang == "en" else ENTITIES_ZH),
        entity_zh=random.choice(ENTITIES_ZH),
        outcome=random.choice(OUTCOMES),
    )

def generate_intent_gold(n=200):
    rows = []
    idx = 0
    intents = list(INTENT_TEMPLATES.keys())
    per_intent = n // len(intents)

    for intent in intents:
        templates = INTENT_TEMPLATES[intent]
        en_templates = templates["en"]
        zh_templates = templates["zh"]
        # 60% EN, 40% ZH
        en_count = int(per_intent * 0.6)
        zh_count = per_intent - en_count

        for _ in range(en_count):
            idx += 1
            t = random.choice(en_templates)
            query = _fill(t, "en")
            rows.append({"id": f"intent-{idx:03d}", "query": query, "expected_intent": intent})

        for _ in range(zh_count):
            idx += 1
            t = random.choice(zh_templates)
            query = _fill(t, "zh")
            rows.append({"id": f"intent-{idx:03d}", "query": query, "expected_intent": intent})

    random.shuffle(rows)
    # Renumber
    for i, row in enumerate(rows, 1):
        row["id"] = f"intent-{i:03d}"
    return rows


# ============================================================================
# Write Guard Gold Set (200 cases)
# ============================================================================

WG_CONTENT_TEMPLATES = {
    "ADD": [
        "new note about {topic}",
        "fresh topic: {topic} setup and configuration",
        "{topic} kickoff meeting notes",
        "personal reminder: {personal}",
        "research paper summary on {topic}",
        "architecture decision record for {topic}",
        "new insight about {topic}",
        "first draft of {topic} documentation",
        "brainstorm session notes about {topic}",
        "customer feedback on {topic}",
    ],
    "UPDATE": [
        "{topic} update: {detail}",
        "revised {topic} with new findings",
        "minor correction to {topic} notes",
        "{topic}: added {detail}",
        "extended {topic} with customer impact assessment",
        "rewritten {topic} analysis with latest data",
        "{topic} v2 with additional sections",
        "follow-up on {topic}: {detail}",
        "{topic} status changed to {status}",
        "amended {topic} timeline",
    ],
    "NOOP": [
        "exact copy of existing {topic} note",
        "identical content: {topic} summary",
        "same {topic} but with different formatting only",
        "duplicate of {topic} postmortem",
        "rephrased version of {topic} with no new info",
        "slightly reformatted {topic} log",
        "carbon copy of {topic} checklist",
        "verbatim repeat of {topic} action items",
        "same {topic} content pasted again",
        "{topic} note unchanged from last version",
    ],
}

TOPICS = [
    "embedding migration", "queue overflow incident", "release checklist",
    "onboarding flow", "reranker tuning", "WAL mode rollout",
    "profile c setup", "visual memory pipeline", "circuit breaker config",
    "database schema change", "MCP protocol upgrade", "search quality",
    "latency optimization", "dependency audit", "security patch",
    "API rate limiting", "token cost analysis", "deployment pipeline",
    "test coverage improvement", "documentation refresh",
]
DETAILS = [
    "resolved after scaling", "root cause confirmed", "new metrics added",
    "timeline extended", "risk assessment updated", "owner reassigned",
    "blocked by dependency", "customer reported", "performance improved",
    "rollback needed", "fix verified in staging",
]
STATUSES = ["resolved", "in progress", "blocked", "done", "pending review"]
PERSONALS = [
    "dentist Thursday 3pm", "team lunch Friday", "buy groceries",
    "renew subscription", "call customer support", "update resume",
]

def generate_wg_gold(n=200):
    rows = []
    actions = ["ADD", "UPDATE", "NOOP"]
    # Distribution: ADD 35%, UPDATE 35%, NOOP 30%
    counts = {"ADD": int(n * 0.35), "UPDATE": int(n * 0.35), "NOOP": n - int(n * 0.35) - int(n * 0.35)}

    idx = 0
    for action in actions:
        templates = WG_CONTENT_TEMPLATES[action]
        for _ in range(counts[action]):
            idx += 1
            t = random.choice(templates)
            content = t.format(
                topic=random.choice(TOPICS),
                detail=random.choice(DETAILS),
                status=random.choice(STATUSES),
                personal=random.choice(PERSONALS),
            )

            if action == "ADD":
                sem = round(random.uniform(0.0, 0.35), 2)
                kw = round(random.uniform(0.0, 0.30), 2)
            elif action == "UPDATE":
                sem = round(random.uniform(0.55, 0.91), 2)
                kw = round(random.uniform(0.15, 0.75), 2)
            else:  # NOOP
                sem = round(random.uniform(0.92, 1.0), 2)
                kw = round(random.uniform(0.60, 1.0), 2)

            rows.append({
                "id": f"wg-{idx:03d}",
                "content": content,
                "semantic_vector_score": sem,
                "keyword_text_score": kw,
                "expected_action": action,
            })

    random.shuffle(rows)
    for i, row in enumerate(rows, 1):
        row["id"] = f"wg-{i:03d}"
    return rows


# ============================================================================
# Gist Gold Set (100 cases)
# ============================================================================

GIST_REFERENCE_TEMPLATES = [
    "rebuilt {component} after {event} and confirmed {outcome}",
    "migrated {component} configuration and validated {metric}",
    "deployed hotfix for {component} {event} bug",
    "configured {component} fallback chain with timeout policy",
    "onboarded new team member with {component} briefing",
    "analyzed {component} {event} regression across profiles",
    "resolved {component} lock contention issue",
    "reviewed pull request for {component} integration",
    "documented {component} troubleshooting steps",
    "tested {component} with large memory corpus",
    "benchmarked {component} accuracy on evaluation dataset",
    "planned sprint priorities for {component} improvements",
    "debugged {component} corruption on concurrent writes",
    "profiled {component} startup time and reduced cold start",
    "refactored {component} to support chinese queries",
    "verified ACL isolation between main and beta agents",
    "added visual memory OCR pipeline for screenshot ingestion",
    "triaged user reports of slow {component}",
    "designed provider probe auto detection for onboarding",
    "fixed race condition in {component} under heavy load",
]

GIST_REPHRASE_PATTERNS = [
    ("rebuilt", "reconstructed"), ("deployed", "shipped"), ("configured", "set up"),
    ("analyzed", "investigated"), ("resolved", "fixed"), ("reviewed", "examined"),
    ("documented", "wrote guide for"), ("tested", "ran tests on"),
    ("benchmarked", "measured"), ("planned", "defined"), ("debugged", "diagnosed"),
    ("profiled", "optimized"), ("refactored", "updated"), ("verified", "confirmed"),
    ("triaged", "looked into"), ("designed", "built"), ("added", "implemented"),
    ("and confirmed", "confirming"), ("and validated", "plus validated"),
    ("with timeout policy", "including timeout settings"),
    ("troubleshooting steps", "troubleshooting guide"),
]

def _rephrase(text):
    result = text
    pairs = random.sample(GIST_REPHRASE_PATTERNS, min(3, len(GIST_REPHRASE_PATTERNS)))
    for old, new in pairs:
        result = result.replace(old, new, 1)
    return result

def generate_gist_gold(n=100):
    rows = []
    components = ["search index", "write guard", "embedding cache", "MCP server",
                  "auto recall", "reranker", "intent classifier", "compact context",
                  "WAL journal", "circuit breaker"]
    events = ["timeout", "crash", "regression", "spike", "failure"]
    outcomes = ["recovery", "stability", "accuracy", "latency drop"]
    metrics = ["search latency", "recall rate", "throughput", "error rate"]

    for i in range(1, n + 1):
        t = random.choice(GIST_REFERENCE_TEMPLATES)
        ref = t.format(
            component=random.choice(components),
            event=random.choice(events),
            outcome=random.choice(outcomes),
            metric=random.choice(metrics),
        )
        candidate = _rephrase(ref)
        rows.append({
            "id": f"gist-{i:03d}",
            "reference_gist": ref,
            "candidate_gist": candidate,
        })
    return rows


def main():
    intent_rows = generate_intent_gold(200)
    wg_rows = generate_wg_gold(200)
    gist_rows = generate_gist_gold(100)

    def _write_jsonl(path, rows):
        with open(path, "w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(f"  {path.name}: {len(rows)} cases")

    print("Generating gold sets:")
    _write_jsonl(FIXTURES_DIR / "intent_gold_set.jsonl", intent_rows)
    _write_jsonl(FIXTURES_DIR / "write_guard_gold_set.jsonl", wg_rows)
    _write_jsonl(FIXTURES_DIR / "compact_context_gist_gold_set.jsonl", gist_rows)

    # Stats
    for name, rows in [("Intent", intent_rows), ("Write Guard", wg_rows), ("Gist", gist_rows)]:
        print(f"\n{name} distribution:")
        if name == "Intent":
            from collections import Counter
            c = Counter(r["expected_intent"] for r in rows)
            zh = sum(1 for r in rows if any(ord(ch) > 0x4e00 for ch in r["query"]))
            for k, v in sorted(c.items()):
                print(f"  {k}: {v}")
            print(f"  Chinese: {zh}, English: {len(rows) - zh}")
        elif name == "Write Guard":
            from collections import Counter
            c = Counter(r["expected_action"] for r in rows)
            for k, v in sorted(c.items()):
                print(f"  {k}: {v}")


if __name__ == "__main__":
    main()

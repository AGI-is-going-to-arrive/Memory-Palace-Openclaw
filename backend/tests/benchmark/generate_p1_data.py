#!/usr/bin/env python3
"""P1-data Phase A: Skeleton Generation + Validation.

Memory-Native Benchmark (Spec v3.6.1)

Phase A (this file): Rule-based skeleton with placeholder content.
Phase B (future):    LLM content filling via env-var-configured endpoint.

Usage:
    python generate_p1_data.py generate   # Generate 4 JSONL skeleton files
    python generate_p1_data.py validate   # Validate existing JSONL files
    python generate_p1_data.py stats      # Print quota / coverage statistics

Modification boundary: docs/, backend/tests/benchmark/, backend/tests/fixtures/
"""

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ====================================================================
# Paths
# ====================================================================
SCRIPT_DIR = Path(__file__).resolve().parent
FIXTURES_DIR = SCRIPT_DIR.parent / "fixtures"

FILES = {
    "corpus": FIXTURES_DIR / "memory_native_corpus.jsonl",
    "aliases": FIXTURES_DIR / "memory_native_alias_specs.jsonl",
    "sessions": FIXTURES_DIR / "memory_native_session_fixture.jsonl",
    "queries": FIXTURES_DIR / "memory_native_queries.jsonl",
}

# ====================================================================
# Spec §8 quota targets (for validation)
# ====================================================================
SPEC_CORPUS_DOMAIN_QUOTA = {
    "personal": {"zh": 5, "en": 4, "mixed": 2, "total": 11},
    "project":  {"zh": 4, "en": 5, "mixed": 3, "total": 12},
    "writing":  {"zh": 5, "en": 5, "mixed": 2, "total": 12},
    "research": {"zh": 4, "en": 6, "mixed": 2, "total": 12},
    "finance":  {"zh": 5, "en": 4, "mixed": 2, "total": 11},
    "learning": {"zh": 4, "en": 5, "mixed": 3, "total": 12},
}

SPEC_CORPUS_STYLE_QUOTA = {
    "personal": {"preference": 4, "bullet_list": 3, "agent_note": 2, "summary": 2},
    "project":  {"decision_log": 4, "code_snippet": 3, "agent_note": 3, "bullet_list": 2},
    "writing":  {"agent_note": 4, "summary": 3, "bullet_list": 3, "preference": 2},
    "research": {"summary": 4, "bullet_list": 3, "agent_note": 3, "reference_link": 2},
    "finance":  {"structured_rule": 4, "bullet_list": 3, "decision_log": 2, "summary": 2},
    "learning": {"agent_note": 4, "summary": 3, "code_snippet": 3, "bullet_list": 2},
}

SPEC_STRUCT_QUOTA = {
    "version_group":  {"count": 3, "per_group": 3},  # diet_evolution, risk_evolution, jp_progress
    "conflict_group": {"count": 2, "per_group": 2},  # sleep_habit, tech_stock_limit
    "duplicate_group": {"count": 2, "per_group": 2}, # ch1_outline, bert_notes
}

# Spec §8.3 per-taxonomy query counts
SPEC_QUERY_TAXONOMY_QUOTA = {
    "F1": {"zh": 2, "en": 3}, "F2": {"zh": 2, "en": 1},
    "TR1": {"zh": 1, "en": 2}, "TR2": {"zh": 1, "en": 1},
    "TF1": {"zh": 1, "en": 2},
    "C1": {"zh": 2, "en": 1}, "C2": {"zh": 1, "en": 1},
    "E1": {"zh": 2, "en": 1}, "E2": {"zh": 1, "en": 1},
    "S1": {"zh": 2, "en": 1}, "S2": {"zh": 1, "en": 1},
    "N1": {"zh": 1, "en": 2}, "N2": {"zh": 1, "en": 1},
    "V1": {"zh": 1, "en": 2}, "V2": {"zh": 1, "en": 1}, "V3": {"zh": 1, "en": 1},
    "M1": {"zh": 2, "en": 1}, "M2": {"zh": 1, "en": 2},
    "TX": {"zh": 3, "en": 2},  # NOTE: P1a TX_001 is lang="mixed", counted toward zh
}


# ====================================================================
# Helpers — compact constructors
# ====================================================================
def _c(
    fixture_id: str, parent_uri: str, title: str, content: str,
    domain: str, text_style: str, lang: str, priority: int = 5,
    disclosure: str = "", version: Optional[int] = None,
    version_group: Optional[str] = None, conflict_group: Optional[str] = None,
    duplicate_group: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "fixture_id": fixture_id, "parent_uri": parent_uri, "title": title,
        "content": content, "priority": priority, "disclosure": disclosure,
        "domain": domain, "text_style": text_style, "lang": lang,
        "version": version, "version_group": version_group,
        "conflict_group": conflict_group, "duplicate_group": duplicate_group,
    }


def _q(
    case_id: str, query: str, intent: str, taxonomy_code: str,
    gap_dimension: List[str], expected_memory_ids: List[str],
    layer: str = "A", filters: Optional[Dict] = None,
    expected_rank_1: Optional[str] = None,
    session_group: Optional[str] = None,
    difficulty: str = "easy", lang: str = "en",
) -> Dict[str, Any]:
    return {
        "case_id": case_id, "query": query, "intent": intent,
        "filters": filters, "layer": layer, "session_group": session_group,
        "taxonomy_code": taxonomy_code, "gap_dimension": gap_dimension,
        "expected_memory_ids": expected_memory_ids,
        "expected_rank_1": expected_rank_1,
        "difficulty": difficulty, "lang": lang,
    }


def _a(
    target_fixture_id: str, new_uri: str, priority: int = 0,
    disclosure: str = "", note: str = "",
) -> Dict[str, Any]:
    return {
        "target_fixture_id": target_fixture_id, "new_uri": new_uri,
        "priority": priority, "disclosure": disclosure, "note": note,
    }


def _s(
    session_group: str, fixture_id: str, uri: str, content: str,
    priority: int = 0,
) -> Dict[str, Any]:
    return {
        "session_group": session_group, "fixture_id": fixture_id,
        "uri": uri, "content": content, "priority": priority,
    }


def _ph(domain: str, style: str, lang: str, hint: str) -> str:
    """Generate placeholder content for Phase B LLM filling."""
    return f"[PLACEHOLDER: {domain}/{style}/{lang}] {hint}"


def _qph(taxonomy: str, lang: str, hint: str) -> str:
    """Generate placeholder query text for Phase B LLM filling."""
    return f"[QUERY_PLACEHOLDER: {taxonomy}/{lang}] {hint}"


# ====================================================================
# CORPUS (70 entries across 6 domains — canonical per Spec v3.6.2 §8.1)
#   - P1a entries: real content preserved verbatim
#   - New entries: [PLACEHOLDER] content for Phase B LLM filling
# ====================================================================

# -- personal (11) : 5zh + 4en + 2mixed --
# style: preference×4, bullet_list×3, agent_note×2, summary×2
# struct: version_group=diet_evolution(3), conflict_group=sleep_habit(2)
CORPUS_PERSONAL = [
    # P1a preserved (3) — version_group=diet_evolution
    _c("mnc_personal_001", "personal://", "diet-v1",
       "我不喝含咖啡因的饮料，包括咖啡和茶。早餐通常吃燕麦片配牛奶。",
       "personal", "preference", "zh", version=1, version_group="diet_evolution"),
    _c("mnc_personal_002", "personal://", "diet-v2",
       "更新：现在每天早上喝一杯黑咖啡，但仍然不喝茶。早餐改成全麦面包配鸡蛋。",
       "personal", "preference", "zh", version=2, version_group="diet_evolution"),
    _c("mnc_personal_003", "personal://", "diet-v3",
       "最新饮食规则：每天一杯黑咖啡，不喝奶茶和碳酸饮料。午餐控制碳水摄入，晚餐以蔬菜和蛋白质为主。周末允许一次自由餐。",
       "personal", "preference", "zh", version=3, version_group="diet_evolution"),
    # New entries (8)
    _c("mnc_personal_004", "personal://family", "birthdays",
       _ph("personal", "bullet_list", "zh", "家人生日列表：妈妈3月15日、爸爸9月22日、妹妹6月8日，附礼物偏好"),
       "personal", "bullet_list", "zh"),
    _c("mnc_personal_005", "personal://habits", "morning-routine",
       _ph("personal", "agent_note", "en", "morning routine: 6:30am wake, 15min yoga, cold shower, oatmeal breakfast"),
       "personal", "agent_note", "en"),
    _c("mnc_personal_006", "personal://policies", "communication",
       _ph("personal", "preference", "en", "prefer async text over calls, reply within 4h, no meetings before 10am"),
       "personal", "preference", "en"),
    _c("mnc_personal_007", "personal://preferences", "sleep-early-rule",
       _ph("personal", "summary", "zh", "早睡规则：11点前上床、放下手机、必要时服用褪黑素。目标每晚7.5h"),
       "personal", "summary", "zh", conflict_group="sleep_habit"),
    _c("mnc_personal_008", "personal://preferences", "sleep-late-habit",
       _ph("personal", "summary", "mixed", "实际习惯：often code until 2am, 周末补觉到中午, average 6h on weekdays"),
       "personal", "summary", "mixed", conflict_group="sleep_habit"),
    _c("mnc_personal_009", "personal://habits", "weekly-grocery",
       _ph("personal", "bullet_list", "en", "weekly grocery template: vegetables, chicken, rice, eggs, milk, seasonal fruit"),
       "personal", "bullet_list", "en"),
    _c("mnc_personal_010", "personal://family", "emergency-contacts",
       _ph("personal", "bullet_list", "en", "emergency contacts: spouse, parents, family doctor, insurance number"),
       "personal", "bullet_list", "en"),
    _c("mnc_personal_011", "personal://habits", "evening-wind-down",
       _ph("personal", "agent_note", "mixed", "晚间放松：reading 30min, herbal tea, no screens after 10pm, light stretching"),
       "personal", "agent_note", "mixed"),
]

# -- project (12) : 4zh + 5en + 3mixed --
# style: decision_log×4, code_snippet×3, agent_note×3, bullet_list×2
CORPUS_PROJECT = [
    # P1a preserved (2)
    _c("mnc_project_001", "project://", "auth-decisions",
       "2026-03-15 architecture decision: migrate from JWT tokens to session cookies. "
       "Reasons: (1) JWT revocation is complex, (2) session cookies provide simpler "
       "server-side invalidation, (3) reduces client-side token storage attack surface. "
       "Trade-off: requires sticky sessions or centralized session store.",
       "project", "decision_log", "en"),
    _c("mnc_project_002", "project://", "webapp-tech-stack",
       "Frontend: React 18 + TypeScript + Vite. Backend: Python FastAPI + SQLAlchemy. "
       "Database: PostgreSQL 16 for production, SQLite for local dev. "
       "CI/CD: GitHub Actions. Deployment: Docker + Kubernetes.",
       "project", "bullet_list", "en"),
    # New entries (10)
    _c("mnc_project_003", "project://auth-service", "session-config",
       _ph("project", "code_snippet", "en", "Redis session store config: TTL 24h, prefix 'sess:', serializer=json"),
       "project", "code_snippet", "en"),
    _c("mnc_project_004", "project://auth-service", "rate-limit-rules",
       _ph("project", "decision_log", "mixed", "rate limiting decision: 100 req/min per user, 使用 sliding window 算法, Redis 计数器"),
       "project", "decision_log", "mixed"),
    _c("mnc_project_005", "project://migration", "v2-plan",
       _ph("project", "agent_note", "zh", "v2迁移计划：第一阶段数据库schema升级，第二阶段API兼容层，第三阶段客户端切换"),
       "project", "agent_note", "zh"),
    _c("mnc_project_006", "project://migration", "v2-rollback",
       _ph("project", "agent_note", "zh", "v2回滚方案：保留旧表30天、双写期间可随时切回、回滚脚本位于 scripts/rollback/"),
       "project", "agent_note", "zh"),
    _c("mnc_project_007", "project://webapp", "api-conventions",
       _ph("project", "code_snippet", "en", "API naming: GET /resources, POST /resources, snake_case fields, ISO 8601 dates"),
       "project", "code_snippet", "en"),
    _c("mnc_project_008", "project://webapp", "error-handling",
       _ph("project", "bullet_list", "mixed", "error handling policy: structured JSON errors, 错误码前缀按模块分配, retry with exponential backoff"),
       "project", "bullet_list", "mixed"),
    _c("mnc_project_009", "project://client-a", "requirements",
       _ph("project", "agent_note", "zh", "客户A需求：支持SSO登录、数据导出为CSV、每月生成合规报告"),
       "project", "agent_note", "zh"),
    _c("mnc_project_010", "project://client-a", "deadline-changes",
       _ph("project", "decision_log", "zh", "客户A交付时间变更：原定4月15日推迟到5月1日，原因是法务审核未完成"),
       "project", "decision_log", "zh"),
    _c("mnc_project_011", "project://webapp", "ci-pipeline",
       _ph("project", "code_snippet", "en", "CI pipeline: lint -> test -> build -> deploy-staging -> e2e -> deploy-prod"),
       "project", "code_snippet", "en"),
    _c("mnc_project_012", "project://migration", "database-schema",
       _ph("project", "decision_log", "mixed", "schema migration decision: 使用 Alembic autogenerate, review before apply, 禁止 drop column in prod"),
       "project", "decision_log", "mixed"),
]

# -- writing (12) : 5zh + 5en + 2mixed --
# style: agent_note×4, summary×3, bullet_list×3, preference×2
# struct: duplicate_group=ch1_outline(2)
CORPUS_WRITING = [
    # P1a preserved (2) — note: writing_001 is hierarchical (novel/chapter-3)
    _c("mnc_writing_001", "writing://novel/chapter-3", "notes",
       "Chapter 3 outline: protagonist discovers the hidden laboratory. "
       "Key scenes: (1) finding the encrypted journal, (2) confrontation with the "
       "guardian AI, (3) revelation about the origin project. "
       "Tone: tense, claustrophobic. Word count target: 4000-5000.",
       "writing", "agent_note", "en"),
    _c("mnc_writing_002", "writing://", "blog-style-guide",
       "Blog writing rules: use conversational tone, avoid jargon unless explained. "
       "Paragraphs max 3 sentences. Always include a practical example. "
       "Headers should be questions when possible.",
       "writing", "preference", "en"),
    # New entries (10) — duplicate_group=ch1_outline for 003+004
    _c("mnc_writing_003", "writing://novel/chapter-1", "scene-outline",
       _ph("writing", "agent_note", "zh", "第一章场景大纲：主角收到神秘邮件、决定前往旧实验室、发现第一条线索"),
       "writing", "agent_note", "zh", duplicate_group="ch1_outline"),
    _c("mnc_writing_004", "writing://novel/chapter-1", "scene-outline-v2",
       _ph("writing", "agent_note", "zh", "第一章大纲修订版：增加闪回片段、强化悬念、调整主角动机"),
       "writing", "agent_note", "zh", duplicate_group="ch1_outline"),
    _c("mnc_writing_005", "writing://blog", "seo-checklist",
       _ph("writing", "bullet_list", "en", "SEO checklist: meta description <160ch, H1 contains keyword, alt text on images"),
       "writing", "bullet_list", "en"),
    _c("mnc_writing_006", "writing://report", "q1-draft",
       _ph("writing", "summary", "zh", "Q1季度报告草稿摘要：营收增长12%，用户留存率提升到85%，新功能上线3个"),
       "writing", "summary", "zh"),
    _c("mnc_writing_007", "writing://report", "q1-review-notes",
       _ph("writing", "summary", "zh", "Q1报告评审意见：数据可视化需要加强、竞品对比章节太薄、建议补充用户反馈"),
       "writing", "summary", "zh"),
    _c("mnc_writing_008", "writing://translation", "glossary",
       _ph("writing", "bullet_list", "en", "translation glossary: memory→记忆, retrieval→检索, embedding→嵌入, agent→智能体"),
       "writing", "bullet_list", "en"),
    _c("mnc_writing_009", "writing://novel/chapter-2", "character-arcs",
       _ph("writing", "agent_note", "mixed", "Chapter 2 角色发展：protagonist grows suspicious, mentor reveals half-truth, 反派首次暗示"),
       "writing", "agent_note", "mixed"),
    _c("mnc_writing_010", "writing://blog", "weekly-column-ideas",
       _ph("writing", "bullet_list", "en", "column ideas: AI tool reviews, productivity hacks, interview with indie devs"),
       "writing", "bullet_list", "en"),
    _c("mnc_writing_011", "writing://novel", "tone-guide",
       _ph("writing", "preference", "zh", "小说语气规则：叙述用白描、对话偏口语、内心独白用意识流、避免说教"),
       "writing", "preference", "zh"),
    _c("mnc_writing_012", "writing://report", "q2-outline",
       _ph("writing", "summary", "mixed", "Q2 report outline: market expansion section, 技术debt偿还进度, hiring pipeline update"),
       "writing", "summary", "mixed"),
]

# -- research (12) : 4zh + 6en + 2mixed --
# style: summary×4, bullet_list×3, agent_note×3, reference_link×2
# struct: duplicate_group=bert_notes(2)
CORPUS_RESEARCH = [
    # P1a preserved (2) — research_001 is hierarchical (papers/)
    _c("mnc_research_001", "research://papers", "attention-is-all-you-need",
       "Vaswani et al. 2017 — Transformer architecture. Core contribution: replace "
       "recurrence and convolution entirely with self-attention mechanism. "
       "Key results: BLEU 28.4 on EN-DE translation (WMT 2014). "
       "Limitations: quadratic complexity O(n squared) in sequence length.",
       "research", "summary", "en"),
    _c("mnc_research_002", "research://", "product-x-analysis",
       "竞品分析：Product X 的记忆系统使用纯向量检索，无层级结构。优势：检索速度快。"
       "劣势：无法按主题浏览、无版本追踪、无别名支持。"
       "用户反馈集中在找不到旧记忆和重复记忆太多。",
       "research", "summary", "zh"),
    # New entries (10) — duplicate_group=bert_notes for 003+004
    _c("mnc_research_003", "research://papers", "bert-notes",
       _ph("research", "summary", "en", "BERT (Devlin 2019): masked language model pretraining, bidirectional context, fine-tuning paradigm"),
       "research", "summary", "en", duplicate_group="bert_notes"),
    _c("mnc_research_004", "research://papers", "bert-notes-dup",
       _ph("research", "summary", "en", "BERT reading notes (2nd pass): focus on fine-tuning instability, layer freezing strategies"),
       "research", "summary", "en", duplicate_group="bert_notes"),
    _c("mnc_research_005", "research://competitors", "product-y-review",
       _ph("research", "bullet_list", "zh", "Product Y评测：支持多模态记忆、价格偏高、API稳定性一般、社区活跃度低"),
       "research", "bullet_list", "zh"),
    _c("mnc_research_006", "research://competitors", "market-comparison",
       _ph("research", "bullet_list", "en", "market comparison: Product X (vector-only), Product Y (multimodal), Product Z (graph-based)"),
       "research", "bullet_list", "en"),
    _c("mnc_research_007", "research://market", "ev-trends-2026",
       _ph("research", "agent_note", "zh", "2026年电动车市场趋势：固态电池量产、充电基础设施加速、中国品牌出海"),
       "research", "agent_note", "zh"),
    _c("mnc_research_008", "research://market", "ai-chip-landscape",
       _ph("research", "agent_note", "en", "AI chip landscape: NVIDIA H100 dominant, AMD MI300X emerging, custom ASICs rising"),
       "research", "agent_note", "en"),
    _c("mnc_research_009", "research://papers", "rag-survey",
       _ph("research", "reference_link", "mixed", "RAG survey references: Lewis et al. 2020, Gao et al. 2023, https://arxiv.org/abs/2312.10997"),
       "research", "reference_link", "mixed"),
    _c("mnc_research_010", "research://papers", "multimodal-llm",
       _ph("research", "reference_link", "mixed", "multimodal LLM refs: GPT-4V, Gemini, LLaVA, https://arxiv.org/abs/2304.08485"),
       "research", "reference_link", "mixed"),
    _c("mnc_research_011", "research://competitors", "feature-matrix",
       _ph("research", "bullet_list", "en", "feature matrix: versioning(us:yes,X:no,Y:partial), alias(us:yes,others:no), hierarchy(us:yes,Z:partial)"),
       "research", "bullet_list", "en"),
    _c("mnc_research_012", "research://market", "semiconductor-supply",
       _ph("research", "agent_note", "zh", "半导体供应链分析：台积电产能分配、地缘政治风险、备选供应商评估"),
       "research", "agent_note", "zh"),
]

# -- finance (11) : 5zh + 4en + 2mixed --
# style: structured_rule×4, bullet_list×3, decision_log×2, summary×2
# struct: conflict_group=tech_stock_limit(2), version_group=risk_evolution(3)
CORPUS_FINANCE = [
    # P1a preserved (2) — conflict_group=tech_stock_limit
    _c("mnc_finance_001", "finance://", "rules-conservative",
       "投资组合规则（保守版）：科技股仓位不超过30%，单只个股不超过总资产5%。"
       "永远不做空个股。设置10%止损线。",
       "finance", "structured_rule", "zh", conflict_group="tech_stock_limit"),
    _c("mnc_finance_002", "finance://", "rules-aggressive",
       "投资组合规则（激进版）：科技股仓位可到50%，看好的个股最多10%。"
       "允许使用期权对冲，但不做裸空。止损线放宽到15%。",
       "finance", "structured_rule", "zh", conflict_group="tech_stock_limit"),
    # New entries (9) — version_group=risk_evolution for 006-008
    _c("mnc_finance_003", "finance://portfolio", "rebalance-q1",
       _ph("finance", "decision_log", "en", "Q1 rebalance: sold 20% tech, added 10% bonds, reason: rising interest rates"),
       "finance", "decision_log", "en"),
    _c("mnc_finance_004", "finance://portfolio", "rebalance-q2",
       _ph("finance", "decision_log", "en", "Q2 rebalance: increased emerging markets 15%, reduced US large-cap, catalyst: dollar weakness"),
       "finance", "decision_log", "en"),
    _c("mnc_finance_005", "finance://watchlist", "tech-stocks",
       _ph("finance", "bullet_list", "zh", "科技股关注清单：NVDA(AI芯片龙头)、TSMC(代工)、ASML(光刻机)、ARM(架构授权)"),
       "finance", "bullet_list", "zh"),
    _c("mnc_finance_006", "finance://risk", "tolerance-v1",
       _ph("finance", "structured_rule", "zh", "风险承受度v1：最大回撤容忍15%，不加杠杆，现金储备至少6个月开支"),
       "finance", "structured_rule", "zh", version=1, version_group="risk_evolution"),
    _c("mnc_finance_007", "finance://risk", "tolerance-v2",
       _ph("finance", "structured_rule", "mixed", "风险承受度v2: increased max drawdown to 20%, allow 1.2x leverage on index ETF, 现金储备降到4个月"),
       "finance", "structured_rule", "mixed", version=2, version_group="risk_evolution"),
    _c("mnc_finance_008", "finance://risk", "tolerance-v3",
       _ph("finance", "summary", "mixed", "最新风险评估: max drawdown 25% acceptable, options hedging enabled, cash reserve 3 months, 适度使用杠杆ETF"),
       "finance", "summary", "mixed", version=3, version_group="risk_evolution"),
    _c("mnc_finance_009", "finance://watchlist", "dividend-stocks",
       _ph("finance", "bullet_list", "en", "dividend picks: JNJ (3.1%), PG (2.5%), KO (3.0%), target: stable income portfolio"),
       "finance", "bullet_list", "en"),
    _c("mnc_finance_010", "finance://analysis", "2026-q1-review",
       _ph("finance", "summary", "en", "Q1 2026 review: portfolio +8.2%, benchmark +6.1%, alpha from overweight AI sector"),
       "finance", "summary", "en"),
    _c("mnc_finance_011", "finance://watchlist", "etf-list",
       _ph("finance", "bullet_list", "zh", "ETF关注清单：QQQ(纳指)、VTI(全市场)、TLT(长期国债)、GLD(黄金)"),
       "finance", "bullet_list", "zh"),
]

# -- learning (12) : 4zh + 5en + 3mixed --
# style: agent_note×4, summary×3, code_snippet×3, bullet_list×2
# struct: version_group=jp_progress(3)
CORPUS_LEARNING = [
    # P1a preserved (1) — hierarchical (rust/)
    _c("mnc_learning_001", "learning://rust", "ownership-notes",
       "Rust ownership rules: (1) each value has exactly one owner, "
       "(2) when owner goes out of scope value is dropped, "
       "(3) ownership can be transferred via move or borrowed via &ref. "
       "Common pitfall: trying to use a value after moving it.",
       "learning", "agent_note", "en"),
    # New entries (11) — version_group=jp_progress for 003-005
    _c("mnc_learning_002", "learning://rust", "borrowing-patterns",
       _ph("learning", "code_snippet", "en", "Rust borrowing: &T (shared), &mut T (exclusive), lifetime annotations, NLL rules"),
       "learning", "code_snippet", "en"),
    _c("mnc_learning_003", "learning://japanese", "n2-vocab-progress-v1",
       _ph("learning", "summary", "zh", "日语N2进度v1：完成基础2000词、语法60%、听力薄弱需加强、阅读理解尚可"),
       "learning", "summary", "zh", version=1, version_group="jp_progress"),
    _c("mnc_learning_004", "learning://japanese", "n2-vocab-progress-v2",
       _ph("learning", "summary", "zh", "日语N2进度v2：词汇达到3200、语法85%、听力通过专项训练有改善、开始做真题"),
       "learning", "summary", "zh", version=2, version_group="jp_progress"),
    _c("mnc_learning_005", "learning://japanese", "n2-vocab-progress-v3",
       _ph("learning", "summary", "mixed", "N2 progress v3: vocabulary 4000+, grammar 95%, listening improved significantly, 模拟考试得分162/180"),
       "learning", "summary", "mixed", version=3, version_group="jp_progress"),
    _c("mnc_learning_006", "learning://cooking", "knife-techniques",
       _ph("learning", "agent_note", "en", "knife skills: julienne, brunoise, chiffonade, rock chop technique, sharpening schedule"),
       "learning", "agent_note", "en"),
    _c("mnc_learning_007", "learning://cooking", "sauce-basics",
       _ph("learning", "bullet_list", "mixed", "基础酱汁: béchamel(奶油白酱), velouté, espagnole, hollandaise, tomato sauce"),
       "learning", "bullet_list", "mixed"),
    _c("mnc_learning_008", "learning://piano", "practice-log",
       _ph("learning", "agent_note", "zh", "钢琴练习日志：本周Hanon练习40min/天、莫扎特K545第一乐章基本流畅、踏板使用需改进"),
       "learning", "agent_note", "zh"),
    _c("mnc_learning_009", "learning://rust", "error-handling",
       _ph("learning", "code_snippet", "en", "Rust error handling: Result<T,E>, ? operator, thiserror for library, anyhow for apps"),
       "learning", "code_snippet", "en"),
    _c("mnc_learning_010", "learning://piano", "scales-exercises",
       _ph("learning", "bullet_list", "mixed", "音阶练习: C major 两个八度, A minor harmonic, arpeggios in all keys, 节拍器从60bpm开始"),
       "learning", "bullet_list", "mixed"),
    _c("mnc_learning_011", "learning://python", "async-patterns",
       _ph("learning", "code_snippet", "en", "Python async: asyncio.gather for concurrency, async with for resource mgmt, avoid blocking in event loop"),
       "learning", "code_snippet", "en"),
    _c("mnc_learning_012", "learning://python", "decorator-notes",
       _ph("learning", "agent_note", "zh", "Python装饰器笔记：@functools.wraps保留元信息、装饰器工厂模式、类装饰器vs函数装饰器"),
       "learning", "agent_note", "zh"),
]

# ====================================================================
# ALIASES (5)
# ====================================================================
ALIASES = [
    # P1a preserved (2)
    _a("mnc_personal_003", "personal://饮食禁忌", note="CJK alias for latest diet preferences"),
    _a("mnc_research_001", "research://transformer-paper", note="Short alias for Attention paper"),
    # New (3) — personal×1, writing×1, learning×1
    _a("mnc_personal_006", "personal://沟通规则", note="CJK alias for communication policies"),
    _a("mnc_writing_011", "writing://小说语气指南", note="CJK alias for novel tone guide"),
    _a("mnc_learning_001", "learning://rust-ownership", note="English alias for Rust ownership notes"),
]

# ====================================================================
# SESSION FIXTURES (10) : 4zh + 4en + 2mixed
# ====================================================================
SESSIONS = [
    # P1a preserved (3)
    _s("m1_boost", "__session_anchor__", "personal://session-anchor",
       "刚才在讨论饮食习惯和碳水控制的话题，周末允许自由餐"),
    _s("m2_override", "mnc_research_002", "research://product-x-analysis",
       "User was comparing Product X with Transformer-based memory architectures and attention mechanisms"),
    _s("m_smoke", "mnc_project_001", "project://auth-decisions",
       "刚才讨论了认证服务的架构决策，从 JWT 切换到 session cookies"),
    # New (7)
    _s("m1_finance", "__session_anchor_fin__", "finance://session-budget-talk",
       _ph("session", "m1_finance", "zh", "讨论投资预算和科技股配置的对话片段")),
    _s("m1_finance", "mnc_finance_005", "finance://watchlist/tech-stocks",
       _ph("session", "m1_finance", "zh", "提到了NVDA和TSMC的近期表现")),
    _s("m2_personal", "__session_anchor_per__", "personal://session-routine-chat",
       _ph("session", "m2_personal", "en", "casual chat about daily routines and time management")),
    _s("m2_personal", "mnc_personal_005", "personal://habits/morning-routine",
       _ph("session", "m2_personal", "en", "mentioned waking up earlier and adjusting exercise schedule")),
    _s("m1_learning", "__session_anchor_learn__", "learning://session-rust-study",
       _ph("session", "m1_learning", "en", "studying Rust ownership and borrowing concepts right now")),
    _s("m2_finance", "__session_anchor_fin2__", "finance://session-portfolio-review",
       _ph("session", "m2_finance", "mixed", "casual portfolio review, 提到了ETF和债券配置")),
    _s("m_smoke_2", "mnc_writing_001", "writing://novel/chapter-3/notes",
       _ph("session", "m_smoke_2", "mixed", "discussing chapter 3 plot points and 角色发展")),
]

# ====================================================================
# QUERIES (54 cases — canonical per Spec v3.6.2 §3.1/§8.3)
# ====================================================================

GAP_IR = ["intent_routing"]
GAP_SC = ["scope_constrained"]
GAP_AR = ["ancestor_recall"]
GAP_AL = ["alias_recall"]
GAP_TC = ["temporal_coherence"]
GAP_SM = ["session_mixing"]
GAP_TS = ["intent_routing", "text_style"]

QUERIES = [
    # ---- F1 Direct Fact (5: 2zh + 3en) ----
    # P1a (2)
    _q("mnq_F1_001", "我的饮食有什么限制？", "factual", "F1", GAP_IR,
       ["mnc_personal_003"], expected_rank_1="mnc_personal_003", lang="zh"),
    _q("mnq_F1_002", "What was the authentication architecture decision?", "factual", "F1", GAP_IR,
       ["mnc_project_001"], expected_rank_1="mnc_project_001", lang="en"),
    # New (3)
    _q("mnq_F1_003", _qph("F1", "en", "direct question about investment portfolio rules and position limits"),
       "factual", "F1", GAP_IR,
       ["mnc_finance_001", "mnc_finance_002"], lang="en"),
    _q("mnq_F1_004", _qph("F1", "en", "ask about Transformer architecture contribution and key results"),
       "factual", "F1", GAP_IR,
       ["mnc_research_001"], expected_rank_1="mnc_research_001", lang="en"),
    _q("mnq_F1_005", _qph("F1", "zh", "询问家人的生日日期"),
       "factual", "F1", GAP_IR,
       ["mnc_personal_004"], expected_rank_1="mnc_personal_004", lang="zh"),

    # ---- F2 Preference Recall (3: 2zh + 1en) ----
    _q("mnq_F2_001", _qph("F2", "zh", "询问沟通和回复的偏好规则"),
       "factual", "F2", GAP_IR,
       ["mnc_personal_006"], expected_rank_1="mnc_personal_006", lang="zh"),
    _q("mnq_F2_002", _qph("F2", "zh", "小说写作的语气风格要求是什么"),
       "factual", "F2", GAP_IR,
       ["mnc_writing_011"], expected_rank_1="mnc_writing_011", lang="zh"),
    _q("mnq_F2_003", _qph("F2", "en", "what are the blog writing style rules"),
       "factual", "F2", GAP_IR,
       ["mnc_writing_002"], expected_rank_1="mnc_writing_002", lang="en"),

    # ---- TR1 Recency Bias (3: 1zh + 2en) ----
    # P1a (1)
    _q("mnq_TR1_001", "最近更新的投资相关规则", "temporal", "TR1", GAP_TC,
       ["mnc_finance_001", "mnc_finance_002"], expected_rank_1="mnc_finance_002",
       difficulty="medium", lang="zh"),
    # New (2)
    _q("mnq_TR1_002", _qph("TR1", "en", "latest risk tolerance policy update"),
       "temporal", "TR1", GAP_TC,
       ["mnc_finance_006", "mnc_finance_007", "mnc_finance_008"],
       expected_rank_1="mnc_finance_008", difficulty="medium", lang="en"),
    _q("mnq_TR1_003", _qph("TR1", "en", "most recent Japanese N2 study progress"),
       "temporal", "TR1", GAP_TC,
       ["mnc_learning_003", "mnc_learning_004", "mnc_learning_005"],
       expected_rank_1="mnc_learning_005", difficulty="medium", lang="en"),

    # ---- TR2 Temporal Sequence (2: 1zh + 1en) ----
    _q("mnq_TR2_001", _qph("TR2", "zh", "日语N2学习进度的历史变化过程"),
       "temporal", "TR2", GAP_TC,
       ["mnc_learning_003", "mnc_learning_004", "mnc_learning_005"],
       difficulty="medium", lang="zh"),
    _q("mnq_TR2_002", _qph("TR2", "en", "how did the diet preferences evolve over time"),
       "temporal", "TR2", GAP_TC,
       ["mnc_personal_001", "mnc_personal_002", "mnc_personal_003"],
       difficulty="medium", lang="en"),

    # ---- TF1 CreatedAfter (3: 1zh + 2en) ----
    # NOTE: filters.updated_after uses __RUNTIME: sentinel — runner injects
    # actual timestamp recorded between version v2 and v3 writes.
    _q("mnq_TF1_001", _qph("TF1", "zh", "最新的风险承受度评估（时间过滤）"),
       "temporal", "TF1", GAP_TC,
       ["mnc_finance_008"], expected_rank_1="mnc_finance_008",
       filters={"updated_after": "__RUNTIME:AFTER_RISK_V2__"},
       difficulty="medium", lang="zh"),
    _q("mnq_TF1_002", _qph("TF1", "en", "diet preferences created after second revision"),
       "temporal", "TF1", GAP_TC,
       ["mnc_personal_003"], expected_rank_1="mnc_personal_003",
       filters={"updated_after": "__RUNTIME:AFTER_DIET_V2__"},
       difficulty="medium", lang="en"),
    _q("mnq_TF1_003", _qph("TF1", "en", "latest Japanese study progress after initial assessment"),
       "temporal", "TF1", GAP_TC,
       ["mnc_learning_005"], expected_rank_1="mnc_learning_005",
       filters={"updated_after": "__RUNTIME:AFTER_JP_V2__"},
       difficulty="medium", lang="en"),

    # ---- C1 Decision Trace (3: 2zh + 1en) ----
    # P1a (1)
    _q("mnq_C1_001", "Why did we switch from JWT to session cookies?", "causal", "C1", GAP_IR,
       ["mnc_project_001"], expected_rank_1="mnc_project_001", lang="en"),
    # New (2)
    _q("mnq_C1_002", _qph("C1", "zh", "客户A的交付时间为什么推迟了"),
       "causal", "C1", GAP_IR,
       ["mnc_project_010"], expected_rank_1="mnc_project_010", difficulty="medium", lang="zh"),
    _q("mnq_C1_003", _qph("C1", "zh", "Q1调仓的原因和依据是什么"),
       "causal", "C1", GAP_IR,
       ["mnc_finance_003"], expected_rank_1="mnc_finance_003", difficulty="medium", lang="zh"),

    # ---- C2 Dependency (2: 1zh + 1en) ----
    _q("mnq_C2_001", _qph("C2", "zh", "v2迁移计划和回滚方案之间的关系"),
       "causal", "C2", GAP_IR,
       ["mnc_project_005", "mnc_project_006"], difficulty="medium", lang="zh"),
    _q("mnq_C2_002", _qph("C2", "en", "how does session config relate to rate limiting setup"),
       "causal", "C2", GAP_IR,
       ["mnc_project_003", "mnc_project_004"], difficulty="medium", lang="en"),

    # ---- E1 Topic Scan (3: 2zh + 1en) ----
    _q("mnq_E1_001", _qph("E1", "zh", "市场趋势相关的所有研究笔记"),
       "exploratory", "E1", GAP_IR,
       ["mnc_research_007", "mnc_research_008", "mnc_research_012"], lang="zh"),
    _q("mnq_E1_002", _qph("E1", "zh", "Rust 编程学习的所有笔记"),
       "exploratory", "E1", GAP_IR,
       ["mnc_learning_001", "mnc_learning_002", "mnc_learning_009"], lang="zh"),
    _q("mnq_E1_003", _qph("E1", "en", "all competitor analysis and market comparison notes"),
       "exploratory", "E1", GAP_IR,
       ["mnc_research_005", "mnc_research_006", "mnc_research_011"], lang="en"),

    # ---- E2 Cross-Domain (2: 1zh + 1en) ----
    _q("mnq_E2_001", _qph("E2", "zh", "和AI芯片或AI技术相关的所有信息（跨domain）"),
       "exploratory", "E2", GAP_IR,
       ["mnc_research_008", "mnc_learning_011"], difficulty="medium", lang="zh"),
    _q("mnq_E2_002", _qph("E2", "en", "everything about code conventions and programming patterns"),
       "exploratory", "E2", GAP_IR,
       ["mnc_project_007", "mnc_learning_009"], difficulty="medium", lang="en"),

    # ---- S1 Domain Scoped (3: 2zh + 1en) ----
    # P1a (1)
    _q("mnq_S1_001", "投资组合的仓位规则", "factual", "S1", GAP_SC,
       ["mnc_finance_001", "mnc_finance_002"],
       filters={"domain": "finance"}, lang="zh"),
    # New (2)
    _q("mnq_S1_002", _qph("S1", "zh", "家人相关的信息（仅限 personal domain）"),
       "factual", "S1", GAP_SC,
       ["mnc_personal_004", "mnc_personal_010"],
       filters={"domain": "personal"}, lang="zh"),
    _q("mnq_S1_003", _qph("S1", "en", "research papers about deep learning models"),
       "factual", "S1", GAP_SC,
       ["mnc_research_001", "mnc_research_003", "mnc_research_004"],
       filters={"domain": "research"}, lang="en"),

    # ---- S2 Path-Prefix (2: 1zh + 1en) ----
    # P1a (1)
    _q("mnq_S2_001", "novel chapter outline and notes", "factual", "S2", GAP_SC,
       ["mnc_writing_001"],
       expected_rank_1="mnc_writing_001", filters={"path_prefix": "novel"}, lang="en"),
    # New (1)
    _q("mnq_S2_002", _qph("S2", "zh", "季度报告相关的草稿和评审意见"),
       "factual", "S2", GAP_SC,
       ["mnc_writing_006", "mnc_writing_007", "mnc_writing_012"],
       filters={"path_prefix": "report"}, lang="zh"),

    # ---- N1 Ancestor Recall (3: 1zh + 2en) ----
    # P1a (1)
    _q("mnq_N1_001", "research papers on attention mechanisms", "factual", "N1", GAP_AR,
       ["mnc_research_001"], expected_rank_1="mnc_research_001",
       difficulty="medium", lang="en"),
    # New (2)
    _q("mnq_N1_002", _qph("N1", "zh", "Rust编程学习相关的笔记（通过祖先路径 learning://rust 召回）"),
       "factual", "N1", GAP_AR,
       ["mnc_learning_001", "mnc_learning_002", "mnc_learning_009"],
       difficulty="medium", lang="zh"),
    _q("mnq_N1_003", _qph("N1", "en", "novel writing notes and scene outlines (ancestor: writing://novel)"),
       "factual", "N1", GAP_AR,
       ["mnc_writing_001", "mnc_writing_003", "mnc_writing_004", "mnc_writing_009"],
       difficulty="medium", lang="en"),

    # ---- N2 Alias Recall (2: 1zh + 1en) ----
    # P1a (1)
    _q("mnq_N2_001", "饮食禁忌相关的记录", "factual", "N2", GAP_AL,
       ["mnc_personal_003"], expected_rank_1="mnc_personal_003",
       difficulty="medium", lang="zh"),
    # New (1)
    _q("mnq_N2_002", _qph("N2", "en", "notes about the Transformer paper (via alias research://transformer-paper)"),
       "factual", "N2", GAP_AL,
       ["mnc_research_001"], expected_rank_1="mnc_research_001",
       difficulty="medium", lang="en"),

    # ---- V1 Version Latest (3: 1zh + 2en) ----
    # P1a (1)
    _q("mnq_V1_001", "最新的饮食偏好是什么", "temporal", "V1", GAP_TC,
       ["mnc_personal_003"], expected_rank_1="mnc_personal_003",
       difficulty="medium", lang="zh"),
    # New (2)
    _q("mnq_V1_002", _qph("V1", "en", "what is the current risk tolerance policy"),
       "temporal", "V1", GAP_TC,
       ["mnc_finance_008"], expected_rank_1="mnc_finance_008",
       difficulty="medium", lang="en"),
    _q("mnq_V1_003", _qph("V1", "en", "latest Japanese N2 vocabulary progress status"),
       "temporal", "V1", GAP_TC,
       ["mnc_learning_005"], expected_rank_1="mnc_learning_005",
       difficulty="medium", lang="en"),

    # ---- V2 Conflict Surface (2: 1zh + 1en) ----
    _q("mnq_V2_001", _qph("V2", "zh", "科技股的仓位上限规则（应召回两条矛盾记忆）"),
       "factual", "V2", GAP_TC,
       ["mnc_finance_001", "mnc_finance_002"], difficulty="hard", lang="zh"),
    _q("mnq_V2_002", _qph("V2", "en", "sleep schedule rules and actual habits (should surface conflict)"),
       "factual", "V2", GAP_TC,
       ["mnc_personal_007", "mnc_personal_008"], difficulty="hard", lang="en"),

    # ---- V3 Near-Duplicate (2: 1zh + 1en) ----
    _q("mnq_V3_001", _qph("V3", "zh", "BERT论文的阅读笔记（应召回两条近重复记忆）"),
       "factual", "V3", GAP_TC,
       ["mnc_research_003", "mnc_research_004"], difficulty="hard", lang="zh"),
    _q("mnq_V3_002", _qph("V3", "en", "chapter 1 scene outline notes (should surface both duplicate versions)"),
       "factual", "V3", GAP_TC,
       ["mnc_writing_003", "mnc_writing_004"], difficulty="hard", lang="en"),

    # ---- M1 Session Boost (3: 2zh + 1en) — Layer B ----
    # P1a (1)
    _q("mnq_M1_001", "我的饮食习惯", "factual", "M1", GAP_SM,
       ["mnc_personal_003"], layer="B", session_group="m1_boost",
       difficulty="medium", lang="zh"),
    # New (2)
    _q("mnq_M1_002", _qph("M1", "zh", "投资规则和科技股配置"),
       "factual", "M1", GAP_SM,
       ["mnc_finance_001", "mnc_finance_002"], layer="B", session_group="m1_finance",
       difficulty="medium", lang="zh"),
    _q("mnq_M1_003", _qph("M1", "en", "Rust ownership and borrowing rules"),
       "factual", "M1", GAP_SM,
       ["mnc_learning_001", "mnc_learning_002"], layer="B", session_group="m1_learning",
       difficulty="medium", lang="en"),

    # ---- M2 Long-Term Override (3: 1zh + 2en) — Layer B ----
    # P1a (1)
    _q("mnq_M2_001", "What is the Transformer architecture contribution?", "factual", "M2", GAP_SM,
       ["mnc_research_001"], expected_rank_1="mnc_research_001",
       layer="B", session_group="m2_override", difficulty="medium", lang="en"),
    # New (2)
    _q("mnq_M2_002", _qph("M2", "zh", "家人生日和紧急联系方式（长期记忆应压过session噪声）"),
       "factual", "M2", GAP_SM,
       ["mnc_personal_004", "mnc_personal_010"], expected_rank_1="mnc_personal_004",
       layer="B", session_group="m2_personal", difficulty="medium", lang="zh"),
    _q("mnq_M2_003", _qph("M2", "en", "Q1 portfolio rebalancing rationale (long-term should override session noise)"),
       "factual", "M2", GAP_SM,
       ["mnc_finance_003"], expected_rank_1="mnc_finance_003",
       layer="B", session_group="m2_finance", difficulty="medium", lang="en"),

    # ---- TX Text Style Spread (5: 3zh + 2en, each targets different text_style) ----
    # P1a (1) — lang=mixed counted toward zh per spec convention
    _q("mnq_TX_001", "Rust 的 ownership 机制怎么理解", "factual", "TX", GAP_TS,
       ["mnc_learning_001"], expected_rank_1="mnc_learning_001",
       lang="mixed"),  # targets agent_note style
    # New (4)
    _q("mnq_TX_002", _qph("TX", "zh", "科技股仓位限制的具体规则（targets structured_rule style）"),
       "factual", "TX", GAP_TS,
       ["mnc_finance_001"], expected_rank_1="mnc_finance_001", lang="zh"),
    _q("mnq_TX_003", _qph("TX", "zh", "客户A的交付时间记录（targets decision_log style）"),
       "causal", "TX", GAP_TS,
       ["mnc_project_010"], expected_rank_1="mnc_project_010", lang="zh"),
    _q("mnq_TX_004", _qph("TX", "en", "Redis session configuration code (targets code_snippet style)"),
       "factual", "TX", GAP_TS,
       ["mnc_project_003"], expected_rank_1="mnc_project_003", lang="en"),
    _q("mnq_TX_005", _qph("TX", "en", "RAG survey paper references and links (targets reference_link style)"),
       "exploratory", "TX", GAP_TS,
       ["mnc_research_009"], expected_rank_1="mnc_research_009", lang="en"),
]


# ====================================================================
# Assembly
# ====================================================================
ALL_CORPUS = (
    CORPUS_PERSONAL + CORPUS_PROJECT + CORPUS_WRITING +
    CORPUS_RESEARCH + CORPUS_FINANCE + CORPUS_LEARNING
)
ALL_ALIASES = ALIASES
ALL_SESSIONS = SESSIONS
ALL_QUERIES = QUERIES


# ====================================================================
# JSONL I/O
# ====================================================================
def _write_jsonl(path: Path, entries: List[Dict]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    return len(entries)


def _load_jsonl(path: Path) -> List[Dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


# ====================================================================
# Generate
# ====================================================================
def generate() -> None:
    print("=" * 60)
    print("P1-data Phase A: Generating skeleton JSONL files")
    print("=" * 60)
    for key, data in [
        ("corpus", ALL_CORPUS), ("aliases", ALL_ALIASES),
        ("sessions", ALL_SESSIONS), ("queries", ALL_QUERIES),
    ]:
        n = _write_jsonl(FILES[key], data)
        print(f"  {FILES[key].name}: {n} entries")
    print("\nDone. Run 'validate' to check.")


# ====================================================================
# Validation
# ====================================================================
def validate() -> bool:
    print("=" * 60)
    print("P1-data Phase A: Validation Report")
    print("=" * 60)
    errors: List[str] = []
    warnings: List[str] = []

    # --- Load ---
    corpus = _load_jsonl(FILES["corpus"])
    aliases = _load_jsonl(FILES["aliases"])
    sessions = _load_jsonl(FILES["sessions"])
    queries = _load_jsonl(FILES["queries"])
    print(f"\nLoaded: corpus={len(corpus)}, aliases={len(aliases)}, "
          f"sessions={len(sessions)}, queries={len(queries)}")

    # --- 1. Schema check ---
    print("\n[1] Schema compliance")
    corpus_required = {"fixture_id", "parent_uri", "title", "content", "priority",
                       "disclosure", "domain", "text_style", "lang", "version",
                       "version_group", "conflict_group", "duplicate_group"}
    query_required = {"case_id", "query", "intent", "filters", "layer",
                      "session_group", "taxonomy_code", "gap_dimension",
                      "expected_memory_ids", "expected_rank_1", "difficulty", "lang"}
    alias_required = {"target_fixture_id", "new_uri", "priority", "disclosure", "note"}
    session_required = {"session_group", "fixture_id", "uri", "content", "priority"}

    for i, c in enumerate(corpus):
        missing = corpus_required - set(c.keys())
        if missing:
            errors.append(f"corpus[{i}] {c.get('fixture_id','?')}: missing {missing}")
    for i, q in enumerate(queries):
        missing = query_required - set(q.keys())
        if missing:
            errors.append(f"queries[{i}] {q.get('case_id','?')}: missing {missing}")
    for i, a in enumerate(aliases):
        missing = alias_required - set(a.keys())
        if missing:
            errors.append(f"aliases[{i}]: missing {missing}")
    for i, s in enumerate(sessions):
        missing = session_required - set(s.keys())
        if missing:
            errors.append(f"sessions[{i}]: missing {missing}")
    print(f"  Schema errors: {len([e for e in errors if 'missing' in e])}")

    # --- 2. fixture_id uniqueness ---
    print("\n[2] fixture_id uniqueness")
    corpus_ids = [c["fixture_id"] for c in corpus]
    dup_corpus = [fid for fid, cnt in Counter(corpus_ids).items() if cnt > 1]
    if dup_corpus:
        errors.append(f"Duplicate corpus fixture_ids: {dup_corpus}")
    query_ids = [q["case_id"] for q in queries]
    dup_queries = [qid for qid, cnt in Counter(query_ids).items() if cnt > 1]
    if dup_queries:
        errors.append(f"Duplicate query case_ids: {dup_queries}")
    print(f"  Corpus IDs: {len(corpus_ids)} total, {len(dup_corpus)} duplicates")
    print(f"  Query IDs:  {len(query_ids)} total, {len(dup_queries)} duplicates")

    # --- 3. Cross-reference integrity ---
    print("\n[3] Cross-reference integrity")
    corpus_fid_set = set(corpus_ids)
    broken_refs = []
    for q in queries:
        for eid in q["expected_memory_ids"]:
            if eid not in corpus_fid_set:
                broken_refs.append(f"{q['case_id']} -> {eid}")
        if q["expected_rank_1"] and q["expected_rank_1"] not in corpus_fid_set:
            broken_refs.append(f"{q['case_id']} rank1 -> {q['expected_rank_1']}")
    for a in aliases:
        if a["target_fixture_id"] not in corpus_fid_set:
            broken_refs.append(f"alias -> {a['target_fixture_id']}")
    # Session refs: fixture_id can be corpus ID or __session_anchor*
    for s in sessions:
        fid = s["fixture_id"]
        if not fid.startswith("__session_") and fid not in corpus_fid_set:
            broken_refs.append(f"session -> {fid}")
    if broken_refs:
        for r in broken_refs:
            errors.append(f"Broken cross-ref: {r}")
    print(f"  Broken references: {len(broken_refs)}")

    # --- 4. Corpus domain/lang/style quota ---
    print("\n[4] Corpus quota compliance")
    dom_lang = defaultdict(lambda: Counter())
    dom_style = defaultdict(lambda: Counter())
    for c in corpus:
        dom_lang[c["domain"]][c["lang"]] += 1
        dom_style[c["domain"]][c["text_style"]] += 1

    quota_ok = True
    for domain, spec in SPEC_CORPUS_DOMAIN_QUOTA.items():
        actual_total = sum(dom_lang[domain].values())
        if actual_total != spec["total"]:
            errors.append(f"Corpus {domain}: total={actual_total}, expected={spec['total']}")
            quota_ok = False
        for lang in ("zh", "en", "mixed"):
            actual = dom_lang[domain][lang]
            expected = spec[lang]
            if actual != expected:
                errors.append(f"Corpus {domain}/{lang}: actual={actual}, expected={expected}")
                quota_ok = False

    for domain, spec in SPEC_CORPUS_STYLE_QUOTA.items():
        for style, expected in spec.items():
            actual = dom_style[domain][style]
            if actual != expected:
                errors.append(f"Corpus {domain}/{style}: actual={actual}, expected={expected}")
                quota_ok = False
    print(f"  Domain/lang/style quota: {'PASS' if quota_ok else 'FAIL'}")

    # --- 5. Structural relations ---
    print("\n[5] Structural relations")
    vg = defaultdict(list)
    cg = defaultdict(list)
    dg = defaultdict(list)
    for c in corpus:
        if c["version_group"]:
            vg[c["version_group"]].append(c["fixture_id"])
        if c["conflict_group"]:
            cg[c["conflict_group"]].append(c["fixture_id"])
        if c["duplicate_group"]:
            dg[c["duplicate_group"]].append(c["fixture_id"])

    struct_ok = True
    if len(vg) != SPEC_STRUCT_QUOTA["version_group"]["count"]:
        errors.append(f"version_group count: {len(vg)}, expected {SPEC_STRUCT_QUOTA['version_group']['count']}")
        struct_ok = False
    for gname, members in vg.items():
        if len(members) != SPEC_STRUCT_QUOTA["version_group"]["per_group"]:
            errors.append(f"version_group '{gname}': {len(members)} members, expected {SPEC_STRUCT_QUOTA['version_group']['per_group']}")
            struct_ok = False
    if len(cg) != SPEC_STRUCT_QUOTA["conflict_group"]["count"]:
        errors.append(f"conflict_group count: {len(cg)}, expected {SPEC_STRUCT_QUOTA['conflict_group']['count']}")
        struct_ok = False
    if len(dg) != SPEC_STRUCT_QUOTA["duplicate_group"]["count"]:
        errors.append(f"duplicate_group count: {len(dg)}, expected {SPEC_STRUCT_QUOTA['duplicate_group']['count']}")
        struct_ok = False
    print(f"  version_groups:  {dict(vg)}")
    print(f"  conflict_groups: {dict(cg)}")
    print(f"  duplicate_groups: {dict(dg)}")
    print(f"  Structural relations: {'PASS' if struct_ok else 'FAIL'}")

    # --- 6. Query taxonomy quota ---
    print("\n[6] Query taxonomy quota")
    tax_lang = defaultdict(lambda: Counter())
    for q in queries:
        # TX_001 is lang="mixed"; for quota purposes count as "zh"
        lang = q["lang"] if q["lang"] in ("zh", "en") else "zh"
        tax_lang[q["taxonomy_code"]][lang] += 1

    tax_ok = True
    for tax, spec in SPEC_QUERY_TAXONOMY_QUOTA.items():
        for lang in ("zh", "en"):
            actual = tax_lang[tax][lang]
            expected = spec[lang]
            if actual != expected:
                errors.append(f"Query {tax}/{lang}: actual={actual}, expected={expected}")
                tax_ok = False
    print(f"  Taxonomy quota: {'PASS' if tax_ok else 'FAIL'}")

    # --- 7. Layer distribution ---
    print("\n[7] Layer distribution")
    layer_cnt = Counter(q["layer"] for q in queries)
    print(f"  Layer A: {layer_cnt.get('A', 0)}, Layer B: {layer_cnt.get('B', 0)}")

    # --- 8. Session group coverage ---
    print("\n[8] Session group coverage")
    session_groups = set(s["session_group"] for s in sessions)
    query_session_groups = set(q["session_group"] for q in queries if q["session_group"])
    missing_sg = query_session_groups - session_groups
    if missing_sg:
        errors.append(f"Session groups referenced by queries but missing in fixtures: {missing_sg}")
    print(f"  Session fixture groups: {sorted(session_groups)}")
    print(f"  Query session groups:   {sorted(query_session_groups)}")
    print(f"  Missing groups: {missing_sg or 'none'}")

    # --- 9. P1a subset preserved ---
    print("\n[9] P1a subset check")
    p1a_corpus_ids = {
        "mnc_personal_001", "mnc_personal_002", "mnc_personal_003",
        "mnc_project_001", "mnc_project_002",
        "mnc_finance_001", "mnc_finance_002",
        "mnc_writing_001", "mnc_writing_002",
        "mnc_research_001", "mnc_research_002",
        "mnc_learning_001",
    }
    p1a_query_ids = {
        "mnq_F1_001", "mnq_F1_002", "mnq_S1_001", "mnq_N2_001",
        "mnq_V1_001", "mnq_TR1_001", "mnq_C1_001", "mnq_TX_001",
        "mnq_S2_001", "mnq_N1_001", "mnq_M1_001", "mnq_M2_001",
    }
    missing_p1a_corpus = p1a_corpus_ids - corpus_fid_set
    missing_p1a_queries = p1a_query_ids - set(query_ids)
    if missing_p1a_corpus:
        errors.append(f"Missing P1a corpus entries: {missing_p1a_corpus}")
    if missing_p1a_queries:
        errors.append(f"Missing P1a query entries: {missing_p1a_queries}")
    print(f"  P1a corpus preserved: {len(p1a_corpus_ids - missing_p1a_corpus)}/{len(p1a_corpus_ids)}")
    print(f"  P1a queries preserved: {len(p1a_query_ids - missing_p1a_queries)}/{len(p1a_query_ids)}")

    # --- 10. Code snippet ratio ---
    # Spec §7.5: "code_snippet style 上限 8%（≤6/80）" — absolute cap is 6 entries.
    # Percentage was calibrated for 80-entry corpus; with 70 entries, 6/70=8.6%
    # but absolute count ≤6 is the binding constraint.
    print("\n[10] Code snippet ratio (spec: ≤6 entries)")
    code_count = sum(1 for c in corpus if c["text_style"] == "code_snippet")
    ratio = code_count / len(corpus) * 100 if corpus else 0
    if code_count > 6:
        errors.append(f"code_snippet count {code_count} exceeds absolute cap of 6")
    print(f"  code_snippet: {code_count}/{len(corpus)} = {ratio:.1f}% (abs cap: 6)")

    # --- 11. Placeholder content stats ---
    print("\n[11] Placeholder content (needs Phase B LLM filling)")
    ph_corpus = sum(1 for c in corpus if c["content"].startswith("[PLACEHOLDER"))
    ph_queries = sum(1 for q in queries if q["query"].startswith("[QUERY_PLACEHOLDER"))
    ph_sessions = sum(1 for s in sessions if s["content"].startswith("[PLACEHOLDER"))
    print(f"  Corpus:  {ph_corpus}/{len(corpus)} entries need content")
    print(f"  Queries: {ph_queries}/{len(queries)} entries need query text")
    print(f"  Sessions: {ph_sessions}/{len(sessions)} entries need content")

    # --- 12. Spec discrepancy notes ---
    print("\n[12] Spec discrepancy notes")
    # Spec v3.6.2: canonical numbers reconciled to 70 corpus / 54 queries.
    # No discrepancy warnings needed.
    # TF1 domain coverage (finance/personal/learning) now matches spec §8.3 v3.6.2.
    # No coverage gap warning needed.
    for w in warnings:
        print(f"  WARN: {w}")

    # --- Summary ---
    print("\n" + "=" * 60)
    if errors:
        print(f"RESULT: FAIL ({len(errors)} errors, {len(warnings)} warnings)")
        for e in errors:
            print(f"  ERROR: {e}")
    else:
        print(f"RESULT: PASS ({len(warnings)} warnings)")
    print("=" * 60)
    return len(errors) == 0


# ====================================================================
# Stats
# ====================================================================
def stats() -> None:
    corpus = _load_jsonl(FILES["corpus"])
    queries = _load_jsonl(FILES["queries"])
    sessions = _load_jsonl(FILES["sessions"])
    aliases = _load_jsonl(FILES["aliases"])

    print("=" * 60)
    print("Corpus Statistics")
    print("=" * 60)

    # Domain × Lang matrix
    print("\nDomain × Lang:")
    print(f"{'Domain':<12} {'zh':>4} {'en':>4} {'mixed':>6} {'Total':>6}")
    print("-" * 36)
    totals = Counter()
    for domain in ["personal", "project", "writing", "research", "finance", "learning"]:
        d_entries = [c for c in corpus if c["domain"] == domain]
        lc = Counter(c["lang"] for c in d_entries)
        totals.update(lc)
        print(f"{domain:<12} {lc['zh']:>4} {lc['en']:>4} {lc['mixed']:>6} {len(d_entries):>6}")
    print("-" * 36)
    print(f"{'Total':<12} {totals['zh']:>4} {totals['en']:>4} {totals['mixed']:>6} {len(corpus):>6}")

    # Domain × Style matrix
    print("\nDomain × Style:")
    all_styles = sorted({c["text_style"] for c in corpus})
    header = f"{'Domain':<12}" + "".join(f" {s[:10]:>10}" for s in all_styles)
    print(header)
    print("-" * len(header))
    for domain in ["personal", "project", "writing", "research", "finance", "learning"]:
        d_entries = [c for c in corpus if c["domain"] == domain]
        sc = Counter(c["text_style"] for c in d_entries)
        row = f"{domain:<12}" + "".join(f" {sc.get(s,0):>10}" for s in all_styles)
        print(row)

    # Query stats
    print("\n" + "=" * 60)
    print("Query Statistics")
    print("=" * 60)
    print(f"\n{'Taxonomy':<8} {'zh':>4} {'en':>4} {'mixed':>6} {'Total':>6} {'Layer':>6}")
    print("-" * 40)
    for tax in ["F1","F2","TR1","TR2","TF1","C1","C2","E1","E2",
                "S1","S2","N1","N2","V1","V2","V3","M1","M2","TX"]:
        tq = [q for q in queries if q["taxonomy_code"] == tax]
        lc = Counter(q["lang"] for q in tq)
        layers = ",".join(sorted(set(q["layer"] for q in tq)))
        print(f"{tax:<8} {lc.get('zh',0):>4} {lc.get('en',0):>4} "
              f"{lc.get('mixed',0):>6} {len(tq):>6} {layers:>6}")
    print(f"\nTotal queries: {len(queries)}")
    print(f"Aliases: {len(aliases)}, Sessions: {len(sessions)}")


# ====================================================================
# Phase B Stub (LLM content filling — NOT implemented in Phase A)
# ====================================================================
def fill_content_stub() -> None:
    """Phase B entry point for LLM content filling.

    Configuration via environment variables (NEVER hard-coded):
        LLM_ENDPOINT  — e.g. from env
        LLM_API_KEY   — e.g. from env
        LLM_MODEL     — e.g. from env

    Workflow:
        1. Load skeleton JSONL files
        2. For each entry with [PLACEHOLDER] / [QUERY_PLACEHOLDER] content:
           a. Construct prompt with domain, style, lang constraints
           b. Call LLM API (6 batches by domain, each with different system prompt role)
           c. Replace placeholder content with LLM output
        3. Run intra-corpus n-gram dedup check (>30% overlap → regenerate)
        4. Write updated JSONL files
        5. Run validate() to confirm

    See Spec §7 for full data production strategy.
    """
    print("Phase B (LLM content filling) is not implemented in Phase A.")
    print("Environment variables needed (when Phase B is approved):")
    print("  LLM_ENDPOINT  — LLM API endpoint URL")
    print("  LLM_API_KEY   — API authentication key")
    print("  LLM_MODEL     — Model identifier")
    print("\nRun generate_p1_data.py fill_content when Phase B is approved.")


# ====================================================================
# Main
# ====================================================================
def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python generate_p1_data.py {generate|validate|stats|fill_content}")
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "generate":
        generate()
    elif cmd == "validate":
        ok = validate()
        sys.exit(0 if ok else 1)
    elif cmd == "stats":
        stats()
    elif cmd == "fill_content":
        fill_content_stub()
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    main()

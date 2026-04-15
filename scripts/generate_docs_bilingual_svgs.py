#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "images"

WIDTH = 3840
HEIGHT = 2160

BG = "#f8f1e6"
PANEL = "#fffaf2"
PANEL_2 = "#f4ecdf"
INK = "#2f2a24"
MUTED = "#7f6f5d"
ACCENT = "#c59b57"
ACCENT_DEEP = "#8a6738"
LINE = "#d8ccb7"
GREEN = "#2f7d62"
AMBER = "#a06b23"


def wrap_lines(text: str, width: int = 28) -> list[str]:
    words = text.split(" ")
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = word if not current else f"{current} {word}"
        if len(candidate) <= width:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def svg_header(title: str, desc: str) -> str:
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" viewBox="0 0 {WIDTH} {HEIGHT}" role="img" aria-labelledby="title desc">
  <title id="title">{title}</title>
  <desc id="desc">{desc}</desc>
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="#fffdf9"/>
      <stop offset="100%" stop-color="#efe2cf"/>
    </linearGradient>
    <filter id="shadow" x="-20%" y="-20%" width="140%" height="140%">
      <feDropShadow dx="0" dy="20" stdDeviation="24" flood-color="#8f6a34" flood-opacity="0.12"/>
    </filter>
  </defs>
  <style>
    .bg {{ fill: url(#bg); }}
    .panel {{ fill: {PANEL}; stroke: {LINE}; stroke-width: 3; filter: url(#shadow); }}
    .panel2 {{ fill: {PANEL_2}; stroke: {LINE}; stroke-width: 3; }}
    .titleZh {{ font: 700 96px 'PingFang SC','Microsoft YaHei',sans-serif; fill: {INK}; }}
    .titleEn {{ font: 600 42px 'Avenir Next','Segoe UI',sans-serif; fill: {MUTED}; letter-spacing: 4px; }}
    .section {{ font: 700 48px 'Avenir Next','PingFang SC',sans-serif; fill: {ACCENT_DEEP}; }}
    .cardTitle {{ font: 700 42px 'PingFang SC','Microsoft YaHei',sans-serif; fill: {INK}; }}
    .cardSub {{ font: 600 23px 'Avenir Next','Segoe UI',sans-serif; fill: {MUTED}; letter-spacing: 1.2px; }}
    .body {{ font: 500 28px 'Avenir Next','PingFang SC',sans-serif; fill: {INK}; }}
    .bodyMuted {{ font: 500 24px 'Avenir Next','PingFang SC',sans-serif; fill: {MUTED}; }}
    .metric {{ font: 700 38px 'Avenir Next','PingFang SC',sans-serif; fill: {GREEN}; }}
    .warn {{ fill: {AMBER}; }}
    .accent {{ fill: {ACCENT}; }}
    .line {{ stroke: {ACCENT}; stroke-width: 8; stroke-linecap: round; fill: none; }}
    .dash {{ stroke-dasharray: 16 12; }}
  </style>
  <rect class="bg" x="0" y="0" width="{WIDTH}" height="{HEIGHT}"/>
  <circle cx="360" cy="280" r="240" fill="rgba(212,175,55,0.10)"/>
  <circle cx="3380" cy="300" r="270" fill="rgba(184,150,86,0.10)"/>
  <circle cx="3240" cy="1820" r="280" fill="rgba(212,175,55,0.08)"/>
"""


def svg_footer() -> str:
    return "</svg>\n"


def two_line_card(x: int, y: int, w: int, h: int, title_zh: str, title_en: str, body_zh: str, body_en: str) -> str:
    body_y = y + 180
    return f"""
  <rect class="panel2" x="{x}" y="{y}" rx="30" ry="30" width="{w}" height="{h}"/>
  <text class="cardTitle" x="{x+44}" y="{y+84}">{title_zh}</text>
  <text class="cardSub" x="{x+46}" y="{y+124}">{title_en}</text>
  <text class="body" x="{x+44}" y="{body_y}">{body_zh}</text>
  <text class="bodyMuted" x="{x+46}" y="{body_y+44}">{body_en}</text>
"""


def write_system_architecture() -> None:
    parts = [svg_header("系统架构 / System Architecture", "Bilingual architecture overview for Memory Palace.")]
    parts.append(f"""
  <text class="titleZh" x="180" y="220">系统架构</text>
  <text class="titleEn" x="182" y="286">System Architecture</text>
  <rect class="panel" x="180" y="360" rx="44" ry="44" width="3480" height="1450"/>
  <text class="section" x="260" y="450">OpenClaw host → plugin → skills → MCP → backend → review / maintenance</text>
""")
    cards = [
        (260, 560, 620, 340, "OpenClaw 宿主", "OpenClaw Host", "active memory slot / hooks", "Active slot and lifecycle hooks"),
        (1040, 560, 620, 340, "插件主链", "Plugin Mainline", "默认 recall / capture / visual harvest", "Default recall, capture, visual harvest"),
        (1820, 560, 620, 340, "Bundled Skills", "Bundled Skills", "显式升级到 memory_get / memory_store_visual", "Escalate to explicit tool usage"),
        (2600, 560, 780, 340, "MCP + Backend", "MCP + Backend", "11 个工具、SQLite、index worker、review/maintenance", "11 tools, SQLite, index worker, review and maintenance"),
        (580, 1120, 820, 340, "审查与回滚", "Review and Rollback", "snapshot / diff / reject / integrate", "Snapshot, diff, reject, integrate"),
        (1560, 1120, 820, 340, "观测与健康", "Observability and Health", "verify / doctor / smoke / observability", "Verify, doctor, smoke, observability"),
        (2540, 1120, 820, 340, "部署阶梯", "Deployment Ladder", "B 默认起步 → C 推荐目标 → D 全量高级面", "B bootstrap → C recommended → D full suite"),
    ]
    for c in cards:
        parts.append(two_line_card(*c))
    parts.append("""
  <path class="line" d="M880 730 H1040"/>
  <path class="line" d="M1660 730 H1820"/>
  <path class="line" d="M2440 730 H2600"/>
  <path class="line dash" d="M1290 900 V1120"/>
  <path class="line dash" d="M2210 900 V1120"/>
  <path class="line dash" d="M3110 900 V1120"/>
""")
    parts.append(svg_footer())
    (OUT / "system_architecture_bilingual.svg").write_text("".join(parts), encoding="utf-8")


def write_onboarding_flow() -> None:
    parts = [svg_header("快速上手流程 / Quick Start Flow", "Bilingual onboarding flow for Memory Palace.")]
    parts.append(f"""
  <text class="titleZh" x="180" y="220">快速上手流程</text>
  <text class="titleEn" x="182" y="286">Quick Start Flow</text>
  <rect class="panel" x="180" y="360" rx="44" ry="44" width="3480" height="1320"/>
  <text class="section" x="260" y="450">从 Profile B 跑通，再按 provider readiness 升级到 C / D</text>
""")
    steps = [
        (260, 620, 620, 300, "1. 准备环境", "Prepare environment", "Python 3.10-3.14 / OpenClaw / Node", "Python 3.10-3.14, OpenClaw, Node"),
        (1040, 620, 620, 300, "2. setup --profile b", "setup --profile b", "先走最稳的 B 档，hash + no reranker", "Start with safe B: hash + no reranker"),
        (1820, 620, 620, 300, "3. verify / doctor / smoke", "verify / doctor / smoke", "确认 stdio / recall / search / read probe", "Confirm stdio, recall, search, read probe"),
        (2600, 620, 780, 300, "4. 升级到 C / D", "Upgrade to C / D", "provider-probe → apply → index --wait", "Provider probe, apply, then reindex"),
    ]
    for c in steps:
        parts.append(two_line_card(*c))
    parts.append("""
  <path class="line" d="M880 770 H1040"/>
  <path class="line" d="M1660 770 H1820"/>
  <path class="line" d="M2440 770 H2600"/>
""")
    parts.append(svg_footer())
    (OUT / "onboarding_flow_bilingual.svg").write_text("".join(parts), encoding="utf-8")


def write_skill_vs_mcp() -> None:
    parts = [svg_header("Skill 与 MCP / Skill vs MCP", "Bilingual skill vs MCP diagram.")]
    parts.append(f"""
  <text class="titleZh" x="180" y="220">Skill 与 MCP 的关系</text>
  <text class="titleEn" x="182" y="286">How Skill and MCP Work Together</text>
  <rect class="panel" x="180" y="360" rx="44" ry="44" width="3480" height="1280"/>
  <text class="section" x="260" y="450">Skill 负责“什么时候做”，MCP 负责“真正怎么做”</text>
""")
    parts.append(two_line_card(360, 640, 1240, 460, "Skill = 调度规则", "Skill = orchestration rules", "告诉模型什么时候该显式介入 durable recall / review / visual memory", "Tells the model when to escalate into durable recall, review, or visual memory"))
    parts.append(two_line_card(2240, 640, 1240, 460, "MCP = 真正执行面", "MCP = execution surface", "提供 read / search / create / update / review / runtime 这些真实工具", "Provides the real tools: read, search, create, update, review, runtime"))
    parts.append("""
  <path class="line" d="M1600 870 H2240"/>
  <circle cx="1920" cy="870" r="34" fill="#fffaf2" stroke="#c59b57" stroke-width="6"/>
  <text class="section" x="1868" y="888">→</text>
""")
    parts.append(svg_footer())
    (OUT / "skill_vs_mcp_bilingual.svg").write_text("".join(parts), encoding="utf-8")


def write_write_review_sequence() -> None:
    parts = [svg_header("写入与审查时序 / Write and Review Sequence", "Bilingual write-review sequence diagram.")]
    parts.append(f"""
  <text class="titleZh" x="180" y="220">写入与审查时序</text>
  <text class="titleEn" x="182" y="286">Write and Review Sequence</text>
  <rect class="panel" x="180" y="360" rx="44" ry="44" width="3480" height="1380"/>
  <text class="section" x="260" y="450">write_guard → snapshot → durable write → review / rollback</text>
""")
    stages = [
        (260, 620, 620, 300, "1. write_guard", "write_guard", "语义匹配 / 关键词 / 可选 LLM", "Semantic, keyword, optional LLM"),
        (1040, 620, 620, 300, "2. snapshot", "snapshot", "写前快照与 diff 基线", "Pre-write snapshot and diff baseline"),
        (1820, 620, 620, 300, "3. durable write", "durable write", "lane 串行化、版本变更、索引入队", "Lane serialization, versioning, index enqueue"),
        (2600, 620, 780, 300, "4. review / rollback", "review / rollback", "Review Ledger, reject, integrate, rollback", "Review ledger, reject, integrate, rollback"),
    ]
    for c in stages:
        parts.append(two_line_card(*c))
    parts.append("""
  <path class="line" d="M880 770 H1040"/>
  <path class="line" d="M1660 770 H1820"/>
  <path class="line" d="M2440 770 H2600"/>
""")
    parts.append(svg_footer())
    (OUT / "write_review_sequence_bilingual.svg").write_text("".join(parts), encoding="utf-8")


def write_benchmark_snapshot() -> None:
    parts = [svg_header("Benchmark 摘要 / Benchmark Snapshot", "Bilingual benchmark snapshot based on real evaluation numbers.")]
    parts.append(f"""
  <text class="titleZh" x="180" y="220">Benchmark 摘要</text>
  <text class="titleEn" x="182" y="286">Benchmark Snapshot</text>
  <rect class="panel" x="180" y="360" rx="44" ry="44" width="3480" height="1320"/>
  <text class="section" x="260" y="450">只使用 docs/EVALUATION.md 当前已成立的公开数字</text>
""")
    cards = [
        (280, 620, 980, 860, "Intent 准确率", "Intent Accuracy", "0.850 → 0.945", "keyword_scoring_v2 → +LLM"),
        (1430, 620, 980, 860, "Write Guard Recall", "Write Guard Recall", "0.839 → 1.000", "heuristic → +LLM"),
        (2580, 620, 980, 860, "Gist / 矛盾检测", "Gist / Contradiction", "ROUGE-L 0.976 · 0.950", "Compact gist and contradiction accuracy"),
    ]
    for x, y, w, h, tzh, ten, metric, note in cards:
        parts.append(f"""
  <rect class="panel2" x="{x}" y="{y}" rx="30" ry="30" width="{w}" height="{h}"/>
  <text class="cardTitle" x="{x+40}" y="{y+86}">{tzh}</text>
  <text class="cardSub" x="{x+42}" y="{y+126}">{ten}</text>
  <text class="metric" x="{x+40}" y="{y+260}">{metric}</text>
  <text class="bodyMuted" x="{x+42}" y="{y+330}">{note}</text>
""")
    parts.append(svg_footer())
    (OUT / "benchmark_snapshot_bilingual.svg").write_text("".join(parts), encoding="utf-8")


def write_security_checklist() -> None:
    parts = [svg_header("安全检查清单 / Security Checklist", "Bilingual security checklist based on current docs and code.")]
    parts.append(f"""
  <text class="titleZh" x="180" y="220">安全检查清单</text>
  <text class="titleEn" x="182" y="286">Security Checklist</text>
  <rect class="panel" x="180" y="360" rx="44" ry="44" width="3480" height="1420"/>
  <text class="section" x="260" y="450">分享前重点：fail-closed、loopback-only bootstrap、受保护接口、无密钥泄露</text>
""")
    checks = [
        ("只提交 .env.example", "Commit .env.example only"),
        ("保护 /maintenance /review /browse /sse", "Protect maintenance, review, browse, and SSE"),
        ("loopback-only bootstrap", "Loopback-only bootstrap"),
        ("Docker 代理转发，不把 key 写进前端", "Proxy keys server-side in Docker"),
        ("预发布脚本通过", "pre_publish_check.sh passes"),
    ]
    y = 620
    for cn, en in checks:
        parts.append(f"""
  <rect class="panel2" x="320" y="{y}" rx="24" ry="24" width="3200" height="160"/>
  <circle cx="420" cy="{y+80}" r="18" fill="{GREEN}"/>
  <text class="body" x="470" y="{y+76}">{cn}</text>
  <text class="bodyMuted" x="472" y="{y+118}">{en}</text>
""")
        y += 200
    parts.append(svg_footer())
    (OUT / "security_checklist_bilingual.svg").write_text("".join(parts), encoding="utf-8")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    write_system_architecture()
    write_onboarding_flow()
    write_skill_vs_mcp()
    write_write_review_sequence()
    write_benchmark_snapshot()
    write_security_checklist()
    print("generated")


if __name__ == "__main__":
    main()

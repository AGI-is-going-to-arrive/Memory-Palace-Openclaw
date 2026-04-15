#!/usr/bin/env python3
"""Phase B1: Fill corpus and session placeholder content via LLM.

Usage:
    # Fill one domain at a time:
    python fill_p1b1_content.py corpus personal
    python fill_p1b1_content.py corpus project
    ...
    python fill_p1b1_content.py sessions

    # Fill all corpus domains + sessions in one run:
    python fill_p1b1_content.py all

LLM config via environment variables (never hard-coded):
    LLM_ENDPOINT, LLM_API_KEY, LLM_MODEL
"""

import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
FIXTURES_DIR = SCRIPT_DIR.parent / "fixtures"
CORPUS_FILE = FIXTURES_DIR / "memory_native_corpus.jsonl"
SESSION_FILE = FIXTURES_DIR / "memory_native_session_fixture.jsonl"

LLM_ENDPOINT = os.environ.get("LLM_ENDPOINT", "")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "")

DOMAIN_ORDER = ["personal", "project", "writing", "research", "finance", "learning"]

DOMAIN_PERSONAS = {
    "personal": "你是一个用 AI 助手管理日常生活的上班族。你把个人偏好、家人信息、日常习惯记录在记忆系统中。",
    "project": "你是一个软件团队的技术负责人。你把架构决策、技术栈、迁移计划、客户需求记录在记忆系统中。",
    "writing": "你是一个自由撰稿人兼小说作者。你把写作风格规则、章节大纲、报告草稿、翻译术语记录在记忆系统中。",
    "research": "你是一个硕士研究生，在做文献综述。你把论文笔记、竞品分析、市场趋势记录在记忆系统中。",
    "finance": "你是一个个人投资者，管理自己的股票组合。你把投资规则、持仓清单、调仓决策、风险评估记录在记忆系统中。",
    "learning": "你是一个在自学多项新技能的终身学习者。你把编程笔记、语言学习进度、烹饪技巧、钢琴练习记录在记忆系统中。",
}

STYLE_INSTRUCTIONS = {
    "preference": "写一条个人偏好/规则声明，简洁直接。",
    "agent_note": "写一条备忘笔记，包含关键要点和上下文信息。",
    "bullet_list": "写一个条目清单（3-6项），用 - 开头，简洁具体。",
    "summary": "写一段简洁的总结/摘要段落。",
    "decision_log": "写一条决策记录，含日期、决策、原因、权衡。",
    "code_snippet": "写技术配置或代码片段附简短说明。不用 markdown 代码块包裹。",
    "structured_rule": "写结构化规则/约束，含具体数值和条件。",
    "reference_link": "写参考文献/链接列表，每条附简短描述。",
}

LANG_INSTRUCTIONS = {
    "zh": "全部使用中文。",
    "en": "Write entirely in English.",
    "mixed": "中英混合自然切换（技术术语可用英文，其余用中文，或反之）。",
}


def _call_llm(system_prompt: str, user_prompt: str, retries: int = 2) -> str:
    payload = json.dumps({
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.75,
        "max_tokens": 300,
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
            content = result["choices"][0]["message"]["content"].strip()
            # Strip markdown wrapping if present
            content = re.sub(r'^```\w*\n?', '', content)
            content = re.sub(r'\n?```$', '', content)
            # Strip leading labels like "内容：" or "Content:"
            content = re.sub(r'^(?:内容|Content|记录|Note|Rule)[：:]\s*', '', content)
            return content.strip()
        except Exception as e:
            if attempt < retries:
                time.sleep(1)
            else:
                raise RuntimeError(f"LLM call failed after {retries+1} attempts: {e}")


def _extract_hint(placeholder: str) -> str:
    m = re.match(r'\[(?:PLACEHOLDER|QUERY_PLACEHOLDER)[^\]]*\]\s*(.*)', placeholder)
    return m.group(1).strip() if m else placeholder


def _is_placeholder(content: str) -> bool:
    return content.startswith("[PLACEHOLDER")


def _load_jsonl(path: Path) -> list:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _write_jsonl(path: Path, entries: list) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


def _group_context(entry: dict) -> str:
    """Add group context hint for version/conflict/duplicate entries."""
    parts = []
    if entry.get("version_group") and entry.get("version"):
        v = entry["version"]
        parts.append(f"这是第{v}版（共3版），" + (
            "最初的保守版本。" if v == 1 else
            "中间调整版本，比v1有所放宽。" if v == 2 else
            "最新版本，反映当前状态。"
        ))
    if entry.get("conflict_group"):
        parts.append("注意：记忆系统中存在与此矛盾的另一条记录，你写的内容应表达一个明确的立场。")
    if entry.get("duplicate_group"):
        parts.append("这是对同一主题的重复记录，内容应与同组另一条相似但有修订或侧重差异。")
    return " ".join(parts)


def fill_corpus_domain(domain: str) -> int:
    """Fill placeholder corpus entries for one domain. Returns count filled."""
    entries = _load_jsonl(CORPUS_FILE)
    filled = 0

    persona = DOMAIN_PERSONAS.get(domain, "你是一个AI助手的用户。")

    for entry in entries:
        if entry["domain"] != domain or not _is_placeholder(entry["content"]):
            continue

        hint = _extract_hint(entry["content"])
        style = entry["text_style"]
        lang = entry["lang"]
        group_ctx = _group_context(entry)

        user_prompt = (
            f"请为我的记忆库生成一条记录。\n\n"
            f"内容提示：{hint}\n"
            f"写作风格：{STYLE_INSTRUCTIONS.get(style, '')}\n"
            f"语言要求：{LANG_INSTRUCTIONS.get(lang, '')}\n"
            + (f"上下文：{group_ctx}\n" if group_ctx else "")
            + "\n要求：\n"
            f"- 长度 60-200 字符\n"
            f"- 真实、具体，像真实用户的记忆条目\n"
            f"- 直接输出内容，不加前缀、引号或标签"
        )

        content = _call_llm(persona, user_prompt)
        entry["content"] = content
        filled += 1
        print(f"  [{filled}] {entry['fixture_id']} ({style}/{lang}): {content[:60]}...")

    _write_jsonl(CORPUS_FILE, entries)
    return filled


def fill_sessions() -> int:
    """Fill placeholder session entries. Returns count filled."""
    entries = _load_jsonl(SESSION_FILE)
    filled = 0
    persona = "你是一个AI助手，正在模拟用户与AI对话中刚讨论过的内容片段。"

    for entry in entries:
        if not _is_placeholder(entry["content"]):
            continue

        hint = _extract_hint(entry["content"])
        user_prompt = (
            f"请生成一条对话上下文片段。\n\n"
            f"内容提示：{hint}\n\n"
            f"要求：\n"
            f"- 长度 30-100 字符\n"
            f"- 像用户刚才在对话中提到的片段\n"
            f"- 直接输出内容，不加前缀"
        )

        content = _call_llm(persona, user_prompt)
        entry["content"] = content
        filled += 1
        print(f"  [session {filled}] {entry['fixture_id']}: {content[:60]}...")

    _write_jsonl(SESSION_FILE, entries)
    return filled


def run_validator() -> bool:
    """Run the generate_p1_data.py validator."""
    import subprocess
    result = subprocess.run(
        [sys.executable, str(SCRIPT_DIR / "generate_p1_data.py"), "validate"],
        capture_output=True, text=True,
    )
    # Print last 5 lines (summary)
    lines = result.stdout.strip().split("\n")
    for line in lines[-5:]:
        print(f"    {line}")
    return result.returncode == 0


def main():
    if not all([LLM_ENDPOINT, LLM_API_KEY, LLM_MODEL]):
        print("ERROR: Set LLM_ENDPOINT, LLM_API_KEY, LLM_MODEL env vars.")
        sys.exit(1)

    if len(sys.argv) < 2:
        print("Usage: fill_p1b1_content.py {all | corpus <domain> | sessions}")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "corpus" and len(sys.argv) >= 3:
        domain = sys.argv[2]
        print(f"\n=== Filling corpus: {domain} ===")
        n = fill_corpus_domain(domain)
        print(f"\nFilled {n} entries. Validating...")
        run_validator()

    elif cmd == "sessions":
        print("\n=== Filling sessions ===")
        n = fill_sessions()
        print(f"\nFilled {n} entries. Validating...")
        run_validator()

    elif cmd == "all":
        total = 0
        for domain in DOMAIN_ORDER:
            print(f"\n{'='*50}")
            print(f"=== Batch: {domain} ===")
            print(f"{'='*50}")
            n = fill_corpus_domain(domain)
            total += n
            print(f"\n  Filled {n} entries. Validating...")
            run_validator()

        print(f"\n{'='*50}")
        print(f"=== Batch: sessions ===")
        print(f"{'='*50}")
        n = fill_sessions()
        total += n
        print(f"\n  Filled {n} entries. Validating...")
        run_validator()

        print(f"\n{'='*50}")
        print(f"Phase B1 complete. Total filled: {total}")
        print(f"{'='*50}")

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    main()

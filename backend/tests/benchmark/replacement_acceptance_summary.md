# Memory Palace — Replacement Acceptance Summary

> **结论：在当前 replacement acceptance scope、当前目标环境与当前 `OpenClaw 2026.4.5` 宿主验证条件下，已证明 memory-palace plugin + skills 可以替代 OpenClaw 默认 memory system 的基础职责。**

Generated: 2026-04-10

本次 2026-04-10 复跑补充确认了三件事：

- Layer A `scripts/test_replacement_acceptance_e2e.py` 在当前仓库代码下已恢复到 `7/7 PASS`
- `phase45` 的 Profile C / D 真实链路也已再次转绿
- 这轮修复只改了仓库内部实现与测试 harness，没有改 OpenClaw 宿主配置

因此，本摘要里的“可替代基础职责”结论，当前仍成立。

---

## Phase 回顾

### Phase 1–2: Retrieval Benchmark

- 验证了 memory-palace 在 Profile A/B/C/D 下的检索质量指标 (HR@k, MRR)
- 确认了 keyword / hybrid / semantic 三种检索模式均可工作
- 建立了 CI gate 和 black-box e2e benchmark 基线

### Phase 3: Host-Level Replacement Acceptance

验证 memory-palace plugin + skills 是否已具备替代 OpenClaw 默认 memory system 的系统级证据。

#### Layer A: CLI/Host Acceptance — 7/7 PASS

| 场景 | Profile | 耗时 | 覆盖口径 |
|------|---------|------|---------|
| S1: 写入后搜索可召回 | B | 39ms | CRUD + recall |
| S2: 更新后召回最新版本 | B | 52ms | CRUD |
| S3: 跨进程重启后持久化 | B | 2987ms | 默认链路 + recall (PID 验证) |
| S4: 重建索引后 hybrid 召回 | C | 2346ms | compact/index |
| S5: Reranker 故障降级 | C→降级 | 2784ms | 失败回退 (degraded=True) |
| S6: compact_context via MCP stdio | B | 1341ms | compact/index (MCP 直连) |
| S7: Profile D: Write Guard + Hybrid Search | D | 12549ms | write_guard + hybrid search |

- 每个场景使用隔离 temp workspace + 独立 SQLite DB
- Profile C/D 使用外部 embedding + reranker；本轮本地复跑还补了 fallback-compatible chat 路径
- S5 确认 reranker 故障时系统返回 `degraded=True` + `reranker_request_failed`，结果仍正常返回
- 这轮额外修掉了 2 个仓库内部 blocker：
  - acceptance backend 的日志管道阻塞导致 S2 偶发超时
  - `phase45` / smart extraction / doctor diagnostics 的内部口径漂移

#### Layer B: WebUI/Playwright Acceptance — current-host 6/6 PASS；isolated 6/6 PASS

| 验证点 | 结果 | 证据 |
|--------|------|------|
| V1: Plugin 在 Skills 可见 | PASS | current-host 与 isolated 都能看到 `memory-palace` 条目 |
| V2: Chat 中 write + recall | PASS | current-host 与 isolated 这轮都能拿到唯一 marker 的成功 recall；部分场景以 CLI strong evidence 补强 |
| V3: 系统集成证据 | PASS | current-host 与 isolated 都能拿到 recall/path/CLI 侧强证据 |
| V4: blocked -> confirm -> force -> recall | PASS | current-host 与 isolated `Profile B/C/D` 都通过；确认前不单独落盘，确认后可 recall |
| V5: 中文极短确认 `记住了。` | PASS | current-host 与 isolated `Profile B/C/D` 都通过 |
| V6: 英文极短确认 `Stored.` | PASS | current-host 与 isolated `Profile B/C/D` 都通过；英文确认已从间接覆盖升级为独立强可见性 E2E |

**Layer B 结果总表**

| 路径 | Profile | 结果 | 报告 |
|---|---|---|---|
| current-host | B | 6/6 PASS | `.tmp/replacement-acceptance/current-host-b/webui_report.json` |
| isolated | B | 6/6 PASS | `.tmp/replacement-acceptance/b/webui_report.json` |
| isolated | C | 6/6 PASS | `.tmp/replacement-acceptance/c/webui_report.json` |
| isolated | D | 6/6 PASS | `.tmp/replacement-acceptance/d/webui_report.json` |

- `current-host` 不再隐式依赖隔离场景里的 `alpha`，会优先走 `main`
- `V4` 已从普通 recall 扩成真实聊天链：`write_guard_blocked -> 用户确认 -> force 写入 -> recall`
- `V5 / V6` 现在是独立验证点，不再只是中文/英文确认的间接覆盖
- Layer B 这轮分别在 current-host 与 `ACCEPTANCE_FORCE_ISOLATED=true OPENCLAW_SCENARIO_PORT=...` 路径上复跑通过
- 经过多轮 Codex 交叉审查修复

---

## 6 条替代口径覆盖状态

| # | 口径 | 验证来源 | 状态 |
|---|------|---------|------|
| 1 | 默认链路可用 | V1 + S3 | ✅ 通过 |
| 2 | CRUD 主链可用 | S1 + S2 | ✅ 通过 |
| 3 | recall 注入可用 | V2 + S1 + S3 | ✅ 通过 |
| 4 | compact/index 主链 | S4 + S6 | ✅ 通过 (S6 via MCP stdio) |
| 5 | chat/gateway 宿主链路 | V2 + S3 | ✅ 通过 |
| 6 | 失败回退可用 | S5 | ✅ 通过 |

---

## 当前最强可公开结论

**在当前 replacement acceptance scope、当前目标环境与当前 `OpenClaw 2026.4.5` 宿主验证条件下，已证明 memory-palace plugin + skills 可以替代 OpenClaw 默认 memory system 的基础职责。当前 CLI/Host replacement acceptance 已重新达到 `7/7 PASS`，WebUI acceptance 仍保持 current-host / isolated `6/6 PASS`，而且仓库内 `phase45` 的 C/D 复跑也已转绿。**

Memory Palace plugin + bundled skills 在 CLI/Host 和 WebUI 两个层面均通过了 replacement acceptance 测试，覆盖了 CRUD、recall、compact/index、fallback 和 chat/gateway 全部 6 条替代口径。WebUI 层现在同时覆盖了 plugin 可见、write+recall、系统集成证据、`blocked->confirm->force->recall`、中文极短确认、英文极短确认。3 个原边界已在补强阶段消除：

| 原边界 | 补强措施 | 状态 |
|--------|---------|------|
| compact_context 仅 MCP-only | S6: MCP stdio 直连 create → compact → search | ✅ 已消除 |
| Layer B 依赖已运行宿主 | `ACCEPTANCE_FORCE_ISOLATED=true OPENCLAW_SCENARIO_PORT=18981` + `prepareScenario` | ✅ 已消除 |
| recall 证据偏 UI marker 级 | current-host / isolated 都增加了 CLI strong evidence fallback 与独立 marker 检验 | ✅ 已收敛为可解释边界 |

Profile D (LLM write guard + compact gist) 作为增强档位的补充覆盖，本轮除了 S7 通过之外，仓库内 `phase45` 的 D 路径也已复跑通过；但它仍然属于“provider 已就绪前提下的高级档位验证”，不是零配置主论据。

---

## 复现命令

### Layer A: CLI/Host

```bash
# 前置：Python 3.10+, backend 依赖已安装；推荐直接使用仓库虚拟环境
cd /path/to/memory-palace
source .venv/bin/activate

# 全量运行（Profile B/C/D，需要 embedding / reranker / LLM provider）
python scripts/test_replacement_acceptance_e2e.py

# 仅 Profile B（无需外部 provider）
python scripts/test_replacement_acceptance_e2e.py --skip-profile-c

# Profile C 需要外部 embedding/reranker 服务可达
# 通过环境变量覆盖默认地址：
RETRIEVAL_EMBEDDING_API_BASE=http://your-embed:11435/v1 \
RETRIEVAL_RERANKER_API_BASE=http://your-reranker:8080/v1 \
python scripts/test_replacement_acceptance_e2e.py
```

**输出**：
- `backend/tests/benchmark/replacement_acceptance_report.json`
- `backend/tests/benchmark/replacement_acceptance_report.md`

如果你不使用仓库 `.venv`，至少要确保当前 Python 环境里已经安装 `uvicorn` 和 backend 依赖；否则脚本会在 backend 启动阶段直接失败。

**依赖本机状态**：Profile C/D 场景需要外部 embedding/reranker/LLM 服务在线。

### Layer B: WebUI/Playwright

```bash
# 前置：Node.js 22+, frontend 依赖已安装 (npm install in frontend/), openclaw CLI 可用
cd /path/to/memory-palace

# 主复现命令：完全隔离 gateway/profile/workspace（最终结论所依赖的模式）
ACCEPTANCE_FORCE_ISOLATED=true OPENCLAW_SCENARIO_PORT=18981 node scripts/test_replacement_acceptance_webui.mjs

# (可选) 开发便捷路径：复用已运行的 gateway（不用于正式结论）
node scripts/test_replacement_acceptance_webui.mjs
```

**输出**：
- `.tmp/replacement-acceptance/<profile>/webui_report.json`
- `.tmp/replacement-acceptance/<profile>/v1_plugin_status.png`
- `.tmp/replacement-acceptance/<profile>/v2_chat_recall.png`
- `.tmp/replacement-acceptance/<profile>/v3_mcp_tools.png`
- `.tmp/replacement-acceptance/<profile>/v4_force_write_chat.png`
- `.tmp/replacement-acceptance/<profile>/v5_chinese_confirm_chat.png`
- `.tmp/replacement-acceptance/<profile>/v6_english_confirm_chat.png`

**依赖本机状态**：隔离模式需要 `openclaw` CLI 可用（用于 `prepareScenario` + `startGateway`）。复用模式需要 OpenClaw gateway 已在运行。

---

## Artifact 清单

### 应 commit 的文件

| 文件 | 说明 |
|------|------|
| `scripts/test_replacement_acceptance_e2e.py` | Layer A CLI/Host 测试 harness |
| `scripts/test_replacement_acceptance_webui.mjs` | Layer B WebUI/Playwright 测试 harness |
| `backend/tests/benchmark/replacement_acceptance_summary.md` | 本摘要文件 |

### 仅保留本地（不 commit）

| 文件 | 说明 |
|------|------|
| `backend/tests/benchmark/replacement_acceptance_report.json` | Layer A 运行报告（含本机 port/IP） |
| `backend/tests/benchmark/replacement_acceptance_report.md` | Layer A 运行报告 Markdown |
| `.tmp/replacement-acceptance/` | Layer B 运行报告 + 截图 |

---

## Profile D 补充说明

S7 验证了 Profile D（API embedding + reranker + LLM write guard + compact gist）路径完整可用：

- **write1**: `guard_method=keyword, guard_action=ADD` — 首次写入，无重复，快速通过
- **write2**: `guard_method=semantic comparison, guard_action=ADD` — 二次写入，semantic 比较分支被触发，评估后允许 ADD
- **LLM guard 未被触发**（`llm_triggered=False`）：embedding/keyword 在阈值内已做出正确决策，LLM 作为最后兜底未被需要

S7 证明了：
- ✅ D 路径可用（所有 provider 配置正确加载）
- ✅ semantic 比较分支正常工作
- ✅ 首次写入和增量写入的 guard 行为正确
- ⚠️ LLM write-guard 推理分支**未被完整验证**（在当前 qwen3-embedding 质量下，embedding/keyword 快速路径已能处理大多数场景，LLM 分支作为最后兜底极少被触发）

因此，Profile D 只作为增强档位的**补充覆盖**，不作为「已证明可替代」的核心论据。核心论据基于 Profile A/B/C 的 S1-S6 和 Layer B 的 V1-V3。

---

## 补强记录

3 项原边界已在补强阶段全部消除：

1. **S6: compact_context via MCP stdio** — 通过 `mcp.ClientSession` + `stdio_client` 直连后端 MCP，调用 create_memory → compact_context(force=true) → search_memory，确认 compact 后内容仍可召回 (PASS, 1154ms)
2. **Layer B 完全隔离** — 新增 `ACCEPTANCE_FORCE_ISOLATED=true` 模式，通过 `prepareScenario({installPlugin: true})` + `startGateway()` 在全新隔离 HOME/state/workspace/config 上运行 WebUI 测试 (6/6 PASS)
3. **聊天面验证扩到 6 项** — 在原有 V1-V3 之外，新增 `V4 blocked->confirm->force->recall`、`V5 中文极短确认`、`V6 英文极短确认`，并全部复跑通过

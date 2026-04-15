> [English](EVALUATION.en.md)

# Memory Palace 评测与验证

这页统一做两件事：

1. 给出**当前公开可以引用**的验证基线
2. 保留真正还有参考价值的 benchmark / 质量门禁摘要

先说边界：

- 这里写的是**当前仓库代码 + 最新留档的真实复跑**的结果
- 不是“所有用户环境天然都一样”
- 对 OpenClaw 用户来说，这页是验证基线页，不是安装入口页
- OpenClaw `setup` 会改你本机的 OpenClaw 配置文件，不是改 OpenClaw 源码；如果你没有在自己的目标环境亲自重跑，就不要把历史结果写成“刚刚现跑”
- 这页里提到的 `provider-probe / onboarding`，都指 **repo wrapper** `python3 scripts/openclaw_memory_palace.py ...`；如果你在 Windows PowerShell 里跑，就把它改成 `py -3 scripts/openclaw_memory_palace.py ...`。它们不是 `openclaw memory-palace` 的稳定子命令

安装入口看：

- `docs/openclaw-doc/README.md`
- `docs/openclaw-doc/01-INSTALL_AND_RUN.md`

---

## 1. 当前公开验证基线

### 1.1 当前页面引用的真实复跑

当前页面最上面的公开基线，现在以 **2026-04-15 Windows 实机复跑** 为主；下面 benchmark / ablation 的历史章节，继续按各自标注日期理解。

当前页面引用的结果来自下列已实际重跑并确认的命令：

| 项目 | 命令 | 结果 |
|---|---|---|
| 验证主机版本 | `openclaw --version` | `OpenClaw 2026.4.14` |
| Profile A setup | `py -3 scripts/openclaw_memory_palace.py setup --mode basic --profile a --transport stdio --json` | `PASS` |
| Profile A 签收 | `openclaw memory-palace verify / doctor / smoke --json` | `PASS`（隔离 target config） |
| Profile B setup | `py -3 scripts/openclaw_memory_palace.py setup --mode basic --profile b --transport stdio --json` | `PASS` |
| 插件加载状态 | `openclaw plugins inspect memory-palace --json` | `PASS`，插件已加载 |
| Profile B onboarding tool 面 | `memory_onboarding_status / probe / apply` | `PASS`，已注册到真实宿主 |
| Profile B 签收 | `openclaw memory-palace verify / doctor / smoke --json` | `PASS` |
| provider probe（Profile C） | `py -3 scripts/openclaw_memory_palace.py provider-probe --profile c ... --json` | `PASS` |
| provider probe（Profile D） | `py -3 scripts/openclaw_memory_palace.py provider-probe --profile d ... --json` | `PASS` |
| apply（Profile C） | `py -3 scripts/openclaw_memory_palace.py onboarding --profile c --apply --validate --json` | `PASS` |
| apply（Profile D） | `py -3 scripts/openclaw_memory_palace.py onboarding --profile d --apply --validate --json` | `PASS` |
| Profile C / D 签收 | `openclaw memory-palace verify / doctor / smoke --json` | `PASS` |
| Windows native validation unittest | `py -3 -m unittest scripts.test_openclaw_memory_palace_windows_native_validation` | `PASS` |
| installer 回归测试 | `py -3 -m unittest scripts.test_openclaw_memory_palace_installer` | `PASS` |
| 扩展测试套件 | `cd extensions/memory-palace && bun test` | `488 pass / 2 skip / 0 fail` |
| 扩展 typecheck | `cd extensions/memory-palace && npm run typecheck` | `PASS` |
| 最新 doc-chat 定向复跑 | `cli-uninstalled-zh`、`cli-installed-zh`、`cli-installed-en` | `PASS` |

这里要按正确口径理解：

- 这页现在只把**当前页面明确列出的真实复跑结果**写成“已验证”。
- 上面这张表现在优先代表 **Windows 实机 OpenClaw 验收**；后面的 benchmark / 历史复盘 / 旧运行日志，继续按原始日期理解。
- 当前公开摘要没有把历史上的 frontend / backend / retrieval benchmark 全量重写成“这次刚验证”，所以旧数字继续按历史记录理解。

### 1.2 这次结果应该怎么读

- `Profile B` 仍然是最稳的默认起步档。
- 如果 embedding + reranker + LLM 都已经 ready，`Profile D` 仍然是当前更强的推荐目标。
- 这轮 Windows 实机已经再次确认：`A / B / C / D` 的 setup / probe / apply / sign-off 主链都能在隔离 target config 上通过。
- 这轮也再次确认了未安装边界：在 plugin 真正装进宿主之前，`memory_onboarding_status / probe / apply` 不存在，不能把未安装和已安装流程揉成一条。
- 最新这轮文案清理后，又定向复跑了 `cli-uninstalled-zh`、`cli-installed-zh`、`cli-installed-en`；所以当前中英文安装 / 已装主分支，已经有和最新版文案对齐的 Windows 证据。

### 1.2A 这轮补记的 macOS / Linux 维护者复跑

下面这些结果是补充性的维护者复跑，不替换上面那张 Windows 主基线表。

- 在 macOS 验证主机上，shared-LLM 的 `Profile D` 路径已经按
  `OpenClaw 2026.4.14` 再次复跑。修掉 placeholder override 之后，
  留档的 `setup --profile d` 和
  `onboarding --profile d --strict-profile --apply --validate`
  都保持了 `effective_profile=d`，没有再 fallback。
- 在 Docker Linux userspace 里，这轮也补跑了 `linux/aarch64` 和
  `linux/amd64` 两套环境；两边都通过了 shared-LLM 的
  `setup --profile b`、`setup --profile d`、以及
  `onboarding --profile d --strict-profile --apply --validate`。
- Docker 侧的 WebUI 复跑也再次确认：默认 gateway 端口链路是通的，
  bundled onboarding skill 仍然可见。
- 但有一条更窄的宿主限制也继续被看到：如果宿主用自定义 gateway 端口启动，
  却没有把端口写回 config，`dashboard --no-open` /
  `gateway.controlUi.allowedOrigins` 仍可能落回默认端口。
  这条在本仓库里按 OpenClaw 宿主限制记录，不按插件保证或宿主热修补丁目标来写。

### 1.3 当前公开口径

- `Profile C / D` 仍然是 provider-backed 路径，不是零配置承诺。
- `onboarding --json` 是 readiness 报告，不是最终签收。
- 真正改配置的是 `setup ...` 或 `onboarding --apply --validate ...`。
- 安装后稳定给用户长期使用的命令面仍然是：
  - `openclaw memory-palace ...`
- 当前公开文档里的插件加载检查，以 `openclaw plugins inspect memory-palace --json` 为准；有些宿主也接受 `plugins info`，但 `openclaw skills list` 不是 bundled onboarding skill 的安装判断条件。
- 当前公开“对话式 onboarding”口径，验证的是：
  - 把本地 checkout 的文档页或文档路径交给 OpenClaw
  - 然后让 OpenClaw 给出正确下一步
- 它**不是**在公开文档里承诺：
  - 任意宿主都能直接抓公开 GitHub URL
  - 任意环境都能一次性聊天完成最终 apply + 全绿签收

---

## 2. Token 效率

Memory Palace 使用按需召回（auto-recall）替代原生 MEMORY.md 的全量注入。
每次对话只检索与当前 prompt 相关的记忆，而不是把整个 MEMORY.md 塞进 system prompt。

**原理**：原生方式每轮注入完整 MEMORY.md（不论相关性），Memory Palace 只返回
与当前 prompt 匹配的 top-k 结果。当 MEMORY.md 增长到数 KB 甚至数十 KB 时，
节省效果显著。

实际节省量取决于你的 MEMORY.md 大小和对话内容。用以下命令在你自己的环境中测量：

```bash
python3 scripts/openclaw_token_efficiency_benchmark.py --json
```

该脚本会读取你本机的 MEMORY.md 大小，并通过真实 FTS 搜索测量每个场景的召回量。

---

## 3. 检索评测摘要

> **评测层级说明**：本节（Section 3）是 **retrieval benchmark**，衡量检索管线本身的召回/排序质量（HR, MRR, NDCG, Recall）。Section 4 是 **product quality ablation**，衡量面向用户的决策质量（write guard, intent, gist）。两者使用不同的数据集和指标体系，不应混合解读。

**数据集定位**：

- **SQuAD v2** = retrieval guard rail（高词汇重叠的段落召回基线，用于防回归）
- **BEIR NFCorpus** = domain-gap stress test（跨术语语义检索压力测试，用于暴露 embedding 弱点）
- **两者都不是 memory-system-fit benchmark**——它们不覆盖 URI 层级、vitality、session-first 排序、write guard / intent 交互等 Memory Palace 特有场景

当前应以 **2026-04-05 真实复测** 为主口径，artifact 分为两类：

**Baseline real rerun**（gitee embedding provider）：

- `backend/tests/benchmark/profile_abcd_real_rerun_20260405.json`
- `backend/tests/benchmark/profile_abcd_real_metrics.json`
- `backend/tests/benchmark/benchmark_results_profile_cd_real.md`

**Follow-up local-provider diagnostics**（local q8_0 embedding，非 baseline gitee provider）：

- `backend/tests/benchmark/beir_improvement_exp_results.json`（Exp 1/2）
- `backend/tests/benchmark/exp3_reranker_weight_results.json`（Exp 3）
- `backend/tests/benchmark/exp4_cm_diagnostic_results.json`（Exp 4）

`benchmark_results_profile_a.md` / `benchmark_results_profile_b.md` / `benchmark_results_profile_cd.md` 仍可作为 2026-03 的历史快照，但不应再被当作当前结论源。

### 3.1 当前复测合同（2026-04-05）

当前真实复测 runner 仍是 `backend/tests/benchmark/helpers/profile_abcd_real_runner.py`，但本轮有效合同已经固定为：

- dataset scope：`SQuAD v2 Dev` + `BEIR NFCorpus`
- sample size：每个数据集 `100`
- `first_relevant_only=true`
- `extra_distractors=200`
- `candidate_multiplier=8`
- `max_results=10`
- `source_variant=sqlite_client.search_advanced`

这意味着本轮结果测的是：在统一 top-10 检索深度、统一 200 条干扰文档、统一单相关文档口径下，各 profile 的召回和排序质量。

按 runner 实现，当前这套合同会把每个 query 的相关文档裁成首个相关项，并把语料裁成“保留正例 + 200 个随机干扰项”；因此实际评测语料只有 `SQuAD=295 docs`、`NFCorpus=293 docs`。这是一套**缩小语料回归 harness**，适合比较 profile 和回归变化，不等价于完整 SQuAD / 完整 BEIR leaderboard 评测。

### 3.2 当前真实复测结果（A/B/C/D）

来源：`profile_abcd_real_rerun_20260405.json` / `profile_abcd_real_metrics.json`。

| Profile | Dataset | HR@10 | MRR | NDCG@10 | Recall@10 | p95(ms) | 备注 |
|---|---|---:|---:|---:|---:|---:|---|
| A | SQuAD v2 Dev | 0.050 | 0.010 | 0.019 | 0.050 | 131.9 | keyword-only |
| A | BEIR NFCorpus | 0.160 | 0.133 | 0.140 | 0.160 | 194.5 | keyword-only |
| B | SQuAD v2 Dev | 0.380 | 0.210 | 0.251 | 0.380 | 174.4 | 本轮记录到 `embedding_fallback_hash` |
| B | BEIR NFCorpus | 0.170 | 0.102 | 0.118 | 0.170 | 248.0 | 本轮记录到 `embedding_fallback_hash` |
| C | SQuAD v2 Dev | 0.990 | 0.924 | 0.941 | 0.990 | 1085.5 | api embedding |
| C | BEIR NFCorpus | 0.380 | 0.254 | 0.285 | 0.380 | 1245.7 | api embedding |
| D | SQuAD v2 Dev | 0.990 | 0.973 | 0.978 | 0.990 | 2049.0 | api embedding + reranker |
| D | BEIR NFCorpus | 0.420 | 0.284 | 0.316 | 0.420 | 1946.8 | api embedding + reranker |

当前最需要直接读出的事实：

- `SQuAD v2` 上，C/D 已接近饱和：`HR@10=0.99`
- `BEIR NFCorpus` 上，C/D 明显弱很多：C=`0.38`，D=`0.42`
- 当前系统对数据集分布高度敏感，不能用 `SQuAD v2` 的接近满分去代表“通用 memory retrieval 已成熟”
- D 相比 C 在 `NFCorpus` 只提升了约 `+0.04 HR@10`，但延迟从 `1245.7ms` 增到 `1946.8ms`

### 3.3 NFCorpus 定向复测（Exp 1-4）

> **Provider 说明**：Exp 1-4 全部运行在 **local q8_0 量化 provider**（本地 Qwen3-Embedding-8B q8_0），**不是** 3.2 baseline 使用的 gitee API provider。因此 Exp 1-4 的绝对数值不能与 3.2 直接比较，只能在 Exp 系列内部互相比较。

2026-04-05 针对 `BEIR NFCorpus` 连续做了 4 轮定向实验，目的是回答”问题是候选召回、reranker 权重，还是 embedding 表征本身”。

#### Exp 1 / 2：weight 与 overfetch

来源：`beir_improvement_exp_results.json`。

| Experiment | Profile | NFCorpus HR@10 | MRR | NDCG@10 | p95(ms) | 结论 |
|---|---|---:|---:|---:|---:|---|
| baseline rerun | C | 0.380 | 0.254 | 0.285 | 1245.7 | 当前起点 |
| Exp1 weight rebalance | C | 0.480 | 0.311 | 0.351 | 829.6 | 有改善，但不稳定复制到组合实验 |
| Exp2 overfetch 3→6 | C | 0.490 | 0.318 | 0.359 | 605.0 | 明确改善 |
| baseline rerun | D | 0.420 | 0.284 | 0.316 | 1946.8 | 当前起点 |
| Exp1 weight rebalance | D | 0.490 | 0.322 | 0.362 | 1718.1 | 小幅改善 |
| Exp2 overfetch 3→6 | D | 0.500 | 0.325 | 0.367 | 1858.9 | 小幅改善 |

解释：

- `Exp 2` 说明 semantic overfetch 确实不是完全没用，`NFCorpus` 上能带来一段提升
- 但改善幅度仍远小于 `SQuAD v2` 的近饱和表现，说明问题没有被“调参”彻底解决

#### Exp 3 / 4：reranker weight 与 candidate multiplier

来源：`exp3_reranker_weight_results.json`、`exp4_cm_diagnostic_results.json`。

| Experiment | Profile | NFCorpus HR@10 | MRR | NDCG@10 | p95(ms) | 结论 |
|---|---|---:|---:|---:|---:|---|
| Exp3 weight=0.25 | D | 0.500 | 0.325 | 0.367 | 1801.3 | 基线控制 |
| Exp3 weight=0.35 | D | 0.500 | 0.323 | 0.365 | 1884.8 | 基本持平 |
| Exp3 weight=0.45 | D | 0.510 | 0.324 | 0.368 | 1835.6 | 仅微小提升 |
| Exp4 cm=8 | C | 0.490 | 0.318 | 0.359 | 520.3 | 基线 |
| Exp4 cm=12 | C | 0.490 | 0.318 | 0.359 | 634.2 | 无质量收益 |
| Exp4 cm=16 | C | 0.490 | 0.318 | 0.359 | 557.2 | 无质量收益 |
| Exp4 cm=24 | C | 0.490 | 0.318 | 0.359 | 605.3 | 无质量收益 |

解释：

- `Exp 3` 表明 reranker weight 不是主瓶颈，只能带来噪声级收益
- `Exp 4` 表明把候选池从 `80` 扩到 `240` 没有质量提升，candidate generation 不是主瓶颈
- 在当前 `Qwen3-Embedding-8B + Qwen3-Reranker-8B` 组合下，`NFCorpus` 的主限制更像是 **embedding/domain representation gap**，而不是简单的候选数不够
- 因为这组实验仍运行在 `first_relevant_only + extra_distractors=200` 的缩小语料合同下，所以 `HR@10≈0.49 / NDCG@10≈0.36` 应被解读为“在 biomedical stress test 上表现中等、明显落后于 SQuAD guard rail”，而不是“完整 BEIR NFCorpus 上可直接横向对比公开 leaderboard 的成绩”

### 3.4 数据集适配性判断：适不适合 OpenClaw memory system？

先说结论：

- `SQuAD v2`：**适合当 guard rail，不适合当主结论数据集**
- `BEIR NFCorpus`：**适合当 semantic stress test，不适合单独代表 memory system 场景**
- 两者一起用，作为“下限 + 压力测试”是合理的；拿它们直接对外宣称“OpenClaw memory retrieval 已被充分代表”则不合理

还有一个容易误读的边界：

- 当前 `NFCorpus` 的 `0.49 / 0.50 HR@10` **不能直接拿去和标准 BEIR leaderboard 或论文表格横向比较**
- 原因不是“这个数不能看”，而是当前 harness 不是完整 BEIR 评测：它用了项目内 materialized corpus、`sample_size=100`、`first_relevant_only=true`、`extra_distractors=200`，测的是 **memory-style retrieval regression contract**，不是全量 zero-shot IR leaderboard
- 因此，`0.49` 的正确解读是：**在当前 memory harness 内，NFCorpus 仍明显难于 SQuAD；在当前 Qwen3 embedding/reranker 组合下，跨术语 biomedical 检索仍是弱项**
- 它最适合做同仓内横向对比（A/B/C/D、参数实验前后、模型替换前后），不适合直接拿来宣称“已达到某个公开 BEIR SOTA 水位”

#### SQuAD v2 测到的是什么

根据官方 SQuAD 说明和论文，`SQuAD v2` 测的是：

- 给定一段固定上下文后，系统能否找到答案 span
- 或者判断该段里没有答案并 abstain
- 本质是 **passage reading comprehension / extractive QA**，不是长期记忆检索

它对当前项目的价值：

- 适合验证“已知相关段落就在 corpus 里时，检索器能不能把它顶到前 10”
- 适合做高置信 guard rail，防止基础检索链路退化

它对 OpenClaw memory system 的缺口：

- 没有真实的个人/项目记忆演化过程
- 没有 session-first、scope hint、ancestor/path、版本冲突、write/noop/update 等 memory 特有约束
- Query 和 doc 都是 Wikipedia QA 风格，和“回忆历史决策 / 偏好 / 时间线 / 约束”的 memory query 差距很大

所以 `SQuAD v2` 上接近满分，只能说明 **paragraph lookup 能力非常强**，不能说明 memory retrieval 已经“几乎解决”。

#### BEIR NFCorpus 测到的是什么

根据 NFCorpus 官方页面与 BEIR 官方仓库/论文，`NFCorpus` 测的是：

- 非技术医疗问句对技术医学文档的检索
- 重点压力在 **lexical gap / domain gap**
- 在 BEIR 里属于 biomedical IR 的 zero-shot generalization 场景

它对当前项目的价值：

- 很适合暴露 embedding 在跨表达方式、跨术语空间下的弱点
- 对“用户口语化提问，记忆内容写得更技术化/更碎片化”这类问题有一定类比价值

它对 OpenClaw memory system 的缺口：

- domain 是 biomedical，不是软件开发 / agent workflow / personal memory
- 文档是公开医学文档，不是带时间、层级、作用域、版本演化的 memory entries
- 不覆盖 write guard、contradiction、gist、multi-intent retrieval 这些产品核心链路

所以 `NFCorpus` 上的弱分数很有价值，因为它确实暴露了语义表征问题；但它仍然只是 **跨域语义检索压力测试**，不是 memory-native benchmark。

### 3.5 对外口径建议

当前最稳的说法应是：

- `SQuAD v2` 用来做 retrieval guard rail，证明基础段落召回没有退化
- `BEIR NFCorpus` 用来做 domain-gap stress test，证明系统在 harder semantic IR 上仍有明显短板
- 这两套 benchmark 足以支持“当前系统对数据集分布敏感，NFCorpus 暴露了 embedding 表征瓶颈”
- 这两套 benchmark **不足以** 支持“已经充分覆盖 OpenClaw memory system 真实使用场景”

**Memory-Native Retrieval Benchmark（2026-04-06 已完成）**：

上述 SQuAD/BEIR benchmark 的 memory-native 空白现已由独立的 memory-native benchmark 填补（spec: `docs/MEMORY_NATIVE_BENCHMARK_SPEC.md` v3.6.2，结果: `backend/tests/benchmark/memory_native_full_report.json`）。该 benchmark 覆盖：

- factual / temporal / causal / exploratory 四类 memory query（19 taxonomy codes）
- scope/path/ancestor 约束、alias 召回
- 近重复记忆、版本漂移、冲突记忆
- 会话优先召回与长期库混排（Layer B）
- 真实 memory 文本风格（6 domain × 8 text_style，70 条 synthetic corpus）

Layer A 定稿结果（2026-04-06 frozen baseline）：

| Profile | HR@10 | MRR | NDCG@10 |
|---|---:|---:|---:|
| A (keyword) | 0.292 | 0.194 | 0.203 |
| B (hybrid/hash) | 0.521 | 0.269 | 0.323 |
| C (hybrid/api) | 0.604 | 0.380 | 0.416 |
| D (hybrid/api+reranker) | 0.917 | 0.797 | 0.791 |

该 benchmark 针对 Memory Palace 检索管线。Layer B 仅提供 session-merge 机制与局部质量证据。完整审计见 `docs/MEMORY_NATIVE_BENCHMARK_HANDOFF_20260405.md`。

**Section 3 结论边界（收口）**：

1. SQuAD/BEIR（Section 3.1-3.3）证明 **通用检索管线特性**：段落召回能力（SQuAD guard rail）和跨域语义检索弱点（BEIR stress test）
2. Memory-native benchmark 证明 **memory 场景检索管线特性**：A→B→C→D 逐级提升成立，D reranker 真实有效，filter/alias/ancestor/temporal 机制功能正确
3. Hard-mode benchmark（60 corpus + 20 零词面重合 query）证明 **语义/改写检索能力**：MP-D(HR=1.000) 和 MP-C(HR=0.950) 在同义改写场景下显著优于 filesystem keyword baseline(HR=0.800)，holdout 8 query 复验通过。但 filesystem keyword baseline 不是实际 OpenClaw native memory 实现
4. 两者合并仍 **不能单独证明 OpenClaw memory-system fit**——缺少与 OpenClaw native memory 完整链路的直接对照。最终是否适合作为 OpenClaw memory system 的替代方案，需要综合判断：acceptance criteria（Section 1）、smoke test（Section 1.2/1.3）、product quality path（Section 4）、replacement acceptance（Section 3.6）

### 3.6 Replacement Acceptance E2E（2026-04-06）

上述 benchmark 证明的是检索管线质量。Section 3.6 验证的是另一个问题：**memory-palace plugin + skills 是否已能在 OpenClaw 宿主里承担默认 memory system 的基础职责**。

这不是 benchmark（不算 HR/MRR），而是 acceptance gate，按 6 条替代口径逐条验证。

**Layer A: CLI/Host Acceptance — 标准集 7/7 PASS；打开 runtime 扩展后 9/9 PASS**

| 场景 | Profile | 验证内容 | 结果 |
|---|---|---|---|
| S1 | B | 写入后关键词搜索可召回 | PASS |
| S2 | B | 更新后召回最新内容、旧内容不残留 | PASS |
| S3 | B | 进程重启后持久化（PID 验证真正重启） | PASS |
| S4 | C | rebuild_index 后 hybrid 搜索可召回 | PASS |
| S5 | C→降级 | 无效 reranker 下 hybrid 搜索仍返回结果，`degraded=True` | PASS |
| S6 | B | compact_context 通过 MCP stdio 直连 create→compact→search | PASS |
| S7 | D | write guard + hybrid search，embedding semantic 比较分支正常 | PASS |

- 每个场景使用隔离 temp workspace + 独立 SQLite DB + 空 env 文件，不触碰 `~/.openclaw`
- S5 确认 `degraded=True` + `reranker_request_failed`，系统回退安全行为
- S7 的 write guard 使用了 keyword 快速通过（首写）和 semantic comparison（二写），LLM 推理分支未被触发；D 路径可用但 LLM guard 增强能力未被完整验证
- `S8` 现在验证“短会话但高价值”的 runtime flush 能立即 recall
- `S9` 现在验证重复高价值文本不会形成 flush storm
- 经 3 轮 Codex 交叉审查修复

**Layer B: WebUI/Playwright Acceptance — current-host strict 6/6 PASS；isolated 核心 gate 6/6 PASS；isolated 开启可选 V7 后 7/7 PASS**

| 验证点 | 结果 | 证据 |
|---|---|---|
| V1: Plugin 可见 | PASS | current-host strict `C/D` 与 isolated `A/B/C/D` 都能看到 `memory-palace` |
| V2: Chat 中 write + recall | PASS | current-host strict `C/D` 继续拿到文本级 recall；isolated `C/D` 现在按 expected agent lane + normalized capture 校验，不再把高级 profile 的 canonical durable write 误判成失败 |
| V3: 系统集成证据 | PASS | current-host strict `C/D` 在最新留档复跑里都拿到 28 个强证据 markers；isolated `A/B` 分别拿到 24 / 27 个；isolated `C/D` 按 lane-aware CLI 证据通过 |
| V4: blocked -> confirm -> force -> recall | PASS | current-host strict `C/D` 与 isolated `A/B/C/D` 都通过 |
| V5: 中文极短确认 `记住了。` | PASS | current-host strict `C/D` 与 isolated `A/B/C/D` 都通过 |
| V6: 英文极短确认 `Stored.` | PASS | current-host strict `C/D` 与 isolated `A/B/C/D` 都通过 |
| V7: 短会话高价值 recall（可选） | PASS | 只在 isolated 路径启用；`A/B/C/D` 在最新留档复跑里都是 `7/7 PASS`，但它验证的是“短会话 recall UX”，不是 strict early-flush 证明 |

**Layer B 结果总表**

| 路径 | Profile | 结果 | 报告 |
|---|---|---|---|
| current-host strict | C | 6/6 PASS | `.tmp/replacement-acceptance/current-host-c-strict/webui_report.json` |
| current-host strict | D | 6/6 PASS | `.tmp/replacement-acceptance/current-host-d-strict/webui_report.json` |
| isolated | A | 7/7 PASS | `.tmp/replacement-acceptance/a/webui_report.json` |
| isolated | B | 7/7 PASS | `.tmp/replacement-acceptance/b/webui_report.json` |
| isolated | C | 7/7 PASS | `.tmp/replacement-acceptance/c/webui_report.json` |
| isolated | D | 7/7 PASS | `.tmp/replacement-acceptance/d/webui_report.json` |

- `2026-04-12` 的补充复跑又覆盖了一次 `isolated A/B/C/D` + `current-host strict C/D`：
  - `isolated C/D` 在该次复跑里的 `V2` 已不再把高级 profile 的 normalized durable capture 误判成失败
  - `current-host strict C/D` 在该次复跑里都重新跑到 `6/6 PASS`
  - `isolated A/B/C/D` 在启用可选 `V7` 后都跑到 `7/7 PASS`
  - `V7` 只代表短会话 recall 用户体验通过；是否真的触发 early flush，以 host-level probe 为准

- current-host strict 复跑命令：
  - `OPENCLAW_ONBOARDING_USE_CURRENT_HOST=true OPENCLAW_ACCEPTANCE_STRICT_UI=true OPENCLAW_PROFILE=c node scripts/test_replacement_acceptance_webui.mjs`
  - `OPENCLAW_ONBOARDING_USE_CURRENT_HOST=true OPENCLAW_ACCEPTANCE_STRICT_UI=true OPENCLAW_PROFILE=d node scripts/test_replacement_acceptance_webui.mjs`
- isolated 复跑命令：
  - `ACCEPTANCE_FORCE_ISOLATED=true OPENCLAW_PROFILE=a OPENCLAW_ACCEPTANCE_INCLUDE_HIGH_VALUE_SHORT_SESSION=true OPENCLAW_SCENARIO_PORT=18980 node scripts/test_replacement_acceptance_webui.mjs`
  - `ACCEPTANCE_FORCE_ISOLATED=true OPENCLAW_PROFILE=b OPENCLAW_ACCEPTANCE_INCLUDE_HIGH_VALUE_SHORT_SESSION=true OPENCLAW_SCENARIO_PORT=18981 node scripts/test_replacement_acceptance_webui.mjs`
  - `ACCEPTANCE_FORCE_ISOLATED=true OPENCLAW_PROFILE=c OPENCLAW_ACCEPTANCE_INCLUDE_HIGH_VALUE_SHORT_SESSION=true OPENCLAW_SCENARIO_PORT=19002 OPENCLAW_SETUP_ARGS_JSON='<provider-args>' node scripts/test_replacement_acceptance_webui.mjs`
  - `ACCEPTANCE_FORCE_ISOLATED=true OPENCLAW_PROFILE=d OPENCLAW_ACCEPTANCE_INCLUDE_HIGH_VALUE_SHORT_SESSION=true OPENCLAW_SCENARIO_PORT=19003 OPENCLAW_SETUP_ARGS_JSON='<provider-args>' node scripts/test_replacement_acceptance_webui.mjs`
- `current-host` 现在不再隐式依赖隔离场景里的 `alpha` agent，会话路由改成按宿主路径优先使用 `main`
- `V4` 在最新留档复跑里已经从“普通 recall”扩成真实聊天链：`write_guard_blocked -> 用户确认 -> force 写入 -> recall`
- `V5 / V6` 现在是独立验证点，不再只是中文/英文确认的间接覆盖
- `V5` 中文极短确认 `记住了。` 已在 current-host 与 isolated 路径都验证通过
- `V6` 英文极短确认 `Stored.` 已在 current-host 与 isolated 路径都验证通过
- `V2` 当前更稳的规则是：先看文本级 recall；如果走的是 isolated `C/D` 慢链路，则允许 expected agent lane 上的 normalized durable capture 作为 CLI fallback 证据

**2026-04-12 安装 / onboarding 时间快照**

下面这组时间只描述**当前记录对应的 workspace + macOS 验证主机**的真实复跑，不外推成所有宿主都会拿到同样数字。

| 路径 | 条件 | 实测 |
|---|---|---|
| `setup --mode basic` `Profile A` | 热路径 | `0.36s` |
| `setup --mode basic` `Profile B` | 热路径 | `0.35s` |
| `setup --mode basic` `Profile C` | provider-ready 热路径 | `8.79s` |
| `setup --mode basic` `Profile D` | provider-ready 热路径 | `8.05s` |
| `onboarding --apply` `Profile A` | 热路径 | `0.35s` |
| `onboarding --apply` `Profile B` | 热路径 | `0.36s` |
| `onboarding --apply` `Profile C` | provider-ready 热路径 | `15.33s` |
| `onboarding --apply` `Profile D` | provider-ready 热路径 | `14.81s` |
| onboarding 文档 -> `CLI` 给出最短正确链路 | 未安装宿主 | `38.59s` |
| onboarding 文档 -> `CLI` 给出后续 `probe/apply` 链路 | 已安装宿主 | `53.01s` |
| onboarding 文档 -> `WebUI` 路径总时长 | 未安装宿主，复用模板 | `13.59s` |
| onboarding 文档 -> `WebUI` 路径总时长 | 已安装宿主，复用模板 | `27.23s` |
| `python3 scripts/test_openclaw_memory_palace_package_install.py` | 当前缓存 / 可复用 runtime 条件 | `1:48.49` |

这组时间更适合这样解释：

- **真正慢的仍然是第一次完整安装**
- 当前这台 macOS 主机上，如果宿主还没有现成 runtime / template，首次冷启动应按 `8-10+ 分钟` 看
- 一旦 runtime / template 已存在，`Profile B` 会回到秒级，`Profile C / D` 会回到十几秒级
- onboarding 页面交给 OpenClaw 后，模型给出正确下一步通常是 `40-60 秒`；聊天回复本身不是主要瓶颈

**结论**：

当前页面可直接引用的已验证事实是：`Profile A/B/C/D` 的 CLI smoke、`Profile A/B/C-default/C-llm/D-default` 的 profile-matrix、replacement acceptance 的标准集与 runtime 扩展集、`B/C/D` 的 host-level high-value probe，以及 replacement acceptance 的 current-host / isolated WebUI 路径，都能在当前 workspace 对上代码与测试。公开口径现在可以收敛成：`WebUI acceptance 的基础 gate 仍是 6 项；current-host strict C/D 当前都是 6/6 PASS；isolated A/B/C/D 在启用可选 V7 后都是 7/7 PASS；严格的 early-flush 证明继续以 B/C/D host-level probe 为准。`

**复跑命令**：

```bash
# Layer A（Profile B only，无需外部 provider）
python scripts/test_replacement_acceptance_e2e.py --skip-profile-c

# Layer A（Full B/C/D，需要 embedding / reranker / LLM provider）
RETRIEVAL_EMBEDDING_API_BASE=... RETRIEVAL_RERANKER_API_BASE=... WRITE_GUARD_LLM_API_BASE=... \
python scripts/test_replacement_acceptance_e2e.py

# Layer B（完全隔离，需要 openclaw CLI）
ACCEPTANCE_FORCE_ISOLATED=true OPENCLAW_SCENARIO_PORT=18981 node scripts/test_replacement_acceptance_webui.mjs
```

完整报告：`backend/tests/benchmark/replacement_acceptance_summary.md`

---

## 4. 质量门禁

> **评测环境说明**：4.1–4.4 的数据来自 **maintenance-phase deterministic mock 环境**（LLM 列使用固定概率 mock，非真实 API 调用）。这些数据用于验证代码逻辑正确性和回归门禁，不等同于真实 LLM 服务下的产品质量。真实 LLM 的 6-cell ablation 结果见 4.6。

### 4.1 Write Guard (deterministic mock environment)

| 模式 | N | Precision | Recall | Exact Match |
|------|--:|----------:|-------:|------------:|
| heuristic (LLM off) | 200 | 1.000 | 0.839 | 0.895 |
| + LLM (deterministic mock) | 200 | 1.000 | 1.000 | 1.000 |

补充：

- 测试用例数：`200`（覆盖 ADD/UPDATE/NOOP 分布 35%/35%/30%）
- 改进：新增 semantic-keyword 交叉验证（高语义+低关键词 → 降级 UPDATE）
- heuristic 模式 Precision 完美，Recall 0.839（21 个 FN：UPDATE score 不够高被判 ADD）
- LLM mock 开启后 Exact Match 从 0.895 提升到 1.000；真实 LLM 环境下的结果见 4.6
- LLM 开启后，write guard 返回的 decision 会额外包含 `contradiction` 字段（布尔值），标识新内容是否与已有记忆矛盾
- contradiction 检测 benchmark：40 case，accuracy >= 0.85，precision >= 0.80，recall >= 0.80
- 真实 LLM（OpenAI-compatible chat model）矛盾检测：accuracy=0.950, precision=0.950, recall=0.950（LLM prompt 已显式引导偏好反转、模式回滚、功能禁用等矛盾模式）

### 4.2 Intent 分类 (deterministic mock environment)

| 模式 | N | Accuracy | Delta |
|------|--:|--------:|------:|
| keyword_scoring_v2 (LLM off) | 200 | 1.000 | baseline |
| + LLM (deterministic mock 92%) | 200 | 0.945 | -0.055 |

Per-intent breakdown (keyword only, N=200)：

| Intent | N | Accuracy |
|--------|--:|--------:|
| factual | 50 | 1.000 |
| exploratory | 50 | 1.000 |
| temporal | 50 | 1.000 |
| causal | 50 | 1.000 |

补充：

- 测试用例数：`200`（含 80 条中文 / 120 条英文，基于 OpenClaw 编程场景模板生成）
- 关键词扩充后（15→53 个词），keyword_scoring_v2 在 basic gold set 上达到 1.000
- LLM mock（模拟 92% 准确率）在 basic gold set 上反而 -5.5%，因 mock 以固定概率引入错误
- Product gold set（200 条，场景更复杂）上 keyword=0.910，LLM 真实效果见 4.6
- LLM prompt 已增加中文 few-shot 定义（factual/exploratory/temporal/causal 各 1 条）

### 4.3 Gist 质量 (deterministic mock environment)

| 指标 | 值 | 阈值 | 结论 |
|---|---:|---:|---|
| ROUGE-L mean | 0.976 | 0.400 | PASS |

补充：

- 测试用例数：`100`（基于 Memory Palace 组件场景生成的 reference/candidate 对）
- ROUGE-L 高是因为 candidate 为确定性改写（同义词替换），实际 LLM gist 质量会更低
- 真实 LLM gist 评测应参考 4.6 中的 diagnostic ROUGE-L 以及独立的 hybrid pairwise evaluation

### 4.4 Ablation 总结 (deterministic mock environment)

| 组件 | Heuristic (N) | + LLM mock (N) | Delta |
|------|---:|---:|------:|
| Intent Accuracy | 1.000 (200) | 0.945 (200) | -5.5% |
| Write Guard Recall | 0.839 (200) | **1.000** (200) | **+16.1%** |
| Write Guard Exact Match | 0.895 (200) | **1.000** (200) | **+10.5%** |

结论（限 deterministic mock 环境）：
- Write Guard: heuristic Precision 始终 1.000（不会误拦），但 Recall 有 gap（漏判 UPDATE）→ LLM 补齐
- Intent: 关键词扩充后 keyword_scoring_v2 在 basic gold set 上已达 1.000，LLM mock(92%) 反低 5.5%；但在 product gold set（4.6）上 keyword=0.910，LLM 通过 prompt 优化后提升到 0.940
- 这些数据基于 OpenClaw 中英文编程用户真实场景，含 40% 中文用例

改进措施：
- Intent: 扩充关键词表 15→53 个词 + LLM prompt 增加中文 few-shot 定义（已完成，4.6 验证 B-on/C-on 0.940）
- Write Guard: semantic-keyword 交叉验证（已完成）；C/D profile 的 score normalization 已实现并默认开启（EM 0.460→0.845，见 4.6.3）
- Gist: 默认 LLM 超时从 8s 提升到 45s (`COMPACT_GIST_TIMEOUT_SEC`)
- 测试基础：gold set 500 条 (200 intent + 200 write guard + 100 gist) + 3 个 host-level spot check

复跑命令：

```bash
backend/.venv/bin/pytest -xvs backend/tests/benchmark/test_quality_ablation.py
cat backend/tests/benchmark/quality_ablation_report.md
```

### 4.5 Live LLM Benchmark

上面 4.4 的 LLM 列使用 deterministic mock（模拟 92% 准确率），不走真实 API。
下面是真实 LLM API 调用的结果（需要设置 `LIVE_LLM_API_KEY`）：

复跑命令：

```bash
LIVE_LLM_API_KEY=<your-key> \
LIVE_LLM_API_BASE=<your-base-url> \
LIVE_LLM_MODEL=<your-model> \
backend/.venv/bin/pytest -xvs backend/tests/benchmark/test_quality_live_llm.py
cat backend/tests/benchmark/quality_live_llm_report.md
```

该测试会跑完全部 gold set（200 intent + 200 write guard），并输出
`quality_live_llm_report.json` 和 `quality_live_llm_report.md`。
结果因模型和 API 服务不同而变化，仅供参考。

### 4.6 Real-LLM 6-Cell Ablation (Profile B/C/D x LLM on/off)

当前真实 LLM 环境下的 6-cell ablation 结果。使用真实 embedding / reranker / LLM 服务，不使用 mock。完整报告和逐条数据见 `backend/tests/benchmark/quality_ablation_real_report.md`。

复跑命令：

```bash
# 需要配置 embedding / reranker / LLM 环境变量
backend/.venv/bin/pytest -xvs backend/tests/benchmark/test_quality_ablation_real.py
```

**Intent**（N=200 per cell）：

| Cell | Accuracy | Provider | 对比修复前 |
|---|---|---|---|
| B-off | 0.910 | keyword only | 不变 |
| B-on | **0.940** | real LLM | **+5.0%**（修复前 0.890） |
| C-off | 0.910 | keyword only | 不变 |
| C-on | **0.940** | real LLM | **+5.0%**（修复前 0.890） |
| D-off | 0.910 | keyword only | 不变 |
| D-on | skipped | — | LLM provider 临时不可用 |

注：中文 few-shot 修复后 LLM-on 反超 LLM-off（0.940 > 0.910），扭转了修复前的净负效果。

**Write Guard**（N=200 per cell）：

| Cell | Precision | Recall | EM | Provider | 说明 |
|---|---|---|---|---|---|
| B-off | 0.977 | 0.962 | 0.960 | keyword only | 基线 |
| B-on | 0.969 | 0.962 | 0.955 | real LLM | LLM 影响极小 |
| C-off | 0.650 | 1.000 | 0.370 | api embed + reranker | 阈值校准中 |
| C-on | 0.650 | 1.000 | 0.370 | api embed + reranker + LLM | 同上 |
| D-off | 0.650 | 1.000 | 0.370 | api embed + reranker | 同上 |
| D-on | skipped | — | — | — | LLM provider 临时不可用 |

注：C/D 的 write guard EM 低于 B，原因已通过 threshold sweep 定位：qwen3-embedding (1024-dim) 的 cosine similarity 存在 ~0.85 floor（min=0.846, mean=0.941），导致所有 case 都超过 UPDATE 阈值，无法通过 threshold 区分 ADD vs UPDATE/NOOP。63 点网格扫描显示 P=0.650 / R=1.000 在整个扫描范围内不变（UPDATE 阈值零影响），仅 NOOP 阈值影响 EM (0.370-0.510)。此问题**不是** threshold calibration 可解的，需要 score normalization、模型级校准或混合评分策略改进。当前保持原始阈值 (0.92/0.78) 不变，Write Guard C/D 校准列为后续任务。详细 sweep 数据见 `backend/tests/benchmark/write_guard_threshold_sweep.md`。

**Gist**（N=90 per cell，diagnostic only）：

| Cell | ROUGE-L | Method |
|---|---|---|
| B-off | 0.720 | extractive_bullets |
| B-on | 0.678 | llm_gist |
| C-off | 0.720 | extractive_bullets |
| C-on | 0.674 | llm_gist |
| D-off | 0.720 | extractive_bullets |
| D-on | skipped | — |

注：ROUGE-L 仅作 diagnostic 指标，系统性偏好 extractive。LLM gist 质量应参考 pairwise judge + factual coverage。

**Host-level spot check（3 项，验证真实宿主链路触发能力）**：

```bash
backend/.venv/bin/pytest -xvs backend/tests/benchmark/test_host_level_spot_check.py
```

| 能力 | 测试 | 结论 |
|---|---|---|
| Intent | classify_intent_with_llm 实际调用 LLM，返回 `intent_llm_applied=True` | factual=正确, causal=正确 |
| Write Guard | seed_memories → real search_advanced → threshold decision | similar→UPDATE(embedding), new→ADD |
| Compact Gist | generate_compact_gist 调用 LLM 返回 llm_gist | method=llm_gist, quality=0.98, 无降级 |

#### 4.6.1 Root-Cause Slicing

**切片维度**：Profile(B/C/D) × LLM(on/off) × intent_type × write_guard_action × language × retrieval_miss

##### Intent Root Cause

| 维度 | 观察 | 诊断 |
|---|---|---|
| **B/C/D × LLM-off** | 全部 0.910，完全一致 | Intent 分类与 profile 无关（预期行为） |
| **B/C/D × LLM-on** | B=0.890, C=0.890, D=0.865 | LLM 引入的错误是一致的，D-on 略低可能因 provider 选择时序差异 |
| **factual intent** | LLM-off: 0.94; LLM-on: 下降 | LLM 将部分 factual 误判为 exploratory（尤其中文"如何配置..."） |
| **temporal/causal intent** | LLM-off: 0.88; LLM-on: 可能改善 | LLM 对 temporal/causal 有提升潜力，但 factual 回退抵消了收益 |
| **中文 queries** | "头脑风暴"→expected exploratory, LLM→factual | 中文 exploratory 关键词未在 LLM prompt 中出现 |
| **中文 temporal** | "近期活动"→expected temporal, LLM→factual | LLM prompt 缺少中文 temporal 模式的 few-shot |
| **中文 causal** | "哪里出了问题"→expected causal, LLM→factual | 同上 |

**结论**：intent 回退是 **LLM prompt / policy 问题**（缺少中文 few-shot + 领域校准），不是 parser 或 model capability 问题。Rule-based classifier 在 product gold set 上已很强（0.910），LLM 需要 prompt 优化才能稳定超越。

##### Write Guard Root Cause

| 维度 | 观察 | 诊断 |
|---|---|---|
| **B-off vs C-off** | EM: 0.960 vs 0.510 | 核心差异来自 retrieval path，非 LLM |
| **LLM on/off** | B: 0.960→0.960, C: 0.510→0.500 | LLM 对 EM 影响 < 1%，可忽略 |
| **UPDATE→ADD** | C-off: ~40 例 | search_advanced(mode=semantic) 未召回目标 memory，因 api embedding 索引可能未就绪 |
| **UPDATE→NOOP** | C-off: ~30 例 | api embedding 分数 ≥ 0.92 NOOP 阈值，但实际应为 UPDATE |
| **NOOP→UPDATE** | C-off: ~8 例 | 反向阈值混淆 |
| **Threshold 校准** | semantic NOOP=0.92, UPDATE=0.78 | 这些阈值为 hash embedding (64-dim) 校准，api embedding (1024-dim) 分数分布不同 |
| **seed_memories 索引** | create_memory 触发索引，但可能异步/超时 | api embedding 索引需要 HTTP 调用，可能在 write_guard 执行前未完成 |

**结论**：C/D 的 EM 崩塌是 **threshold calibration 问题**，具体包括：
1. **阈值不适配**（主因）：hash embedding (64-dim) 的 cosine similarity 分布与 api embedding (1024-dim) 显著不同，固定阈值(0.92/0.78)导致 UPDATE↔NOOP↔ADD 系统性混淆
2. **索引是同步的**（Codex 交叉验证确认）：`create_memory` 默认 `index_now=True`，embedding 在 `seed_memories` 返回前已完成索引。测试中添加的 `rebuild_index` 是安全网，非必需
3. LLM guard 几乎无影响，证明 write guard 的瓶颈在检索层，不在决策层

##### Gist Root Cause

| 维度 | 观察 | 诊断 |
|---|---|---|
| **ROUGE-L: extractive vs LLM** | 0.720 vs 0.679 (B) | extractive 直接复制源文本 token → 高 ROUGE-L |
| **Dual-reference scoring** | extractive_ref 始终 >> abstractive_ref | 例: gist-p-001 extractive=0.898 vs abstractive=0.683 |
| **Pairwise judge** | 独立 canary 测试 | LLM gist 在 pairwise 中通常获胜（coverage, conciseness） |
| **Factual coverage** | 独立 canary 测试 | LLM gist coverage rate ≈ extractive |
| **LLM timeout** | 默认 8s，实际需要 ~14s | gist 生成需要更长推理时间，可能导致超时降级 |

**结论**：ROUGE-L 下降是 **指标偏差**（structural bias toward extractive），**不代表质量下降**。Pairwise judge + factual coverage 是更可靠的 gist 质量指标。额外发现：默认 HTTP 超时 8s 对推理型模型不够，可能导致 gist LLM 降级。

#### 4.6.2 修复 & 澄清清单

| 项目 | 状态 | 说明 |
|---|---|---|
| Intent LLM 中文回退 | **fixed** | LLM prompt 增加中文 few-shot 定义，B-on/C-on 从 0.890 提升到 0.940 |
| Intent/WG LLM JSON 解析 | **fixed** | `classify_intent_with_llm` 和 `_write_guard_llm_decision` 改用 `_parse_chat_json_object`（支持 `<think>` 标签、markdown code fence、unquoted keys） |
| Gist LLM 超时 | **fixed** | 新增 `COMPACT_GIST_TIMEOUT_SEC`（默认 45s），避免推理型模型超时降级 |
| Gist ROUGE-L 偏差 | **clarified** | ROUGE-L 系统性偏好 extractive，非功能退化。pairwise judge + factual coverage 是 gist 主指标 |
| Write Guard C/D EM 偏低 | **partially fixed** | Score normalization + expanded cross-check 已修复 score-compression / UPDATE→NOOP 主回归（C/D EM 0.460→0.845）。仍存在 29 个 UPDATE→ADD 残余误差，详见 4.6.3 |

#### 4.6.3 Write Guard C/D score normalization（已实现）

**根因**：qwen3-embedding (1024-dim) 的 cosine similarity 压缩在 ~0.85-1.00 窄带（min=0.846, mean=0.941），固定阈值无法区分 ADD/UPDATE/NOOP。63 点网格扫描确认 threshold tuning 不可行。

**修复方案**（已实现，对 api embedding backend 默认开启）：

1. **Score normalization for NOOP decision**：对 NOOP 判定使用归一化分数（`floor=0.85`，将 `[0.85, 1.0]` 映射到 `[0, 1]`），仅真正的近重复（raw ~1.0）才触发 NOOP。UPDATE 判定仍使用原始分数。
2. **Expanded keyword cross-check for ADD detection**：在 UPDATE 区间，若 global keyword score 极低（< 0.10），说明新内容没有任何词面证据匹配已有记忆，降级为 ADD。

**6-cell A/B 结果**（同一 DB 背靠背，200 case gold set）：

| Cell | Baseline EM | Normalized EM | ΔEM |
|---|---|---|---|
| B-off | 0.975 | 0.975 | +0.000 |
| B-on | 0.975 | 0.975 | +0.000 |
| C-off | 0.460 | **0.845** | **+0.385** |
| C-on | 0.460 | **0.845** | **+0.385** |
| D-off | 0.460 | **0.845** | **+0.385** |
| D-on | 0.460 | **0.845** | **+0.385** |

**配置**：
- `WRITE_GUARD_SCORE_NORMALIZATION`：api/router/openai backend 默认 `true`，hash backend 默认 `false`。设 `false` 可回退到原始逻辑。
- `WRITE_GUARD_NORMALIZATION_FLOOR`：归一化下限（默认 `0.85`）
- `WRITE_GUARD_CROSS_CHECK_ADD_FLOOR`：keyword cross-check 阈值（默认 `0.10`）
- `WRITE_GUARD_LLM_DIFF_RESCUE_ENABLED`：在 UPDATE/ADD 边界区做内容级二次判断（默认 `false`，需 `WRITE_GUARD_LLM_ENABLED=true` 才生效）

**已知残余误差**：29 个 UPDATE 在当前 heuristic signals 下被误判为 ADD（kw=0 且 semantic 分布与 ADD 重叠，reranker score 无区分度）。LLM content-level diff rescue（`WRITE_GUARD_LLM_DIFF_RESCUE_ENABLED`）可改善 UPDATE recall 但会损害 ADD precision，暂不适合作为默认策略。

**Follow-up**：继续优化 C/D write guard 的 UPDATE→ADD 残余误差。可探索方向：richer candidate context（完整记忆内容而非 220 字符摘要）、content-level diff prompt 改进、更强的候选打包策略、或更严格的 LLM ADD/UPDATE 判定逻辑。

---

## 5. 如何复跑

### 5.1 代码层

```bash
backend/.venv/bin/pytest -q
```

```bash
cd frontend
npm test
```

```bash
cd extensions/memory-palace
npm test
npm run typecheck
npm run build
```

### 5.2 OpenClaw 基础链路

```bash
python3 scripts/openclaw_memory_palace.py setup --mode basic --profile b --transport stdio --json
openclaw memory-palace verify --json
openclaw memory-palace doctor --json
openclaw memory-palace smoke --json
```

### 5.3 OpenClaw `Profile C/D`

```bash
python3 scripts/openclaw_memory_palace.py onboarding --profile c --json
```

```bash
python3 scripts/openclaw_memory_palace_profile_smoke.py \
  --modes local \
  --profiles c,d \
  --model-env <your-model-env-file> \
  --skip-frontend-e2e \
  --report .tmp/profile_smoke_cd.md
```

```bash
node scripts/test_openclaw_onboarding_doc_chat_flow.mjs
```

如果你想复用当前已安装的宿主而不是让脚本重新 `prepareScenario`，可以加：

```bash
OPENCLAW_ONBOARDING_USE_CURRENT_HOST=true \
  node scripts/test_openclaw_onboarding_doc_chat_flow.mjs
```

这个模式会跳过 installed 场景的 gateway 启停，直接用你当前宿主的 gateway。
它更适合做“顺手复用当前宿主”的便捷检查；如果你要拿一个更稳定、可复现的黑盒基线，仍然优先用不带这个变量的默认脚本路径。

可选环境变量：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `OPENCLAW_ONBOARDING_USE_CURRENT_HOST` | `false` | 设为 `true` 时复用当前宿主，跳过 gateway 启停 |
| `OPENCLAW_ONBOARDING_CHAT_FLOW_MAX_RETRIES` | `2` | 429 限流重试次数 |
| `OPENCLAW_ONBOARDING_CHAT_FLOW_RETRY_DELAY_MS` | `5000` | 重试基础间隔（毫秒），实际间隔 = 基础值 × 当前重试轮次 |

### 5.4 更深入的 benchmark

如果你要继续复核检索质量、factual pool cap、不同数据集或更大干扰量，直接使用：

- `backend/tests/benchmark/`

这部分属于维护和调优入口，不再作为公开首页材料展开。

---

## 6. 公开写法建议

如果你要在别的页面里写当前状态，优先直接写成：

- 当前仓库代码层测试已通过
- OpenClaw `Profile B` 基础链路已复跑
- OpenClaw `Profile C/D` 本地 smoke 已复跑
- 但 `Profile C/D` 仍然依赖用户自己提供可用模型服务
- 最终是否可用，仍以目标环境复跑为准
- 当前代码下，普通 shell 直跑 `verify / doctor / smoke --json` 时，`host-config-path` 正常也应该能 pass；如果这项又出现 warning，更应该先排查自定义 `OPENCLAW_CONFIG_PATH` / `OPENCLAW_CONFIG`、宿主配置文件路径漂移，或权限问题，而不是把它当成当前预期行为
- Replacement acceptance（Section 3.6）在最新留档复跑里，current-host strict `Profile C/D` 已达到 `6/6 PASS`，isolated `Profile A/B/C/D` 在启用可选 `V7` 后都达到 `7/7 PASS`
- 补充的 fresh isolated `Profile B` 复跑，在启用高价值短会话路径后结果已到 `7/7 PASS`，同时 harness 已经不再默认吃宿主 `~/.openclaw/openclaw.json`
- 这个结论有明确范围限定，不应被缩写成无条件的"已完全替代"

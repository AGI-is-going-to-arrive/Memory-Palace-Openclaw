> [English](00-IMPLEMENTED_CAPABILITIES.en.md)

# 00 · 当前已实现能力清单

这页现在只保留一件事：

> 固定当前已经落地、已经公开交付、已经不该再写回“未来计划”的能力边界。

先说定位：

- 这页是维护者附录
- 不是普通用户默认入口
- 普通用户优先看：
  - `README.md`
  - `docs/openclaw-doc/README.md`
  - `docs/openclaw-doc/01-INSTALL_AND_RUN.md`
  - `docs/EVALUATION.md`

再说项目口径：

- 当前仓库首先是 **OpenClaw plugin + bundled skills**
- 它做的是对宿主 memory 的增强
- 不是替代宿主自己的 `USER.md / MEMORY.md / memory/*.md`

---

## 1. 当前已经稳定成立的事实

当前不该再写成“设计中”或“下一步要补”的能力，至少包括：

- OpenClaw `memory` plugin 已落地
- 稳定用户入口是 `openclaw memory-palace ...`
- direct `skill + MCP` 路线仍保留给 `Claude Code / Codex / Gemini CLI / OpenCode`
- `memory_search / memory_get / memory_store_visual` 已是正式能力
- `setup / verify / doctor / smoke / migrate / upgrade` 已形成稳定命令面
- Dashboard 已是配套运行面，不是对外主产品首页
- review / snapshot / rollback 已在后端主链里
- visual memory 已在当前产品面里成立
- 实验性多 Agent ACL 已进入当前产品边界，但后端 / 直连 API 这一层仍待继续硬化
- `before_prompt_build` 已作为主 lifecycle hook；`before_agent_start` 只保留兼容 fallback，当前还补上了按 session 的去重标记，避免同一轮重复 recall
- durable / reflection auto recall 当前会把本 session 一起纳入检索合并，不再那么容易出现“当前会话里的上下文没带回来”
- `command:new` reflection 去重现在带 session 边界、TTL 和容量上限，不再容易在长会话里重复写入或把缓存越跑越大
- `command:new` reflection 和 smart extraction 在找不到目标 session transcript 时，当前会直接跳过（fail-closed），不再偷读“最新但无关”的 transcript
- `workflow` 相关的 profile / durable recall / host bridge prompt block 当前会先做净化；onboarding 文档路径、provider 诊断、confirmation code 这类噪声不会再被当成稳定 workflow 写回或注入 prompt
- WAL 模式已默认启用（解决并发 FTS 锁冲突）
- intent 分类支持中英文隐式模式匹配（causal/temporal/exploratory/factual 四类）
- vitality 检索排序已集成时间衰减（stale 记忆自动降权）
- 质量门禁 gold set 已扩充到 500 条（200 intent + 200 write guard + 100 gist）
- write guard LLM 模式现在会返回 `contradiction` 字段，标识新内容是否与已有记忆矛盾；LLM prompt 已显式引导矛盾判断（偏好反转、模式回滚、功能禁用等），真实 LLM 测试 Recall=0.950
- write guard 新增 score normalization：修复 Profile C/D 使用 qwen3-embedding 时 cosine similarity 压缩导致的主回归（EM 从 0.460 提升到 0.845）。对 api embedding backend 默认开启，hash backend 自动关闭，B 零回归。主回归（UPDATE→NOOP）已修复，仍有 29 个 UPDATE→ADD 残余误差；详见 `EVALUATION.md` Section 4.6.3
- 新增 contradiction 检测 benchmark（40 case）和 live LLM benchmark（400 case，支持真实 API 调用）
- 数据库迁移现在是原子的（DDL + version INSERT 在同一事务内，失败整体回滚）
- `update_memory` 写入路径增加了 stale-read 保护（`expected_old_id` 校验），多进程共享同一 DB 时不会静默覆盖
- 搜索结果 snippet 现在会高亮匹配的查询词（`<<term>>` 标记）
- write lane 新增 `lock_retries_total` / `lock_retries_exhausted` 观测指标
- 安装脚本已拆分为 `scripts/installer/` 模块包（5 个子模块），原文件保持向后兼容
- 安装脚本现在接受旧版 env var 短名（如 `EMBEDDING_API_KEY`），自动映射到正式名并输出迁移提示
- Dashboard 前端增加了输入长度限制、`prefers-reduced-motion` 支持、错误重试按钮和 ARIA 标签
- write guard 在不可用或返回异常时，不再允许 `force_write` 强行绕过（fail-closed），REST API 和 MCP 写入工具都遵守同一规则
- 本地开发免密模式（insecure local auth bypass）现在启动和请求时都会打 WARNING，提醒生产环境必须配 `MCP_API_KEY`
- 数据库 PRAGMA 配置项改为白名单验证，不再接受任意值
- host-bridge 文件读取加了防竞态保护，防止读取过程中路径被替换
- `memory_learn` 的 profile block 延后到主记录写入成功后再做；如果 profile 写入失败但主记录已落盘，整体仍报成功
- `memory_learn` 的 `force=true` 在遇到"已有记录需更新"的 guard 判定时，跳过 merge 直接创建新记录
- onboarding 工具链的密钥传递从 CLI 参数改为环境变量注入，不再在进程列表里暴露
- visual-memory 的外部进程调用做了安全加固（数组参数 + 环境变量白名单）
- Dashboard 可观测性页面拆分为 5 个子组件，主组件减负
- Dashboard 全局增加了渲染错误兜底
- 安全相关模块（prompt 过滤 / 视觉脱敏 / ACL 搜索 / 反思）新增 68 个专用单元测试
- restart 端点增加 30 秒冷却，冷却期内返回 429

---

## 2. 当前必须一起写清的边界

这些边界现在也不该再写模糊：

- `memory-palace` 可以接管 OpenClaw 的 active memory slot
- 这不等于替代宿主自己的文件记忆
- 自动 recall / auto-capture / visual auto-harvest 依赖支持 hooks 的宿主
- 当前自动链路的支持下限按代码和现有口径是 `OpenClaw >= 2026.3.2`
- 新宿主里可能还会一起挂上 `memory-core` 这类 compat shim，但只要 `plugins.slots.memory` 还是 `memory-palace`，active memory slot 就没有变
- visual context 可以自动 harvest
- 长期 visual memory 仍然是显式 `memory_store_visual`
- 这次修复管的是插件自己的 recall/capture 逻辑，不是去改 OpenClaw core；如果宿主里已经留下历史脏 workflow 数据，清理仍属于一次性维护动作
- 当前 repo 不再把 active `.github/workflows/*` 当公开验证主入口

---

## 3. 这页不再重复什么

这页不再重复下面几类内容：

- 测试数字和复跑命令
  - 统一看 `docs/EVALUATION.md`
- 安装步骤和用户路径
  - 统一看 `01-INSTALL_AND_RUN.md`
- 真实页面截图和视频
  - 统一看 `15-END_USER_INSTALL_AND_USAGE.md`
- 更细的实现拆分
  - 统一看 `docs/TECHNICAL_OVERVIEW.md`

---

## 4. 维护者下一步怎么引用

如果后续还要写维护说明，推荐按这个顺序引用：

1. `00-IMPLEMENTED_CAPABILITIES.md`
2. `07-PHASED_UPGRADE_ROADMAP.md`
3. `06-UPGRADE_CHECKLIST.md`

对应关系：

- `00`
  - 回答“已经稳定成立什么”
- `07`
  - 回答“当前阶段主链怎么读”
- `06`
  - 回答“交付面和验证纪律是否还像产品”

---

## 一句总结

> **这页现在只保留稳定事实，不再承担长篇阶段记录、测试流水账或历史方案说明。**

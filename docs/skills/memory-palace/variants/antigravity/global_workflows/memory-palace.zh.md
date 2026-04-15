> [English Source](memory-palace.md)
>
> 这是一份中文对照阅读页。真正给 Antigravity 工作流引用的运行时源文件仍是 `docs/skills/memory-palace/variants/antigravity/global_workflows/memory-palace.md`。

# /memory-palace

## 前提条件

- 当前请求确实和 Memory Palace 持久记忆、跨会话 recall、write guard、上下文压缩或索引恢复有关。
- 普通 README / UI / 测试 / 编码任务不要走这条工作流。
- 优先引用仓库里的本地 reference，不要拿泛化的 memory 建议代替。

## 输入

- 用户请求：`$ARGUMENTS`
- 仓库本地 workflow reference：`docs/skills/memory-palace/references/mcp-workflow.md`
- 仓库本地 trigger samples：`docs/skills/memory-palace/references/trigger-samples.md`

## 执行

1. 如果这是本 session 第一次真正的 Memory Palace 操作，先 `read_memory("system://boot")`。
2. URI 不明确时，先 `search_memory(..., include_session=true)`，不要直接猜路径。
3. 在任何 mutation（`create_memory`、`update_memory`、`delete_memory`、`add_alias`）之前，先读目标或最匹配候选。
4. 如果 `guard_action` 是 `NOOP`，停写，检查 `guard_target_uri` / `guard_target_id`，先读建议目标，再决定后面要不要改。
5. 如果 retrieval 质量下降，先看 `index_status()`，再决定要不要 `rebuild_index(wait=true)`。
6. 长会话或高噪声会话需要蒸馏时，用 `compact_context(force=false)`。
7. 如果用户问的是工作流本身，就用上面两个本地 reference 里的事实来回答，不要凭泛化记忆猜。

## 验证

- 适用时，答案或动作流里要提到 `read_memory("system://boot")`
- `NOOP` 必须被当成 stop-and-inspect，而不是“照常继续”
- trigger sample path 要报告成 `docs/skills/memory-palace/references/trigger-samples.md`
- 仓库可见的 reference 路径优先级高于隐藏 mirror 路径

## 输出

- 一份简洁、贴合当前仓库的回答或动作计划
- 如果缺上下文或缺运行能力，就直接说明缺什么

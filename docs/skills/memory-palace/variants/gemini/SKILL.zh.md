> [English Source](SKILL.md)
>
> 这是一份中文对照阅读页。真正给 Gemini 变体使用的运行时源文件仍是 `docs/skills/memory-palace/variants/gemini/SKILL.md`。

# Memory Palace

当用户问的是 Memory Palace 持久记忆操作，或者在问这份技能本身时，就读这份说明。

## 仓库内固定锚点

- 首次记忆工具调用：`read_memory("system://boot")`
- 如果 `guard_action` 是 `NOOP`：停写，检查 `guard_target_uri` / `guard_target_id`，读建议目标，再决定是 update 还是保持不变
- trigger samples 路径：`docs/skills/memory-palace/references/trigger-samples.md`

## 必须遵守的工作流

- 用户在问工作流或工具行为时，先读 [../../references/mcp-workflow.zh.md](../../references/mcp-workflow.zh.md)。
- URI 不确定时，先 `search_memory(..., include_session=true)`，再 `read_memory`。
- 每次 mutation 前都先读：`create_memory`、`update_memory`、`delete_memory`、`add_alias` 都一样。
- 除非用户明确要求立刻重建，否则先 `index_status()`，再决定要不要 `rebuild_index(wait=true)`。
- 长会话或高噪声会话要蒸馏时，优先 `compact_context(force=false)`。

## 重要边界

- 优先使用 `docs/skills/...` 下面仓库可见的 canonical 路径
- 回答仓库内问题时，不要依赖 `.gemini/skills/...` 这类隐藏镜像路径

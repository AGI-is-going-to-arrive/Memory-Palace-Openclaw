> [English Source](SKILL.md)
>
> 这是一份中文对照阅读页。真正被同步脚本、安装脚本和客户端镜像使用的运行时源文件仍是 `docs/skills/memory-palace/SKILL.md`。

# Memory Palace

只要任务本身涉及 Memory Palace 记忆系统，就应该读这份说明。

## 选择工具前先做什么

- 在决定具体工具前，先读 [references/mcp-workflow.zh.md](references/mcp-workflow.zh.md)。
- 如果用户问的是 Memory Palace 的行为、工作流、触发规则，不要直接靠记忆回答，先看仓库里的本地 reference。
- 如果请求本身不够清楚，先把它收敛成最小且安全的工作流，不要盲目串很多工具。

## 首次记忆工具调用

- 在一个 session 里第一次做真正的 Memory Palace 操作前，先从 `read_memory("system://boot")` 开始。
- 如果任务偏向 recall，而 boot 之后 URI 还是不明确，再继续用 `search_memory(..., include_session=true)`。

## 新上下文规则

- 不要默认子代理、全新 CLI session、重试后的会话会继承父会话全部上下文。
- 只要上下文重要，就显式重新加载 Memory Palace 状态：`system://boot`、`search_memory(...)` 或定向 `read_memory(...)`。
- 只有互不影响的读取、搜索、镜像检查可以并行。互相冲突的写入和重叠编辑必须串行。

## 不能省略的规则

- 一个 session 里第一次真实记忆操作前，先 `read_memory("system://boot")`。
- URI 不确定时，先 `search_memory(..., include_session=true)`，再 `read_memory`。
- 每次变更前都先读：`create_memory`、`update_memory`、`delete_memory`、`add_alias` 都一样。
- guard 已经指向现有记忆时，优先 `update_memory`，不要重复 `create_memory`。
- `guard_action=NOOP|UPDATE|DELETE` 不是提示语，而是必须停下来检查的信号。
- 如果 `guard_action` 是 `NOOP`，就停写，检查 `guard_target_uri` / `guard_target_id`，先读建议目标，再决定要不要继续。
- 把 `guard_target_uri` 和 `guard_target_id` 当作真正的目标提示，不要靠模糊感觉去猜。
- `compact_context(force=false)` 只用于长会话或高噪声会话的压缩，不要当成常规动作。
- 除非用户明确要求立刻重建，否则先 `index_status()`，再决定要不要 `rebuild_index(wait=true)`。
- 涉及删除、迁移、别名这类结构性变更时，结束前顺手说清回看路径或回滚路径。

## 默认工作流

1. 先 boot 当前记忆上下文。
2. 先 recall 候选目标。
3. 再读精确目标。
4. 只在确认之后做变更。
5. 只有症状足够明确时才 compact 或 rebuild。
6. 最后用一句短总结交代改了什么、碰了哪些 URI。

## 这份技能覆盖哪些工具

### 核心工具

- `read_memory`：按 URI 读取记忆，首次通常从 `system://boot` 开始
- `search_memory`：按 query 找记忆
- `create_memory`：创建新记忆
- `update_memory`：更新已有记忆
- `delete_memory`：删除记忆

### 维护工具

- `add_alias`：给已有记忆加 URI 别名
- `compact_context`：把长会话压成短摘要
- `compact_context_reflection`：压缩并写入 reflection lane，通常由插件自动调用
- `rebuild_index`：重建搜索索引，前面先看 `index_status`
- `index_status`：查看索引状态
- `ensure_visual_namespace_chain`：补齐可视化命名空间链路

## 什么时候打开 reference

需要下面这些信息时，打开 [references/mcp-workflow.zh.md](references/mcp-workflow.zh.md)：

- 精确的工具选择规则
- write guard 的处理顺序
- 什么时候 compact，什么时候 rebuild
- review、snapshot、maintenance 的最小要求
- 11 个 MCP 工具与它们最安全的使用顺序

需要看 should-trigger / should-not-trigger 样例时，打开 [references/trigger-samples.zh.md](references/trigger-samples.zh.md)。

如果某些 CLI 能加载 skill，但不能稳定读取隐藏目录里的镜像文件，优先引用仓库可见的 canonical 路径，不要依赖 `.gemini/skills/...`、`.codex/skills/...` 这类隐藏镜像路径。

## 示例

- 应触发：“帮我把这条长期偏好记到 Memory Palace。”
- 应触发：“查一下之前记过没有，再决定 create 还是 update。”
- 应触发：“为什么 `guard_action=NOOP`，下一步该怎么做？”
- 不应触发：“把 README 第一段改得更顺一点。”
- 不应触发：“解释一下 Python 的内存管理。”

## 排障

- 如果写入被 `guard_action=NOOP` 挡住，先停，检查 `guard_target_uri` / `guard_target_id`，读建议目标，再决定下一步。
- 如果干净会话、子代理或重试丢了上下文，就重新 `read_memory("system://boot")`；URI 仍不明确时，再 `search_memory(..., include_session=true)`。
- 如果某个 CLI 能加载 skill，但读不到隐藏 skill 目录，就用 `docs/skills/...` 下面这些仓库可见 canonical 路径来回答。

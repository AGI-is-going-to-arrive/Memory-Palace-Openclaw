> [English Source](mcp-workflow.md)
>
> 这是一份中文对照阅读页。真正被运行时引用的 canonical path 仍是 `docs/skills/memory-palace/references/mcp-workflow.md`。

# Memory Palace MCP 工作流

这份文件是 `memory-palace` skill 的操作 reference。

## 路径规则

- 运行时 canonical path：`docs/skills/memory-palace/references/mcp-workflow.md`
- 这份中文对照阅读页：`docs/skills/memory-palace/references/mcp-workflow.zh.md`
- trigger samples 的运行时 canonical path：`docs/skills/memory-palace/references/trigger-samples.md`
- trigger samples 的中文对照阅读页：`docs/skills/memory-palace/references/trigger-samples.zh.md`
- 如果某个 CLI 能加载 skill，但不能直接读取 `.gemini/skills/...`、`.cursor/skills/...` 这类隐藏镜像目录，优先引用这些仓库可见路径

## 核心意图映射

- **启动 session 上下文** → `read_memory("system://boot")`
- **按主题或模糊提示找记忆** → `search_memory`
- **检查一条具体记忆** → `read_memory`
- **新建记忆** → `create_memory`
- **修改现有记忆** → `update_memory`
- **删除记忆** → `delete_memory`
- **安全重命名或迁移路径** → 先 `add_alias`，再 `delete_memory`
- **压缩冗长 session 上下文** → `compact_context`
- **检查或修复搜索 / 索引状态** → 先 `index_status`，再 `rebuild_index`
- **提前补齐可视化命名空间** → `ensure_visual_namespace_chain`

## 默认安全顺序

### 1. Boot

在一个 session 里第一次做真正的记忆动作前，先跑：

```python
read_memory("system://boot")
```

### 2. 先 recall 再 read

如果用户给的是主题，不是精确 URI：

```python
search_memory(query="...", include_session=True)
read_memory("best-match-uri")
```

### 3. 先读再写

不要一上来就 mutate。

```python
read_memory("target-or-parent-uri")
update_memory(...)
```

只有在目标确实不存在时才用 `create_memory(...)`。

### 4. 带 guard 地写

`create_memory` 或 `update_memory` 返回这些字段时，要检查：

- `guard_action`
- `guard_reason`
- `guard_method`
- `guard_target_uri`
- `guard_target_id`

操作规则：

- `NOOP` → 停下来，先检查建议目标
- `UPDATE` → 先读建议目标，通常改走 `update_memory`
- `DELETE` → 停下来，先确认旧记忆是否真的该被替换

如果返回了 `guard_target_id`，判断“是不是同一条记忆”时优先用它，不要依赖模糊相似度。

### 5. Compact 还是 rebuild

下面这些情况，用 `compact_context(force=false)`：

- session 太长
- notes 太吵
- 你需要的是一份蒸馏摘要，而不是修索引

下面这些情况，先 `index_status()`，必要时再 `rebuild_index(wait=true)`：

- 连续出现 retrieval degrade
- 怀疑 index freshness 不对
- search 质量明显低于正常水平

## 并行边界

- 可以并行：互不影响的 recall、互不影响的 search probe、mirror drift check
- 必须串行：重叠写入、alias/delete 迁移、或者同一套 skill/docs 的重叠编辑

## 工具清单

这份技能要保持对齐的 11 个 MCP 工具：

- `read_memory`
- `create_memory`
- `update_memory`
- `delete_memory`
- `add_alias`
- `search_memory`
- `compact_context`
- `compact_context_reflection`
- `rebuild_index`
- `index_status`
- `ensure_visual_namespace_chain`

## 面向 review 的总结模板

做完一次 Memory Palace 操作后，至少交代：

- 走的是哪条工作流
- 读了哪些 URI
- 改了哪些 URI
- 有没有 guard 中途拦截
- 有没有触发 compact 或 rebuild

## Trigger 样例

### 应触发

- “帮我把这条用户偏好写进 Memory Palace”
- “先从 system://boot 读一下，再帮我查最近这类记忆”
- “这个记忆可能重复了，帮我判断是 update 还是 create”
- “最近 search 质量下降了，帮我看看要不要 rebuild_index”
- “我想清理长会话，把它压缩成 notes”

### 不应触发

- “给我重写 README”
- “修一下前端按钮样式”
- “帮我分析 benchmark 结果”
- “更新 docs/skills 的文字说明”

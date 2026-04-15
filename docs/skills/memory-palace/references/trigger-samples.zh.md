> [English Source](trigger-samples.md)
>
> 这是一份中文对照阅读页。真正被运行时回答和测试脚本引用的 canonical path 仍是 `docs/skills/memory-palace/references/trigger-samples.md`。

# Memory Palace Trigger 样例集

这份文件是 `memory-palace` skill 的 trigger 评估样例集。

## 这份样例集是干什么的

- 检查该触发的时候会不会触发
- 检查不该触发的时候会不会保持安静
- 检查触发之后，第一步动作是不是对的
- 给未来的 trigger regression 留下一套稳定 prompt

## 怎么用

对每一条样例：

1. 判断这个 skill **应不应该触发**
2. 如果应触发，检查第一步动作是否符合预期工作流
3. 如果不应触发，检查 agent 是否保持在普通工作流里，没有误切到 Memory Palace 专属行为

## 应触发

### T01

- Prompt: `帮我把“用户偏好简洁回答”写进 Memory Palace，并避免重复创建。`
- Why: 明确要写记忆，而且要求去重
- Expected first move: `read_memory("system://boot")`，然后 `search_memory(..., include_session=true)`

### T02

- Prompt: `先从 system://boot 读一下，再帮我查最近关于部署偏好的记忆。`
- Why: 明确给出了 Memory Palace URI 和 recall 语义
- Expected first move: `read_memory("system://boot")`

### T03

- Prompt: `这条记忆可能已经存在，帮我判断应该 create 还是 update。`
- Why: 明确是 write-guard / dedupe 判断
- Expected first move: recall 或 read，不能先 mutate

### T04

- Prompt: `最近 search_memory 结果不太对，帮我看看要不要 rebuild_index。`
- Why: 明确是 retrieval degrade 诊断
- Expected first move: 先检查检索状态，通常是 `index_status()`

### T05

- Prompt: `把这段很长的会话压缩进 notes，别直接丢信息。`
- Why: 明确是 context compaction 工作流
- Expected first move: 先 boot 或检查当前上下文，再 `compact_context(force=false)`

### T06

- Prompt: `帮我把 core://agent 下这条规则迁移到新路径，并保留旧别名。`
- Why: 明确是 alias + delete 的迁移流
- Expected first move: 先读目标，再 `add_alias`，最后做受控清理

### T07

- Prompt: `跨会话回忆一下我们之前记住的发布口径。`
- Why: 明确要做跨 session 长期记忆 recall
- Expected first move: `read_memory("system://boot")` 或 `search_memory(..., include_session=true)`

### T08

- Prompt: `这个 Memory Palace 写入被 guard 拦截了，帮我找真实目标。`
- Why: 明确是 guard 处理
- Expected first move: 检查 `guard_target_uri` / `guard_target_id`，然后 `read_memory(...)`

### T09

- Prompt: `请排查 index_status 里的 degrade_reasons，并给出恢复顺序。`
- Why: 明确是 maintenance / recovery 语义
- Expected first move: `index_status()`

### T10

- Prompt: `更新一下这个仓库里的 Memory Palace skill 文档，让它和真实 MCP 工作流一致。`
- Why: 明确是这个仓库里的 Memory Palace skill 维护
- Expected first move: 先打开 canonical skill/docs reference，而不是只按普通 docs 编辑处理

## 不应触发

### N01

- Prompt: `给我重写 README 的开头介绍。`
- Why: 普通文档编辑，不是 Memory Palace 操作
- Expected behavior: 不要进入 Memory Palace MCP 工作流

### N02

- Prompt: `修一下前端按钮 hover 样式。`
- Why: 纯 UI 任务
- Expected behavior: 不要做 Memory Palace 专属工具规划

### N03

- Prompt: `帮我分析 benchmark 图表。`
- Why: 只是评测讨论，不是记忆操作
- Expected behavior: 保持普通分析模式

### N04

- Prompt: `把这个 Docker 脚本改成支持 arm64。`
- Why: 纯部署 / 代码任务
- Expected behavior: 不要走 boot / search / update 记忆流

### N05

- Prompt: `写一个新的 skill 给 UI 设计用。`
- Why: 这是技能编写任务，不是 Memory Palace 记忆系统本身
- Expected behavior: 不要走 Memory Palace 工具工作流

### N06

- Prompt: `帮我整理 llmdoc 的目录结构。`
- Why: 文档系统任务，和记忆操作无关
- Expected behavior: 不要触发 Memory Palace

### N07

- Prompt: `看看这个 API 为什么 500。`
- Why: 后端排障，不带记忆语义
- Expected behavior: 走普通调试流，不走 Memory Palace 工作流

### N08

- Prompt: `为这个组件补单元测试。`
- Why: 普通编码 / 测试任务
- Expected behavior: 不要触发 Memory Palace

### N09

- Prompt: `把这段英文翻译成中文。`
- Why: 纯语言任务
- Expected behavior: 不要触发 Memory Palace

### N10

- Prompt: `总结一下这篇博客。`
- Why: 普通总结任务
- Expected behavior: 不要触发 Memory Palace

## 边界样例

这些样例适合在调 `description` 时用。

### B01

- Prompt: `把这次调试结论记下来。`
- Why: 语义有歧义，可能是本地笔记，也可能是 Memory Palace
- Preferred decision: 只有上下文明确指向 Memory Palace 或项目长期记忆时才触发

### B02

- Prompt: `回忆一下我们刚刚说过的话。`
- Why: 可能只是当前聊天上下文，不一定是长期记忆
- Preferred decision: 除非用户明确说的是跨 session recall，否则不要触发

### B03

- Prompt: `帮我保存这个结论，后面还要用。`
- Why: “保存”也可能指文件、issue comment 或 Memory Palace
- Preferred decision: 只有用户明确想做 durable memory persistence 时才触发

### B04

- Prompt: `这个知识库是不是该重建一下。`
- Why: “知识库”可能是 docs index、vector DB，也可能是 Memory Palace index
- Preferred decision: 只有已经建立起 Memory Palace retrieval / index 语境时才触发

## Review Checklist

- T 系列 prompt 是否都触发了？
- N 系列 prompt 是否都保持静默？
- T 系列 prompt 触发之后，第一步动作是否正确？
- B 系列 prompt 的判断是否保守，而且理由站得住？

> [English](03-PROFILES_AND_DEPLOY.en.md)

# 03 · Profile 与部署怎么选

这页只回答一个问题：

> **OpenClaw 用户到底该先用哪个 Profile。**

如果你想看完整环境变量和高级调参，直接去：

- [../DEPLOYMENT_PROFILES.md](../DEPLOYMENT_PROFILES.md)

<p align="center">
  <img src="../images/profile_ladder_bilingual_4k.png" width="1100" alt="Profile A/B/C/D 双语能力阶梯图" />
</p>

---

## 1. 先说结论

### Profile A

- 最保守
- 纯关键词
- 只适合最低配验证

### Profile B

- 默认起步档
- `hash` embedding（dim=64）
- 不依赖真实外部 embedding / reranker，不需要 provider-probe
- 最适合先把插件链路跑通

### Profile C

- provider-backed retrieval 档
- 需要真实 embedding / reranker
- 默认开 embedding + reranker
- LLM 辅助不是默认强开；交互安装时会明确问你要不要开启一组可选 LLM 辅助
- 这组可选 LLM 目前对应：
  - `write_guard`
  - `compact_gist`
  - `intent_llm`
- 适合已经准备好本地或内网模型服务的人

### Profile D

- 完整高级目标档
- 需要真实 embedding / reranker / LLM
- 默认目标是 embedding + reranker + LLM 辅助全开
- 可以是本地、内网或远程 provider；关键不是部署拓扑，而是三条 provider 链路都健康
- 质量强，但时延也更高

一句话总结：

- **先跑通：B**
- **先把检索升级上去：C**
- **embedding / reranker / LLM 都准备好后再上全功能高级面：D**

---

## 2. 当前公开基线怎么读

当前更稳的读法，是把**用户页证据**和**完整复跑记录**分开看：

- 用户页只保留用户真正需要知道的结论
- 完整命令、次数、宿主版本、跳过项统一看 [../EVALUATION.md](../EVALUATION.md)
- 最新留档复跑已经再次确认：
  - `openclaw plugins inspect memory-palace --json` 可以看到 plugin 已加载
  - 同一份 onboarding 文档已经验证过可以在 CLI / WebUI、未安装 / 已安装、中英文这些主分支里给出正确下一步
  - `Profile C / D` 的 repo wrapper `onboarding --apply --validate` 路径在最新留档复跑中已通过
  - 最新一轮 profile-matrix 记录里，已经复现当前实验性 `A / B / C / D + ACL` 行为

这页当前可以公开引用的复跑事实是：

- onboarding 文档直交 OpenClaw 的 `CLI / WebUI` 两条分支，在最新留档复跑中已经通过
- 这组结果当前应按“**聊天里的下一步指导通过**”理解，而不是“只靠一轮聊天已经把最终 profile apply 全部做完”
- `Profile C` 当前默认值明确是 `embedding + reranker`
- `Profile C` 的 `write_guard / compact_gist / intent_llm` 仅在 opt-in 时启用
- `Profile D` 当前默认值明确包含 `write_guard / compact_gist / intent_llm`
- 当前兼容层已经接好，不会改掉 `plugins.slots.memory=memory-palace`
- `Profile C / D` 继续依赖你自己的 provider 健康状态；不是“填了 env 就已经 provider-ready”
- 最新留档的 shell / onboarding / profile-matrix 复跑都对上了当前代码行为；如果你的目标模型端点不健康，`doctor / smoke` 仍然可能返回 `warn`

这些记录并不是在讲“WebUI 会长出一套新页面”，而是在说明：

- `Profile B` 更像安全 bootstrap 基线
- `Profile C` 更像 provider 真正健康之后的长期使用档
- `Profile D` 是把 `write_guard / compact_gist / intent_llm` 一起推到默认高级面

完整复跑命令和上下文统一看：

- [../EVALUATION.md](../EVALUATION.md)

### 当前基线里的时间和平台怎么理解

对用户更稳的说法是：

- **第一次完整安装**仍然不要理解成秒装
- 同一台机器后续重复安装 / 复核通常会比第一次快很多
- 如果只是把 onboarding 页面交给 OpenClaw，让它先给出下一步，聊天返回速度和“真正完成安装”的耗时不是一回事

平台边界也要说清：

- 当前公开摘要对应的最新**真实宿主**完整复跑，已经包含 **Windows native**
- 较早留档的 **macOS** 真实宿主复跑仍然保留为增量维护者证据
- 这轮也补上了 Docker 里的 **Linux userspace** 复跑：`linux/aarch64` 和 `linux/amd64` 都已经实际跑过 `setup --profile b`、`setup --profile d`、`onboarding --profile d --strict-profile --apply --validate`
- 仓库本身仍然提供 **Linux / Windows** 的模板和验证路径
- 但如果你要把“现在可用”写成某台目标机器上的事实，还是应该在那个目标环境再跑一次
- 即使现在已经有 **Windows native** 的留档基线，如果你要把“这台目标机已经 ready”写成事实，还是建议在目标 Windows 主机上直接重跑同一条
  `setup -> verify -> doctor -> smoke` 链路
- 更细的 Windows 验证附录属于维护者材料，不在公开用户文档集里

---

## 3. OpenClaw 用户最稳的选择顺序

### 第一步

先用：

- `python3 scripts/openclaw_memory_palace.py setup --mode basic --profile b --transport stdio --json`

如果你在 Windows PowerShell 里跑，这条 repo wrapper 直接写成 `py -3 scripts/openclaw_memory_palace.py setup --mode basic --profile b --transport stdio --json`。

### 第二步

确认下面这几条能成功跑完：

- `openclaw memory-palace verify --json`
- `openclaw memory-palace doctor --json`
- `openclaw memory-palace smoke --json`

如果结果是 `ok=true`，但 `status=warn`，更稳的理解是“已经通过，但带注意事项”；先把告警看清，再决定是不是已经适合日常使用。

### 第三步

只有在你已经准备好自己的模型服务时，再切到：

- `python3 scripts/openclaw_memory_palace.py setup --mode basic --profile c --transport stdio --json`
- `python3 scripts/openclaw_memory_palace.py setup --mode basic --profile d --transport stdio --json`

如果你在 Windows PowerShell 里跑，这两条 repo wrapper 也统一改成 `py -3`。

说人话就是：

- **Profile C** 适合先把检索升级到 provider-backed
- **Profile D** 适合 embedding / reranker / LLM 都已经准备好，并且你想默认打开完整高级能力

如果你使用的是支持多输出维度的 embedding 路线：

- `provider-probe` 会先探测当前 provider/model 的可用维度上限
- 最终 `RETRIEVAL_EMBEDDING_DIM` 应按 probe 结果配置

不要把模板里的默认值机械当成所有 provider/model 的最终值。

这也是为什么这页反复强调：

- `Profile C/D` 的门槛不是“你已经把 API 地址写进 env”
- 真正门槛是 `provider-probe`、`verify`、`doctor`、`smoke` 这些检查在你的环境里都能成功跑完
- 如果这些检查里还有 `warn`，更稳的说法是“已经通过，但带注意事项”，先看清告警再决定是否把环境写成 ready
- 如果你看的正好是 package/tgz 安装验证，还要把 `smoke_mode=seeded_retrieval` 和 `stdio_capture_* / sse_*` 分开读；前者是 seeded retrieval 烟测，后者才是当前真实 capture/profile 结论
- 如果 package/tgz 验证里刚好是重复写同一条稳定 workflow，profile block 这次没重写也不一定是错；先看 capture 有没有被处理，再看原来的 profile block 还能不能正常读

---

## 4. 需要记住的边界

- `A/B/C/D` 主要影响的是 retrieval 深度和相关高级能力默认值
- `Profile C` 不应被理解成“默认 LLM 全开”
- `Profile D` 也不应再被理解成“只有 write_guard 用到 LLM”；当前安装器会把共享 LLM 复用到 `write_guard / compact_gist / intent_llm`
- 自动 recall / auto-capture / visual auto-harvest 并不只属于某个单独 profile
- 这条自动链路本身仍然依赖支持 hooks 的 OpenClaw 宿主
- public 文档不写任何私有模型地址、私有 key、私有 env 路径

---

## 5. 什么时候回去看总部署文档

如果你已经不是“选档位”，而是要看：

- `.env` 参数
- Docker
- reranker / embedding / LLM 配置
- 调优建议

直接看：

- [../DEPLOYMENT_PROFILES.md](../DEPLOYMENT_PROFILES.md)

---

## 延伸阅读：25 号总说明

如果你已经不只是想“选哪个 profile”，而是想一次看懂：

- `memory-palace` 到底接管了 OpenClaw 的哪一层
- plugin、skills、MCP、backend 怎么分工
- 写入链路、召回链路、ACL 隔离链路怎么串起来
- `Profile A / B / C / D` 的真实产品语义和能力边界

直接看：

- [25-MEMORY_ARCHITECTURE_AND_PROFILES.md](25-MEMORY_ARCHITECTURE_AND_PROFILES.md)

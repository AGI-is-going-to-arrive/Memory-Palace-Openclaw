> [English](04-TROUBLESHOOTING.en.md)

# 04 · 常见问题排查

这页只保留普通用户和自托管使用时最常见的问题。

先说一个总原则：

- 先判断是不是**当前稳定入口**的问题
- 再判断是不是环境、provider、Docker 或浏览器那一层的问题
- 维护者脚本（例如长跑 gate、额外 e2e）不应拿来当普通用户的第一判断标准

---

## 1. `openclaw memory ...` 看起来还是宿主老逻辑

先说结论：

- 当前默认保证的是 `openclaw memory-palace ...`
- 不是宿主默认的 `openclaw memory ...`

所以如果你看到：

```bash
openclaw memory status
```

还是宿主 builtin memory 的输出，这不代表插件没装好。

先用：

```bash
openclaw memory-palace status
```

把这条稳定入口跑通，再去判断宿主是否支持更进一步的委托。

---

## 2. 升级到 OpenClaw 2026.4.5+ 后，连宿主命令都坏了

先说结论：

- 这时先别查 `Memory Palace` 主链
- 先确认是不是宿主自己的 install / bundled extension runtime 已经坏了

先查：

```bash
openclaw --version
openclaw doctor --help
openclaw plugins list --json
openclaw status --json
openclaw health --json
```

如果你这次还要走本机 gateway / Control UI / 浏览器链路，再补一条：

```bash
openclaw gateway status --json
```

如果上面这些命令本身就报错、卡住，或者直接出现 `Cannot find package ...` / `Cannot find module ...`，先把它当成宿主问题，不要继续按插件 transport / provider 故障去排。

这时更像是：

- 宿主升级后 bundled extension runtime 依赖没补齐
- 或宿主自己的 plugin path / install 状态没修好

这一步没过时，不要继续往下跑 `setup / verify / doctor / smoke`，先把宿主修到这些基础命令恢复正常。

如果你只是想先把宿主恢复到“能正常判断问题”的状态，当前更保守的顺序是：

1. 先跑：

```bash
openclaw doctor --fix
```

2. 再把上面的宿主自检命令重跑一遍
3. 如果还是坏，用你原来的安装方式重装当前 OpenClaw 版本，再重跑宿主自检
4. 只有这一步转绿了，再回来查 `memory-palace`

---

## 3. `Unable to connect to Memory Palace MCP over the configured transports`

更常见的原因是：

- 源码仓 load path 场景下，插件依赖没装
- 没走 `setup`，但直接要求 wrapper 去找用户态 runtime
- `DATABASE_URL` 不对
- 你以为在走 SSE，但 `sse.url` 或 `MCP_API_KEY` 没配好
- 你装的是当前仓库自己打出来的 local tgz，但在 `OpenClaw 2026.4.5+` 下没带 `--dangerously-force-unsafe-install`

先查：

```bash
cd extensions/memory-palace
npm install --no-package-lock
```

如果你走普通用户推荐路径，再查：

```bash
python3 scripts/openclaw_memory_palace.py setup --mode basic --profile b --transport stdio --json
openclaw plugins inspect memory-palace --json
openclaw memory-palace status --json
```

如果你在 Windows PowerShell 里跑，这条 repo wrapper 直接写成 `py -3 scripts/openclaw_memory_palace.py setup --mode basic --profile b --transport stdio --json`。

这页后面再出现的恢复 / 重试 repo wrapper 命令，在 Windows PowerShell 里也统一把 `python3` 改成 `py -3`，包括 `provider-probe` 和 `onboarding --apply --validate`。

如果你装的是当前仓库自己打出来的 local tgz，再确认安装命令是不是：

```bash
openclaw plugins install --dangerously-force-unsafe-install ./openclaw-memory-palace-<version>.tgz
```

注意：

- 这条 `--dangerously-force-unsafe-install` 只给**你刚从当前仓库打出来的本地 tgz**用
- 不要拿它去安装来源不明的第三方插件包
- 如果你已经带了这条参数，但当前宿主版本上仍卡在 `openclaw plugins install`，先不要硬把它理解成你本地 profile/provider 配错了
- 当前记录在案的 `OpenClaw 2026.4.9` 基线里，这条 local tgz 路径已经重新转绿；所以如果你自己环境里仍失败，更像是宿主版本差异、npm/pip 网络、或本机 clean-room 环境差异，需要按 install 路径单独排
- 同一轮后段，维护者当前宿主的 `CLI / gateway / verify` 也又按 `OpenClaw 2026.4.11` 重新复核过一次；不要把上面的 `2026.4.9` tgz 结果误读成“当前宿主版本只能是 4.9”

---

## 4. `/setup` 里点 `Apply` 返回 `401/403`

先说结论：

- 这通常不是插件主链坏了
- 更常见的是 `/bootstrap/*` 的门控和你以为的 dashboard key 不是一回事

当前边界是：

- `/bootstrap/*` 只允许本机 loopback
- 如果 backend 已经配置了 `MCP_API_KEY`
  - `Apply / Restart Backend` 也要带这把 key
- 右上角的 `Set API key`
  - 在当前 loopback / 同源页面链路里，前端会把它带到 `/bootstrap/provider-probe`、`/bootstrap/apply`、`/bootstrap/restart`
  - 但它不会绕过 `/bootstrap/*` 自己的 loopback / 同源限制

先查：

```bash
openclaw memory-palace status --json
openclaw memory-palace verify --json
```

然后确认：

- 当前是不是直连本机 loopback
- backend 有没有配置 `MCP_API_KEY`
- 失败的是不是 `/bootstrap/apply`

---

## 5. `/setup` 点了 `Restart Backend`，页面一直不回来

更常见的原因是：

- 旧 backend 进程还没退掉
- 旧端口没释放
- 或新 backend 没真正拉起来

如果你当前就是在**源码仓 checkout** 里跑这条链路，helper 超时或启动失败时，通常会把原因写到：

```text
./.tmp/bootstrap-restart-supervisor.log
```

源码仓场景先查：

```bash
tail -n 50 ./.tmp/bootstrap-restart-supervisor.log
openclaw memory-palace verify --json
```

如果你不是源码仓场景，或者这个文件根本不存在：

- 不要先按“路径不对 = 插件坏了”理解
- 更稳的是直接回到 `verify / doctor`
- 再看宿主当前是源码仓 `setup`、本地 tgz，还是别的安装形态

如果 `verify` 仍然是 `pass`，更像是 restart 交接没接好，不是插件主链本身坏了。

---

## 6. Docker smoke 一开始就报 `Docker daemon unavailable`

这通常不是插件配置错了，而是：

- Docker Desktop / daemon 没启动
- 当前 shell 连不到 Docker socket

先查：

```bash
docker info
docker compose version
```

如果 `docker info` 都不通，先别查插件。

---

## 7. `verify=pass`，但 `doctor / smoke=warn`

先说结论：

- **不一定是坏了**
- fresh runtime 下出现 `warn but ok=true`，更常见地表示还没 seed 到可读目标

更常见的 warning 是：

- `last-rule-capture-decision`
- `search-probe`
- `profile-memory-state`
- `host-plugin-split-brain`
- `read-probe target missing`

当前正常安装后的 shell 直跑里，`host-config-path` 这项本身应该能过。
如果它还在 warning，更应该先查：

- 你是不是手动设了一个失效的 `OPENCLAW_CONFIG_PATH` / `OPENCLAW_CONFIG`
- 当前宿主配置文件是不是被移走、重命名，或者权限不可读

> 在已有记忆数据的 runtime 下，`doctor` / `smoke` 的 `search_memory probe` 返回少量命中（如 3 条）是正常基线，不表示检索异常。

说人话就是：

- `verify=pass`
  - 更像 wiring / transport / `index_status` 主链已经通了
- `doctor / smoke=warn`
  - 在空库、空工作区、fresh runtime 下并不稀奇

如果你看的正好是 `python3 scripts/test_openclaw_memory_palace_package_install.py` 这条 package-install 结果，还要先把两条 `stdio` 结果分开读：

- `smoke_status` + `smoke_mode=seeded_retrieval`
  - 这条只证明 seeded durable memory 还能被 `search + read`
  - 这里继续出现 `profile-memory-state / capture-layer-distribution / host-plugin-split-brain` 的 `warn`，当前是预期结果，不单独代表 profile block 失败
- `stdio_capture_verify / doctor / smoke`
  - 这条才是当前 package-install 里的真实 profile/capture 验证
  - 当前记录在案的基线已经是 `pass`

如果你想把这类 `warn` 收敛掉，当前更有效的顺序是：

1. 先 seed 一条稳定可读目标
2. 再显式带 `--path-or-uri` 跑 smoke
3. 如果还是 `host-plugin-split-brain`
   - 先给它一个正常 host workspace

---

## 8. `Profile C/D` 配了还是不工作

更常见的真实原因是：

- embedding / reranker / LLM 服务本身不可用
- `RETRIEVAL_EMBEDDING_DIM` 跟真实模型不一致
- endpoint / key / model 名写错

更稳的排法是：

1. 先确认 `Profile B` 主链能通
2. 再补真实模型配置
3. 再看 `verify / doctor / smoke`

记住一条边界：

- `C/D` 已有已记录的本地 smoke 基线
- 但它们不等于“所有用户环境零配置可跑”

---

## 9. `request timeout after ... during index_status/search_memory`

这通常更像：

- backend 当下很慢
- 外部 embedding / reranker 很慢
- 或 transport 没能在超时时间内返回

先看：

```bash
openclaw memory-palace status --json
openclaw memory-palace doctor --json
```

再判断是：

- transport 问题
- index worker 问题
- provider 响应慢

---

## 10. `memory_store_visual` 为什么有时会 merge、reject，或者新建 `--new-01`

这不是随机行为。

当前视觉写入有重复策略：

- `merge`
  - 合并到已有记录
- `reject`
  - 明确拒绝重复写入
- `new`
  - 新建一个变体路径，例如 `--new-01`

如果你看到结果不符合预期，先查：

- 当前 duplicate policy
- 当前 visual 内容是不是本来就被判定为同一条记录

---

## 11. visual search 为什么有时先看到 namespace，再看到正文

这通常不是检索坏了，而是：

- namespace 容器本身也在结果里
- 真正正文记录排在后面

如果你只是想判断“有没有找到正文”，不要只看第一条，继续往下看实际 record。

---

## 12. 单独在插件目录里跑检查，报 `openclaw/plugin-sdk/...` 找不到

更常见的原因是：

- 你没在正确目录安装依赖
- 或 `dist` 产物还没更新

先做：

```bash
cd extensions/memory-palace
npm install --no-package-lock
npm test
npm run typecheck
npm run build
```

如果只是刚改了插件代码，不重建 `dist`，真实 OpenClaw 仍可能继续读旧产物。

---

## 13. `openclaw agent --local ...` 报 `session file locked`

这通常不是 onboarding skill 坏了，而是：

- 同一个 agent 的 session 文件正在被别的 OpenClaw 进程占用

更常见的来源是：

- 这个 session 已经在 WebUI 里打开
- 另一个本地 CLI / gateway 进程正在写同一个 session

更稳的处理顺序是：

1. 换一个新的 `--session-id`
2. 或新建一个临时 agent 做这轮测试
3. 或先关掉当前占用这个 session 的 UI / CLI 进程

---

## 14. `Concurrent modification detected`

如果你在写入或编辑记忆时看到这个错误：

先说结论：

- 这说明你读到了一份旧版本的记忆，但在你提交修改之前，另一个进程已经更新了它
- 这是正常的并发保护行为，不是 bug

更稳的处理顺序是：

1. 重新读取当前内容
2. 基于最新内容重新编辑
3. 再提交

如果你是单人单机使用，频繁出现这个错误更可能是：
- 同时开了多个 MCP server 进程写同一条记忆
- 或 Dashboard 和 CLI 同时在写同一条

---

## 14. onboarding skill 已加载，但对话里报 `401` / `auth_unavailable`

如果你已经确认：

- `Skills` 页面里已经出现 Memory Palace 相关条目
- `memory_onboarding_status / probe / apply` 已经注册到插件工具面

但真实对话仍然报：

```text
HTTP 401 ...
```

或：

```text
auth_unavailable
```

更准确的理解通常是：

- onboarding skill 本身已经加载
- 出问题的是宿主当前聊天模型 provider

这时先查：

```bash
openclaw plugins inspect memory-palace --json
```

如果你只是想确认插件已经装好，`plugins inspect memory-palace --json` 是显式检查方式。有些宿主也接受 `plugins info`，但不要依赖某一个 `Skills` 可见名，也不要把 `openclaw skills list` 当成 bundled onboarding skill 的安装判断条件。

再确认：

- 当前 OpenClaw 用的模型 endpoint / key / model 是否还有效
- 本机 `openclaw agent --local` 使用的 provider 是否真的可直接调用

更稳的补查顺序是：

```bash
openclaw models status --json --probe --agent main
```

这条命令更适合先回答两个问题：

1. 当前 `main` agent 实际默认模型是不是你以为的那条
2. 当前 provider probe 到底是 `ok` 还是已经在 auth / timeout 上坏了

还有一个很容易误判的点：

- 日志最后如果看起来像是 `anthropic` 缺 auth
- 不代表根因一定就是 `anthropic`

更真实的情况也可能是：

- 主模型本来就是你自己配的本地 OpenAI-compatible provider
- 只是主模型先失败了
- 后面的 failover / auth 路径才把 `anthropic` 的报错打出来

所以不要只看最后一条 provider 名，先看 `models status --probe` 的结果。

如果模型 provider 自己就坏了，不要把这件事误写成“onboarding 流程不可用”。

---

## 15. 明明已经重配成新维度，对话里还在说旧值

如果你已经做过这三步：

1. `python3 scripts/openclaw_memory_palace.py provider-probe --json`
2. `python3 scripts/openclaw_memory_palace.py onboarding --apply --validate --json`
3. `openclaw memory-palace index --wait --json`

而且 runtime / verify 里也已经看到新维度已经生效，

但某条旧会话里还是继续回答更早的维度值，更准确的理解通常是：

- 这条会话还在复读更早的 recall 结果
- 不是当前共享 runtime 还没改对

更稳的处理顺序是：

1. 换一条新 session 再问
2. 或直接重新跑 `python3 scripts/openclaw_memory_palace.py provider-probe --json`
3. 如果你希望宿主一定按实时状态回答，就明确要求它调用 `memory_onboarding_probe`

再补一句，避免误会：

- 如果你只是用自然语言问“当前推荐维度是多少”
- 宿主模型也可能直接猜一个值

所以只要你要的是**当前实时值**，最稳的还是：

1. `python3 scripts/openclaw_memory_palace.py provider-probe --json`
2. 或显式让宿主调用 `memory_onboarding_probe`

---

## 16. `lock_retries_total` 很高，说明什么

如果你在 `openclaw memory-palace status --json` 或 Dashboard Observability 页面里看到 `lock_retries_total` 数字很大：

先说结论：

- 这通常说明有多个进程在同时写同一个 SQLite 文件
- 不一定是坏了——write lane 会自动重试（指数退避，默认 3 次）
- 大多数重试最终会成功

更稳的排法是：

1. 先看 `lock_retries_exhausted`——这个是重试用完仍失败的次数
2. 如果 `exhausted` 为 0 或很低，说明重试都成功了，不需要干预
3. 如果 `exhausted` 持续增长，说明竞争太严重，考虑：
   - 减少同时运行的 MCP server 进程数
   - 或加大 `RUNTIME_WRITE_LOCK_RETRY_ATTEMPTS`

---

## 17. `Timed out waiting for snapshot session lock`

先说结论：

- 这更像正常竞争或长事务
- 不一定是 review / snapshot 主链坏了

先处理：

1. 等前一个操作结束
2. 再重试
3. 如果持续复现，再去查是不是有长时间占锁的流程

只有在你频繁复现、而且明显不是正常并发时，才值得继续往更深的锁竞争去排。

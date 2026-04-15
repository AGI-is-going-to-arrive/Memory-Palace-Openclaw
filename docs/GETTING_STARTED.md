> [English](GETTING_STARTED.en.md)

# Memory Palace 快速上手

本指南帮助你在 5 分钟内跑通 Memory Palace 的本地开发环境或 Docker 部署。

> 先说明定位：
>
> - 当前公开发布更适合按 **OpenClaw memory plugin + bundled skills** 来理解
> - 本页讲的是仓库里的 backend / dashboard / direct MCP 这条技术路径
> - 如果你只是想给 OpenClaw 装插件，优先看 `docs/openclaw-doc/README.md`
> - direct MCP / skill 路线当前优先覆盖 `Claude Code / Codex / Gemini CLI / OpenCode`；`Cursor` 仍然建议人工验证

---

## 1. 环境要求

| 依赖 | 最低版本 | 检查命令 |
|---|---|---|
| Python | `3.10-3.14` | `python3 --version` |
| OpenClaw | `2026.3.2+` | `openclaw --version` |
| Node.js | `20.19+`（或 `>=22.12`） | `node --version` |
| npm | `9+` | `npm --version` |
| Bun（源码仓打包插件时需要） | 当前稳定版 | `bun --version` |
| Docker（可选） | `20+` | `docker --version` |
| Docker Compose（可选） | `2.0+` | `docker compose version` |

> **提示**：macOS 用户推荐使用 [Homebrew](https://brew.sh) 安装 Python 和 Node.js。Windows 用户推荐从官网下载安装包或使用 [Scoop](https://scoop.sh)。
>
> 如果你本机使用 `nvm`，可以直接在仓库根目录执行 `nvm use`。当前仓库附带的 `.nvmrc` 固定为 `22.12.0`；前端的 `package.json` 也只接受当前 LTS 线路（`20.19+` 或 `22.12+`）。

---

## 2. 仓库结构速览

```
memory-palace/
├── backend/              # FastAPI + SQLite 后端
│   ├── main.py           # 应用入口（FastAPI 实例、/health 端点）
│   ├── mcp_server.py     # 11 个 MCP 工具入口（FastMCP）+ 兼容 wrapper
│   ├── mcp_runtime_context.py # request-context / session-id helper
│   ├── mcp_client_compat.py # sqlite_client 兼容调用 helper
│   ├── mcp_transport.py  # SSE host/origin 保护 helper
│   ├── mcp_uri.py        # URI 解析与可写 domain 校验
│   ├── mcp_snapshot.py   # 快照 helper
│   ├── mcp_snapshot_wrappers.py # 绑定 runtime state 的 snapshot wrapper
│   ├── mcp_reading.py    # read_memory helper
│   ├── mcp_views.py      # system:// 视图生成
│   ├── mcp_server_config.py # domain/search/runtime 默认配置
│   ├── mcp_force_create.py # force-create / force-variant 判定 helper
│   ├── mcp_runtime_services.py # import-learn / gist / auto-flush 服务 helper
│   ├── mcp_tool_common.py # 共享 MCP guard/response helper
│   ├── mcp_tool_read.py  # read_memory MCP 工具实现
│   ├── mcp_tool_search.py # search_memory MCP 工具实现
│   ├── mcp_tool_write_runtime.py # write-lane / index enqueue runtime helper
│   ├── mcp_tool_write.py # create/update/delete/add_alias 写入工具实现
│   ├── mcp_tool_runtime.py # compact_context / rebuild_index / index_status 实现
│   ├── runtime_state.py  # 写入 Lane、索引 Worker、会话缓存
│   ├── run_sse.py        # MCP SSE 传输层（Starlette + API Key 鉴权）
│   ├── mcp_wrapper.py    # MCP 包装器
│   ├── requirements.txt  # Python 依赖清单
│   ├── db/               # 数据库 Schema、检索引擎
│   │   ├── sqlite_client.py     # 对外稳定导出的 SQLite facade
│   │   ├── sqlite_models.py     # ORM 模型定义
│   │   ├── sqlite_paths.py      # sqlite URL / 路径 / 时间 helper
│   │   └── sqlite_client_retrieval.py # 检索打分 / context recall helper
│   ├── api/              # HTTP 路由
│   │   ├── browse.py     # 记忆树浏览（GET /browse/node）
│   │   ├── review.py     # 审查接口（/review/*）
│   │   ├── maintenance.py        # `/maintenance` 路由兼容 facade
│   │   ├── maintenance_common.py # maintenance 共用鉴权 / env / 时间 helper
│   │   ├── maintenance_models.py # 请求模型与 lazy client proxy
│   │   ├── maintenance_index.py  # index worker / rebuild / retry 逻辑
│   │   └── maintenance_transport.py # transport / observability 聚合 helper
├── frontend/             # React + Vite + Tailwind Dashboard
│   ├── package.json      # 版本 1.0.1
│   └── vite.config.js    # 开发服务器 port 5173，代理到后端 8000
├── extensions/           # OpenClaw 插件
│   └── memory-palace/
│       ├── index.ts      # 插件入口与编排
│       └── src/          # runtime-layout / prompt-safety / host-bridge / assistant-derived / reflection 等 helper
├── deploy/               # Docker 与 Profile 配置
│   ├── docker/           # Dockerfile.backend / Dockerfile.frontend
│   └── profiles/         # macos / windows / docker 档位模板
├── scripts/              # 运维脚本
│   ├── apply_profile.sh  # Profile 应用脚本（macOS/Linux）
│   ├── apply_profile.ps1 # Profile 应用脚本（Windows）
│   ├── docker_one_click.sh   # Docker 一键部署（macOS/Linux）
│   ├── docker_one_click.ps1  # Docker 一键部署（Windows）
├── docs/                 # 项目文档
├── .env.example          # 配置模板（包含常用配置项）
├── docker-compose.yml    # Compose 编排文件
└── LICENSE               # 开源许可证
```

---

> 📌 当前启动顺序直接以正文命令为准：
>
> - 后端默认是 `uvicorn` 跑在 `127.0.0.1:8000`
> - 前端开发服务器默认是 `5173`
> - Docker 默认入口是 `http://127.0.0.1:3000`（Dashboard）与 `http://127.0.0.1:3000/sse`（SSE）
> - 如果你想先看 Dashboard 这 5 个页面分别负责什么，直接看 `docs/openclaw-doc/16-DASHBOARD_GUIDE.md`

<p align="center">
  <img src="openclaw-doc/assets/real-openclaw-run/dashboard-current/dashboard-setup-page.zh.png" width="900" alt="OpenClaw Dashboard Setup 真实截图" />
</p>

<p align="center">
  <img src="openclaw-doc/assets/real-openclaw-run/dashboard-current/dashboard-memory-page.zh.png" width="900" alt="OpenClaw Dashboard Memory 真实截图" />
</p>

<p align="center">
  <img src="openclaw-doc/assets/real-openclaw-run/dashboard-current/dashboard-review-page.zh.png" width="900" alt="OpenClaw Dashboard Review 真实截图" />
</p>

## 3. 本地开发（推荐先走这一条）

### Step 1：准备配置文件

```bash
cp .env.example .env
```

> 这里复制出来的是**更保守的 `.env.example` 最小模板**。它足够你先完成本地启动，但**不等于已经套用了 Profile B**。
>
> 如果你想直接使用仓库里定义好的 Profile B 默认值（例如本地 hash Embedding），请优先使用下面的 Profile 脚本；如果你继续手动改 `.env.example` 也可以，就把它理解成“从最小模板开始按需补配置”。

> **重要**：复制后请检查 `.env` 中的 `DATABASE_URL`，将路径改成你的实际路径。共享环境或接近生产的场景更推荐使用绝对路径。例如：
>
> ```
> DATABASE_URL=sqlite+aiosqlite:////absolute/path/to/memory_palace/demo.db
> ```

也可以使用 Profile 脚本快速生成带有默认配置的 `.env`：

```bash
# macOS —— 参数：平台 档位 [目标文件]
bash scripts/apply_profile.sh macos b

# Linux —— 参数：平台 档位 [目标文件]
bash scripts/apply_profile.sh linux b

# Windows PowerShell
.\scripts\apply_profile.ps1 -Platform windows -Profile b
```

> apply_profile 脚本会将 `.env.example` 复制到 `.env`（或你指定的目标文件），然后追加对应 Profile 的覆盖配置。macOS/Linux 平台都会自动检测并填充 `DATABASE_URL`。
>
> `apply_profile.sh/.ps1` 当前会在生成后统一去重重复 env key；当前维护中的 Windows 原生 basic-path 也已经统一成 `setup -> verify -> doctor -> smoke`。但发布前仍建议在目标 Windows OpenClaw 主机上单独补跑一次原生验证，不要把其它环境里的 `pwsh` 代理验证当成等价证明。
>
> 下面提到的 `provider-probe / onboarding`，都指 **repo wrapper** `python3 scripts/openclaw_memory_palace.py ...` 这条链路，不属于 `openclaw memory-palace` 的稳定子命令面。
>
> 这里也要把边界说清：
> - `apply_profile.*` 最适合先拿到 **Profile B** 这类保守本地模板
> - 如果你要直接做 **Profile C/D**，它更像“生成字段骨架并拒绝明显占位值”
> - 真正是否 provider-ready，还是要以 `provider-probe / verify / doctor / smoke` 为准
>
> 但要注意：**macOS / Linux / Windows 本地生成的 profile-b `.env` 都不会自动补 `MCP_API_KEY`**。如果你接下来就要打开 Dashboard，或者直接调 `/browse` / `/review` / `/maintenance`、`/sse`、`/messages`，请再自行补 `MCP_API_KEY`，或仅在本机回环调试时设置 `MCP_API_KEY_ALLOW_INSECURE_LOCAL=true`。只有 `docker` 平台的 profile 脚本会在 key 为空时自动生成一把本地 key。

#### 关键配置项说明

以下是 `.env` 中最常用的配置项（更多配置项请查看 `.env.example` 中的注释说明）：

| 配置项 | 说明 | 模板示例值 |
|---|---|---|
| `DATABASE_URL` | SQLite 数据库路径（**建议使用绝对路径**） | `sqlite+aiosqlite:////absolute/path/to/memory_palace/demo.db` |
| `SEARCH_DEFAULT_MODE` | 检索模式：`keyword` / `semantic` / `hybrid` | `hybrid` |
| `RETRIEVAL_EMBEDDING_BACKEND` | 嵌入后端：`none` / `hash` / `router` / `api` / `openai` | `hash` |
| `RETRIEVAL_EMBEDDING_MODEL` | Embedding 模型名 | `Qwen3-Embedding-8B` |
| `RETRIEVAL_EMBEDDING_DIM` | Embedding 请求维度；Profile B 默认 `64`（hash），Profile C/D 应以 provider-probe 的实时探测结果为准 | `64` (B) / `provider-probe` (C/D) |
| `RETRIEVAL_RERANKER_ENABLED` | 是否启用 Reranker | `false` |
| `RETRIEVAL_RERANKER_API_BASE` | Reranker API 地址 | 空 |
| `RETRIEVAL_RERANKER_API_KEY` | Reranker API 密钥 | 空 |
| `RETRIEVAL_RERANKER_MODEL` | Reranker 模型名 | `Qwen3-Reranker-8B` |
| `INTENT_LLM_ENABLED` | 实验性意图 LLM 开关 | `false` |
| `RETRIEVAL_MMR_ENABLED` | hybrid 检索下的去重 / 多样性重排 | `false` |
| `RETRIEVAL_SQLITE_VEC_ENABLED` | sqlite-vec rollout 开关 | `false` |
| `MCP_API_KEY` | HTTP/SSE 接口鉴权密钥 | 空（见下方鉴权说明） |
| `MCP_API_KEY_ALLOW_INSECURE_LOCAL` | 本地调试时允许无 Key 访问（仅对直连 loopback 请求生效） | `false` |
| `CORS_ALLOW_ORIGINS` | 允许跨域访问的来源列表（留空使用本地默认） | 空 |
| `VALID_DOMAINS` | 允许的可写记忆 URI 域（`system://` 为内建只读域） | `core,writer,game,notes` |
| `RUNTIME_FLUSH_HIGH_VALUE_EARLY_ENABLED` | 对短但明显高价值的 workflow / preference 事件提前触发保守 flush | `true` |
| `RUNTIME_FLUSH_HIGH_VALUE_MIN_EVENTS` | 高价值提前 flush 的最小事件数 | `2` |
| `RUNTIME_FLUSH_HIGH_VALUE_MIN_CHARS` | 高价值提前 flush 的非 CJK 最小字符数 | `120` |
| `RUNTIME_FLUSH_HIGH_VALUE_MIN_CHARS_CJK` | 高价值提前 flush 的 CJK 最小字符数 | `100` |

> B 档位默认使用本地 hash Embedding 且不启用 Reranker；它仍然是**默认起步档位**。
>
> 如果你已经准备好模型服务，**更推荐尽快升级到 Profile C**：它需要你在 `.env` 中把 Embedding / Reranker 链路填好；如果还要启用 LLM 辅助的 write guard / gist / intent routing，再继续填写 `WRITE_GUARD_LLM_*`、`COMPACT_GIST_LLM_*`、可选的 `INTENT_LLM_*`。详见 [DEPLOYMENT_PROFILES.md](DEPLOYMENT_PROFILES.md)。
>
> 上表这里写的是 `.env.example` 当前真实模板值；如果某些检索环境变量在运行时完全缺失，后端内部还会使用自己的回退值（例如 `hash` / `hash-v1` / `64`）。
>
> 配置语义说明：`RETRIEVAL_EMBEDDING_BACKEND` 只作用于 Embedding。Reranker 不存在 `RETRIEVAL_RERANKER_BACKEND` 开关，优先读取 `RETRIEVAL_RERANKER_*`，缺失时才回退 `ROUTER_*`（最后回退 `OPENAI_*` 的 base/key）。
>
> 更多高级选项（如 `INTENT_LLM_*`、`RETRIEVAL_MMR_*`、`RETRIEVAL_SQLITE_VEC_*`、`CORS_ALLOW_*`、运行时观测/睡眠整合开关）已写在 `.env.example`，默认保持保守值，不影响最小启动路径。
>
> 这一轮新增的 runtime flush 默认值也按这个原则理解：它们只改善摘要质量和短高价值事件的 flush 时机，不改数据库 schema、不改 MCP API，也不改 Profile A/B/C/D 的主逻辑。
>
> 推荐默认值（直接照抄通常没问题）：
> - `INTENT_LLM_ENABLED=false`：先用内建关键词规则，少一层外部依赖
> - `RETRIEVAL_MMR_ENABLED=false`：先看原始 hybrid 结果，只有“前几条太像”时再开
> - `RETRIEVAL_SQLITE_VEC_ENABLED=false`：普通部署先保持 legacy 路径
> - `CORS_ALLOW_ORIGINS=`：本地开发留空；要开放给浏览器跨域访问时再写明确域名
>
> 公开文档不预设单一 provider/model 组合。请把 `RETRIEVAL_EMBEDDING_MODEL`、`RETRIEVAL_RERANKER_MODEL` 和可选的 `*_LLM_MODEL` 填成你自己环境中已经验证可用的模型名。
>
> 如果你接下来就要在本地打开 Dashboard，或者直接用 `curl` 调 `/browse` / `/review` / `/maintenance`，建议再补一项鉴权配置（二选一）：
>
> - `MCP_API_KEY=change-this-local-key`
> - `MCP_API_KEY_ALLOW_INSECURE_LOCAL=true`（只建议你自己机器上的回环调试时使用）

### Step 2：先把 OpenClaw 插件主链接通（推荐）

如果你现在的目标是先把 **OpenClaw 插件链路**跑起来，而不是自己手动编辑本机 OpenClaw 配置文件，直接在仓库根目录执行：

```bash
python3 scripts/openclaw_memory_palace.py setup --mode basic --profile b --transport stdio --json
openclaw memory-palace verify --json
openclaw memory-palace doctor --json
openclaw memory-palace smoke --json
```

> 注意：当前 OpenClaw `setup` / package runtime 的稳定路径要求 **Python `3.10-3.14`**。如果你机器上的默认 Python 不在这个范围内，请显式改用本机已安装的受支持版本（例如 `3.14`）。
>
> 当前公开验证口径统一看 `docs/EVALUATION.md`。对这条命令链来说，更重要的是理解现象：在当前代码下，从 shell 直接运行时，`host-config-path` 本身正常也应该能 pass；如果这项 warning 又出现，更应先查你是否手动指到了失效的 `OPENCLAW_CONFIG_PATH` / `OPENCLAW_CONFIG`，或宿主配置文件本身已经漂移/不可读，而不是把它当成当前预期行为。
>
> 如果你后面打开 Dashboard `/setup`，当前页面也会直接显示 `embedding / reranker / llm` 的最近一次 provider probe 结果，以及是否已经因为探活失败回退到 `Profile B`，不再只是停在“已配置 / 未配置”。
>
> 当前公开更稳的理解是：
>
> - `Profile B` 代表零外部依赖起步档
> - `Profile C/D` 代表 provider-ready 的能力上限路径
> - 只有 `provider-probe / verify / doctor / smoke` 这一组都跑通，才能把 `C/D` 写成“当前已经生效”
>
> 如果你是**故意在同一套 runtime** 里从 `Profile B` 切到 `Profile C/D`，或者改了现有 runtime 的 `RETRIEVAL_EMBEDDING_DIM`，请在 `setup` 之后立刻补一条：
>
> ```bash
> openclaw memory-palace index --wait --json
> ```
>
> 重建后，混杂维度的向量行会回到当前生效 profile 的单一维度。
>
> 很多 embedding 模型支持多种输出维度。当前更稳的口径是：如果 `provider-probe` 探测出的维度和模板默认值不同，以 probe 结果为准，并在切换维度后补跑一次 `openclaw memory-palace index --wait --json`。
>
> 如果宿主在某条旧会话里继续复读更早的维度值，优先换一个新 session，或直接重新跑一次 `python3 scripts/openclaw_memory_palace.py provider-probe --json` 看实时结果。更完整的解释统一放到 `docs/openclaw-doc/04-TROUBLESHOOTING.md`。
>
> 反过来说，如果你平时走的是仓库里的维护者自动化脚本，这两条链路现在本身就会使用隔离临时 config / state / db；如果它们误指向共享 `~/.openclaw` runtime，当前会直接 fail-fast，而不是悄悄把共享库跑脏。

这条路径会自动：

- 在 `~/.openclaw/memory-palace` 下准备用户态 runtime
- 写入 `plugins.allow` / `plugins.load.paths` / `plugins.slots.memory` / `plugins.entries.memory-palace`
- 如果当前已经有 `openclaw.json`，会先做一份备份再改配置
- 默认按 **Profile B** 起步
- 如果你在这条本地路径里留空 `MCP_API_KEY`，它还会自动生成一把本地 key

这里补一句最容易误会的边界：

- 这条路改的是你本机的 OpenClaw 配置文件
- 不是改 OpenClaw 源码
- 如果你想自己管理 slot 绑定，wrapper 也支持 `--no-activate`

如果你想验证 **本地打包出来的插件包**，命令链还是下面这一组：

```bash
cd extensions/memory-palace
npm pack
openclaw plugins install --dangerously-force-unsafe-install ./<generated-tgz>
npm exec --yes --package ./<generated-tgz> memory-palace-openclaw -- setup --mode basic --profile b --transport stdio --json
openclaw memory-palace verify --json
openclaw memory-palace doctor --json
openclaw memory-palace smoke --json
```

> 如果你手里已经有受信任的 `tgz`，就直接把上面命令里的
> `./<generated-tgz>` 替换成那个文件路径，跳过 `npm pack`。
>
> 这里的 `--dangerously-force-unsafe-install` 只适用于**你刚从当前仓库打出来的本地 tgz**。`Memory Palace` 会调用受信任的本地 launcher / onboarding / visual helper，`OpenClaw 2026.4.5+` 会把这种本地包默认拦下；不要把这个开关用于来源不明的第三方插件包。
>
> 这条源码仓 `npm pack` 路径当前依赖 **Bun** 和一套受支持的 **Python `3.10-3.14`**，因为扩展包的 `prepack` 会执行 `bun build`、`bun test` 和 Python 打包 wrapper。
>
> 当前记录在案的 **local tgz / clean-room** 路径仍然按“已验证可走”理解，但这里不再重复抄一遍 session 里的具体版本号和状态细节。更稳的理解是：如果你只是想先把插件跑起来，优先还是走前面的源码仓 `setup --mode basic/full` 路径；如果你要验证本地打包交付物，再走这里。package/tgz 的最新验证状态统一看 `docs/EVALUATION.md`。

### Step 3：启动后端

```bash
cd backend
python3 -m venv .venv

# macOS / Linux
source .venv/bin/activate

# Windows PowerShell
# .\.venv\Scripts\Activate.ps1

pip install -r requirements.txt
uvicorn main:app --host 127.0.0.1 --port 8000 --reload
```

预期输出：

```
Memory API starting...
SQLite database initialized.
INFO:     Uvicorn running on http://127.0.0.1:8000
```

> 后端现在通过共享的 `runtime_bootstrap.py` 完成初始化；`main.py` 里的 `lifespan` 会走这条 bootstrap，stdio / SSE 启动路径也走同一套逻辑。简单理解就是：legacy SQLite 恢复、`init_db()`、以及运行时状态（Write Lane、Index Worker）启动，当前都按同一条顺序执行，不再是不同入口各做一半。

### Step 4：启动前端

```bash
cd frontend
npm install
npm run dev
```

预期输出：

```
VITE v7.x.x  ready in xxx ms
➜  Local:   http://127.0.0.1:5173/
```

打开浏览器访问 `http://127.0.0.1:5173`，即可看到 Memory Palace Dashboard。

> 如果这是**第一次本地启动**，而当前还没有 bootstrap 配置，页面可能会先跳到 **`/setup`**。这时最稳的默认选择仍然是 **`basic + Profile B`**，先把 bootstrap 基线跑通。
> 但如果你的 embedding / reranker / LLM provider 已经就绪，就不要停在 B：当前新版 setup 会把 **`Profile C / D`** 明确标成更推荐的目标路径，建议切到对应路径后再做最终验证和签收。点 `Apply` 之后，如果页面显示 `Restart Backend`，再点一次，等几秒让页面重新连上。

> 如果后端已经配置了 `MCP_API_KEY`，但页面仍然显示 `Set API key`，就点右上角填入同一把 key。这个前端兜底 key 现在仅保存在当前页面的内存中（memory-only），关闭标签页或刷新后需重新输入，或由部署脚本通过 `window.__MEMORY_PALACE_RUNTIME__` 注入。

> 这里有一个很关键的边界：右上角输入的这把 key 主要是给 **Dashboard 的受保护数据接口**（`/browse/*`、`/review/*`、`/maintenance/*`）兜底；在当前同源 / loopback 的 Dashboard 里，前端也会把它一并带到 `/bootstrap/provider-probe`、`/bootstrap/apply`、`/bootstrap/restart`。真正额外的限制不是“这把 key 无效”，而是 `/bootstrap/*` 仍然要求你走本机 loopback / 当前页面同源链路。如果你现在不是在本机 loopback 页面上操作，或者想绕开页面态 key，直接走 CLI 更稳：
>
> ```bash
> python3 scripts/openclaw_memory_palace.py setup --mode basic --profile b
> ```

> 如果你启用了 `MCP_API_KEY_ALLOW_INSECURE_LOCAL=true`，本机回环地址上的直连请求可直接访问这些受保护数据接口；但 `/bootstrap/*` 仍然先看 loopback / 同源条件，再看 key 是否带上。

> 当前前端默认英文显示，右上角支持中英切换；所选语言会持久化到浏览器 `localStorage`，刷新后仍会保留。
>
> 当前前端还补了一层运行时文案归一化：`/setup` 和 `/observability` 这类页面里常见的 bootstrap / 诊断动态文本，会尽量跟随当前界面语言显示，不再把后端返回的原始中文字符串直接露到英文页面上。

> 前端开发服务器通过 `vite.config.js` 中配置的 proxy 将 `/api` 路径代理到后端 `http://127.0.0.1:8000`，因此前后端无需手动配置 CORS。

如果你想补一条真实浏览器回归验证，可以执行：

```bash
cd frontend
npx playwright install chromium
npm run test:e2e
```

这条 E2E 覆盖的是**完成 bootstrap 之后**的 Dashboard 主路径：切换中英界面、保存语言选择、输入 API key 并打开受保护页面。首轮 `/setup` 跳转由前端测试单独覆盖。

---

## 4. Docker 一键部署

```bash
# macOS / Linux
bash scripts/docker_one_click.sh --profile b

# Windows PowerShell
.\scripts\docker_one_click.ps1 -Profile b

# 若需把当前进程中的运行时 API 密钥/地址注入本次运行的 Docker env 文件（例如 profile c/d）
# 需显式开启注入开关（默认关闭）：
bash scripts/docker_one_click.sh --profile c --allow-runtime-env-injection
# 或
.\scripts\docker_one_click.ps1 -Profile c -AllowRuntimeEnvInjection
```

> `docker_one_click.sh/.ps1` 默认会为**每次运行**生成独立的临时 Docker env 文件，并通过 `MEMORY_PALACE_DOCKER_ENV_FILE` 传给 `docker compose`；只有显式设置该环境变量时才会复用指定文件，而不是固定共享 `.env.docker`。
>
> 同一 checkout 下的并发部署会被 deployment lock 串行化；若已有另一条一键部署在执行，后续进程会直接退出并提示稍后重试。
>
> 如果 Docker env 文件里的 `MCP_API_KEY` 为空，`apply_profile.*` 会自动生成一把本地 key。Docker 前端会在代理层自动带上这把 key，所以 Dashboard 默认不需要再手动点 `Set API key`。
>
> 现在 Docker 暴露端口默认通过 `MEMORY_PALACE_BIND_HOST=127.0.0.1` 只绑定到本机回环地址。如果你显式改成非回环地址做远程访问，要把前端视为受保护 Dashboard 路由的特权代理，并自行补齐网络隔离与访问控制。
>
> **C/D 本地联调建议**：
>
> - 如果你本机的 `router` 还没接好 embedding / reranker / llm，可以先直接分别配置 `RETRIEVAL_EMBEDDING_*`、`RETRIEVAL_RERANKER_*`、`WRITE_GUARD_LLM_*` / `COMPACT_GIST_LLM_*`。
> - 这样更容易判断到底是哪一条链路不可达，不会把“某个模型没配好”误判成整个系统不可用。
> - 如果你的 reranker 是本地 `llama.cpp` 这类自部署服务，第一次 `setup --profile c/d --strict-profile`、`verify` 或 `smoke` 前，先手工打一次 `/rerank` 预热会更稳。
> - 无论你最终采用 `router` 方案，还是分别直配 `RETRIEVAL_EMBEDDING_*` / `RETRIEVAL_RERANKER_*`，都建议按**最终实际部署配置**重新跑一次启动与健康检查。

> 脚本会自动执行以下步骤：
>
> 1. 调用 Profile 脚本生成本次运行使用的 Docker env 文件（默认临时文件；若显式设置 `MEMORY_PALACE_DOCKER_ENV_FILE` 则复用指定路径）
> 2. 默认不读取当前进程环境变量覆盖模板策略键（避免隐式改档）；仅在显式开启注入开关时注入 API 地址/密钥/模型字段
> 3. 检测端口占用并自动寻找可用端口
> 4. 对同一 checkout 的并发部署加锁，避免多次 `docker_one_click` 互相覆盖
> 5. 通过 `docker compose` 构建并启动容器

默认访问地址：

| 服务 | 地址 |
|---|---|
| Frontend | `http://localhost:3000` |
| Backend API | `http://localhost:18000` |
| SSE | `http://localhost:3000/sse` |
| Health Check | `http://localhost:18000/health` |
| API 文档 (Swagger) | `http://localhost:18000/docs` |

> **端口映射说明**（来自 `docker-compose.yml`）：
>
> - 前端容器内部运行在 `8080` 端口，对外映射到 `3000`（可通过 `MEMORY_PALACE_FRONTEND_PORT` 环境变量覆盖）
> - 后端容器内部运行在 `8000` 端口，对外映射到 `18000`（可通过 `MEMORY_PALACE_BACKEND_PORT` 环境变量覆盖）
> - 如果启动过程中出现 `[port-adjust]`，说明脚本已经自动改用了别的空闲端口；这时请以控制台最后打印出来的 Frontend / Backend / SSE 地址为准，不要机械照抄上面这张默认表

停止服务：

```bash
COMPOSE_PROJECT_NAME=<控制台打印出的 compose project> docker compose -f docker-compose.yml down --remove-orphans
```

> 如果你需要验证 Windows 路径，建议直接在目标 Windows 环境里补跑一次启动与 smoke。

发布前如果你想用仓库自带 gate 一次性带上前端浏览器回归：

```bash
bash scripts/pre_publish_check.sh --release-gate --profile-smoke-modes local --review-smoke-modes local
```

如果你走的是新的 checkpoint release-gate，当前默认还会把 packaged Python 矩阵 smoke 一起跑掉：

```bash
python3 scripts/openclaw_memory_palace.py release-gate
```

如果你这次只想先跑更快的一轮日常检查，不想顺带重跑 `3.10-3.14` 的 packaged Python 矩阵，可以直接加：

```bash
python3 scripts/openclaw_memory_palace.py release-gate --skip-python-matrix
```

如果你只想跳过浏览器 E2E、保留前端单测：

```bash
bash scripts/pre_publish_check.sh --release-gate --skip-frontend-e2e
```

### 4.1 备份当前数据库

在做批量测试、迁移验证或大范围配置切换前，建议先做一次 SQLite 一致性备份：

```bash
# macOS / Linux
bash scripts/backup_memory.sh

# 指定 env / 输出目录
bash scripts/backup_memory.sh --env-file .env --output-dir backups
```

当前 profile 模板目录：

```text
deploy/profiles/{macos,linux,windows,docker}/profile-{a,b,c,d}.env
```

```powershell
# Windows PowerShell
.\scripts\backup_memory.ps1
```

> 备份文件默认写入 `backups/`。它属于运行期目录，通常只在你自己的机器上使用。
>
> 💡 如果你只是想做本地试验，建议把 `backups/` 也当成“只放自己机器上”的目录。

### 4.2 哪些文件通常只在你自己的机器上使用

当前仓库已经把以下典型本地产物放入 `<repo-root>/.gitignore`：

- 运行期数据库：`*.db`、`*.sqlite`、`*.sqlite3`
- 本地工具配置：`.mcp.json`、`.claude/`、`.codex/`、`.cursor/`、`.opencode/`、`.gemini/`、`.agent/`
- 本地缓存与临时目录：`.tmp/`、`backend/.pytest_cache/`
- 前端本地产物：`frontend/node_modules/`、`frontend/dist/`
- 日志与快照：`*.log`、`snapshots/`、`backups/`
- 临时测试草稿：`frontend/src/*.tmp.test.jsx`
- 维护期内部文档：`docs/improvement/`、`backend/docs/benchmark_*.md`
- 一次性对照摘要：`docs/evaluation_old_vs_new_*.md`

如果你准备把项目分享给别人、打包交付，或者只是想做一次环境自检，建议执行：

```bash
bash scripts/pre_publish_check.sh
```

它会检查本地敏感产物、数据库、日志、个人路径和 `.env.example` 占位项，帮助你确认哪些内容更适合只留在当前机器上。当前这条检查也会额外扫描 `README/docs` 里的通用 macOS / Windows 用户目录绝对路径，不再只匹配当前机器用户名。

如果你额外运行下面这些验证脚本：

```bash
python scripts/evaluate_memory_palace_skill.py
cd backend && python ../scripts/evaluate_memory_palace_mcp_e2e.py
```

脚本会在本机生成类似 `TRIGGER_SMOKE_REPORT.md`、`MCP_LIVE_E2E_REPORT.md` 这样的摘要文件。这些结果更适合当成你自己机器上的验证记录，而不是主说明文档；它们默认也被 `.gitignore` 排除，所以公开 GitHub 仓库里通常不会带上这些文件。

---

## 5. 首次验证

> 这里的检查以“先跑通系统”为主；如果你需要额外的本地 Markdown 验证摘要，再运行上面的验证脚本即可。

### 5.1 健康检查

```bash
# 本地开发
curl -fsS http://127.0.0.1:8000/health

# Docker 部署
curl -fsS http://localhost:18000/health
```

预期返回（来自 `main.py` 的 `/health` 端点）：

```json
{
  "status": "ok",
  "timestamp": "2026-02-19T08:00:00Z",
  "index": {
    "index_available": true,
    "degraded": false
  },
  "runtime": {
    "write_lanes": { ... },
    "index_worker": { ... }
  }
}
```

> `status` 为 `"ok"` 表示系统正常；若 index 不可用或报错，`status` 会变为 `"degraded"`。

### 5.2 浏览记忆树

```bash
curl -fsS "http://127.0.0.1:8000/browse/node?domain=core&path=" \
  -H "X-MCP-API-Key: <YOUR_MCP_API_KEY>"
```

> 此端点来自 `api/browse.py` 的 `GET /browse/node`，用于查看指定域下的记忆节点树。`domain` 参数对应 `.env` 中 `VALID_DOMAINS` 配置的域名。
>
> - 如果你配置了 `MCP_API_KEY`，请像上面这样带 `X-MCP-API-Key`
> - 如果你启用了 `MCP_API_KEY_ALLOW_INSECURE_LOCAL=true`，并且请求来自本机回环地址（且没有 forwarded headers），也可以直接省略鉴权头

### 5.3 查看 API 文档

浏览器访问 `http://127.0.0.1:8000/docs`，可打开 FastAPI 自动生成的 Swagger 文档，查看所有 HTTP 端点的参数和返回格式。

---

## 6. MCP 接入

Memory Palace 通过 [MCP 协议](https://modelcontextprotocol.io/) 提供 **11 个工具**。当前 `mcp_server.py` 仍保留工具入口和依赖注入层，但 search/write 实现、domain/search/runtime 默认配置、force-create 判定、snapshot wrapper、URI、读取和 system view 这些 helper 已经拆到独立模块中：

| 工具名 | 用途 |
|---|---|
| `read_memory` | 读取记忆（支持 `system://boot`、`system://index` 等特殊 URI） |
| `create_memory` | 创建新记忆节点 |
| `update_memory` | 更新已有记忆（支持 diff patch） |
| `delete_memory` | 删除记忆节点 |
| `add_alias` | 为记忆节点添加别名 |
| `search_memory` | 搜索记忆（keyword / semantic / hybrid 三种模式） |
| `compact_context` | 压缩上下文（清理旧会话日志） |
| `compact_context_reflection` | 将压缩结果写入 reflection lane（反思记忆通道）；通常由插件自动调用 |
| `rebuild_index` | 重建搜索索引 |
| `index_status` | 查看索引状态 |
| `ensure_visual_namespace_chain` | visual / OpenClaw 场景下预建 namespace；普通用户一般不用手动调用 |

### 6.1 stdio 模式（推荐本地使用）

```bash
bash scripts/run_memory_palace_mcp_stdio.sh
```

> 这条 wrapper 路径会优先使用仓库里的 backend `.venv`，并自动补上本地 runtime 默认值。对普通用户来说，它比直接裸跑 `python mcp_server.py` 更接近项目当前实际接线方式。
>
> `stdio` 模式下 MCP 工具直接通过进程的标准输入/输出通信，**不经过 HTTP/SSE 鉴权层**，无需配置 `MCP_API_KEY` 即可使用。
>
> 如果你只是想做后端开发调试，才再直接用：
>
> ```bash
> cd backend
> python mcp_server.py
> ```

### 6.2 SSE 模式

```bash
cd backend
HOST=127.0.0.1 PORT=8010 python run_sse.py
```

> `run_sse.py` 默认会先尝试监听 `127.0.0.1:8000`；如果 `8000` 已被占用，当前实现会自动回退到 `127.0.0.1:8010`。SSE 端点路径为 `/sse`，并受 `MCP_API_KEY` 鉴权保护。稳妥做法是以实际启动日志里打印出来的地址为准。
>
> 上面这条命令故意绑定到 `127.0.0.1`，更适合本机调试。如果你真的需要让其他机器访问，再把 `HOST` 改成 `0.0.0.0`（或你的实际监听地址），并同时补齐 API Key、反向代理、防火墙和传输层安全。
>
> 这条 standalone `run_sse.py` 路径现在主要保留给本地独立调试和兼容旧配置。当前主后端 `uvicorn main:app` 也已经直接挂载 `/sse`、`/messages`、`/sse/messages`，因此单进程即可同时提供 REST API 和 SSE。
>
> 如果你使用 Docker 一键部署，SSE 现在不再由独立容器启动，而是直接挂在 backend 进程里，再通过前端代理暴露在 `http://127.0.0.1:3000/sse`。
>
> 上面的 `HOST=127.0.0.1 PORT=8010` 示例是**本机回环**写法。只有在你确实要开放给远程客户端时，才改为 `HOST=0.0.0.0`（或目标绑定地址），并自行补齐网络侧安全控制。

### 6.3 客户端配置示例

**stdio 模式**（适用于 Claude Code / Codex / OpenCode 等；`Cursor` 仍建议人工验证）：

```json
{
  "mcpServers": {
    "memory-palace": {
      "command": "bash",
      "args": ["/path/to/memory-palace/scripts/run_memory_palace_mcp_stdio.sh"]
    }
  }
}
```

**SSE 模式**：

```json
{
  "mcpServers": {
    "memory-palace": {
      "url": "http://127.0.0.1:8010/sse"
    }
  }
}
```

> ⚠️ 请将 `/path/to/memory-palace` 替换为你的实际项目路径。SSE 模式的端口需与你启动 `run_sse.py` 时的 `PORT` 一致。
>
> ⚠️ SSE 仍受 `MCP_API_KEY` 保护。多数客户端还需要额外配置请求头或 Bearer Token；具体字段名请以客户端自己的 MCP 文档为准。
>
> ⚠️ 如果你把 `HOST` 改成非回环地址做远程访问，当前默认仍会继续校验 `Host` / `Origin`。真要放远程流量进来，请显式配置 `MCP_ALLOWED_HOSTS` / `MCP_ALLOWED_ORIGINS`。

---

## 7. HTTP/SSE 接口鉴权

Memory Palace 的部分 HTTP 接口受 `MCP_API_KEY` 保护，采用 **fail-closed** 策略（未配置 Key 时默认返回 `401`）。

### 受保护的接口

| 路由前缀 | 说明 | 鉴权方式 |
|---|---|---|
| `/maintenance/*` | 维护接口（孤立节点清理等） | `require_maintenance_api_key` |
| `/review/*` | 审查接口（内容审核流程） | `require_maintenance_api_key` |
| `/browse/*`（GET/POST/PUT/DELETE） | 记忆树读写操作 | `require_maintenance_api_key` |
| `run_sse.py` 的 `/sse` 与 `/messages` | MCP SSE 传输通道与消息入口 | `apply_mcp_api_key_middleware` |

### 鉴权方式

后端支持两种 Header 传递 API Key（当前实现位于 `api/maintenance_common.py`，`maintenance.py` 继续复用这套逻辑；SSE 侧对应 `run_sse.py`）：

```bash
# 方式一：自定义 Header
curl -fsS http://127.0.0.1:8000/maintenance/orphans \
  -H "X-MCP-API-Key: <YOUR_MCP_API_KEY>"

# 方式二：Bearer Token
curl -fsS http://127.0.0.1:8000/maintenance/orphans \
  -H "Authorization: Bearer <YOUR_MCP_API_KEY>"
```

### 前端鉴权配置

如果前端也需要访问受保护接口，请在 `frontend/index.html` 的 `<head>` 中注入运行时配置（前端 `src/lib/api.js` 会读取 `window.__MEMORY_PALACE_RUNTIME__`）：

```html
<script>
  window.__MEMORY_PALACE_RUNTIME__ = {
    maintenanceApiKey: "<YOUR_MCP_API_KEY>",
    maintenanceApiKeyMode: "header"
  };
</script>
```

> 这段配置主要用于**本地手动启动前后端**的场景。
>
> Docker 一键部署默认不需要把 key 写进页面：前端容器会在代理层自动把同一把 `MCP_API_KEY` 转发到 `/api/*`、`/sse` 和 `/messages`。

### 本地调试跳过鉴权

如果在本地开发时不想配置 API Key，可在 `.env` 中设置：

```env
MCP_API_KEY_ALLOW_INSECURE_LOCAL=true
```

> 此选项仅对来自 `127.0.0.1` / `::1` / `localhost` 的**直连请求**生效；如果请求带有 forwarded headers，仍会被拒绝。它只影响 HTTP/SSE 接口，**不影响** stdio 模式（stdio 不经过鉴权层）。

---

## 8. 常见新手问题

| 问题 | 原因与解决 |
|---|---|
| 启动后端时 `ModuleNotFoundError` | 未激活虚拟环境或未安装依赖。执行 `source .venv/bin/activate && pip install -r requirements.txt` |
| `DATABASE_URL` 报错 | 路径建议使用绝对路径，并且要带 `sqlite+aiosqlite:///` 前缀。示例：`sqlite+aiosqlite:////absolute/path/to/memory_palace.db` |
| 前端访问 API 返回 `502` 或 `Network Error` | 确认后端已启动且运行在 `8000` 端口。检查 `vite.config.js` 中 proxy 目标与后端端口是否一致 |
| 受保护接口返回 `401` | 本地手动启动：配置 `MCP_API_KEY` 或设置 `MCP_API_KEY_ALLOW_INSECURE_LOCAL=true`；Docker：优先确认是否使用 `apply_profile.*` / `docker_one_click.*` 生成的 Docker env 文件 |
| Docker 启动端口冲突 | `docker_one_click.sh` 默认会自动寻找空闲端口。也可通过 `--frontend-port` / `--backend-port` 手动指定 |

更多问题排查请参考 [TROUBLESHOOTING.md](TROUBLESHOOTING.md)。

---

## 9. 继续阅读

| 文档 | 内容 |
|---|---|
| [DEPLOYMENT_PROFILES.md](DEPLOYMENT_PROFILES.md) | 部署档位（A/B/C/D）参数详解与选择指南 |
| [TOOLS.md](TOOLS.md) | 11 个 MCP 工具的完整语义、参数和返回格式 |
| [TECHNICAL_OVERVIEW.md](TECHNICAL_OVERVIEW.md) | 系统架构、数据流与技术细节 |
| [TROUBLESHOOTING.md](TROUBLESHOOTING.md) | 常见问题排查与诊断 |
| [SECURITY_AND_PRIVACY.md](SECURITY_AND_PRIVACY.md) | 安全模型与隐私设计 |

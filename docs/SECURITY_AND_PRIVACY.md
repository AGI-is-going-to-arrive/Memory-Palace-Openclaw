> [English](SECURITY_AND_PRIVACY.en.md)

# Memory Palace 安全与隐私指南

<p align="center">
  <img src="images/write_guard_review_loop_zh_4k.png" width="1100" />
</p>

本文档面向部署和维护 Memory Palace 的用户，涵盖密钥管理、接口鉴权、Docker 安全，以及分享或正式发布前的安全自检。

---

## 1. 你需要保护什么

以下密钥 **只应存在于本地 `.env` 或受保护的部署环境变量中**，不应提交到 Git 仓库。

> 完整密钥清单可参考 [`.env.example`](../.env.example)。

| 密钥 | 用途 | 在 `.env.example` 中对应变量 |
|---|---|---|
| `MCP_API_KEY` | 维护接口、审查接口、Browse 读写与 SSE 鉴权 | `MCP_API_KEY=` |
| `RETRIEVAL_EMBEDDING_API_KEY` | Embedding 模型 API 访问 | `RETRIEVAL_EMBEDDING_API_KEY=` |
| `RETRIEVAL_RERANKER_API_KEY` | Reranker 模型 API 访问 | `RETRIEVAL_RERANKER_API_KEY=` |
| `WRITE_GUARD_LLM_API_KEY` | Write Guard LLM 决策 | `WRITE_GUARD_LLM_API_KEY=` |
| `COMPACT_GIST_LLM_API_KEY` | Compact Context Gist LLM（为空时自动回退到 Write Guard） | `COMPACT_GIST_LLM_API_KEY=` |
| `INTENT_LLM_API_KEY` | 实验性 Intent LLM 决策 | `INTENT_LLM_API_KEY=` |
| `ROUTER_API_KEY` | Router 模式下的 Embedding API 访问；以及 Reranker 未显式配置 `RETRIEVAL_RERANKER_API_KEY` 时的回退密钥 | `ROUTER_API_KEY=` |

---

## 2. 推荐做法

- ✅ 只提交 `.env.example`，**不要提交** `.env`（已写入 [`.gitignore`](../.gitignore)）
- ✅ 文档里只写 `<YOUR_API_KEY>` 这种占位符
- ✅ 公开截图前确认没有包含真实 key、用户名、绝对路径
- ✅ 对外日志中不打印请求头和密钥
- ✅ 定期轮换 API Key，尤其在团队成员变更后
- ✅ Docker 场景优先使用服务端代理转发鉴权头，而不是把 key 写进前端静态资源

另外，这轮安装链路里还有两个已经落地的安全默认值：

- `scripts/run_memory_palace_mcp_stdio.sh` 在读取 `OPENCLAW_MEMORY_PALACE_ENV_FILE` 时，会直接忽略 `PATH`、`LD_PRELOAD`、`DYLD_INSERT_LIBRARIES`、`PYTHONPATH`、`PYTHONHOME`、`VIRTUAL_ENV`、`HOME`、`TMPDIR`、`SHELL` 这类进程级敏感变量，不再允许 runtime env 覆盖宿主执行环境
- `scripts/docker_one_click.sh` 生成临时 env 文件和保留副本时，会把权限直接收紧到 `0600`

---

## 3. 接口鉴权策略

### 受保护的接口范围

当配置 `MCP_API_KEY` 后，以下接口需要鉴权：

| 接口前缀 | 保护范围 | 代码出处 |
|---|---|---|
| `/maintenance/*` | 所有请求 | `backend/api/maintenance_common.py` 定义 `require_maintenance_api_key`，`backend/api/maintenance.py` 统一复用 |
| `/review/*` | 所有请求 | `backend/api/review.py` — 导入并依赖同一鉴权函数 |
| `/browse/*` | 所有请求（含读操作） | `backend/api/browse.py` — 路由统一挂载 `Depends(require_maintenance_api_key)` |
| SSE 接口 | `/sse` 与 `/messages` | `backend/run_sse.py` — ASGI 中间件 `apply_mcp_api_key_middleware`（standalone 与 embedded backend 共用） |
| `/bootstrap/*` | 本地首启配置、受控重启 | `backend/main.py` — `require_bootstrap_access`（始终只允许直连 loopback；不走浏览器侧 maintenance key 注入） |

> 📖 `/browse/node` 的 `GET` 请求也在鉴权范围内，请携带 `X-MCP-API-Key` 或 `Authorization: Bearer`。

### 鉴权方式（二选一）

**Header 方式（推荐）：**

```
X-MCP-API-Key: <MCP_API_KEY>
```

**Bearer Token 方式：**

```
Authorization: Bearer <MCP_API_KEY>
```

> 后端使用 `hmac.compare_digest` 进行恒等时间比较（`maintenance` 侧在 `backend/api/maintenance_common.py`，SSE 侧在 `backend/run_sse.py`），防止时序攻击。

### 无 Key 时的默认行为

鉴权遵循 **fail-closed** 策略，具体逻辑如下：

| 条件 | 行为 | HTTP 响应 |
|---|---|---|
| `MCP_API_KEY` 已设置且请求携带正确 Key | ✅ 放行 | — |
| `MCP_API_KEY` 已设置但 Key 错误或缺失 | ❌ 拒绝 | `401`，`reason: invalid_or_missing_api_key` |
| `MCP_API_KEY` 为空，`MCP_API_KEY_ALLOW_INSECURE_LOCAL=true`，请求来自 loopback 且不包含 `Forwarded` / `X-Forwarded-*` / `X-Real-IP` 等转发头 | ✅ 放行 | — |
| `MCP_API_KEY` 为空，`MCP_API_KEY_ALLOW_INSECURE_LOCAL=true`，请求来自 loopback 但包含 `Forwarded` / `X-Forwarded-*` / `X-Real-IP` 等转发头 | ❌ 拒绝 | `401`，`reason: insecure_local_override_requires_loopback` |
| `MCP_API_KEY` 为空，`MCP_API_KEY_ALLOW_INSECURE_LOCAL=true`，请求非 loopback | ❌ 拒绝 | `401`，`reason: insecure_local_override_requires_loopback` |
| `MCP_API_KEY` 为空，未开启 insecure local | ❌ 拒绝 | `401`，`reason: api_key_not_configured` |

> 📌 Loopback 地址仅包含 `127.0.0.1`、`::1`、`localhost`（代码常量 `_LOOPBACK_CLIENT_HOSTS`）；且必须为直连本机请求（无 `Forwarded` / `X-Forwarded-*` / `X-Real-IP` 等转发头）。
>
> 📌 **bootstrap 例外**：`/bootstrap/status`、`/bootstrap/provider-probe`、`/bootstrap/apply`、`/bootstrap/restart` 不直接套用上面这张表。当前真实行为是：
>
> - `/bootstrap/*` 始终只允许**直连 loopback**
> - 非 loopback 或带 forwarding headers 的请求会被拒绝，返回 `403` / `reason: loopback_required`
> - 如果 backend 已经配置了 `MCP_API_KEY`
>   - `/bootstrap/provider-probe` / `/bootstrap/apply` / `/bootstrap/restart` 也必须带这把 key
> - 如果 backend 还没配置 `MCP_API_KEY`
>   - loopback 下的 bootstrap 预检 / 写操作仍可继续
>   - 但 backend 会打一次明确 warning，说明这轮 bootstrap 写路径当前未受 key 保护
> - Dashboard 顶部 `Set API key` / `Update API key` 这类浏览器侧 maintenance key 注入，当前会给 `/bootstrap/provider-probe`、`/bootstrap/apply`、`/bootstrap/restart` 带同一把 key；服务端仍会继续检查 loopback 与 bootstrap 自己的门控规则

### 当前仓库中的验证锚点

以上鉴权逻辑在当前仓库的以下测试文件中有覆盖：

- `backend/tests/test_week6_maintenance_auth.py` — 维护 API 五项鉴权场景
- `backend/tests/test_week6_sse_auth.py` — SSE 鉴权场景
- `backend/tests/test_sensitive_api_auth.py` — Review 与 Browse 读写鉴权
- `backend/tests/test_review_rollback.py` — Review 操作携带鉴权测试

---

## 4. 前端密钥注入（运行时）

前端不在构建时写死密钥，而是通过运行时注入。在 `index.html` 或部署脚本中添加：

```html
<script>
  window.__MEMORY_PALACE_RUNTIME__ = {
    maintenanceApiKey: "<YOUR_MCP_API_KEY>",
    maintenanceApiKeyMode: "header"  // 可选值: "header" | "bearer"
  };
</script>
```

**工作原理**（参见 `frontend/src/lib/api.js`）：

1. `readRuntimeMaintenanceAuth()` 读取 `window.__MEMORY_PALACE_RUNTIME__`
2. axios 请求拦截器 `isProtectedApiRequest()` 判断请求是否需要鉴权
3. 对 `/maintenance/*`、`/review/*` 和 `/browse/*`（含读写）自动注入鉴权头

这里再强调一次，避免误会：

- 这套浏览器侧注入当前**也会覆盖** `/bootstrap/provider-probe`、`/bootstrap/apply`、`/bootstrap/restart`
- 但 `/setup` 页面依赖的 bootstrap 请求仍要遵守 bootstrap 自己的 loopback / key 门控规则
- Docker 一键部署默认不受这个问题影响，因为代理层会在服务端统一补头

如果你不用运行时注入，也可以在页面右上角手工输入 `Set API key` / `Update API key`。这条**浏览器侧兜底 key** 现在仅保存在当前页面的内存中（memory-only），关闭标签页或刷新后需重新输入，或由部署脚本通过 `window.__MEMORY_PALACE_RUNTIME__` 注入：

- 关闭标签页或刷新页面后需重新输入
- 不会写入 `sessionStorage` 或 `localStorage`
- 启动时会主动清除历史遗留的持久化副本

> 兼容性：也支持旧字段名 `window.__MCP_RUNTIME_CONFIG__`（同一文件第 14 行 fallback 逻辑）。

**Docker 一键部署的默认做法不一样：**

- `apply_profile.*` 在 `docker` 平台下如果发现 `MCP_API_KEY` 为空，会自动生成一把本地 key
- 前端容器不会把这把 key 写进页面，而是由 Nginx 代理在服务端转发到 `/api/*`、`/sse`、`/messages`
- 这样浏览器可以直接使用 Dashboard，但不会在页面源码里暴露真实 key

### `/bootstrap/restart` 的边界

当前 `/bootstrap/restart` 只面向**本机 loopback 场景**：

- 适用于你在本机直接启动的 backend API
- 适用于 `/setup` 页面在本机完成配置后，点 `Restart Backend` 载入最新 env
- 不适用于远程反向代理、共享测试环境，或带 forwarded headers 的访问链路

说人话就是：这条接口的目标是帮本机开发 / 本机试用少走一步手工重启，不是远程运维入口。

**前端测试覆盖：**

- `frontend/src/lib/api.contract.test.js` — 验证 runtime config 注入与鉴权头附加

---

## 5. Docker 安全

以下安全配置可在项目 Docker 文件中直接验证：

| 安全措施 | 实现方式 | 文件引用 |
|---|---|---|
| 非 root 运行（后端） | `groupadd --gid 10001 app && useradd --uid 10001` | `deploy/docker/Dockerfile.backend` |
| 非 root 运行（前端） | 使用 `nginxinc/nginx-unprivileged:1.27-alpine` 基础镜像 | `deploy/docker/Dockerfile.frontend` |
| 前端代理鉴权 | 由 Nginx 在服务端转发 `X-MCP-API-Key`，浏览器侧不保存真实 key | `deploy/docker/nginx.conf.template` |
| 禁止提权 | `security_opt: no-new-privileges:true` | `docker-compose.yml` 第 13 行 |
| 数据持久化 | Docker Volume `memory_palace_data` 挂载到 `/app/data` | `docker-compose.yml` 第 9、40 行 |
| 健康检查（后端） | Python `urllib.request.urlopen('http://127.0.0.1:8000/health')` | `docker-compose.yml` 第 15 行 |
| 健康检查（前端） | `wget -q -O - http://127.0.0.1:8080/` | `docker-compose.yml` 第 32 行 |
| 启动顺序 | `frontend` 仅在 `backend` `service_healthy` 后启动；SSE 已内嵌在 backend 进程内 | `docker-compose.yml` |

---

## 6. 分享或发布前自检清单

在分享项目、交付环境或正式发布之前，请完成以下仓库卫生与安全自检步骤：

0. **一键自检（推荐）**：

   ```bash
   bash scripts/pre_publish_check.sh
   ```

   该脚本会检查：本地敏感产物是否存在、是否被 git 跟踪、已跟踪文件中的密钥模式、个人绝对路径泄露、`.env.example` 的 API key 占位状态。当前这条扫描也会额外覆盖 `README/docs` 里的通用 macOS / Windows 用户目录绝对路径，不再只匹配当前机器用户名。

   脚本会把检查结果直接输出在当前终端；如果你另外运行 `python scripts/evaluate_memory_palace_skill.py` 或 `cd backend && python ../scripts/evaluate_memory_palace_mcp_e2e.py`，对应的 Markdown 摘要会在 `<repo-root>/docs/skills/` 下本地生成或更新，通常更适合当成你自己机器上的验证记录；这些摘要默认也被 `.gitignore` 排除，所以公开 GitHub 仓库里通常不会带上。

1. **检查工作区状态** — 确认无意外暴露：

   ```bash
   git status
   ```

   应确保以下文件不在提交中（均已在 `.gitignore` 中配置）：
   - `.env`、`.env.docker`（如果你显式复用了固定 Docker env 文件）
   - `.venv`、`.mcp.json`、`.claude/`、`.codex/`、`.cursor/`、`.opencode/`、`.gemini/`、`.agent/`（通常由你本地的 sync / install 脚本生成）
   - `frontend/pnpm-lock.yaml`（当前公开仓库前端 lockfile 统一以 `frontend/package-lock.json` 为准）
   - `*.db`（数据库文件）
   - `backend/backend.log`、`frontend/frontend.log`
   - `snapshots/`、`frontend/dist/`
   - `backend/tests/benchmark/.real_profile_cache/`
   - `backend/tests/datasets/raw/`、`backend/tests/datasets/processed/` 下的本地 benchmark 原始/处理后数据
   - 任意 `.DS_Store`

2. **关键字扫描** — 检查代码和文档中是否残留真实密钥：

   ```bash
   # 搜索可能的密钥泄露（建议只看文件名，避免在终端回显真实值）
   rg -n -l "sk-[A-Za-z0-9]{16,}|AKIA[0-9A-Z]{16}|BEGIN (RSA|OPENSSH|EC|DSA) PRIVATE KEY" .
   ```

3. **检查绝对路径** — 确保文档中不包含本机路径：

   ```bash
   # 如需手工补查，请先把下面的占位符替换成你自己的实际路径前缀
   grep -rn "<user-home>" --include="*.md" <repo-root>
   grep -rn "C:/absolute/path/to/" --include="*.md" <repo-root>
   ```

   > 说明：仓库自带的 `scripts/pre_publish_check.sh` 当前也会补扫 `README` 与 `docs/` 下这类通用绝对路径模式，不需要你先把路径改成当前机器用户名再跑。

4. **运行验证** — 确认项目可复现构建：

   ```bash
   # 最小检查
   bash scripts/pre_publish_check.sh
   curl -fsS http://127.0.0.1:8000/health

   # 前端构建检查
   cd frontend && npm ci && npm run test && npm run build
   ```

   > 如需更深一层的验证，再额外运行 `cd backend && python -m pytest tests -q`。

---

## 7. 通常只在自己机器上使用的文件与维护文档

以下内容分为两类：一类已在 [`.gitignore`](../.gitignore) 中配置排除；另一类是运行脚本后在你自己的工作区生成或更新、通常更适合只留在当前机器上的摘要。

| 文件 / 目录 | 说明 |
|---|---|
| `.env`、`.env.docker`（如果你显式复用了固定 Docker env 文件） | 可能包含真实 API Key |
| `.venv`、`backend/.venv`、`frontend/.venv` | 本地虚拟环境，不应进入仓库 |
| `.mcp.json`、`.claude/`、`.codex/`、`.cursor/`、`.opencode/`、`.gemini/`、`.agent/` | 本地工具 / MCP 配置目录（通常由你本地的 sync / install 脚本生成） |
| `*.db` | SQLite 数据库文件（如 `demo.db`） |
| `backend/backend.log` | 后端运行日志 |
| `frontend/frontend.log` | 前端运行日志 |
| `snapshots/` | 本地快照目录 |
| `backend/tests/benchmark/.real_profile_cache/` | 本地 benchmark 临时数据库 |
| `__pycache__/`、`backend/.pytest_cache/` | Python 缓存 |
| `frontend/node_modules` | NPM 依赖 |
| `frontend/pnpm-lock.yaml` | 当前源码仓前端 lockfile 统一以 `frontend/package-lock.json` 为准；本地如果用 `pnpm` 做临时调试，不要把生成的 lockfile 提交回来 |
| `frontend/dist/` | 前端构建产物 |
| `.DS_Store` | macOS 系统文件 |
| `backups/` | 本地备份目录，通常只在你自己的机器上使用 |
| `backend/tests/datasets/raw/` | 公共 benchmark 原始下载目录；按本地生成处理，公开仓库只保留 manifests / `.gitkeep` |
| `backend/tests/datasets/processed/` | 公共 benchmark 处理结果目录；按本地生成处理，公开仓库只保留 manifests / `.gitkeep` |
| `docs/improvement/` | 阶段性实施计划、重测草稿、内部排障记录 |
| `<repo-root>/docs/skills/TRIGGER_SMOKE_REPORT.md` | 运行 `python scripts/evaluate_memory_palace_skill.py` 后本地生成或更新的 skill smoke 摘要（默认 `.gitignore` 排除） |
| `<repo-root>/docs/skills/MCP_LIVE_E2E_REPORT.md` | 运行 `cd backend && python ../scripts/evaluate_memory_palace_mcp_e2e.py` 后本地生成或更新的 MCP e2e 摘要（默认 `.gitignore` 排除） |
| `backend/docs/benchmark_*.md` | 本地 benchmark 分析笔记 |
| `backend/tests/benchmark_results.md` | 一次性 benchmark 汇总草稿 |
| `docs/evaluation_old_vs_new_executive_summary_2026-03-05.md` | 一次性对照摘要，更适合作为维护阶段材料；公开 GitHub 仓库里可能不存在这一类本地文件 |
| `docs/changelog/current_code_improvements_vs_legacy_docs.md` | 面向维护者的补充差异清单；公开 GitHub 仓库里可能不存在这一类本地文件 |

> 💡 保留 `.env.example` 作为配置模板提交到仓库。
>
> 💡 公开文档里建议统一使用占位符：
>
> - `<repo-root>`：仓库根目录
> - `<user-home>`：用户目录
> - `/absolute/path/to/...`：macOS / Linux 绝对路径示例
> - `C:/absolute/path/to/...`：Windows 绝对路径示例

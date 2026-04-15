> [English](TROUBLESHOOTING.en.md)

# Memory Palace 常见问题排查

本文档帮助你快速定位和解决 Memory Palace 使用过程中的常见问题。

---

## 1. 前端 502 或接口超时

**现象**：页面能打开，但列表为空或接口报错。

> 📌 当前版本还有一种很常见的“看起来像坏了，其实是正常门控”的情况：
>
> - 页面先跳到 `/setup`
> - 页面右上角出现 `Set API key`
> - `Memory / Review / Maintenance / Observability` 里出现空态、等待提示或 `401`
>
> 这通常不是前端挂了，而是**你还没给受保护接口授权**。
>
> 如果你用的是 **Docker 一键部署**，默认不需要手动点这个按钮。优先先确认是不是用了 `apply_profile.*` / `docker_one_click.*` 生成的 Docker env 文件。

**排查步骤**：

1. 确认**后端已启动**：

   ```bash
   curl -fsS http://127.0.0.1:8000/health
   ```

   > 后端健康检查端点 `GET /health` 会返回 `status`、`index`、`runtime` 等字段（参见 `backend/main.py` 中 `health()` 函数）。

2. 确认**前端代理目标正确**：

   检查 `frontend/vite.config.js` 中 `apiProxyTarget` 的值：

   ```javascript
   // 默认目标: http://127.0.0.1:8000
   const apiProxyTarget =
     process.env.MEMORY_PALACE_API_PROXY_TARGET ||
     process.env.NOCTURNE_API_PROXY_TARGET ||
     'http://127.0.0.1:8000'
   ```

   如果后端运行在其他端口，请在 `frontend/` 目录下设置环境变量后启动前端：

   ```bash
   cd frontend
   MEMORY_PALACE_API_PROXY_TARGET=http://127.0.0.1:9000 npm run dev
   ```

3. **Docker 场景**下确认端口映射：

   - 默认后端端口：`18000`（映射到容器内 `8000`）
   - 默认前端端口：`3000`（映射到容器内 `8080`）
   - 可通过 `MEMORY_PALACE_BACKEND_PORT`、`MEMORY_PALACE_FRONTEND_PORT` 环境变量覆盖（参见 `docker-compose.yml`）

4. 检查后端日志：

   ```bash
   # 本地直接启动（uvicorn/python run_sse.py）时，优先看当前终端输出
   # Docker 部署时查看容器日志
   docker compose -f docker-compose.yml logs backend --tail=50
   ```

---

## 2. `/maintenance/*`、`/review/*` 或 `/browse/*` 返回 401

**原因**：启用了 `MCP_API_KEY` 但请求没带鉴权头。注意 `/browse/node` 的读操作也受保护。

**排查与处理**：

- **方式一**：curl 加鉴权头：

  ```bash
  curl -fsS http://127.0.0.1:8000/maintenance/orphans \
    -H "X-MCP-API-Key: <YOUR_MCP_API_KEY>"
  ```

- **方式二**：使用 Bearer 格式：

  ```bash
  curl -fsS http://127.0.0.1:8000/maintenance/orphans \
    -H "Authorization: Bearer <YOUR_MCP_API_KEY>"
  ```

- **前端**：注入 `window.__MEMORY_PALACE_RUNTIME__`（详见 [SECURITY_AND_PRIVACY.md](SECURITY_AND_PRIVACY.md) 第 4 节）
- **前端页面**：
  - 如果你看到的是 `/setup`，先按页面提示选择 `basic + Profile B`，点 `Apply`
  - 如果页面继续提示 `Restart Backend`，再点一次完成本地受控重启
  - 如果后端已经配置了 `MCP_API_KEY`，也可以直接点右上角的 `Set API key` / `Update API key`

这里要再补一句，避免把两套 key 混成一件事：

- 右上角 `Set API key / Update API key`
  - 主要解决 dashboard 受保护接口
  - 在当前 loopback / 同源页面链路里，前端也会把它带到 `/bootstrap/provider-probe`、`/bootstrap/apply`、`/bootstrap/restart`
- `/setup` 里的 `status / apply / restart`
  - 仍走 `/bootstrap/*` 自己的 loopback / 同源门控
  - 如果 backend 已经配置了 `MCP_API_KEY`
    - 这条链也必须带这把 key
  - 如果 backend 还没配置 `MCP_API_KEY`
    - 当前 loopback 写操作仍可继续
    - 但 backend 会记一条 warning，提示 bootstrap 写请求当前未受 key 保护

- **本地调试** 可设置 insecure local override（仅 loopback 生效）：

  ```bash
  # .env 中添加
  MCP_API_KEY_ALLOW_INSECURE_LOCAL=true
  ```

**根据返回的 `reason` 字段判断具体原因（当前鉴权 helper 定义在 `backend/api/maintenance_common.py`，`maintenance.py` / `browse.py` / `review.py` 都复用它）：**

| `reason` | 含义 | 处理方式 |
|---|---|---|
| `invalid_or_missing_api_key` | Key 错误或未提供 | 检查 Key 是否正确 |
| `api_key_not_configured` | 本地手动启动时 `.env` 中 `MCP_API_KEY` 为空 | 设置 Key 或启用 insecure local |
| `insecure_local_override_requires_loopback` | 启用了 insecure local 但请求非 loopback | 确保从 `127.0.0.1` 或 `localhost` 访问 |

> 💡 如果你看到的是：
>
> - `Awaiting Input`
> - `Failed to load node`
> - `Connection Lost`
> - `maintenance_auth_failed | api_key_not_configured`
>
> 优先先配 key，再判断是不是别的问题。
>
> 另外补一句：前端手工输入的这把 Dashboard API key 现在仅保存在当前页面的内存中（memory-only），关闭标签页或刷新后需重新输入，或由部署脚本通过 `window.__MEMORY_PALACE_RUNTIME__` 注入。

---

## 3. SSE 启动失败或端口占用

**现象**：

- 本地手动启动 `python run_sse.py` 报 `address already in use`
- 或 Docker 下访问 `http://127.0.0.1:3000/sse` 失败

**处理**：

1. 更换端口（SSE 默认端口为 `8000`，参见 `backend/run_sse.py` 第 105 行）：

   ```bash
   HOST=127.0.0.1 PORT=8010 python run_sse.py
   ```

2. 或查找并释放被占用端口：

   ```bash
   # macOS / Linux
   lsof -i :8000
   kill -9 <PID>
   ```

   ```powershell
   # Windows PowerShell
   netstat -ano | findstr :8000
   taskkill /PID <PID> /F
   ```

3. Docker 一键部署时，优先检查前端入口：

   ```bash
   curl -i -H 'Accept: text/event-stream' http://127.0.0.1:3000/sse
   ```

   正常情况下你应该能看到：

   ```text
   event: endpoint
   data: /messages/?session_id=...
   ```

4. 如果你是**跨机器访问** `run_sse.py`：

   - 不要继续用 `HOST=127.0.0.1`
   - 改成 `HOST=0.0.0.0`（或你的实际绑定地址）
   - 同时补齐 `MCP_API_KEY`、防火墙、反向代理等网络侧保护

补一句当前口径，避免把两条路径混成一件事：

- `python run_sse.py`
  - 是 standalone SSE 调试路径
- `uvicorn main:app`
  - 现在也已经直接挂载 `/sse`、`/messages`、`/sse/messages`
- Docker / 前端代理
  - 默认走的是 backend 内嵌 SSE，不再有独立 `sse` 容器

---

### 远程访问 `/sse` 返回 `421 Misdirected Request`

**现象**：

- 本机能连，跨机器访问 `/sse` 却直接返回 `421`
- 或者你手动带了一个不对的 `Host` 头，请求被拒

**处理**：

1. 先看当前 `HOST` 是不是还在 loopback：

   ```bash
   HOST=127.0.0.1 PORT=8010 python run_sse.py
   ```

   这种写法本来就只给本机访问。要远程访问，请改成：

   ```bash
   HOST=0.0.0.0 PORT=8010 python run_sse.py
   ```

2. 如果你就是本机 loopback 调试，但收到了 `421` / `403`：

   - 先检查请求里的 `Host` / `Origin` 是否和当前地址一致
   - 再检查是否经过了会改写请求头的代理

3. 如果后端输出里**没有** Python traceback，只看到 `421` / `403`：

   - 这通常不是服务崩了
   - 而是 `Host` / `Origin` 校验按预期拒绝了请求

---

## 4. Docker 一键脚本失败

**排查步骤**：

1. 确认 Docker 可用：

   ```bash
   docker compose version
   ```

2. 确认 profile 合法（`a`、`b`、`c`、`d`）：

   ```bash
   # 查看帮助
   bash scripts/docker_one_click.sh --help
   ```

3. 端口冲突时指定端口：

   ```bash
   bash scripts/docker_one_click.sh --profile b --frontend-port 3100 --backend-port 18100
   ```

4. 镜像构建失败时，检查 Dockerfile 是否完整：
   - `deploy/docker/Dockerfile.backend` — 基于 `python:3.12-slim`
   - `deploy/docker/Dockerfile.frontend` — 基于 `node:22-alpine`（构建）+ `nginxinc/nginx-unprivileged:1.27-alpine`（运行）

> 💡 Windows 用户可使用 `scripts/docker_one_click.ps1`（PowerShell 版本）。

---

## 5. 搜索质量突然下降

**排查步骤**：

1. **查看 `degrade_reasons`**：`search_memory` MCP 工具返回的 `degrade_reasons` 字段会告诉你检索链路的具体降级原因。常见值包括：

   | `degrade_reasons` 值 | 含义 | 来源文件 |
   |---|---|---|
   | `embedding_fallback_hash` | Embedding API 不可达，回退到本地 hash | `backend/db/sqlite_client.py` |
   | `embedding_config_missing` | Embedding 配置缺失 | `backend/db/sqlite_client.py` |
   | `embedding_request_failed` | Embedding API 请求失败 | `backend/db/sqlite_client.py` |
   | `reranker_request_failed` | Reranker API 请求失败 | `backend/db/sqlite_client.py` |
   | `reranker_config_missing` | Reranker 配置缺失 | `backend/db/sqlite_client.py` |
   | `compact_gist_llm_empty` | Compact Gist LLM 返回空结果 | `backend/mcp_server.py` |
   | `index_enqueue_dropped` | 索引任务入队被丢弃 | `backend/mcp_server.py` |

   > `write_guard_exception` 属于写入/学习链路（如 `create_memory`、`update_memory`、显式学习触发），语义为写入已 fail-closed 拒绝，并非检索质量降级。
   >
   > 如果这里出现了 `embedding_fallback_hash`，当前 payload 还会同时带 `semantic_search_unavailable=true`。这时你可能仍然拿到结果，但更准确的理解应该是“关键词 / fallback 检索还活着”，而不是“语义召回仍然正常”。
   >
   > 另外补一句，避免把旧日志或 skip 误看成插件故障：
   >
   > - 旧版本里，如果宿主在 `message:preprocessed` hook 上省略了 `ctx`，可能会看到 `Hook error [message:preprocessed]`
   > - 当前版本已经把这条路径的缺失 `ctx` 归一成空对象处理，这种现象本身不应再被当成插件失败
   > - auto-capture 遇到 `Skipped: write_guard blocked create_memory` / `update_memory` 这类碰撞时，通常表示本次候选内容被正常跳过，不是插件整体失效

2. **检查 Embedding / Reranker API 可达性**：

   ```bash
   # 配置语义：RETRIEVAL_EMBEDDING_BACKEND 只控制 embedding。
   # reranker 不存在 RETRIEVAL_RERANKER_BACKEND；如需本地强制走自有 reranker API，
   # 请显式设置 RETRIEVAL_RERANKER_ENABLED=true 与 RETRIEVAL_RERANKER_API_BASE/API_KEY/MODEL。
   # 注意：RETRIEVAL_*_API_BASE 可能已包含 /v1，避免再手动拼接 /v1
   # 用实际调用端点做健康检查更准确：
   curl -fsS -X POST <RETRIEVAL_EMBEDDING_API_BASE>/embeddings \
     -H "Content-Type: application/json" \
     -d '{"model":"<RETRIEVAL_EMBEDDING_MODEL>","input":"ping"}'
   curl -fsS -X POST <RETRIEVAL_RERANKER_API_BASE>/rerank \
     -H "Content-Type: application/json" \
     -d '{"model":"<RETRIEVAL_RERANKER_MODEL>","query":"ping","documents":["pong"]}'
   ```

   > **排障顺序建议**：
   > - 先确认你当前使用的是 `router` 方案，还是分别直配 `RETRIEVAL_EMBEDDING_*`、`RETRIEVAL_RERANKER_*`、`WRITE_GUARD_LLM_* / COMPACT_GIST_LLM_*`。
   > - 如果采用直配方案，优先检查实际 `*_API_BASE` / `*_API_KEY` / `*_MODEL`。
   > - 如果采用 `router` 方案，再检查 `ROUTER_*` 配置和 router 服务本身。

3. **重建索引**（通过 MCP 工具调用）：

   ```python
   # 重建索引
   rebuild_index(wait=true)
   # 检查索引状态
   index_status()
   ```

4. **查看观测摘要**（通过 HTTP API）：

   ```bash
   curl -fsS http://127.0.0.1:8000/maintenance/observability/summary \
     -H "X-MCP-API-Key: <YOUR_MCP_API_KEY>"
   ```

5. **检查 gist audit 后台任务**（如果你在维护页触发了 gist 质量回评）：

   ```bash
   curl -fsS -X POST http://127.0.0.1:8000/maintenance/gist-audit/run \
     -H "X-MCP-API-Key: <YOUR_MCP_API_KEY>"

   curl -fsS http://127.0.0.1:8000/maintenance/gist-audit/job/<JOB_ID> \
     -H "X-MCP-API-Key: <YOUR_MCP_API_KEY>"
   ```

   - `run` 现在会立即返回 `status=queued` 和 `job_id`
   - `observability/summary` 里的 `gist_audit` 只看汇总统计；单个任务状态要看 `job/{job_id}`

6. **检查配置参数**：确认 `RETRIEVAL_RERANKER_WEIGHT` 在合理范围（`.env.example` 注释建议 `0.20 ~ 0.40`，默认 `0.25`）

7. **观测页里看到新增字段不要慌**：

   - `scope_hint`：只是告诉检索“优先看哪个范围”
   - `sm-lite`：是当前版本新增的一组轻量运行时状态，不是报错
   - `Runtime Snapshot`：是帮助你排障的摘要，不是必须每项都有值
   - `gist_audit`：是 gist 质量回评的汇总，不代表当前请求正在阻塞执行

---

## 6. 前端构建失败

```bash
cd frontend
rm -rf node_modules       # 清理缓存
npm ci                     # 全新安装依赖
npm run test               # 运行测试
npm run build              # 构建产物
```

> **Windows 用户**：使用 `rmdir /s /q node_modules` 替代 `rm -rf`。

常见原因：

- Node.js 版本不兼容：建议使用 Node.js `20.19+`（或 `>=22.12`）
- 网络问题导致 `npm ci` 失败：可配置 NPM Mirror
- 如果你在较新的非 LTS Node 上看到 `--localstorage-file was provided without a valid path`：
  - 先切回仓库建议的 LTS 版本
  - 当前前端测试 worker 已显式关闭 Node 原生 Web Storage，正常 `npm test` 路径下这条 warning 不应再出现

---

## 7. 测试失败或想做更深验证

如果你只是想先确认安装可用，先做最小运行检查：

```bash
curl -fsS http://127.0.0.1:8000/health
```

如果你想继续确认后端/前端都可用，再运行仓库自带测试：

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate           # Windows PowerShell: .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pytest tests -q

cd ../frontend
npm ci
npm run test
npm run build
```

> **Windows PowerShell 用户**：`source` 命令不可用，使用 `.\.venv\Scripts\Activate.ps1` 激活虚拟环境。

**快速定位技巧**：优先查看最近改动文件对应的测试集，再扩大全量回归：

```bash
# 只运行特定测试文件
pytest tests/test_week6_maintenance_auth.py -q

# 只运行匹配名称的测试
pytest tests -k "test_search" -q
```

---

## 8. 数据库迁移异常

**现象**：启动时报迁移锁超时，类似 `Timed out waiting for migration lock`。

**背景**：Memory Palace 使用基于文件锁的迁移机制（参见 `backend/db/migration_runner.py`），防止多个进程同时执行迁移。

**排查与处理**：

1. **检查是否有重复进程同时启动**

2. **调整锁超时**：在 `.env` 中设置（默认 `10` 秒）：

   ```bash
   DB_MIGRATION_LOCK_TIMEOUT_SEC=30
   ```

3. **手动指定锁文件路径**：

   ```bash
   DB_MIGRATION_LOCK_FILE=/tmp/memory_palace.migrate.lock
   ```

   > 如果不设置，默认锁文件为 `<数据库文件>.migrate.lock`（例如 `demo.db.migrate.lock`），保存在与数据库文件同一目录下。

4. **手动删除残留锁文件后重启**：

   ```bash
   # 找到锁文件并删除（默认在数据库文件旁）
   rm -f /path/to/demo.db.migrate.lock
   ```

**验证锚点**：当前仓库中的 `backend/tests/test_migration_runner.py` 覆盖了迁移锁与超时场景。

---

## 9. 索引重建后仍无改善

**排查步骤**：

1. **确认索引已就绪**：

   ```python
   # MCP 工具调用
   index_status()
   # 返回中应包含 index_available=true
   ```

2. **检查 Embedding 后端配置是否正确**（参见 `.env.example`）：

   | 部署档位 | `RETRIEVAL_EMBEDDING_BACKEND` 应设为 | 说明 |
   |---|---|---|
   | Profile A | `none` | 纯关键字搜索，不使用 Embedding |
   | Profile B | `hash` | 本地 hash Embedding（默认值） |
   | Profile C/D | `api` 或 `router` | 本地开发优先用 `api` 直配排障；发布验证默认回到 `router` 主链路 |

3. **确认有记忆内容**：

   ```bash
   curl -fsS \
     -H "X-MCP-API-Key: ${MCP_API_KEY}" \
     "http://127.0.0.1:8000/browse/node?domain=core&path="
   ```

4. **尝试 Sleep Consolidation**（通过 MCP 工具）：

   ```python
   rebuild_index(sleep_consolidation=true, wait=true)
   ```

   > Sleep Consolidation 会触发深度索引重建（参见 `backend/runtime_state.py` 中 `SleepTimeConsolidator`）。

5. **检查 `degrade_reasons`** 中是否存在降级标识（参见本文档第 5 节降级原因表）

---

## 10. CORS 报错（跨域访问被拒绝）

**现象**：前端请求后端 API 时浏览器报 CORS 错误。

**说明**：当前默认**不是允许所有 Origin**。后端在 `CORS_ALLOW_ORIGINS` 留空时，只放行本地常用来源：

```python
http://localhost:5173
http://127.0.0.1:5173
http://localhost:3000
http://127.0.0.1:3000
```

**如果仍然报错**，通常原因是：

- 前端开发服务器的代理未正确配置（检查 `frontend/vite.config.js`）
- Docker 部署时前端 Nginx 没有正确转发到后端（检查 `deploy/docker/nginx.conf.template`）
- 你正在从一个**不在允许列表里的浏览器来源**访问后端

**处理建议**：

- 本地开发：
  - 保持 `CORS_ALLOW_ORIGINS=` 留空即可
- 生产浏览器访问：
  - 把 `CORS_ALLOW_ORIGINS` 显式写成你的前端地址列表
  - 例如：`CORS_ALLOW_ORIGINS=https://app.example.com,https://admin.example.com`
- 不建议为了省事直接写 `*`
  - 尤其是你还需要 credentials / cookie / auth 头的时候

---

## 11. external import 返回 `rate_limit_state_unavailable` / `state_lock_timeout`

**现象**：外部导入请求被拒绝，返回里看到：

- `reason=rate_limit_state_unavailable`
- `rate_limit_state_error=state_lock_timeout`

**说人话**：共享 rate-limit state file 这次没及时抢到锁。

这通常不是“文件坏了”，更常见的是：

- 你开了共享 rate limit
- 同一时刻又有多个进程在碰这份 state file
- 默认等待时间对当前机器/磁盘/并发强度来说不够

**排查与处理**：

1. 先确认你是不是确实在用共享 state file：

   ```bash
   EXTERNAL_IMPORT_RATE_LIMIT_STATE_FILE=/path/to/external_import_rate_limit.json
   EXTERNAL_IMPORT_REQUIRE_SHARED_RATE_LIMIT=true
   ```

2. 如果这条链路本来就会多进程并发，直接把锁等待时间调大一点：

   ```bash
   EXTERNAL_IMPORT_RATE_LIMIT_STATE_LOCK_TIMEOUT_SECONDS=3.0
   ```

   常见起步值可以先试：

   - `3.0`
   - `5.0`

3. 再确认 `EXTERNAL_IMPORT_RATE_LIMIT_STATE_FILE` 指向的是：
   - 一个本地可写目录里的普通文件
   - 不是目录
   - 不是只读挂载

4. 如果你其实不需要跨进程共享 rate limit：
   - 可以去掉 `EXTERNAL_IMPORT_REQUIRE_SHARED_RATE_LIMIT=true`
   - 或者干脆不配 `EXTERNAL_IMPORT_RATE_LIMIT_STATE_FILE`

另外补一句这轮安全加固后的拒绝原因，避免第一次看到就误解成“文件坏了”：

- `symlink_not_allowed`
  - 更接近“这次导入目标本身是 symlink，当前策略直接拒绝”
- `file_changed_during_validation`
  - 更接近“文件在校验和真正打开之间被替换过”
  - 当前会按风险处理，不继续读
- `path_not_allowed`
  - 更接近“这次导入目标虽然最开始看起来还在允许根目录里，但最终解析路径已经跳到允许根目录外”
  - 当前会直接 fail-close，不继续读

**补一句**：

- `EXTERNAL_IMPORT_RATE_LIMIT_STATE_LOCK_TIMEOUT_SECONDS` 只影响这把 state file 锁等多久
- 它不会改变主业务限流窗口，也不会放宽 `allowed_roots / allowed_exts / max_files / max_total_bytes`

---

## 12. 后端启动时报 `No module named 'diff_match_patch'`

**现象**：启动 `backend/main.py`、`run_sse.py` 或调用 `/review/diff` 前后，看到 `ModuleNotFoundError: No module named 'diff_match_patch'`。

**当前真实行为**：

- `diff_match_patch` 现在是可选依赖
- 如果它存在，`/review/diff` 会优先返回语义化 HTML diff
- 如果它缺失，后端会自动回退到 `difflib.HtmlDiff` 表格 diff 与 `difflib.SequenceMatcher` 摘要
- 所以这个包缺失不该再把后端启动直接打死

**排查与处理**：

1. 先确认当前实际跑的是不是这份仓库代码：

   ```bash
   cd backend && python - <<'PY'
   from api.utils import get_text_diff
   html, unified, summary = get_text_diff("old\n", "new\n")
   print("<table class=\"diff\"" in html, "--- old_version" in unified, summary)
   PY
   ```

2. 如果你只是想恢复更好的语义化 diff 展示，再安装这个可选包：

   ```bash
   cd backend && pip install diff-match-patch
   ```

3. 如果你仍然在启动阶段直接因为这个错误退出：
   - 优先确认运行的不是旧 venv、旧 Docker 镜像、旧 wheel
   - 再确认本地进程没有指向其他仓库路径

**边界说明**：fallback 只保证服务可用和 review 链路不断；语义化 diff 展示质量仍然以 `diff_match_patch` 更好。

---

## 13. 获取帮助

如果以上步骤无法解决你的问题：

1. 查看后端完整日志：本地看启动终端输出，Docker 看 `docker compose -f docker-compose.yml logs backend --tail=200`
2. 检查 `GET /health` 返回的 `status` 和 `index` 字段
3. 通过 `GET /maintenance/observability/summary` 查看系统运行概况（该接口受 `MCP_API_KEY` 保护，请携带 `X-MCP-API-Key` 或 `Authorization: Bearer`）
4. 提交 Issue 时请附上：错误信息、操作系统、Python 版本、Node.js 版本、使用的 Profile 档位

> [English](SKILLS_QUICKSTART.en.md)

# Memory Palace Skills 快速上手

> 这份文档专门写给“先跑通、先用起来”的人。
>
> 不讲一大堆抽象概念，就回答三件事：**这套 skills 到底是什么、当前仓库怎么直接用、四个客户端分别怎么配。**
>
> 先补一句边界：
>
> - 这页讲的是 **direct skill + MCP** 路线
> - 如果你当前走的是 OpenClaw plugin，优先看 `docs/openclaw-doc/README.md`
> - OpenClaw `setup` 已经会把 plugin-bundled skill 和 runtime 一起接好，通常不需要再按这页手动装 canonical skill
> - 对整个仓库来说，这页是**次入口**，不是主入口

---

## 🚀 先说结论

当前这个仓库已经把 `memory-palace` 的 **canonical skill**、同步脚本和安装脚本整理好了。按下面命令执行后，你可以在**自己的本地工作区**把 direct skill + MCP 这条辅路线接起来：

| 客户端 | skill 自动识别 | MCP 连接现状 | 你该怎么做 |
|---|---|---|---|
| `Claude Code` | 执行 workspace 安装后即可 | workspace 安装后会生成 repo-local skill 和项目级 MCP 入口 | 先执行本文命令，再在本仓库打开 |
| `Gemini CLI` | 执行 workspace 安装后即可 | workspace 安装后可生成 `.gemini/settings.json`；是否稳定连上仍建议你本机检查 | 先执行本文命令；需要更稳时再补一次 user-scope 安装 |
| `Codex CLI` | sync 后有 repo-local skill | `--scope user --with-mcp` 会直接写用户级 MCP 配置 | 先跑安装脚本，再用 `codex mcp list` 检查；只有脚本入口缺失时才手工补 |
| `OpenCode` | sync 后有 repo-local skill | `--scope user --with-mcp` 会直接写 OpenCode MCP 配置 | 先跑安装脚本，再用 `opencode mcp list` 检查；只有脚本入口缺失时才手工补 |

一句话理解：

- **skill** 负责“什么时候该进入 Memory Palace 工作流”
- **MCP** 负责“真正去调用 `read_memory / search_memory / update_memory` 这些工具”
- 两个都到位，才叫“真的能自动触发并且真的能干活”

> 当前公开口径：
>
> - 当前仓库已经提供 `sync / install / --check / smoke / evaluator` 这套验证入口
> - direct skill + MCP 这条路线能不能在你的机器上稳定工作，仍以你本机复跑结果为准
> - `Cursor / Antigravity` 目前仍是 **PARTIAL / 需人工验证**

---

## 🧠 skill 和 MCP 到底啥关系

可以把它理解成：

- **skill** = 司机脑子里的“出车规则”
- **MCP** = 真正的车和方向盘

只有 skill，没有 MCP：

- 模型知道“这时候应该用 Memory Palace”
- 但真到要读写记忆时，没工具可调

只有 MCP，没有 skill：

- 工具明明存在
- 但模型不一定知道什么时候该用，容易漏触发、误触发

所以当前仓库做的事情，本质上就是把这两层一起补齐。

---

## ✅ 运行同步 / 安装后，本地通常会看到什么

公开仓库默认只带 canonical bundle。你执行上面的同步 / 安装命令后，本地工作区通常会看到这些关键入口：

| 文件 | 作用 |
|---|---|
| `docs/skills/memory-palace/` | canonical skill 真源（公开仓库默认存在） |
| `.claude/skills/memory-palace/SKILL.md` | Claude Code 的 repo-local skill 镜像（本地生成） |
| `.codex/skills/memory-palace/SKILL.md` | Codex 的 repo-local skill 镜像（本地生成） |
| `.opencode/skills/memory-palace/SKILL.md` | OpenCode 的 repo-local skill 镜像（本地生成） |
| `.gemini/skills/memory-palace/SKILL.md` | Gemini 的 repo-local skill 入口（本地生成） |
| `.gemini/settings.json` | Gemini 的项目级 MCP 配置（workspace 安装后生成） |
| `.mcp.json` | Claude Code 的项目级 MCP 配置（workspace 安装后生成） |

所以：

- `Claude Code`、`Gemini CLI` 在**当前仓库执行完 workspace 安装后**是最省心的路线
- `Codex CLI` 和 `OpenCode` 的 **skill** 已经就位
- `Codex CLI` 和 `OpenCode` 仍建议先手动确认一次 MCP 是否真的接上

---

## 🛠️ 推荐安装顺序

如果你只是想把当前仓库这条链路接通，按这个顺序做：

### 1) 先同步 repo-local skill mirrors

```bash
python scripts/sync_memory_palace_skill.py
python scripts/sync_memory_palace_skill.py --check
```

### 2) 再打通当前工作区的 workspace 入口

```bash
python scripts/install_skill.py \
  --targets claude,codex,gemini,opencode \
  --scope workspace \
  --with-mcp \
  --force
```

检查：

```bash
python scripts/install_skill.py \
  --targets claude,codex,gemini,opencode \
  --scope workspace \
  --with-mcp \
  --check
```

### 3) 最后补 user-scope MCP

这一步主要是给 `Codex` / `OpenCode`，以及需要跨仓复用的 `Claude` / `Gemini`。
按当前脚本真实行为，`--scope user --with-mcp` 会直接把 `Codex / OpenCode`
对应的 user-scope MCP 配置写进去，不需要你再手工补一遍：

```bash
python scripts/install_skill.py \
  --targets claude,codex,gemini,opencode \
  --scope user \
  --with-mcp \
  --force
```

检查：

```bash
python scripts/install_skill.py \
  --targets claude,codex,gemini,opencode \
  --scope user \
  --with-mcp \
  --check
```

一句话理解：

- `Claude / Gemini` 更接近 **workspace 直连**
- `Codex / OpenCode` 更接近 **repo-local skill + user-scope MCP**

---

## 🛠️ 四个客户端怎么配

## 1) `Claude Code`

最省心。

先执行上面的 workspace 安装后，本地工作区里会有：

- `.claude/skills/memory-palace/`
- `.mcp.json`

你只要在这个仓库里启动 `Claude Code`，它就同时看得到：

1. `memory-palace` skill
2. `memory-palace` MCP server

推荐检查：

```bash
claude mcp list
```

如果你看到项目里有 `memory-palace`，基本就对了。

更稳的判断方式不是看一句“能不能用”，而是直接看 `claude mcp list` 和后面的 smoke / e2e。

---

## 2) `Gemini CLI`

先执行上面的 workspace 安装后，本地工作区里会补齐：

- `.gemini/skills/memory-palace/SKILL.md`
- `.gemini/settings.json`

所以在**当前工作区本地**里，Gemini 可以直接走项目级入口。

推荐检查：

```bash
gemini skills list --all
gemini mcp list
```

如果你想把这套能力带到**别的仓库**复用，再执行：

```bash
python scripts/install_skill.py --targets gemini --scope user --with-mcp --force
```

这一步属于“跨仓复用”，不是“当前仓库最小可用”的必需步骤。
另外要按脚本当前口径理解：Gemini 的 workspace-local 路线在某些机器上仍然更容易受限；
如果你要稳定复用，优先还是 `--scope user`。

如果你看到这种提示：

- `Skill conflict detected`
- `... overriding the same skill from ~/.gemini/skills/...`

这通常不是坏事，表示**当前工作区里的 skill 正在覆盖用户目录里的旧版本**。

如果你看到这种提示：

- `gemini mcp list` 里 `memory-palace` 是 `Disconnected`
- 或 Gemini 回答里出现 `MCP issues detected`

先把旧条目删掉，再重新跑脚本支持的入口：

```bash
gemini mcp remove memory-palace
python scripts/install_skill.py --targets gemini --scope workspace --with-mcp --force
python scripts/install_skill.py --targets gemini --scope user --with-mcp --force
```

如果你必须手工补录，也不要再直接 `cd backend && python mcp_server.py`。
当前仓库唯一推荐的手工兜底入口是仓库 wrapper：

```text
command: bash
args:
  - /ABS/PATH/TO/REPO/scripts/run_memory_palace_mcp_stdio.sh
```

---

## 3) `Codex CLI`

`Codex` 这边要分开看：

- **skill**：执行 `sync/install` 后，本地会有 `.codex/skills/memory-palace/`
- **MCP**：当前安装脚本在 `--scope user --with-mcp` 时会直接写 `~/.codex/config.toml`

说人话就是：

- 在这个仓库里，`Codex` 已经知道有 `memory-palace` 这套 skill
- 但你第一次在自己的机器上用时，还要告诉它“Memory Palace MCP 服务器怎么启动”

如果你已经执行过上面的 `--scope user --with-mcp`，这里通常**不用再手工执行一次**。
更稳的顺序是先检查：

```bash
codex mcp list
```

只有在这里**仍然看不到** `memory-palace`，或者你明确跳过了 user-scope MCP 安装时，
才需要手工执行一次，而且要直接绑定仓库 wrapper，而不是手写 `DATABASE_URL=.../backend/memory.db`：

```bash
codex mcp add memory-palace \
  -- bash /ABS/PATH/TO/REPO/scripts/run_memory_palace_mcp_stdio.sh
```

然后检查：

```bash
codex mcp list
```

注意：

- 把上面的 `/ABS/PATH/TO/REPO` 换成你的真实仓库路径
- 这条配置会写到 `~/.codex/config.toml`
- wrapper 默认会把数据库落到 `backend/data/memory-palace.db`，避免你手工起一条 `backend/memory.db` 支线把记忆写散
- 这是 `Codex CLI` 当前的产品行为，不是本仓库少了文件

---

## 4) `OpenCode`

`OpenCode` 这边在你执行 `sync/install` 后，本地通常会有：

- `.opencode/skills/memory-palace/`

这条接法的关键不是“看起来有 skill 目录”，而是你本机的 `mcp list` 里真的有可用入口。
按当前脚本真实行为，`--scope user --with-mcp` 会直接把 OpenCode 的 user-scope MCP
配置写到它自己的配置文件里。

但如果你换一台新机器，更稳妥的顺序是：

```bash
opencode mcp list
```

如果已经能看到 `memory-palace`，那就不用再手工补。

如果看不到，就在 `OpenCode` 自己的 MCP 管理入口里新增一个本地 stdio server，核心参数就是：

```text
name: memory-palace
type: local / stdio
command: bash
args:
  - /ABS/PATH/TO/REPO/scripts/run_memory_palace_mcp_stdio.sh
```

不同版本的 `OpenCode` 交互入口可能长得不一样，但要填的本质就是这几项。
这里也不要再手工内联 `backend/memory.db`；否则你很容易把 wrapper 路线和手工直跑路线拆成两套数据库。

---

## 🔍 怎么判断“真的触发成功了”

最简单的正向问题：

```text
先从 system://boot 读一下，再帮我查最近关于部署偏好的记忆。
```

如果命中了 `memory-palace`，回答或执行里通常会体现这些信号：

- 先走 `read_memory("system://boot")`
- 不会直接瞎写
- 会提到 `search_memory(..., include_session=true)` 或等价 recall 流程

最简单的反向问题：

```text
给我重写 README 的开头介绍。
```

这类纯文档任务**不应该**误触发 Memory Palace 工作流。

---

## 🧪 仓库里已经有的验证命令

先看 skill 镜像有没有漂移：

```bash
python scripts/sync_memory_palace_skill.py --check
```

再看仓库里已经准备好的 smoke / evaluator：

```bash
python scripts/evaluate_memory_palace_skill.py
```

再看真实 MCP 调用链：

```bash
cd backend && python ../scripts/evaluate_memory_palace_mcp_e2e.py
```

这两条脚本都会在本地生成验证报告，例如：

- `TRIGGER_SMOKE_REPORT.md`（脱敏后的 smoke 摘要）
- `MCP_LIVE_E2E_REPORT.md`

默认建议把它们当成你自己机器上的复核产物，不把它们当成主入口文档；这些文件默认也被 `.gitignore` 排除，所以公开 GitHub 仓库里通常不会带上。

---

## 🙋 常见误区

### 误区 1：看到 skill 文件就等于能用了

不是。

skill 只解决“该不该触发”。
真正要调工具，还得有 MCP server 配置。

### 误区 2：Gemini 发现了 skill，就一定能稳定触发

也不是。

Gemini 对隐藏目录有时更保守，所以这套安装链才会在你本地同时补：

- `.gemini/skills/...`
- `.gemini/settings.json`
- `variants/gemini/SKILL.md`

### 误区 3：本地已经有 `.codex/skills/...`，就不用配 MCP

还是不够。

`Codex` 的 MCP 目前主要看用户级配置 `~/.codex/config.toml`。

### 误区 4：只同步了 skill mirrors

现象：

- 看起来 skill 在
- 但 MCP 没绑到当前仓库

### 误区 5：只补了 MCP，没补 skill

现象：

- 工具能用
- 但客户端不会自动进入 Memory Palace 工作流

### 误区 6：直接依赖隐藏路径

现象：

- skill 已加载
- 但读取 `.gemini/skills/...` 或 `.codex/skills/...` 时被本机策略拦掉

所以更稳的做法仍然是优先引用 repo-visible 路径：

```text
docs/skills/memory-palace/references/mcp-workflow.md
docs/skills/memory-palace/references/trigger-samples.md
```

上面这两条是**运行时 canonical path**。
如果你只是中文阅读，对应看同目录下的 `*.zh.md` 即可。

---

## 📚 继续往下看什么

如果你已经能跑起来，下一步按这个顺序读：

1. [MEMORY_PALACE_SKILLS.md](MEMORY_PALACE_SKILLS.md) —— 设计原则、维护边界、为什么要优先引用 repo-visible canonical path
2. [memory-palace/SKILL.zh.md](memory-palace/SKILL.zh.md) —— skill 本体的中文对照阅读页
3. [memory-palace/references/mcp-workflow.zh.md](memory-palace/references/mcp-workflow.zh.md) —— MCP 工作流中文对照阅读页
4. [memory-palace/references/trigger-samples.zh.md](memory-palace/references/trigger-samples.zh.md) —— trigger 样例集中文对照阅读页

如果你只想先验证现在是不是通的，就盯住这 3 条命令：

```bash
python scripts/sync_memory_palace_skill.py --check
python scripts/evaluate_memory_palace_skill.py
cd backend && python ../scripts/evaluate_memory_palace_mcp_e2e.py
```

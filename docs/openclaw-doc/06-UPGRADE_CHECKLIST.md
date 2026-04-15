> [English](06-UPGRADE_CHECKLIST.en.md)

# 06 · 升级测试 Review 清单（维护者附录）

这页现在只保留一件事：

> 给维护者一份发布前最小复核清单。

先说定位：

- 这页是维护者附录
- 不是普通用户默认入口
- 最新公开验证数字统一看 `../EVALUATION.md`

---

## 1. 发布前最少要复核什么

1. 安装和命令面
   - `setup / verify / doctor / smoke / migrate / upgrade` 仍在稳定命令面上
2. 插件包形态
   - source-repo 路径和 local-tgz 路径都还能被 OpenClaw 正常识别
3. 用户口径
   - OpenClaw 用户默认面对的是 active memory plugin
   - 不是“裸 MCP 工具箱”
4. 文档卫生
   - 公开页没有把维护者本机路径、私有模型配置、历史计划写成当前主线

---

## 2. 当前仍要保守写的边界

- 目标环境仍然要重跑
- Windows / macOS / Linux 结论不能互相替代
- `C/D` 仍然依赖用户自己的模型服务
- fresh runtime 下的 `warn but ok=true` 不能直接等价为失败
- 当前 repo 不再把 active `.github/workflows/*` 当公开验证主入口

---

## 3. 先过宿主自检 gate

```bash
openclaw --version
openclaw doctor --help
openclaw plugins list --json
openclaw status --json
openclaw health --json
```

如果这轮还覆盖本机 gateway / Control UI / 浏览器链路，再补一条：

```bash
openclaw gateway status --json
```

如果这些宿主命令本身就报错、卡住，或者直接出现 `Cannot find package ...` / `Cannot find module ...`，先停在这里。

这时更像是：

- 宿主自己的 install / plugin path 状态没修好
- 宿主 bundled extension runtime 依赖没补齐

这一步没过时，不要继续把问题直接写成 `memory-palace verify / doctor / smoke` 回归。

如果这一步没过，当前更保守的处理顺序是：

1. 先跑：

```bash
openclaw doctor --fix
```

2. 再把上面的宿主自检命令重跑一遍
3. 如果还是坏，用原来的安装方式重装当前 OpenClaw 版本，再重跑宿主自检
4. 只有宿主 gate 先转绿，才继续记 `memory-palace` 侧回归

---

## 4. 最小复核命令

```bash
cd extensions/memory-palace
npm pack
openclaw plugins install --dangerously-force-unsafe-install ./openclaw-memory-palace-<version>.tgz
openclaw plugins inspect memory-palace --json
```

如果这里安装的是**你刚从当前仓库打出来的本地 tgz**，`OpenClaw 2026.4.5+` 需要显式带上 `--dangerously-force-unsafe-install`；否则宿主会把插件里的本地 launcher / helper 代码当成危险模式直接拦下。

再补一句边界：

- 这条 flag 只给**当前仓库自己打出来的本地 tgz**用
- 不要复用到来源不明的第三方插件包

```bash
openclaw memory-palace verify --json
openclaw memory-palace doctor --json
openclaw memory-palace smoke --json
```

```bash
python3 scripts/openclaw_memory_palace.py migrate --dry-run --json
python3 scripts/openclaw_memory_palace.py upgrade --dry-run --json
```

---

## 5. 这页现在怎么用

1. 用 `00-IMPLEMENTED_CAPABILITIES.md` 固定“已经做完什么”
2. 用本页复核“发布前还要看什么”
3. 用 `07-PHASED_UPGRADE_ROADMAP.md` 看历史阶段回顾与当前维护边界
4. 用 `../EVALUATION.md` 看最新公开验证基线

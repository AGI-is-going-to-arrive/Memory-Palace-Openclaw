> [English](README.en.md)

# Memory Palace Skills Docs

这页现在只做一件事：

> **把 direct skill + MCP 路线重新导回真正该看的入口。**

> 如果这个项目对你的 OpenClaw 使用有帮助，欢迎顺手点个 Star ⭐。

先把关系说清：

- 这不是当前仓库的主入口
- 它只是 **direct skill + MCP** 路线的短跳转页
- 当前仓库主入口仍然是 `../openclaw-doc/README.md`
- OpenClaw plugin 用户通常**不需要**再按这里手动装一遍 canonical skill

如果你当前走的是 **OpenClaw plugin** 路线，直接回：

- `../openclaw-doc/README.md`
- `../openclaw-doc/README.en.md`

如果你当前走的是 **direct skill + MCP** 路线，真正应该从这里开始：

1. [SKILLS_QUICKSTART.md](SKILLS_QUICKSTART.md)
2. [MEMORY_PALACE_SKILLS.md](MEMORY_PALACE_SKILLS.md)
3. 如果你要英文版，对应看：
   [SKILLS_QUICKSTART.en.md](SKILLS_QUICKSTART.en.md) /
   [MEMORY_PALACE_SKILLS.en.md](MEMORY_PALACE_SKILLS.en.md)

一句话理解：

- `SKILLS_QUICKSTART.md` = 先接通、先验证
- `MEMORY_PALACE_SKILLS.md` = 再看完整设计和维护边界

补一句边界：

- 这个目录已经补上了中英两套 quickstart / design 入口
- 这页继续只承担中文侧短跳转职责
- 本地验证摘要属于**本机复核产物**，默认不作为公开主入口
- `docs/skills/memory-palace/*` 是实现侧 repo-visible canonical refs，不是公开主入口
- 为了不改运行时文件名，深层中文对照阅读页使用 `*.zh.md`，运行时 canonical source 仍保留英文文件名

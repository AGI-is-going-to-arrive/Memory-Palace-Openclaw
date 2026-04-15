> [中文版](SKILLS_QUICKSTART.md)

# Memory Palace Skills Quickstart

> This page is for people who want to get the direct skill + MCP route working
> first, without reading the full design notes up front.

Keep the boundary clear first:

- this page is about the **direct skill + MCP** route
- if you are using the normal OpenClaw plugin path, start from
  `docs/openclaw-doc/README.en.md`
- OpenClaw `setup` already wires the bundled plugin skill and runtime for most
  normal plugin users
- for the repository as a whole, this is still a **secondary entry**, not the
  main product entry

---

## Quick Conclusion

This repository already ships:

- the canonical `memory-palace` skill bundle
- sync scripts
- install scripts
- repo-local mirror generation

Use these when you want the direct route in your own local workspace:

| Client | Skill discovery | MCP state | Recommended move |
|---|---|---|---|
| `Claude Code` | ready after workspace install | repo-local MCP entry is generated | install, then open this repo |
| `Gemini CLI` | ready after workspace install | workspace/project config can be generated | install first, then check locally |
| `Codex CLI` | repo-local skill mirror available | user-scope MCP can be written by the installer | install, then verify with `codex mcp list` |
| `OpenCode` | repo-local skill mirror available | user-scope MCP can be written by the installer | install, then verify with `opencode mcp list` |

In plain language:

- **skill** decides when Memory Palace should be used
- **MCP** is the actual tool surface that performs memory operations
- you need both for reliable triggering and actual memory reads/writes
- for `Codex CLI` and `OpenCode`, the repo-local skill mirror alone is not the full story; the installer still needs to write the usable MCP entry, and user-scope MCP is usually the more important boundary than workspace files alone

---

## What You Usually Do

### 1. Sync repo-local skill mirrors

```bash
python scripts/sync_memory_palace_skill.py
python scripts/sync_memory_palace_skill.py --check
```

### 2. Install the workspace-level skill + MCP entry

```bash
python scripts/install_skill.py \
  --targets claude,codex,gemini,opencode \
  --scope workspace \
  --with-mcp \
  --force
```

### 3. If you need cross-repo reuse, add user-scope install

```bash
python scripts/install_skill.py \
  --targets claude,codex,gemini,opencode \
  --scope user \
  --with-mcp \
  --force
```

### 4. Check the result

```bash
python scripts/install_skill.py \
  --targets claude,codex,gemini,opencode \
  --scope workspace \
  --with-mcp \
  --check
```

---

## What Gets Generated Locally

After sync/install, local mirrors usually show up here:

| File or directory | Role |
|---|---|
| `docs/skills/memory-palace/` | canonical public source |
| `.claude/skills/memory-palace/SKILL.md` | Claude repo-local mirror |
| `.codex/skills/memory-palace/SKILL.md` | Codex repo-local mirror |
| `.opencode/skills/memory-palace/SKILL.md` | OpenCode repo-local mirror |
| `.gemini/skills/memory-palace/SKILL.md` | Gemini repo-local entry |
| `.gemini/settings.json` | Gemini project-level MCP config |
| `.mcp.json` | Claude Code project-level MCP config |

These local mirrors are generated on your machine. They are not the public
repository default payload.

---

## Current Public Boundary

- this direct route is real and supported
- it still depends on your own local verification
- `Cursor / Antigravity` are still partial / manual-check paths
- the normal OpenClaw plugin path remains the main product path
- `docs/skills/memory-palace/` is the repo-visible canonical source, but the public bilingual entry set still starts from `README* / SKILLS_QUICKSTART* / MEMORY_PALACE_SKILLS*`

If you want the deeper maintenance/design notes next, continue to:

- [MEMORY_PALACE_SKILLS.en.md](MEMORY_PALACE_SKILLS.en.md)

If you already have the direct route working and want the implementation refs next, read in this order:

1. [memory-palace/SKILL.md](memory-palace/SKILL.md)
2. [memory-palace/references/mcp-workflow.md](memory-palace/references/mcp-workflow.md)
3. [memory-palace/references/trigger-samples.md](memory-palace/references/trigger-samples.md)

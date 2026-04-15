> [中文版](MEMORY_PALACE_SKILLS.md)

# Memory Palace Skills Design and Maintenance Notes

This page is the maintenance baseline for the `memory-palace` skill system.

Keep the positioning clear first:

- this page is for maintainers and deep users
- it documents the canonical bundle behind the direct skill + MCP route
- it is not the main entry for normal OpenClaw plugin users

Current single source of truth:

```text
docs/skills/memory-palace/
├── SKILL.md
├── references/
├── agents/
└── variants/
```

Distribution script:

```text
scripts/sync_memory_palace_skill.py
```

Install script:

```text
scripts/install_skill.py
```

One terminology boundary up front:

- `docs/skills/memory-palace/` is the repo-visible canonical source
- the public bilingual entry set still starts from `README*`, `SKILLS_QUICKSTART*`, and `MEMORY_PALACE_SKILLS*`
- where runtime compatibility requires the English filename to stay fixed, the Chinese reading mirror lives next to it as `*.zh.md`

---

## 1. Why the Bundle Was Restructured

The older problem was not a lack of information. The problem was drift:

- strategy notes existed, but not a stable canonical bundle
- multiple CLI mirrors were maintained by hand
- trigger rules, execution rules, and verification rules were not one loop

The current design goals are:

- **distributable**
  - canonical bundle lives in `docs/skills/memory-palace/`
- **cross-client**
  - local mirrors are generated for supported clients
- **verifiable**
  - sync/install checks exist inside the repository
- **iterable**
  - trigger quality can be improved without rewriting the whole repo

---

## 2. Current Conformance Boundary

The current bundle already aligns with the modern skill layout in a few ways:

- standard `skill-name/SKILL.md` structure
- explicit trigger wording in `description`
- progressive loading through `references/`
- client-specific mirrors instead of multiple drifting sources

Still, one boundary matters:

- current verification is engineering-focused smoke / e2e validation
- it is not yet a full blind-comparator / benchmark optimization loop

So the precise statement is:

- the structure is aligned
- the evaluation flow is still lighter than a full benchmark suite

---

## 3. Directory Responsibilities

### `docs/skills/memory-palace/SKILL.md`

Responsible for:

- trigger boundary
- the shortest safe default flow
- references to deeper materials
- Chinese reading mirror: `docs/skills/memory-palace/SKILL.zh.md`

### `docs/skills/memory-palace/variants/gemini/SKILL.md`

Responsible for:

- Gemini-specific shorter anchor wording
- the first-memory-tool-call and `NOOP` handling anchors
- stronger self-reference behavior when Gemini is asked about the skill itself
- Chinese reading mirror: `docs/skills/memory-palace/variants/gemini/SKILL.zh.md`

### `docs/skills/memory-palace/variants/antigravity/global_workflows/memory-palace.md`

Responsible for:

- the Antigravity workflow wrapper for the same Memory Palace contract
- repo-local workflow and trigger-sample anchors
- Chinese reading mirror: `docs/skills/memory-palace/variants/antigravity/global_workflows/memory-palace.zh.md`

### `docs/skills/memory-palace/references/mcp-workflow.md`

Responsible for:

- the minimal safe MCP workflow across all governed tools
- recall / mutate / compact / rebuild ordering
- the repo-visible canonical paths that should be cited instead of hidden mirrors
- Chinese reading mirror: `docs/skills/memory-palace/references/mcp-workflow.zh.md`

### `docs/skills/memory-palace/references/trigger-samples.md`

Responsible for:

- the stable should-trigger / should-not-trigger / borderline prompt set
- the fixed comparison set for trigger regression work
- the repo-local trigger sample path expected by verification scripts
- Chinese reading mirror: `docs/skills/memory-palace/references/trigger-samples.zh.md`

---

## 4. Sync and Install Boundary

Use `sync_memory_palace_skill.py` when:

- you changed the canonical bundle
- you want local mirrors refreshed

Use `install_skill.py` when:

- you want workspace/user-scope installation
- you want MCP entries written for supported clients

Key boundary:

- hidden directories such as `.claude/`, `.codex/`, `.opencode/`, `.gemini/`
  are local outputs
- they are not the public canonical source
- when answering repository-local questions, prefer `docs/skills/...` repo-visible paths over hidden mirror-relative paths

---

## 5. Recommended Maintenance Order

When you change this route, keep the order simple:

1. update the canonical source
2. run sync
3. run install/check
4. verify the affected client path

That keeps the public source and the local mirrors aligned.

---

## 6. Read Next

- first-run direct path:
  - [SKILLS_QUICKSTART.en.md](SKILLS_QUICKSTART.en.md)
- implementation refs:
  - [memory-palace/SKILL.md](memory-palace/SKILL.md)
  - [memory-palace/references/mcp-workflow.md](memory-palace/references/mcp-workflow.md)
  - [memory-palace/references/trigger-samples.md](memory-palace/references/trigger-samples.md)
- main product path:
  - [../openclaw-doc/README.en.md](../openclaw-doc/README.en.md)

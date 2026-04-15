<p align="center">
  <img src="docs/images/system_architecture_bilingual_4k.png" width="1100" alt="Memory Palace Architecture Overview" />
</p>

<h1 align="center">🏛️ Memory Palace</h1>

<p align="center">
  <strong>OpenClaw memory plugin + bundled skills for durable memory.</strong>
</p>

<p align="center">
  <em>"Every conversation leaves a trace. Every trace becomes memory."</em>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License" />
  <img src="https://img.shields.io/badge/python-3.10--3.14-3776ab.svg?logo=python&logoColor=white" alt="Python" />
  <img src="https://img.shields.io/badge/FastAPI-009688.svg?logo=fastapi&logoColor=white" alt="FastAPI" />
  <img src="https://img.shields.io/badge/React-18-61dafb.svg?logo=react&logoColor=black" alt="React" />
  <img src="https://img.shields.io/badge/Vite-646cff.svg?logo=vite&logoColor=white" alt="Vite" />
  <img src="https://img.shields.io/badge/SQLite-003b57.svg?logo=sqlite&logoColor=white" alt="SQLite" />
  <img src="https://img.shields.io/badge/protocol-MCP-orange.svg" alt="MCP" />
  <img src="https://img.shields.io/badge/OpenClaw-plugin-green.svg" alt="OpenClaw plugin" />
</p>

<p align="center">
  <a href="README_CN.md">中文</a> ·
  <a href="docs/README.en.md">Docs</a> ·
  <a href="docs/openclaw-doc/README.en.md">OpenClaw</a> ·
  <a href="docs/skills/README.en.md">Skills</a> ·
  <a href="docs/EVALUATION.en.md">Evaluation Summary</a> ·
  <a href="https://github.com/AGI-is-going-to-arrive/Memory-Palace-Openclaw">GitHub</a>
</p>

> If this repo helps with your OpenClaw workflow, please give it a GitHub star ⭐.

---

## Quickstart

Assume OpenClaw is already installed on this machine (`>= 2026.3.2`).
If the host itself is not installed yet, install OpenClaw first and then come
back to `docs/openclaw-doc/01-INSTALL_AND_RUN.en.md`.
Official host install guide: `https://docs.openclaw.ai/install`

<p align="center">
  <img src="docs/images/install_boundary_bilingual_4k.png" width="1100" />
</p>

Recommended install flow:

1. If this repo is not cloned on this machine yet, clone it first. If it is already cloned, keep the local checkout.
2. In OpenClaw CLI or WebUI chat, hand the local `docs/openclaw-doc/18-CONVERSATIONAL_ONBOARDING.en.md` path to OpenClaw first.
3. If OpenClaw says the plugin is not installed yet, follow the shortest terminal install chain below.
4. After apply or setup, use `verify / doctor / smoke` as the final sign-off.

Prompt to paste into OpenClaw:

```text
I want to install Memory Palace for OpenClaw through the recommended chat-first path. First determine whether this machine already has a checked-out copy of https://github.com/AGI-is-going-to-arrive/Memory-Palace-Openclaw. If it is already cloned, start with docs/openclaw-doc/18-CONVERSATIONAL_ONBOARDING.en.md from that local repo. If it is not cloned yet, tell me to clone the repo first and then continue from docs/openclaw-doc/18-CONVERSATIONAL_ONBOARDING.en.md. If you can also open repo links, use this page only as the matching reference: https://github.com/AGI-is-going-to-arrive/Memory-Palace-Openclaw/blob/main/docs/openclaw-doc/18-CONVERSATIONAL_ONBOARDING.en.md. Once the local repo exists, prefer the local doc path over the GitHub page. Then determine whether the memory-palace plugin is already installed and loaded. If it is not installed yet, give me the shortest install chain first. If it is already installed, continue with memory_onboarding_status -> memory_onboarding_probe -> memory_onboarding_apply. Reuse any provider settings already present on the host, do not push me to the dashboard by default, start with Profile B when no provider stack is ready yet, and if embedding + reranker + LLM are already ready, recommend Profile D directly. If I provide one shared LLM API base + key + model during onboarding/setup, fan that tuple out to WRITE_GUARD / COMPACT_GIST / INTENT by default. Do not call Profile D ready until the final resolved WRITE_GUARD_* / COMPACT_GIST_* / INTENT_* fields are all non-placeholder and probe / verify / doctor / smoke pass in the target environment. Only after apply remind me to run openclaw memory-palace verify / doctor / smoke.
```

```bash
# 0. Clone the repo
git clone https://github.com/AGI-is-going-to-arrive/Memory-Palace-Openclaw.git
cd Memory-Palace-Openclaw

# 1. Terminal fallback if OpenClaw tells you the plugin is not installed yet
python3 scripts/openclaw_memory_palace.py setup --mode basic --profile b --transport stdio --json

# 2. If setup or onboarding says restartRequired=true, restart OpenClaw first

# 3. Final sign-off
openclaw memory-palace verify --json
openclaw memory-palace doctor --json
openclaw memory-palace smoke --json
```

On Windows PowerShell, the same fallback is:

```powershell
py -3 scripts/openclaw_memory_palace.py setup --mode basic --profile b --transport stdio --json
openclaw memory-palace verify --json
openclaw memory-palace doctor --json
openclaw memory-palace smoke --json
```

If you only want to confirm that the plugin is already wired into the current host, prefer `openclaw plugins inspect memory-palace --json`. Some hosts also accept `openclaw plugins info memory-palace`, but `inspect` is the explicit command surface. Do not use `openclaw skills list` as the install gate for this plugin; the onboarding skill is bundled with the plugin package rather than installed as a separate host skill.

## Recommended User Path

- Start with `docs/openclaw-doc/18-CONVERSATIONAL_ONBOARDING.en.md`. That is the recommended install path for normal users, because it covers both states: "not installed yet" and "already installed, continue setup".
- If OpenClaw tells you the plugin is not installed yet, use `setup --mode basic --profile b` as the shortest terminal fallback.
- If you already have `API base + API key + model name` ready for embedding, reranker, and LLM, strongly prefer `Profile D` as the full-feature target. If you only want the retrieval upgrade first, use `Profile C`.

Two boundaries matter here:

- the **repository wrapper** commands live under `python3 scripts/openclaw_memory_palace.py ...` (on Windows PowerShell: `py -3 scripts/openclaw_memory_palace.py ...`)
- the **stable OpenClaw user commands** live under `openclaw memory-palace ...`

Do not mix them. In particular:

- `bootstrap-status` and `provider-probe` belong to the repo wrapper / onboarding flow
- they are **not** subcommands of `openclaw memory-palace`
- if `setup` says `restartRequired=true`, finish that restart before you treat `verify / doctor / smoke` as the final sign-off
- after installation, the user-facing sign-off commands are still `verify / doctor / smoke`

Current downgraded install shapes:

- the public npm spec `@openclaw/memory-palace` currently returns `Package not found on npm`
- `openclaw plugins install memory-palace` currently resolves to a skill rather than this plugin
- the current public docs do **not** treat either of those as a recommended install path
- if you are already inside this repository checkout, use the conversational onboarding path first and keep the source-checkout `setup` chain as the terminal fallback
- local `tgz` installation remains an advanced path for users who explicitly want to validate a trusted local package

**Recommended profile path:**

- start with **Profile B** for the zero-config bootstrap
- use **Profile C** when you only want the provider-backed retrieval upgrade first
- if you already have embedding, reranker, and LLM settings ready, strongly prefer **Profile D** for the full advanced suite
- when onboarding/setup receives one shared LLM tuple (`LLM_API_BASE`, `LLM_API_KEY`, `LLM_MODEL`), it should fan that tuple out to `WRITE_GUARD_*`, `COMPACT_GIST_*`, and `INTENT_*` by default
- treat `Profile C / D` as ready only after the real probe and verification commands pass in your environment; for `Profile D`, the final resolved `WRITE_GUARD_*`, `COMPACT_GIST_*`, and `INTENT_*` fields must also all be non-placeholder

If the plugin is already installed in OpenClaw, prefer the in-chat onboarding tools
first: `memory_onboarding_status -> memory_onboarding_probe -> memory_onboarding_apply`.
The repo CLI examples below are the terminal-side fallback for a checked-out repo.

```bash
# Profile C = provider-backed retrieval first
python3 scripts/openclaw_memory_palace.py setup --mode basic --profile c --transport stdio --json

# Readiness report only (does not apply changes)
python3 scripts/openclaw_memory_palace.py onboarding --profile c --json
python3 scripts/openclaw_memory_palace.py onboarding --profile d --json

# Apply after the readiness report looks correct
python3 scripts/openclaw_memory_palace.py onboarding --profile c --apply --validate --json

# Profile D = full advanced suite target when embedding/reranker/LLM are all ready
python3 scripts/openclaw_memory_palace.py setup --mode basic --profile d --transport stdio --json
# or: python3 scripts/openclaw_memory_palace.py onboarding --profile d --apply --validate --json
```

On Windows PowerShell, run the same repo-wrapper fallback with `py -3`.

<details>
<summary><strong>Profile C/D provider configuration example</strong></summary>

Profile C requires an embedding provider and reranker. On Profile C, the LLM
assist suite remains optional and should be enabled explicitly in onboarding.
On Profile D, `setup` / `onboarding` accept one shared LLM tuple
(`LLM_API_BASE`, `LLM_API_KEY`, `LLM_MODEL`) and fan it out to
`write_guard + compact_gist + intent_llm` by default. Profile D is only ready
after those final resolved `WRITE_GUARD_*`, `COMPACT_GIST_*`, and `INTENT_*`
fields are all non-placeholder and the real `probe / verify / doctor / smoke`
checks pass. If you edit a static env file manually instead of using
onboarding/setup, explicitly fill the resolved fields below as well so
placeholder values do not trigger downgrade / fallback. Profile B itself does
not require external embedding / reranker services, but optional LLM settings
can still be reused when they already exist on the host.

Use the following environment variables, or let the repo wrapper generate a
chat-friendly readiness report first:

- chat tools inside OpenClaw: `memory_onboarding_status -> memory_onboarding_probe -> memory_onboarding_apply`
- repo wrapper chain: `python3 scripts/openclaw_memory_palace.py bootstrap-status -> provider-probe -> onboarding/setup` (on Windows PowerShell: `py -3 scripts/openclaw_memory_palace.py ...`)

```bash
# Embedding (required for Profile C/D)
RETRIEVAL_EMBEDDING_API_KEY=your-embedding-api-key
RETRIEVAL_EMBEDDING_API_BASE=https://your-embedding-provider/v1/embeddings
RETRIEVAL_EMBEDDING_MODEL=your-embedding-model
RETRIEVAL_EMBEDDING_DIM=1024          # must match your model's output dimension

# Reranker (required for Profile C/D)
RETRIEVAL_RERANKER_ENABLED=true
RETRIEVAL_RERANKER_API_KEY=your-reranker-api-key
RETRIEVAL_RERANKER_API_BASE=https://your-reranker-provider/v1/rerank
RETRIEVAL_RERANKER_MODEL=your-reranker-model

# Shared LLM input accepted by onboarding/setup (auto-fanned out on apply)
LLM_API_BASE=https://your-llm-provider/v1
LLM_API_KEY=your-llm-api-key
LLM_MODEL=your-llm-model

# Optional on Profile C. Expected on Profile D.
# For manual static env editing, fill these resolved runtime fields explicitly.
WRITE_GUARD_LLM_ENABLED=true
WRITE_GUARD_LLM_API_BASE=https://your-llm-provider/v1
WRITE_GUARD_LLM_API_KEY=your-llm-api-key
WRITE_GUARD_LLM_MODEL=your-llm-model
COMPACT_GIST_LLM_ENABLED=true
COMPACT_GIST_LLM_API_BASE=https://your-llm-provider/v1
COMPACT_GIST_LLM_API_KEY=your-llm-api-key
COMPACT_GIST_LLM_MODEL=your-llm-model
INTENT_LLM_ENABLED=true
INTENT_LLM_API_BASE=https://your-llm-provider/v1
INTENT_LLM_API_KEY=your-llm-api-key
INTENT_LLM_MODEL=your-llm-model
```

`setup` and `onboarding` probe providers and report detected dimensions. See
`.env.example` for common knobs; the final defaults live in the runtime code.

</details>

---

## Highlights

- **Made for OpenClaw**: `memory-palace` takes over the active memory slot without modifying OpenClaw source code.
- **Stable user command surface**: users work through `openclaw memory-palace ...`, not repo-only helper chains.
- **Chat-first onboarding**: once the plugin is installed, users can stay in CLI / WebUI chat for `memory_onboarding_status -> memory_onboarding_probe -> memory_onboarding_apply`; after apply, finish the sign-off with `openclaw memory-palace verify / doctor / smoke`.
- **Experimental multi-agent isolation**: current ACL evidence still shows `alpha -> stored -> beta -> UNKNOWN`, but ACL should be treated as an experimental feature rather than a hardened security boundary.
- **Clear rollout path**: `Profile B` is the zero-config bootstrap, `Profile C` is the provider-backed retrieval step, and `Profile D` is the full advanced target when embedding / reranker / LLM are all ready.

## Architecture At A Glance

- **OpenClaw side**: the `memory` slot, plugin tools, and lifecycle hooks are what users actually touch.
- **Plugin side**: recall, auto-capture, visual memory, onboarding, ACL, and host-bridge logic live in `extensions/memory-palace/`.
- **Runtime side**: `backend/` provides the FastAPI + MCP + SQLite runtime, including durable storage and hybrid retrieval.
- **Support surfaces**: Dashboard, `verify / doctor / smoke`, and packaged-install checks sit on top of the same runtime.

---

## What This Repo Is

This repository should be read primarily as the **OpenClaw `memory-palace`
plugin** plus the bundled skills and runtime it needs.

The key boundary is simple:

- the default setup path updates your local OpenClaw config so the plugin is loaded and the memory slot points to `memory-palace`
- it does **not** modify OpenClaw source code
- it does **not** replace host-side `USER.md`, `MEMORY.md`, or `memory/*.md`
- it adds durable, searchable, and auditable memory on top of the host
- the repo still keeps a direct `skill + MCP` path for `Claude Code / Codex / Gemini CLI / OpenCode`

If you are an OpenClaw user, read this repo as:

> **OpenClaw plugin first, direct MCP path second.**

If you want the fastest GitHub-friendly visual proof, start with:

- `docs/openclaw-doc/15-END_USER_INSTALL_AND_USAGE.en.md`

That page shows what users actually see in `Skills`, `Chat`, ACL isolation, and
the visible meaning of Profiles B / C / D.

The standalone HTML pages still exist, but they are optional local reference
material rather than the main GitHub entry.

---

## Start Here

- **OpenClaw users**: `docs/openclaw-doc/README.en.md`
- **Chat-first onboarding**: `docs/openclaw-doc/18-CONVERSATIONAL_ONBOARDING.en.md`
- **GitHub-friendly WebUI proof page**: `docs/openclaw-doc/15-END_USER_INSTALL_AND_USAGE.en.md`
- **Direct skill + MCP users**: `docs/skills/README.en.md`
- **Recorded validation notes**: `docs/EVALUATION.en.md`

If you are doing local backend / dashboard development rather than normal
OpenClaw installation, use `docs/GETTING_STARTED.en.md`.

---

## Validation Snapshot

Use this section as a boundary, not as a marketing claim.

- `Profile B` remains the safest first-run path.
- `Profile C / D` remain provider-dependent and should be treated as ready only after real checks pass in your environment.
- `Profile D` additionally requires the final resolved `WRITE_GUARD_*`, `COMPACT_GIST_*`, and `INTENT_*` fields to be non-placeholder; manual static env users should fill those fields explicitly instead of relying on template placeholders.
- the stable user command surface is still `openclaw memory-palace ...`
- the repo wrapper remains useful for readiness reports and guided setup, but it is not the stable user CLI surface
- the current public chat-first claim covers handing the checked-out local document page or local doc path to OpenClaw; it does not claim that every host can fetch arbitrary public GitHub URLs on its own
- the recorded repo checks already confirm that:
  - `openclaw plugins inspect memory-palace --json` reports the plugin as loaded on the real host; some hosts also accept `openclaw plugins info memory-palace`
  - `openclaw skills list` is not the install gate for the bundled onboarding skill
  - the same onboarding document can drive correct next-step guidance in CLI and WebUI, in installed and uninstalled states, in both Chinese and English
  - the latest profile-matrix run reproduced the current experimental `A / B / C / D + ACL` behavior in isolated scenarios
- exact commands, counts, and caveats live in `docs/EVALUATION.en.md`
- provider-dependent checks can still warn when the target model endpoint itself is unhealthy, so always rerun in the environment you actually plan to use

---

## Thanks

Thanks to the [linux.do community](https://linux.do/).

## Star History

<a href="https://www.star-history.com/?repos=AGI-is-going-to-arrive%2FMemory-Palace-Openclaw&type=date&legend=top-left">

 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/chart?repos=AGI-is-going-to-arrive/Memory-Palace-Openclaw&type=date&theme=dark&logscale&legend=top-left" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/chart?repos=AGI-is-going-to-arrive/Memory-Palace-Openclaw&type=date&logscale&legend=top-left" />
   <img alt="Star History Chart" src="https://api.star-history.com/chart?repos=AGI-is-going-to-arrive/Memory-Palace-Openclaw&type=date&logscale&legend=top-left" />
 </picture>

</a>

---

## License

MIT

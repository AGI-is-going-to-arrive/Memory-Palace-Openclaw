> [中文版](DEPLOYMENT_PROFILES.md)

# Memory Palace Deployment Profiles

This document helps you choose the right Memory Palace configuration profile (A / B / C / D) based on your hardware and use case, and complete the deployment.

---

## Quick Navigation

| Section | Content |
|---|---|
| [1. Three Steps to Get Started](#1-three-steps-to-get-started) | The fastest way to begin |
| [2. Profile Overview](#2-profile-overview) | Differences between A/B/C/D configurations |
| [3. Detailed Profile Configuration](#3-detailed-profile-configuration) | `.env` parameter details for each profile |
| [4. Optional LLM Parameters](#4-optional-llm-parameters-write_guard--compact_context--intent) | Write guard and context compaction |
| [5. Docker One-Click Deployment](#5-docker-one-click-deployment-recommended) | Recommended containerized deployment |
| [6. Manual Startup](#6-manual-startup) | Local startup without Docker |
| [7. Local Inference Service Reference](#7-local-inference-service-reference) | Ollama / LM Studio / vLLM / SGLang |
| [8. Vitality Parameters](#8-vitality-parameters) | Memory vitality decay and cleanup mechanism |
| [9. API Authentication](#9-api-authentication) | Maintenance / SSE / Browse / Review interface security |
| [10. Tuning and Troubleshooting](#10-tuning-and-troubleshooting) | Common issues and tuning tips |
| [11. Utility Script Reference](#11-utility-script-reference) | All deployment-related scripts |

---

## 1. Three Steps to Get Started

1. **Choose a profile**: Select `A`, `B`, `C`, or `D` based on your hardware (if unsure, start with **B** to get running; when model services are ready for long-term use, prioritize **C**)
2. **Decide on a deployment path**:
   - If you go with **Docker one-click deployment**, simply run `docker_one_click.*`; the script auto-generates a Docker env file for each run
   - If you go with **manual startup**:
     - use `apply_profile.*` when you want the conservative **A/B local template**
     - for **C/D**, prefer the **repo wrapper** `setup / provider-probe / onboarding`, or manually write real provider values into `.env`
3. **Start the services**:
   - Docker one-click deployment: the script starts frontend, backend, and SSE directly
   - Manual startup: start backend / frontend separately following the steps later in this document

> **Recommendation**: **Profile B remains the default starting profile** because it does not require external embedding/reranker services. Once model services are ready, **Profile C is the best long-term choice for most users**. If your goal is the **full advanced suite enabled by default**, then move to **Profile D**. Before upgrading to C/D, make sure you fill in embedding / reranker in the corresponding `.env` fields. If you also want to enable LLM-assisted capabilities, fill in the LLM configuration as well.
>
> If you are following the **OpenClaw plugin's `setup --profile c/d`** path, the current installer has been enhanced:
> - In a local interactive terminal, it first asks whether you want to provide a `.env` or fill in items manually
> - It first tries to reuse compatible provider hints that already exist on the host, then asks only for what is still missing
> - After filling in, it directly probes whether embedding / reranker / LLM are available
> - In default mode, a failed probe clearly indicates which components are unavailable and temporarily falls back to `Profile B`
> - In `--strict-profile` mode, it does not fall back but reports an error directly
>
> If your reranker is a locally self-hosted service like `llama.cpp` / OpenAI-compatible, manually warm up `/rerank` once before the first `--strict-profile` `setup` / `verify` / `smoke`. This avoids misinterpreting cold-start timeouts as configuration errors.

One more boundary so the two paths do not get conflated:

- the “asks questions” behavior only applies to **local interactive `setup`**
- `onboarding --json` itself returns a **structured readiness report**, not a field-by-field questionnaire
- every `setup / provider-probe / onboarding` mention here refers to `python3 scripts/openclaw_memory_palace.py ...`

---

## 2. Profile Overview

<p align="center">
  <img src="images/profile_ladder_bilingual_4k.png" width="1100" alt="Profile upgrade ladder and capability progression" />
</p>

| Profile | Search Mode | Embedding Method | Reranker | Use Case |
|:---:|---|---|---|---|
| **A** | `keyword` | Disabled (`none`) | Disabled | Minimum requirements, pure keyword search, quick validation |
| **B** | `hybrid` | Local hash (`hash`) | Disabled | **Default starting profile**, single-machine development, no extra services needed |
| **C** | `hybrid` | API call (`api`) | Enabled | **Strongly recommended profile**, local embedding/reranker model service deployment |
| **D** | `hybrid` | API call (`api`) | Enabled | Full advanced surface; providers can be local, intranet, or remote |

**Key Differences**:

- **A to B**: Upgrade from pure keyword to hybrid search using built-in hash vectors (no external services required)
- **B to C/D**: Connect real embedding + reranker models for the best semantic search results
- **C vs D**: The main difference is not “local vs remote.” D defaults to the full `write_guard / compact_gist / intent_llm` assist surface being on, while C keeps that LLM suite optional. Provider addresses can still be local, intranet, or remote for both.

> **Terminology note (to avoid confusion with evaluation docs)**: Deployment templates for C enable the reranker by default. In the "real A/B/C/D runs" in `docs/EVALUATION.en.md`, `profile_c` disables the reranker as a control group (`profile_d` enables it) to observe the performance gain.
>
> **Additional note**: in the current repository, the effective runtime path for C/D is now aligned on `RETRIEVAL_EMBEDDING_BACKEND=api`. The templates still keep a set of `ROUTER_*` fields because many real deployments place a router / gateway in front of an OpenAI-compatible endpoint. If you do not use a unified router, just fill in `RETRIEVAL_EMBEDDING_*`, `RETRIEVAL_RERANKER_*`, and `WRITE_GUARD_LLM_* / COMPACT_GIST_LLM_*` directly.
>
> **Why not force everything through router**:
> - The `embedding`, `reranker`, and `llm` pipelines have different models, addresses, keys, and failure modes. Configuring them separately makes it easier to diagnose and replace.
> - The repository already supports direct configuration: `RETRIEVAL_EMBEDDING_*`, `RETRIEVAL_RERANKER_*`, `WRITE_GUARD_LLM_* / COMPACT_GIST_LLM_*` can each work independently.
> - The primary value of `router` is on the production side: unified entry point, model orchestration, auth, rate limiting, auditing, and future provider switching. It is a common production integration path, but not the first default for ordinary users. Ordinary users should fill `RETRIEVAL_EMBEDDING_* / RETRIEVAL_RERANKER_*` first; only add `ROUTER_*` when there is a real router / gateway in front.
>
>
> **Configuration priority note (to avoid misconfiguration)**:
> - `RETRIEVAL_EMBEDDING_BACKEND` only affects the Embedding pipeline, not the Reranker.
> - There is no `RETRIEVAL_RERANKER_BACKEND` toggle; whether Reranker is enabled is controlled solely by `RETRIEVAL_RERANKER_ENABLED`.
> - Reranker address/key priority: `RETRIEVAL_RERANKER_API_BASE/API_KEY` first, then falls back to `ROUTER_API_BASE/ROUTER_API_KEY`, and finally to `OPENAI_BASE_URL/OPENAI_API_BASE` and `OPENAI_API_KEY`.

---

## 3. Detailed Profile Configuration

### Profile A -- Pure Keyword (Minimum Configuration)

Zero dependencies, keyword matching only:

```bash
# Core configuration (see deploy/profiles/macos/profile-a.env)
SEARCH_DEFAULT_MODE=keyword
RETRIEVAL_EMBEDDING_BACKEND=none
RETRIEVAL_RERANKER_ENABLED=false
RUNTIME_INDEX_WORKER_ENABLED=false    # No index worker needed
```

### Profile B -- Hybrid Search + Local Hash (Default)

Uses built-in 64-dimensional hash vectors for basic semantic capability:

```bash
# Core configuration (see deploy/profiles/macos/profile-b.env)
SEARCH_DEFAULT_MODE=hybrid
RETRIEVAL_EMBEDDING_BACKEND=hash
RETRIEVAL_EMBEDDING_MODEL=hash-v1
RETRIEVAL_EMBEDDING_DIM=64
RETRIEVAL_RERANKER_ENABLED=false
RUNTIME_INDEX_WORKER_ENABLED=true     # Enable async indexing
RUNTIME_INDEX_DEFER_ON_WRITE=true
```

The current stable public baseline is:

- `Profile B` uses `hash-v1 / 64`
- It remains the default starting profile
- Latest public validation results are documented in `docs/EVALUATION.en.md`

### Profile C/D -- Hybrid Search + Real Models (Recommended Target; C is Strongly Recommended)

C and D share the same user-facing capability boundary and both rely on real embedding + reranker. In the current installer / smoke / docker runtime, the effective path is `RETRIEVAL_EMBEDDING_BACKEND=api`, and the default templates give D a higher reranker weight (`0.35`).

> **Summary**:
> - **Profile B**: Default starting point, ensures you can get running today
> - **Profile C**: The best long-term choice for most provider-ready users
> - **Profile D**: the profile you use when you explicitly want the full advanced suite enabled by default; providers can still be local, intranet, or remote
>
> **Minimum preparation before upgrading to Profile C**:
> - Embedding: `RETRIEVAL_EMBEDDING_*`
> - Reranker: `RETRIEVAL_RERANKER_*`
> - Interactive `setup/onboarding` now explicitly asks whether to enable the optional Profile C LLM assist suite
> - The current installer explains that suite as:
>   - `write_guard`: screens risky or contradictory durable writes before commit
>   - `compact_gist`: generates better `compact_context` summaries
>   - `intent_llm`: improves intent classification/routing for ambiguous queries and remains experimental
> - If you choose to enable it, the installer asks for one shared OpenAI-compatible chat configuration and reuses it across those three features

**Profile C** (local model services) -- suitable for users with a GPU or using local inference such as Ollama/vLLM:

```bash
# Core configuration (see deploy/profiles/macos/profile-c.env)
SEARCH_DEFAULT_MODE=hybrid
RETRIEVAL_EMBEDDING_BACKEND=api

# Embedding configuration
ROUTER_API_BASE=http://127.0.0.1:PORT/v1          # <- Replace PORT with your actual port
ROUTER_API_KEY=replace-with-your-key
ROUTER_EMBEDDING_MODEL=replace-with-your-embedding-model
RETRIEVAL_EMBEDDING_MODEL=replace-with-your-embedding-model
RETRIEVAL_EMBEDDING_API_BASE=http://127.0.0.1:PORT/v1
RETRIEVAL_EMBEDDING_API_KEY=replace-with-your-key
RETRIEVAL_EMBEDDING_DIM=1024

# Reranker configuration
RETRIEVAL_RERANKER_ENABLED=true
RETRIEVAL_RERANKER_API_BASE=http://127.0.0.1:PORT/v1
RETRIEVAL_RERANKER_API_KEY=replace-with-your-key
RETRIEVAL_RERANKER_MODEL=replace-with-your-reranker-model
RETRIEVAL_RERANKER_WEIGHT=0.30                     # Recommended 0.20 ~ 0.40
```

The most commonly overlooked point in actual usage:

- The `profile c/d` default templates request `RETRIEVAL_EMBEDDING_DIM=1024`
- If you switch embedding providers, remember to also check this value
- If your provider/model requires a different dimension, override it explicitly
- The most stable approach is to write your final `RETRIEVAL_*`, `WRITE_GUARD_LLM_*`, `COMPACT_GIST_LLM_*` directly into your actual configuration file

Many embedding models support multiple output dimensions. The `provider-probe` step will auto-detect the maximum usable dimension for your specific provider and model combination.

In other words:

- The template default value is just a starting point
- If you have already obtained a more accurate real-time result through onboarding / provider-probe, trust the probe result rather than mechanically keeping the template value

The LLM rule for `Profile C` should now be read like this:

- **Profile C defaults to embedding + reranker**
- **Profile C does not force LLM on by default; the conversational installer should explicitly ask whether to enable it**
- But if you opt in during interactive setup, or explicitly provide one complete shared LLM configuration, the installer enables **write_guard / compact_gist / intent_llm** together
- If only the optional LLM probe fails while embedding + reranker are healthy, the installer now prefers to **keep Profile C** and leave the optional LLM assists disabled, instead of downgrading the whole setup to Profile B

If you do not use a unified `router`, you can also directly configure OpenAI-compatible embedding / reranker services:

```bash
# Direct connection to OpenAI-compatible services
RETRIEVAL_EMBEDDING_BACKEND=api
RETRIEVAL_RERANKER_ENABLED=true
RETRIEVAL_RERANKER_API_BASE=http://127.0.0.1:PORT/v1
RETRIEVAL_RERANKER_API_KEY=replace-with-your-key
# Fill in the actual model names for your service below
RETRIEVAL_EMBEDDING_MODEL=replace-with-your-embedding-model
RETRIEVAL_RERANKER_MODEL=replace-with-your-reranker-model
# Note: There is no RETRIEVAL_RERANKER_BACKEND configuration option
```

**Profile D** (full advanced surface) -- the snippet below is only a remote-provider example, not a hard requirement for D:

```bash
# This block only shows one remote-provider example.
# The real difference from C is not provider location; D defaults to embedding + reranker + the LLM assist surface together.
ROUTER_API_BASE=https://<your-router-host>/v1
RETRIEVAL_EMBEDDING_API_BASE=https://<your-router-host>/v1
RETRIEVAL_RERANKER_API_BASE=https://<your-router-host>/v1
RETRIEVAL_RERANKER_WEIGHT=0.35                     # Slightly higher recommended for remote
```

The current `Profile D` rule is now:

- **default target = embedding + reranker + full LLM assist suite**
- Here, “full LLM assist suite” means:
  - `write_guard`
  - `compact_gist`
  - `intent_llm`
- The installer reuses one shared LLM configuration across those three features, so `D` should no longer be read as “only write_guard uses LLM”
- If the product message is “turn on the full advanced feature surface,” the public recommendation should point to **Profile D**
- This still does **not** mean every advanced toggle in the project is auto-enabled; it only applies to the LLM assist surface

> **C/D first tuning parameter**: `RETRIEVAL_RERANKER_WEIGHT`, recommended range `0.20 ~ 0.40`, fine-tune in `0.05` increments.

The current appropriate public framing is:

- The `Profile C/D` path has documented local smoke baselines
- But they depend on your own embedding / reranker / LLM services
- Final availability should be validated by re-running in your own target environment

If you adopt the direct connection approach, the minimum verification steps are:

```bash
# 1) Start with your final configuration for the corresponding profile
bash scripts/docker_one_click.sh --profile c

# 2) Verify basic endpoints
curl -fsS http://127.0.0.1:18000/health
curl -fsS http://127.0.0.1:18000/browse/node -H "X-MCP-API-Key: <YOUR_MCP_API_KEY>"
```

Result assessment criteria:

1. Only use the **same final deployment configuration** for comparison and acceptance -- do not mix results from different pipelines.
2. Regardless of whether you use `router` or direct connection, startup + health check should pass under your final configuration.
3. If startup fails with placeholder endpoints/keys, this is expected fail-closed behavior; replace with real, working values and re-verify.

### Recommended Model Selection

Default models configured in the project profile templates:

| Purpose | Default Model | Dimensions | Notes |
|---|---|---|---|
| Embedding | `your-embedding-model` | `provider-probe` | Use the result validated in your own provider/model route |
| Reranker | `your-reranker-model` | -- | Use the result validated in your own provider/model route |

You can also replace with other OpenAI-compatible models, such as `bge-m3`, `text-embedding-3-small`, etc., by modifying the corresponding `*_MODEL` and `*_DIM` parameters.

One additional note to avoid conflicting with the probe guidance above:

- The `1024` in the table above still represents the **template default value**
- It is not the actual upper limit for all provider/model combinations
- The actual maximum dimension depends on your provider and model — run `provider-probe` to detect it

---

## 4. Optional LLM Parameters (write_guard / compact_context / intent)

These parameters control three LLM features: **write guard** (quality filtering), **context compaction** (summary generation), and **intent classification enhancement** (intent routing).

Configure in `.env`:

```bash
# Write Guard LLM (write guard, filters low-quality memories)
WRITE_GUARD_LLM_ENABLED=false
WRITE_GUARD_LLM_API_BASE=             # OpenAI-compatible /chat/completions endpoint
WRITE_GUARD_LLM_API_KEY=
WRITE_GUARD_LLM_MODEL=replace-with-your-llm-model

# Compact Context Gist LLM (context compaction, generates summaries)
COMPACT_GIST_LLM_ENABLED=false
COMPACT_GIST_LLM_API_BASE=
COMPACT_GIST_LLM_API_KEY=
COMPACT_GIST_LLM_MODEL=replace-with-your-llm-model

# Intent LLM (experimental intent classification enhancement)
INTENT_LLM_ENABLED=false
INTENT_LLM_API_BASE=
INTENT_LLM_API_KEY=
INTENT_LLM_MODEL=replace-with-your-llm-model

# Compact Gist timeout (reasoning models may need more time)
# COMPACT_GIST_TIMEOUT_SEC=45

# Write Guard score normalization (default on for Profile C/D, auto-disabled for Profile B)
# Fixes classification failure caused by qwen3-embedding cosine similarity compressed to [0.85, 1.0]
# WRITE_GUARD_SCORE_NORMALIZATION=true
# WRITE_GUARD_NORMALIZATION_FLOOR=0.85
# WRITE_GUARD_CROSS_CHECK_ADD_FLOOR=0.10

# LLM content-diff rescue: second-pass content-level judgment for borderline UPDATE/ADD (default: off)
# Requires WRITE_GUARD_LLM_ENABLED=true to take effect
# WRITE_GUARD_LLM_DIFF_RESCUE_ENABLED=false
```

> **Fallback mechanism**: When `COMPACT_GIST_LLM_*` is not configured, `compact_context` automatically falls back to using the `WRITE_GUARD_LLM_*` configuration. Both pipelines use the OpenAI-compatible chat interface (`/chat/completions`).
>
> **Default behavior added in this round**:
> - smarter `rolling summary` and conservative high-value early flush now have a **non-LLM default path**
> - current defaults are:
>   - `RUNTIME_FLUSH_HIGH_VALUE_EARLY_ENABLED=true`
>   - `RUNTIME_FLUSH_HIGH_VALUE_MIN_EVENTS=2`
>   - `RUNTIME_FLUSH_HIGH_VALUE_MIN_CHARS=120`
>   - `RUNTIME_FLUSH_HIGH_VALUE_MIN_CHARS_CJK=100`
> - this only changes `compact_context` trigger timing and summary quality; it does not change the schema, MCP API shape, or profile core logic
> - these defaults were rerun successfully in the `Profile B / C / D` high-value write checks, and repeated high-value text still does not create a flush storm
> - exact rerun commands and the latest recorded results stay in `docs/EVALUATION.en.md`
>
> **Current installer shared-LLM rule**:
> - If you provide one shared LLM configuration, the installer reuses it for `write_guard`, `compact_gist`, and `intent_llm`
> - `Profile C` treats this suite as an **optional enhancement**
> - `Profile D` treats this suite as part of the **default advanced surface**
>
> **Public guidance**: the repository does not assume one fixed vendor/model pair. Treat embedding, reranker, and optional LLMs as “your own OpenAI-compatible services,” and use provider-probe / verify / doctor / smoke as the final source of truth.
>
> **Additional note**: `INTENT_LLM_*` is an experimental capability. When disabled or unavailable, it falls back directly to keyword rules without affecting the default search path.
>
> **Complete advanced configuration**: `CORS_ALLOW_*`, `RETRIEVAL_MMR_*`, `INDEX_LITE_ENABLED`, `AUDIT_VERBOSE`, runtime observation/sleep consolidation limits, etc., are not expanded here individually. `.env.example` lists common configuration items; for complete defaults refer to `backend/` source code.
>
> **Enablement recommendations (safe to follow directly)**:
> - `INTENT_LLM_ENABLED=false`
>   - Suitable for default production / default user deployment
>   - Only try when you already have a stable chat model and want to enhance fuzzy query intent classification
> - `RETRIEVAL_MMR_ENABLED=false`
>   - Off by default
>   - Only enable when hybrid search top results clearly show excessive duplication
> - `CORS_ALLOW_ORIGINS=`
>   - Leave empty for local development; uses the built-in local allowlist
>   - For production browser access, explicitly list allowed domains; do not use `*` directly
> - `RETRIEVAL_SQLITE_VEC_ENABLED=false`
>   - Still a rollout toggle
>   - Not recommended for standard user deployment by default; only enable during maintenance phases to validate extended paths, readiness, and fallback pipelines

---

## 5. Docker One-Click Deployment (Recommended)

### Prerequisites

- [Docker](https://docs.docker.com/get-docker/) installed and Docker Engine running
- `docker compose` supported (included by default in Docker Desktop)

### macOS

```bash
cd <project-root>
bash scripts/docker_one_click.sh --profile b
# To inject API addresses/keys/models from the current shell into this run's Docker env file (disabled by default):
bash scripts/docker_one_click.sh --profile c --allow-runtime-env-injection
```

### Linux

```bash
cd <project-root>
bash scripts/docker_one_click.sh --profile b
# To inject API addresses/keys/models from the current shell into this run's Docker env file (disabled by default):
bash scripts/docker_one_click.sh --profile c --allow-runtime-env-injection
```

### Windows PowerShell

```powershell
cd <project-root>
.\scripts\docker_one_click.ps1 -Profile b
# To inject the current PowerShell process environment into this run's Docker env file (disabled by default):
.\scripts\docker_one_click.ps1 -Profile c -AllowRuntimeEnvInjection
```

> `apply_profile.ps1` now performs "keep last value" deduplication for **all duplicate env keys**, not just `DATABASE_URL`.
>
> Native Windows / `pwsh` is still recommended to be re-run separately on the target environment; these steps are for deployment verification and should not be mixed with beginner onboarding documentation.
>
> The Windows native basic-path currently under maintenance has been unified to `setup -> verify -> doctor -> smoke`, and it is still recommended to run separately on the target environment. The public baseline should also continue to be based on the latest re-run on the target environment.
>
> `docker_one_click.sh/.ps1` generates an independent temporary Docker env file for each run by default, passing it to `docker compose` via `MEMORY_PALACE_DOCKER_ENV_FILE`. It only reuses a specified path when you explicitly set that environment variable, rather than sharing a fixed `.env.docker`.
>
> If `MCP_API_KEY` is empty in this Docker env file, `apply_profile.sh/.ps1` auto-generates a local key, shared by the Dashboard proxy and SSE.
>
> Concurrent one-click deployments within the same checkout are serialized by a deployment lock, preventing shared compose project / env files from overwriting each other.

### Access Addresses After Deployment

| Service | Default Host Port | Container Internal Port | Access Method |
|---|:---:|:---:|---|
| Frontend (Web UI) | `3000` | `8080` | `http://localhost:3000` |
| Backend (API) | `18000` | `8000` | `http://localhost:18000` |
| SSE (frontend proxy) | `3000` | `8080 -> 8000` | `http://localhost:3000/sse` |
| Health Check | `18000` | `8000` | `http://localhost:18000/health` |

### What the One-Click Script Does

1. Calls the profile script to generate the Docker env file for this run from templates (per-run temporary file by default; reuses the specified path only when `MEMORY_PALACE_DOCKER_ENV_FILE` is explicitly set)
2. Disables runtime environment injection by default to avoid implicit template overrides; only overrides runtime parameters when the injection flag is explicitly enabled. For `profile c/d`, injection mode additionally forces `RETRIEVAL_EMBEDDING_BACKEND=api` for local integration.
3. Automatically detects port conflicts and increments to find available ports if defaults are occupied
4. Detects existing data volumes (`memory_palace_data` or `nocturne_*` series) and automatically reuses them to preserve historical data
5. Applies a deployment lock for concurrent deployments within the same checkout, preventing multiple `docker_one_click` runs from overwriting each other
6. Builds and starts backend and frontend containers using `docker compose`; the SSE entry point is embedded within the backend process

### Security Notes

- **Backend container**: Runs as a non-root user (`UID=10001`, see `deploy/docker/Dockerfile.backend`)
- **Frontend container**: Uses the `nginxinc/nginx-unprivileged` image (default `UID=101`)
- Docker Compose is configured with `security_opt: no-new-privileges:true`

### Stopping Services

```bash
cd <project-root>
COMPOSE_PROJECT_NAME=<compose project printed in console> docker compose -f docker-compose.yml down --remove-orphans
```

---

## 6. Manual Startup

If not using Docker, you can start the backend and frontend manually.

### Step 1: Generate `.env` Configuration

```bash
# macOS (generate Profile C configuration)
cd <project-root>
bash scripts/apply_profile.sh macos c

# Linux (generate Profile C configuration)
bash scripts/apply_profile.sh linux c

# Windows PowerShell
.\scripts\apply_profile.ps1 -Platform windows -Profile c
```

> Script execution logic: copies `.env.example` to `.env`, then appends the override parameters from `deploy/profiles/<platform>/profile-<x>.env`.
>
> `apply_profile.sh/.ps1` currently deduplicates repeated env keys after generation to avoid inconsistent behavior across different parsers when the same key appears multiple times.
>
> But keep the current boundary explicit:
> - `Profile C`: the script only insists that **embedding + reranker** are no longer placeholders; optional LLM fields can still be filled later
> - `Profile D`: the script still treats **embedding + reranker + the LLM assist surface** as required
> - so `apply_profile.*` on `C/D` should be read as “generate the right skeleton and reject obvious placeholders,” not “one command fully completes an advanced profile”

### Step 2: Start the Backend

```bash
cd <project-root>/backend
python3 -m venv .venv
source .venv/bin/activate          # Windows PowerShell: .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn main:app --host 127.0.0.1 --port 18000
```

### Step 3: Start the Frontend

```bash
cd <project-root>/frontend
npm install
MEMORY_PALACE_API_PROXY_TARGET=http://127.0.0.1:18000 npm run dev -- --host 127.0.0.1 --port 3000
```

---

## 7. Local Inference Service Reference

If using Profile C, you need to run embedding/reranker models locally. Here are commonly used local inference services:

| Service | Official Docs | Hardware Recommendations |
|---|---|---|
| Ollama | [docs.ollama.com](https://docs.ollama.com/gpu) | CPU capable; GPU recommended with VRAM matched to model size |
| LM Studio | [lmstudio.ai](https://lmstudio.ai/docs/app/system-requirements) | 16GB+ RAM recommended |
| vLLM | [docs.vllm.ai](https://docs.vllm.ai/en/stable/getting_started/installation/gpu.html) | Linux-first; NVIDIA compute capability 7.0+ |
| SGLang | [docs.sglang.ai](https://docs.sglang.ai/index.html) | Supports NVIDIA / AMD / CPU / TPU |

**OpenAI-compatible API documentation**:

- Ollama: [OpenAI Compatibility](https://docs.ollama.com/api/openai-compatibility)
- LM Studio: [OpenAI Endpoints](https://lmstudio.ai/docs/app/api/endpoints/openai)

> **Important**: Memory Palace's embedding/reranker are both called via OpenAI-compatible APIs. If you have enabled the reranker (enabled by default in C/D), the backend service requires a working rerank endpoint (defaults to `/rerank`) in addition to `/v1/embeddings`.
>
> If the reranker is a locally self-hosted service like `llama.cpp` that starts on demand, manually warm it up with a `/rerank` call before the first strict-profile `setup` / `verify` / `smoke`.

---

## 8. Vitality Parameters

The Vitality system manages memory lifecycle automatically: **access reinforcement -> natural decay -> cleanup candidate -> manual confirmation**.

| Parameter | Default | Description |
|---|:---:|---|
| `VITALITY_MAX_SCORE` | `3.0` | Maximum vitality score |
| `VITALITY_REINFORCE_DELTA` | `0.08` | Score increase per search hit |
| `VITALITY_DECAY_HALF_LIFE_DAYS` | `30` | Decay half-life (days); vitality halves after 30 days |
| `VITALITY_DECAY_MIN_SCORE` | `0.05` | Decay floor; score will not drop below this value |
| `VITALITY_CLEANUP_THRESHOLD` | `0.35` | Memories with vitality below this become cleanup candidates |
| `VITALITY_CLEANUP_INACTIVE_DAYS` | `14` | Inactivity threshold in days, combined with vitality score to determine cleanup candidates |
| `RUNTIME_VITALITY_DECAY_CHECK_INTERVAL_SECONDS` | `600` | Decay check interval (seconds), default 10 minutes |
| `RUNTIME_CLEANUP_REVIEW_TTL_SECONDS` | `900` | Cleanup confirmation window (seconds), default 15 minutes |
| `RUNTIME_CLEANUP_REVIEW_MAX_PENDING` | `64` | Maximum pending cleanup confirmations |

**Tuning Tips**:

1. Keep defaults first, observe for 1--2 weeks before adjusting
2. If too many cleanup candidates appear -> increase `VITALITY_CLEANUP_THRESHOLD` or `VITALITY_CLEANUP_INACTIVE_DAYS`
3. If the confirmation window is too short -> increase `RUNTIME_CLEANUP_REVIEW_TTL_SECONDS`

---

## 9. API Authentication

The following interfaces are protected by `MCP_API_KEY` (**fail-closed**: returns `401` by default when no key is configured):

- `GET/POST/DELETE /maintenance/*`
- `GET/POST/PUT/DELETE /browse/*` and `GET/POST/DELETE /review/*`
- SSE interfaces (`/sse` and `/messages`; standalone `run_sse.py` and embedded backend share the same auth middleware)

### Request Header Format (choose one)

```
X-MCP-API-Key: <your MCP_API_KEY>
Authorization: Bearer <your MCP_API_KEY>
```

### Local Debug Bypass

Setting `MCP_API_KEY_ALLOW_INSECURE_LOCAL=true` skips authentication during local debugging:

- Only applies to **direct loopback requests without any `Forwarded` / `X-Forwarded-*` / `X-Real-IP` headers**
- Non-loopback requests still return `401` (with `reason=insecure_local_override_requires_loopback`)

> **MCP stdio mode** does not go through the HTTP/SSE authentication middleware and is not affected by this restriction.

> **Bootstrap exception**: `/bootstrap/status`, `/bootstrap/provider-probe`, `/bootstrap/apply`, and `/bootstrap/restart` are not under the same rule as this table. The current actual behavior is:
>
> - `/bootstrap/*` always only allows direct loopback
> - Non-loopback requests or requests with forwarding headers return `403` / `reason=loopback_required`
> - If the backend already has `MCP_API_KEY`, `/bootstrap/provider-probe`, `/bootstrap/apply`, and `/bootstrap/restart` must also carry that key
> - The browser-side `Set API key` maintenance key injection at the top now forwards the same key to `/bootstrap/provider-probe`, `/bootstrap/apply`, and `/bootstrap/restart`; the server still enforces the bootstrap loopback / key gates separately

### Frontend Access to Protected Interfaces

When **manually starting frontend and backend locally**, inject the API Key at runtime (not recommended to hardcode in build variables):

```html
<script>
  window.__MEMORY_PALACE_RUNTIME__ = {
    maintenanceApiKey: "<MCP_API_KEY>",
    maintenanceApiKeyMode: "header"   // or "bearer"
  };
</script>
```

> Also compatible with the legacy field name: `window.__MCP_RUNTIME_CONFIG__`

When using **Docker one-click deployment**, you do not need to write the key into the browser page:

- The frontend container automatically includes the same `MCP_API_KEY` at the proxy layer for `/api/*`, `/sse`, and `/messages`
- This key is stored by default in the Docker env file used for this run
- The browser only sees the proxied result and does not directly receive the actual key

### SSE Startup Example

```bash
HOST=127.0.0.1 PORT=8010 python run_sse.py
```

> The `HOST=127.0.0.1` here is a local loopback debugging example. To allow access from other machines, change it to `0.0.0.0` (or your actual listening address), and ensure `MCP_API_KEY`, network isolation, reverse proxy, and TLS protections are in place.
>
> This standalone `run_sse.py` path is now primarily retained for local standalone debugging and backward compatibility. In the default Docker / frontend proxy setup, `/sse` and `/messages` are already mounted directly within the backend process, no longer requiring a separate `sse` container.

For Docker one-click deployment, use directly:

```bash
http://localhost:3000/sse
```

---

## 10. Tuning and Troubleshooting

### Common Issues

| Issue | Cause and Solution |
|---|---|
| Poor search results | Confirm `SEARCH_DEFAULT_MODE` is `hybrid`; for C/D profiles, check if `RETRIEVAL_RERANKER_WEIGHT` is reasonable |
| Model service unavailable | The system degrades automatically; check the `degrade_reasons` field in the response to identify the specific cause |
| C/D shows `embedding_request_failed` / `embedding_fallback_hash` | Typically means the external embedding/reranker pipeline is unreachable (e.g., the local router has no model deployed), not a backend main-process crash; follow "C/D degradation signal quick troubleshooting" below |
| Docker port conflict | The one-click script automatically finds available ports; you can also manually specify ports (bash: `--frontend-port` / `--backend-port`, PowerShell: `-FrontendPort` / `-BackendPort`) |
| SSE startup fails with `address already in use` | Release the occupied port, or switch with `PORT=<available-port>` |
| Database lost after upgrade | The backend automatically recovers from legacy filenames (`agent_memory.db` / `nocturne_memory.db` / `nocturne.db`) on startup |

### C/D Degradation Signal Quick Troubleshooting (Local Integration)

```bash
# First check if the service is actually running
curl -fsS http://127.0.0.1:18000/health
```

1. If logs or response results still show `embedding_request_failed` / `embedding_fallback_hash`, first check whether the embedding / reranker service itself is reachable and whether the API key is valid.
2. Directly testing the actual call endpoints is more reliable than just checking configuration files:

```bash
curl -fsS -X POST <RETRIEVAL_EMBEDDING_API_BASE>/embeddings \
  -H "Content-Type: application/json" \
  -d '{"model":"<RETRIEVAL_EMBEDDING_MODEL>","input":"ping"}'
curl -fsS -X POST <RETRIEVAL_RERANKER_API_BASE>/rerank \
  -H "Content-Type: application/json" \
  -d '{"model":"<RETRIEVAL_RERANKER_MODEL>","query":"ping","documents":["pong"]}'
```

If the embedding service is reachable but the dimensions do not match, the typical symptoms are:

- Noticeably degraded search quality
- Additional warnings or degradation signals in `verify / doctor / smoke`
- Problems only appeared after switching providers

3. If you are just troubleshooting on the current machine, you can temporarily switch to `RETRIEVAL_EMBEDDING_BACKEND=api` and directly configure embedding / reranker / llm individually; restore to the target environment's `router` configuration and re-verify before going live.

### PowerShell / Windows Validation Tips

- Both `scripts/apply_profile.sh` and `scripts/apply_profile.ps1` perform deduplication on repeated env keys.
- For Windows native OpenClaw setup / package / full-stack conclusions, it is recommended to re-run startup, verify, and smoke on the target Windows machine using the same template, and use the latest local report as the baseline.
- `pwsh` running inside Linux/macOS Docker can at most help verify PowerShell invocation syntax and cannot substitute for native Windows OpenClaw real-machine validation.
- Main documentation retains only publicly executable steps; target-environment-specific validation should be documented separately.

### Tuning Tips

1. **`RETRIEVAL_RERANKER_WEIGHT`**: Setting too high causes excessive dependence on the re-ranking model; tune in `0.05` increments
2. **Docker data persistence**: Uses `memory_palace_data` volume by default (see `docker-compose.yml`)
3. **Legacy compatibility**: The one-click script automatically recognizes legacy `NOCTURNE_*` environment variables and historical data volumes
4. **Migration lock**: `DB_MIGRATION_LOCK_FILE` (default `<db_file>.migrate.lock`) and `DB_MIGRATION_LOCK_TIMEOUT_SEC` (default `10` seconds) prevent concurrent migration conflicts across multiple processes

---

## 11. Utility Script Reference

| Script | Description |
|---|---|
| `scripts/apply_profile.sh` | Generate `.env` from template (macOS / Linux) |
| `scripts/apply_profile.ps1` | Generate `.env` from template (Windows PowerShell) |
| `scripts/docker_one_click.sh` | Docker one-click deployment (macOS / Linux) |
| `scripts/docker_one_click.ps1` | Docker one-click deployment (Windows PowerShell) |

### Configuration Template File Structure

```
deploy/profiles/
├── linux/
│   ├── profile-a.env
│   ├── profile-b.env
│   ├── profile-c.env
│   └── profile-d.env
├── macos/
│   ├── profile-a.env
│   ├── profile-b.env
│   ├── profile-c.env
│   └── profile-d.env
├── windows/
│   ├── profile-a.env
│   ├── profile-b.env
│   ├── profile-c.env
│   └── profile-d.env
└── docker/
    ├── profile-a.env
    ├── profile-b.env
    ├── profile-c.env
    └── profile-d.env
```

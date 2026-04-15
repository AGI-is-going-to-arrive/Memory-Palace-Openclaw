---
name: memory-palace-openclaw-onboarding
description: 通过对话引导用户把 Memory Palace 接进 OpenClaw，不依赖 dashboard；for chat-first onboarding, Profile C/D provider collection, probing, fallback explanation, and setup/apply on VPS or terminal-only environments.
metadata: { "openclaw": { "emoji": "🛠️", "always": true, "requires": { "anyBins": ["memory-palace-openclaw", "py", "python3", "python"] } } }
---

# Memory Palace OpenClaw Onboarding

Use this skill when the user wants to install, bootstrap, reconfigure, or troubleshoot Memory Palace onboarding through normal OpenClaw conversation.

This skill is only for onboarding.

- It is not the day-to-day durable memory skill.
- It should not replace `memory-palace-openclaw`.
- It should assume the user may be on a VPS with no dashboard access.

## Default posture

- Prefer tool-driven conversational onboarding first.
- Use the onboarding tools before falling back to raw CLI commands.
- Do not require the dashboard unless the user explicitly asks for it.
- Prefer `stdio` transport unless the user already knows they need `sse`.
- Prefer `Profile C/D` when the user can provide real providers.
- Fall back to `Profile B` only when providers are missing, failing, or the user explicitly wants the safest bootstrap path.

Primary onboarding tools:

- `memory_onboarding_status`
- `memory_onboarding_probe`
- `memory_onboarding_apply`

Locale rule:

- if the user is speaking Chinese, call these tools with `locale="zh-CN"`
- if the user is speaking English, call these tools with `locale="en"`

## Supported provider input forms

When collecting provider values, accept these forms:

### Embedding

- Base URL:
  - `https://provider.example/v1`
  - `https://provider.example/v1/embeddings`
- API key:
  - any normal bearer-style secret string
- Model:
  - exact provider model id, for example `Qwen3-Embedding-8B`

### Reranker

- Base URL:
  - `https://provider.example/v1`
  - `https://provider.example/v1/rerank`
- API key:
  - any normal bearer-style secret string
- Model:
  - exact provider model id, for example `Qwen/Qwen3-Reranker-8B`

### LLM

- Base URL:
  - `https://provider.example/v1`
  - `https://provider.example/v1/chat/completions`
  - `https://provider.example/v1/responses`
- API key:
  - any normal bearer-style secret string
- Model:
  - exact provider model id, for example `gpt-5.4`

Interpretation boundary:

- OpenAI-compatible chat-style configuration is supported.
- If the user pastes a full `/chat/completions` or `/responses` URL, treat it as valid input and normalize it to the base automatically.
- The current runtime accepts `/responses` as an input alias, but the main write-guard / compact-gist LLM flows still call `/chat/completions`.

## What to ask, in order

Ask only for what is still missing.

1. Whether they want the safest bootstrap path or the strongest capability path.
2. Whether they need `basic` or `full`.
3. Whether they can provide real embedding, reranker, and LLM providers right now.
4. The missing provider fields section by section.

Keep the wording simple:

- “If you can provide real providers now, I recommend Profile C or D.”
- “If not, we can start with Profile B first and upgrade later.”

## Preferred execution flow

1. Start with `memory_onboarding_status`.
2. If the user wants the strongest path and has provider values, run `memory_onboarding_probe`.
3. Read the probe result before applying setup.
4. If the embedding probe reports `recommendedDim`, `detectedMaxDim`, or `detectedDim`, explicitly tell the user:
   - what dimension was detected
   - that this is the recommended `RETRIEVAL_EMBEDDING_DIM`
5. Apply setup only after the probe result is understood:
   - `memory_onboarding_apply`
6. If onboarding tools are unavailable, fall back to raw CLI:
   - `python3 scripts/openclaw_memory_palace.py onboarding --json`
   - `python3 scripts/openclaw_memory_palace.py provider-probe --profile c|d ... --json`
   - `python3 scripts/openclaw_memory_palace.py setup ... --json`
   - On Windows PowerShell, replace `python3` with `py -3`
7. After setup, always verify:
   - `openclaw memory-palace verify --json`
   - `openclaw memory-palace doctor --json`
   - `openclaw memory-palace smoke --json`

## How to interpret probe/setup results

### If provider probe passes

- Tell the user C/D is ready to apply.
- If embedding dimension was detected, recommend using that value.

### If provider probe reports missing fields

- Tell the user exactly which fields are still missing.
- Ask only for those fields.
- Do not claim that C/D is ready.

### If provider probe reports failed providers

- Tell the user which provider failed and why.
- Ask them to fix the address, key, model, or reachability.
- Re-run `provider-probe` before setup.

### If setup returns `fallbackApplied=true`

- State clearly that the requested C/D profile did not stay active.
- State that the environment is now on `Profile B`.
- Explain that this is a temporary safe fallback, not the target capability path.
- Tell the user to re-probe and then re-run setup after fixing providers.

## Profile guidance

### Preferred path

Strongly prefer `Profile C/D` when the user can provide real providers.

Why:

- real embedding
- real reranker
- better retrieval quality
- stronger end-state capability

### Safe bootstrap path

`Profile B` is still the safe bootstrap baseline.

Its hard retrieval boundary is:

- retrieval stays on local hash embeddings
- embedding dimension stays on the local baseline
- reranker stays off

But `Profile B` can still keep optional LLM-assisted features when valid LLM config is present.

That means:

- write guard can still be enabled
- compact gist can still be enabled
- retrieval quality still does not become true provider-backed C/D retrieval

If no LLM config is provided, those LLM-assisted features stay off and the system continues on the non-LLM path.

## Interaction rules

- Do not force the user into the dashboard.
- Do not ask for all fields at once if the probe already narrowed the missing set.
- Do not invent provider values.
- Do not echo secrets back unless the user explicitly asks for the literal command line.
- Prefer the onboarding tools when they are available.
- If tool execution is unavailable, provide the exact commands the user should run.
- If shell execution is available but onboarding tools are also available, still prefer the tools so the conversation stays simple for VPS users.

## Anti-patterns

- Do not treat the runtime memory skill as the onboarding skill.
- Do not say C/D is ready before running `provider-probe` or equivalent validation.
- Do not hide fallback to B.
- Do not describe `Profile B` as equivalent to C/D.
- Do not assume the user can open `/setup` or any graphical page.

## Common Errors

### `session file locked`

If the user runs a local CLI agent turn and hits:

```text
session file locked
```

Explain it directly:

- another OpenClaw process is already writing the same session file
- the usual cause is an already-open WebUI or another CLI/gateway process using that same agent/session

Preferred recovery:

1. use a fresh session or a temporary agent for the test turn
2. or close the process currently holding that session lock

### `HTTP 401 ... authentication token has been invalidated`

If the conversational turn fails with model-auth 401, do not describe it as the onboarding skill failing.

Explain:

- the onboarding skill and onboarding tools were already loaded
- the failure is in the host's current chat-model provider authentication

Preferred recovery:

1. refresh or re-login the current model provider token
2. or switch the agent to a currently working model, then retry the onboarding conversation

### provider probe failure

If a provider probe returns `fail`, always tell the user:

1. which provider failed
2. whether the likely cause is base URL, API key, model name, or reachability
3. that default apply will only fall back to `Profile B` temporarily
4. that this does not mean `Profile C/D` is actually ready

## Trigger examples

- “帮我把 memory-palace 接进 OpenClaw，不开 dashboard。”
- “我在 VPS 上，只能对话配置，你来引导我装。”
- “帮我配置 Profile C。”
- “provider 报错了，先看哪里没填好。”
- “先帮我探测 embedding 维度，再决定怎么填 env。”

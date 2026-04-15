---
name: memory-palace-openclaw
description: Use the Memory Palace OpenClaw plugin when the task is about durable memory recall, explicit memory verification, visual memory storage, plugin health/index maintenance, or chat-guided onboarding/bootstrap/provider configuration inside OpenClaw.
metadata: { "openclaw": { "emoji": "🧠" } }
---

# Memory Palace OpenClaw

Use this skill inside OpenClaw when the active memory plugin is `memory-palace`.

## Default behavior

On a hook-capable OpenClaw host, the default path is:

- the active memory plugin is already `memory-palace`
- durable memory recall is attempted automatically before the agent starts
- a subset of user messages may be auto-captured after a successful turn
- visual context may be auto-harvested during the current turn

Current host boundary:

- automatic recall / capture / visual-harvest require typed lifecycle hooks from the host
- the current supported floor is `OpenClaw >= 2026.3.2`
- if the host lacks those hooks, treat `openclaw memory-palace ...` as the primary path and do not assume the default auto path is active

So the normal starting point is:

- trust the default recall / capture path first
- only intervene explicitly when the user asks for it, or when the default path is clearly not enough
- do not upgrade ordinary day-to-day requests into explicit tool calls unless there is a clear reason

Important boundary:

- text durable memory may be auto-recalled and auto-captured
- visual context may be auto-harvested
- visual memory is **not** auto-stored as a long-term record unless you explicitly use `memory_store_visual`
- the stable user-facing entry is the memory slot plus `openclaw memory-palace ...`, not raw MCP tools

Onboarding boundary:

- if the user wants to install, bootstrap, reconfigure, or verify provider readiness through chat, prefer the onboarding tools first
- do **not** send the user to the dashboard by default when the request is explicitly chat-first or VPS/terminal-only
- when Profile C/D is requested, provider probe comes before apply
- if provider probe fails or required fields are missing, explain the exact gap and fallback behavior in plain Chinese before suggesting apply

## When to intervene explicitly

Use explicit tools only in these cases:

1. The user clearly asks to check whether something is already in memory.
2. The user wants the exact content of a remembered item.
3. The user explicitly asks you to remember a stable fact, preference, or workflow for future turns.
4. The user wants to persist an image-derived summary as a long-term visual memory.
5. The user asks to check backend health, diagnose degradation, or rebuild the index.
6. The user asks to install/bootstrap/reconfigure Memory Palace through chat without opening the dashboard.

## Preferred intervention order

1. Prefer the default OpenClaw memory path first.
2. If the user explicitly says “remember this” / “记住这个” and the content is a stable long-term fact, use `memory_learn`.
   - If `memory_learn` succeeds, acknowledge it with a minimal confirmation like `记住了。` / `Stored.` and move on.
   - If the user explicitly asked for an exact confirmation phrase after storing, use that phrase verbatim instead of paraphrasing it.
   - If `memory_learn` returns a blocked write, explain the brief human-readable reason.
   - Only if the user confirms they still want a separate durable memory saved should you rerun `memory_learn` with `force=true`.
3. If you need explicit recall, use `memory_search`.
4. If a hit matters, follow with `memory_get` using the returned `path`.
5. If the user wants a long-term visual record, use `memory_store_visual`.
6. If the user wants onboarding/bootstrap through chat, use:
   - `memory_onboarding_status`
   - `memory_onboarding_probe`
   - `memory_onboarding_apply`
7. If the problem is operational, ask the operator to run:
   - `openclaw memory-palace status`
   - `openclaw memory-palace verify`
   - `openclaw memory-palace doctor`
   - `openclaw memory-palace smoke`
   - `openclaw memory-palace index`

Use these explicit intervention tools:

- `memory_search`
- `memory_learn`
- `memory_get`
- `memory_store_visual`
- `memory_onboarding_status`
- `memory_onboarding_probe`
- `memory_onboarding_apply`

## Anti-patterns

- Do not treat every request as a manual memory workflow.
- Do not treat this OpenClaw plugin skill as a mirror of the canonical MCP skill.
- Do not start with `memory_search` when the default recall path is already enough.
- Do not rely only on implicit auto-capture when the user explicitly asks you to remember a stable fact for future turns; use `memory_learn`.
- Do not use `force=true` on `memory_learn` unless the user has already confirmed they still want the memory saved after a blocked write.
- Do not invent memory contents.
- Treat backend/tool errors as real failures, not as empty recall.
- Do not imply that visual auto-harvest means long-term visual auto-storage.
- Do not present raw MCP tools as the default entry for ordinary OpenClaw users.
- Prefer text summaries and references for visual memory, never raw binary image storage.
- If you explicitly recall something important, prefer `memory_search` first and `memory_get` second.
- Do not tell VPS/terminal-only users that dashboard setup is required when onboarding tools can do the same job.
- Do not claim Profile C/D is ready before provider probe actually passes.
- Do not hide fallback to Profile B; name the failing provider and tell the user what to fix.
- Do not tell users that `/responses` is the primary LLM runtime path; current main path is still `/chat/completions`.

## Trigger examples

Default recall path:

- “回忆一下之前记过的偏好”
- “继续我们上次的决定”

Explicit verification:

- “查记忆里有没有这件事”
- “把刚才提到的那条记忆正文读出来”

Explicit durable remember:

- “请记住这个长期偏好：以后默认简洁回答”
- “Remember this workflow for future turns: code first, tests immediately after, docs last”
- “请记住这个长期偏好，存好后只回复‘已记录。’”
- “Remember this durable fact and reply only ‘stored profile c’ after saving”

Exact confirmation phrase:

- If the user explicitly requires one exact confirmation string after `memory_learn`, pass it through `confirmationPhrase` (or `confirmation_phrase`) and reuse that phrase verbatim.
- Example:
  `memory_learn({ content: \"请记住这个长期偏好：以后默认简洁回答\", confirmationPhrase: \"已记录。\" })`
- Example:
  `memory_learn({ content: \"Remember this workflow for future turns\", confirmationPhrase: \"stored profile c\" })`

Long-term visual storage:

- “把这张白板照片的 OCR 摘要存成 visual memory”
- “把这张会议截图整理成长期 visual record”

Maintenance and diagnostics:

- “检查 memory backend 状态并重建索引”
- “跑一遍 verify / doctor / smoke 看插件是不是正常”

Chat-first onboarding:

- “不要开 dashboard，直接通过对话把 Memory Palace 接进 OpenClaw”
- “先帮我判断该走 Profile B 还是 C/D，再一步一步收集 provider 配置”
- “我在 VPS 上，只能通过聊天引导完成 setup”

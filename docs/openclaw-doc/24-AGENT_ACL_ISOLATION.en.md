> [中文版](24-AGENT_ACL_ISOLATION.md)

# 24 · Multi-Agent Memory Isolation (ACL) Quick Guide

This page covers one thing:

> **How to show users that `alpha` stores a memory first, `beta` still cannot read it, and what to change if you want that isolation turned on.**

In plain language:

> **Here, ACL means experimental multi-agent memory isolation. Once enabled, the current tested plugin path can narrow each agent to its own long-term memory area by default, but this is not yet a fully hardened security boundary.**

If you have not set up the plugin yet, start here:

1. [01-INSTALL_AND_RUN.en.md](01-INSTALL_AND_RUN.en.md)
2. [15-END_USER_INSTALL_AND_USAGE.en.md](15-END_USER_INSTALL_AND_USAGE.en.md)
3. [18-CONVERSATIONAL_ONBOARDING.en.md](18-CONVERSATIONAL_ONBOARDING.en.md)

If you have already cloned the repo and still want the single-page HTML summary, open it locally only when needed:

- [23-PROFILE_CAPABILITY_BOUNDARIES.en.html](23-PROFILE_CAPABILITY_BOUNDARIES.en.html)

---

## 1. If you are here to turn ACL on, do these 3 steps first

1. Run:

```bash
openclaw config file
```

2. In the active `openclaw.json`, find:

- `plugins.entries.memory-palace.config.acl`

3. Set `enabled` to `true`, then fill in your real agent IDs for:

- `allowedUriPrefixes`
- `writeRoots`

After that, restart the gateway and verify with the same `alpha -> beta -> UNKNOWN` flow shown below.

If you want OpenClaw to help, jump straight to **6.2**.

---

## 2. Current Public Conclusion

The current `memory-palace` multi-agent ACL:

- **Should currently be treated as an experimental feature**
- **Is not enabled by default**
- **Can still be directly observed in the OpenClaw WebUI once enabled**
- the latest profile-matrix record still reproduces the current `A / B / C / D + ACL` behavior in isolated scenarios
- the current public ACL screenshots and videos still match the user-facing story on this page

More precisely:

- Without ACL enabled, multi-agent long-term memory does not necessarily enforce strict boundaries
- With ACL enabled, the current tested plugin path changes which long-term memory each agent can read and where it can write
- This change still manifests in the recall blocks and response results users see in the WebUI
- The current design is still stronger on the plugin path than on backend / direct API hardening, so do not read this page as a strict security guarantee yet
- The latest isolated ACL record still matches the same story:
  - `alpha` can recall what `alpha` wrote
  - `beta` and `main` continue to answer `UNKNOWN`

If you only want the short version:

- **ACL off**: multiple agents are not guaranteed to stay strictly isolated
- **ACL on**: the current experimental path can narrow each agent to its own long-term memory by default, but it is not yet a fully hardened security boundary

---

## 3. Most Intuitive Verification Method in the WebUI

The most reliable user-perspective verification sequence is now:

1. On the `Agents` page, confirm you are not on a single agent but have `main / alpha / beta`
2. Switch to `alpha` chat first and explicitly write a workflow memory
3. Then switch to `beta` chat
4. Observe that the recall block is restricted to `core://agents/beta/...` only
5. Then ask:

```text
What is alpha's default workflow? Reply UNKNOWN if you cannot know.
```

If the current experimental ACL behavior is effective:

- `beta` should ultimately respond with `UNKNOWN`
- Here, `UNKNOWN` does not mean "the system did not remember" -- it means that in this tested flow, `beta` did not read `alpha`'s long-term memory
- In plain language: **alpha remembered it, but in this recorded flow beta still could not read it**

---

## 4. Watch the Video

The ACL scenario videos now use **burned-subtitle MP4** format:

- Chinese:
  - [openclaw-control-ui-acl-scenario.zh.mp4](./assets/real-openclaw-run/openclaw-control-ui-acl-scenario.zh.mp4)
- English:
  - [openclaw-control-ui-acl-scenario.en.mp4](./assets/real-openclaw-run/openclaw-control-ui-acl-scenario.en.mp4)

The narrative sequence of this video is now fixed as:

- `Agents` page shows `main / alpha / beta`
- Enter `alpha` scope and write a memory
- Switch to `beta` scope
- Recall block narrows to `core://agents/beta/...`
- Ask `What is alpha's default workflow?`
- Response is `UNKNOWN`

The current stance is explicit:

> **Subtitles are burned into the MP4; HTML overlay subtitles are no longer used.**

---

## 5. Three Key Evidence Screenshots

### 5.1 First Confirm Three Agents on the `Agents` Page

![ACL Agents Page](./assets/real-openclaw-run/24-acl-agents-page.en.png)

What to look for here is not the styling, but the actual agent names:

- The current WebUI indeed has `main / alpha / beta`
- This is the scope set used by the current experimental ACL story

### 5.2 Then Explicitly Write a Memory in the `alpha` Conversation

![ACL Alpha Memory Confirmed](./assets/real-openclaw-run/24-acl-alpha-memory-confirmed.en.png)

This screenshot requires checking only 2 things:

1. The user did ask the system to remember a workflow in the `alpha` conversation
2. The WebUI did provide a confirmation reply that the memory was stored for alpha

This step is the prerequisite for the ACL proof that follows: you must first prove the memory was actually written.

### 5.3 Finally Confirm It Is Unreadable in the `beta` Conversation

![ACL Beta Chat Isolated](./assets/real-openclaw-run/24-acl-beta-chat-isolated.en.png)

This screenshot requires checking 3 things simultaneously:

1. The recall block is restricted to `core://agents/beta/...`
2. The user asked about `alpha`'s workflow
3. The final response is `UNKNOWN`

So the real conclusion here is:

> **Memory Palace did remember that memory, and in this recorded experimental flow `beta` still did not read `alpha`'s long-term memory.**

That is also why the ACL video conclusion for this round can stay on the same public narrative:

- the story itself does not change: `Agents -> alpha stores -> beta gets UNKNOWN` is still the most accurate user-facing explanation on this page

---

## 6. Minimum Viable Configuration

If you want to enable ACL by hand, first find the active host config file:

```bash
openclaw config file
```

Then place the following into:

- `plugins.entries.memory-palace.config.acl`

One thing first:

- this is only an **example template**
- `main / alpha / beta` are only placeholder examples
- the `agents` block must match the real agent IDs on that host

```json
{
  "enabled": true,
  "sharedUriPrefixes": [],
  "sharedWriteUriPrefixes": [],
  "defaultPrivateRootTemplate": "core://agents/{agentId}",
  "allowIncludeAncestors": false,
  "defaultDisclosure": "Agent-scoped durable memory.",
  "agents": {
    "main": {
      "allowedUriPrefixes": ["core://agents/main"],
      "writeRoots": ["core://agents/main"],
      "allowIncludeAncestors": false
    },
    "alpha": {
      "allowedUriPrefixes": ["core://agents/alpha"],
      "writeRoots": ["core://agents/alpha"],
      "allowIncludeAncestors": false
    },
    "beta": {
      "allowedUriPrefixes": ["core://agents/beta"],
      "writeRoots": ["core://agents/beta"],
      "allowIncludeAncestors": false
    }
  }
}
```

You only need to change two things:

1. Set `enabled` to `true`
2. Add each agent's real agent ID with:
   - `allowedUriPrefixes`
   - `writeRoots`

### 6.1 If you want to edit it yourself

1. Run `openclaw config file` and make sure you are editing the active `openclaw.json`
2. Find `plugins.entries.memory-palace.config.acl`
3. Set `enabled` to `true`
4. For each real agent ID you actually use, fill in:
   - `allowedUriPrefixes`
   - `writeRoots`
5. Save, restart the gateway, then verify with the same `alpha -> beta -> UNKNOWN` flow shown above

If your real agents are not named `main / alpha / beta`, replace those IDs with the real ones on your host.

### 6.2 If you want OpenClaw to help

One boundary first:

- **there is no dedicated ACL onboarding tool right now**
- so do not wait for an “ACL wizard”
- just let OpenClaw read the current config, generate the ACL JSON, and apply only the smallest patch

You can paste this directly to OpenClaw:

```text
Please read the current OpenClaw config at plugins.entries.memory-palace.config.acl. Do not overwrite the whole memory-palace config. Apply only the smallest patch required by document 24: 1) set enabled=true; 2) for each real agent id I use, write allowedUriPrefixes and writeRoots. First show me the ACL JSON you plan to write and the dry-run command, then wait for my confirmation before executing it.
```

If OpenClaw already generated the ACL JSON, you can run:

```bash
openclaw config set plugins.entries.memory-palace.config.acl '<ACL_JSON>' --strict-json --dry-run
openclaw config set plugins.entries.memory-palace.config.acl '<ACL_JSON>' --strict-json
```

This `config set` command is part of the current public configuration path. The safer order is always:

1. let OpenClaw generate the ACL JSON
2. run `--dry-run` first
3. only remove `--dry-run` after you confirm the patch looks right
4. restart the gateway
5. verify again with the `alpha -> beta -> UNKNOWN` flow

---

## 7. What ACL Primarily Isolates

This minimum configuration primarily targets the following agent-private long-term memory paths:

- `profileMemory`
  - `core://agents/<agentId>/profile/...`
- `autoCapture`
  - `core://agents/<agentId>/captured/...`
- `hostBridge`
  - `core://agents/<agentId>/host-bridge/...`
- `assistant-derived / llm-extracted`
  - `core://agents/<agentId>/...`

In one sentence:

> **As long as the long-term memory is under `core://agents/<agentId>/...`, the current ACL design mainly tries to keep it within the corresponding agent path.**

Boundaries to clarify upfront:

- `core://visual/...` typically remains a global visual namespace
- If you want visual memory to be strictly separated by agent as well:
  - You need to plan additional visual roots

---

## 7. Most Common Configuration Mistakes

### 8.1 Agent ID Typo

The real agent in OpenClaw is called:

```text
reviewer
```

But the ACL configuration says:

```text
review
```

This policy will not match.

### 8.2 Configuring Read Only, Not Write

If you only specify `allowedUriPrefixes` without `writeRoots`:

- Read and write boundaries will be inconsistent

Recommendation:

- Set both to the same agent root

### 8.3 Assuming Visual Memory Is Automatically Isolated

This template primarily addresses:

- **Agent-private durable recall**

Not:

- "All namespaces automatically strictly partitioned"

---

## 8. Current Recommended Public Statement

The recommended standard statement is:

> **Memory Palace's multi-agent memory isolation (ACL) is currently an experimental feature. It is not enabled by default. The checked plugin path can already show `alpha -> stored -> beta -> UNKNOWN`, but the design is still being hardened and should not yet be presented as a strict security boundary.**

Do not state:

- "All agents are strictly isolated by default"
- "ACL is primarily understood through the Dashboard or a separate page"

Because the most intuitive evidence is now in the OpenClaw WebUI videos and chat screenshots.

---

## See 25 for the Full Architecture

If you want to go one layer deeper and understand not just ACL itself, but also:

- how `memory-palace` takes over the current OpenClaw memory slot
- why backend durable memory is versioned instead of overwritten in place
- what `write_guard`, hybrid retrieval, reflection, and host bridge each do in the main path
- how `ACL` and `Profile A / B / C / D` each fit into the full memory system

Go directly to:

- [25-MEMORY_ARCHITECTURE_AND_PROFILES.en.md](25-MEMORY_ARCHITECTURE_AND_PROFILES.en.md)

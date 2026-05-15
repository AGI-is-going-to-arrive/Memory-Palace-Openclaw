> [中文版](00-IMPLEMENTED_CAPABILITIES.md)

# 00 · Implemented Capabilities Checklist

This page now keeps one job only:

> fix the boundary of what is already shipped and should no longer be written
> back into “future plans”.

Positioning first:

- this is a maintainer appendix
- it is not the default user entry
- normal users should start from:
  - `README.md`
  - `docs/openclaw-doc/README.en.md`
  - `docs/openclaw-doc/01-INSTALL_AND_RUN.en.md`
  - `docs/EVALUATION.en.md`

---

## 1. Stable Facts That Already Hold

These should no longer be described as “planned”:

- OpenClaw memory plugin is shipped
- stable user entry is `openclaw memory-palace ...`
- direct `skill + MCP` route remains for other agent clients
- `setup / verify / doctor / smoke / migrate / upgrade` are real command surfaces
- dashboard is a support surface, not the main public homepage
- review / snapshot / rollback are in the main backend path
- visual memory is a real product surface
- experimental multi-agent ACL is inside the current product boundary
- `before_prompt_build` is the primary lifecycle hook
- durable / reflection auto recall now merge the current session into recall scope, so “the current chat context did not come back” is less likely on the default path
- search / browse return active memories by default; historical, future-validity,
  or superseded records require explicit `include_expired=true`
- command:new reflection dedupe has session/TTL/budget protection
- command:new reflection and smart extraction now fail closed when the target session transcript cannot be identified, instead of scanning the latest unrelated transcript
- smart extraction now runs in the background after foreground capture returns, with same agent/session work queued
- workflow-related profile recall, durable recall, and host-bridge prompt blocks are now sanitized before prompt injection; onboarding doc paths, provider diagnostics, and confirmation-code noise are no longer supposed to be written back or injected as stable workflow context
- control-ui / WeChat-style chat surfaces are no longer supposed to echo raw `memory-palace-profile` / `memory-palace-recall` blocks or `openclaw-control-ui` metadata noise back into visible replies
- compact-context recall uses gist text for visible chat; internal metadata such
  as `session_id`, `source_hash`, and `gist_method` should not be echoed to users
- write guard actions now cover `ADD / UPDATE / NOOP / DELETE / IGNORE`;
  `IGNORE` means “not worth storing” and is distinct from duplicate-style `NOOP`
- `Memory` schema now includes `valid_from`, `valid_until`, and `superseded_by`
- `memory_links` now exists for typed directed relations such as `related`,
  `supersedes`, `derived_from`, and `contradicts`
- provenance fields are in the schema; current `create_memory` writes
  `source_operation=create_memory`, while agent/session values still depend on
  upstream host context
- hybrid retrieval supports opt-in `RETRIEVAL_FUSION_METHOD=rrf`; the default
  remains `weighted_sum`
- backend MCP / SQLite layers now use typed exceptions in more places, so public
  handling should prefer structured codes / reasons over raw internal exception text
- onboarding tools avoid passing secrets on the command line
- current installer accepts legacy env aliases and maps them forward

---

## 2. Boundaries That Must Stay Explicit

- `memory-palace` can take over the active OpenClaw memory slot
- that does **not** replace the host's own file-based memory
- automatic recall / capture / visual harvest still depend on hook-capable hosts
- `include_expired=true` is a historical lookup switch, not the default recall mode
- `MemoryLink` should be described as a schema/model capability, not as a claim
  that every recall path expands the full relation graph automatically
- provenance is partial; do not claim agent/session fields are automatically
  filled by the host yet
- this fix changes the plugin's own recall/capture logic, not OpenClaw core; if a host already contains polluted historical workflow records, the cleanup is still a one-time maintenance task
- newer hosts may also keep a compatibility shim such as `memory-core`, but as long as `plugins.slots.memory` still points at `memory-palace`, the active slot has not changed
- visual context harvest is not the same thing as long-term visual storage

---

## 3. What This Page No Longer Repeats

- rerun numbers and benchmark commands
  - see `docs/EVALUATION.en.md`
- installation steps
  - see `01-INSTALL_AND_RUN.en.md`
- screenshots and videos
  - see `15-END_USER_INSTALL_AND_USAGE.en.md`
- deeper architecture notes
  - see `docs/TECHNICAL_OVERVIEW.en.md`

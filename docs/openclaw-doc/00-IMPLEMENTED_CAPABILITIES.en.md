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
- command:new reflection dedupe has session/TTL/budget protection
- command:new reflection and smart extraction now fail closed when the target session transcript cannot be identified, instead of scanning the latest unrelated transcript
- workflow-related profile recall, durable recall, and host-bridge prompt blocks are now sanitized before prompt injection; onboarding doc paths, provider diagnostics, and confirmation-code noise are no longer supposed to be written back or injected as stable workflow context
- onboarding tools avoid passing secrets on the command line
- current installer accepts legacy env aliases and maps them forward

---

## 2. Boundaries That Must Stay Explicit

- `memory-palace` can take over the active OpenClaw memory slot
- that does **not** replace the host's own file-based memory
- automatic recall / capture / visual harvest still depend on hook-capable hosts
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

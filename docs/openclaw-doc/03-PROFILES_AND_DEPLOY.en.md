> [中文版](03-PROFILES_AND_DEPLOY.md)

# 03 - How to Choose Profiles and Deployment

This page answers one question:

> **Which Profile should OpenClaw users start with?**

If you want to see the full environment variables and advanced tuning options, go directly to:

- [../DEPLOYMENT_PROFILES.en.md](../DEPLOYMENT_PROFILES.en.md)

<p align="center">
  <img src="../images/profile_ladder_bilingual_4k.png" width="1100" alt="Bilingual profile capability ladder" />
</p>

---

## 1. The Short Answer

### Profile A

- Most conservative
- Keyword-only search
- Only suitable for minimum-configuration validation

### Profile B

- Default starting profile
- `hash` embedding (dim=64)
- Does not depend on real external embedding / reranker, does not require provider-probe
- Best for getting the plugin chain up and running first

### Profile C

- Provider-backed retrieval profile
- Requires real embedding / reranker
- Defaults to embedding + reranker
- LLM assists are not forced on by default; interactive setup now explicitly asks whether to enable the optional LLM assist suite
- That optional LLM suite currently means:
  - `write_guard`
  - `compact_gist`
  - `intent_llm`
- Suitable for users who already have local or intranet model services ready

### Profile D

- Full advanced target profile
- Requires real embedding / reranker / LLM
- Default target is embedding + reranker + the full LLM assist suite
- Those providers can be local, intranet, or remote; the real boundary is provider health, not deployment topology
- Higher quality, but also higher latency

One-line summary:

- **Get it running first: B**
- **Upgrade retrieval first: C**
- **Full advanced surface when embedding / reranker / LLM are all ready: D**

---

## 2. How to Read the Current Public Baseline

The safest reading is to separate **user-facing proof pages** from the **full rerun record**:

- user-facing pages keep only the conclusions users actually need
- the full commands, counts, host versions, and caveats stay in [../EVALUATION.en.md](../EVALUATION.en.md)
- the latest recorded reruns confirm that:
  - `openclaw plugins info memory-palace` reports the plugin as loaded
  - the same onboarding document can drive the correct next step in CLI / WebUI, in installed / uninstalled states, in both Chinese and English
  - the repo-wrapper `Profile C / D` onboarding `--apply --validate` path passed in the latest recorded rerun
  - the latest profile-matrix record reproduced the current experimental `A / B / C / D + ACL` behavior

The current rerun facts this page can cite are:

- the latest recorded onboarding-doc checks for the direct-to-OpenClaw `CLI / WebUI` branches passed
- read that result as “the in-chat next-step guidance passed,” not as “one chat turn alone completed the final profile apply state”
- `Profile C` now publicly defaults to `embedding + reranker`
- `Profile C` enables `write_guard / compact_gist / intent_llm` only when the LLM suite is explicitly opted in
- `Profile D` now publicly defaults to `write_guard / compact_gist / intent_llm` being on
- the current compatibility layer is already wired and does not change `plugins.slots.memory=memory-palace`
- `Profile C / D` still depend on your own provider health rather than “env is filled, therefore provider-ready”
- the latest recorded shell / onboarding / profile-matrix reruns all match the current code behavior; if the target model endpoint is unhealthy, `doctor / smoke` can still warn

Those records do not mean “the WebUI grows a separate page.” They mean:

- `Profile B` remains the safe bootstrap baseline
- `Profile C` is the long-term tier once your providers are actually healthy
- `Profile D` is the default advanced surface where `write_guard / compact_gist / intent_llm` are all part of the intended stack

For the full reproduction commands and context, use:

- [../EVALUATION.en.md](../EVALUATION.en.md)

### How to Read Time and Platform in the Current Baseline

The safer user-facing wording is:

- the **first full installation** should still not be read as a seconds-only path
- repeated reruns on the same machine are usually much faster than the first install
- if you only hand the onboarding page to OpenClaw so it can return the next step, chat reply speed and “full installation completed” are still two different things

Keep the platform boundary explicit:

- the current recorded full end-to-end rerun was executed on **macOS**
- the repository still provides supported templates and validation paths for **Linux / Windows**
- but if you want to state that a target machine is really ready, rerun in that target environment
- for **Windows native** specifically, rerun the same `setup -> verify -> doctor -> smoke` chain on the target Windows host before treating it as ready
- the detailed Windows appendix is maintainer-only and is not part of the public user-doc set

---

## 3. Most Stable Selection Order for OpenClaw Users

### Step One

Start with:

- `python3 scripts/openclaw_memory_palace.py setup --mode basic --profile b --transport stdio --json`

On Windows PowerShell, run the same repo-wrapper command as `py -3 scripts/openclaw_memory_palace.py setup --mode basic --profile b --transport stdio --json`.

### Step Two

Verify that the following commands complete successfully:

- `openclaw memory-palace verify --json`
- `openclaw memory-palace doctor --json`
- `openclaw memory-palace smoke --json`

If they return `ok=true` but still show `status=warn`, treat that as “passed
with cautions”, inspect the warnings, and only then decide whether the
environment is ready enough for daily use.

### Step Three

Only switch to the following when you have your own model services ready:

- `python3 scripts/openclaw_memory_palace.py setup --mode basic --profile c --transport stdio --json`
- `python3 scripts/openclaw_memory_palace.py setup --mode basic --profile d --transport stdio --json`

On Windows PowerShell, use `py -3` for those repo-wrapper commands too.

In user terms:

- choose **Profile C** when you only want the provider-backed retrieval upgrade first
- choose **Profile D** when embedding / reranker / LLM are all ready and you want the full advanced suite by default

If you are using an embedding route that supports multiple output dimensions:

- `provider-probe` will detect the maximum usable dimension for your current provider/model
- The final `RETRIEVAL_EMBEDDING_DIM` should follow the probe result

Do not mechanically assume the template default is the final value for all provider/model combinations.

That is also why this page keeps repeating one boundary:

- the threshold for `Profile C/D` is not “some API fields exist in env”
- the real threshold is that `provider-probe`, `verify`, `doctor`, and `smoke` all complete successfully in your environment
- if one of those commands still returns `warn`, treat it as “passed with cautions”, inspect the warnings, and do not over-claim readiness
- if you are reading the package/tgz validation path, also split `smoke_mode=seeded_retrieval` from `stdio_capture_* / sse_*`; the former is the seeded retrieval smoke, while the latter is the current real capture/profile conclusion

---

## 4. Boundaries to Remember

- `A/B/C/D` primarily affect retrieval depth and default values for related advanced capabilities
- `Profile C` should not be read as “LLM on by default”
- `Profile D` should no longer be read as “only write_guard uses LLM”; the installer now reuses the shared LLM configuration across `write_guard / compact_gist / intent_llm`
- Automatic recall / auto-capture / visual auto-harvest are not exclusive to any single profile
- This automatic chain still depends on an OpenClaw host that supports hooks
- Public documentation does not include any private model addresses, private keys, or private environment paths

---

## 5. When to Refer to the Full Deployment Documentation

If you are past "choosing a profile" and need to see:

- `.env` parameters
- Docker
- Reranker / embedding / LLM configuration
- Tuning recommendations

Go directly to:

- [../DEPLOYMENT_PROFILES.en.md](../DEPLOYMENT_PROFILES.en.md)

---

## Further Reading: Document 25

If you are no longer just choosing a profile and instead want one page that explains:

- which OpenClaw layer `memory-palace` actually takes over
- how plugin, skills, MCP, and backend divide responsibilities
- how the write path, recall path, and ACL isolation path fit together
- the real product semantics and capability boundaries of `Profile A / B / C / D`

Go directly to:

- [25-MEMORY_ARCHITECTURE_AND_PROFILES.en.md](25-MEMORY_ARCHITECTURE_AND_PROFILES.en.md)

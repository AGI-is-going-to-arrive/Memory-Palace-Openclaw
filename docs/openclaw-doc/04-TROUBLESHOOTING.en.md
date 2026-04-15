> [中文版](04-TROUBLESHOOTING.md)

# 04 · Troubleshooting Guide

This page covers only the most common issues encountered by regular users and self-hosted deployments.

One general principle first:

- First determine whether the issue is with the **current stable entry point**
- Then determine whether it is an environment, provider, Docker, or browser layer issue
- Maintainer scripts (e.g., long-running gates, additional e2e) should not be used as the first diagnostic criteria for regular users

---

## 1. `openclaw memory ...` Still Shows the Host's Built-in Logic

The key point:

- The currently guaranteed default entry point is `openclaw memory-palace ...`
- Not the host's built-in `openclaw memory ...`

So if you see:

```bash
openclaw memory status
```

still showing the host's built-in memory output, this does not mean the plugin failed to install.

Try first:

```bash
openclaw memory-palace status
```

Get this stable entry point working, then check whether the host supports further delegation.

---

## 2. Upgraded to OpenClaw 2026.4.5+ and Even the Host Commands Are Broken

The key point:

- Do not start by debugging the `Memory Palace` main chain
- First confirm whether the host install / bundled extension runtime is already broken

Check first:

```bash
openclaw --version
openclaw doctor --help
openclaw plugins list --json
openclaw status --json
openclaw health --json
```

If you are also using the local gateway / Control UI / browser path in this run, add:

```bash
openclaw gateway status --json
```

If these commands themselves fail, hang, or immediately show `Cannot find package ...` / `Cannot find module ...`, treat that as a host problem first. Do not continue debugging it as a plugin transport / provider issue.

At that point it is more likely that:

- the host upgrade left bundled extension runtime dependencies incomplete
- or the host's own plugin path / install state is broken

If this layer is not healthy yet, do not keep going with `setup / verify / doctor / smoke`. Repair the host until these base commands work again.

If your immediate goal is just to get the host back to a state where you can judge the problem normally, the more conservative sequence right now is:

1. Run:

```bash
openclaw doctor --fix
```

2. Then rerun the host self-check commands above
3. If they are still broken, reinstall the current OpenClaw version using the same install method you originally used, then rerun the host self-checks
4. Only come back to `memory-palace` once this layer is green again

---

## 3. `Unable to connect to Memory Palace MCP over the configured transports`

The more common causes are:

- Plugin dependencies not installed in a source repo load path scenario
- Skipped `setup` but directly asked the wrapper to find the user-space runtime
- `DATABASE_URL` is incorrect
- You thought you were using SSE, but `sse.url` or `MCP_API_KEY` was not configured properly
- You installed a local tgz built from this repository, but on `OpenClaw 2026.4.5+` did not pass `--dangerously-force-unsafe-install`

Check first:

```bash
cd extensions/memory-palace
npm install --no-package-lock
```

If you are following the recommended user path, also check:

```bash
python3 scripts/openclaw_memory_palace.py setup --mode basic --profile b --transport stdio --json
openclaw plugins inspect memory-palace --json
openclaw memory-palace status --json
```

If you installed a local tgz built from this repository, also confirm the install command was:

```bash
openclaw plugins install --dangerously-force-unsafe-install ./openclaw-memory-palace-<version>.tgz
```

Note:

- This `--dangerously-force-unsafe-install` form is only for a **local tgz you just built from this repository**
- Do not reuse it for an untrusted third-party plugin bundle
- If you already passed this flag but still get stuck at `openclaw plugins install` on the current host version, do not immediately assume your local profile/provider configuration is wrong
- In the current recorded `OpenClaw 2026.4.9` baseline, this local tgz path is green again; if it still fails on your machine, it is more likely a host-version difference, npm/pip network issue, or another clean-room environment difference that should be debugged on the install path itself
- Later in the same session, the maintainer-side current-host `CLI / gateway / verify` path was also rechecked on `OpenClaw 2026.4.11`; do not misread the `2026.4.9` tgz note above as “the current host version can only be 4.9”

---

## 4. Clicking `Apply` in `/setup` Returns `401/403`

The key point:

- This usually does not mean the plugin's main chain is broken
- More commonly, the `/bootstrap/*` gate and what you think is the dashboard key are not the same thing

The current boundaries are:

- `/bootstrap/*` only allows localhost loopback
- If the backend has `MCP_API_KEY` configured:
  - `Apply / Restart Backend` also requires this key
- The `Set API key` button in the top-right corner:
  - On the current loopback / same-origin page path, the frontend also attaches it to `/bootstrap/provider-probe`, `/bootstrap/apply`, and `/bootstrap/restart`
  - But it does not bypass `/bootstrap/*`'s own loopback / same-origin restriction

Check first:

```bash
openclaw memory-palace status --json
openclaw memory-palace verify --json
```

Then confirm:

- Whether you are directly connected via localhost loopback
- Whether the backend has `MCP_API_KEY` configured
- Whether the failure is on `/bootstrap/apply`

---

## 5. Clicked `Restart Backend` in `/setup`, but the Page Never Comes Back

The more common causes are:

- The old backend process has not exited yet
- The old port has not been released
- Or the new backend did not actually start

If you are running this from a **repository checkout**, a helper timeout or startup failure usually writes the reason to:

```text
./.tmp/bootstrap-restart-supervisor.log
```

In a repository-checkout path, check first:

```bash
tail -n 50 ./.tmp/bootstrap-restart-supervisor.log
openclaw memory-palace verify --json
```

If you are not in a repository-checkout path, or that file does not exist:

- do not interpret “path missing” as “the plugin is broken”
- the safer move is to go back to `verify / doctor`
- then confirm whether you are on the source-checkout path, the local tgz path, or another install shape

If `verify` still returns `pass`, it is more likely a restart handoff issue, not a broken plugin main chain.

---

## 6. Docker Smoke Immediately Reports `Docker daemon unavailable`

This is usually not a plugin configuration error, but rather:

- Docker Desktop / daemon is not running
- The current shell cannot connect to the Docker socket

Check first:

```bash
docker info
docker compose version
```

If `docker info` itself does not work, do not investigate the plugin yet.

---

## 7. `verify=pass`, but `doctor / smoke=warn`

The key point:

- **This does not necessarily mean something is broken**
- On a fresh runtime, seeing `warn but ok=true` more commonly indicates that no readable target has been seeded yet

The more common warnings are:

- `last-rule-capture-decision`
- `search-probe`
- `profile-memory-state`
- `host-plugin-split-brain`
- `read-probe target missing`

On a normal current install, `host-config-path` itself should already pass in a direct shell rerun.
If it is still warning, check first whether:

- you manually pinned `OPENCLAW_CONFIG_PATH` / `OPENCLAW_CONFIG` to a stale path
- the host config file was moved, renamed, or became unreadable

> On a runtime with existing memory data, `doctor` / `smoke`'s `search_memory probe` returning a small number of hits (e.g., 3 results) is a normal baseline and does not indicate a retrieval anomaly.

In plain terms:

- `verify=pass`
  - More like the wiring / transport / `index_status` main chain is working
- `doctor / smoke=warn`
  - Not unusual on an empty database, empty workspace, or fresh runtime

If the result you are reading is specifically `python3 scripts/test_openclaw_memory_palace_package_install.py`, split the two stdio signals first:

- `smoke_status` + `smoke_mode=seeded_retrieval`
  - This only proves that a seeded durable memory is still retrievable via `search + read`
  - Seeing `profile-memory-state / capture-layer-distribution / host-plugin-split-brain` as `warn` here is currently expected and does not, by itself, mean the profile block failed
- `stdio_capture_verify / doctor / smoke`
  - This is the real profile/capture validation path inside the current package-install script
  - The current recorded baseline for this trio is already `pass`

If you want to resolve these `warn` results, the more effective sequence is:

1. Seed a stable readable target first
2. Then explicitly run smoke with `--path-or-uri`
3. If `host-plugin-split-brain` persists:
   - Give it a normal host workspace first

---

## 8. `Profile C/D` Configured but Still Not Working

The more common real causes are:

- The embedding / reranker / LLM service itself is unavailable
- `RETRIEVAL_EMBEDDING_DIM` does not match the actual model
- Endpoint / key / model name is incorrect

The more reliable diagnostic sequence is:

1. First confirm the `Profile B` main chain works
2. Then add real model configuration
3. Then check `verify / doctor / smoke`

Remember this boundary:

- `C/D` have documented local smoke baselines
- But they do not mean "zero-configuration ready for all user environments"

---

## 9. `request timeout after ... during index_status/search_memory`

This usually looks more like:

- The backend is currently slow
- External embedding / reranker is slow
- Or the transport did not return within the timeout window

Check first:

```bash
openclaw memory-palace status --json
openclaw memory-palace doctor --json
```

Then determine whether it is:

- A transport issue
- An index worker issue
- A slow provider response

---

## 10. Why Does `memory_store_visual` Sometimes Merge, Reject, or Create `--new-01`

This is not random behavior.

The current visual write path has a duplicate strategy:

- `merge`
  - Merges into an existing record
- `reject`
  - Explicitly rejects duplicate writes
- `new`
  - Creates a variant path, e.g., `--new-01`

If the result does not match your expectations, check first:

- The current duplicate policy
- Whether the current visual content was already determined to be the same record

---

## 11. Why Does Visual Search Sometimes Show Namespace First, Then the Actual Content

This usually does not mean retrieval is broken, but rather:

- The namespace container itself is also in the results
- The actual content records are ranked further down

If you just want to check "was the content found," do not look only at the first result -- continue scrolling to see the actual records.

---

## 12. Running Checks in the Plugin Directory Reports `openclaw/plugin-sdk/...` Not Found

The more common cause is:

- You did not install dependencies in the correct directory
- Or `dist` artifacts have not been updated

Do this first:

```bash
cd extensions/memory-palace
npm install --no-package-lock
npm test
npm run typecheck
npm run build
```

If you only changed plugin code without rebuilding `dist`, the real OpenClaw runtime may continue reading old artifacts.

---

## 13. `openclaw agent --local ...` Reports `session file locked`

This usually does not mean the onboarding skill is broken, but rather:

- The session file for this agent is being occupied by another OpenClaw process

The more common sources are:

- This session is already open in the WebUI
- Another local CLI / gateway process is writing to the same session

The more reliable resolution sequence is:

1. Use a new `--session-id`
2. Or create a temporary agent for this round of testing
3. Or close the UI / CLI process currently occupying this session

---

## 14. `Concurrent modification detected`

If you see this error when writing or editing a memory:

The key point:

- This means you read an old version of the memory, but another process updated it before you submitted your changes
- This is normal concurrency protection behavior, not a bug

The more reliable resolution sequence is:

1. Re-read the current content
2. Re-edit based on the latest content
3. Submit again

If you are a single user on a single machine and encounter this error frequently, it is more likely that:
- Multiple MCP server processes are writing to the same memory simultaneously
- Or Dashboard and CLI are writing to the same entry at the same time

---

## 14. Onboarding Skill Loaded, but Conversation Reports `401` / `auth_unavailable`

If you have confirmed:

- a Memory Palace-related entry is visible on the `Skills` page
- `memory_onboarding_status / probe / apply` are registered in the plugin tool surface

But the real conversation still reports:

```text
HTTP 401 ...
```

Or:

```text
auth_unavailable
```

The more accurate understanding is usually:

- The onboarding skill itself has loaded
- The issue is with the host's current chat model provider

Check first:

```bash
openclaw plugins inspect memory-palace --json
```

If your goal is simply to confirm the plugin is installed, `plugins info` is more reliable than depending on one specific `Skills` display label.

Then confirm:

- Whether the model endpoint / key / model currently used by OpenClaw is still valid
- Whether the provider used by local `openclaw agent --local` can actually be called directly

The more reliable follow-up sequence is:

```bash
openclaw models status --json --probe --agent main
```

This command is better suited to answer two questions first:

1. Whether the actual default model for the `main` agent is the one you expected
2. Whether the provider probe is actually `ok` or already failing on auth / timeout

There is one easily misjudged point:

- If the last entry in the logs looks like an `anthropic` auth failure
- It does not necessarily mean the root cause is `anthropic`

The more realistic scenario may be:

- The primary model was actually your own locally configured OpenAI-compatible provider
- The primary model failed first
- The subsequent failover / auth path then surfaced the `anthropic` error

So do not focus only on the last provider name -- check the `models status --probe` result first.

If the model provider itself is broken, do not misattribute this as "the onboarding flow is unavailable."

---

## 15. The Runtime Is Already on the New Dimension, but Conversation Still Says an Older Value

If you have already completed these three steps:

1. `python3 scripts/openclaw_memory_palace.py provider-probe --json`
2. `python3 scripts/openclaw_memory_palace.py onboarding --apply --validate --json`
3. `openclaw memory-palace index --wait --json`

And the runtime / verify already show that the new dimension is active,

But an older conversation session still reports an earlier dimension value, the more accurate understanding is usually:

- That session is still replaying an earlier recall result
- It does not mean the current shared runtime has not been updated correctly

The more reliable resolution sequence is:

1. Open a new session and ask again
2. Or directly re-run `python3 scripts/openclaw_memory_palace.py provider-probe --json`
3. If you want the host to always answer based on real-time state, explicitly request it to call `memory_onboarding_probe`

One more clarification to avoid confusion:

- If you simply ask in natural language "What is the currently recommended dimension?"
- The host model may just guess a value

So whenever you need the **current real-time value**, the most reliable approach is:

1. `python3 scripts/openclaw_memory_palace.py provider-probe --json`
2. Or explicitly ask the host to call `memory_onboarding_probe`

---

## 16. `lock_retries_total` Is Very High -- What Does It Mean

If you see a large number for `lock_retries_total` in `openclaw memory-palace status --json` or the Dashboard Observability page:

The key point:

- This usually means multiple processes are writing to the same SQLite file simultaneously
- It does not necessarily mean something is broken -- the write lane automatically retries (exponential backoff, 3 attempts by default)
- Most retries eventually succeed

The more reliable diagnostic sequence is:

1. Check `lock_retries_exhausted` first -- this is the count of retries that were fully exhausted and still failed
2. If `exhausted` is 0 or very low, retries are succeeding, and no intervention is needed
3. If `exhausted` is continuously growing, contention is too severe; consider:
   - Reducing the number of concurrently running MCP server processes
   - Or increasing `RUNTIME_WRITE_LOCK_RETRY_ATTEMPTS`

---

## 17. `Timed out waiting for snapshot session lock`

The key point:

- This is more likely normal contention or a long transaction
- It does not necessarily mean the review / snapshot main chain is broken

Handle it by:

1. Wait for the previous operation to finish
2. Retry
3. If it keeps recurring, then investigate whether there is a long-held lock

Only when you see frequent recurrence -- and it is clearly not normal concurrency -- is it worth investigating deeper lock contention.

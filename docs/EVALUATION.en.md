> [中文版](EVALUATION.md)

# Evaluation Summary

This page keeps the public English validation summary short and explicit.

The top summary below now reflects the latest recorded **Windows real-machine**
reruns dated **April 15, 2026**. Treat everything here as
**environment-specific evidence**, not as a universal promise for every
machine.

## Latest Recorded Windows Real-Machine Reruns

The commands below were rerun on the current Windows validation host:

| Area | Command | Result |
|---|---|---|
| host version | `openclaw --version` | `OpenClaw 2026.4.14` |
| Profile A setup | `py -3 scripts/openclaw_memory_palace.py setup --mode basic --profile a --transport stdio --json` | pass |
| Profile A sign-off | `openclaw memory-palace verify / doctor / smoke --json` | pass on isolated target config |
| Profile B setup | `py -3 scripts/openclaw_memory_palace.py setup --mode basic --profile b --transport stdio --json` | pass |
| Profile B plugin load | `openclaw plugins inspect memory-palace --json` | plugin loaded |
| Profile B onboarding tools | `memory_onboarding_status / probe / apply` | registered on the real host |
| Profile B sign-off | `openclaw memory-palace verify / doctor / smoke --json` | pass |
| Profile C provider probe | `py -3 scripts/openclaw_memory_palace.py provider-probe --profile c ... --json` | pass |
| Profile D provider probe | `py -3 scripts/openclaw_memory_palace.py provider-probe --profile d ... --json` | pass |
| Profile C apply | `py -3 scripts/openclaw_memory_palace.py onboarding --profile c --apply --validate --json` | pass |
| Profile D apply | `py -3 scripts/openclaw_memory_palace.py onboarding --profile d --apply --validate --json` | pass |
| Profile C / D sign-off | `openclaw memory-palace verify / doctor / smoke --json` | pass after apply |
| local tgz / clean-room package validation | `py -3 scripts/test_openclaw_memory_palace_package_install.py` | pass, including `stdio` / `sse` capture and auto-install |
| full Windows native validation rerun | `py -3 scripts/openclaw_memory_palace_windows_native_validation.py --profiles b,c,d --model-env <model-env> --skip-package-install --keep-artifacts` | pass, with refreshed `windows_native_validation_report.{json,md}` |
| Windows native validation unittest | `py -3 -m unittest scripts.test_openclaw_memory_palace_windows_native_validation` | pass |
| installer regression | `py -3 -m unittest scripts.test_openclaw_memory_palace_installer` | pass |
| extension test suite | `cd extensions/memory-palace && bun test` | `488 pass / 2 skip / 0 fail` |
| extension typecheck | `cd extensions/memory-palace && npm run typecheck` | pass |
| latest doc-chat targeted reruns | `cli-uninstalled-zh`, `cli-installed-zh`, `cli-installed-en` | pass |

## How To Read The Current Results

- The untouched host boundary was also reconfirmed on Windows: before the
  plugin is installed, `memory_onboarding_status / probe / apply` do not exist
  yet. Do not collapse the installed and uninstalled branches into one path.
- `Profile B` remains the safest first-run baseline.
- Once embedding + reranker + LLM are already ready, `Profile D` remains the
  current strong recommendation.
- On this Windows host, the real-machine `A / B / C / D` path now has passing
  setup / probe / apply / sign-off evidence under isolated target configs.
- In the latest `windows_native_validation_report.{json,md}`, `Profile B / C / D`
  are all recorded as `PASS`.
- This round also reran the local `tgz` / clean-room package path on the real
  Windows host; the recorded `stdio` / `sse` capture legs are both green again.
- So if you are reading that Windows native report and see
  `Package Install: skipped by flag`, do not misread it as “package-install was
  not validated in this round”; package-install was rerun separately on purpose.
- In package-install validation, if the same stable workflow is written again,
  a profile block that stays unchanged is no longer treated as a failure by
  itself; the more important checks are whether the capture was processed and
  whether the existing profile block is still readable.
- The latest wording cleanup was rechecked through targeted doc-chat reruns for
  `cli-uninstalled-zh`, `cli-installed-zh`, and `cli-installed-en`.

## Additional macOS / Linux Maintainer Reruns After The Windows Baseline

These reruns are additive maintainer evidence. They do **not** replace the
Windows top baseline above.

- On the macOS validation host, the shared-LLM `Profile D` path was rerun
  against `OpenClaw 2026.4.14` after the placeholder-override fix. The recorded
  `setup --profile d` and `onboarding --profile d --strict-profile --apply --validate`
  runs kept `effective_profile=d` and `fallback_applied=false`.
- A fresh isolated `Profile B` replacement-acceptance rerun was also added on
  the macOS validation host without reusing `current-host main` and without the
  optional `V7` short-session extension. The final report is
  `.tmp/replacement-acceptance/isolated-clean-lane-b-final/webui_report.json`,
  and the base WebUI gate is now `6/6 PASS`.
- In that fresh isolated rerun, `V4` confirmed the forced workflow variant via
  `core://agents/alpha/captured/workflow/sha256-e99b298bd064`, while `V5 / V6`
  each confirmed their own isolated `alpha` fact capture. So this repository no
  longer needs `current-host main` reuse to explain those final acceptance
  results.
- A second fresh isolated `Profile B` rerun was then added as a strict UI
  negative gate, still without reusing `current-host main` and still without
  the optional `V7` short-session extension. The final report is
  `.tmp/replacement-acceptance/isolated-clean-lane-b-ui-noise-fix3/webui_report.json`,
  and that stricter gate also ends at `6/6 PASS`.
- In that stricter rerun, `V2 / V4 / V5 / V6` also required the visible chat
  body to stay free of raw `memory-palace-profile` / `memory-palace-recall`
  blocks and the earlier control-ui metadata noise. So the `v1.1.1` fix is now
  recorded as both a functional recall fix and a user-visible chat-surface fix.
- In Docker Linux userspace reruns, both `linux/aarch64` and `linux/amd64`
  passed the same shared-LLM `Profile D` path: `setup --profile b`,
  `setup --profile d`, and `onboarding --profile d --strict-profile --apply --validate`.
- The Docker WebUI reruns also reconfirmed that the default gateway port path
  stayed reachable and that the bundled onboarding skill remained visible.
- A narrower host limitation still remained visible: if the host is started on
  a custom gateway port without persisting that port back into config,
  `dashboard --no-open` / `gateway.controlUi.allowedOrigins` may still fall
  back to the default port. This repository records that as an OpenClaw host
  limitation rather than a plugin-side promise or host patch target.

## Stable Public Boundaries

- `Profile C / D` are provider-backed paths, not zero-config promises.
- `onboarding --json` is a readiness report.
- `setup ...` and `onboarding --apply --validate ...` are the commands that
  actually change host configuration.
- The stable user command surface after install remains
  `openclaw memory-palace ...`.
- The current docs use `openclaw plugins inspect memory-palace --json` as the
  explicit plugin-load check. Some hosts also accept `plugins info`, but
  `openclaw skills list` is not the install gate for the bundled onboarding
  skill.
- The current public chat-first claim covers handing the checked-out local page
  or local doc path to OpenClaw. It does **not** claim that every host can
  fetch arbitrary public GitHub URLs on its own.

## Official Host Install Entry

If OpenClaw itself is not installed yet, start here:

- `https://docs.openclaw.ai/install`

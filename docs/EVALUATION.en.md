> [中文版](EVALUATION.md)

# Evaluation Summary

This page keeps the public English validation summary short and explicit.

It reflects the latest recorded reruns on a macOS validation host. Treat
everything here as **environment-specific evidence**, not as a universal
promise for every machine.

## Latest Recorded Reruns

The commands below were rerun on the current macOS validation host:

| Area | Command | Result |
|---|---|---|
| host version | `openclaw --version` | `OpenClaw 2026.4.11` |
| plugin load | `openclaw plugins info memory-palace --json` | plugin loaded |
| current-host status | `openclaw memory-palace status --json` | reachable |
| current-host verify | `openclaw memory-palace verify --json` | `ok=true`, `status=warn` |
| current-host doctor | `openclaw memory-palace doctor --json` | `ok=true`, `status=warn` |
| current-host smoke | `openclaw memory-palace smoke --json` | `ok=true`, `status=warn` |
| wrapper bootstrap status | `python3 scripts/openclaw_memory_palace.py bootstrap-status --json` | reachable |
| wrapper onboarding (`Profile B`) | `python3 scripts/openclaw_memory_palace.py onboarding --profile b --json` | readiness available |
| provider probe (`Profile C`) | `python3 scripts/openclaw_memory_palace.py provider-probe --profile c ... --json` | pass |
| provider probe (`Profile D`) | `python3 scripts/openclaw_memory_palace.py provider-probe --profile d ... --json` | pass |
| onboarding preview (`Profile C`) | `python3 scripts/openclaw_memory_palace.py onboarding --profile c ... --json` | ready |

## How To Read The Current Results

- `Profile B` remains the safest first-run baseline.
- The latest recorded provider-probe reruns for `Profile C / D` passed on the
  tested provider configuration.
- The latest isolated profile-matrix rerun passed for `A / B / C-default /
  C-llm / D-default`, and `onboarding --apply --validate` passed for
  `Profile C / D` on the same macOS validation host.
- The current-host `verify / doctor / smoke` reruns did **not** fail hard, but
  they still returned `warn`.
- In the latest recorded reruns, the main warn reasons were recorded fallback
  metadata and capture-path diagnostics rather than “plugin not installed”.

## Stable Public Boundaries

- `Profile C / D` are provider-backed paths, not zero-config promises.
- `onboarding --json` is a readiness report.
- `setup ...` and `onboarding --apply --validate ...` are the commands that
  actually change host configuration.
- The stable user command surface after install remains
  `openclaw memory-palace ...`.
- The current public chat-first claim covers handing the checked-out local page
  or local doc path to OpenClaw. It does **not** claim that every host can
  fetch arbitrary public GitHub URLs on its own.

## Official Host Install Entry

If OpenClaw itself is not installed yet, start here:

- `https://docs.openclaw.ai/install`

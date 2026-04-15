> [中文版](06-UPGRADE_CHECKLIST.md)

# 06 · Upgrade Review Checklist

This page keeps one job only:

> give maintainers a minimal pre-release review checklist.

Positioning first:

- this is a maintainer appendix
- it is not the default user entry
- current public validation summary stays in `../EVALUATION.en.md`

---

## 1. Minimum Things to Recheck Before Release

1. install and command surface
   - `setup / verify / doctor / smoke / migrate / upgrade` are still coherent
2. plugin package shape
   - source checkout and local `tgz` paths are still recognizable by OpenClaw
3. user-facing message
   - OpenClaw users still see a memory plugin path, not a raw MCP toolbox
4. documentation hygiene
   - no private paths, provider secrets, or maintainer-only local state leak into public pages

---

## 2. Boundaries That Still Need Conservative Wording

- target environments still need their own reruns
- Windows / macOS / Linux conclusions do not substitute for each other
- `Profile C/D` still depend on user-provided model services
- `warn but ok=true` should not be flattened into “hard failure” without context

---

## 3. Host Self-Check Gate First

Run host self-checks before blaming the plugin:

```bash
openclaw --version
openclaw doctor --help
openclaw plugins list --json
openclaw status --json
openclaw health --json
```

If the host itself is broken, stop there and fix the host layer first.

---

## 4. Minimal Package/Command Recheck

```bash
cd extensions/memory-palace
npm pack
openclaw plugins install --dangerously-force-unsafe-install ./openclaw-memory-palace-<version>.tgz
openclaw plugins inspect memory-palace --json
openclaw memory-palace verify --json
openclaw memory-palace doctor --json
openclaw memory-palace smoke --json
```

For a local `tgz` built from this repository, some host builds may still require
`--dangerously-force-unsafe-install`.
Do not generalize that flag to every future installation path.

import test from "node:test";
import assert from "node:assert/strict";
import os from "node:os";
import path from "node:path";
import { mkdtemp, rm, writeFile } from "node:fs/promises";

import {
  buildDashboardUrlForPort,
  getDashboardUrl,
  loadMainConfigSnapshot,
  parseDashboardUrlOutput,
} from "./openclaw_onboarding_doc_test_lib.mjs";

test("loadMainConfigSnapshot prefers OPENCLAW_CONFIG_PATH over host config lookup", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "openclaw-main-config-"));
  const configPath = path.join(root, "openclaw.json");
  const originalConfigPath = process.env.OPENCLAW_CONFIG_PATH;
  const originalConfig = process.env.OPENCLAW_CONFIG;

  try {
    await writeFile(
      configPath,
      JSON.stringify(
        {
          models: {
            providers: {
              demo: {
                baseUrl: "http://127.0.0.1:8318/v1",
                api: "openai-completions",
                models: [{ id: "gpt-5.4-mini", name: "gpt-5.4-mini" }],
              },
            },
          },
        },
        null,
        2,
      ),
      "utf8",
    );

    process.env.OPENCLAW_CONFIG_PATH = configPath;
    delete process.env.OPENCLAW_CONFIG;

    const snapshot = await loadMainConfigSnapshot();
    assert.equal(snapshot.configPath, configPath);
    assert.deepEqual(Object.keys(snapshot.payload.models.providers), ["demo"]);
  } finally {
    if (originalConfigPath === undefined) {
      delete process.env.OPENCLAW_CONFIG_PATH;
    } else {
      process.env.OPENCLAW_CONFIG_PATH = originalConfigPath;
    }
    if (originalConfig === undefined) {
      delete process.env.OPENCLAW_CONFIG;
    } else {
      process.env.OPENCLAW_CONFIG = originalConfig;
    }
    await rm(root, { recursive: true, force: true });
  }
});

test("loadMainConfigSnapshot prefers OPENCLAW_ONBOARDING_BASE_CONFIG_PATH over OPENCLAW_CONFIG_PATH", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "openclaw-main-config-"));
  const baseConfigPath = path.join(root, "base-openclaw.json");
  const configPath = path.join(root, "openclaw.json");
  const originalBaseConfigPath = process.env.OPENCLAW_ONBOARDING_BASE_CONFIG_PATH;
  const originalConfigPath = process.env.OPENCLAW_CONFIG_PATH;

  try {
    await writeFile(
      baseConfigPath,
      JSON.stringify(
        {
          models: {
            providers: {
              onboarding: {
                baseUrl: "http://127.0.0.1:8318/v1",
                api: "openai-completions",
                models: [{ id: "gpt-5.4-mini", name: "gpt-5.4-mini" }],
              },
            },
          },
        },
        null,
        2,
      ),
      "utf8",
    );
    await writeFile(
      configPath,
      JSON.stringify({ agents: { defaults: { workspace: root } } }, null, 2),
      "utf8",
    );

    process.env.OPENCLAW_ONBOARDING_BASE_CONFIG_PATH = baseConfigPath;
    process.env.OPENCLAW_CONFIG_PATH = configPath;

    const snapshot = await loadMainConfigSnapshot();
    assert.equal(snapshot.configPath, baseConfigPath);
    assert.deepEqual(Object.keys(snapshot.payload.models.providers), ["onboarding"]);
  } finally {
    if (originalBaseConfigPath === undefined) {
      delete process.env.OPENCLAW_ONBOARDING_BASE_CONFIG_PATH;
    } else {
      process.env.OPENCLAW_ONBOARDING_BASE_CONFIG_PATH = originalBaseConfigPath;
    }
    if (originalConfigPath === undefined) {
      delete process.env.OPENCLAW_CONFIG_PATH;
    } else {
      process.env.OPENCLAW_CONFIG_PATH = originalConfigPath;
    }
    await rm(root, { recursive: true, force: true });
  }
});

test("buildDashboardUrlForPort renders tokenized local dashboard url", () => {
  assert.equal(
    buildDashboardUrlForPort(18951),
    "http://127.0.0.1:18951/#token=status-probe-local-only",
  );
});

test("parseDashboardUrlOutput extracts dashboard url from command output", () => {
  const output = [
    "Dashboard URL: http://127.0.0.1:18951/#token=status-probe-local-only",
    "Copy to clipboard unavailable.",
    "Browser launch disabled (--no-open). Use the URL above.",
  ].join("\n");

  assert.equal(
    parseDashboardUrlOutput(output),
    "http://127.0.0.1:18951/#token=status-probe-local-only",
  );
});

test("getDashboardUrl prefers cli dashboard output even when scenario has a port", async () => {
  const calls = [];
  const scenario = {
    env: { OPENCLAW_CONFIG_PATH: "/tmp/demo-openclaw.json" },
    port: 48231,
  };

  const url = await getDashboardUrl(scenario, {
    commandRunner: async (command, args, options) => {
      calls.push({ command, args, options });
      return {
        code: 0,
        stdout: "Dashboard URL: http://127.0.0.1:18951/#token=status-probe-local-only\n",
        stderr: "",
      };
    },
  });

  assert.equal(url, "http://127.0.0.1:18951/#token=status-probe-local-only");
  assert.equal(calls.length, 1);
  assert.deepEqual(calls[0].args, ["dashboard", "--no-open"]);
  assert.equal(calls[0].options?.env, scenario.env);
});

test("getDashboardUrl can still use the scenario port fallback explicitly", async () => {
  const scenario = {
    env: { OPENCLAW_CONFIG_PATH: "/tmp/demo-openclaw.json" },
    port: 18951,
  };

  const url = await getDashboardUrl(scenario, {
    source: "scenario-port",
    commandRunner: async () => {
      throw new Error("dashboard cli should not be invoked for explicit scenario-port mode");
    },
  });

  assert.equal(url, "http://127.0.0.1:18951/#token=status-probe-local-only");
});

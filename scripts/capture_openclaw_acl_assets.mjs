#!/usr/bin/env node
import os from "node:os";
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(scriptDir, "..");
const assetsDir = path.join(repoRoot, "docs", "openclaw-doc", "assets", "real-openclaw-run");
const useCurrentHost = String(process.env.OPENCLAW_ACL_USE_CURRENT_HOST || "").trim().toLowerCase() === "true";
const currentHostUrl =
  process.env.OPENCLAW_ACL_CONTROL_UI_URL || "http://127.0.0.1:48231/#token=status-probe-local-only";

function ensureScenarioTempRoot() {
  if (!String(process.env.OPENCLAW_ONBOARDING_TEMP_ROOT || "").trim()) {
    process.env.OPENCLAW_ONBOARDING_TEMP_ROOT = path.join(
      os.tmpdir(),
      "openclaw-onboarding-doc-chat-flow",
    );
  }
  if (!String(process.env.PYTHON3 || "").trim()) {
    process.env.PYTHON3 = "python3";
  }
}

async function loadScenarioLib() {
  ensureScenarioTempRoot();
  return await import("./openclaw_onboarding_doc_test_lib.mjs");
}

const aclCaptureSpecs = {
  zh: {
    locale: "zh-CN",
    agentsPath: "24-acl-agents-page.png",
    alphaPath: "24-acl-alpha-memory-confirmed.png",
    betaPath: "24-acl-beta-chat-isolated.png",
    alphaPrompt: "请记住：alpha 的默认 workflow 是先列清单，再实现，最后补测试。只回复“已为 alpha 记住”。",
    alphaExpected: "已为 alpha 记住",
    betaPrompt: "alpha 的默认 workflow 是什么？如果你不知道，只回复 UNKNOWN。",
    betaExpected: "UNKNOWN",
    overlayTitle: "ACL 演示 agent",
    overlaySubtitle: "当前这次 WebUI 里可切换的 scope",
    overlayItems: ["main (default)", "alpha", "beta", "beta 不应 recall alpha"],
  },
  en: {
    locale: "en-US",
    agentsPath: "24-acl-agents-page.en.png",
    alphaPath: "24-acl-alpha-memory-confirmed.en.png",
    betaPath: "24-acl-beta-chat-isolated.en.png",
    alphaPrompt: "Please remember: alpha's default workflow is list first, implement next, tests last. Reply only \"Stored for alpha\".",
    alphaExpected: "Stored for alpha",
    betaPrompt: "What is alpha's default workflow? Reply UNKNOWN if you cannot know.",
    betaExpected: "UNKNOWN",
    overlayTitle: "ACL run agents",
    overlaySubtitle: "What this WebUI run is scoped to",
    overlayItems: ["main (default)", "alpha", "beta", "beta must not recall alpha"],
  },
};

function routeUrl(baseUrl, routePath) {
  return new URL(routePath, baseUrl.replace(/#.*$/, "")).toString();
}

async function redactSensitiveText(page) {
  await page.evaluate(() => {
    const replacements = [
      [/\/Users\/[^/\s]+/g, "/Users/<redacted>"],
      [/\/home\/[^/\s]+/g, "/home/<redacted>"],
      [/\/private\/var\/folders\/[^\s)]+/g, "/private/var/folders/<redacted>"],
      [/\/var\/folders\/[^\s)]+/g, "/var/folders/<redacted>"],
      [/[A-Za-z]:\\\\Users\\\\[^\\\\\s]+/g, "C:\\\\Users\\\\<redacted>"],
    ];
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    let current = walker.nextNode();
    while (current) {
      let text = current.textContent || "";
      for (const [pattern, replacement] of replacements) {
        text = text.replace(pattern, replacement);
      }
      current.textContent = text;
      current = walker.nextNode();
    }
  });
}

async function addAgentsOverlay(page, captureSpec) {
  await page.evaluate(({ title, subtitle, items }) => {
    const existing = document.getElementById("acl-safe-overlay");
    existing?.remove();
    const overlay = document.createElement("aside");
    overlay.id = "acl-safe-overlay";
    overlay.style.position = "fixed";
    overlay.style.top = "70px";
    overlay.style.right = "40px";
    overlay.style.width = "300px";
    overlay.style.padding = "18px 20px";
    overlay.style.borderRadius = "18px";
    overlay.style.border = "1px solid rgba(170, 140, 98, 0.48)";
    overlay.style.background = "rgba(248, 239, 223, 0.96)";
    overlay.style.boxShadow = "0 18px 44px rgba(57, 42, 26, 0.12)";
    overlay.style.fontFamily = "Avenir Next, PingFang SC, sans-serif";
    overlay.style.color = "#4a3723";
    overlay.style.zIndex = "9999";

    const heading = document.createElement("div");
    heading.style.fontSize = "18px";
    heading.style.fontWeight = "700";
    heading.textContent = title;
    overlay.appendChild(heading);

    const sub = document.createElement("div");
    sub.style.marginTop = "4px";
    sub.style.fontSize = "12px";
    sub.style.opacity = "0.82";
    sub.textContent = subtitle;
    overlay.appendChild(sub);

    const list = document.createElement("ul");
    list.style.margin = "14px 0 0";
    list.style.padding = "0";
    list.style.listStyle = "none";
    list.style.display = "grid";
    list.style.gap = "8px";
    for (const item of items) {
      const li = document.createElement("li");
      li.style.padding = "8px 10px";
      li.style.borderRadius = "10px";
      li.style.background = "rgba(255,255,255,0.42)";
      li.style.fontSize = "14px";
      li.textContent = item;
      list.appendChild(li);
    }
    overlay.appendChild(list);
    document.body.appendChild(overlay);
  }, {
    title: captureSpec.overlayTitle,
    subtitle: captureSpec.overlaySubtitle,
    items: captureSpec.overlayItems,
  });
}

async function countOccurrences(page, text) {
  return page.evaluate((needle) => {
    const haystack = document.body.innerText || "";
    if (!needle) return 0;
    return haystack.split(needle).length - 1;
  }, text);
}

function patchAclConfig(config) {
  const nextConfig = JSON.parse(JSON.stringify(config || {}));
  nextConfig.plugins = nextConfig.plugins || {};
  nextConfig.plugins.allow = Array.from(new Set([...(nextConfig.plugins.allow || []), "memory-palace"]));
  nextConfig.plugins.load = nextConfig.plugins.load || {};
  nextConfig.plugins.load.paths = Array.from(new Set([...(nextConfig.plugins.load.paths || []), path.join(repoRoot, "extensions", "memory-palace")]));
  nextConfig.plugins.slots = { ...(nextConfig.plugins.slots || {}), memory: "memory-palace" };
  nextConfig.plugins.entries = nextConfig.plugins.entries || {};
  nextConfig.plugins.entries["memory-palace"] = nextConfig.plugins.entries["memory-palace"] || {
    enabled: true,
    config: {},
  };
  nextConfig.plugins.entries["memory-palace"].enabled = true;
  nextConfig.plugins.entries["memory-palace"].config = {
    ...(nextConfig.plugins.entries["memory-palace"].config || {}),
    acl: {
      enabled: true,
      sharedUriPrefixes: [],
      sharedWriteUriPrefixes: [],
      defaultPrivateRootTemplate: "core://agents/{agentId}",
      allowIncludeAncestors: false,
      defaultDisclosure: "Agent-scoped durable memory.",
      agents: {
        main: {
          allowedUriPrefixes: ["core://agents/main"],
          writeRoots: ["core://agents/main"],
          allowIncludeAncestors: false,
        },
        alpha: {
          allowedUriPrefixes: ["core://agents/alpha"],
          writeRoots: ["core://agents/alpha"],
          allowIncludeAncestors: false,
        },
        beta: {
          allowedUriPrefixes: ["core://agents/beta"],
          writeRoots: ["core://agents/beta"],
          allowIncludeAncestors: false,
        },
      },
    },
  };
  return nextConfig;
}

async function resolveHost({ getDashboardUrl, prepareScenario, startGateway }) {
  if (useCurrentHost) {
    return {
      dashboardUrl: currentHostUrl,
      scenario: null,
      gateway: null,
    };
  }
  const scenario = await prepareScenario({
    name: "acl-ui-capture",
    port: 48231,
    installPlugin: true,
    profile: "b",
    setupMode: "basic",
    extraAgents: ["alpha", "beta"],
    configMutator: async (config) => patchAclConfig(config),
  });
  const gateway = await startGateway(scenario);
  const dashboardUrl = await getDashboardUrl(scenario);
  return { dashboardUrl, scenario, gateway };
}

async function captureLanguage(page, dashboardUrl, language, captureSpec) {
  await page.context().setDefaultTimeout(60_000);
  await page.goto(routeUrl(dashboardUrl, "/agents"), { waitUntil: "networkidle", timeout: 90_000 });
  await page.waitForTimeout(1_200);
  await redactSensitiveText(page);
  await addAgentsOverlay(page, captureSpec);
  await page.screenshot({ path: path.join(assetsDir, captureSpec.agentsPath), fullPage: false });

  const alphaUrl = new URL(routeUrl(dashboardUrl, "/chat"));
  alphaUrl.searchParams.set("session", "agent:alpha:main");
  await page.goto(alphaUrl.toString(), { waitUntil: "networkidle", timeout: 90_000 });
  await page.waitForTimeout(1_400);
  const alphaInput = page.locator('textarea, input[placeholder*="Message"], input[placeholder*="消息"], [contenteditable="true"]').last();
  const alphaBefore = await countOccurrences(page, captureSpec.alphaExpected);
  await alphaInput.click();
  await alphaInput.fill(captureSpec.alphaPrompt);
  await page.keyboard.press("Enter");
  await page.waitForTimeout(2_000);
  const alphaAfterPrompt = await countOccurrences(page, captureSpec.alphaExpected);
  await page.waitForFunction(
    ({ expected, before }) => {
      const body = document.body.innerText || "";
      return body.split(expected).length - 1 > before;
    },
    { expected: captureSpec.alphaExpected, before: Math.max(alphaBefore, alphaAfterPrompt) },
    { timeout: 90_000 },
  );
  await page.waitForTimeout(1_000);
  await redactSensitiveText(page);
  await page.screenshot({ path: path.join(assetsDir, captureSpec.alphaPath), fullPage: false });

  const betaUrl = new URL(routeUrl(dashboardUrl, "/chat"));
  betaUrl.searchParams.set("session", "agent:beta:main");
  await page.goto(betaUrl.toString(), { waitUntil: "networkidle", timeout: 90_000 });
  await page.waitForTimeout(1_400);
  const betaInput = page.locator('textarea, input[placeholder*="Message"], input[placeholder*="消息"], [contenteditable="true"]').last();
  const betaBefore = await countOccurrences(page, captureSpec.betaExpected);
  await betaInput.click();
  await betaInput.fill(captureSpec.betaPrompt);
  await page.keyboard.press("Enter");
  await page.waitForTimeout(2_000);
  const betaAfterPrompt = await countOccurrences(page, captureSpec.betaExpected);
  await page.waitForFunction(
    ({ expected, before }) => {
      const body = document.body.innerText || "";
      return body.split(expected).length - 1 > before;
    },
    { expected: captureSpec.betaExpected, before: Math.max(betaBefore, betaAfterPrompt) },
    { timeout: 90_000 },
  );
  await page.waitForTimeout(1_000);
  await redactSensitiveText(page);
  await page.screenshot({ path: path.join(assetsDir, captureSpec.betaPath), fullPage: false });

  return {
    language,
    agents: captureSpec.agentsPath,
    alpha: captureSpec.alphaPath,
    beta: captureSpec.betaPath,
  };
}

async function main() {
  await mkdir(assetsDir, { recursive: true });
  const scenarioLib = await loadScenarioLib();
  const { getDashboardUrl, loadPlaywright, prepareScenario, startGateway, stopGateway } = scenarioLib;
  const { dashboardUrl, scenario, gateway } = await resolveHost({
    getDashboardUrl,
    prepareScenario,
    startGateway,
  });
  const playwright = await loadPlaywright();
  const browser = await playwright.chromium.launch({ headless: true });
  const results = [];
  try {
    for (const [language, captureSpec] of Object.entries(aclCaptureSpecs)) {
      const context = await browser.newContext({
        viewport: { width: 1600, height: 1200 },
        locale: captureSpec.locale,
      });
      const page = await context.newPage();
      try {
        results.push(await captureLanguage(page, dashboardUrl, language, captureSpec));
      } finally {
        await context.close().catch(() => {});
      }
    }
    const payload = {
      generatedAt: new Date().toISOString(),
      controlUiUrl: useCurrentHost ? "<current-host>" : "<isolated-scenario>",
      scenarioRoot: scenario?.root || null,
      configPath: scenario?.configPath || null,
      captures: results,
    };
    await writeFile(
      path.join(assetsDir, "openclaw-acl-assets-manifest.json"),
      `${JSON.stringify(payload, null, 2)}\n`,
      "utf8",
    );
    console.log(JSON.stringify(payload, null, 2));
  } finally {
    await browser.close().catch(() => {});
    await stopGateway(gateway).catch(() => {});
  }
}

main().catch((error) => {
  console.error(error instanceof Error ? error.stack || error.message : String(error));
  process.exitCode = 1;
});

#!/usr/bin/env node
import path from "node:path";
import {
  assertExcludes,
  assertIncludes,
  docEnPath,
  docZhPath,
  getDashboardUrl,
  loadPlaywright,
  prepareScenario,
  repoRoot,
  runGatewayAgent,
  startGateway,
  stopGateway,
  tempRoot,
  writeJson,
} from "./openclaw_onboarding_doc_test_lib.mjs";

const reportPath = path.join(tempRoot, "onboarding-doc-chat-flow.report.json");
const rawMaxRetries = Number.parseInt(
  process.env.OPENCLAW_ONBOARDING_CHAT_FLOW_MAX_RETRIES || "2",
  10,
);
const maxTransientRetries = Number.isFinite(rawMaxRetries) && rawMaxRetries >= 0 ? rawMaxRetries : 2;
const rawRetryDelay = Number.parseInt(
  process.env.OPENCLAW_ONBOARDING_CHAT_FLOW_RETRY_DELAY_MS || "5000",
  10,
);
const retryDelayMs = Number.isFinite(rawRetryDelay) && rawRetryDelay > 0 ? rawRetryDelay : 5000;
const useCurrentInstalledHost =
  String(process.env.OPENCLAW_ONBOARDING_USE_CURRENT_HOST || "").trim().toLowerCase() === "true";
const sessionRunNonce =
  String(process.env.OPENCLAW_ONBOARDING_CHAT_FLOW_RUN_ID || "").trim()
  || Date.now().toString(36);
const expectedProfileBSetupCommand = process.platform === "win32"
  ? "py -3 scripts/openclaw_memory_palace.py setup --mode basic --profile b --transport stdio --json"
  : "python3 scripts/openclaw_memory_palace.py setup --mode basic --profile b --transport stdio --json";
const selectedCases = new Set(
  String(process.env.OPENCLAW_ONBOARDING_CASES || "")
    .split(",")
    .map((value) => value.trim())
    .filter(Boolean),
);

function okCase(name, details) {
  return { name, ok: true, ...details };
}

function assertIncludesAny(text, expectedTexts, context) {
  const rendered = String(text || "");
  if (expectedTexts.some((expected) => rendered.includes(expected))) {
    return;
  }
  throw new Error(
    `${context}: expected output to include one of ${expectedTexts.map((value) => JSON.stringify(value)).join(", ")}\n\n${rendered}`,
  );
}

function assertUninstalledOnboardingBoundary(text, context) {
  const rendered = String(text || "");
  const boundarySignals = [
    "memory_onboarding_status",
    "tool 还不存在",
    "tools 还不存在",
    "tool 不存在",
    "不能一上来就假设",
    "不要假设",
    "plugin 未安装",
    "还没安装",
  ];
  if (boundarySignals.some((signal) => rendered.includes(signal))) {
    return;
  }
  throw new Error(
    `${context}: expected output to explain that onboarding tools cannot be assumed before plugin install\n\n${rendered}`,
  );
}

function shouldRunCase(name) {
  return selectedCases.size === 0 || selectedCases.has(name);
}

function isTransientRateLimitFailure(error) {
  const text = String(error?.stack || error || "");
  return /API rate limit reached|model_cooldown|HTTP 429|Too Many Requests/i.test(text);
}

function withAttemptSessionId(sessionId, attempt) {
  return `${sessionId}-a${attempt}`;
}

function installedSessionId(sessionId, attempt) {
  return useCurrentInstalledHost
    ? `${sessionId}-${sessionRunNonce}-a${attempt}`
    : withAttemptSessionId(sessionId, attempt);
}

async function withTransientRetry(name, task) {
  let lastError;
  for (let attempt = 1; attempt <= maxTransientRetries + 1; attempt += 1) {
    try {
      return await task(attempt);
    } catch (error) {
      lastError = error;
      if (attempt > maxTransientRetries || !isTransientRateLimitFailure(error)) {
        throw error;
      }
      const delayMs = retryDelayMs * attempt;
      console.warn(`[retry] ${name} hit a transient rate limit, retrying in ${delayMs}ms`);
      await new Promise((resolve) => setTimeout(resolve, delayMs));
    }
  }
  throw lastError;
}

async function appendCase(report, entry) {
  report.cases.push(entry);
  await writeJson(reportPath, report);
}

async function runWebPrompt({
  scenario,
  prompt,
  expectedText,
}) {
  const { chromium } = await loadPlaywright();
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
  });
  const page = await context.newPage();
  try {
    const url = await getDashboardUrl(scenario);
    await page.goto(url, { waitUntil: "networkidle", timeout: 60_000 });
    await page.waitForTimeout(1_000);

    // Some profiles may still show the initial connect card first.
    const connectButton = page.getByRole("button", { name: /连接|connect/i }).first();
    if (await connectButton.count()) {
      await connectButton.click().catch(() => {});
      await page.waitForTimeout(1_000);
    }

    await page.waitForURL(/\/chat/, { timeout: 60_000 });
    const input = page.getByRole("textbox").last();
    await input.fill(prompt);
    await input.press("Enter");
    const deadline = Date.now() + 120_000;
    let pageText = "";
    while (Date.now() < deadline) {
      pageText = await page.locator("main").innerText();
      if (pageText.includes(expectedText)) {
        break;
      }
      await page.waitForTimeout(1000);
    }
    assertIncludes(pageText, expectedText, "web-ui expected text");
    if (!page.url().includes("/chat")) {
      throw new Error(`Expected WebUI to stay on /chat, got ${page.url()}`);
    }
    return {
      url: page.url(),
      excerpt: expectedText,
    };
  } finally {
    await context.close();
    await browser.close();
  }
}

async function main() {
  const report = {
    generatedAt: new Date().toISOString(),
    reportPath,
    ok: false,
    cases: [],
  };

  const needsUninstalledScenario = [
    "cli-uninstalled-zh",
    "web-uninstalled-zh",
  ].some(shouldRunCase);
  const needsInstalledScenario = [
    "cli-installed-zh",
    "cli-installed-en",
    "corner-provider-probe-fail-zh",
    "corner-session-file-locked-zh",
    "corner-responses-boundary-en",
    "web-installed-zh",
  ].some(shouldRunCase);

  const uninstalled = needsUninstalledScenario
    ? await prepareScenario({
        name: "uninstalled",
        port: 18891,
        installPlugin: false,
      })
    : null;
  const installed = !needsInstalledScenario
    ? null
    : useCurrentInstalledHost
      ? {
          root: "current-openclaw-host",
          env: process.env,
        }
      : await prepareScenario({
          name: "installed",
          port: 18892,
          installPlugin: true,
        });

  const uninstalledPromptZh =
    `请阅读 ${docZhPath} ，并按文档规则回答：如果当前宿主还没安装 memory-palace plugin，你会先检查什么，然后给我最短安装链路。不要假设 memory_onboarding_status 已经存在。`;
  const installedPromptZh =
    `请阅读 ${docZhPath} 。然后按这页的规则回答：第一步你会先检查什么？如果 plugin 未安装你会先让我做什么？如果 plugin 已安装你会走哪条链路？不要让我打开 dashboard。`;
  const installedPromptEn =
    `Read ${docEnPath} and answer by the updated rules only: what must you check first, what do you do if the plugin is not installed yet, and what chain do you follow if it is already installed? Do not push me to the dashboard.`;

  if (uninstalled) {
    const uninstalledGateway = await startGateway(uninstalled);
    try {
      if (shouldRunCase("cli-uninstalled-zh")) {
        console.log("[test] cli-uninstalled-zh");
        const uninstalledCli = await withTransientRetry("cli-uninstalled-zh", async (attempt) =>
          await runGatewayAgent({
            scenario: uninstalled,
            sessionId: withAttemptSessionId("doc-link-uninstalled-cli", attempt),
            message: uninstalledPromptZh,
          }),
        );
        assertIncludesAny(uninstalledCli.text, ["先检查", "先查", "先确认"], "uninstalled cli");
        assertIncludes(
          uninstalledCli.text,
          expectedProfileBSetupCommand,
          "uninstalled cli install chain",
        );
        assertUninstalledOnboardingBoundary(uninstalledCli.text, "uninstalled cli tool warning");
        await appendCase(report,
          okCase("cli-uninstalled-zh", {
            excerpt: uninstalledCli.text,
            scenarioRoot: uninstalled.root,
          }),
        );
      }

      if (shouldRunCase("web-uninstalled-zh")) {
        console.log("[test] web-uninstalled-zh");
        const uninstalledWeb = await withTransientRetry("web-uninstalled-zh", async () =>
          await runWebPrompt({
            scenario: uninstalled,
            prompt: uninstalledPromptZh,
            expectedText: "最短安装链路",
          }),
        );
        await appendCase(report, okCase("web-uninstalled-zh", uninstalledWeb));
      }
    } finally {
      await stopGateway(uninstalledGateway);
    }
  }

  if (installed) {
    const installedGateway = useCurrentInstalledHost ? null : await startGateway(installed);
    try {
      if (shouldRunCase("cli-installed-zh")) {
        console.log("[test] cli-installed-zh");
        const installedCliZh = await withTransientRetry("cli-installed-zh", async (attempt) =>
          await runGatewayAgent({
            scenario: installed,
            sessionId: installedSessionId("doc-link-installed-cli-zh", attempt),
            message: installedPromptZh,
          }),
        );
        assertIncludesAny(installedCliZh.text, ["先检查", "先查", "先确认"], "installed cli zh");
        assertIncludes(installedCliZh.text, "plugin", "installed cli zh plugin state");
        assertIncludes(installedCliZh.text, "probe", "installed cli zh probe");
        assertIncludes(installedCliZh.text, "apply", "installed cli zh apply");
        await appendCase(report,
          okCase("cli-installed-zh", {
            excerpt: installedCliZh.text,
            scenarioRoot: installed.root,
          }),
        );
      }

      if (shouldRunCase("cli-installed-en")) {
        console.log("[test] cli-installed-en");
        const installedCliEn = await withTransientRetry("cli-installed-en", async (attempt) =>
          await runGatewayAgent({
            scenario: installed,
            sessionId: installedSessionId("doc-link-installed-cli-en", attempt),
            message: installedPromptEn,
          }),
        );
        assertIncludes(installedCliEn.text, "installed", "installed cli en");
        assertIncludes(installedCliEn.text, "probe", "installed cli en probe");
        assertIncludes(installedCliEn.text, "apply", "installed cli en apply");
        await appendCase(report,
          okCase("cli-installed-en", {
            excerpt: installedCliEn.text,
            scenarioRoot: installed.root,
          }),
        );
      }

      if (shouldRunCase("corner-provider-probe-fail-zh")) {
        console.log("[test] corner-provider-probe-fail-zh");
        const providerFail = await withTransientRetry("corner-provider-probe-fail-zh", async (attempt) =>
          await runGatewayAgent({
            scenario: installed,
            sessionId: installedSessionId("doc-link-provider-fail-zh", attempt),
            message: `请阅读 ${docZhPath} 。按文档规则回答：如果 provider probe fail，你应该怎么向用户解释？不要说 C/D 已经 ready。`,
          }),
        );
        assertIncludes(providerFail.text, "provider", "provider fail explanation");
        assertIncludesAny(
          providerFail.text,
          ["provider-probe", "重跑 probe", "probe 通过后"],
          "provider fail recovery",
        );
        await appendCase(report, okCase("corner-provider-probe-fail-zh", { excerpt: providerFail.text }));
      }

      if (shouldRunCase("corner-session-file-locked-zh")) {
        const sessionLock = await withTransientRetry("corner-session-file-locked-zh", async (attempt) =>
          await runGatewayAgent({
            scenario: installed,
            sessionId: installedSessionId("doc-link-session-lock-zh", attempt),
            message: `请阅读 ${docZhPath} 。按文档规则回答：如果用户在 CLI 里遇到 session file locked，最稳的恢复顺序是什么？`,
          }),
        );
        assertIncludesAny(sessionLock.text, ["新的 session", "新会话", "fresh session"], "session lock order");
        assertIncludesAny(sessionLock.text, ["临时 agent", "temporary agent"], "session lock temp agent");
        await appendCase(report, okCase("corner-session-file-locked-zh", { excerpt: sessionLock.text }));
      }

      if (shouldRunCase("corner-responses-boundary-en")) {
        console.log("[test] corner-responses-boundary-en");
        const responsesBoundary = await withTransientRetry("corner-responses-boundary-en", async (attempt) =>
          await runGatewayAgent({
            scenario: installed,
            sessionId: installedSessionId("doc-link-responses-en", attempt),
            message:
              `Read ${docEnPath} now, using the exact file text rather than previous memory or earlier session context. ` +
              `Answer by the document only: is /responses the final runtime path for this project, or only an accepted input alias? ` +
              `State the real main path too.`,
          }),
        );
        assertIncludesAny(
          responsesBoundary.text,
          ["accepted input alias", "not presented here as the final runtime path", "not the final runtime path"],
          "responses alias",
        );
        assertIncludes(responsesBoundary.text, "/responses", "responses boundary mention");
        await appendCase(report, okCase("corner-responses-boundary-en", { excerpt: responsesBoundary.text }));
      }

      if (shouldRunCase("web-installed-zh")) {
        console.log("[test] web-installed-zh");
        const installedWeb = await withTransientRetry("web-installed-zh", async () =>
          await runWebPrompt({
            scenario: installed,
            prompt: installedPromptZh,
            expectedText: "如果 plugin 已安装",
          }),
        );
        await appendCase(report, okCase("web-installed-zh", installedWeb));
      }
    } finally {
      if (installedGateway) await stopGateway(installedGateway);
    }
  }

  report.ok = true;
  await writeJson(reportPath, report);
  console.log(JSON.stringify(report, null, 2));
}

main().catch(async (error) => {
  const failure = {
    generatedAt: new Date().toISOString(),
    reportPath,
    ok: false,
    error: String(error?.stack || error),
  };
  await writeJson(reportPath, failure);
  console.error(failure.error);
  process.exitCode = 1;
});

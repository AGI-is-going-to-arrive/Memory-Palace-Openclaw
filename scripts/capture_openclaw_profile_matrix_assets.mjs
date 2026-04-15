#!/usr/bin/env node
import { mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { spawn } from "node:child_process";
import {
  loadPlaywright,
  parseJsonOutput,
  PYTHON_BIN,
  prepareScenario,
  repoRoot,
  startGateway,
  stopGateway,
} from "./openclaw_onboarding_doc_test_lib.mjs";

const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const assetsRoot = path.join(repoRoot, "docs", "openclaw-doc", "assets", "real-openclaw-run");
const dashboardCurrentDir = path.join(assetsRoot, "dashboard-current");
const profileMatrixDir = path.join(assetsRoot, "profile-matrix");
const reportRoot = path.join(repoRoot, ".tmp", "webui-profile-matrix");
const acceptanceScript = path.join(scriptDir, "test_replacement_acceptance_webui.mjs");
const controlCaptureScript = path.join(repoRoot, "frontend", "e2e", "capture-openclaw-control-ui.mjs");
const docCaptureScript = path.join(repoRoot, "frontend", "e2e", "capture-openclaw-doc-assets.mjs");
const canonicalProfile = String(process.env.OPENCLAW_CANONICAL_WEBUI_PROFILE || "b")
  .trim()
  .toLowerCase() || "b";
const requestedCases = new Set(
  String(process.env.OPENCLAW_WEBUI_CASES || "a,b,c-default,d-default")
    .split(",")
    .map((item) => item.trim().toLowerCase())
    .filter(Boolean),
);

const scenarioPorts = {
  a: 19350,
  b: 19351,
  "c-default": 19352,
  "d-default": 19353,
  "c-llm": 19354,
};

const profileCases = [
  { id: "a", profile: "a", requireRetrieval: false, requireLlm: false },
  { id: "b", profile: "b", requireRetrieval: false, requireLlm: false },
  { id: "c-default", profile: "c", requireRetrieval: true, requireLlm: false },
  { id: "c-llm", profile: "c", requireRetrieval: true, requireLlm: true },
  { id: "d-default", profile: "d", requireRetrieval: true, requireLlm: true },
];

function firstNonBlank(...values) {
  for (const value of values) {
    const rendered = String(value || "").trim();
    if (rendered) return rendered;
  }
  return "";
}

function requiredRetrievalArgs() {
  const embeddingApiBase = firstNonBlank(
    process.env.OPENCLAW_TEST_EMBEDDING_API_BASE,
    process.env.RETRIEVAL_EMBEDDING_API_BASE,
  );
  const embeddingApiKey = firstNonBlank(
    process.env.OPENCLAW_TEST_EMBEDDING_API_KEY,
    process.env.RETRIEVAL_EMBEDDING_API_KEY,
  );
  const embeddingModel = firstNonBlank(
    process.env.OPENCLAW_TEST_EMBEDDING_MODEL,
    process.env.RETRIEVAL_EMBEDDING_MODEL,
  );
  const embeddingDim = firstNonBlank(
    process.env.OPENCLAW_TEST_EMBEDDING_DIM,
    process.env.RETRIEVAL_EMBEDDING_DIM,
    "1024",
  );
  const rerankerApiBase = firstNonBlank(
    process.env.OPENCLAW_TEST_RERANKER_API_BASE,
    process.env.RETRIEVAL_RERANKER_API_BASE,
  );
  const rerankerApiKey = firstNonBlank(
    process.env.OPENCLAW_TEST_RERANKER_API_KEY,
    process.env.RETRIEVAL_RERANKER_API_KEY,
  );
  const rerankerModel = firstNonBlank(
    process.env.OPENCLAW_TEST_RERANKER_MODEL,
    process.env.RETRIEVAL_RERANKER_MODEL,
  );
  const required = {
    embeddingApiBase,
    embeddingApiKey,
    embeddingModel,
    embeddingDim,
    rerankerApiBase,
    rerankerApiKey,
    rerankerModel,
  };
  for (const [key, value] of Object.entries(required)) {
    if (!value) {
      throw new Error(`Missing retrieval provider input for ${key}`);
    }
  }
  return [
    "--embedding-api-base", embeddingApiBase,
    "--embedding-api-key", embeddingApiKey,
    "--embedding-model", embeddingModel,
    "--embedding-dim", embeddingDim,
    "--reranker-api-base", rerankerApiBase,
    "--reranker-api-key", rerankerApiKey,
    "--reranker-model", rerankerModel,
  ];
}

function requiredLlmArgs() {
  const llmApiBase = firstNonBlank(
    process.env.OPENCLAW_TEST_LLM_API_BASE_PRIMARY,
    process.env.OPENCLAW_TEST_LLM_API_BASE,
    process.env.LLM_API_BASE,
    process.env.OPENAI_API_BASE,
    process.env.OPENAI_BASE_URL,
  );
  const llmApiKey = firstNonBlank(
    process.env.OPENCLAW_TEST_LLM_API_KEY,
    process.env.LLM_API_KEY,
    process.env.OPENAI_API_KEY,
  );
  const llmModel = firstNonBlank(
    process.env.OPENCLAW_TEST_LLM_MODEL,
    process.env.LLM_MODEL_NAME,
    process.env.OPENAI_MODEL,
  );
  const required = { llmApiBase, llmApiKey, llmModel };
  for (const [key, value] of Object.entries(required)) {
    if (!value) {
      throw new Error(`Missing LLM provider input for ${key}`);
    }
  }
  return [
    "--llm-api-base", llmApiBase,
    "--llm-api-key", llmApiKey,
    "--llm-model", llmModel,
  ];
}

function buildSetupArgs(caseConfig) {
  const args = [];
  if (caseConfig.requireRetrieval) {
    args.push(...requiredRetrievalArgs());
  }
  if (caseConfig.requireLlm) {
    args.push(...requiredLlmArgs());
  }
  return args;
}

async function runNode(command, args, { env = {}, timeoutMs = 240_000 } = {}) {
  return await new Promise((resolve, reject) => {
    const child = spawn(command, args, {
      cwd: repoRoot,
      env: { ...process.env, ...env },
      stdio: ["ignore", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    let settled = false;
    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      child.kill("SIGKILL");
      reject(
        new Error(
          `${command} ${args.join(" ")} timed out after ${timeoutMs}ms\nSTDOUT:\n${stdout}\nSTDERR:\n${stderr}`,
        ),
      );
    }, timeoutMs);
    child.stdout.on("data", (chunk) => {
      stdout += chunk.toString();
    });
    child.stderr.on("data", (chunk) => {
      stderr += chunk.toString();
    });
    child.on("close", (code) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      if ((code ?? 0) !== 0) {
        reject(
          new Error(
            `${command} ${args.join(" ")} failed with code ${code}\nSTDOUT:\n${stdout}\nSTDERR:\n${stderr}`,
          ),
        );
        return;
      }
      resolve({ stdout, stderr });
    });
    child.on("error", (error) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      reject(error);
    });
  });
}

async function loadEnvFile(filePath) {
  try {
    const raw = await readFile(filePath, "utf8");
    const pairs = new Map();
    for (const line of raw.split(/\r?\n/)) {
      const trimmed = line.trim();
      if (!trimmed || trimmed.startsWith("#")) continue;
      const index = trimmed.indexOf("=");
      if (index <= 0) continue;
      const key = trimmed.slice(0, index).trim();
      const value = trimmed.slice(index + 1).trim();
      pairs.set(key, value);
    }
    return Object.fromEntries(pairs);
  } catch {
    return {};
  }
}

async function runRepoPython(args, { env = {}, timeoutMs = 240_000 } = {}) {
  const result = await runNode(PYTHON_BIN, args, {
    env,
    timeoutMs,
  });
  const rawOutput = result.stdout.trim() ? result.stdout : result.stderr;
  if (!rawOutput.trim()) {
    throw new Error(`No JSON output from ${args.join(" ")}`);
  }
  return parseJsonOutput(rawOutput);
}

function sanitizedDashboardUrl(port) {
  return `http://127.0.0.1:${port}/#token=status-probe-local-only`;
}

async function navigateAndCapture(page, baseUrl, route, outputPath) {
  const url = new URL(route, baseUrl.replace(/#.*$/, "")).toString();
  await page.goto(url, { waitUntil: "networkidle", timeout: 90_000 });
  await page.waitForTimeout(1_200);
  await page.evaluate(() => {
    const replacements = [
      [/\/Users\/[^/\s]+/g, "/Users/<redacted>"],
      [/\/home\/[^/\s]+/g, "/home/<redacted>"],
      [/\/private\/var\/folders\/[^\s)]+/g, "/private/var/folders/<redacted>"],
      [/\/var\/folders\/[^\s)]+/g, "/var/folders/<redacted>"],
      [/[A-Za-z]:\\\\Users\\\\[^\\\\\s]+/g, "C:\\Users\\<redacted>"],
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
  await page.screenshot({ path: outputPath, fullPage: false });
  return { route, outputPath };
}

async function findChatInput(page) {
  const locator = page.locator(
    'textarea, input[placeholder*="Message"], input[placeholder*="message"], [contenteditable="true"]',
  ).last();
  await locator.waitFor({ timeout: 30_000 });
  return locator;
}

async function countOccurrences(page, text) {
  return page.evaluate((needle) => {
    const body = document.body.innerText || "";
    return needle ? body.split(needle).length - 1 : 0;
  }, text);
}

async function runProfileChatScenario(page, baseUrl, caseConfig, screenshotDir) {
  const marker = `matrix-${caseConfig.id}`;
  const confirm = `stored profile ${caseConfig.id}`;
  const chatUrl = new URL("/chat", baseUrl.replace(/#.*$/, ""));
  chatUrl.searchParams.set("session", `agent:main:profile-${caseConfig.id}`);
  await page.goto(chatUrl.toString(), { waitUntil: "networkidle", timeout: 90_000 });
  await page.waitForTimeout(1_200);

  const connectButton = page.getByRole("button", { name: /连接|connect/i }).first();
  if (await connectButton.count()) {
    await connectButton.click().catch(() => {});
    await page.waitForTimeout(1_000);
  }

  const input = await findChatInput(page);
  const confirmBefore = await countOccurrences(page, confirm);
  await input.click();
  await input.fill(
    `Please remember this durable note for profile ${caseConfig.id}: project marker is ${marker}. Reply only "${confirm}".`,
  );
  await page.keyboard.press("Enter");
  await page.waitForFunction(
    ({ expected, baseline }) => {
      const body = document.body.innerText || "";
      return body.split(expected).length - 1 > baseline;
    },
    { expected: confirm, baseline: confirmBefore },
    { timeout: 90_000 },
  );

  await input.click();
  await input.fill(`What is the project marker for profile ${caseConfig.id}? Reply only with the marker.`);
  await page.keyboard.press("Enter");
  await page.waitForFunction(
    (expected) => (document.body.innerText || "").includes(expected),
    marker,
    { timeout: 90_000 },
  );

  const screenshotPath = path.join(screenshotDir, "chat.png");
  await page.screenshot({ path: screenshotPath, fullPage: false });
  return { marker, confirm, screenshotPath };
}

async function runAcceptanceCase(caseConfig) {
  const screenshotDir = path.join(profileMatrixDir, caseConfig.id);
  const reportPath = path.join(reportRoot, `${caseConfig.id}.json`);
  const port = scenarioPorts[caseConfig.id];
  const setupArgs = buildSetupArgs(caseConfig);
  let browser = null;
  const scenario = await prepareScenario({
    name: `profile-matrix-${caseConfig.id}`,
    port,
    installPlugin: true,
    profile: caseConfig.profile,
    setupArgs,
  });
  const gateway = await startGateway(scenario);
  const dashboardUrl = sanitizedDashboardUrl(port);
  await mkdir(screenshotDir, { recursive: true });
  await mkdir(reportRoot, { recursive: true });
  try {
    const playwright = await loadPlaywright();
    browser = await playwright.chromium.launch({ headless: true });
    const context = await browser.newContext({
      viewport: { width: 1600, height: 1200 },
      locale: "en-US",
    });
    const page = await context.newPage();

    const overview = await navigateAndCapture(page, dashboardUrl, "/", path.join(screenshotDir, "overview.png"));
    const skills = await navigateAndCapture(page, dashboardUrl, "/skills", path.join(screenshotDir, "skills.png"));
    const agents = await navigateAndCapture(page, dashboardUrl, "/agents", path.join(screenshotDir, "agents.png"));
    const settings = await navigateAndCapture(page, dashboardUrl, "/settings", path.join(screenshotDir, "settings.png"));
    const chat = await runProfileChatScenario(page, dashboardUrl, caseConfig, screenshotDir);
    await context.close().catch(() => {});

    const payload = {
      generatedAt: new Date().toISOString(),
      profile: caseConfig.id,
      requestedProfile: caseConfig.profile,
      scenarioRoot: scenario.root,
      configPath: scenario.configPath,
      setupRoot: scenario.setupRoot,
      dashboardUrl: "<isolated-scenario>",
      screenshots: {
        overview,
        skills,
        agents,
        settings,
        chat,
      },
      ok: true,
    };
    await writeFile(reportPath, `${JSON.stringify(payload, null, 2)}\n`, "utf8");

    return {
      profile: caseConfig.id,
      dashboardUrl: "<isolated-scenario>",
      reportPath,
      screenshotDir,
      canonicalCapture: caseConfig.id === canonicalProfile,
      scenarioRoot: scenario.root,
      configPath: scenario.configPath,
      setupRoot: scenario.setupRoot,
    };
  } finally {
    await browser?.close().catch(() => {});
    await stopGateway(gateway).catch(() => {});
  }
}

async function runCanonicalCaptures(caseConfig) {
  const port = scenarioPorts[caseConfig.id];
  const dashboardPort = port + 100;
  const backendApiPort = port + 200;
  const setupArgs = buildSetupArgs(caseConfig);
  const scenario = await prepareScenario({
    name: `profile-canonical-${caseConfig.id}`,
    port,
    installPlugin: true,
    profile: caseConfig.profile,
    setupArgs,
  });
  const gateway = await startGateway(scenario);
  const controlUiUrl = sanitizedDashboardUrl(port);
  const warnings = [];
  let dashboardReport = null;
  try {
    const runtimeEnv = await loadEnvFile(path.join(scenario.setupRoot, "runtime.env"));
    const mcpApiKey = runtimeEnv.MCP_API_KEY || "";
    try {
      await runNode("node", [controlCaptureScript], {
        env: {
          OPENCLAW_CONTROL_UI_URL: controlUiUrl,
          OPENCLAW_DOC_CAPTURE_OUTPUT_DIR: assetsRoot,
          OPENCLAW_CONTROL_UI_AGENTS_SCREENSHOT: "dashboard-current/openclaw-webui-agents.png",
        },
        timeoutMs: 240_000,
      });
    } catch (error) {
      warnings.push(`controlCapture failed: ${String(error?.message || error)}`);
    }
    try {
      dashboardReport = await runRepoPython(
        [
          path.join(repoRoot, "scripts", "openclaw_memory_palace.py"),
          "dashboard",
          "start",
          "--setup-root",
          scenario.setupRoot,
          "--dashboard-port",
          String(dashboardPort),
          "--backend-api-port",
          String(backendApiPort),
          "--json",
        ],
        {
          env: scenario.env,
          timeoutMs: 240_000,
        },
      );
      const dashboardCaptureUrl =
        dashboardReport?.dashboard?.url && String(dashboardReport.dashboard.url).trim();
      if (!dashboardCaptureUrl) {
        throw new Error(`Dashboard start did not return a usable url: ${JSON.stringify(dashboardReport)}`);
      }
      for (const localizedCapture of [
        { locale: "en", suffix: "en" },
        { locale: "zh-CN", suffix: "zh" },
      ]) {
        await runNode("node", [docCaptureScript], {
          env: {
            OPENCLAW_DOC_CAPTURE_BASE_URL: dashboardCaptureUrl,
            OPENCLAW_DOC_CAPTURE_API_KEY: mcpApiKey,
            OPENCLAW_DOC_CAPTURE_OUTPUT_DIR: dashboardCurrentDir,
            OPENCLAW_DOC_CAPTURE_VISUAL_ASSETS_DIR: assetsRoot,
            OPENCLAW_DOC_CAPTURE_LOCALE: localizedCapture.locale,
            OPENCLAW_DOC_CAPTURE_LOCALE_SUFFIX: localizedCapture.suffix,
            OPENCLAW_DOC_CAPTURE_ALLOW_FIXTURE_WRITE: "true",
            OPENCLAW_DOC_CAPTURE_SETUP_ROOT: scenario.setupRoot,
            OPENCLAW_DOC_CAPTURE_RUNTIME_ENV_FILE: path.join(scenario.setupRoot, "runtime.env"),
            OPENCLAW_DOC_CAPTURE_RUNTIME_PYTHON: path.join(scenario.setupRoot, "runtime", "bin", "python"),
          },
          timeoutMs: 240_000,
        });
      }
    } catch (error) {
      warnings.push(`docCapture failed: ${String(error?.message || error)}`);
    }
    return {
      profile: caseConfig.id,
      scenarioRoot: scenario.root,
      configPath: scenario.configPath,
      setupRoot: scenario.setupRoot,
      dashboardUrl: "<isolated-scenario>",
      assetsRoot,
      dashboardCurrentDir,
      warnings,
    };
  } finally {
    try {
      await runRepoPython(
        [
          path.join(repoRoot, "scripts", "openclaw_memory_palace.py"),
          "dashboard",
          "stop",
          "--setup-root",
          scenario.setupRoot,
          "--json",
        ],
        {
          env: scenario.env,
          timeoutMs: 120_000,
        },
      );
    } catch (error) {
      warnings.push(`dashboardStop failed: ${String(error?.message || error)}`);
    }
    await stopGateway(gateway).catch(() => {});
  }
}

async function main() {
  await mkdir(assetsRoot, { recursive: true });
  await mkdir(profileMatrixDir, { recursive: true });
  await mkdir(reportRoot, { recursive: true });

  const results = [];
  for (const caseConfig of profileCases) {
    if (!requestedCases.has(caseConfig.id)) {
      continue;
    }
    console.log(`[profile-webui] ${caseConfig.id}`);
    results.push(await runAcceptanceCase(caseConfig));
  }

  let canonicalResult = null;
  const canonicalCase = profileCases.find((item) => item.id === canonicalProfile);
  if (canonicalCase) {
    console.log(`[profile-webui] canonical ${canonicalCase.id}`);
    canonicalResult = await runCanonicalCaptures(canonicalCase);
  }

  const payload = {
    generatedAt: new Date().toISOString(),
    canonicalProfile,
    results,
    canonicalResult,
  };
  await writeFile(
    path.join(reportRoot, "manifest.json"),
    `${JSON.stringify(payload, null, 2)}\n`,
    "utf8",
  );
  console.log(JSON.stringify(payload, null, 2));
}

main().catch((error) => {
  console.error(error instanceof Error ? error.stack || error.message : String(error));
  process.exitCode = 1;
});

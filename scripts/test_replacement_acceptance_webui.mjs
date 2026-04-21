#!/usr/bin/env node
/**
 * Replacement Acceptance WebUI Test
 *
 * Verifies that the memory-palace plugin is visible and functional in the
 * OpenClaw WebUI across six verification points:
 *   V1 – Plugin Status Visible in Dashboard
 *   V2 – Memory Write + Recall in Chat
 *   V3 – Memory System Integration Evidence
 *   V4 – Guarded Write -> Confirm -> Force Save in Chat
 *   V5 – Chinese Minimal Confirmation in Chat
 *   V6 – English Minimal Confirmation in Chat
 *
 * Usage:
 *   node scripts/test_replacement_acceptance_webui.mjs
 *
 * Environment variables:
 *   OPENCLAW_ACL_CONTROL_UI_URL  – dashboard base URL (default: http://127.0.0.1:48231/#token=status-probe-local-only)
 *   OPENCLAW_ONBOARDING_USE_CURRENT_HOST – "true" to skip scenario setup and use the running host
 *   OPENCLAW_ACCEPTANCE_DASHBOARD_URL_SOURCE – "cli" (default) to resolve isolated dashboard URLs
 *                                  through `openclaw dashboard --no-open`, or "scenario" to
 *                                  fall back to the known scenario port
 *   OPENCLAW_ACCEPTANCE_STRICT_UI – "true" to require UI-visible evidence for V2/V4/V5/V6
 *                                  in current-host Profile C/D runs, and to reject visible raw
 *                                  memory tags / metadata noise on chat verifications (default: off)
 */
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import {
  getDashboardUrl,
  loadPlaywright,
  OPENCLAW_BIN,
  parseJsonOutput,
  prepareScenario,
  repoRoot,
  runCommand,
  startGateway,
  stopGateway,
  writeJson,
} from "./openclaw_onboarding_doc_test_lib.mjs";

// ---------------------------------------------------------------------------
// Paths
// ---------------------------------------------------------------------------
const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const requestedProfile = String(process.env.OPENCLAW_PROFILE || "b").trim().toLowerCase() || "b";
const screenshotDir = String(process.env.OPENCLAW_SCREENSHOT_DIR || "").trim()
  || path.join(repoRoot, ".tmp", "replacement-acceptance", requestedProfile);
const reportPath = String(process.env.OPENCLAW_REPORT_PATH || "").trim()
  || path.join(screenshotDir, "webui_report.json");

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------
const CONTROL_UI_URL =
  process.env.OPENCLAW_ACL_CONTROL_UI_URL ||
  "http://127.0.0.1:48231/#token=status-probe-local-only";
const useCurrentHost =
  String(process.env.OPENCLAW_ONBOARDING_USE_CURRENT_HOST || "")
    .trim()
    .toLowerCase() === "true";
const strictUiRequested =
  String(process.env.OPENCLAW_ACCEPTANCE_STRICT_UI || "")
    .trim()
    .toLowerCase() === "true";
const dashboardUrlSource =
  String(process.env.OPENCLAW_ACCEPTANCE_DASHBOARD_URL_SOURCE || "").trim().toLowerCase()
  || "cli";
const includeHighValueShortSession =
  String(process.env.OPENCLAW_ACCEPTANCE_INCLUDE_HIGH_VALUE_SHORT_SESSION || "")
    .trim()
    .toLowerCase() === "true";
const forceIsolated =
  String(process.env.ACCEPTANCE_FORCE_ISOLATED || "")
    .trim()
    .toLowerCase() === "true";
const VERIFICATION_TIMEOUT_MS = requestedProfile === "d" ? 90_000 : 60_000;
const scenarioPortValue = Number.parseInt(process.env.OPENCLAW_SCENARIO_PORT || "18951", 10);
const SCENARIO_PORT = Number.isFinite(scenarioPortValue) ? scenarioPortValue : 18951;
const scenarioName = String(process.env.OPENCLAW_SCENARIO_NAME || "").trim()
  || `replacement-acceptance-${requestedProfile}`;
const acceptanceMode = String(process.env.OPENCLAW_ACCEPTANCE_MODE || "acl").trim().toLowerCase() || "acl";
const profileMatrixMode = acceptanceMode === "profile-matrix";
const acceptanceAgentKey = profileMatrixMode || useCurrentHost ? "main" : "alpha";
const strictCurrentHostUiMode =
  strictUiRequested && useCurrentHost && ["c", "d"].includes(requestedProfile);
const acceptanceRunId = String(process.env.OPENCLAW_ACCEPTANCE_RUN_ID || "").trim()
  || `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
const rawSetupArgs = (() => {
  const raw = String(process.env.OPENCLAW_SETUP_ARGS_JSON || "").trim();
  if (!raw) return [];
  try {
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed.map((item) => String(item)) : [];
  } catch {
    return [];
  }
})();

function normalizeSetupArgs(args) {
  if (!Array.isArray(args) || args.length === 0) return [];

  const renamedFlags = new Map([
    ["--embedding-base-url", "--embedding-api-base"],
    ["--reranker-base-url", "--reranker-api-base"],
    ["--llm-base-url", "--llm-api-base"],
  ]);
  const dropFlags = new Set([
    "--embedding-ctx",
    "--llm-fallback-base-url",
    "--llm-fallback-api-key",
    "--llm-fallback-model",
  ]);
  const valueFlags = new Set([
    "--embedding-api-base",
    "--embedding-api-key",
    "--embedding-model",
    "--embedding-dim",
    "--reranker-api-base",
    "--reranker-api-key",
    "--reranker-model",
    "--llm-api-base",
    "--llm-api-key",
    "--llm-model",
    "--write-guard-llm-api-base",
    "--write-guard-llm-api-key",
    "--write-guard-llm-model",
    "--compact-gist-llm-api-base",
    "--compact-gist-llm-api-key",
    "--compact-gist-llm-model",
  ]);

  const normalized = [];
  const seen = new Map();

  for (let index = 0; index < args.length; index += 1) {
    const rawFlag = String(args[index] || "").trim();
    if (!rawFlag) continue;
    const flag = renamedFlags.get(rawFlag) || rawFlag;
    const nextValue = index + 1 < args.length ? String(args[index + 1]) : "";
    if (dropFlags.has(flag)) {
      index += 1;
      continue;
    }
    if (!valueFlags.has(flag)) {
      normalized.push(flag);
      continue;
    }
    if (index + 1 >= args.length) {
      continue;
    }
    normalized.push(flag, nextValue);
    seen.set(flag, nextValue);
    index += 1;
  }

  const llmDefaults = [
    ["--write-guard-llm-api-base", seen.get("--llm-api-base")],
    ["--write-guard-llm-api-key", seen.get("--llm-api-key")],
    ["--write-guard-llm-model", seen.get("--llm-model")],
    ["--compact-gist-llm-api-base", seen.get("--llm-api-base")],
    ["--compact-gist-llm-api-key", seen.get("--llm-api-key")],
    ["--compact-gist-llm-model", seen.get("--llm-model")],
  ];
  for (const [flag, value] of llmDefaults) {
    if (seen.has(flag) || !value) continue;
    normalized.push(flag, value);
    seen.set(flag, value);
  }

  return normalized;
}

const setupArgs = normalizeSetupArgs(rawSetupArgs);
const verificationCatalog = [
  { id: "V1", name: "Plugin Status Visible" },
  { id: "V2", name: "Memory Write + Recall in Chat" },
  { id: "V3", name: "Memory System Integration Evidence" },
  { id: "V4", name: "Guarded Write Confirm + Force Save" },
  { id: "V5", name: "Chinese Minimal Confirmation" },
  { id: "V6", name: "English Minimal Confirmation" },
  ...(includeHighValueShortSession
    ? [{ id: "V7", name: "Short High-Value Session Recall" }]
    : []),
];
const totalVerifications = verificationCatalog.length;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Build a structured verification result. */
function buildResult({
  id,
  name,
  setup,
  action,
  expected,
  actual,
  pass_fail,
  screenshot_path,
  selectors_checked,
}) {
  return {
    id,
    name,
    setup,
    action,
    expected,
    actual: actual ?? "",
    pass_fail: pass_fail ?? "FAIL",
    screenshot_path: screenshot_path ?? "",
    selectors_checked: selectors_checked ?? [],
  };
}

function summarizeReport(verifications = []) {
  const summary = { total: totalVerifications, pass: 0, fail: 0, skip: 0 };
  for (const verification of verifications) {
    if (verification?.pass_fail === "PASS") summary.pass += 1;
    else if (verification?.pass_fail === "SKIP") summary.skip += 1;
    else if (verification) summary.fail += 1;
  }
  return summary;
}

async function persistReport(report, extra = {}) {
  const next = {
    ...report,
    ...extra,
  };
  next.summary = summarizeReport(next.verifications || []);
  if (typeof next.ok !== "boolean") {
    next.ok = false;
  }
  await mkdir(screenshotDir, { recursive: true }).catch(() => {});
  await writeJson(reportPath, next);
  Object.assign(report, next);
}

async function persistProgressReport(report) {
  report.summary.pass = 0;
  report.summary.fail = 0;
  report.summary.skip = 0;
  report.summary.total = totalVerifications;
  for (const verification of report.verifications) {
    if (verification.pass_fail === "PASS") report.summary.pass += 1;
    else if (verification.pass_fail === "SKIP") report.summary.skip += 1;
    else report.summary.fail += 1;
  }
  report.ok = report.summary.fail === 0 && report.verifications.length >= 6;
  await writeJson(reportPath, report);
}

function shouldEnforceStrictUiEvidence(verificationId) {
  return strictCurrentHostUiMode && ["V2", "V4", "V5", "V6", "V7"].includes(verificationId);
}

function shouldRejectVisibleUiNoise(verificationId) {
  return strictUiRequested && ["V2", "V4", "V5", "V6"].includes(verificationId);
}

function shouldAllowCliDurableFallback(verificationId) {
  return !shouldEnforceStrictUiEvidence(verificationId);
}

async function scanUiRoutesForMarker(page, baseUrl, chatUrl, marker, selectorsChecked, label) {
  const evidenceRoutes = [
    ["chat", chatUrl.toString()],
    ["settings", new URL("/settings", baseUrl.replace(/#.*$/, "")).toString()],
    ["root", new URL("/", baseUrl.replace(/#.*$/, "")).toString()],
  ];
  selectorsChecked.push(`${label}_route_scan:true`);
  for (const [routeLabel, targetUrl] of evidenceRoutes) {
    selectorsChecked.push(`${label}_route:${routeLabel}`);
    await page.goto(targetUrl, {
      waitUntil: "networkidle",
      timeout: VERIFICATION_TIMEOUT_MS,
    });
    await page.waitForTimeout(1_200);
    const routeText = await page.locator("body").innerText();
    if (routeText.includes(marker)) {
      return `Marker "${marker}" became visible on ${routeLabel} route`;
    }
  }
  return "";
}

function buildAcceptanceMemorySpec() {
  if (profileMatrixMode) {
    const marker = `matrix-${requestedProfile}-${acceptanceRunId}`;
    const confirmText = "Stored.";
    return {
      confirmText,
      marker,
      writePrompt:
        `Please remember this durable workflow preference for profile ${requestedProfile}: ` +
        `the default workflow marker is ${marker}. ` +
        "Treat it as the default marker for later turns. " +
        `Reply only "${confirmText}".`,
      recallPrompt:
        `Do you still recall the default workflow marker for profile ${requestedProfile}? ` +
        "Reply with the marker only.",
      expectedScope: `profile ${requestedProfile}`,
    };
  }

  const marker = `alpha-marker-${acceptanceRunId}`;
  const confirmText = "记住了。";
  return {
    confirmText,
    marker,
    writePrompt:
      `请记住这个长期协作 workflow：以后默认按这个 workflow 协作：先列清单，再实现，最后补测试。运行标记：${marker}。` +
      `收到后只回复“${confirmText}”。`,
    recallPrompt: "还记得刚才那个默认 workflow 吗？请完整重复并保留运行标记，只输出内容。",
    expectedScope: useCurrentHost ? "current-host session" : "alpha",
  };
}

function buildForceWriteSpec() {
  if (profileMatrixMode) {
    const namespace = `force-lane-${requestedProfile}`;
    const baseMarker = `matrix-force-base-${requestedProfile}-${acceptanceRunId}`;
    const variantMarker = `matrix-force-variant-${requestedProfile}-${acceptanceRunId}`;
    return {
      namespace,
      baseMarker,
      variantMarker,
      pausedText: "Paused.",
      storedText: "Stored.",
      baseWritePrompt:
        `Please remember this durable incident-response workflow baseline for profile ${requestedProfile}: ` +
        `namespace ${namespace}; sequence pause release, collect rollback evidence, then notify the owner; ` +
        `baseline marker ${baseMarker}. Reply only "Stored.".`,
      blockedWritePrompt:
        `Please remember this near-duplicate durable incident-response workflow variant for profile ${requestedProfile}: ` +
        `namespace ${namespace}; sequence pause release, collect rollback evidence, then notify the owner; ` +
        `baseline marker ${baseMarker}; escalation marker ${variantMarker}. ` +
        `If it is not stored as a separate durable memory, reply with one very short sentence that includes "Paused.".`,
      confirmForcePrompt:
        `I confirm that I still want the separate durable memory saved for the incident-response variant in namespace ${namespace} ` +
        `with escalation marker ${variantMarker}, even if it is close to an existing memory. Reply only "Stored.".`,
      recallPrompt:
        `What is the escalation marker for the confirmed incident-response variant in namespace ${namespace} for profile ${requestedProfile}? ` +
        "Reply with the marker only.",
      expectedScope: `profile ${requestedProfile}`,
    };
  }

  const namespace = "force-lane";
  const baseMarker = `force-base-${acceptanceRunId}`;
  const variantMarker = `force-variant-${acceptanceRunId}`;
  return {
    namespace,
    baseMarker,
    variantMarker,
    pausedText: "已暂停。",
    storedText: "记住了。",
    baseWritePrompt:
      `请记住这个长期 incident-response workflow 基线：命名空间是 ${namespace}。流程是先暂停发布，再收集回滚证据，最后通知负责人。基础标记：${baseMarker}。` +
      `收到后只回复“记住了。”。`,
    blockedWritePrompt:
      `请记住这个非常接近已有记忆的 incident-response workflow 变体：命名空间是 ${namespace}。流程仍然是先暂停发布，再收集回滚证据，最后通知负责人。` +
      `基础标记：${baseMarker}。升级标记：${variantMarker}。` +
      `如果它没有被单独存入，请用一句很短的话说明，并包含“已暂停。”。`,
    confirmForcePrompt:
      `我确认：即使它和已有长期记忆很接近，也要把 ${namespace} 命名空间里包含升级标记 ${variantMarker} 的 incident-response workflow 变体单独长期记住。` +
      `只回复“记住了。”。`,
    recallPrompt:
      `现在只回答刚才那个已确认 ${namespace} incident-response 变体的升级标记，只输出标记本身。`,
    expectedScope: useCurrentHost ? "current-host session" : "alpha",
  };
}

const acceptanceMemorySpec = buildAcceptanceMemorySpec();
const acceptanceForceWriteSpec = buildForceWriteSpec();
const acceptanceChineseConfirmSpec = {
  confirmText: "记住了。",
  marker: `zh-confirm-${requestedProfile}-${acceptanceRunId}`,
  writePrompt:
    `请记住这个新的长期事实：以后如果我说“中文确认代号”，默认就是 zh-confirm-${requestedProfile}-${acceptanceRunId}。` +
    "这不是已有偏好或 workflow 的改写，而是一条新的长期事实。" +
    `收到后只回复“记住了。”。`,
  recallPrompt:
    "还记得刚才那条“中文确认代号”吗？只输出完整代号，不要解释。",
};
const acceptanceEnglishConfirmSpec = {
  confirmText: "Stored.",
  marker: `en-confirm-${requestedProfile}-${acceptanceRunId}`,
  writePrompt:
    `Please remember this new long-term fact: if I say "English confirmation code", the default code is en-confirm-${requestedProfile}-${acceptanceRunId}. ` +
    "This is a new durable fact, not a rewrite of an existing preference or workflow. " +
    'Reply only "Stored.".',
  recallPrompt:
    'Do you still remember the "English confirmation code"? Reply with the full code only.',
};
const acceptanceShortHighValueSpec = profileMatrixMode
  ? {
      confirmText: "Stored.",
      marker: `hv-short-${requestedProfile}-${acceptanceRunId}`,
      writePrompt:
        `Please remember this short high-value workflow preference for profile ${requestedProfile}: ` +
        `the default workflow marker is hv-short-${requestedProfile}-${acceptanceRunId}. ` +
        'Reply only "Stored.".',
      recallPrompt:
        `Do you still recall the short high-value workflow marker for profile ${requestedProfile}? ` +
        "Reply with the marker only.",
    }
  : {
      confirmText: "记住了。",
      marker: `hv-short-${requestedProfile}-${acceptanceRunId}`,
      writePrompt:
        `请记住这个短会话高价值 workflow 偏好：默认 workflow 标记是 hv-short-${requestedProfile}-${acceptanceRunId}。` +
        `收到后只回复“记住了。”。`,
      recallPrompt: "现在只输出刚才那个短会话 workflow 标记，不要解释。",
    };
const acceptanceChatSession = profileMatrixMode
  ? `agent:main:profile-${requestedProfile}-${acceptanceRunId}`
  : `agent:${acceptanceAgentKey}:acceptance-${acceptanceRunId}`;
const acceptanceForceChatSession = profileMatrixMode
  ? `agent:main:force-${requestedProfile}-${acceptanceRunId}`
  : `agent:${acceptanceAgentKey}:force-${acceptanceRunId}`;
const acceptanceChineseChatSession = profileMatrixMode
  ? `agent:main:zh-confirm-${requestedProfile}-${acceptanceRunId}`
  : `agent:${acceptanceAgentKey}:zh-confirm-${acceptanceRunId}`;
const acceptanceEnglishChatSession = profileMatrixMode
  ? `agent:main:en-confirm-${requestedProfile}-${acceptanceRunId}`
  : `agent:${acceptanceAgentKey}:en-confirm-${acceptanceRunId}`;

/** Count occurrences of `needle` inside the page body text. */
async function countOccurrences(page, needle) {
  return page.evaluate((n) => {
    const body = document.body.innerText || "";
    if (!n) return 0;
    return body.split(n).length - 1;
  }, needle);
}

async function waitForMainTextToSettle(page, timeoutMs = 8_000, pollMs = 500, stablePolls = 3) {
  let previousText = await page.locator("main").innerText().catch(() => "");
  let stableCount = 0;
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    await page.waitForTimeout(pollMs);
    const currentText = await page.locator("main").innerText().catch(() => "");
    if (currentText === previousText) {
      stableCount += 1;
      if (stableCount >= stablePolls) {
        return true;
      }
      continue;
    }
    previousText = currentText;
    stableCount = 0;
  }
  return false;
}

const VISIBLE_UI_NOISE_MARKERS = [
  "memory-palace-profile",
  "memory-palace-recall",
  "memory-palace-reflection",
  "memory-palace-host-bridge",
  "untrusted metadata",
  "session_id",
  "session_key",
  "agent_id",
  "captured_at",
  "auto captured memory",
  "## content",
  "<<sender>>",
  "&lt;&lt;sender&gt;&gt;",
  "```json",
];

function collectVisibleUiNoiseHits(text) {
  const normalized = String(text || "").toLowerCase();
  if (!normalized) {
    return [];
  }
  return VISIBLE_UI_NOISE_MARKERS.filter((marker) => normalized.includes(marker));
}

async function scanVisibleUiNoiseOnChat(page, chatUrl, selectorsChecked, label) {
  selectorsChecked.push(`${label}_ui_noise_scan:true`);
  await page.goto(chatUrl.toString(), {
    waitUntil: "networkidle",
    timeout: VERIFICATION_TIMEOUT_MS,
  });
  await page.waitForTimeout(1_500);
  const settled = await waitForMainTextToSettle(page);
  selectorsChecked.push(`${label}_ui_noise_settled:${settled}`);
  const mainText = await page.locator("main").innerText().catch(() => "");
  const hits = collectVisibleUiNoiseHits(mainText);
  selectorsChecked.push(`${label}_ui_noise_free:${hits.length === 0}`);
  if (hits.length > 0) {
    selectorsChecked.push(`${label}_ui_noise_hits:${hits.join("|")}`);
  }
  return {
    ok: hits.length === 0,
    hits,
  };
}

async function clickConnectIfPresent(page) {
  const connectButton = page.getByRole("button", { name: /连接|connect/i }).first();
  if (await connectButton.count()) {
    await connectButton.click().catch(() => {});
    await page.waitForTimeout(1_000);
  }
}

async function stabilizeChatRouteAfterConnect(page, chatUrl, selectorsChecked, reason) {
  selectorsChecked.push(`chat_route_revisit:${reason}`);
  await page.goto(chatUrl.toString(), {
    waitUntil: "networkidle",
    timeout: VERIFICATION_TIMEOUT_MS,
  });
  await page.waitForTimeout(1_400);
  await clickConnectIfPresent(page);
}

function getChatInputLocator(page) {
  return page.locator(
    'textarea, input[placeholder*="Message"], [contenteditable="true"]',
  ).last();
}

/** Attempt to detect whether the gateway/dashboard is reachable. */
async function isGatewayReachable(baseUrl) {
  try {
    const clean = baseUrl.replace(/#.*$/, "");
    const resp = await fetch(clean, { signal: AbortSignal.timeout(5_000) });
    return resp.ok || resp.status === 304;
  } catch {
    return false;
  }
}

async function runAcceptanceCliJson(scenario, args, timeoutMs = 120_000) {
  const env = {
    ...process.env,
    ...(scenario?.env ?? {}),
    NO_COLOR: "1",
    FORCE_COLOR: "0",
  };
  const result = await runCommand(OPENCLAW_BIN, args, {
    env,
    timeoutMs,
    allowFailure: true,
  });
  const raw = (result.stdout || result.stderr || "").trim();
  let payload = null;
  if (raw) {
    try {
      payload = parseJsonOutput(raw);
    } catch {
      payload = null;
    }
  }
  return { ...result, payload };
}

function stringifyAcceptancePayload(value) {
  try {
    return JSON.stringify(value ?? {});
  } catch {
    return "";
  }
}

function expectedAcceptanceAgentId() {
  return profileMatrixMode || useCurrentHost ? "main" : acceptanceAgentKey;
}

function normalizeCliCaptureUri(rawUri) {
  const rendered = String(rawUri || "").trim();
  if (!rendered) return null;
  const expectedPrefix = `core://agents/${expectedAcceptanceAgentId()}/`;
  return rendered.startsWith(expectedPrefix) ? rendered : null;
}

function parseAcceptanceTimestampMs(rawTimestamp) {
  const parsed = Date.parse(String(rawTimestamp || ""));
  return Number.isFinite(parsed) ? parsed : null;
}

function buildMarkerNeedles(marker) {
  const rendered = String(marker || "").trim();
  if (!rendered) {
    return [];
  }
  const needles = [rendered];
  // Status/runtime details can truncate long markers near the tail. Keep a
  // long-enough prefix so clean-lane unique run ids still disambiguate.
  if (rendered.length > 30) {
    needles.push(rendered.slice(0, 30));
  }
  return needles;
}

function collectRecentRuntimeMarkerEntries(runtimeState, marker) {
  const markerNeedles = buildMarkerNeedles(marker);
  const recentCaptureLayers = Array.isArray(runtimeState?.recentCaptureLayers)
    ? runtimeState.recentCaptureLayers
    : [];
  return recentCaptureLayers
    .map((entry) => {
      const details = String(entry?.details || "");
      if (!markerNeedles.some((needle) => details.includes(needle))) {
        return null;
      }
      return {
        at: String(entry?.at || ""),
        atMs: parseAcceptanceTimestampMs(entry?.at),
        layer: String(entry?.layer || ""),
        action: String(entry?.action || ""),
        pending: Boolean(entry?.pending),
        rawUri: String(entry?.uri || ""),
        uri: normalizeCliCaptureUri(entry?.uri) || null,
        details,
      };
    })
    .filter(Boolean);
}

function hasRecentRuntimeMarkerEvidence(markerEvidence, minTimestampMs = 0) {
  const recentEntries = Array.isArray(markerEvidence?.recentMatchingEntries)
    ? markerEvidence.recentMatchingEntries
    : [];
  return recentEntries.some((entry) => (
    Boolean(entry?.atMs)
    && entry.atMs >= minTimestampMs
    && !entry.pending
    && ["ADD", "UPDATE"].includes(entry.action)
  ));
}

async function collectCliMarkerEvidence(scenario, marker) {
  const markerNeedles = buildMarkerNeedles(marker);
  const searchResult = await runAcceptanceCliJson(scenario, [
    "memory-palace",
    "search",
    marker,
    "--json",
  ]);
  const statusResult = await runAcceptanceCliJson(scenario, [
    "memory-palace",
    "status",
    "--json",
  ]);
  const searchPayload = searchResult.payload ?? {};
  const searchResults = Array.isArray(searchPayload.results) ? searchPayload.results : [];
  const searchFound = searchResults.some((entry) => {
    const haystacks = [
      entry?.path,
      entry?.citation,
      entry?.snippet,
    ];
    return haystacks.some((value) => String(value || "").includes(marker));
  }) || stringifyAcceptancePayload(searchPayload).includes(marker);
  const runtimeState = statusResult.payload?.runtimeState ?? {};
  const structuredStatusEntries = [
    runtimeState.lastCapturePath,
    runtimeState.lastReconcile,
  ].filter((entry) => entry && typeof entry === "object");
  const matchingStructuredEntry = structuredStatusEntries.find((entry) => {
    const normalizedUri = normalizeCliCaptureUri(entry?.uri);
    const details = String(entry?.details || "");
    return Boolean(normalizedUri)
      && markerNeedles.some((needle) => details.includes(needle));
  }) || null;
  const recentMatchingEntries = collectRecentRuntimeMarkerEntries(runtimeState, marker);
  const lastCaptureUri = matchingStructuredEntry
    ? normalizeCliCaptureUri(matchingStructuredEntry.uri)
    : null;
  let statusFound = false;
  let getResult = null;
  if (lastCaptureUri) {
    try {
      getResult = await runAcceptanceCliJson(scenario, [
        "memory-palace",
        "get",
        lastCaptureUri,
        "--json",
      ]);
      const getText = String(getResult.payload?.text || stringifyAcceptancePayload(getResult.payload));
      statusFound = getText.includes(marker);
    } catch {
      statusFound = false;
    }
  }
  return {
    searchFound,
    statusFound,
    strongEvidence: searchFound || statusFound,
    lastCaptureUri,
    recentMatchingEntries,
    recentMatchFound: recentMatchingEntries.length > 0,
    searchResult,
    statusResult,
    getResult,
  };
}

async function collectCliIntegrationEvidence(scenario, marker) {
  const markerEvidence = await collectCliMarkerEvidence(scenario, marker);
  const statusPayload = markerEvidence.statusResult.payload ?? {};
  const runtimeState = statusPayload.runtimeState ?? {};
  const checks = Array.isArray(statusPayload.checks) ? statusPayload.checks : [];
  const strongEvidence = [];
  if (runtimeState.lastCapturePath?.uri) {
    strongEvidence.push(`cli:lastCapturePath:${runtimeState.lastCapturePath.uri}`);
  }
  if (runtimeState.lastRuleCaptureDecision?.uri) {
    strongEvidence.push(`cli:lastRuleCapture:${runtimeState.lastRuleCaptureDecision.uri}`);
  }
  if (markerEvidence.searchFound) {
    strongEvidence.push(`cli:search:${marker}`);
  }
  if (markerEvidence.statusFound) {
    strongEvidence.push(`cli:status:${marker}`);
  }
  for (const check of checks) {
    if (check?.id === "auto-recall" && check?.status === "pass") {
      strongEvidence.push("cli:auto-recall:pass");
    }
    if (check?.id === "auto-capture" && check?.status === "pass") {
      strongEvidence.push("cli:auto-capture:pass");
    }
  }
  return {
    ...markerEvidence,
    strongEvidence,
    hasStrongEvidence: strongEvidence.length > 0,
  };
}

// ---------------------------------------------------------------------------
// Verification Points
// ---------------------------------------------------------------------------

/**
 * V1 – Plugin Status Visible in Dashboard
 *
 * Navigates to the plugins / settings area and asserts that
 * "memory-palace" (or "memory palace") text is present.
 */
async function verifyV1(page, baseUrl) {
  const id = "V1";
  const name = "Plugin Status Visible";
  const screenshotPath = path.join(screenshotDir, "v1_plugin_status.png");
  const selectorsChecked = [];

  try {
    // Try several likely routes where plugin info appears.
    const candidateRoutes = ["/plugins", "/settings", "/skills", "/extensions", "/"];
    let found = false;
    let actual = "";
    const needles = [
      "memory-palace",
      "memory palace",
      "Memory Palace",
      "memory_palace",
      "@openclaw/memory-palace",
    ];

    for (const route of candidateRoutes) {
      const target = new URL(route, baseUrl.replace(/#.*$/, "")).toString();
      selectorsChecked.push(`route:${route}`);
      await page.goto(target, { waitUntil: "networkidle", timeout: VERIFICATION_TIMEOUT_MS });
      for (const waitMs of [1_200, 2_500]) {
        await page.waitForTimeout(waitMs);
        const bodyText = await page.locator("body").innerText();
        for (const needle of needles) {
          selectorsChecked.push(`text:${needle}@${route}+${waitMs}`);
          if (bodyText.toLowerCase().includes(needle.toLowerCase())) {
            found = true;
            actual = `Found "${needle}" on route ${route} after ${waitMs}ms`;
            break;
          }
        }
        if (found) break;
      }
      if (found) break;
    }

    await page.screenshot({ path: screenshotPath, fullPage: false });

    return buildResult({
      id,
      name,
      setup: "Navigate to dashboard plugin/settings pages",
      action: `Checked routes: ${candidateRoutes.join(", ")}`,
      expected: 'Plugin name "memory-palace" or equivalent text is visible',
      actual: found ? actual : "Plugin name not found on any checked route",
      pass_fail: found ? "PASS" : "FAIL",
      screenshot_path: screenshotPath,
      selectors_checked: selectorsChecked,
    });
  } catch (err) {
    await page.screenshot({ path: screenshotPath, fullPage: false }).catch(() => {});
    return buildResult({
      id,
      name,
      setup: "Navigate to dashboard",
      action: "Look for memory-palace entry",
      expected: "Plugin name visible",
      actual: `Error: ${err.message}`,
      pass_fail: "FAIL",
      screenshot_path: screenshotPath,
      selectors_checked: selectorsChecked,
    });
  }
}

/**
 * V2 – Memory Write + Recall in Chat
 *
 * Use the documented alpha workflow scenario instead of a random marker.
 * This keeps the acceptance path aligned with the repo's own WebUI ACL demo
 * and avoids false negatives caused by free-form recall phrasing.
 */
async function verifyV2(page, baseUrl, scenario) {
  const id = "V2";
  const name = "Memory Write + Recall in Chat";
  const screenshotPath = path.join(screenshotDir, "v2_chat_recall.png");
  const allowCliFallback = shouldAllowCliDurableFallback(id);
  const strictUiOnly = shouldEnforceStrictUiEvidence(id);
  const rejectVisibleUiNoise = shouldRejectVisibleUiNoise(id);
  const { confirmText, marker: rememberedMarker, writePrompt, recallPrompt, expectedScope } =
    acceptanceMemorySpec;
  const assistantLabel = profileMatrixMode ? "Onboarding Doc Test" : "Scenario alpha";
  const selectorsChecked = [];

  try {
    const chatUrl = new URL("/chat", baseUrl.replace(/#.*$/, ""));
    chatUrl.searchParams.set("session", acceptanceChatSession);
    selectorsChecked.push("route:/chat");
    selectorsChecked.push(`strict_ui_mode:${strictUiOnly}`);
    selectorsChecked.push(`reject_visible_ui_noise:${rejectVisibleUiNoise}`);
    await page.goto(chatUrl.toString(), { waitUntil: "networkidle", timeout: VERIFICATION_TIMEOUT_MS });
    await page.waitForTimeout(1_400);

    await clickConnectIfPresent(page);
    if (!profileMatrixMode && !useCurrentHost && ["c", "d"].includes(requestedProfile)) {
      await stabilizeChatRouteAfterConnect(page, chatUrl, selectorsChecked, "advanced_profile_initial_bind");
    }

    // Locate the input area.
    const inputLocator = getChatInputLocator(page);
    selectorsChecked.push("selector:textarea|input[placeholder*=Message]|[contenteditable]");

    // --- Step 1: Write ---
    const writeCountBefore = await countOccurrences(page, confirmText);
    const assistantLabelCountBefore = await countOccurrences(page, assistantLabel);

    await inputLocator.click();
    await inputLocator.fill(writePrompt);
    await page.keyboard.press("Enter");

    await page.waitForTimeout(2_000);
    const writeCountAfterPrompt = await countOccurrences(page, confirmText);
    const assistantLabelCountAfterPrompt = await countOccurrences(page, assistantLabel);

    const writeDeadline = Date.now() + VERIFICATION_TIMEOUT_MS;
    let writeConfirmed = assistantLabelCountAfterPrompt > assistantLabelCountBefore;
    let writeActual = "";
    while (Date.now() < writeDeadline) {
      const mainText = await page.locator("main").innerText();
      const currentCount = mainText.split(confirmText).length - 1;
      const currentAssistantLabelCount = mainText.split(assistantLabel).length - 1;
      if (
        currentCount > writeCountAfterPrompt ||
        currentAssistantLabelCount > assistantLabelCountAfterPrompt
      ) {
        writeConfirmed = true;
        writeActual =
          `"${confirmText}" count=${currentCount} (promptBaseline=${writeCountAfterPrompt}); ` +
          `assistant label "${assistantLabel}" count=${currentAssistantLabelCount} ` +
          `(promptBaseline=${assistantLabelCountAfterPrompt})`;
        break;
      }
      await page.waitForTimeout(1_000);
    }
    selectorsChecked.push(
      `write_confirmed:${writeConfirmed}`,
      `assistant_label_before:${assistantLabelCountBefore}`,
      `assistant_label_after_prompt:${assistantLabelCountAfterPrompt}`,
      `confirm_count_after_prompt:${writeCountAfterPrompt}`,
    );

    await waitForMainTextToSettle(page);

    // --- Step 2: Recall in the same scope ---
    // Count occurrences after the recall prompt lands. The prompt itself does
    // not include the marker, so any increase must come from the assistant
    // response or a visible recall block.
    await inputLocator.click();
    await inputLocator.fill(recallPrompt);
    await page.keyboard.press("Enter");

    await page.waitForTimeout(2_000); // let the prompt render first
    const afterPromptCount = await countOccurrences(page, rememberedMarker);

    const recallDeadline = Date.now() + VERIFICATION_TIMEOUT_MS;
    let recallFound = false;
    let recallActual = "";
    let recallTagSeen = false;
    let cliEvidence = null;
    let lastCliEvidenceAt = 0;
    while (Date.now() < recallDeadline) {
      const mainText = await page.locator("main").innerText();
      const currentCount = mainText.split(rememberedMarker).length - 1;

      if (currentCount > afterPromptCount) {
        recallFound = true;
        recallActual = `Agent text recall: marker "${rememberedMarker}" count=${currentCount} (baseline=${afterPromptCount})`;
        break;
      }
      if (mainText.includes("memory-palace-recall") && currentCount >= afterPromptCount) {
        recallTagSeen = true;
        const extDeadline = Date.now() + 10_000;
        while (Date.now() < extDeadline) {
          const retryText = await page.locator("main").innerText();
          const retryCount = retryText.split(rememberedMarker).length - 1;
          if (retryCount > afterPromptCount) {
            recallFound = true;
            recallActual = `Agent text recall: marker "${rememberedMarker}" count=${retryCount} (baseline=${afterPromptCount})`;
            break;
          }
          await page.waitForTimeout(1_000);
        }
        if (recallFound) {
          break;
        }
      }
      if (allowCliFallback && Date.now() - lastCliEvidenceAt >= 5_000) {
        lastCliEvidenceAt = Date.now();
        cliEvidence = await collectCliMarkerEvidence(scenario, rememberedMarker).catch(() => null);
        const advancedProfileNormalizedCapture =
          !profileMatrixMode
          && ["c", "d"].includes(requestedProfile)
          && Boolean(cliEvidence?.lastCaptureUri);
        if (
          cliEvidence?.strongEvidence
          && (
            cliEvidence.searchFound
            || cliEvidence.statusFound
            || advancedProfileNormalizedCapture
          )
        ) {
          recallFound = true;
          recallActual = cliEvidence.searchFound || cliEvidence.statusFound
            ? (
              `CLI confirmed durable memory for marker "${rememberedMarker}"` +
              (cliEvidence.lastCaptureUri ? ` via ${cliEvidence.lastCaptureUri}` : "")
            )
            : (
              `CLI observed normalized durable capture for marker "${rememberedMarker}"` +
              ` via ${cliEvidence.lastCaptureUri}; advanced profiles can normalize` +
              " the durable record before raw marker search catches up"
            );
          break;
        }
      }
      await page.waitForTimeout(1_000);
    }
    const shouldRunExtendedUiSettle =
      ["c", "d"].includes(requestedProfile) || strictUiOnly;
    if (!recallFound && shouldRunExtendedUiSettle) {
      const settleDeadline = Date.now() + 20_000;
      selectorsChecked.push("recall_settle_window:true");
      while (Date.now() < settleDeadline) {
        await page.goto(chatUrl.toString(), {
          waitUntil: "networkidle",
          timeout: VERIFICATION_TIMEOUT_MS,
        });
        await page.waitForTimeout(1_500);
        const settledText = await page.locator("main").innerText();
        const settledCount = settledText.split(rememberedMarker).length - 1;
        if (settledCount > afterPromptCount) {
          recallFound = true;
          recallActual =
            `Agent text recall after settle: marker "${rememberedMarker}" ` +
            `count=${settledCount} (baseline=${afterPromptCount})`;
          break;
        }
        if (allowCliFallback) {
          cliEvidence = await collectCliMarkerEvidence(scenario, rememberedMarker).catch(() => null);
          const advancedProfileNormalizedCapture =
            !profileMatrixMode
            && ["c", "d"].includes(requestedProfile)
            && Boolean(cliEvidence?.lastCaptureUri);
          if (
            cliEvidence?.strongEvidence
            && (
              cliEvidence.searchFound
              || cliEvidence.statusFound
              || advancedProfileNormalizedCapture
            )
          ) {
            recallFound = true;
            recallActual = cliEvidence.searchFound || cliEvidence.statusFound
              ? (
                `CLI confirmed durable memory for marker "${rememberedMarker}"` +
                (cliEvidence.lastCaptureUri ? ` via ${cliEvidence.lastCaptureUri}` : "")
              )
              : (
                `CLI observed normalized durable capture for marker "${rememberedMarker}"` +
                ` via ${cliEvidence.lastCaptureUri}; advanced profiles can normalize` +
                " the durable record before raw marker search catches up"
              );
            break;
          }
        }
        await page.waitForTimeout(1_500);
      }
    }
    if (!recallFound && shouldRunExtendedUiSettle) {
      const routeScanActual = await scanUiRoutesForMarker(
        page,
        baseUrl,
        chatUrl,
        rememberedMarker,
        selectorsChecked,
        "recall",
      );
      if (routeScanActual) {
        recallFound = true;
        recallActual = routeScanActual;
      }
    }
    if (!recallFound && recallTagSeen) {
      recallActual = `memory-palace-recall tag present but unique marker "${rememberedMarker}" was not rendered`;
    }
    selectorsChecked.push(`recall_found:${recallFound}`, `recall_tag_seen:${recallTagSeen}`);

    const uiNoiseState = rejectVisibleUiNoise
      ? await scanVisibleUiNoiseOnChat(page, chatUrl, selectorsChecked, "v2")
      : { ok: true, hits: [] };

    await page.screenshot({ path: screenshotPath, fullPage: false });

    cliEvidence =
      cliEvidence || (recallFound
        ? null
        : await collectCliMarkerEvidence(scenario, rememberedMarker));
    const advancedProfileNormalizedCapture =
      !profileMatrixMode
      && ["c", "d"].includes(requestedProfile)
      && Boolean(cliEvidence?.lastCaptureUri);
    const recallPass =
      recallFound ||
      (
        allowCliFallback &&
        Boolean(
          cliEvidence?.strongEvidence
          && (
            cliEvidence.searchFound
            || cliEvidence.statusFound
            || advancedProfileNormalizedCapture
          )
        )
      );
    const pass = recallPass && uiNoiseState.ok;
    if (!recallFound && cliEvidence?.strongEvidence) {
      recallActual = allowCliFallback
        ? (
          cliEvidence.searchFound || cliEvidence.statusFound
            ? (
              `CLI confirmed durable memory for marker "${rememberedMarker}"` +
              (cliEvidence.lastCaptureUri ? ` via ${cliEvidence.lastCaptureUri}` : "")
            )
            : advancedProfileNormalizedCapture
              ? (
                `CLI observed normalized durable capture for marker "${rememberedMarker}"` +
                ` via ${cliEvidence.lastCaptureUri}; advanced profiles can normalize` +
                " the durable record before raw marker search catches up"
              )
            : (
              `Observed recent capture activity` +
              (cliEvidence.lastCaptureUri ? ` via ${cliEvidence.lastCaptureUri}` : "") +
              `, but CLI search/status did not yet confirm marker "${rememberedMarker}"`
            )
        )
        : (
          `Strict UI mode: CLI saw durable memory for marker "${rememberedMarker}"` +
          (cliEvidence.lastCaptureUri ? ` via ${cliEvidence.lastCaptureUri}` : "") +
          ", but the marker never became visible in the UI."
        );
    }
    selectorsChecked.push(
      `cli_search_found:${cliEvidence?.searchFound ?? false}`,
      `cli_status_found:${cliEvidence?.statusFound ?? false}`,
    );

    return buildResult({
      id,
      name,
      setup: profileMatrixMode ? "Navigate to main profile chat interface" : "Navigate to alpha chat interface",
      action: profileMatrixMode
        ? `Stored and recalled the profile marker for ${requestedProfile}`
        : "Sent a durable alpha workflow marker, then asked for recall",
      expected:
        `Agent completes the write for "${rememberedMarker}" and later exposes the remembered marker ` +
        `"${rememberedMarker}" ` +
        `for ${expectedScope}`,
      actual: pass
        ? (
          recallFound
            ? (writeConfirmed
              ? recallActual
              : `${recallActual}; assistant confirmation was not independently observed before recall`)
            : recallActual
        )
        : [
            recallFound
              ? (
                writeConfirmed
                  ? ""
                  : "Assistant confirmation was not independently observed before recall."
              )
              : (recallActual || (profileMatrixMode
                ? "Profile marker not recalled within timeout."
                : "Alpha marker not recalled within timeout.")),
            uiNoiseState.ok
              ? ""
              : `Visible chat reply still exposed raw memory tags or metadata noise: ${uiNoiseState.hits.join(", ")}.`,
          ].filter(Boolean).join(" "),
      pass_fail: pass ? "PASS" : "FAIL",
      screenshot_path: screenshotPath,
      selectors_checked: selectorsChecked,
    });
  } catch (err) {
    await page.screenshot({ path: screenshotPath, fullPage: false }).catch(() => {});
    const cliEvidence = await collectCliMarkerEvidence(scenario, rememberedMarker).catch(
      () => null,
    );
    const recoveredByCli = allowCliFallback && Boolean(
      cliEvidence?.strongEvidence && (cliEvidence.searchFound || cliEvidence.statusFound),
    );
    return buildResult({
      id,
      name,
      setup: "Navigate to chat interface",
      action: "Send write + recall messages",
      expected: "Response contains the marker text",
      actual: recoveredByCli
        ? `Error: ${err.message}; CLI confirmed durable memory for marker "${rememberedMarker}"`
        : (
          !allowCliFallback && cliEvidence?.strongEvidence
            ? `Error: ${err.message}; CLI saw durable memory for marker "${rememberedMarker}", but strict UI mode requires visible UI evidence.`
            : `Error: ${err.message}`
        ),
      pass_fail: recoveredByCli ? "PASS" : "FAIL",
      screenshot_path: screenshotPath,
      selectors_checked: [
        ...selectorsChecked,
        `cli_search_found:${cliEvidence?.searchFound ?? false}`,
        `cli_status_found:${cliEvidence?.statusFound ?? false}`,
      ],
    });
  }
}

/**
 * V3 – Memory System Integration Evidence
 *
 * Verifies that the memory-palace plugin is actively integrated into the
 * OpenClaw host by checking for visible evidence across multiple surfaces:
 * - Skills page: memory-palace skills entries
 * - Agents page: agent with memory-palace config
 * - Nodes page: memory node entries
 * - Chat page: recall block tags like <memory-palace-recall>
 * - Settings/Config: memory-palace configuration entries
 *
 * MCP tool names (search_memory etc.) are protocol-internal and not shown
 * as text in the UI, so we check for integration artifacts instead.
 */
async function verifyV3(page, baseUrl, scenario) {
  const id = "V3";
  const name = "Memory System Integration Evidence";
  const screenshotPath = path.join(screenshotDir, "v3_mcp_tools.png");
  const selectorsChecked = [];
  const { marker: acceptanceMarker } = acceptanceMemorySpec;
  const chatFlowMarkers = [acceptanceMarker];
  // Evidence markers: things that prove memory-palace is loaded and active
  const evidenceMarkers = [
    // Plugin/config markers
    "memory-palace",
    "memory palace",
    "Memory Palace",
    // Recall block markers (visible in chat responses)
    "memory-palace-recall",
    "durable-memory",
    // Skill/agent markers
    "memory_palace",
    // Path markers from memory system
    "memory-palace/core",
    // MCP stdio marker
    "mcp_server",
    "run_memory_palace_mcp_stdio",
    acceptanceMarker,
  ];

  try {
    // Check multiple surfaces for integration evidence
    const chatEvidenceRoute = `/chat?session=${encodeURIComponent(acceptanceChatSession)}`;
    const candidateRoutes = ["/skills", "/agents", "/nodes", chatEvidenceRoute, "/settings", "/"];
    let found = false;
    const matchedEvidence = [];
    // Track strong evidence (recall tags, paths) vs weak (just plugin name)
    let hasStrongEvidence = false;
    const strongMarkers = new Set([
      "memory-palace-recall",
      "durable-memory",
      "memory-palace/core",
      "mcp_server",
      "run_memory_palace_mcp_stdio",
    ]);

    for (const route of candidateRoutes) {
      const target = new URL(route, baseUrl.replace(/#.*$/, "")).toString();
      selectorsChecked.push(`route:${route}`);
      await page.goto(target, { waitUntil: "networkidle", timeout: VERIFICATION_TIMEOUT_MS });
      await page.waitForTimeout(1_500);

      const bodyText = await page.locator("body").innerText();
      let routeHits = 0;

      for (const marker of evidenceMarkers) {
        if (bodyText.toLowerCase().includes(marker.toLowerCase())) {
          matchedEvidence.push(`${marker} @ ${route}`);
          routeHits++;
          if (strongMarkers.has(marker)) {
            hasStrongEvidence = true;
          }
          found = true;
        }
      }
      if (route.startsWith("/chat")) {
        for (const marker of chatFlowMarkers) {
          if (bodyText.includes(marker)) {
            matchedEvidence.push(`chat_flow:${marker} @ ${route}`);
            routeHits++;
            hasStrongEvidence = true;
            found = true;
          }
        }
      }
      // Per-route: only report hits for THIS route
      selectorsChecked.push(`evidence_found_on_${route}:${routeHits > 0}`);
    }

    await page.screenshot({ path: screenshotPath, fullPage: false });

    const cliEvidence = await collectCliIntegrationEvidence(
      scenario,
      acceptanceMarker,
    ).catch(() => null);
    if (cliEvidence?.hasStrongEvidence) {
      hasStrongEvidence = true;
      found = true;
      matchedEvidence.push(...cliEvidence.strongEvidence);
    }
    selectorsChecked.push(`cli_strong_evidence:${cliEvidence?.hasStrongEvidence ?? false}`);

    const passCondition = found && hasStrongEvidence;

    return buildResult({
      id,
      name,
      setup: "Navigate to skills/agents/nodes/chat/settings pages",
      action: `Checked ${candidateRoutes.length} routes for memory system integration evidence`,
      expected: profileMatrixMode
        ? "At least one strong integration artifact (profile marker, node path, mcp_server) visible"
        : "At least one strong integration artifact (recall tag, node path, mcp_server) visible",
      actual: found
        ? `Found ${matchedEvidence.length} evidence markers (strong=${hasStrongEvidence}): [${matchedEvidence.join("; ")}]`
        : "No memory system integration evidence found on any checked route",
      pass_fail: passCondition ? "PASS" : "FAIL",
      screenshot_path: screenshotPath,
      selectors_checked: selectorsChecked,
    });
  } catch (err) {
    await page.screenshot({ path: screenshotPath, fullPage: false }).catch(() => {});
    const cliEvidence = await collectCliIntegrationEvidence(
      scenario,
      acceptanceMarker,
    ).catch(() => null);
    return buildResult({
      id,
      name,
      setup: "Navigate to integration surfaces",
      action: "Look for memory system integration evidence",
      expected: "At least one integration artifact visible",
      actual: cliEvidence?.hasStrongEvidence
        ? `Error: ${err.message}; CLI strong evidence: [${cliEvidence.strongEvidence.join("; ")}]`
        : `Error: ${err.message}`,
      pass_fail: cliEvidence?.hasStrongEvidence ? "PASS" : "FAIL",
      screenshot_path: screenshotPath,
      selectors_checked: [
        ...selectorsChecked,
        `cli_strong_evidence:${cliEvidence?.hasStrongEvidence ?? false}`,
      ],
    });
  }
}

/**
 * V4 – Guarded Write -> Confirm -> Force Save in Chat
 *
 * Exercises the explicit memory_learn recovery path through the real chat UI:
 * 1. store a baseline memory,
 * 2. send a near-duplicate variant that should pause behind write_guard,
 * 3. confirm that the separate durable memory should still be saved,
 * 4. verify the variant can be recalled.
 */
async function verifyV4(page, baseUrl, scenario) {
  const id = "V4";
  const name = "Guarded Write Confirm + Force Save";
  const screenshotPath = path.join(screenshotDir, "v4_force_write_chat.png");
  const selectorsChecked = [];
  const allowCliFallback = shouldAllowCliDurableFallback(id);
  const strictUiOnly = shouldEnforceStrictUiEvidence(id);
  const rejectVisibleUiNoise = shouldRejectVisibleUiNoise(id);
  const {
    baseMarker,
    variantMarker,
    pausedText,
    storedText,
    baseWritePrompt,
    blockedWritePrompt,
    confirmForcePrompt,
    recallPrompt,
    expectedScope,
  } = acceptanceForceWriteSpec;
  const assistantLabel = profileMatrixMode ? "Onboarding Doc Test" : "Scenario alpha";
  const flowTimeoutMs = Math.max(VERIFICATION_TIMEOUT_MS, 90_000);

  try {
    const chatUrl = new URL("/chat", baseUrl.replace(/#.*$/, ""));
    chatUrl.searchParams.set("session", acceptanceForceChatSession);
    selectorsChecked.push("route:/chat(force)");
    selectorsChecked.push(`strict_ui_mode:${strictUiOnly}`);
    selectorsChecked.push(`reject_visible_ui_noise:${rejectVisibleUiNoise}`);
    await page.goto(chatUrl.toString(), { waitUntil: "networkidle", timeout: VERIFICATION_TIMEOUT_MS });
    await page.waitForTimeout(1_400);
    await clickConnectIfPresent(page);

    const inputLocator = getChatInputLocator(page);
    selectorsChecked.push("selector:chat-input(force)");

    // Step 1: seed a baseline durable memory.
    const assistantLabelCountBeforeBase = await countOccurrences(page, assistantLabel);
    const storedCountBeforeBase = await countOccurrences(page, storedText);
    await inputLocator.click();
    await inputLocator.fill(baseWritePrompt);
    await page.keyboard.press("Enter");

    await page.waitForTimeout(2_000);
    const assistantLabelCountAfterBasePrompt = await countOccurrences(page, assistantLabel);
    const storedCountAfterBasePrompt = await countOccurrences(page, storedText);

    const baseDeadline = Date.now() + flowTimeoutMs;
    let baseWriteConfirmed = assistantLabelCountAfterBasePrompt > assistantLabelCountBeforeBase;
    while (Date.now() < baseDeadline) {
      const mainText = await page.locator("main").innerText();
      const currentAssistantLabelCount = mainText.split(assistantLabel).length - 1;
      const currentStoredCount = mainText.split(storedText).length - 1;
      if (
        currentAssistantLabelCount > assistantLabelCountAfterBasePrompt ||
        currentStoredCount > storedCountAfterBasePrompt
      ) {
        baseWriteConfirmed = true;
        break;
      }
      await page.waitForTimeout(1_000);
    }

    const baseCliEvidence = await collectCliMarkerEvidence(scenario, baseMarker);
    selectorsChecked.push(
      `base_write_confirmed:${baseWriteConfirmed}`,
      `base_cli_search_found:${baseCliEvidence.searchFound}`,
      `base_assistant_label_after_prompt:${assistantLabelCountAfterBasePrompt}`,
      `base_stored_count_after_prompt:${storedCountAfterBasePrompt}`,
    );

    // Step 2: ask for a near-duplicate variant and expect a guarded pause.
    await inputLocator.click();
    await inputLocator.fill(blockedWritePrompt);
    await page.keyboard.press("Enter");
    await page.waitForTimeout(2_000);
    const pausedBaseline = await countOccurrences(page, pausedText);

    const blockedAssistantLabelBefore = await countOccurrences(page, assistantLabel);
    const blockedToolOutputBefore = await countOccurrences(page, "Tool output");
    const blockedDeadline = Date.now() + flowTimeoutMs;
    let blockedSeenInChat = false;
    let blockedHandledInChat = false;
    let blockedActual = "";
    while (Date.now() < blockedDeadline) {
      const mainText = await page.locator("main").innerText();
      const pausedCount = mainText.split(pausedText).length - 1;
      const assistantLabelCount = mainText.split(assistantLabel).length - 1;
      const toolOutputCount = mainText.split("Tool output").length - 1;
      if (pausedCount > pausedBaseline) {
        blockedSeenInChat = true;
        blockedHandledInChat = true;
        blockedActual = `Chat pause confirmed via "${pausedText}".`;
        break;
      }
      if (
        (assistantLabelCount > blockedAssistantLabelBefore ||
         toolOutputCount > blockedToolOutputBefore) &&
        (/paused|blocked|guard/i.test(mainText) || mainText.includes(pausedText))
      ) {
        blockedHandledInChat = true;
      }
      await page.waitForTimeout(1_000);
    }

    const blockedSearch = await runAcceptanceCliJson(scenario, [
      "memory-palace",
      "search",
      variantMarker,
      "--json",
    ]);
    const blockedSearchText = stringifyAcceptancePayload(blockedSearch.payload);
    const variantStoredBeforeConfirm = blockedSearchText.includes(variantMarker);
    selectorsChecked.push(
      `blocked_seen_in_chat:${blockedSeenInChat}`,
      `blocked_handled_in_chat:${blockedHandledInChat}`,
      `variant_stored_before_confirm:${variantStoredBeforeConfirm}`,
    );

    // Step 3: confirm and require a force-backed separate durable write.
    await waitForMainTextToSettle(page);
    const assistantLabelCountBeforeConfirm = await countOccurrences(page, assistantLabel);
    const storedCountBeforeConfirm = await countOccurrences(page, storedText);
    await inputLocator.click();
    await inputLocator.fill(confirmForcePrompt);
    const confirmStartedAtMs = Date.now();
    await page.keyboard.press("Enter");
    await page.waitForTimeout(2_000);
    const storedBaseline = await countOccurrences(page, storedText);
    const assistantLabelCountAfterConfirmPrompt = await countOccurrences(page, assistantLabel);

    const forceDeadline = Date.now() + flowTimeoutMs;
    let forceConfirmedInChat = assistantLabelCountAfterConfirmPrompt > assistantLabelCountBeforeConfirm;
    let forceCliEvidence = null;
    let lastForceCliCheckAt = 0;
    while (Date.now() < forceDeadline) {
      const mainText = await page.locator("main").innerText();
      const storedCount = mainText.split(storedText).length - 1;
      const currentAssistantLabelCount = mainText.split(assistantLabel).length - 1;
      if (
        storedCount > storedBaseline ||
        currentAssistantLabelCount > assistantLabelCountAfterConfirmPrompt
      ) {
        forceConfirmedInChat = true;
        break;
      }
      if (allowCliFallback && Date.now() - lastForceCliCheckAt >= 5_000) {
        lastForceCliCheckAt = Date.now();
        forceCliEvidence = await collectCliMarkerEvidence(scenario, variantMarker).catch(() => null);
        const postConfirmRuntimeFound = hasRecentRuntimeMarkerEvidence(
          forceCliEvidence,
          confirmStartedAtMs,
        );
        if (
          forceCliEvidence?.strongEvidence
          && (
            forceCliEvidence.searchFound
            || forceCliEvidence.statusFound
            || postConfirmRuntimeFound
          )
        ) {
          forceConfirmedInChat = true;
          break;
        }
      }
      await page.waitForTimeout(1_000);
    }

    forceCliEvidence = forceCliEvidence || await collectCliMarkerEvidence(scenario, variantMarker);
    let forcePostConfirmRuntimeFound = hasRecentRuntimeMarkerEvidence(
      forceCliEvidence,
      confirmStartedAtMs,
    );
    if (
      allowCliFallback
      && forceConfirmedInChat
      && !forceCliEvidence.searchFound
      && !forceCliEvidence.statusFound
      && !forcePostConfirmRuntimeFound
    ) {
      const runtimeSettleDeadline = Date.now() + 45_000;
      selectorsChecked.push("force_runtime_settle_window:true");
      while (Date.now() < runtimeSettleDeadline) {
        await page.waitForTimeout(3_000);
        forceCliEvidence = await collectCliMarkerEvidence(scenario, variantMarker).catch(
          () => forceCliEvidence,
        );
        forcePostConfirmRuntimeFound = hasRecentRuntimeMarkerEvidence(
          forceCliEvidence,
          confirmStartedAtMs,
        );
        if (
          forceCliEvidence.searchFound
          || forceCliEvidence.statusFound
          || forcePostConfirmRuntimeFound
        ) {
          break;
        }
      }
    }
    selectorsChecked.push(
      `force_confirmed_in_chat:${forceConfirmedInChat}`,
      `force_cli_search_found:${forceCliEvidence.searchFound}`,
      `force_cli_status_found:${forceCliEvidence.statusFound}`,
      `force_cli_recent_match_found:${forceCliEvidence.recentMatchFound}`,
      `force_cli_post_confirm_runtime_found:${forcePostConfirmRuntimeFound}`,
      `force_assistant_label_after_prompt:${assistantLabelCountAfterConfirmPrompt}`,
      `force_stored_count_before:${storedCountBeforeConfirm}`,
    );

    // Step 4: verify recall of the forced variant marker in chat.
    await waitForMainTextToSettle(page);
    await inputLocator.click();
    await inputLocator.fill(recallPrompt);
    await page.keyboard.press("Enter");
    await page.waitForTimeout(2_000);
    const recallBaseline = await countOccurrences(page, variantMarker);

    const recallDeadline = Date.now() + flowTimeoutMs;
    let recallFound = false;
    let recallActual = "";
    let recallCliEvidence = null;
    let lastRecallCliCheckAt = 0;
    while (Date.now() < recallDeadline) {
      const mainText = await page.locator("main").innerText();
      const markerCount = mainText.split(variantMarker).length - 1;
      if (markerCount > recallBaseline) {
        recallFound = true;
        recallActual = `Variant recall confirmed for "${variantMarker}" in ${expectedScope}.`;
        break;
      }
      if (allowCliFallback && Date.now() - lastRecallCliCheckAt >= 5_000) {
        lastRecallCliCheckAt = Date.now();
        recallCliEvidence = await collectCliMarkerEvidence(scenario, variantMarker).catch(() => null);
        if (recallCliEvidence?.strongEvidence && (recallCliEvidence.searchFound || recallCliEvidence.statusFound)) {
          recallFound = true;
          recallActual =
            `CLI confirmed durable memory for "${variantMarker}"` +
            (recallCliEvidence.lastCaptureUri ? ` via ${recallCliEvidence.lastCaptureUri}` : "");
          break;
        }
      }
      await page.waitForTimeout(1_000);
    }
    if (!recallFound && strictUiOnly) {
      const routeScanActual = await scanUiRoutesForMarker(
        page,
        baseUrl,
        chatUrl,
        variantMarker,
        selectorsChecked,
        "force_recall",
      );
      if (routeScanActual) {
        recallFound = true;
        recallActual = routeScanActual;
      }
    }

    const uiNoiseState = rejectVisibleUiNoise
      ? await scanVisibleUiNoiseOnChat(page, chatUrl, selectorsChecked, "v4")
      : { ok: true, hits: [] };

    await page.screenshot({ path: screenshotPath, fullPage: false });

    const baseStored = Boolean(
      allowCliFallback
        ? (
          baseWriteConfirmed ||
          baseCliEvidence.searchFound ||
          baseCliEvidence.statusFound
        )
        : baseWriteConfirmed
    );
    const forceStored = Boolean(
      allowCliFallback
        ? (
          forceCliEvidence.searchFound ||
          forceCliEvidence.statusFound ||
          forcePostConfirmRuntimeFound
        )
        : forceConfirmedInChat
    );
    const recallSatisfied = allowCliFallback ? (recallFound || forceStored) : recallFound;
    const pass =
      baseStored &&
      blockedHandledInChat &&
      !variantStoredBeforeConfirm &&
      forceConfirmedInChat &&
      forceStored &&
      recallSatisfied &&
      uiNoiseState.ok;

    return buildResult({
      id,
      name,
      setup: "Use a dedicated chat session to exercise blocked memory_learn recovery",
      action:
        "Seeded a baseline memory, triggered a guarded near-duplicate write, confirmed the separate write, then asked for recall",
      expected:
        `The variant marker "${variantMarker}" is blocked first, then stored only after confirmation, and later recalled in chat.`,
      actual: pass
        ? (recallActual || `Variant marker "${variantMarker}" was stored after confirmation.`)
        : [
            baseStored ? "" : `Baseline marker "${baseMarker}" was not stored.`,
            blockedHandledInChat
              ? ""
              : `The blocked write did not show a clear handled response in chat.`,
            !variantStoredBeforeConfirm
              ? ""
              : `Variant marker "${variantMarker}" appeared to be stored before confirmation.`,
            forceConfirmedInChat ? "" : "Force confirmation response was not observed in chat.",
            forceStored
              ? ""
              : `Variant marker "${variantMarker}" was not found after confirmation.`,
            recallSatisfied ? "" : `Variant marker "${variantMarker}" was not recalled in chat.`,
            !allowCliFallback && recallCliEvidence?.strongEvidence
              ? `CLI saw durable evidence for "${variantMarker}", but strict UI mode requires the marker to be visible in the UI.`
              : "",
            uiNoiseState.ok
              ? ""
              : `Visible chat reply still exposed raw memory tags or metadata noise: ${uiNoiseState.hits.join(", ")}.`,
            blockedActual,
          ].filter(Boolean).join(" "),
      pass_fail: pass ? "PASS" : "FAIL",
      screenshot_path: screenshotPath,
      selectors_checked: selectorsChecked,
    });
  } catch (err) {
    await page.screenshot({ path: screenshotPath, fullPage: false }).catch(() => {});
    return buildResult({
      id,
      name,
      setup: "Run guarded write recovery through chat",
      action: "Seed baseline, trigger blocked write, confirm force save, recall variant",
      expected: "The blocked write is confirmed and only then saved as a separate durable memory.",
      actual: `Error: ${err.message}`,
      pass_fail: "FAIL",
      screenshot_path: screenshotPath,
      selectors_checked: selectorsChecked,
    });
  }
}

/**
 * V5 – Chinese Minimal Confirmation in Chat
 *
 * Verifies that a Chinese explicit remember request produces a visible minimal
 * confirmation ("记住了。") in chat, then the remembered marker can be recalled.
 */
async function verifyV5(page, baseUrl, scenario) {
  const id = "V5";
  const name = "Chinese Minimal Confirmation";
  const screenshotPath = path.join(screenshotDir, "v5_chinese_confirm_chat.png");
  const selectorsChecked = [];
  const allowCliFallback = shouldAllowCliDurableFallback(id);
  const strictUiOnly = shouldEnforceStrictUiEvidence(id);
  const rejectVisibleUiNoise = shouldRejectVisibleUiNoise(id);
  const { confirmText, marker, writePrompt, recallPrompt } = acceptanceChineseConfirmSpec;
  const assistantLabel = profileMatrixMode ? "Onboarding Doc Test" : "Scenario alpha";

  try {
    const chatUrl = new URL("/chat", baseUrl.replace(/#.*$/, ""));
    chatUrl.searchParams.set("session", acceptanceChineseChatSession);
    selectorsChecked.push("route:/chat(zh-confirm)");
    selectorsChecked.push(`strict_ui_mode:${strictUiOnly}`);
    selectorsChecked.push(`reject_visible_ui_noise:${rejectVisibleUiNoise}`);
    await page.goto(chatUrl.toString(), { waitUntil: "networkidle", timeout: VERIFICATION_TIMEOUT_MS });
    await page.waitForTimeout(1_400);
    await clickConnectIfPresent(page);

    const inputLocator = getChatInputLocator(page);
    selectorsChecked.push("selector:chat-input(zh-confirm)");

    const confirmCountBefore = await countOccurrences(page, confirmText);
    const toolOutputBefore = await countOccurrences(page, "Tool output");
    const memoryLearnToolBefore = await countOccurrences(page, "memory_learn");
    const assistantLabelCountBefore = await countOccurrences(page, assistantLabel);

    await inputLocator.click();
    await inputLocator.fill(writePrompt);
    await page.keyboard.press("Enter");

    // Wait for the prompt to render, then record an intermediate baseline
    // so the confirmation text inside the user prompt itself is not counted.
    await page.waitForTimeout(2_000);
    const afterPromptConfirmCount = await countOccurrences(page, confirmText);
    const afterPromptToolOutputCount = await countOccurrences(page, "Tool output");
    const afterPromptMemoryLearnToolCount = await countOccurrences(page, "memory_learn");
    const afterPromptAssistantLabelCount = await countOccurrences(page, assistantLabel);

    const confirmDeadline = Date.now() + VERIFICATION_TIMEOUT_MS;
    let confirmSeen = (
      afterPromptConfirmCount > confirmCountBefore
      && (
        afterPromptToolOutputCount > toolOutputBefore ||
        afterPromptMemoryLearnToolCount > memoryLearnToolBefore ||
        afterPromptAssistantLabelCount > assistantLabelCountBefore
      )
    );
    while (Date.now() < confirmDeadline) {
      const mainText = await page.locator("main").innerText();
      const confirmCount = mainText.split(confirmText).length - 1;
      const toolOutputCount = mainText.split("Tool output").length - 1;
      const memoryLearnToolCount = mainText.split("memory_learn").length - 1;
      const assistantLabelCount = mainText.split(assistantLabel).length - 1;
      if (
        confirmCount > afterPromptConfirmCount &&
        (
          toolOutputCount > afterPromptToolOutputCount ||
          memoryLearnToolCount > afterPromptMemoryLearnToolCount ||
          assistantLabelCount > afterPromptAssistantLabelCount
        )
      ) {
        confirmSeen = true;
        break;
      }
      await page.waitForTimeout(1_000);
    }
    selectorsChecked.push(
      `confirm_seen:${confirmSeen}`,
      `confirm_count_before:${confirmCountBefore}`,
      `after_prompt_confirm_count:${afterPromptConfirmCount}`,
      `tool_output_before:${toolOutputBefore}`,
      `memory_learn_tool_before:${memoryLearnToolBefore}`,
      `assistant_label_before:${assistantLabelCountBefore}`,
      `assistant_label_after_prompt:${afterPromptAssistantLabelCount}`,
    );

    await waitForMainTextToSettle(page);
    await inputLocator.click();
    await inputLocator.fill(recallPrompt);
    await page.keyboard.press("Enter");
    await page.waitForTimeout(2_000);
    const recallBaseline = await countOccurrences(page, marker);

    const recallDeadline = Date.now() + VERIFICATION_TIMEOUT_MS;
    let recallSeen = false;
    let recallActual = "";
    let recallCliEvidence = null;
    let lastRecallCliCheckAt = 0;
    while (Date.now() < recallDeadline) {
      const mainText = await page.locator("main").innerText();
      const markerCount = mainText.split(marker).length - 1;
      if (markerCount > recallBaseline) {
        recallSeen = true;
        recallActual = `Chinese confirmation marker "${marker}" was recalled.`;
        break;
      }
      if (allowCliFallback && Date.now() - lastRecallCliCheckAt >= 5_000) {
        lastRecallCliCheckAt = Date.now();
        recallCliEvidence = await collectCliMarkerEvidence(scenario, marker).catch(() => null);
        if (recallCliEvidence?.strongEvidence && (recallCliEvidence.searchFound || recallCliEvidence.statusFound)) {
          recallSeen = true;
          recallActual =
            `CLI confirmed Chinese confirmation marker "${marker}"` +
            (recallCliEvidence.lastCaptureUri ? ` via ${recallCliEvidence.lastCaptureUri}` : "");
          break;
        }
      }
      await page.waitForTimeout(1_000);
    }
    if (!recallSeen && strictUiOnly) {
      const routeScanActual = await scanUiRoutesForMarker(
        page,
        baseUrl,
        chatUrl,
        marker,
        selectorsChecked,
        "zh_recall",
      );
      if (routeScanActual) {
        recallSeen = true;
        recallActual = routeScanActual;
      }
    }
    selectorsChecked.push(`recall_seen:${recallSeen}`);

    const cliEvidence = recallCliEvidence || await collectCliMarkerEvidence(scenario, marker).catch(() => null);
    selectorsChecked.push(
      `cli_search_found:${cliEvidence?.searchFound ?? false}`,
      `cli_status_found:${cliEvidence?.statusFound ?? false}`,
    );

    const uiNoiseState = rejectVisibleUiNoise
      ? await scanVisibleUiNoiseOnChat(page, chatUrl, selectorsChecked, "v5")
      : { ok: true, hits: [] };

    await page.screenshot({ path: screenshotPath, fullPage: false });

    const pass = uiNoiseState.ok && confirmSeen && (
      recallSeen || (
        allowCliFallback &&
        Boolean(cliEvidence?.strongEvidence)
      )
    );
    return buildResult({
      id,
      name,
      setup: "Use a dedicated chat session for a Chinese explicit remember request",
      action: "Asked for a Chinese minimal confirmation, then requested recall of the same marker",
      expected: `The assistant visibly replies with "${confirmText}" in chat and later recalls "${marker}".`,
      actual: pass
        ? recallActual
        : [
            confirmSeen ? "" : `Chinese minimal confirmation "${confirmText}" was not visibly rendered in chat.`,
            recallSeen || (allowCliFallback && cliEvidence?.strongEvidence)
              ? ""
              : `Chinese confirmation marker "${marker}" was not recalled in chat.`,
            !allowCliFallback && cliEvidence?.strongEvidence
              ? `CLI saw durable evidence for "${marker}", but strict UI mode requires visible UI evidence.`
              : "",
            uiNoiseState.ok
              ? ""
              : `Visible chat reply still exposed raw memory tags or metadata noise: ${uiNoiseState.hits.join(", ")}.`,
          ].filter(Boolean).join(" "),
      pass_fail: pass ? "PASS" : "FAIL",
      screenshot_path: screenshotPath,
      selectors_checked: selectorsChecked,
    });
  } catch (err) {
    await page.screenshot({ path: screenshotPath, fullPage: false }).catch(() => {});
    return buildResult({
      id,
      name,
      setup: "Run Chinese confirmation flow through chat",
      action: "Send a Chinese explicit remember request and check for the minimal confirmation",
      expected: `A visible "${confirmText}" confirmation followed by marker recall.`,
      actual: `Error: ${err.message}`,
      pass_fail: "FAIL",
      screenshot_path: screenshotPath,
      selectors_checked: selectorsChecked,
    });
  }
}

/**
 * V6 – English Minimal Confirmation in Chat
 *
 * Verifies that an English explicit remember request produces a visible minimal
 * confirmation ("Stored.") in chat, then the remembered marker can be recalled.
 */
async function verifyV6(page, baseUrl, scenario) {
  const id = "V6";
  const name = "English Minimal Confirmation";
  const screenshotPath = path.join(screenshotDir, "v6_english_confirm_chat.png");
  const selectorsChecked = [];
  const allowCliFallback = shouldAllowCliDurableFallback(id);
  const strictUiOnly = shouldEnforceStrictUiEvidence(id);
  const rejectVisibleUiNoise = shouldRejectVisibleUiNoise(id);
  const { confirmText, marker, writePrompt, recallPrompt } = acceptanceEnglishConfirmSpec;
  const assistantLabel = profileMatrixMode ? "Onboarding Doc Test" : "Scenario alpha";

  try {
    const chatUrl = new URL("/chat", baseUrl.replace(/#.*$/, ""));
    chatUrl.searchParams.set("session", acceptanceEnglishChatSession);
    selectorsChecked.push("route:/chat(en-confirm)");
    selectorsChecked.push(`strict_ui_mode:${strictUiOnly}`);
    selectorsChecked.push(`reject_visible_ui_noise:${rejectVisibleUiNoise}`);
    await page.goto(chatUrl.toString(), { waitUntil: "networkidle", timeout: VERIFICATION_TIMEOUT_MS });
    await page.waitForTimeout(1_400);
    await clickConnectIfPresent(page);

    const inputLocator = getChatInputLocator(page);
    selectorsChecked.push("selector:chat-input(en-confirm)");

    const confirmCountBefore = await countOccurrences(page, confirmText);
    const assistantLabelCountBefore = await countOccurrences(page, assistantLabel);
    const toolOutputBefore = await countOccurrences(page, "Tool output");
    const memoryLearnToolBefore = await countOccurrences(page, "memory_learn");

    await inputLocator.click();
    await inputLocator.fill(writePrompt);
    await page.keyboard.press("Enter");

    await page.waitForTimeout(2_000);
    const afterPromptConfirmCount = await countOccurrences(page, confirmText);
    const afterPromptAssistantLabelCount = await countOccurrences(page, assistantLabel);
    const afterPromptToolOutputCount = await countOccurrences(page, "Tool output");
    const afterPromptMemoryLearnToolCount = await countOccurrences(page, "memory_learn");

    const confirmDeadline = Date.now() + VERIFICATION_TIMEOUT_MS;
    let confirmSeen = (
      afterPromptConfirmCount > confirmCountBefore
      && (
        afterPromptAssistantLabelCount > assistantLabelCountBefore ||
        afterPromptToolOutputCount > toolOutputBefore ||
        afterPromptMemoryLearnToolCount > memoryLearnToolBefore
      )
    );
    while (Date.now() < confirmDeadline) {
      const mainText = await page.locator("main").innerText();
      const confirmCount = mainText.split(confirmText).length - 1;
      const assistantLabelCount = mainText.split(assistantLabel).length - 1;
      const toolOutputCount = mainText.split("Tool output").length - 1;
      const memoryLearnToolCount = mainText.split("memory_learn").length - 1;
      if (confirmCount > confirmCountBefore) {
        if (
          confirmCount > afterPromptConfirmCount &&
          (
            assistantLabelCount > afterPromptAssistantLabelCount ||
            toolOutputCount > afterPromptToolOutputCount ||
            memoryLearnToolCount > afterPromptMemoryLearnToolCount
          )
        ) {
          confirmSeen = true;
          break;
        }
      }
      await page.waitForTimeout(1_000);
    }
    selectorsChecked.push(
      `confirm_seen:${confirmSeen}`,
      `confirm_count_before:${confirmCountBefore}`,
      `confirm_count_after_prompt:${afterPromptConfirmCount}`,
      `assistant_label_before:${assistantLabelCountBefore}`,
      `assistant_label_after_prompt:${afterPromptAssistantLabelCount}`,
    );

    await waitForMainTextToSettle(page);
    await inputLocator.click();
    await inputLocator.fill(recallPrompt);
    await page.keyboard.press("Enter");
    await page.waitForTimeout(2_000);
    const recallBaseline = await countOccurrences(page, marker);

    const recallDeadline = Date.now() + VERIFICATION_TIMEOUT_MS;
    let recallSeen = false;
    let recallActual = "";
    let recallCliEvidence = null;
    let lastRecallCliCheckAt = 0;
    while (Date.now() < recallDeadline) {
      const mainText = await page.locator("main").innerText();
      const markerCount = mainText.split(marker).length - 1;
      if (markerCount > recallBaseline) {
        recallSeen = true;
        recallActual = `English confirmation marker "${marker}" was recalled.`;
        break;
      }
      if (allowCliFallback && Date.now() - lastRecallCliCheckAt >= 5_000) {
        lastRecallCliCheckAt = Date.now();
        recallCliEvidence = await collectCliMarkerEvidence(scenario, marker).catch(() => null);
        if (recallCliEvidence?.strongEvidence && (recallCliEvidence.searchFound || recallCliEvidence.statusFound)) {
          recallSeen = true;
          recallActual =
            `CLI confirmed English confirmation marker "${marker}"` +
            (recallCliEvidence.lastCaptureUri ? ` via ${recallCliEvidence.lastCaptureUri}` : "");
          break;
        }
      }
      await page.waitForTimeout(1_000);
    }
    if (!recallSeen && strictUiOnly) {
      const routeScanActual = await scanUiRoutesForMarker(
        page,
        baseUrl,
        chatUrl,
        marker,
        selectorsChecked,
        "en_recall",
      );
      if (routeScanActual) {
        recallSeen = true;
        recallActual = routeScanActual;
      }
    }
    selectorsChecked.push(`recall_seen:${recallSeen}`);

    const cliEvidence = recallCliEvidence || await collectCliMarkerEvidence(scenario, marker).catch(() => null);
    selectorsChecked.push(
      `cli_search_found:${cliEvidence?.searchFound ?? false}`,
      `cli_status_found:${cliEvidence?.statusFound ?? false}`,
    );

    const uiNoiseState = rejectVisibleUiNoise
      ? await scanVisibleUiNoiseOnChat(page, chatUrl, selectorsChecked, "v6")
      : { ok: true, hits: [] };

    await page.screenshot({ path: screenshotPath, fullPage: false });

    const finalConfirmCount = await countOccurrences(page, confirmText);
    const confirmSatisfied = confirmSeen || finalConfirmCount >= afterPromptConfirmCount + 1;
    selectorsChecked.push(`final_confirm_count:${finalConfirmCount}`);

    const pass = uiNoiseState.ok && confirmSatisfied && (
      recallSeen || (
        allowCliFallback &&
        Boolean(cliEvidence?.strongEvidence)
      )
    );
    return buildResult({
      id,
      name,
      setup: "Use a dedicated chat session for an English explicit remember request",
      action: 'Asked for a visible "Stored." confirmation, then requested recall of the same marker',
      expected: `The assistant visibly replies with "${confirmText}" in chat and later recalls "${marker}".`,
      actual: pass
        ? recallActual
        : [
            confirmSatisfied ? "" : `English minimal confirmation "${confirmText}" was not visibly rendered in chat.`,
            recallSeen || (allowCliFallback && cliEvidence?.strongEvidence)
              ? ""
              : `English confirmation marker "${marker}" was not recalled in chat.`,
            !allowCliFallback && cliEvidence?.strongEvidence
              ? `CLI saw durable evidence for "${marker}", but strict UI mode requires visible UI evidence.`
              : "",
            uiNoiseState.ok
              ? ""
              : `Visible chat reply still exposed raw memory tags or metadata noise: ${uiNoiseState.hits.join(", ")}.`,
          ].filter(Boolean).join(" "),
      pass_fail: pass ? "PASS" : "FAIL",
      screenshot_path: screenshotPath,
      selectors_checked: selectorsChecked,
    });
  } catch (err) {
    await page.screenshot({ path: screenshotPath, fullPage: false }).catch(() => {});
    return buildResult({
      id,
      name,
      setup: "Run English confirmation flow through chat",
      action: 'Send an English explicit remember request and check for the minimal "Stored." confirmation',
      expected: `A visible "${confirmText}" confirmation followed by marker recall.`,
      actual: `Error: ${err.message}`,
      pass_fail: "FAIL",
      screenshot_path: screenshotPath,
      selectors_checked: selectorsChecked,
    });
  }
}

/**
 * V7 – Short High-Value Session Recall
 *
 * Uses an isolated short chat session with a high-value workflow/preference prompt.
 * In isolated mode, the scenario runtime env can enable conservative early flush so
 * the verification can check both immediate recall and durable compact evidence.
 */
async function verifyV7(page, baseUrl, scenario) {
  const id = "V7";
  const name = "Short High-Value Session Recall";
  const screenshotPath = path.join(screenshotDir, "v7_short_high_value_recall.png");
  const allowCliFallback = shouldAllowCliDurableFallback(id);
  const strictUiOnly = shouldEnforceStrictUiEvidence(id);
  const selectorsChecked = [];
  const {
    confirmText,
    marker,
    writePrompt,
    recallPrompt,
  } = acceptanceShortHighValueSpec;

  try {
    const chatUrl = new URL("/chat", baseUrl.replace(/#.*$/, ""));
    chatUrl.searchParams.set("session", `${acceptanceChatSession}-high-value`);
    selectorsChecked.push("route:/chat(high-value)");
    selectorsChecked.push(`strict_ui_mode:${strictUiOnly}`);
    await page.goto(chatUrl.toString(), { waitUntil: "networkidle", timeout: VERIFICATION_TIMEOUT_MS });
    await page.waitForTimeout(1_400);

    await clickConnectIfPresent(page);

    const inputLocator = getChatInputLocator(page);
    selectorsChecked.push("selector:chat-input(high-value)");
    const confirmCountBefore = await countOccurrences(page, confirmText);
    await inputLocator.fill(writePrompt);
    await inputLocator.press("Enter");
    await page.waitForTimeout(1_000);

    const afterPromptConfirmCount = await countOccurrences(page, confirmText);
    const confirmDeadline = Date.now() + VERIFICATION_TIMEOUT_MS;
    let confirmSeen = afterPromptConfirmCount > confirmCountBefore;
    while (Date.now() < confirmDeadline) {
      const mainText = await page.locator("body").innerText();
      const confirmCount = mainText.split(confirmText).length - 1;
      if (confirmCount > confirmCountBefore) {
        confirmSeen = true;
        break;
      }
      await page.waitForTimeout(1_000);
    }
    selectorsChecked.push(`confirm_seen:${confirmSeen}`);

    await inputLocator.fill(recallPrompt);
    await inputLocator.press("Enter");
    await page.waitForTimeout(1_000);

    const recallBaseline = await countOccurrences(page, marker);
    const recallDeadline = Date.now() + VERIFICATION_TIMEOUT_MS;
    let recallSeen = false;
    let recallActual = "";
    let cliEvidence = null;
    while (Date.now() < recallDeadline) {
      const bodyText = await page.locator("body").innerText();
      const markerCount = bodyText.split(marker).length - 1;
      if (markerCount > recallBaseline) {
        recallSeen = true;
        recallActual = `Short high-value marker "${marker}" was recalled in chat.`;
        break;
      }
      if (!cliEvidence && allowCliFallback && scenario) {
        cliEvidence = await collectCliMarkerEvidence(scenario, marker).catch(() => null);
        if (cliEvidence?.strongEvidence && (cliEvidence.searchFound || cliEvidence.statusFound)) {
          recallSeen = true;
          recallActual =
            `CLI confirmed short high-value marker "${marker}"` +
            (cliEvidence.lastCaptureUri ? ` via ${cliEvidence.lastCaptureUri}` : "");
          break;
        }
      }
      await page.waitForTimeout(1_000);
    }

    const statusPayload = cliEvidence?.statusResult?.payload || {};
    const lastCompactContext = statusPayload?.runtimeState?.lastCompactContext || null;
    const flushTrackerStats = statusPayload?.runtime?.sm_lite?.flush_tracker || {};
    const compactFlushed = Boolean(lastCompactContext?.flushed);
    const compactPersisted = Boolean(lastCompactContext?.dataPersisted);
    const compactSourceHash = String(lastCompactContext?.sourceHash || "");
    const compactReason = String(lastCompactContext?.reason || "");
    const flushResultsTotal = Number(flushTrackerStats?.flush_results_total || 0);
    const earlyFlushCount = Number(flushTrackerStats?.early_flush_count || 0);
    const dedupedRatio = Number(flushTrackerStats?.write_guard_deduped_ratio || 0);
    const statsLastSourceHash = String(flushTrackerStats?.last_source_hash || "");
    selectorsChecked.push(`recall_seen:${recallSeen}`);
    selectorsChecked.push(`compact_context_flushed:${compactFlushed}`);
    selectorsChecked.push(`compact_context_persisted:${compactPersisted}`);
    selectorsChecked.push(`compact_context_reason:${compactReason || "-"}`);
    selectorsChecked.push(`flush_results_total:${flushResultsTotal}`);
    selectorsChecked.push(`early_flush_count:${earlyFlushCount}`);
    selectorsChecked.push(`write_guard_deduped_ratio:${dedupedRatio}`);
    selectorsChecked.push(`stats_last_source_hash:${statsLastSourceHash ? "present" : "missing"}`);

    await page.screenshot({ path: screenshotPath, fullPage: false });

    const pass = confirmSeen
      && (recallSeen || (allowCliFallback && cliEvidence?.strongEvidence));

    return buildResult({
      id,
      name,
      setup: useCurrentHost
        ? "Current-host chat short session"
        : "Isolated chat short session with high-value early flush env enabled",
      action: "Send short high-value workflow preference, then immediately ask for recall",
      expected: `Visible confirmation plus recall of "${marker}", with compact-context runtime evidence in isolated mode.`,
      actual: pass
        ? [
            recallActual || `Short high-value marker "${marker}" was confirmed and recalled.`,
            `flush_results_total=${flushResultsTotal}`,
            `early_flush_count=${earlyFlushCount}`,
            `write_guard_deduped_ratio=${dedupedRatio}`,
            (!useCurrentHost && compactFlushed && compactPersisted && compactSourceHash)
              ? `compact_context observed (${compactReason || "ok"}, ${compactSourceHash.slice(0, 12)})`
              : (!useCurrentHost
                ? (
                    statsLastSourceHash
                      ? `status.flush_tracker last_source_hash=${statsLastSourceHash.slice(0, 12)}`
                      : "compact_context runtime evidence not surfaced on this chat path"
                  )
                : ""),
          ].filter(Boolean).join("; ")
        : [
            confirmSeen ? "" : `Confirmation "${confirmText}" was not visible.`,
            recallSeen || (allowCliFallback && cliEvidence?.strongEvidence)
              ? ""
              : `Marker "${marker}" was not recalled in chat or CLI evidence.`,
          ].filter(Boolean).join(" "),
      pass_fail: pass ? "PASS" : "FAIL",
      screenshot_path: screenshotPath,
      selectors_checked: selectorsChecked,
    });
  } catch (err) {
    await page.screenshot({ path: screenshotPath, fullPage: false }).catch(() => {});
    return buildResult({
      id,
      name,
      setup: "Run short high-value chat flow",
      action: "Write + immediate recall in a short session",
      expected: "Confirmation, recall, and compact-context evidence are visible",
      actual: `Error: ${err.message}`,
      pass_fail: "FAIL",
      screenshot_path: screenshotPath,
      selectors_checked: selectorsChecked,
    });
  }
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------
async function main() {
  await mkdir(screenshotDir, { recursive: true });

  const report = {
    generatedAt: new Date().toISOString(),
    reportPath,
    requestedProfile,
    strictUiRequested,
    strictCurrentHostUiMode,
    dashboardUrlSource,
    includeHighValueShortSession,
    setupArgs,
    ok: false,
    verifications: [],
    summary: { total: totalVerifications, pass: 0, fail: 0, skip: 0 },
  };

  // -----------------------------------------------------------------------
  // Gateway detection / scenario setup
  // -----------------------------------------------------------------------
  let scenario = null;
  let gateway = null;
  let dashboardBaseUrl = CONTROL_UI_URL;

  const shouldStartIsolated = forceIsolated || !useCurrentHost;

  if (useCurrentHost && !forceIsolated) {
    console.log("[acceptance] Using current host (OPENCLAW_ONBOARDING_USE_CURRENT_HOST=true)");
  } else {
    // Check if we should reuse an already-running gateway (only when not forced isolated)
    const alreadyRunning = !forceIsolated && await isGatewayReachable(CONTROL_UI_URL);
    if (alreadyRunning) {
      console.log("[acceptance] Gateway already reachable at", CONTROL_UI_URL);
    } else {
      // Start a fully isolated scenario with its own profile/workspace/gateway
      console.log(
        forceIsolated
          ? "[acceptance] ACCEPTANCE_FORCE_ISOLATED=true, starting isolated gateway..."
          : "[acceptance] Gateway not reachable, starting isolated gateway...",
      );
      try {
        // Disable ANSI colors so startGateway can detect the readiness signal
        process.env.NO_COLOR = "1";
        process.env.FORCE_COLOR = "0";
        if (
          process.env.OPENCLAW_CONFIG_PATH
          && !process.env.OPENCLAW_ONBOARDING_BASE_CONFIG_PATH
        ) {
          process.env.OPENCLAW_ONBOARDING_BASE_CONFIG_PATH = process.env.OPENCLAW_CONFIG_PATH;
        }
        scenario = await prepareScenario({
          name: scenarioName,
          port: SCENARIO_PORT,
          installPlugin: true,
          profile: requestedProfile,
          extraAgents: profileMatrixMode ? [] : ["alpha"],
          extraEnv: includeHighValueShortSession
            ? {
                RUNTIME_FLUSH_HIGH_VALUE_EARLY_ENABLED: "true",
                RUNTIME_FLUSH_HIGH_VALUE_MIN_EVENTS: "2",
                RUNTIME_FLUSH_HIGH_VALUE_MIN_CHARS: "120",
                RUNTIME_FLUSH_HIGH_VALUE_MIN_CHARS_CJK: "100",
                RUNTIME_FLUSH_MIN_EVENTS: "6",
                RUNTIME_FLUSH_TRIGGER_CHARS: "6000",
              }
            : {},
          setupArgs,
        });
        gateway = await startGateway(scenario);
        dashboardBaseUrl = await getDashboardUrl(scenario, {
          source: dashboardUrlSource === "scenario" ? "scenario-port" : "cli",
        });
        if (dashboardUrlSource !== "scenario") {
          const resolvedDashboardPort = Number.parseInt(
            new URL(dashboardBaseUrl.replace(/#.*$/, "")).port || "80",
            10,
          );
          if (resolvedDashboardPort !== SCENARIO_PORT) {
            throw new Error(
              `Dashboard URL port mismatch for isolated scenario: expected ${SCENARIO_PORT}, got ${resolvedDashboardPort} from ${dashboardBaseUrl}`,
            );
          }
        }
        console.log("[acceptance] Isolated gateway started on port", SCENARIO_PORT);
        console.log("[acceptance] Dashboard URL source:", dashboardUrlSource, "->", dashboardBaseUrl);
      } catch (err) {
        console.warn(
          `[acceptance] Could not start gateway: ${err.message}\n` +
            "All verifications will be marked SKIP.",
        );
        const skipReason = `Gateway not available and could not be started: ${err.message}`;
        for (const verification of verificationCatalog) {
          report.verifications.push(
            buildResult({
              id: verification.id,
              name: verification.name,
              setup: "Detect or start gateway",
              action: "Attempted prepareScenario + startGateway",
              expected: "Gateway reachable",
              actual: skipReason,
              pass_fail: "SKIP",
              screenshot_path: "",
              selectors_checked: [],
            }),
          );
          report.summary.skip += 1;
        }
        report.summary.total = totalVerifications;
        await writeJson(reportPath, report);
        console.log(JSON.stringify(report, null, 2));
        return;
      }
    }
  }

  // -----------------------------------------------------------------------
  // Launch browser
  // -----------------------------------------------------------------------
  const { chromium } = await loadPlaywright();
  const browser = await chromium.launch({ headless: true });
  let context = null;
  let page = null;
  const resetBrowserPage = async () => {
    if (context) {
      await context.close().catch(() => {});
    }
    context = null;
    page = null;
  };
  const ensurePage = async () => {
    if (page && !page.isClosed()) {
      return page;
    }
    await resetBrowserPage();
    context = await browser.newContext({
      viewport: { width: 1440, height: 900 },
    });
    page = await context.newPage();
    return page;
  };

  const restartIsolatedGateway = async () => {
    if (!scenario || useCurrentHost || !forceIsolated) {
      return false;
    }
    if (gateway) {
      await stopGateway(gateway).catch(() => {});
      gateway = null;
    }
    await resetBrowserPage();
    gateway = await startGateway(scenario);
    return true;
  };

  const runVerificationWithRetry = async (id, label, fn) => {
    const execute = async () => fn(await ensurePage(), dashboardBaseUrl, scenario);
    let result = await execute();
    if (result.pass_fail === "PASS" || !scenario || useCurrentHost || !forceIsolated || id === "V1") {
      return result;
    }

    const gatewayHealthy = await isGatewayReachable(dashboardBaseUrl);
    const actualText = String(result.actual || "").toLowerCase();
    const retryableFailure =
      !gatewayHealthy
      || actualText.includes("connection refused")
      || actualText.includes("timed out")
      || actualText.includes("timeout")
      || actualText.includes("locator.innertext")
      || actualText.includes("marker not recalled")
      || actualText.includes("marker was not found after confirmation");
    if (!retryableFailure) {
      return result;
    }

    console.log(`  -> retrying ${id} after isolated gateway refresh`);
    await restartIsolatedGateway();
    result = await execute();
    result.selectors_checked = [
      ...(Array.isArray(result.selectors_checked) ? result.selectors_checked : []),
      "retry_after_gateway_restart:true",
    ];
    return result;
  };

  try {
    // --- V1 ---
    console.log("[acceptance] V1: Plugin Status Visible");
    const v1 = await verifyV1(await ensurePage(), dashboardBaseUrl);
    report.verifications.push(v1);
    await persistProgressReport(report);
    console.log(`  -> ${v1.pass_fail}: ${v1.actual}`);

    // --- V2 ---
    console.log("[acceptance] V2: Memory Write + Recall in Chat");
    const v2 = await runVerificationWithRetry("V2", "Memory Write + Recall in Chat", verifyV2);
    report.verifications.push(v2);
    await persistProgressReport(report);
    console.log(`  -> ${v2.pass_fail}: ${v2.actual}`);

    // --- V3 ---
    console.log("[acceptance] V3: Memory System Integration Evidence");
    const v3 = await runVerificationWithRetry("V3", "Memory System Integration Evidence", verifyV3);
    report.verifications.push(v3);
    await persistProgressReport(report);
    console.log(`  -> ${v3.pass_fail}: ${v3.actual}`);

    // --- V4 ---
    console.log("[acceptance] V4: Guarded Write Confirm + Force Save");
    const v4 = await runVerificationWithRetry("V4", "Guarded Write Confirm + Force Save", verifyV4);
    report.verifications.push(v4);
    await persistProgressReport(report);
    console.log(`  -> ${v4.pass_fail}: ${v4.actual}`);

    // --- V5 ---
    console.log("[acceptance] V5: Chinese Minimal Confirmation");
    const v5 = await runVerificationWithRetry("V5", "Chinese Minimal Confirmation", verifyV5);
    report.verifications.push(v5);
    await persistProgressReport(report);
    console.log(`  -> ${v5.pass_fail}: ${v5.actual}`);

    // --- V6 ---
    console.log("[acceptance] V6: English Minimal Confirmation");
    const v6 = await runVerificationWithRetry("V6", "English Minimal Confirmation", verifyV6);
    report.verifications.push(v6);
    await persistProgressReport(report);
    console.log(`  -> ${v6.pass_fail}: ${v6.actual}`);

    if (includeHighValueShortSession) {
      console.log("[acceptance] V7: Short High-Value Session Recall");
      const v7 = await runVerificationWithRetry("V7", "Short High-Value Session Recall", verifyV7);
      report.verifications.push(v7);
      await persistProgressReport(report);
      console.log(`  -> ${v7.pass_fail}: ${v7.actual}`);
    }
  } finally {
    await resetBrowserPage();
    await browser.close().catch(() => {});
    if (gateway) {
      await stopGateway(gateway);
    }
  }

  // -----------------------------------------------------------------------
  // Summarize
  // -----------------------------------------------------------------------
  report.summary.pass = 0;
  report.summary.fail = 0;
  report.summary.skip = 0;
  for (const v of report.verifications) {
    if (v.pass_fail === "PASS") report.summary.pass += 1;
    else if (v.pass_fail === "SKIP") report.summary.skip += 1;
    else report.summary.fail += 1;
  }
  report.ok = report.summary.fail === 0;
  report.dashboardBaseUrl = dashboardBaseUrl;
  await writeJson(reportPath, report);
  console.log(JSON.stringify(report, null, 2));

  if (!report.ok) {
    process.exitCode = 1;
  }
}

main().catch(async (err) => {
  const failure = {
    generatedAt: new Date().toISOString(),
    reportPath,
    ok: false,
    error: String(err?.stack || err),
    verifications: [],
    summary: { total: 6, pass: 0, fail: 0, skip: 0 },
  };
  await mkdir(screenshotDir, { recursive: true }).catch(() => {});
  await writeJson(reportPath, failure).catch(() => {});
  console.error(failure.error);
  process.exitCode = 1;
});

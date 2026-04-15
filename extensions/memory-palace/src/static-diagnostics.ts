import path from "node:path";
import type {
  DiagnosticCheck,
  HostPlatform,
  PluginConfig,
  PluginRuntimeSnapshot,
} from "./types.js";
import {
  isRecord,
  readBoolean,
  readFlexibleNumber,
  readString,
} from "./utils.js";

function hasFreshRuntimeState(runtime: PluginRuntimeSnapshot): boolean {
  const captureCount = Object.values(runtime.captureLayerCounts).reduce((total, value) => {
    return total + (typeof value === "number" && Number.isFinite(value) ? value : 0);
  }, 0);
  return (
    !runtime.lastCapturePath?.uri &&
    !runtime.lastFallbackPath &&
    !runtime.lastRuleCaptureDecision &&
    !runtime.lastCompactContext &&
    captureCount <= 0 &&
    runtime.recentCaptureLayers.length === 0
  );
}

function compactContextReasonIsBenign(reason: string | undefined): boolean {
  return reason === "threshold_not_reached" || reason === "no_pending_events";
}

function requiresTypedLifecycleHooks(config: PluginConfig): boolean {
  return (
    (config.profileMemory.enabled && config.profileMemory.injectBeforeAgentStart) ||
    config.autoRecall.enabled ||
    config.hostBridge.enabled ||
    config.visualMemory.enabled ||
    config.autoCapture.enabled ||
    config.capturePipeline.captureAssistantDerived ||
    config.smartExtraction.enabled ||
    config.reflection.enabled
  );
}

export function collectPhaseRuntimeChecks(
  config: PluginConfig,
  runtime: PluginRuntimeSnapshot,
): DiagnosticCheck[] {
  const checks: DiagnosticCheck[] = [];
  const freshRuntime = hasFreshRuntimeState(runtime);
  const hookRequired = requiresTypedLifecycleHooks(config);
  const typedHooksUnavailable =
    runtime.lastFallbackPath?.stage === "hook_registration" &&
    runtime.lastFallbackPath.reason === "typed_hook_api_unavailable";
  const profileRequiresPhase4 =
    config.smartExtraction.effectiveProfile === "c" || config.smartExtraction.effectiveProfile === "d";
  checks.push({
    id: "host-hook-api",
    status: hookRequired ? (typedHooksUnavailable ? "fail" : "pass") : "pass",
    message: hookRequired
      ? typedHooksUnavailable
        ? "Host plugin API does not expose typed lifecycle hooks (`api.on`); automatic recall/capture/visual-harvest are unavailable."
        : "Host plugin API exposes the typed lifecycle hooks required for automatic recall/capture/visual-harvest."
      : "No automatic hook-driven features are enabled that require typed lifecycle hooks.",
    action:
      hookRequired && typedHooksUnavailable
        ? "Upgrade OpenClaw to >= 2026.3.2 and rerun verify/doctor after reloading the plugin. Until then, use explicit `openclaw memory-palace ...` commands."
        : undefined,
    details:
      hookRequired && typedHooksUnavailable
        ? runtime.lastFallbackPath
        : {
            required: hookRequired,
          },
  });
  const smartExtractionMessage = config.smartExtraction.enabled
    ? `Smart extraction is enabled (${config.smartExtraction.effectiveMode}, min ${config.smartExtraction.minConversationMessages} messages, timeout ${config.smartExtraction.timeoutMs}ms).`
    : "Smart extraction is disabled by config.";
  checks.push({
    id: "smart-extraction",
    status:
      !config.smartExtraction.enabled
        ? profileRequiresPhase4
          ? "warn"
          : "pass"
        : config.smartExtraction.modelAvailable
          ? "pass"
          : "warn",
    message: smartExtractionMessage,
    action:
      !config.smartExtraction.enabled
        ? profileRequiresPhase4
          ? "Enable plugins.entries.memory-palace.config.smartExtraction.enabled to turn on Phase 4 extraction for C/D."
          : undefined
        : config.smartExtraction.modelAvailable
          ? undefined
          : "Provide SMART_EXTRACTION_LLM_* or compatible WRITE_GUARD/OPENAI env values so smart extraction does not immediately degrade to B.",
    details: {
      mode: config.smartExtraction.mode,
      effectiveMode: config.smartExtraction.effectiveMode,
      modelAvailable: config.smartExtraction.modelAvailable,
      modelName: config.smartExtraction.modelName ?? null,
      circuit: runtime.smartExtractionCircuit,
    },
  });
  checks.push({
    id: "reconcile-mode",
    status: config.reconcile.enabled ? "pass" : profileRequiresPhase4 ? "warn" : "pass",
    message: config.reconcile.enabled
      ? `Reconcile is enabled (${config.reconcile.profileMergePolicy}, similarity ${config.reconcile.similarityThreshold.toFixed(2)}, actions ${config.reconcile.actions.join("/")}).`
      : "Reconcile is disabled by config.",
    action: config.reconcile.enabled
      ? undefined
      : profileRequiresPhase4
        ? "Enable plugins.entries.memory-palace.config.reconcile.enabled to apply Phase 4 merge/update decisions."
        : undefined,
    details: {
      profileMergePolicy: config.reconcile.profileMergePolicy,
      eventMergePolicy: config.reconcile.eventMergePolicy,
      similarityThreshold: config.reconcile.similarityThreshold,
      actions: config.reconcile.actions,
    },
  });
  checks.push({
    id: "last-capture-path",
    status: runtime.lastCapturePath?.uri ? "pass" : freshRuntime ? "pass" : "warn",
    message: runtime.lastCapturePath?.uri
      ? `Last capture path: ${runtime.lastCapturePath.uri}.`
      : freshRuntime
        ? "No recent capture path is recorded yet. This is expected on a fresh runtime before the first real capture turn."
        : "No recent capture path is recorded yet.",
    action: runtime.lastCapturePath?.uri
      ? undefined
      : freshRuntime
        ? undefined
        : "Run a real capture turn, then rerun verify or doctor to inspect the latest capture path.",
    details: runtime.lastCapturePath ?? null,
  });
  checks.push({
    id: "last-fallback-path",
    status: runtime.lastFallbackPath ? "warn" : "pass",
    message: runtime.lastFallbackPath
      ? `Last fallback path: ${runtime.lastFallbackPath.reason}.`
      : "No recent fallback path is recorded.",
    action: runtime.lastFallbackPath
      ? "Inspect the fallback reason, then verify model availability and runtime health before rerunning the capture path."
      : undefined,
    details: runtime.lastFallbackPath ?? null,
  });
  checks.push({
    id: "last-rule-capture-decision",
    status: runtime.lastRuleCaptureDecision
      ? runtime.lastRuleCaptureDecision.decision === "skipped"
        ? "warn"
        : "pass"
      : freshRuntime
        ? "pass"
        : "warn",
    message: runtime.lastRuleCaptureDecision
      ? runtime.lastRuleCaptureDecision.decision === "captured"
        ? `Last rule capture stored ${runtime.lastRuleCaptureDecision.category ?? "fact"} at ${runtime.lastRuleCaptureDecision.uri ?? "n/a"}.`
        : runtime.lastRuleCaptureDecision.decision === "pending"
          ? `Last rule capture stored pending ${runtime.lastRuleCaptureDecision.category ?? "candidate"} at ${runtime.lastRuleCaptureDecision.uri ?? "n/a"}.`
          : `Last rule capture skipped (${runtime.lastRuleCaptureDecision.reason}).`
      : freshRuntime
        ? "No recent rule-capture decision is recorded yet. This is expected on a fresh runtime before the first real capture turn."
        : "No recent rule-capture decision is recorded yet.",
    action: runtime.lastRuleCaptureDecision
      ? runtime.lastRuleCaptureDecision.decision === "skipped"
        ? "Inspect the skip reason to decide whether the message should become a stable fact, a pending reminder/event, or remain uncaptured."
        : undefined
      : freshRuntime
        ? undefined
        : "Run a real capture turn, then rerun status or doctor to inspect the latest rule-capture decision.",
    details: runtime.lastRuleCaptureDecision ?? null,
  });
  const compactContextActive =
    config.reflection.enabled && config.reflection.source === "compact_context";
  const compactContextReason = readString(runtime.lastCompactContext?.reason);
  const compactContextFlushed = runtime.lastCompactContext?.flushed === true;
  const compactContextPersisted = runtime.lastCompactContext?.dataPersisted === true;
  checks.push({
    id: "last-compact-context",
    status: !compactContextActive
      ? "pass"
      : runtime.lastCompactContext
        ? compactContextFlushed || compactContextReasonIsBenign(compactContextReason)
          ? "pass"
          : "warn"
        : freshRuntime
          ? "pass"
          : "warn",
    message: !compactContextActive
      ? "compact_context reflection is not the active reflection source."
      : runtime.lastCompactContext
        ? compactContextFlushed
          ? compactContextPersisted
            ? `Last compact_context persisted a durable summary at ${runtime.lastCompactContext.uri ?? "n/a"}.`
            : `Last compact_context completed without persisting a new durable summary (${compactContextReason ?? "unknown_reason"}).`
          : compactContextReasonIsBenign(compactContextReason)
            ? `Last compact_context did not need to flush (${compactContextReason}).`
            : `Last compact_context did not flush (${compactContextReason ?? "unknown_reason"}).`
        : freshRuntime
          ? "No recent compact_context result is recorded yet. This is expected before the first compact_context reflection run."
          : "No recent compact_context result is recorded yet.",
    action: !compactContextActive
      ? undefined
      : runtime.lastCompactContext
        ? compactContextFlushed || compactContextReasonIsBenign(compactContextReason)
          ? undefined
          : "Inspect compact_context write_guard / transport state, then rerun the reflection flow."
        : freshRuntime
          ? undefined
          : "Run a compact_context reflection turn, then rerun verify or doctor to inspect the result.",
    details: runtime.lastCompactContext ?? null,
  });
  return checks;
}

function trimTrailingPathSeparators(inputPath: string): string {
  let normalized = inputPath.replace(/\\/g, "/");
  while (normalized.length > 1 && normalized.endsWith("/")) {
    normalized = normalized.slice(0, -1);
  }
  return normalized;
}

function flipAsciiCase(value: string): string {
  return value.replace(/[A-Za-z]/g, (char) =>
    char === char.toLowerCase() ? char.toUpperCase() : char.toLowerCase(),
  );
}

function usesCaseInsensitiveFilesystem(
  samplePath: string,
  currentHostPlatform: HostPlatform,
  pathExists: (inputPath: string) => boolean,
): boolean {
  if (currentHostPlatform === "windows") {
    return true;
  }
  const dirname = path.dirname(samplePath);
  const basename = path.basename(samplePath);
  const toggledBasename = flipAsciiCase(basename);
  if (!basename || toggledBasename === basename) {
    return false;
  }
  return pathExists(path.join(dirname, toggledBasename));
}

function pathCompareVariants(
  inputPath: string,
  currentHostPlatform: HostPlatform,
  caseInsensitiveFilesystem: boolean,
): Set<string> {
  const variants = new Set<string>();
  const addVariant = (candidate: string) => {
    const normalized = trimTrailingPathSeparators(candidate);
    if (!normalized) {
      return;
    }
    variants.add(normalized);
    if (caseInsensitiveFilesystem) {
      variants.add(normalized.toLowerCase());
    }
  };
  addVariant(path.normalize(inputPath));
  addVariant(path.resolve(inputPath));
  return variants;
}

function configuredLoadPathsIncludePlugin(
  loadPaths: string[],
  configPath: string,
  pluginExtensionRoot: string,
  currentHostPlatform: HostPlatform,
  pathExists: (inputPath: string) => boolean,
): boolean {
  const caseInsensitiveFilesystem = usesCaseInsensitiveFilesystem(
    configPath,
    currentHostPlatform,
    pathExists,
  );
  const expectedVariants = pathCompareVariants(
    pluginExtensionRoot,
    currentHostPlatform,
    caseInsensitiveFilesystem,
  );
  return loadPaths.some((entry) => {
    const resolved = path.isAbsolute(entry) ? entry : path.resolve(path.dirname(configPath), entry);
    for (const variant of pathCompareVariants(resolved, currentHostPlatform, caseInsensitiveFilesystem)) {
      if (expectedVariants.has(variant)) {
        return true;
      }
    }
    return false;
  });
}

export function collectStaticHostConfigChecks(
  config: PluginConfig,
  options: {
    configPath?: string;
    currentHostPlatform: HostPlatform;
    parseConfigFile: (configPath: string) => unknown;
    pathExists?: (inputPath: string) => boolean;
    pluginExtensionRoot: string;
  },
): DiagnosticCheck[] {
  const pathExists = options.pathExists ?? (() => true);
  const configPath = readString(options.configPath);
  if (!configPath) {
    return [
      {
        id: "host-config-path",
        status: "warn",
        message: "OPENCLAW_CONFIG_PATH is unavailable; skipped host config checks.",
        action:
          "Export OPENCLAW_CONFIG_PATH or use the installer/wrapper commands that inject it before using doctor/verify.",
      },
    ];
  }

  if (!pathExists(configPath)) {
    return [
      {
        id: "host-config-path",
        status: "fail",
        message: "OPENCLAW_CONFIG_PATH points to a missing file.",
        action: `Create or restore the OpenClaw config at ${configPath}.`,
        details: configPath,
      },
    ];
  }

  let parsed: unknown;
  try {
    parsed = options.parseConfigFile(configPath);
  } catch (error) {
    return [
      {
        id: "host-config-parse",
        status: "warn",
        message: "OpenClaw config is not valid JSON; skipped plugin wiring checks.",
        action: "Run `openclaw config validate` or repair the config file syntax.",
        details: error instanceof Error ? error.message : String(error),
      },
    ];
  }

  const configRecord = isRecord(parsed) ? parsed : {};
  const plugins = isRecord(configRecord.plugins) ? configRecord.plugins : {};
  const allow = Array.isArray(plugins.allow) ? plugins.allow.filter((entry) => typeof entry === "string") : [];
  const load = isRecord(plugins.load) ? plugins.load : {};
  const loadPaths = Array.isArray(load.paths) ? load.paths.filter((entry) => typeof entry === "string") : [];
  const slots = isRecord(plugins.slots) ? plugins.slots : {};
  const entries = isRecord(plugins.entries) ? plugins.entries : {};
  const pluginEntry = isRecord(entries["memory-palace"]) ? entries["memory-palace"] : {};
  const pluginConfig = isRecord(pluginEntry.config) ? pluginEntry.config : {};
  const hasPluginLoadPath = configuredLoadPathsIncludePlugin(
    loadPaths,
    configPath,
    options.pluginExtensionRoot,
    options.currentHostPlatform,
    pathExists,
  );

  return [
    {
      id: "host-config-path",
      status: "pass",
      message: `OpenClaw config path detected: ${configPath}.`,
    },
    {
      id: "host-allow",
      status: allow.includes("memory-palace") ? "pass" : "fail",
      message: allow.includes("memory-palace")
        ? "`plugins.allow` trusts `memory-palace`."
        : "`plugins.allow` is missing `memory-palace`.",
      action: allow.includes("memory-palace") ? undefined : "Add `memory-palace` to `plugins.allow` or rerun the installer.",
    },
    {
      id: "host-load-paths",
      status: hasPluginLoadPath ? "pass" : "warn",
      message: hasPluginLoadPath
        ? "`plugins.load.paths` includes the resolved plugin path."
        : "`plugins.load.paths` does not include the resolved plugin path.",
      action: hasPluginLoadPath
        ? undefined
        : `Ensure \`${options.pluginExtensionRoot}\` is present in \`plugins.load.paths\`.`,
      details: loadPaths,
    },
    {
      id: "host-slot-memory",
      status: readString(slots.memory) === "memory-palace" ? "pass" : "fail",
      message:
        readString(slots.memory) === "memory-palace"
          ? "`plugins.slots.memory` points to `memory-palace`."
          : "`plugins.slots.memory` is not set to `memory-palace`.",
      action:
        readString(slots.memory) === "memory-palace"
          ? undefined
          : "Set `plugins.slots.memory` to `memory-palace` before retrying.",
    },
    {
      id: "host-entry-enabled",
      status: readBoolean(pluginEntry.enabled) !== false ? "pass" : "fail",
      message:
        readBoolean(pluginEntry.enabled) !== false
          ? "`plugins.entries.memory-palace` is enabled."
          : "`plugins.entries.memory-palace` is disabled.",
      action:
        readBoolean(pluginEntry.enabled) !== false
          ? undefined
          : "Set `plugins.entries.memory-palace.enabled` to true.",
    },
    {
      id: "host-transport-match",
      status: readString(pluginConfig.transport) === config.transport ? "pass" : "warn",
      message:
        readString(pluginConfig.transport) === config.transport
          ? "Host config transport matches the resolved plugin transport."
          : "Host config transport differs from the resolved plugin transport.",
      action:
        readString(pluginConfig.transport) === config.transport
          ? undefined
          : "Reinstall or update `plugins.entries.memory-palace.config.transport` to match the intended transport.",
      details: {
        host: readString(pluginConfig.transport),
        resolved: config.transport,
      },
    },
  ];
}

export function buildDoctorActions(
  config: PluginConfig,
  report: {
    checks: Array<{
      id?: string;
      name?: string;
      status: string;
      action?: string;
    }>;
  },
  options: {
    currentHostPlatform: HostPlatform;
    defaultStdioWrapper: string;
    defaultWindowsMcpWrapper: string;
  },
): string[] {
  const actions = new Set<string>();
  const failed = new Set(
    report.checks
      .filter((entry) => entry.status === "fail" || entry.status === "FAIL")
      .map((entry) => entry.id ?? entry.name ?? ""),
  );
  const warned = new Set(
    report.checks
      .filter((entry) => entry.status === "warn" || entry.status === "WARN")
      .map((entry) => entry.id ?? entry.name ?? ""),
  );

  if (failed.has("stdio-wrapper") || failed.has("stdio_wrapper")) {
    const wrapperPath =
      options.currentHostPlatform === "windows"
        ? options.defaultWindowsMcpWrapper
        : options.defaultStdioWrapper;
    actions.add(`Ensure ${wrapperPath} exists or override stdio.command.`);
  }
  if (failed.has("host-allow") || failed.has("host_allow")) {
    actions.add("Add `memory-palace` to `plugins.allow`, or rerun the installer.");
  }
  if (warned.has("host-load-paths") || warned.has("host_load_paths")) {
    actions.add("Ensure `plugins.load.paths` contains the resolved plugin install root.");
  }
  if (failed.has("host-slot-memory") || failed.has("host_slot_memory")) {
    actions.add("Set `plugins.slots.memory` to `memory-palace` before retrying.");
  }
  if (failed.has("host-entry-enabled") || failed.has("host_entry_enabled")) {
    actions.add("Enable `plugins.entries.memory-palace.enabled` before retrying.");
  }
  if (warned.has("bundled-skill") || failed.has("bundled-skill")) {
    actions.add("Repack/reinstall the plugin so the bundled OpenClaw skill directory is available.");
  }
  if (warned.has("visual-auto-harvest") || failed.has("visual-auto-harvest")) {
    actions.add("Enable visualMemory.enabled or keep using explicit `memory_store_visual` for long-term image records.");
  }
  if (warned.has("profile-memory") || failed.has("profile-memory")) {
    actions.add("Enable profileMemory.enabled and keep injectBeforeAgentStart=true to prepend stable identity / preferences / workflow blocks.");
  }
  if (warned.has("auto-recall") || failed.has("auto-recall")) {
    actions.add("Enable autoRecall.enabled to restore the default pre-response durable recall path.");
  }
  if (warned.has("auto-capture") || failed.has("auto-capture")) {
    actions.add("Enable autoCapture.enabled to restore the default post-turn durable capture path.");
  }
  if (warned.has("host-bridge") || failed.has("host-bridge")) {
    actions.add("Enable hostBridge.enabled so host USER.md / MEMORY.md / memory/*.md hits can backfill plugin memory after recall misses.");
  }
  if (warned.has("assistant-derived") || failed.has("assistant-derived")) {
    actions.add("Enable capturePipeline.captureAssistantDerived to persist quote-grounded workflow candidates from multi-turn conversations.");
  }
  if (warned.has("smart-extraction") || failed.has("smart-extraction")) {
    actions.add("Provide a compatible LLM endpoint/model and enable smartExtraction so Profile C/D can extract stable long-term facts.");
  }
  if (warned.has("reconcile-mode") || failed.has("reconcile-mode")) {
    actions.add("Enable reconcile.enabled so smart-extracted facts can update or skip existing durable records.");
  }
  if (
    warned.has("stdio-backend-python") ||
    warned.has("backend_venv") ||
    failed.has("stdio-backend-python") ||
    failed.has("backend_venv")
  ) {
    actions.add("Run setup again to create the dedicated runtime venv before using stdio transport.");
  }
  if (warned.has("sse-url") || warned.has("sse_url") || (config.transport === "sse" && failed.has("index-status"))) {
    actions.add("Provide a reachable SSE URL and re-run `openclaw memory-palace verify --json`.");
  }
  if (failed.has("index-status") || failed.has("status")) {
    actions.add("Run `openclaw memory-palace status --json` to inspect the normalized transport error.");
    actions.add("Re-run `openclaw memory-palace verify --json` after applying the transport fix.");
  }
  if (actions.size === 0) {
    actions.add("No blocking issues detected.");
  }

  return Array.from(actions);
}

export function buildLegacyDoctorActions(
  report: { checks: Array<{ name: string; status: "PASS" | "WARN" | "FAIL"; summary: string }> },
): string[] {
  const actions: string[] = [];
  const statusByName = new Map(report.checks.map((item) => [item.name, item.status]));
  if (statusByName.get("backend_venv") === "WARN" || statusByName.get("backend_venv") === "FAIL") {
    actions.push("Run setup again to create the dedicated runtime venv before relying on stdio transport.");
  }
  if (statusByName.get("status") === "FAIL") {
    actions.push("Run `openclaw memory-palace status --json` to inspect the normalized transport error.");
  }
  if (statusByName.get("status") === "FAIL" || statusByName.get("search") === "FAIL") {
    actions.push("Re-run `openclaw memory-palace verify --json` after fixing transport or backend health.");
  }
  if (actions.length === 0) {
    actions.push("No blocking follow-up action was generated.");
  }
  return actions;
}

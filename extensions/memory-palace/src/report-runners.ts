import type {
  MemoryPalaceClientDiagnostics,
  MemoryPalaceMcpClient,
} from "./client.js";
import type {
  DiagnosticCheck,
  DiagnosticReport,
  JsonRecord,
  MemorySearchResult,
  PluginConfig,
  PluginRuntimeSnapshot,
} from "./types.js";
import { isRecord, readFlexibleNumber } from "./utils.js";

export type LegacyVerifyCheck = {
  name: string;
  status: "PASS" | "WARN" | "FAIL";
  summary: string;
  detail?: unknown;
};

type LegacyRuntimeClient = {
  activeTransportKind?: string | null;
  indexStatus?: () => Promise<unknown>;
  searchMemory?: (args: Record<string, unknown>) => Promise<unknown>;
  readMemory?: (args: Record<string, unknown>) => Promise<unknown>;
};

type LegacyVerifyRuntime = {
  run<T>(run: (client: LegacyRuntimeClient) => Promise<T>): Promise<T>;
  describeTransportPlan?: () => { fallbackOrder?: string[] };
  diagnostics?: () => unknown;
};

type ReportClient = LegacyRuntimeClient & {
  healthCheck: (force?: boolean) => Promise<{
    ok: boolean;
    transport?: string | null;
    error?: string;
    diagnostics?: unknown;
  }>;
};

type ReportSession = {
  withClient<T>(run: (client: ReportClient) => Promise<T>): Promise<T>;
  diagnostics?: MemoryPalaceClientDiagnostics;
  persistClient: MemoryPalaceMcpClient;
};

type HostWorkspaceHitLike = {
  citation: string;
  score: number;
  snippet: string;
};

type NormalizedSearchPayload = {
  results: MemorySearchResult[];
  provider: string;
  model?: string;
  mode?: string;
  degraded: boolean;
  backendMethod?: string;
  intent?: string;
  strategyTemplate?: string;
  disabled?: boolean;
  unavailable?: boolean;
  error?: string;
  raw: JsonRecord;
};

type ReadPayload = {
  text: string;
  selection?: unknown;
  degraded?: boolean;
  error?: string;
};

function readCountValue(value: unknown): number | undefined {
  return readFlexibleNumber(value);
}

function isFreshRuntimeIndexStatus(value: unknown): boolean {
  if (!isRecord(value)) {
    return false;
  }
  const counts = isRecord(value.counts) ? value.counts : {};
  const activeMemories = readCountValue(counts.active_memories) ?? 0;
  const memoryChunks = readCountValue(counts.memory_chunks) ?? 0;
  return activeMemories <= 0 && memoryChunks <= 0;
}

function extractFreshRuntimeFromChecks(checks: DiagnosticCheck[]): boolean {
  const indexStatusCheck = checks.find((entry) => entry.id === "index-status");
  return isFreshRuntimeIndexStatus(indexStatusCheck?.details);
}

function inferCaptureLayerFromResult(result: MemorySearchResult): string | undefined {
  const normalizedPath = String(result.path ?? "").replace(/\\/g, "/").toLowerCase();
  const normalizedSnippet = String(result.snippet ?? "").toLowerCase();
  if (
    normalizedPath.includes("/captured/llm-extracted/") ||
    normalizedSnippet.includes("source_mode: llm_extracted") ||
    normalizedSnippet.includes("capture_layer: smart_extraction")
  ) {
    return "llm_extracted";
  }
  if (
    normalizedPath.includes("/captured/assistant-derived/") ||
    normalizedSnippet.includes("source_mode: assistant_derived")
  ) {
    return "assistant_derived";
  }
  if (
    normalizedPath.includes("/captured/host-bridge/") ||
    normalizedSnippet.includes("source_mode: host_workspace_import")
  ) {
    return "bridge";
  }
  if (
    normalizedPath.includes("/captured/rule-capture/") ||
    normalizedPath.includes("/captured/pending/") ||
    normalizedSnippet.includes("source_mode: rule_capture") ||
    normalizedSnippet.includes("capture_layer: auto_capture")
  ) {
    return "rule";
  }
  return undefined;
}

function inferCaptureLayerCountsFromSearchResults(
  results: MemorySearchResult[],
): Record<string, number> {
  const counts: Record<string, number> = {};
  for (const result of results) {
    const layer = inferCaptureLayerFromResult(result);
    if (!layer) {
      continue;
    }
    counts[layer] = (counts[layer] ?? 0) + 1;
  }
  return counts;
}

function normalizeCaptureLayerLabel(
  layer: string | undefined,
  sourceMode: string | undefined,
  uri: string | undefined,
): string | undefined {
  const normalizedLayer = String(layer ?? "").trim().toLowerCase();
  const normalizedSourceMode = String(sourceMode ?? "").trim().toLowerCase();
  const normalizedUri = String(uri ?? "").replace(/\\/g, "/").toLowerCase();
  if (
    normalizedSourceMode === "llm_extracted" ||
    normalizedLayer === "smart_extraction" ||
    normalizedLayer === "llm_extracted" ||
    normalizedUri.includes("/captured/llm-extracted/")
  ) {
    return "llm_extracted";
  }
  if (normalizedSourceMode === "assistant_derived" || normalizedLayer === "assistant_derived") {
    return "assistant_derived";
  }
  if (
    normalizedSourceMode === "host_workspace_import" ||
    normalizedLayer === "bridge" ||
    normalizedUri.includes("/captured/host-bridge/")
  ) {
    return "bridge";
  }
  if (
    normalizedSourceMode === "rule_capture" ||
    normalizedLayer.startsWith("auto_capture") ||
    normalizedLayer === "rule" ||
    normalizedUri.includes("/captured/rule-capture/") ||
    normalizedUri.includes("/captured/pending/")
  ) {
    return "rule";
  }
  return normalizedLayer || undefined;
}

function mergeRuntimeCaptureLayerCounts(
  baseCounts: Record<string, number>,
  runtimeSnapshot: PluginRuntimeSnapshot,
): Record<string, number> {
  const merged: Record<string, number> = { ...baseCounts };
  const mark = (
    layer: string | undefined,
    sourceMode: string | undefined,
    uri: string | undefined,
  ) => {
    const normalized = normalizeCaptureLayerLabel(layer, sourceMode, uri);
    if (!normalized) {
      return;
    }
    merged[normalized] = Math.max(merged[normalized] ?? 0, 1);
  };
  for (const entry of runtimeSnapshot.recentCaptureLayers) {
    mark(entry.layer, entry.sourceMode, entry.uri);
  }
  if (runtimeSnapshot.lastCapturePath) {
    mark(
      runtimeSnapshot.lastCapturePath.layer,
      runtimeSnapshot.lastCapturePath.sourceMode,
      runtimeSnapshot.lastCapturePath.uri,
    );
  }
  return merged;
}

function extractSleepConsolidationStatus(value: unknown): JsonRecord | undefined {
  if (!isRecord(value)) {
    return undefined;
  }
  const runtime = isRecord(value.runtime) ? value.runtime : {};
  return isRecord(runtime.sleep_consolidation)
    ? runtime.sleep_consolidation
    : undefined;
}

export type ReportRunnerDeps = {
  buildDiagnosticReport: (
    command: DiagnosticReport["command"],
    config: PluginConfig,
    checks: DiagnosticCheck[],
    activeTransport: string | null,
  ) => DiagnosticReport;
  collectLegacyHostConfigChecks: (config: PluginConfig) => LegacyVerifyCheck[];
  collectStaticDoctorChecks: (config: PluginConfig) => DiagnosticCheck[];
  extractPayloadFailureMessage: (value: unknown) => string;
  extractReadText: (raw: unknown) => ReadPayload;
  formatError: (error: unknown) => string;
  getTransportFallbackOrder: (config: PluginConfig) => string[];
  isTransientSqliteLockError: (error: unknown) => boolean;
  normalizeIndexStatusPayload: (raw: unknown) => JsonRecord;
  normalizeSearchPayload: (
    raw: unknown,
    mapping: PluginConfig["mapping"],
    visualConfig?: Pick<PluginConfig["visualMemory"], "pathPrefix">,
  ) => NormalizedSearchPayload;
  pathExists: (inputPath: string) => boolean;
  payloadIndicatesFailure: (value: unknown) => boolean;
  persistTransportDiagnosticsSnapshot: (
    config: PluginConfig,
    client: MemoryPalaceMcpClient,
    report?: DiagnosticReport,
  ) => void;
  probeProfileMemoryState: (
    client: ReportClient,
    config: PluginConfig,
  ) => Promise<{ blockCount: number; paths: string[] }>;
  resolvePathLikeToUri: (pathOrUri: string, mapping: PluginConfig["mapping"]) => string;
  resolveHostWorkspaceDir: (context: Record<string, unknown>) => string | undefined;
  scanHostWorkspaceForQuery: (
    query: string,
    workspaceDir: string,
    config: PluginConfig["hostBridge"],
  ) => HostWorkspaceHitLike[] | Promise<HostWorkspaceHitLike[]>;
  snapshotPluginRuntimeState: (config: PluginConfig) => PluginRuntimeSnapshot;
  withTransientSqliteLockRetry: <T>(
    operation: () => Promise<T>,
    shouldRetry?: (value: T) => boolean,
    maxAttempts?: number,
    baseDelayMs?: number,
  ) => Promise<T>;
};

function finalizeReport(
  command: DiagnosticReport["command"],
  config: PluginConfig,
  checks: DiagnosticCheck[],
  activeTransport: string | null,
  session: ReportSession,
  deps: ReportRunnerDeps,
): DiagnosticReport {
  const report = {
    ...deps.buildDiagnosticReport(command, config, checks, activeTransport),
    ...(session.diagnostics ? { diagnostics: session.diagnostics } : {}),
  };
  deps.persistTransportDiagnosticsSnapshot(config, session.persistClient, report);
  return report;
}

export async function runVerify(
  config: PluginConfig,
  runtime: LegacyVerifyRuntime,
  options: {
    query?: string;
    path?: string;
    readFirstSearchHit?: boolean;
  } = {},
  deps: ReportRunnerDeps,
): Promise<{
  ok: boolean;
  status: string;
  activeTransport: string | null;
  fallbackOrder: string[];
  checks: Array<{ name: string; status: string; summary: string; details?: unknown }>;
  diagnostics?: unknown;
}> {
  const checks = [
    ...deps.collectStaticDoctorChecks(config).map((item) => ({
      name: item.id.replace(/-/g, "_"),
      status: item.status.toUpperCase(),
      summary: item.message,
      ...(item.details !== undefined ? { details: item.details } : {}),
    })),
    ...deps.collectLegacyHostConfigChecks(config).map((item) => ({
      name: item.name,
      status: item.status,
      summary: item.summary,
      ...(item.detail !== undefined ? { details: item.detail } : {}),
    })),
  ];
  const fallbackOrder =
    runtime.describeTransportPlan?.().fallbackOrder ?? deps.getTransportFallbackOrder(config);
  let activeTransport: string | null = null;
  let firstReadablePath: string | undefined;

  try {
    const payload = await runtime.run(async (client) => {
      activeTransport = client.activeTransportKind ?? null;
      return client.indexStatus
        ? deps.normalizeIndexStatusPayload(await client.indexStatus())
        : { ok: false };
    });
    checks.push({
      name: "status",
      status: deps.payloadIndicatesFailure(payload) ? "FAIL" : "PASS",
      summary: deps.payloadIndicatesFailure(payload)
        ? "index_status returned a degraded or failed payload."
        : "index_status responded successfully.",
      details: payload,
    });
  } catch (error) {
    checks.push({
      name: "status",
      status: "FAIL",
      summary: deps.formatError(error),
    });
  }

  if (options.query) {
    try {
      const payload = await runtime.run(async (client) => {
        activeTransport = client.activeTransportKind ?? activeTransport;
        return client.searchMemory
          ? deps.normalizeSearchPayload(
              await client.searchMemory({
                query: options.query,
                max_results: Math.max(1, config.query.maxResults ?? 3),
                candidate_multiplier: Math.max(1, config.query.candidateMultiplier ?? 2),
                include_session: false,
                mode: config.query.mode,
                scope_hint: config.query.scopeHint,
                filters: config.query.filters,
              }),
              config.mapping,
              config.visualMemory,
            )
          : { ok: false, results: [] as MemorySearchResult[] };
      });
      if (Array.isArray(payload.results) && payload.results.length > 0) {
        firstReadablePath = payload.results[0]?.citation ?? payload.results[0]?.path;
      }
      checks.push({
        name: "search",
        status: deps.payloadIndicatesFailure(payload) ? "FAIL" : "PASS",
        summary: deps.payloadIndicatesFailure(payload)
          ? "search_memory probe failed."
          : `search_memory probe returned ${Array.isArray(payload.results) ? payload.results.length : 0} hit(s).`,
        details: payload,
      });
    } catch (error) {
      checks.push({
        name: "search",
        status: "FAIL",
        summary: deps.formatError(error),
      });
    }
  }

  const readTarget = options.path ?? (options.readFirstSearchHit ? firstReadablePath : undefined);
  if (readTarget) {
    try {
      const payload = await runtime.run(async (client) => {
        activeTransport = client.activeTransportKind ?? activeTransport;
        const uri = deps.resolvePathLikeToUri(readTarget, config.mapping);
        const raw = client.readMemory
          ? await client.readMemory({ uri })
          : "Error: read_memory unavailable";
        const extracted = deps.extractReadText(raw);
        if (extracted.error) {
          throw new Error(extracted.error);
        }
        return {
          uri,
          text: extracted.text,
        };
      });
      checks.push({
        name: "read",
        status: payload.text.trim() ? "PASS" : "WARN",
        summary: payload.text.trim()
          ? `read_memory probe succeeded for ${payload.uri}.`
          : `read_memory probe reached ${payload.uri} but returned empty text.`,
        details: payload,
      });
    } catch (error) {
      checks.push({
        name: "read",
        status: "FAIL",
        summary: deps.formatError(error),
      });
    }
  }

  const overallStatus = checks.some((item) => item.status === "FAIL")
    ? "FAIL"
    : checks.some((item) => item.status === "WARN")
      ? "WARN"
      : "PASS";

  return {
    ok: overallStatus !== "FAIL",
    status: overallStatus,
    activeTransport,
    fallbackOrder,
    checks,
    ...(runtime.diagnostics ? { diagnostics: runtime.diagnostics() } : {}),
  };
}

export async function runVerifyReport(
  config: PluginConfig,
  session: ReportSession,
  deps: ReportRunnerDeps,
): Promise<DiagnosticReport> {
  const checks = deps.collectStaticDoctorChecks(config);
  let activeTransport: string | null = null;
  let freshRuntime = false;
  try {
    const health = await deps.withTransientSqliteLockRetry(
      () =>
        session.withClient(async (client) => {
          const report = await client.healthCheck(true);
          activeTransport = report.transport ?? null;
          return report;
        }),
      (report) => !report.ok && deps.isTransientSqliteLockError(report.error ?? ""),
    );
    checks.push({
      id: "transport-health",
      status: health.ok ? "pass" : "fail",
      message: health.ok
        ? `Transport health check passed over ${health.transport ?? "unknown"}.`
        : health.error ?? "Transport health check failed.",
      action: health.ok
        ? undefined
        : "Run `openclaw memory-palace doctor --json` to inspect transport diagnostics.",
    });
  } catch (error) {
    checks.push({
      id: "transport-health",
      status: "fail",
      message: deps.formatError(error),
      action:
        "Check the configured stdio/sse transport, then retry `openclaw memory-palace verify --json`.",
    });
  }
  try {
    const payload = await deps.withTransientSqliteLockRetry(
      () =>
        session.withClient(async (client) => {
          const status = deps.normalizeIndexStatusPayload(await client.indexStatus?.());
          return {
            transport: client.activeTransportKind,
            status,
          };
        }),
      (result) =>
        deps.payloadIndicatesFailure(result.status) &&
        deps.isTransientSqliteLockError(deps.extractPayloadFailureMessage(result.status)),
    );
    activeTransport = payload.transport ?? null;
    const failed = deps.payloadIndicatesFailure(payload.status);
    const degraded = !failed && isRecord(payload.status) && payload.status.degraded === true;
    freshRuntime = !failed && !degraded && isFreshRuntimeIndexStatus(payload.status);
    checks.push({
      id: "index-status",
      status: failed ? "fail" : degraded ? "warn" : "pass",
      message: failed
        ? "index_status returned a failed payload."
        : degraded
          ? "index_status returned a degraded payload."
          : "index_status responded successfully.",
      action:
        failed || degraded
          ? "Run `openclaw memory-palace doctor --json` for a deeper diagnosis."
          : undefined,
      details: payload.status,
    });
    const sleepConsolidation = extractSleepConsolidationStatus(payload.status);
    if (sleepConsolidation) {
      const enabled = sleepConsolidation.enabled === true;
      const scheduled = sleepConsolidation.scheduled === true;
      const enqueueReason = String(
        sleepConsolidation.enqueue_reason ?? sleepConsolidation.reason ?? "",
      ).trim();
      checks.push({
        id: "sleep-consolidation",
        status: !enabled
          ? "warn"
          : scheduled
            ? "pass"
            : enqueueReason === "queue_full"
              ? "warn"
              : "pass",
        message: !enabled
          ? "Sleep consolidation is disabled."
          : scheduled
            ? "Sleep consolidation is enabled and has a scheduled runtime status."
            : enqueueReason === "queue_full"
              ? "Sleep consolidation is enabled, but the runtime queue is currently full."
              : "Sleep consolidation is enabled and currently idle.",
        action: !enabled
          ? "Enable sleep consolidation in the runtime env if you want low-risk idle-time consolidation."
          : enqueueReason === "queue_full"
            ? "Wait for the runtime queue to drain or retry later."
            : undefined,
        details: sleepConsolidation,
      });
    }
  } catch (error) {
    checks.push({
      id: "index-status",
      status: "fail",
      message: deps.formatError(error),
      action: "Check the configured stdio/sse transport, then retry `openclaw memory-palace verify`.",
    });
  }
  if (config.profileMemory.enabled) {
    try {
      const payload = await deps.withTransientSqliteLockRetry(() =>
        session.withClient(async (client) => {
          const profileState = await deps.probeProfileMemoryState(client, config);
          return {
            transport: client.activeTransportKind,
            profileState,
          };
        }),
      );
      activeTransport = payload.transport ?? activeTransport;
      checks.push({
        id: "profile-memory-state",
        status: payload.profileState.blockCount > 0 ? "pass" : freshRuntime ? "pass" : "warn",
        message:
          payload.profileState.blockCount > 0
            ? `Profile memory probe found ${payload.profileState.blockCount} stored block(s).`
            : freshRuntime
              ? "Profile memory is configured and the runtime is still fresh, so no stored profile blocks are expected yet."
              : "Profile memory is configured, but no stored profile blocks were detected yet.",
        action:
          payload.profileState.blockCount > 0
            ? undefined
            : freshRuntime
              ? undefined
              : "Seed a stable identity / preferences / workflow fact, then rerun verify.",
        details: {
          blockCount: payload.profileState.blockCount,
          paths: payload.profileState.paths,
        },
      });
    } catch (error) {
      checks.push({
        id: "profile-memory-state",
        status: "warn",
        message: deps.formatError(error),
        action: "Inspect profile block read/search access and retry verify.",
      });
    }
  }
  if (config.smartExtraction.enabled) {
    const circuit = deps.snapshotPluginRuntimeState(config).smartExtractionCircuit;
    checks.push({
      id: "smart-extraction-circuit",
      status: circuit.state === "open" ? "warn" : "pass",
      message:
        circuit.state === "open"
          ? `Smart extraction circuit breaker is open after ${circuit.failureCount} failure(s).`
          : "Smart extraction circuit breaker is closed.",
      action:
        circuit.state === "open"
          ? "Wait for the cooldown window or fix the smart-extraction LLM path before retrying."
          : undefined,
      details: circuit,
    });
  }
  return finalizeReport("verify", config, checks, activeTransport, session, deps);
}

export async function runDoctorReport(
  config: PluginConfig,
  session: ReportSession,
  query: string,
  deps: ReportRunnerDeps,
): Promise<DiagnosticReport> {
  const report = await runVerifyReport(config, session, deps);
  const checks = [...report.checks];
  const freshRuntime = extractFreshRuntimeFromChecks(checks);
  let activeTransport = report.activeTransport;
  let pluginSearchResultCount = 0;
  try {
    const payload = await deps.withTransientSqliteLockRetry(
      () =>
        session.withClient(async (client) => {
          const result = deps.normalizeSearchPayload(
            await client.searchMemory?.({
              query,
              max_results: Math.max(1, config.query.maxResults ?? 3),
              candidate_multiplier: 1,
              include_session: false,
              mode: "keyword",
              scope_hint: config.query.scopeHint,
              filters: config.query.filters,
            }),
            config.mapping,
            config.visualMemory,
          );
          return {
            transport: client.activeTransportKind,
            result,
          };
        }),
      (value) =>
        deps.payloadIndicatesFailure(value.result) &&
        deps.isTransientSqliteLockError(deps.extractPayloadFailureMessage(value.result)),
    );
    activeTransport = payload.transport ?? activeTransport;
    const resultCount = payload.result.results.length;
    pluginSearchResultCount = resultCount;
    const failed = deps.payloadIndicatesFailure(payload.result);
    const degraded = !failed && isRecord(payload.result) && payload.result.degraded === true;
    checks.push({
      id: "search-probe",
      status: failed ? "fail" : degraded ? "warn" : resultCount > 0 ? "pass" : freshRuntime ? "pass" : "warn",
      message: failed
        ? "search_memory probe failed."
        : degraded
          ? `search_memory probe returned ${resultCount} hit(s) with degraded retrieval.`
          : resultCount > 0
            ? `search_memory probe returned ${resultCount} hit(s).`
            : freshRuntime
              ? "search_memory probe returned no hits yet. This is expected on a fresh runtime before you seed any durable facts."
              : "search_memory probe succeeded but returned no hits.",
      action: failed
        ? "Check query filters, backend health, and configured transport."
        : degraded
          ? "Inspect `degrade_reasons` in the payload, then verify model / reranker availability and retry."
          : resultCount > 0
            ? undefined
            : freshRuntime
              ? undefined
              : "If you expect hits, seed a known memory and rerun `openclaw memory-palace smoke --expect-hit`.",
      details: payload.result,
    });
  } catch (error) {
    checks.push({
      id: "search-probe",
      status: "fail",
      message: deps.formatError(error),
      action: "Check search_memory availability and backend connectivity.",
    });
  }
  const runtimeSnapshot = deps.snapshotPluginRuntimeState(config);
  const searchCheckDetails = checks.find(
    (entry) => entry.id === "search-probe",
  )?.details as NormalizedSearchPayload | undefined;
  const searchResults = Array.isArray(searchCheckDetails?.results)
    ? searchCheckDetails.results
    : [];
  const inferredCaptureLayerCounts = inferCaptureLayerCountsFromSearchResults(
    searchResults,
  );
  const effectiveCaptureLayerCounts = mergeRuntimeCaptureLayerCounts(
    {
      ...runtimeSnapshot.captureLayerCounts,
      ...inferredCaptureLayerCounts,
    },
    runtimeSnapshot,
  );
  const captureLayerEntries = Object.entries(effectiveCaptureLayerCounts)
    .filter(([, value]) => typeof value === "number" && Number.isFinite(value) && value > 0)
    .sort((left, right) => right[1] - left[1]);
  checks.push({
    id: "capture-layer-distribution",
    status: captureLayerEntries.length > 0 ? "pass" : freshRuntime ? "pass" : "warn",
    message:
      captureLayerEntries.length > 0
        ? Object.keys(inferredCaptureLayerCounts).length > 0
          ? `Recent capture layers: ${captureLayerEntries.map(([layer, count]) => `${layer}=${count}`).join(", ")}.`
          : `Recent capture layers: ${captureLayerEntries.map(([layer, count]) => `${layer}=${count}`).join(", ")}.`
        : freshRuntime
          ? "No recent capture-layer history is recorded yet. This is expected before the first real capture turn."
          : "No recent capture-layer history is recorded yet.",
    action:
      captureLayerEntries.length > 0
        ? undefined
        : freshRuntime
          ? undefined
          : "Run a real capture turn so doctor can inspect rule / bridge / assistant_derived / llm_extracted distribution.",
    details: {
      counts: effectiveCaptureLayerCounts,
      recent: runtimeSnapshot.recentCaptureLayers,
      lastCapturePath: runtimeSnapshot.lastCapturePath,
      lastFallbackPath: runtimeSnapshot.lastFallbackPath,
      inferredFromSearch:
        Object.keys(inferredCaptureLayerCounts).length > 0
          ? {
              counts: inferredCaptureLayerCounts,
              paths: searchResults
                .map((entry) => entry.path)
                .filter((value) => Boolean(value)),
            }
          : undefined,
    },
  });
  if (config.hostBridge.enabled) {
    const workspaceDir = deps.resolveHostWorkspaceDir({});
    if (workspaceDir && deps.pathExists(workspaceDir)) {
      const hostHits = await deps.scanHostWorkspaceForQuery(
        query,
        workspaceDir,
        config.hostBridge,
      );
      checks.push({
        id: "host-plugin-split-brain",
        status:
          hostHits.length > 0 && pluginSearchResultCount === 0
            ? "warn"
            : "pass",
        message:
          hostHits.length > 0 && pluginSearchResultCount === 0
            ? `Host workspace matched ${hostHits.length} candidate fact(s), but plugin search returned no durable hits.`
            : hostHits.length > 0 && pluginSearchResultCount > 0
              ? `Host workspace matched ${hostHits.length} candidate fact(s) and plugin search already returned ${pluginSearchResultCount} durable hit(s).`
              : "No matching host workspace fact was found for the current doctor query. This does not indicate a wiring failure.",
        action:
          hostHits.length > 0 && pluginSearchResultCount === 0
            ? "Run a real agent turn with this query so hostBridge can backfill plugin-owned memory, then rerun doctor."
            : undefined,
        details: {
          workspaceDir,
          hostHits: hostHits.map((entry) => ({
            citation: entry.citation,
            score: Number(entry.score.toFixed(2)),
            snippet: entry.snippet,
          })),
          pluginResultCount: pluginSearchResultCount,
        },
      });
    } else {
      checks.push({
        id: "host-plugin-split-brain",
        status: "warn",
        message:
          "Host bridge is enabled, but the host workspace directory could not be resolved for this doctor run.",
        action:
          "Run doctor through `openclaw` with OPENCLAW_CONFIG_PATH set, or provide a normal agent workspace.",
      });
    }
  }
  return finalizeReport("doctor", config, checks, activeTransport, session, deps);
}

export async function runSmokeReport(
  config: PluginConfig,
  session: ReportSession,
  options: {
    query: string;
    pathOrUri?: string;
    expectHit: boolean;
  },
  deps: ReportRunnerDeps,
): Promise<DiagnosticReport> {
  const doctor = await runDoctorReport(config, session, options.query, deps);
  const checks = [...doctor.checks];
  const freshRuntime = extractFreshRuntimeFromChecks(checks);
  let activeTransport = doctor.activeTransport;

  let resolvedPathOrUri = options.pathOrUri;
  let searchPayload: NormalizedSearchPayload | null = null;
  const searchCheck = checks.find((entry) => entry.id === "search-probe");
  if (searchCheck?.details && isRecord(searchCheck.details)) {
    searchPayload = searchCheck.details as NormalizedSearchPayload;
  }
  if (
    !resolvedPathOrUri &&
    searchPayload &&
    Array.isArray(searchPayload.results) &&
    searchPayload.results.length > 0
  ) {
    resolvedPathOrUri = searchPayload.results[0]?.citation ?? searchPayload.results[0]?.path;
  }

  if (!resolvedPathOrUri) {
    checks.push({
      id: "read-probe",
      status: options.expectHit ? "fail" : freshRuntime ? "pass" : "warn",
      message:
        options.expectHit
          ? "No readable path was available for the smoke read probe."
          : freshRuntime
            ? "No readable path was available for the smoke read probe yet. This is expected on a fresh runtime unless you seed a known memory or pass --path-or-uri."
            : "No readable path was available for the smoke read probe.",
      action: options.expectHit
        ? "Pass --path-or-uri with a known memory or seed a test memory before rerunning smoke."
        : freshRuntime
          ? undefined
          : "Pass --path-or-uri to force a follow-up read probe.",
    });
    return finalizeReport("smoke", config, checks, activeTransport, session, deps);
  }

  try {
    const payload = await deps.withTransientSqliteLockRetry(() =>
      session.withClient(async (client) => {
        const uri = deps.resolvePathLikeToUri(resolvedPathOrUri!, config.mapping);
        const raw = await client.readMemory?.({
          uri,
          ...(config.read.maxChars ? { max_chars: config.read.maxChars } : {}),
        });
        const extracted = deps.extractReadText(raw);
        if (extracted.error) {
          throw new Error(extracted.error);
        }
        return {
          transport: client.activeTransportKind,
          uri,
          text: extracted.text,
        };
      }),
    );
    activeTransport = payload.transport ?? activeTransport;
    checks.push({
      id: "read-probe",
      status: payload.text.trim() ? "pass" : "warn",
      message: payload.text.trim()
        ? `read_memory probe succeeded for ${payload.uri}.`
        : `read_memory probe reached ${payload.uri} but returned empty text.`,
      action:
        payload.text.trim()
          ? undefined
          : "Inspect the target memory contents directly and confirm it is not empty.",
    });
  } catch (error) {
    checks.push({
      id: "read-probe",
      status: "fail",
      message: deps.formatError(error),
      action: "Confirm the target path/URI exists, then rerun smoke.",
    });
  }

  return finalizeReport("smoke", config, checks, activeTransport, session, deps);
}

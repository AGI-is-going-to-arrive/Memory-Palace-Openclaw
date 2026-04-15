import fs from "node:fs";
import path from "node:path";
import type {
  MemoryPalaceLatencySummary,
  MemoryPalaceMcpClient,
  MemoryPalaceTransportEvent,
} from "./client.js";
import type {
  DiagnosticReport,
  PluginConfig,
  PluginRuntimeSignature,
  PluginRuntimeSnapshot,
} from "./types.js";

export function sanitizeTransportSnapshotText(
  value: string | undefined,
  redactText: (value: string | undefined) => string | undefined,
): string | undefined {
  return redactText(value);
}

export function normalizeTransportLatencySnapshot(
  value: MemoryPalaceLatencySummary | undefined,
): Record<string, number | null> {
  return {
    last: typeof value?.last === "number" && Number.isFinite(value.last) ? value.last : null,
    avg: typeof value?.avg === "number" && Number.isFinite(value.avg) ? value.avg : null,
    p95: typeof value?.p95 === "number" && Number.isFinite(value.p95) ? value.p95 : null,
    max: typeof value?.max === "number" && Number.isFinite(value.max) ? value.max : null,
    samples:
      typeof value?.samples === "number" && Number.isFinite(value.samples) && value.samples > 0
        ? Math.trunc(value.samples)
        : 0,
  };
}

export function normalizeTransportEventSnapshot(
  event: MemoryPalaceTransportEvent,
  sanitizeText: (value: string | undefined) => string | undefined,
): Record<string, unknown> {
  return {
    at: event.at,
    category: event.category,
    status: event.status,
    transport: event.transport,
    ...(event.tool ? { tool: event.tool } : {}),
    ...(event.attempt !== undefined ? { attempt: event.attempt } : {}),
    ...(event.fallback !== undefined ? { fallback: event.fallback } : {}),
    ...(event.retry !== undefined ? { retry: event.retry } : {}),
    ...(event.reused !== undefined ? { reused: event.reused } : {}),
    ...(event.latencyMs !== undefined ? { latency_ms: event.latencyMs } : {}),
    ...(event.message ? { message: sanitizeText(event.message) } : {}),
  };
}

export function resolveTransportDiagnosticsInstancePath(
  targetPath: string,
  instanceId: string,
): string {
  const parsed = path.parse(targetPath);
  return path.join(parsed.dir, `${parsed.name}.instances`, `instance-${instanceId}.json`);
}

type TransportDiagnosticsSnapshotOptions = {
  buildPluginRuntimeSignature: (config: PluginConfig) => PluginRuntimeSignature;
  getTransportFallbackOrder: (config: PluginConfig) => string[];
  instanceId: string;
  pluginVersion: string;
  processId: number;
  sanitizeText: (value: string | undefined) => string | undefined;
  snapshotPluginRuntimeState: (config: PluginConfig) => PluginRuntimeSnapshot;
};

export function buildTransportDiagnosticsSnapshot(
  config: PluginConfig,
  client: MemoryPalaceMcpClient,
  options: TransportDiagnosticsSnapshotOptions,
  report?: DiagnosticReport,
): Record<string, unknown> {
  const pluginRuntime = options.snapshotPluginRuntimeState(config);
  const diagnostics = client.diagnostics;
  const status =
    report?.status ??
    (diagnostics.lastHealthCheckError || diagnostics.lastError ? "warn" : "pass");

  return {
    source: "openclaw.memory_palace",
    instance_id: `instance-${options.instanceId}`,
    process_id: options.processId,
    updated_at: new Date().toISOString(),
    plugin_version: options.pluginVersion,
    connection_model: "persistent-client",
    configured_transport: config.transport,
    fallback_order: options.getTransportFallbackOrder(config),
    active_transport: client.activeTransportKind,
    status,
    summary:
      report?.summary ??
      (status === "pass"
        ? "transport diagnostics healthy"
        : "transport diagnostics report warnings"),
    diagnostics: {
      preferred_transport: diagnostics.preferredTransport,
      configured_transports: diagnostics.configuredTransports,
      active_transport_kind: diagnostics.activeTransportKind,
      connect_attempts: diagnostics.connectAttempts,
      connect_retry_count: diagnostics.connectRetryCount,
      call_retry_count: diagnostics.callRetryCount,
      request_retries: diagnostics.requestRetries,
      fallback_count: diagnostics.fallbackCount,
      reuse_count: diagnostics.reuseCount,
      last_connected_at: diagnostics.lastConnectedAt ?? null,
      connect_latency_ms: normalizeTransportLatencySnapshot(diagnostics.connectLatencyMs),
      last_error: options.sanitizeText(diagnostics.lastError) ?? null,
      last_health_check_at: diagnostics.lastHealthCheckAt ?? null,
      last_health_check_error: options.sanitizeText(diagnostics.lastHealthCheckError) ?? null,
      healthcheck_tool: diagnostics.healthcheckTool,
      healthcheck_ttl_ms: diagnostics.healthcheckTtlMs,
      recent_events: diagnostics.recentEvents
        .slice(-Math.max(1, config.observability.maxRecentTransportEvents))
        .map((event) => normalizeTransportEventSnapshot(event, options.sanitizeText)),
    },
    ...(report
      ? {
          last_report: {
            command: report.command,
            ok: report.ok,
            status: report.status,
            summary: report.summary,
            active_transport: report.activeTransport,
            checks: report.checks.map((check) => ({
              id: check.id,
              status: check.status,
              message: check.message,
              ...(check.code ? { code: check.code } : {}),
              ...(check.cause ? { cause: check.cause } : {}),
              ...(check.action ? { action: options.sanitizeText(check.action) } : {}),
            })),
          },
        }
      : {}),
    plugin_runtime: {
      ...pluginRuntime,
      signature: options.buildPluginRuntimeSignature(config),
    },
  };
}

export function persistTransportDiagnosticsSnapshot(
  config: PluginConfig,
  client: MemoryPalaceMcpClient,
  options: TransportDiagnosticsSnapshotOptions,
  report?: DiagnosticReport,
): void {
  if (!config.observability.enabled) {
    return;
  }
  const targetPath = config.observability.transportDiagnosticsPath;
  const instancePath = resolveTransportDiagnosticsInstancePath(
    targetPath,
    options.instanceId,
  );

  try {
    fs.mkdirSync(path.dirname(targetPath), { recursive: true });
    fs.mkdirSync(path.dirname(instancePath), { recursive: true });
    const snapshot = `${JSON.stringify(
      buildTransportDiagnosticsSnapshot(config, client, options, report),
      null,
      2,
    )}\n`;
    const tempPath = `${targetPath}.tmp`;
    fs.writeFileSync(tempPath, snapshot, "utf8");
    fs.renameSync(tempPath, targetPath);

    const instanceTempPath = `${instancePath}.tmp`;
    fs.writeFileSync(instanceTempPath, snapshot, "utf8");
    fs.renameSync(instanceTempPath, instancePath);

    const now = Date.now();
    for (const entry of fs.readdirSync(path.dirname(instancePath), { withFileTypes: true })) {
      if (!entry.isFile() || !entry.name.endsWith(".json")) {
        continue;
      }
      const entryPath = path.join(path.dirname(instancePath), entry.name);
      const stats = fs.statSync(entryPath);
      if (now - stats.mtimeMs > 7 * 24 * 60 * 60 * 1000) {
        fs.rmSync(entryPath, { force: true });
      }
    }
  } catch {
    // Observability persistence is best-effort and must not break the plugin.
  }
}

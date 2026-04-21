import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { SSEClientTransport } from "@modelcontextprotocol/sdk/client/sse.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
import type { Transport } from "@modelcontextprotocol/sdk/shared/transport.js";
import { isRecord } from "./utils.js";

export type MemoryPalaceTransportMode = "auto" | "stdio" | "sse";

export type MemoryPalaceStdioConfig = {
  command?: string;
  args?: string[];
  cwd?: string;
  env?: Record<string, string>;
};

export type MemoryPalaceSseConfig = {
  url?: string;
  apiKey?: string;
  headers?: Record<string, string>;
};

export type MemoryPalaceRetryConfig = {
  attempts?: number;
  baseDelayMs?: number;
  maxDelayMs?: number;
};

export type MemoryPalaceClientConfig = {
  transport?: MemoryPalaceTransportMode;
  timeoutMs?: number;
  clientName?: string;
  clientVersion?: string;
  connectRetries?: number;
  connectBackoffMs?: number;
  connectBackoffMaxMs?: number;
  stdio?: MemoryPalaceStdioConfig;
  sse?: MemoryPalaceSseConfig;
  retry?: MemoryPalaceRetryConfig;
  requestRetries?: number;
  healthcheckTool?: string;
  healthcheckTtlMs?: number;
};

export type MemoryPalaceHealthReport = {
  ok: boolean;
  transport: TransportKind | null;
  latencyMs?: number;
  status?: unknown;
  error?: string;
  diagnostics: MemoryPalaceClientDiagnostics;
};

export type MemoryPalaceLatencySummary = {
  last: number | null;
  avg: number | null;
  p95: number | null;
  max: number | null;
  samples: number;
};

export type MemoryPalaceClientDiagnostics = {
  preferredTransport: MemoryPalaceTransportMode;
  configuredTransports: TransportKind[];
  activeTransportKind: TransportKind | null;
  connectAttempts: number;
  connectRetryCount: number;
  callRetryCount: number;
  requestRetries: number;
  fallbackCount: number;
  reuseCount: number;
  lastConnectedAt?: string;
  connectLatencyMs: MemoryPalaceLatencySummary;
  lastError?: string;
  lastHealthCheckAt?: string;
  lastHealthCheckError?: string;
  healthcheckTool: string;
  healthcheckTtlMs: number;
  recentEvents: MemoryPalaceTransportEvent[];
};

type TransportKind = Exclude<MemoryPalaceTransportMode, "auto">;
type TransportEventCategory = "connect" | "healthcheck" | "tool_call";
type TransportEventStatus = "start" | "pass" | "warn" | "fail";

export type MemoryPalaceTransportEvent = {
  at: string;
  category: TransportEventCategory;
  status: TransportEventStatus;
  transport: TransportKind | null;
  tool?: string;
  attempt?: number;
  message?: string;
  fallback?: boolean;
  retry?: boolean;
  reused?: boolean;
  latencyMs?: number;
};

type ConnectedTransport = {
  kind: TransportKind;
  close?: () => Promise<void>;
};

type ToolTextContent = {
  type?: string;
  text?: string;
};

type ToolResultEnvelope = {
  isError?: boolean;
  structuredContent?: unknown;
  content?: ToolTextContent[];
};

type TimeoutErrorWithCleanup = Error & {
  memoryPalaceTimeoutCleanup?: boolean;
};

type RetryConfig = {
  attempts: number;
  baseDelayMs: number;
  maxDelayMs: number;
};

type HealthCheckConfig = {
  tool: string;
  ttlMs: number;
};

type TransportCandidate =
  | ({
      kind: "stdio";
    } & Required<Pick<MemoryPalaceStdioConfig, "command">> &
      Omit<MemoryPalaceStdioConfig, "command">)
  | ({
      kind: "sse";
    } & Required<Pick<MemoryPalaceSseConfig, "url">> & {
      requestInit?: RequestInit;
    });

const DEFAULT_RETRY: RetryConfig = {
  attempts: 2,
  baseDelayMs: 250,
  maxDelayMs: 1_000,
};
const DEFAULT_OPERATION_TIMEOUT_MS = 30_000;

const DEFAULT_HEALTHCHECK: HealthCheckConfig = {
  tool: "index_status",
  ttlMs: 5_000,
};

const TRANSPORT_EVENT_LIMIT = 24;
const TRANSPORT_LATENCY_SAMPLE_LIMIT = 64;
const TRANSPORT_REDACTION_PATTERNS: Array<[RegExp, string]> = [
  [/\b(authorization\s*[:=]\s*bearer\s+)[^\s,;]+/giu, "$1[REDACTED]"],
  [/\b(x-mcp-api-key\s*[:=]\s*)[^\s,;]+/giu, "$1[REDACTED]"],
  [/\b(api[-_ ]?key\s*[:=]\s*)[^\s,;]+/giu, "$1[REDACTED]"],
  [/\b(token\s*[:=]\s*)[^\s,;]+/giu, "$1[REDACTED]"],
  [/([?&](?:api[-_]?key|token|key)=)[^&\s]+/giu, "$1[REDACTED]"],
];

function parseMaybeJson(value: string): unknown {
  const trimmed = value.trim();
  if (!trimmed) {
    return value;
  }
  if (
    !(
      (trimmed.startsWith("{") && trimmed.endsWith("}")) ||
      (trimmed.startsWith("[") && trimmed.endsWith("]"))
    )
  ) {
    return value;
  }
  try {
    return JSON.parse(trimmed) as unknown;
  } catch {
    return value;
  }
}

function extractErrorString(value: string): string | null {
  const trimmed = value.trim();
  if (!trimmed || !/^Error:/i.test(trimmed)) {
    return null;
  }
  const normalized = trimmed.replace(/^Error:\s*/i, "").trim();
  return normalized || "unknown_error";
}

function unwrapNestedResultPayload(value: unknown): unknown {
  let current: unknown = value;
  for (let depth = 0; depth < 4; depth += 1) {
    if (!isRecord(current)) {
      return current;
    }
    const wrapperRecord = Object.fromEntries(
      Object.entries(current).filter(([key]) => key !== "result"),
    );
    const nextValue = current.result;
    if (isRecord(nextValue)) {
      current = Object.keys(wrapperRecord).length > 0
        ? { ...wrapperRecord, ...nextValue }
        : nextValue;
      continue;
    }
    if (typeof nextValue !== "string") {
      return current;
    }
    const parsed = parseMaybeJson(nextValue);
    if (isRecord(parsed)) {
      current = Object.keys(wrapperRecord).length > 0
        ? { ...wrapperRecord, ...parsed }
        : parsed;
      continue;
    }
    if (Array.isArray(parsed)) {
      return parsed;
    }
    return current;
  }
  return current;
}

function extractToolPayload(value: unknown): unknown {
  if (!isRecord(value)) {
    return value;
  }
  const structuredContent = value.structuredContent;
  if (structuredContent !== undefined) {
    return structuredContent;
  }
  const content = Array.isArray(value.content) ? value.content : [];
  const text = content
    .filter((entry): entry is ToolTextContent => isRecord(entry))
    .filter((entry) => entry.type === "text" && typeof entry.text === "string")
    .map((entry) => entry.text ?? "")
    .join("\n");
  if (!text) {
    return value;
  }
  return parseMaybeJson(text);
}

function extractToolError(value: unknown): string | null {
  if (!isRecord(value) || value.isError !== true) {
    return null;
  }
  const payload = extractToolPayload(value);
  if (typeof payload === "string" && payload.trim()) {
    return payload;
  }
  if (isRecord(payload) && typeof payload.error === "string" && payload.error.trim()) {
    return payload.error;
  }
  return JSON.stringify(payload, null, 2);
}

function extractPayloadError(value: unknown): string | null {
  const normalized = unwrapNestedResultPayload(value);
  if (typeof normalized === "string") {
    return extractErrorString(normalized);
  }
  if (!isRecord(normalized)) {
    return null;
  }

  const explicitlyFailed =
    normalized.ok === false ||
    normalized.success === false ||
    normalized.disabled === true ||
    normalized.unavailable === true;
  if (!explicitlyFailed) {
    if (typeof normalized.result === "string") {
      return extractErrorString(normalized.result);
    }
    return null;
  }

  const detail = normalized.detail;
  if (typeof normalized.error === "string" && normalized.error.trim()) {
    return normalized.error.trim();
  }
  if (typeof normalized.message === "string" && normalized.message.trim()) {
    return normalized.message.trim();
  }
  if (typeof normalized.reason === "string" && normalized.reason.trim()) {
    return normalized.reason.trim();
  }
  if (typeof detail === "string" && detail.trim()) {
    return detail.trim();
  }
  if (isRecord(detail)) {
    if (typeof detail.error === "string" && detail.error.trim()) {
      return detail.error.trim();
    }
    if (typeof detail.message === "string" && detail.message.trim()) {
      return detail.message.trim();
    }
    if (typeof detail.reason === "string" && detail.reason.trim()) {
      return detail.reason.trim();
    }
  }
  return JSON.stringify(normalized, null, 2);
}

function timeoutCleanupHandled(error: unknown): boolean {
  return Boolean((error as TimeoutErrorWithCleanup | undefined)?.memoryPalaceTimeoutCleanup);
}

function redactSensitiveText(value: string | undefined): string | undefined {
  if (!value) {
    return value;
  }
  return TRANSPORT_REDACTION_PATTERNS.reduce(
    (current, [pattern, replacement]) => current.replace(pattern, replacement),
    value,
  );
}

function normalizeHeaderRecord(headers?: Record<string, string>): Record<string, string> | undefined {
  if (!headers) {
    return undefined;
  }
  const normalized: Record<string, string> = {};
  for (const [key, value] of Object.entries(headers)) {
    const headerName = key.trim();
    const headerValue = value.trim();
    if (headerName && headerValue) {
      normalized[headerName] = headerValue;
    }
  }
  return Object.keys(normalized).length > 0 ? normalized : undefined;
}

function normalizeRetryConfig(retry?: MemoryPalaceRetryConfig): RetryConfig {
  const attempts =
    typeof retry?.attempts === "number" && Number.isFinite(retry.attempts)
      ? Math.max(1, Math.trunc(retry.attempts))
      : DEFAULT_RETRY.attempts;
  const baseDelayMs =
    typeof retry?.baseDelayMs === "number" && Number.isFinite(retry.baseDelayMs)
      ? Math.max(0, Math.trunc(retry.baseDelayMs))
      : DEFAULT_RETRY.baseDelayMs;
  const maxDelayMs =
    typeof retry?.maxDelayMs === "number" && Number.isFinite(retry.maxDelayMs)
      ? Math.max(baseDelayMs, Math.trunc(retry.maxDelayMs))
      : DEFAULT_RETRY.maxDelayMs;
  return {
    attempts,
    baseDelayMs,
    maxDelayMs,
  };
}

function normalizePositiveInteger(value: number | undefined, fallback: number): number {
  if (typeof value === "number" && Number.isFinite(value)) {
    return Math.max(1, Math.trunc(value));
  }
  return fallback;
}

function normalizeHealthCheckConfig(
  tool: string | undefined,
  ttlMs: number | undefined,
): HealthCheckConfig {
  return {
    tool:
      typeof tool === "string" && tool.trim()
        ? tool.trim()
        : DEFAULT_HEALTHCHECK.tool,
    ttlMs:
      typeof ttlMs === "number" && Number.isFinite(ttlMs)
        ? Math.max(0, Math.trunc(ttlMs))
        : DEFAULT_HEALTHCHECK.ttlMs,
  };
}

function backoffDelayForAttempt(attempt: number, retry: RetryConfig): number {
  if (attempt <= 0) {
    return 0;
  }
  return Math.min(retry.maxDelayMs, retry.baseDelayMs * 2 ** (attempt - 1));
}

async function sleep(delayMs: number): Promise<void> {
  if (delayMs <= 0) {
    return;
  }
  await new Promise((resolve) => {
    setTimeout(resolve, delayMs);
  });
}

function isRetryableTransportError(error: unknown): boolean {
  if (error instanceof MemoryPalaceConnectionError) {
    return true;
  }
  const message = error instanceof Error ? error.message : String(error);
  const lowered = message.toLowerCase();
  return [
    "unable to connect to memory palace mcp",
    "connect timeout",
    "request timeout",
    "timed out",
    "econnrefused",
    "econnreset",
    "epipe",
    "fetch failed",
    "network",
    "socket hang up",
    "terminated",
    "closed",
  ].some((marker) => lowered.includes(marker));
}

function isSafeRetryTool(name: string, args?: Record<string, unknown>): boolean {
  if (
    name === "search_memory" ||
    name === "read_memory" ||
    name === "index_status"
  ) {
    return true;
  }
  if (name === "compact_context") {
    const force = args?.force;
    return force !== true && force !== "true" && force !== 1 && force !== "1";
  }
  return false;
}

function formatConfiguredTransports(candidates: TransportCandidate[]): TransportKind[] {
  return candidates.map((candidate) => candidate.kind);
}

function toIsoTimestamp(input?: number): string | undefined {
  return input ? new Date(input).toISOString() : undefined;
}

function roundTransportLatencyMs(value: number): number {
  return Math.max(0, Number(value.toFixed(3)));
}

function summarizeTransportLatencySamples(samples: number[]): MemoryPalaceLatencySummary {
  const normalized = samples
    .filter((value) => Number.isFinite(value) && value >= 0)
    .map((value) => roundTransportLatencyMs(value));
  if (normalized.length === 0) {
    return {
      last: null,
      avg: null,
      p95: null,
      max: null,
      samples: 0,
    };
  }
  const sorted = [...normalized].sort((left, right) => left - right);
  const p95Index = Math.min(
    sorted.length - 1,
    Math.max(0, Math.ceil(sorted.length * 0.95) - 1),
  );
  const sum = normalized.reduce((acc, value) => acc + value, 0);
  return {
    last: normalized.at(-1) ?? null,
    avg: roundTransportLatencyMs(sum / normalized.length),
    p95: sorted[p95Index] ?? null,
    max: sorted.at(-1) ?? null,
    samples: normalized.length,
  };
}

export class MemoryPalaceConnectionError extends Error {
  readonly causes: string[];

  constructor(message: string, causes: string[] = []) {
    super(message);
    this.name = "MemoryPalaceConnectionError";
    this.causes = causes;
  }
}

export class MemoryPalaceMcpClient {
  private readonly config: Required<Pick<MemoryPalaceClientConfig, "clientName" | "clientVersion">> &
    Omit<MemoryPalaceClientConfig, "retry"> & {
      retry: RetryConfig;
      requestRetries: number;
      healthcheck: HealthCheckConfig;
    };

  private client: Client | null = null;

  private transport: ConnectedTransport | null = null;

  private connectPromise: Promise<Client> | null = null;

  private healthCheckPromise: Promise<MemoryPalaceHealthReport> | null = null;

  private connectAttempts = 0;

  private connectRetryCount = 0;

  private callRetryCount = 0;

  private fallbackCount = 0;

  private reuseCount = 0;

  private lastConnectedAt?: number;

  private connectLatencySamplesMs: number[] = [];

  private lastError?: string;

  private lastHealthCheckAt?: number;

  private lastHealthCheckError?: string;

  private connectionEpoch = 0;

  private recentEvents: MemoryPalaceTransportEvent[] = [];

  constructor(config: MemoryPalaceClientConfig = {}) {
    const retryConfig =
      config.retry ??
      {
        attempts:
          typeof config.connectRetries === "number" && Number.isFinite(config.connectRetries)
            ? Math.max(1, Math.trunc(config.connectRetries) + 1)
            : undefined,
        baseDelayMs: config.connectBackoffMs,
        maxDelayMs:
          typeof config.connectBackoffMaxMs === "number" && Number.isFinite(config.connectBackoffMaxMs)
            ? Math.max(
                Math.trunc(config.connectBackoffMs ?? DEFAULT_RETRY.baseDelayMs),
                Math.trunc(config.connectBackoffMaxMs),
              )
            : typeof config.connectBackoffMs === "number" && Number.isFinite(config.connectBackoffMs)
              ? Math.max(Math.trunc(config.connectBackoffMs), Math.trunc(config.connectBackoffMs) * 4)
              : undefined,
      };
    this.config = {
      clientName: config.clientName ?? "openclaw-memory-palace",
      clientVersion: config.clientVersion ?? "1.1.1",
      transport: config.transport,
      timeoutMs: normalizePositiveInteger(config.timeoutMs, DEFAULT_OPERATION_TIMEOUT_MS),
      stdio: config.stdio,
      sse: config.sse,
      retry: normalizeRetryConfig(retryConfig),
      requestRetries: normalizePositiveInteger(config.requestRetries, normalizeRetryConfig(retryConfig).attempts),
      healthcheck: normalizeHealthCheckConfig(config.healthcheckTool, config.healthcheckTtlMs),
    };
  }

  get activeTransportKind(): TransportKind | null {
    return this.transport?.kind ?? null;
  }

  get diagnostics(): MemoryPalaceClientDiagnostics {
    return {
      preferredTransport: this.config.transport ?? "auto",
      configuredTransports: formatConfiguredTransports(resolveTransportCandidates(this.config)),
      activeTransportKind: this.activeTransportKind,
      connectAttempts: this.connectAttempts,
      connectRetryCount: this.connectRetryCount,
      callRetryCount: this.callRetryCount,
      requestRetries: this.config.requestRetries,
      fallbackCount: this.fallbackCount,
      reuseCount: this.reuseCount,
      lastConnectedAt: toIsoTimestamp(this.lastConnectedAt),
      connectLatencyMs: summarizeTransportLatencySamples(this.connectLatencySamplesMs),
      lastError: this.lastError,
      lastHealthCheckAt: toIsoTimestamp(this.lastHealthCheckAt),
      lastHealthCheckError: this.lastHealthCheckError,
      healthcheckTool: this.config.healthcheck.tool,
      healthcheckTtlMs: this.config.healthcheck.ttlMs,
      recentEvents: this.recentEvents.map((event) => ({ ...event })),
    };
  }

  async searchMemory(args: Record<string, unknown>): Promise<unknown> {
    return this.callTool("search_memory", args);
  }

  async readMemory(args: Record<string, unknown>): Promise<unknown> {
    return this.callTool("read_memory", args);
  }

  async indexStatus(args: Record<string, unknown> = {}): Promise<unknown> {
    return this.callTool("index_status", args);
  }

  async rebuildIndex(args: Record<string, unknown>): Promise<unknown> {
    return this.callTool("rebuild_index", args);
  }

  async compactContext(args: Record<string, unknown>): Promise<unknown> {
    return this.callTool("compact_context", args);
  }

  async compactContextReflection(args: Record<string, unknown>): Promise<unknown> {
    return this.callTool("compact_context_reflection", args);
  }

  async createMemory(args: Record<string, unknown>): Promise<unknown> {
    return this.callTool("create_memory", args);
  }

  async ensureVisualNamespaceChain(args: Record<string, unknown>): Promise<unknown> {
    return this.callTool("ensure_visual_namespace_chain", args);
  }

  async addAlias(args: Record<string, unknown>): Promise<unknown> {
    return this.callTool("add_alias", args);
  }

  async updateMemory(args: Record<string, unknown>): Promise<unknown> {
    return this.callTool("update_memory", args);
  }

  async deleteMemory(args: Record<string, unknown>): Promise<unknown> {
    return this.callTool("delete_memory", args);
  }

  async healthCheck(force = false): Promise<MemoryPalaceHealthReport> {
    if (
      !force &&
      this.lastHealthCheckAt !== undefined &&
      Date.now() - this.lastHealthCheckAt < this.config.healthcheck.ttlMs
    ) {
      return {
        ok: !this.lastHealthCheckError,
        transport: this.activeTransportKind,
        ...(this.lastHealthCheckError ? { error: this.lastHealthCheckError } : {}),
        diagnostics: this.diagnostics,
      };
    }

    if (this.healthCheckPromise) {
      return this.healthCheckPromise;
    }

    const startedAt = Date.now();
    const expectedEpoch = this.connectionEpoch;
    let pending: Promise<MemoryPalaceHealthReport> | null = null;
    pending = (async (): Promise<MemoryPalaceHealthReport> => {
      try {
        const client = await this.ensureConnected();
        const status = await this.invokeTool(client, this.config.healthcheck.tool, {});
        if (expectedEpoch !== this.connectionEpoch) {
          throw new MemoryPalaceConnectionError("Health check was invalidated by a concurrent connection reset.");
        }
        this.lastHealthCheckAt = Date.now();
        this.lastHealthCheckError = undefined;
        this.recordTransportEvent({
          category: "healthcheck",
          status: "pass",
          tool: this.config.healthcheck.tool,
          latencyMs: Date.now() - startedAt,
          message: "health check passed",
        });
        return {
          ok: true,
          transport: this.activeTransportKind,
          latencyMs: Date.now() - startedAt,
          status,
          diagnostics: this.diagnostics,
        };
      } catch (error) {
        const formatted = this.formatAndRememberError(error);
        if (expectedEpoch === this.connectionEpoch) {
          this.lastHealthCheckAt = Date.now();
          this.lastHealthCheckError = formatted;
        }
        this.recordTransportEvent({
          category: "healthcheck",
          status: "fail",
          tool: this.config.healthcheck.tool,
          latencyMs: Date.now() - startedAt,
          message: formatted,
        });
        if (expectedEpoch === this.connectionEpoch) {
          await this.resetConnection();
        }
        return {
          ok: false,
          transport: this.activeTransportKind,
          latencyMs: Date.now() - startedAt,
          error: formatted,
          diagnostics: this.diagnostics,
        };
      } finally {
        if (this.healthCheckPromise === pending) {
          this.healthCheckPromise = null;
        }
      }
    })();
    this.healthCheckPromise = pending;
    return pending;
  }

  async close(): Promise<void> {
    await this.resetConnection();
  }

  private async invokeTool(
    client: Client,
    name: string,
    args: Record<string, unknown>,
  ): Promise<unknown> {
    const result = await this.withOperationTimeout(
      client.callTool({
        name,
        arguments: args,
      } as { name: string; arguments?: Record<string, unknown> }),
      `request timeout after ${this.config.timeoutMs}ms during ${name}`,
      async () => {
        await this.resetConnection();
      },
    );
    const payload = unwrapNestedResultPayload(extractToolPayload(result as ToolResultEnvelope));
    const errorText =
      extractToolError(result as ToolResultEnvelope) ??
      extractPayloadError(payload);
    if (errorText) {
      throw new Error(errorText);
    }
    return payload;
  }

  private async callTool(name: string, args: Record<string, unknown>): Promise<unknown> {
    const maxAttempts = this.config.requestRetries;
    let lastError: unknown;
    const safeToRetry = isSafeRetryTool(name, args);

    for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
      try {
        await this.ensureHealthyBeforeCall(name);
        const client = await this.ensureConnected();
        const payload = await this.invokeTool(client, name, args);
        this.lastError = undefined;
        this.lastHealthCheckAt = Date.now();
        this.lastHealthCheckError = undefined;
        this.recordTransportEvent({
          category: "tool_call",
          status: "pass",
          tool: name,
          attempt,
          message: "tool call passed",
        });
        return payload;
      } catch (error) {
        lastError = error;
        const retryable = isRetryableTransportError(error);
        const cleanupHandled = timeoutCleanupHandled(error);
        const formatted = this.formatAndRememberError(error);
        this.recordTransportEvent({
          category: "tool_call",
          status: retryable && safeToRetry && attempt < maxAttempts ? "warn" : "fail",
          tool: name,
          attempt,
          retry: retryable && safeToRetry && attempt < maxAttempts,
          message: formatted,
        });
        if (retryable && !cleanupHandled) {
          await this.resetConnection();
        }
        if (!safeToRetry || !retryable || attempt >= maxAttempts) {
          throw error;
        }
        this.callRetryCount += 1;
        await sleep(backoffDelayForAttempt(attempt, this.config.retry));
      }
    }

    throw lastError instanceof Error ? lastError : new Error(String(lastError));
  }

  private async ensureConnected(): Promise<Client> {
    if (this.client) {
      this.reuseCount += 1;
      this.recordTransportEvent({
        category: "connect",
        status: "pass",
        reused: true,
        message: "reused existing client",
      });
      return this.client;
    }
    if (this.connectPromise) {
      return this.connectPromise;
    }

    const pending = this.connect(this.connectionEpoch);
    this.connectPromise = pending;
    try {
      return await pending;
    } finally {
      if (this.connectPromise === pending) {
        this.connectPromise = null;
      }
    }
  }

  private async connect(expectedEpoch: number): Promise<Client> {
    const candidates = this.getTransportCandidates();
    if (candidates.length === 0) {
      const guidance = this.buildTransportGuidance();
      throw new MemoryPalaceConnectionError("No Memory Palace transport is configured.", [guidance]);
    }

    const errors: string[] = [];

    for (let attempt = 1; attempt <= this.config.retry.attempts; attempt += 1) {
      if (expectedEpoch !== this.connectionEpoch) {
        throw new MemoryPalaceConnectionError("Connection was reset while connecting.");
      }
      for (const [index, candidate] of candidates.entries()) {
        if (expectedEpoch !== this.connectionEpoch) {
          throw new MemoryPalaceConnectionError("Connection was reset while connecting.");
        }
        this.connectAttempts += 1;
        const connectStartedAt = Date.now();
        this.recordTransportEvent({
          category: "connect",
          status: "start",
          transport: candidate.kind,
          attempt,
          fallback: index > 0,
          message: "connecting",
        });
        const client = new Client({
          name: this.config.clientName,
          version: this.config.clientVersion,
        });
        let transport: StdioClientTransport | SSEClientTransport | undefined;

        try {
          transport =
            candidate.kind === "stdio"
              ? new StdioClientTransport({
                  command: candidate.command,
                  args: candidate.args,
                  cwd: candidate.cwd,
                  env: candidate.env,
                })
              : new SSEClientTransport(new URL(candidate.url), {
                  requestInit: candidate.requestInit,
                });

          await this.connectWithTimeout(client, transport);
          if (expectedEpoch !== this.connectionEpoch) {
            await this.disposePendingConnection(client, transport);
            throw new MemoryPalaceConnectionError("Connection was reset while connecting.");
          }
          const connectedTransport = transport;
          this.client = client;
          this.transport = {
            kind: candidate.kind,
            close: async () => {
              await connectedTransport.close?.();
            },
          };
          const connectLatencyMs = Date.now() - connectStartedAt;
          this.recordConnectLatency(connectLatencyMs);
          this.lastConnectedAt = Date.now();
          this.lastHealthCheckAt = this.lastConnectedAt;
          this.lastHealthCheckError = undefined;
          this.lastError = undefined;
          if (index > 0) {
            this.fallbackCount += 1;
            this.recordTransportEvent({
              category: "connect",
              status: "warn",
              transport: candidate.kind,
              attempt,
              fallback: true,
              latencyMs: connectLatencyMs,
              message: "connected after transport fallback",
            });
          } else {
            this.recordTransportEvent({
              category: "connect",
              status: "pass",
              transport: candidate.kind,
              attempt,
              latencyMs: connectLatencyMs,
              message: "connected",
            });
          }
          return client;
        } catch (error) {
          const connectLatencyMs = Date.now() - connectStartedAt;
          const detail = redactSensitiveText(error instanceof Error ? error.message : String(error)) ?? "unknown_error";
          this.recordTransportEvent({
            category: "connect",
            status: "fail",
            transport: candidate.kind,
            attempt,
            fallback: index > 0,
            latencyMs: connectLatencyMs,
            message: detail,
          });
          errors.push(`attempt ${attempt} ${candidate.kind}: ${detail}`);
          if (!timeoutCleanupHandled(error)) {
            await this.disposePendingConnection(client, transport);
          }
        }
      }

      if (attempt < this.config.retry.attempts) {
        this.connectRetryCount += 1;
        await sleep(backoffDelayForAttempt(attempt, this.config.retry));
      }
    }

    errors.push(this.buildTransportGuidance());
    throw new MemoryPalaceConnectionError(
      "Unable to connect to Memory Palace MCP over the configured transports.",
      errors,
    );
  }

  private async resetConnection(): Promise<void> {
    this.connectionEpoch += 1;
    const transport = this.transport;
    const client = this.client;
    this.transport = null;
    this.client = null;
    this.connectPromise = null;
    this.healthCheckPromise = null;
    this.lastHealthCheckAt = undefined;
    this.lastHealthCheckError = undefined;

    try {
      await (client as { close?: () => Promise<void> } | null)?.close?.();
    } catch {
      //
    }
    try {
      await transport?.close?.();
    } catch {
      //
    }
  }

  private async disposePendingConnection(
    client: { close?: () => Promise<void> } | null,
    transport?: { close?: () => Promise<void> },
  ): Promise<void> {
    try {
      await client?.close?.();
    } catch {
      //
    }
    try {
      await transport?.close?.();
    } catch {
      //
    }
  }

  private async connectWithTimeout(client: Client, transport: Transport): Promise<void> {
    await this.withOperationTimeout(
      client.connect(transport),
      `connect timeout after ${this.config.timeoutMs}ms`,
      async () => {
        await this.disposePendingConnection(client, transport);
      },
    );
  }

  private async withOperationTimeout<T>(
    operation: Promise<T>,
    timeoutMessage: string,
    onTimeout?: () => Promise<void> | void,
  ): Promise<T> {
    const timeoutMs = this.config.timeoutMs;
    let timer: ReturnType<typeof setTimeout> | undefined;
    try {
      return await Promise.race([
        operation,
        new Promise<never>((_, reject) => {
          timer = setTimeout(() => {
            void Promise.resolve(onTimeout?.()).catch(() => undefined);
            const timeoutError = new Error(timeoutMessage) as TimeoutErrorWithCleanup;
            timeoutError.memoryPalaceTimeoutCleanup = Boolean(onTimeout);
            reject(timeoutError);
          }, timeoutMs);
        }),
      ]);
    } finally {
      if (timer) {
        clearTimeout(timer);
      }
      void operation.catch(() => undefined);
    }
  }

  private getTransportCandidates(): TransportCandidate[] {
    return resolveTransportCandidates(this.config);
  }

  private buildTransportGuidance(): string {
    const candidates = formatConfiguredTransports(this.getTransportCandidates());
    if (candidates.length === 0) {
      return "Configure at least one of stdio.command or sse.url before using Memory Palace.";
    }
    return `Configured transport order: ${candidates.join(" -> ")}.`;
  }

  private formatAndRememberError(error: unknown): string {
    const formatted =
      error instanceof MemoryPalaceConnectionError
        ? [error.message, ...error.causes].filter(Boolean).join(" | ")
        : error instanceof Error
          ? error.message
          : String(error);
    const redacted = redactSensitiveText(formatted) ?? formatted;
    this.lastError = redacted;
    return redacted;
  }

  private recordConnectLatency(latencyMs: number): void {
    if (!Number.isFinite(latencyMs) || latencyMs < 0) {
      return;
    }
    const normalized = roundTransportLatencyMs(latencyMs);
    this.connectLatencySamplesMs = [
      ...this.connectLatencySamplesMs.slice(-(TRANSPORT_LATENCY_SAMPLE_LIMIT - 1)),
      normalized,
    ];
  }

  private recordTransportEvent(
    event: Omit<MemoryPalaceTransportEvent, "at" | "transport"> & { transport?: TransportKind | null },
  ): void {
    const next: MemoryPalaceTransportEvent = {
      at: new Date().toISOString(),
      transport: event.transport ?? this.activeTransportKind,
      ...event,
      ...(typeof event.latencyMs === "number" && Number.isFinite(event.latencyMs)
        ? { latencyMs: roundTransportLatencyMs(event.latencyMs) }
        : {}),
      ...(event.message ? { message: redactSensitiveText(event.message) } : {}),
    };
    this.recentEvents = [...this.recentEvents.slice(-(TRANSPORT_EVENT_LIMIT - 1)), next];
  }

  private async ensureHealthyBeforeCall(name: string): Promise<void> {
    if (
      this.client === null ||
      name === this.config.healthcheck.tool ||
      this.config.healthcheck.ttlMs <= 0
    ) {
      return;
    }
    if (
      this.lastHealthCheckAt !== undefined &&
      Date.now() - this.lastHealthCheckAt < this.config.healthcheck.ttlMs
    ) {
      return;
    }
    const report = await this.healthCheck(false);
    if (!report.ok) {
      throw new MemoryPalaceConnectionError(
        `Health check failed before ${name}.`,
        report.error ? [report.error] : [],
      );
    }
  }
}

function resolveTransportCandidates(config: MemoryPalaceClientConfig): TransportCandidate[] {
  const stdio = config.stdio;
  const sse = config.sse;
  const sseHeaders = normalizeHeaderRecord({
    ...(sse?.headers ?? {}),
    ...(sse?.apiKey
      ? {
          Authorization: `Bearer ${sse.apiKey}`,
          "X-MCP-API-Key": sse.apiKey,
        }
      : {}),
  });

  const stdioCandidate: TransportCandidate | null =
    stdio?.command && stdio.command.trim()
      ? {
          kind: "stdio" as const,
          command: stdio.command.trim(),
          args: Array.isArray(stdio.args) ? stdio.args : [],
          cwd: stdio.cwd,
          env: stdio.env,
        }
      : null;

  const sseCandidate: TransportCandidate | null =
    sse?.url && sse.url.trim()
      ? {
          kind: "sse" as const,
          url: sse.url.trim(),
          requestInit: sseHeaders ? { headers: sseHeaders } : undefined,
        }
      : null;

  const mode = config.transport ?? "auto";
  if (mode === "stdio") {
    return stdioCandidate ? [stdioCandidate] : [];
  }
  if (mode === "sse") {
    return sseCandidate ? [sseCandidate] : [];
  }

  const candidates: TransportCandidate[] = [];
  if (stdioCandidate) {
    candidates.push({
      kind: "stdio",
      command: stdioCandidate.command,
      args: stdioCandidate.args ?? [],
      cwd: stdioCandidate.cwd,
      env: stdioCandidate.env,
    });
  }
  if (sseCandidate) {
    candidates.push(sseCandidate);
  }
  return candidates;
}

export const __testing = {
  parseMaybeJson,
  unwrapNestedResultPayload,
  extractToolPayload,
  extractToolError,
  extractPayloadError,
  normalizeHeaderRecord,
  normalizeRetryConfig,
  normalizePositiveInteger,
  normalizeHealthCheckConfig,
  backoffDelayForAttempt,
  resolveBackoffDelay(
    baseDelay: number | undefined,
    attempt: number,
    maxDelayMs?: number,
  ) {
    const normalizedBase = Math.max(50, Math.trunc(baseDelay ?? DEFAULT_RETRY.baseDelayMs));
    const normalizedMax = Math.max(normalizedBase, Math.trunc(maxDelayMs ?? Number.MAX_SAFE_INTEGER));
    return Math.min(normalizedMax, normalizedBase * Math.max(1, 2 ** attempt));
  },
  resolveHealthcheckTool(value: string | undefined) {
    return normalizeHealthCheckConfig(value, undefined).tool;
  },
  resolveHealthcheckTtlMs(value: number | undefined) {
    return normalizeHealthCheckConfig(undefined, value).ttlMs;
  },
  redactSensitiveText,
  isSafeRetryTool,
  isRetryableTransportError,
  isRetriableTransportError: isRetryableTransportError,
  resolveTransportCandidates,
};

import fs, { existsSync } from "node:fs";
import type { OpenClawPluginApi } from "openclaw/plugin-sdk/core";
import type { MemoryPalaceClientConfig } from "./client.js";
import {
  PROFILE_BLOCK_NAMES,
  SMART_EXTRACTION_CATEGORY_NAMES,
} from "./types.js";
import type {
  DefaultStdioLaunch,
  EffectiveSmartExtractionMode,
  HostPlatform,
  HostPlatformProfile,
  PluginConfig,
  ProfileBlockName,
  ReconcileAction,
  SmartExtractionCategory,
  SmartExtractionModelConfig,
  SmartExtractionMode,
  VisualDuplicatePolicy,
  VisualEnrichmentCommandConfig,
} from "./types.js";
import {
  formatError,
  isRecord,
  mergeStringRecords,
  normalizeBaseUrl,
  normalizeChatApiBase,
  parseJsonRecord,
  parseJsonRecordWithWarning,
  pickFirstNonBlank,
  readBoolean,
  readNonNegativeNumber,
  readPositiveNumber,
  readProfileBlockArray,
  readString,
  readStringArray,
  readStringMap,
  stripWrappingQuotes,
} from "./utils.js";
import {
  normalizeVisualPathPrefix,
  readVisualDuplicatePolicy,
} from "./visual-memory.js";

export type ParsePluginConfigOptions = {
  hostPlatform: HostPlatform;
  transportDiagnosticsPathEnv: string;
  defaultTransportDiagnosticsPath: string;
  defaultVisualMemoryDisclosure: string;
  defaultVisualMemoryRetentionNote: string;
  resolveDefaultStdioLaunch: (
    runtimeEnv: Record<string, string> | undefined,
    hostPlatform: HostPlatform,
  ) => DefaultStdioLaunch;
};

const ISOLATED_RUNTIME_ENV_PREFIXES = [
  "OPENAI_",
  "LLM_",
  "SMART_EXTRACTION_LLM_",
  "WRITE_GUARD_LLM_",
  "COMPACT_GIST_LLM_",
  "RETRIEVAL_EMBEDDING_",
  "RETRIEVAL_RERANKER_",
  "ROUTER_",
  "EMBEDDING_PROVIDER_",
] as const;

const ISOLATED_RUNTIME_ENV_KEYS = new Set([
  "DATABASE_URL",
  "MCP_API_KEY",
  "OPENCLAW_MEMORY_PALACE_PROFILE_EFFECTIVE",
  "OPENCLAW_MEMORY_PALACE_PROFILE_REQUESTED",
]);

/**
 * Allowlist for host environment keys that may be passed through even when
 * no dedicated env file is configured.  Without this filter the entire
 * `process.env` (HOME, SSH_AUTH_SOCK, cloud credentials, …) would leak
 * into `runtimeEnv.hostValues`.
 */
const HOST_ENV_ALLOWLIST_KEYS = new Set([
  // ── OS / shell basics ──
  "PATH",
  "HOME",
  "TMPDIR",
  "LANG",
  "SHELL",
  "TERM",
  "USER",
  "LOGNAME",
  "NODE_ENV",

  // ── Memory Palace plugin keys ──
  "DATABASE_URL",
  "MCP_API_KEY",
  "MCP_API_KEY_ALLOW_INSECURE_LOCAL",
  "OPENCLAW_MEMORY_PALACE_ENV_FILE",
  "OPENCLAW_MEMORY_PALACE_PROFILE_EFFECTIVE",
  "OPENCLAW_MEMORY_PALACE_PROFILE_REQUESTED",
  "OPENCLAW_MEMORY_PALACE_RUNTIME_PYTHON",
  "OPENCLAW_MEMORY_PALACE_PLUGIN_ROOT_HINT",
  "VALID_DOMAINS",
  "CORS_ALLOW_ORIGINS",
  "SEARCH_DEFAULT_MODE",
  "LOG_LEVEL",
  // ── Proxy pass-through for shell-managed network routing ──
  "HTTP_PROXY",
  "HTTPS_PROXY",
  "NO_PROXY",
  "ALL_PROXY",
  "http_proxy",
  "https_proxy",
  "no_proxy",
  "all_proxy",
]);

const HOST_ENV_ALLOWLIST_PREFIXES = [
  "OPENCLAW_MEMORY_PALACE_",
  "RETRIEVAL_",
  "WRITE_GUARD_LLM_",
  "COMPACT_GIST_LLM_",
  "INTENT_LLM_",
  "EMBEDDING_PROVIDER_",
  "RUNTIME_WRITE_",
  "EXTERNAL_IMPORT_",
  "AUTO_LEARN_",
  "SMART_EXTRACTION_LLM_",
  "OPENAI_",
  "LLM_",
  "ROUTER_",
] as const;

function isHostEnvAllowlisted(key: string): boolean {
  return HOST_ENV_ALLOWLIST_KEYS.has(key) ||
    HOST_ENV_ALLOWLIST_PREFIXES.some((prefix) => key.startsWith(prefix));
}

const QUERY_MODE_NAMES = ["keyword", "semantic", "hybrid"] as const;

function endsWithUnescapedQuote(value: string, quote: string): boolean {
  if (!value.endsWith(quote)) {
    return false;
  }
  let backslashCount = 0;
  for (let index = value.length - 2; index >= 0 && value[index] === "\\"; index -= 1) {
    backslashCount += 1;
  }
  return backslashCount % 2 === 0;
}

function looksLikeEnvAssignmentLine(value: string): boolean {
  const normalized = value.replace(/^\uFEFF/u, "").trim().replace(/^export\s+/u, "");
  return /^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$/u.test(normalized);
}

export function readEnvAssignment(
  lines: string[],
  startIndex: number,
): { key: string; value: string; nextIndex: number; warning?: string } | undefined {
  if (!Number.isInteger(startIndex) || startIndex < 0 || startIndex >= lines.length) {
    return undefined;
  }
  const normalizedLine = lines[startIndex].replace(/^\uFEFF/u, "").trim().replace(/^export\s+/u, "");
  const match = normalizedLine.match(/^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$/u);
  if (!match) {
    return undefined;
  }

  const [, key, firstValue] = match;
  let value = firstValue;
  let nextIndex = startIndex;
  const quote = value[0];

  if ((quote === "\"" || quote === "'") && !endsWithUnescapedQuote(value, quote)) {
    let foundClosingQuote = false;
    while (nextIndex + 1 < lines.length) {
      const nextLine = lines[nextIndex + 1] ?? "";
      if (looksLikeEnvAssignmentLine(nextLine)) {
        return {
          key,
          value: stripWrappingQuotes(firstValue),
          nextIndex: startIndex,
          warning: `unterminated quoted value for ${key}`,
        };
      }
      nextIndex += 1;
      value += `\n${nextLine}`;
      if (endsWithUnescapedQuote(nextLine, quote)) {
        foundClosingQuote = true;
        break;
      }
    }
    if (!foundClosingQuote) {
      return {
        key,
        value: stripWrappingQuotes(firstValue),
        nextIndex: startIndex,
        warning: `unterminated quoted value for ${key}`,
      };
    }
  }

  return { key, value: stripWrappingQuotes(value), nextIndex };
}

function warnEnvFileIssue(
  logger: Pick<OpenClawPluginApi["logger"], "warn"> | undefined,
  warnedIssues: Set<string> | undefined,
  issueKey: string,
  message: string,
): void {
  if (!logger?.warn) {
    return;
  }
  if (warnedIssues?.has(issueKey)) {
    return;
  }
  warnedIssues?.add(issueKey);
  logger.warn(message);
}

function readEnvFileRecord(
  filePath: string | undefined,
  logger?: Pick<OpenClawPluginApi["logger"], "warn">,
  warnedIssues?: Set<string>,
): Record<string, string> {
  const resolved = readString(filePath);
  if (!resolved || !existsSync(resolved)) {
    return {};
  }
  try {
    const values: Record<string, string> = {};
    const content = fs.readFileSync(resolved, "utf8");
    const lines = content.split(/\r?\n/);
    for (let index = 0; index < lines.length; index += 1) {
      const rawLine = lines[index];
      const line = rawLine.replace(/^\uFEFF/u, "").trim();
      if (!line || line.startsWith("#")) {
        continue;
      }
      const assignment = readEnvAssignment(lines, index);
      if (!assignment) {
        continue;
      }
      if (assignment.warning) {
        warnEnvFileIssue(
          logger,
          warnedIssues,
          `${resolved}:${assignment.warning}`,
          `memory-palace runtime env file ${resolved} has ${assignment.warning}; using the first line only`,
        );
      }
      values[assignment.key] = assignment.value;
      index = assignment.nextIndex;
    }
    return values;
  } catch (error) {
    warnEnvFileIssue(
      logger,
      warnedIssues,
      `${resolved}:read_failed`,
      `memory-palace failed to read runtime env file ${resolved}: ${formatError(error)}`,
    );
    return {};
  }
}

export {
  formatError,
  normalizeVisualPathPrefix,
  parseJsonRecord,
  parseJsonRecordWithWarning,
  readVisualDuplicatePolicy,
  stripWrappingQuotes,
};

function readQueryMode(value: unknown): string | undefined {
  const normalized = readString(value)?.trim().toLowerCase();
  if (!normalized) {
    return undefined;
  }
  return (QUERY_MODE_NAMES as readonly string[]).includes(normalized) ? normalized : undefined;
}

function isIsolatedRuntimeEnvKey(key: string): boolean {
  return ISOLATED_RUNTIME_ENV_KEYS.has(key) ||
    ISOLATED_RUNTIME_ENV_PREFIXES.some((prefix) => key.startsWith(prefix));
}

function runtimeEnvFileExists(runtimeEnvFile: string | undefined): boolean {
  const rendered = readString(runtimeEnvFile);
  return Boolean(rendered && existsSync(rendered));
}

function sanitizeHostRuntimeEnv(
  values: Record<string, string>,
  runtimeEnvFile: string | undefined,
): Record<string, string> {
  if (!runtimeEnvFileExists(runtimeEnvFile)) {
    // No env file — apply allowlist so only known-needed keys pass through
    // instead of leaking the entire process.env.
    return Object.fromEntries(
      Object.entries(values).filter(([key]) => isHostEnvAllowlisted(key)),
    );
  }
  return Object.fromEntries(
    Object.entries(values).filter(([key]) => !isIsolatedRuntimeEnvKey(key)),
  );
}

export function resolveConfiguredEffectiveProfile(
  stdioEnv: Record<string, string> | undefined,
  logger?: Pick<OpenClawPluginApi["logger"], "warn">,
  warnedIssues?: Set<string>,
): HostPlatformProfile | undefined {
  const configured = readString(stdioEnv?.OPENCLAW_MEMORY_PALACE_PROFILE_EFFECTIVE)?.toLowerCase();
  if (configured === "a" || configured === "b" || configured === "c" || configured === "d") {
    return configured;
  }
  const envFileCandidates = [
    readString(stdioEnv?.OPENCLAW_MEMORY_PALACE_ENV_FILE),
    readString(process.env.OPENCLAW_MEMORY_PALACE_ENV_FILE),
  ];
  const hasConfiguredEnvFile = envFileCandidates.some((candidate) => Boolean(candidate));
  for (const candidate of envFileCandidates) {
    if (!candidate) {
      continue;
    }
    const envValues = readEnvFileRecord(candidate, logger, warnedIssues);
    const effective = readString(envValues.OPENCLAW_MEMORY_PALACE_PROFILE_EFFECTIVE)?.toLowerCase();
    if (effective === "a" || effective === "b" || effective === "c" || effective === "d") {
      return effective;
    }
  }
  if (hasConfiguredEnvFile) {
    return undefined;
  }
  const inherited = readString(process.env.OPENCLAW_MEMORY_PALACE_PROFILE_EFFECTIVE)?.toLowerCase();
  if (inherited === "a" || inherited === "b" || inherited === "c" || inherited === "d") {
    return inherited;
  }
  return undefined;
}

export function resolveRuntimeEnvConfig(
  stdioEnv: Record<string, string> | undefined,
  logger?: Pick<OpenClawPluginApi["logger"], "warn">,
  warnedIssues?: Set<string>,
): PluginConfig["runtimeEnv"] {
  const envFile = pickFirstNonBlank(
    stdioEnv?.OPENCLAW_MEMORY_PALACE_ENV_FILE,
    process.env.OPENCLAW_MEMORY_PALACE_ENV_FILE,
  );
  const inheritedHostValues = Object.fromEntries(
    Object.entries(process.env).map(([key, value]) => [key, value ?? ""]),
  );
  const hostValues = sanitizeHostRuntimeEnv(inheritedHostValues, envFile);
  const envFileValues = runtimeEnvFileExists(envFile)
    ? readEnvFileRecord(envFile!, logger, warnedIssues)
    : {};
  const stdioValues = stdioEnv ?? {};
  return {
    envFile,
    stdioValues,
    envFileValues,
    hostValues,
    values: mergeStringRecords(hostValues, envFileValues, stdioValues),
  };
}

export function resolveSmartExtractionEffectiveMode(
  config: PluginConfig["smartExtraction"],
): EffectiveSmartExtractionMode {
  if (!config.enabled || config.mode === "disabled") {
    return "off";
  }
  if (config.mode === "local" || config.mode === "remote") {
    return config.mode;
  }
  if (config.effectiveProfile === "d") {
    return "remote";
  }
  return "local";
}

export function resolveRuntimeEnvValueFromSources(
  runtimeEnv: PluginConfig["runtimeEnv"],
  ...keys: string[]
): string | undefined {
  for (const record of [runtimeEnv.stdioValues, runtimeEnv.envFileValues, runtimeEnv.hostValues]) {
    for (const key of keys) {
      if (!Object.hasOwn(record, key)) {
        continue;
      }
      const value = readString(record[key]);
      if (value?.trim()) {
        return value.trim();
      }
      return undefined;
    }
  }
  return undefined;
}

export function resolveSmartExtractionModelConfig(
  runtimeEnv: PluginConfig["runtimeEnv"],
): SmartExtractionModelConfig {
  return {
    baseUrl: normalizeChatApiBase(
      resolveRuntimeEnvValueFromSources(
        runtimeEnv,
        "SMART_EXTRACTION_LLM_API_BASE",
        "WRITE_GUARD_LLM_API_BASE",
        "OPENAI_BASE_URL",
        "OPENAI_API_BASE",
        "COMPACT_GIST_LLM_API_BASE",
      ),
    ),
    apiKey: resolveRuntimeEnvValueFromSources(
      runtimeEnv,
      "SMART_EXTRACTION_LLM_API_KEY",
      "WRITE_GUARD_LLM_API_KEY",
      "OPENAI_API_KEY",
      "COMPACT_GIST_LLM_API_KEY",
    ),
    model: resolveRuntimeEnvValueFromSources(
      runtimeEnv,
      "SMART_EXTRACTION_LLM_MODEL",
      "WRITE_GUARD_LLM_MODEL",
      "OPENAI_MODEL",
      "COMPACT_GIST_LLM_MODEL",
    ),
  };
}

export function resolveRuntimeEnvValue(config: PluginConfig, ...keys: string[]): string | undefined {
  return resolveRuntimeEnvValueFromSources(config.runtimeEnv, ...keys);
}

export function normalizeSmartExtractionCategory(
  value: string | undefined,
): SmartExtractionCategory | undefined {
  const normalized = readString(value)?.toLowerCase();
  if (!normalized) {
    return undefined;
  }
  const aliasMap: Record<string, SmartExtractionCategory> = {
    preferences: "preference",
    entities: "entity",
    events: "event",
    cases: "case",
    patterns: "pattern",
    reminders: "reminder",
  };
  const canonical = aliasMap[normalized] ?? normalized;
  return (SMART_EXTRACTION_CATEGORY_NAMES as readonly string[]).includes(canonical)
    ? (canonical as SmartExtractionCategory)
    : undefined;
}

export function displaySmartExtractionCategory(value: SmartExtractionCategory): string {
  const displayMap: Record<SmartExtractionCategory, string> = {
    profile: "profile",
    preference: "preferences",
    workflow: "workflow",
    entity: "entities",
    event: "events",
    case: "cases",
    pattern: "patterns",
    reminder: "reminders",
  };
  return displayMap[value];
}

export function readSmartExtractionCategoryArray(
  value: unknown,
): SmartExtractionCategory[] | undefined {
  const values = readStringArray(value)
    ?.map((entry) => normalizeSmartExtractionCategory(entry))
    .filter((entry): entry is SmartExtractionCategory => Boolean(entry));
  if (!values || values.length === 0) {
    return undefined;
  }
  return Array.from(new Set(values));
}

export function parseVisualEnrichmentCommandConfig(
  value: unknown,
  api: OpenClawPluginApi,
  defaultTimeoutMs: number,
): VisualEnrichmentCommandConfig | undefined {
  if (!isRecord(value)) {
    return undefined;
  }
  const command = readString(value.command);
  const args = readStringArray(value.args);
  const cwd = readString(value.cwd);
  const env = readStringMap(value.env);
  const timeoutMs = readPositiveNumber(value.timeoutMs) ?? defaultTimeoutMs;
  if (!command && !args && !cwd && !env) {
    return undefined;
  }
  return {
    command,
    args,
    cwd: cwd ? api.resolvePath(cwd) : undefined,
    env,
    timeoutMs,
  };
}

export function parsePluginConfig(
  raw: unknown,
  api: OpenClawPluginApi,
  options: ParsePluginConfigOptions,
): PluginConfig {
  const value = isRecord(raw) ? raw : {};
  const connectionRaw = isRecord(value.connection) ? value.connection : {};
  const stdioRaw = isRecord(value.stdio) ? value.stdio : {};
  const sseRaw = isRecord(value.sse) ? value.sse : {};
  const queryRaw = isRecord(value.query) ? value.query : {};
  const readRaw = isRecord(value.read) ? value.read : {};
  const mappingRaw = isRecord(value.mapping) ? value.mapping : {};
  const visualMemoryRaw = isRecord(value.visualMemory) ? value.visualMemory : {};
  const visualEnrichmentRaw = isRecord(visualMemoryRaw.enrichment) ? visualMemoryRaw.enrichment : {};
  const observabilityRaw = isRecord(value.observability) ? value.observability : {};
  const profileMemoryRaw = isRecord(value.profileMemory) ? value.profileMemory : {};
  const hostBridgeRaw = isRecord(value.hostBridge) ? value.hostBridge : {};
  const smartExtractionRaw = isRecord(value.smartExtraction) ? value.smartExtraction : {};
  const reconcileRaw = isRecord(value.reconcile) ? value.reconcile : {};
  const capturePipelineRaw = isRecord(value.capturePipeline) ? value.capturePipeline : {};
  const autoRecallRaw = isRecord(value.autoRecall) ? value.autoRecall : {};
  const autoCaptureRaw = isRecord(value.autoCapture) ? value.autoCapture : {};
  const aclRaw = isRecord(value.acl) ? value.acl : {};
  const reflectionRaw = isRecord(value.reflection) ? value.reflection : {};
  const apiKeyEnv = readString(sseRaw.apiKeyEnv);
  const apiKey = readString(sseRaw.apiKey) ?? (apiKeyEnv ? readString(process.env[apiKeyEnv]) : undefined);
  const stdioCommand = readString(stdioRaw.command);
  const stdioArgs = readStringArray(stdioRaw.args);
  const stdioCwd = readString(stdioRaw.cwd);
  const stdioEnv = readStringMap(stdioRaw.env);
  const warnedRuntimeEnvIssues = new Set<string>();
  const runtimeEnv = resolveRuntimeEnvConfig(stdioEnv, api.logger, warnedRuntimeEnvIssues);
  const effectiveProfile = resolveConfiguredEffectiveProfile(
    stdioEnv,
    api.logger,
    warnedRuntimeEnvIssues,
  );
  const defaultSmartExtractionEnabled = effectiveProfile === "c" || effectiveProfile === "d";
  const defaultSmartExtractionTimeoutMs = defaultSmartExtractionEnabled ? 60_000 : 8_000;
  const resolvedSmartExtractionModel = resolveSmartExtractionModelConfig(runtimeEnv);
  const hasExplicitStdioConfig = Boolean(stdioCommand || stdioArgs || stdioCwd || stdioEnv);
  const hasSseConfig = Boolean(readString(sseRaw.url));
  const mappingDefaultDomain = readString(mappingRaw.defaultDomain) ?? "core";
  const defaultVisualEnrichmentTimeoutMs = readPositiveNumber(visualEnrichmentRaw.timeoutMs) ?? 8_000;
  const configuredProfileBlocks = (readProfileBlockArray(profileMemoryRaw.blocks) ?? [])
    .map((entry) => entry.trim().toLowerCase())
    .filter((entry): entry is ProfileBlockName =>
      (PROFILE_BLOCK_NAMES as readonly string[]).includes(entry),
    )
    .filter((entry, index, values) => values.indexOf(entry) === index);
  const requestedTransport = readString(value.transport);
  const transportMode =
    requestedTransport === "stdio" || requestedTransport === "sse" || requestedTransport === "auto"
      ? requestedTransport
      : hasSseConfig && hasExplicitStdioConfig
        ? "auto"
        : hasSseConfig
          ? "sse"
          : "stdio";
  const aclAgentsRaw = isRecord(aclRaw.agents) ? aclRaw.agents : {};
  const aclAgents: PluginConfig["acl"]["agents"] = {};
  for (const [agentId, policyRaw] of Object.entries(aclAgentsRaw)) {
    if (!isRecord(policyRaw)) {
      continue;
    }
    aclAgents[agentId] = {
      allowedDomains: readStringArray(policyRaw.allowedDomains),
      allowedUriPrefixes: readStringArray(policyRaw.allowedUriPrefixes),
      writeRoots: readStringArray(policyRaw.writeRoots),
      disclosurePolicy: readString(policyRaw.disclosurePolicy),
      allowIncludeAncestors: readBoolean(policyRaw.allowIncludeAncestors),
    };
  }
  const reflectionSourceRaw = readString(reflectionRaw.source);
  const defaultStdioLaunch = options.resolveDefaultStdioLaunch(stdioEnv, options.hostPlatform);
  const configuredReconcileActions = readStringArray(reconcileRaw.actions)
    ?.map((entry) => readString(entry)?.toUpperCase())
    .filter((entry): entry is ReconcileAction =>
      entry === "ADD" || entry === "UPDATE" || entry === "DELETE" || entry === "NONE",
    );

  const parsed: PluginConfig = {
    transport: transportMode,
    timeoutMs: readPositiveNumber(value.timeoutMs),
    connection: {
      connectRetries:
        readNonNegativeNumber(connectionRaw.connectRetries) ??
        readNonNegativeNumber(value.connectRetries) ??
        1,
      connectBackoffMs:
        readPositiveNumber(connectionRaw.connectBackoffMs) ??
        readPositiveNumber(value.connectBackoffMs) ??
        250,
      connectBackoffMaxMs:
        readPositiveNumber(connectionRaw.connectBackoffMaxMs) ??
        Math.max(
          readPositiveNumber(connectionRaw.connectBackoffMs) ??
            readPositiveNumber(value.connectBackoffMs) ??
            250,
          1_000,
        ),
      requestRetries: readPositiveNumber(connectionRaw.requestRetries) ?? 2,
      idleCloseMs: readNonNegativeNumber(connectionRaw.idleCloseMs) ?? 1_500,
      healthcheckTool: readString(connectionRaw.healthcheckTool) ?? "index_status",
      healthcheckTtlMs: readNonNegativeNumber(connectionRaw.healthcheckTtlMs) ?? 5_000,
    },
    stdio: {
      command: stdioCommand ?? defaultStdioLaunch.command,
      args: stdioArgs ?? defaultStdioLaunch.args,
      cwd: stdioCwd ? api.resolvePath(stdioCwd) : defaultStdioLaunch.cwd,
      env: stdioEnv,
    },
    sse: {
      url: readString(sseRaw.url),
      apiKey,
      apiKeyEnv,
      headers: readStringMap(sseRaw.headers),
    },
    query: {
      mode: readQueryMode(queryRaw.mode),
      maxResults: readPositiveNumber(queryRaw.maxResults),
      candidateMultiplier: readPositiveNumber(queryRaw.candidateMultiplier),
      includeSession: readBoolean(queryRaw.includeSession),
      verbose: readBoolean(queryRaw.verbose),
      filters: parseJsonRecordWithWarning(queryRaw.filters, "config.query.filters", api.logger),
      scopeHint: readString(queryRaw.scopeHint),
    },
    read: {
      maxChars: readPositiveNumber(readRaw.maxChars),
      includeAncestors: readBoolean(readRaw.includeAncestors),
    },
    mapping: {
      virtualRoot: readString(mappingRaw.virtualRoot) ?? "memory-palace",
      defaultDomain: mappingDefaultDomain,
    },
    visualMemory: {
      enabled: readBoolean(visualMemoryRaw.enabled) ?? true,
      defaultDomain: readString(visualMemoryRaw.defaultDomain) ?? mappingDefaultDomain,
      pathPrefix: normalizeVisualPathPrefix(readString(visualMemoryRaw.pathPrefix)),
      maxSummaryChars: readPositiveNumber(visualMemoryRaw.maxSummaryChars),
      maxOcrChars: readPositiveNumber(visualMemoryRaw.maxOcrChars),
      duplicatePolicy: readVisualDuplicatePolicy(visualMemoryRaw.duplicatePolicy) ?? "merge",
      disclosure: readString(visualMemoryRaw.disclosure) ?? options.defaultVisualMemoryDisclosure,
      retentionNote:
        readString(visualMemoryRaw.retentionNote) ?? options.defaultVisualMemoryRetentionNote,
      traceEnabled: readBoolean(visualMemoryRaw.traceEnabled) ?? false,
      storeOcr: readBoolean(visualMemoryRaw.storeOcr) ?? true,
      storeEntities: readBoolean(visualMemoryRaw.storeEntities) ?? true,
      storeScene: readBoolean(visualMemoryRaw.storeScene) ?? true,
      storeWhyRelevant: readBoolean(visualMemoryRaw.storeWhyRelevant) ?? true,
      currentTurnCacheTtlMs: readNonNegativeNumber(visualMemoryRaw.currentTurnCacheTtlMs) ?? 900_000,
      enrichment: {
        enabled: readBoolean(visualEnrichmentRaw.enabled) ?? false,
        timeoutMs: defaultVisualEnrichmentTimeoutMs,
        ocr: parseVisualEnrichmentCommandConfig(
          visualEnrichmentRaw.ocr,
          api,
          defaultVisualEnrichmentTimeoutMs,
        ),
        analyzer: parseVisualEnrichmentCommandConfig(
          visualEnrichmentRaw.analyzer,
          api,
          defaultVisualEnrichmentTimeoutMs,
        ),
      },
    },
    observability: {
      enabled: readBoolean(observabilityRaw.enabled) ?? true,
      transportDiagnosticsPath: api.resolvePath(
        readString(observabilityRaw.transportDiagnosticsPath) ??
          readString(process.env[options.transportDiagnosticsPathEnv]) ??
          options.defaultTransportDiagnosticsPath,
      ),
      maxRecentTransportEvents: readPositiveNumber(observabilityRaw.maxRecentTransportEvents) ?? 12,
    },
    profileMemory: {
      enabled: readBoolean(profileMemoryRaw.enabled) ?? false,
      injectBeforeAgentStart: readBoolean(profileMemoryRaw.injectBeforeAgentStart) ?? true,
      maxCharsPerBlock: readPositiveNumber(profileMemoryRaw.maxCharsPerBlock) ?? 1_200,
      blocks: configuredProfileBlocks.length > 0 ? configuredProfileBlocks : [...PROFILE_BLOCK_NAMES],
    },
    hostBridge: {
      enabled: readBoolean(hostBridgeRaw.enabled) ?? true,
      importUserMd: readBoolean(hostBridgeRaw.importUserMd) ?? true,
      importMemoryMd: readBoolean(hostBridgeRaw.importMemoryMd) ?? true,
      importDailyMemory: readBoolean(hostBridgeRaw.importDailyMemory) ?? true,
      writeBackSummary: readBoolean(hostBridgeRaw.writeBackSummary) ?? false,
      maxHits: readPositiveNumber(hostBridgeRaw.maxHits) ?? 3,
      maxImportPerRun: readPositiveNumber(hostBridgeRaw.maxImportPerRun) ?? 2,
      maxFileBytes: readPositiveNumber(hostBridgeRaw.maxFileBytes) ?? 262_144,
      maxSnippetChars: readPositiveNumber(hostBridgeRaw.maxSnippetChars) ?? 220,
      traceEnabled: readBoolean(hostBridgeRaw.traceEnabled) ?? true,
    },
    smartExtraction: {
      enabled: readBoolean(smartExtractionRaw.enabled) ?? defaultSmartExtractionEnabled,
      mode:
        readString(smartExtractionRaw.mode) === "disabled" ||
        readString(smartExtractionRaw.mode) === "local" ||
        readString(smartExtractionRaw.mode) === "remote"
          ? (readString(smartExtractionRaw.mode) as SmartExtractionMode)
          : "auto",
      minConversationMessages: readPositiveNumber(smartExtractionRaw.minConversationMessages) ?? 2,
      maxTranscriptChars: readPositiveNumber(smartExtractionRaw.maxTranscriptChars) ?? 8_000,
      timeoutMs: readPositiveNumber(smartExtractionRaw.timeoutMs) ?? defaultSmartExtractionTimeoutMs,
      retryAttempts: readPositiveNumber(smartExtractionRaw.retryAttempts) ?? 2,
      circuitBreakerFailures: readPositiveNumber(smartExtractionRaw.circuitBreakerFailures) ?? 3,
      circuitBreakerCooldownMs: readPositiveNumber(smartExtractionRaw.circuitBreakerCooldownMs) ?? 300_000,
      categories:
        readSmartExtractionCategoryArray(smartExtractionRaw.categories) ??
        [...SMART_EXTRACTION_CATEGORY_NAMES],
      effectiveProfile,
      traceEnabled: readBoolean(smartExtractionRaw.traceEnabled) ?? true,
      effectiveMode: "off",
      modelAvailable: Boolean(resolvedSmartExtractionModel.baseUrl && resolvedSmartExtractionModel.model),
      modelName: resolvedSmartExtractionModel.model,
    },
    reconcile: {
      enabled: readBoolean(reconcileRaw.enabled) ?? defaultSmartExtractionEnabled,
      profileMergePolicy:
        readString(reconcileRaw.profileMergePolicy) === "replace" ? "replace" : "always_merge",
      eventMergePolicy:
        readString(reconcileRaw.eventMergePolicy) === "replace" ? "replace" : "append_only",
      similarityThreshold: Math.max(
        0,
        Math.min(
          1,
          typeof reconcileRaw.similarityThreshold === "number" &&
            Number.isFinite(reconcileRaw.similarityThreshold)
            ? reconcileRaw.similarityThreshold
            : 0.7,
        ),
      ),
      actions:
        configuredReconcileActions && configuredReconcileActions.length > 0
          ? configuredReconcileActions
          : ["ADD", "UPDATE", "NONE"],
      pendingOnConflict: readBoolean(reconcileRaw.pendingOnConflict) ?? true,
      maxSearchResults: readPositiveNumber(reconcileRaw.maxSearchResults) ?? 6,
    },
    capturePipeline: {
      mode: readString(capturePipelineRaw.mode) === "v1" ? "v1" : "v2",
      captureAssistantDerived:
        readBoolean(capturePipelineRaw.captureAssistantDerived) ??
        (effectiveProfile ? effectiveProfile !== "a" : false),
      maxAssistantDerivedPerRun: readPositiveNumber(capturePipelineRaw.maxAssistantDerivedPerRun) ?? 2,
      pendingOnFailure: readBoolean(capturePipelineRaw.pendingOnFailure) ?? true,
      minConfidence: Math.max(
        0,
        Math.min(
          1,
          typeof capturePipelineRaw.minConfidence === "number" &&
            Number.isFinite(capturePipelineRaw.minConfidence)
            ? capturePipelineRaw.minConfidence
            : 0.72,
        ),
      ),
      pendingConfidence: Math.max(
        0,
        Math.min(
          1,
          typeof capturePipelineRaw.pendingConfidence === "number" &&
            Number.isFinite(capturePipelineRaw.pendingConfidence)
            ? capturePipelineRaw.pendingConfidence
            : 0.55,
        ),
      ),
      effectiveProfile,
      traceEnabled: readBoolean(capturePipelineRaw.traceEnabled) ?? true,
    },
    autoRecall: {
      enabled: readBoolean(autoRecallRaw.enabled) ?? true,
      maxResults: readPositiveNumber(autoRecallRaw.maxResults) ?? 3,
      minPromptChars: readPositiveNumber(autoRecallRaw.minPromptChars) ?? 4,
      allowShortCjk: readBoolean(autoRecallRaw.allowShortCjk) ?? true,
      traceEnabled: readBoolean(autoRecallRaw.traceEnabled) ?? true,
    },
    autoCapture: {
      enabled: readBoolean(autoCaptureRaw.enabled) ?? true,
      minChars: readPositiveNumber(autoCaptureRaw.minChars) ?? 12,
      maxChars: readPositiveNumber(autoCaptureRaw.maxChars) ?? 800,
      maxItemsPerRun: readPositiveNumber(autoCaptureRaw.maxItemsPerRun) ?? 3,
      traceEnabled: readBoolean(autoCaptureRaw.traceEnabled) ?? true,
    },
    acl: {
      enabled: readBoolean(aclRaw.enabled) ?? false,
      sharedUriPrefixes: readStringArray(aclRaw.sharedUriPrefixes) ?? [],
      sharedWriteUriPrefixes: readStringArray(aclRaw.sharedWriteUriPrefixes) ?? [],
      defaultPrivateRootTemplate:
        readString(aclRaw.defaultPrivateRootTemplate) ?? "core://agents/{agentId}",
      allowIncludeAncestors: readBoolean(aclRaw.allowIncludeAncestors) ?? false,
      defaultDisclosure:
        readString(aclRaw.defaultDisclosure) ??
        "When agent memory recall is relevant for the current request.",
      agents: aclAgents,
    },
    reflection: {
      enabled: readBoolean(reflectionRaw.enabled) ?? false,
      autoRecall: readBoolean(reflectionRaw.autoRecall) ?? true,
      maxResults: readPositiveNumber(reflectionRaw.maxResults) ?? 2,
      rootUri: readString(reflectionRaw.rootUri) ?? "core://reflection",
      source:
        reflectionSourceRaw === "compact_context" ||
        reflectionSourceRaw === "agent_end" ||
        reflectionSourceRaw === "command_new"
          ? reflectionSourceRaw
          : "agent_end",
      compactMaxLines: readPositiveNumber(reflectionRaw.compactMaxLines) ?? 8,
      traceEnabled: readBoolean(reflectionRaw.traceEnabled) ?? true,
    },
    runtimeEnv,
  };
  parsed.smartExtraction.effectiveMode = resolveSmartExtractionEffectiveMode(parsed.smartExtraction);
  return parsed;
}

export function buildClientConfig(config: PluginConfig): MemoryPalaceClientConfig {
  const configuredConnectRetries = Math.max(0, config.connection.connectRetries);
  const configuredBackoffMs = Math.max(1, config.connection.connectBackoffMs);
  return {
    transport: config.transport,
    timeoutMs: config.timeoutMs,
    healthcheckTool: config.connection.healthcheckTool,
    healthcheckTtlMs: config.connection.healthcheckTtlMs,
    stdio: config.stdio,
    sse: {
      url: config.sse?.url,
      apiKey: config.sse?.apiKey,
      headers: config.sse?.headers,
    },
    retry: {
      attempts: configuredConnectRetries + 1,
      baseDelayMs: configuredBackoffMs,
      maxDelayMs: Math.max(configuredBackoffMs, config.connection.connectBackoffMaxMs),
    },
    requestRetries: Math.max(1, config.connection.requestRetries),
  };
}

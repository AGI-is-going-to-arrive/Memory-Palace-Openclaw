import type {
  MemoryPalaceClientDiagnostics,
  MemoryPalaceMcpClient,
} from "./client.js";

export type JsonRecord = Record<string, unknown>;
export type SearchSource = "memory" | "sessions";
export type TransportKind = "stdio" | "sse";
export type DiagnosticStatus = "pass" | "warn" | "fail";
export type HostPlatformProfile = "a" | "b" | "c" | "d";
export type SmartExtractionMode = "auto" | "disabled" | "local" | "remote";
export type EffectiveSmartExtractionMode = "off" | "local" | "remote";
export type SmartExtractionCategory =
  | "profile"
  | "preference"
  | "workflow"
  | "entity"
  | "event"
  | "case"
  | "pattern"
  | "reminder";
export type ReconcileAction = "ADD" | "UPDATE" | "DELETE" | "NONE";
export const PROFILE_BLOCK_NAMES = ["identity", "preferences", "workflow"] as const satisfies readonly ProfileBlockName[];
export const SMART_EXTRACTION_CATEGORY_NAMES = [
  "profile",
  "preference",
  "workflow",
  "entity",
  "event",
  "case",
  "pattern",
  "reminder",
] as const satisfies readonly SmartExtractionCategory[];

export type DiagnosticCheck = {
  id: string;
  status: DiagnosticStatus;
  message: string;
  code?: string;
  cause?: string;
  action?: string;
  details?: unknown;
};

export type ProfileBlockName = "identity" | "preferences" | "workflow";
export type HostPlatform = "windows" | "posix";
export type DefaultStdioLaunch = {
  command: string;
  args: string[];
  cwd: string;
};

export type PluginRuntimeCapturePath = {
  at: string;
  layer: string;
  category?: string;
  sourceMode?: string;
  uri?: string;
  pending?: boolean;
  action?: string;
  details?: string;
};

export type PluginRuntimeFallbackPath = {
  at: string;
  stage: string;
  reason: string;
  details?: string;
  degradedTo?: string;
};

export type PluginRuntimeRuleCaptureDecision = {
  at: string;
  decision: "captured" | "pending" | "skipped";
  reason: string;
  category?: string;
  uri?: string;
  pending?: boolean;
  details?: string;
};

export type PluginRuntimeCompactContext = {
  at: string;
  flushed: boolean;
  dataPersisted: boolean;
  reason: string;
  uri?: string;
  guardAction?: string;
  gistMethod?: string;
  sourceHash?: string;
};

export type PluginRuntimeCircuitState = {
  state: "closed" | "open";
  failureCount: number;
  openedAt?: string;
  lastFailureReason?: string;
  cooldownMs: number;
};

export type PluginRuntimeSignature = {
  effectiveProfile: HostPlatformProfile | "unknown";
  transport: PluginConfig["transport"];
  smartExtractionEnabled: boolean;
  smartExtractionMode: EffectiveSmartExtractionMode;
  smartExtractionModelAvailable: boolean;
  reconcileEnabled: boolean;
  autoCaptureEnabled: boolean;
  autoRecallEnabled: boolean;
  hostBridgeEnabled: boolean;
  visualMemoryEnabled: boolean;
  profileMemoryEnabled: boolean;
  profileMemoryInjectBeforeAgentStart: boolean;
  captureAssistantDerived: boolean;
};

export type PluginRuntimeState = {
  loaded: boolean;
  captureLayerCounts: Record<string, number>;
  recentCaptureLayers: PluginRuntimeCapturePath[];
  lastCapturePath?: PluginRuntimeCapturePath;
  lastFallbackPath?: PluginRuntimeFallbackPath;
  lastRuleCaptureDecision?: PluginRuntimeRuleCaptureDecision;
  lastCompactContext?: PluginRuntimeCompactContext;
  lastReconcile?: PluginRuntimeCapturePath;
  smartExtractionCircuit: PluginRuntimeCircuitState;
};

export type PluginRuntimeSnapshot = {
  captureLayerCounts: Record<string, number>;
  recentCaptureLayers: PluginRuntimeCapturePath[];
  lastCapturePath: PluginRuntimeCapturePath | null;
  lastFallbackPath: PluginRuntimeFallbackPath | null;
  lastRuleCaptureDecision: PluginRuntimeRuleCaptureDecision | null;
  lastCompactContext: PluginRuntimeCompactContext | null;
  lastReconcile: PluginRuntimeCapturePath | null;
  smartExtractionCircuit: PluginRuntimeCircuitState;
};

export type PluginRuntimeLayout = {
  pluginExtensionRoot: string;
  isRepoExtensionLayout: boolean;
  packagedScriptsRoot: string;
  packagedBackendRoot: string;
  isPackagedPluginLayout: boolean;
  pluginProjectRoot: string;
  defaultStdioWrapper: string;
  defaultTransportDiagnosticsPath: string;
  bundledSkillRoot: string;
};

export type SharedClientSession = {
  client: MemoryPalaceMcpClient;
  withClient<T>(run: (client: MemoryPalaceMcpClient) => Promise<T>): Promise<T>;
  close(): Promise<void>;
};

export type VisualDuplicatePolicy = "merge" | "reject" | "new";
export type VisualFieldSource =
  | "direct"
  | "context"
  | "derived"
  | "adapter"
  | "policy_disabled"
  | "missing";
export type VisualFieldProvider =
  | "direct"
  | "explicit_context"
  | "runtime_context"
  | "cached_context"
  | "derived"
  | "missing";
export type VisualEnrichmentField = "summary" | "ocr" | "scene" | "entities" | "whyRelevant";
export type VisualEnrichmentProviderName = "ocr" | "analyzer";
export type VisualEnrichmentCommandConfig = {
  command?: string;
  args?: string[];
  cwd?: string;
  env?: Record<string, string>;
  timeoutMs?: number;
};
export type RuntimeVisualSource =
  | "tool_context_only"
  | "message_preprocessed"
  | "before_prompt_build"
  | "agent_end";
export type RuntimeVisualProbe =
  | "none"
  | "tool_context_only"
  | "message_preprocessed"
  | "cli_store_visual_only";

export type TraceLogger = {
  debug?: (message: string) => void;
  info?: (message: string) => void;
  warn?: (message: string) => void;
};

export type PluginConfig = {
  transport: "auto" | "stdio" | "sse";
  timeoutMs?: number;
  connection: {
    connectRetries: number;
    connectBackoffMs: number;
    connectBackoffMaxMs: number;
    requestRetries: number;
    idleCloseMs: number;
    healthcheckTool: string;
    healthcheckTtlMs: number;
  };
  stdio?: {
    command?: string;
    args?: string[];
    cwd?: string;
    env?: Record<string, string>;
  };
  sse?: {
    url?: string;
    apiKey?: string;
    apiKeyEnv?: string;
    headers?: Record<string, string>;
  };
  query: {
    mode?: string;
    maxResults?: number;
    candidateMultiplier?: number;
    includeSession?: boolean;
    verbose?: boolean;
    filters?: JsonRecord;
    scopeHint?: string;
  };
  read: {
    maxChars?: number;
    includeAncestors?: boolean;
  };
  mapping: {
    virtualRoot: string;
    defaultDomain: string;
  };
  visualMemory: {
    enabled: boolean;
    defaultDomain: string;
    pathPrefix: string;
    maxSummaryChars?: number;
    maxOcrChars?: number;
    duplicatePolicy: VisualDuplicatePolicy;
    disclosure: string;
    retentionNote: string;
    traceEnabled: boolean;
    storeOcr: boolean;
    storeEntities: boolean;
    storeScene: boolean;
    storeWhyRelevant: boolean;
    currentTurnCacheTtlMs: number;
    enrichment: {
      enabled: boolean;
      timeoutMs: number;
      ocr?: VisualEnrichmentCommandConfig;
      analyzer?: VisualEnrichmentCommandConfig;
    };
  };
  observability: {
    enabled: boolean;
    transportDiagnosticsPath: string;
    maxRecentTransportEvents: number;
  };
  profileMemory: {
    enabled: boolean;
    injectBeforeAgentStart: boolean;
    maxCharsPerBlock: number;
    blocks: ProfileBlockName[];
  };
  hostBridge: {
    enabled: boolean;
    importUserMd: boolean;
    importMemoryMd: boolean;
    importDailyMemory: boolean;
    writeBackSummary: boolean;
    maxHits: number;
    maxImportPerRun: number;
    maxFileBytes: number;
    maxSnippetChars: number;
    traceEnabled: boolean;
  };
  smartExtraction: {
    enabled: boolean;
    mode: SmartExtractionMode;
    minConversationMessages: number;
    maxTranscriptChars: number;
    timeoutMs: number;
    retryAttempts: number;
    circuitBreakerFailures: number;
    circuitBreakerCooldownMs: number;
    categories: SmartExtractionCategory[];
    effectiveProfile?: HostPlatformProfile;
    traceEnabled: boolean;
    effectiveMode: EffectiveSmartExtractionMode;
    modelAvailable: boolean;
    modelName?: string;
  };
  reconcile: {
    enabled: boolean;
    profileMergePolicy: "always_merge" | "replace";
    eventMergePolicy: "append_only" | "replace";
    similarityThreshold: number;
    actions: ReconcileAction[];
    pendingOnConflict: boolean;
    maxSearchResults: number;
  };
  capturePipeline: {
    mode: "v1" | "v2";
    captureAssistantDerived: boolean;
    maxAssistantDerivedPerRun: number;
    pendingOnFailure: boolean;
    minConfidence: number;
    pendingConfidence: number;
    effectiveProfile?: HostPlatformProfile;
    traceEnabled: boolean;
  };
  autoRecall: {
    enabled: boolean;
    maxResults: number;
    minPromptChars: number;
    allowShortCjk: boolean;
    traceEnabled: boolean;
  };
  autoCapture: {
    enabled: boolean;
    minChars: number;
    maxChars: number;
    maxItemsPerRun: number;
    traceEnabled: boolean;
  };
  acl: {
    enabled: boolean;
    sharedUriPrefixes: string[];
    sharedWriteUriPrefixes: string[];
    defaultPrivateRootTemplate: string;
    allowIncludeAncestors: boolean;
    defaultDisclosure: string;
    agents: Record<
      string,
      {
        allowedDomains?: string[];
        allowedUriPrefixes?: string[];
        writeRoots?: string[];
        disclosurePolicy?: string;
        allowIncludeAncestors?: boolean;
      }
    >;
  };
  reflection: {
    enabled: boolean;
    autoRecall: boolean;
    maxResults: number;
    rootUri: string;
    source: "agent_end" | "compact_context" | "command_new";
    compactMaxLines: number;
    traceEnabled: boolean;
  };
  runtimeEnv: {
    envFile?: string;
    stdioValues: Record<string, string>;
    envFileValues: Record<string, string>;
    hostValues: Record<string, string>;
    values: Record<string, string>;
  };
};

export type DiagnosticReport = {
  command: "verify" | "doctor" | "smoke";
  ok: boolean;
  status: DiagnosticStatus;
  code: string;
  summary: string;
  connectionModel: "persistent-client";
  configuredTransport: PluginConfig["transport"];
  fallbackOrder: string[];
  activeTransport: string | null;
  checks: DiagnosticCheck[];
  diagnostics?: MemoryPalaceClientDiagnostics;
  runtimeState?: PluginRuntimeSnapshot;
  nextActions?: string[];
};

export type ResolvedAclPolicy = {
  enabled: boolean;
  agentId?: string;
  agentKey: string;
  explicitAllowedDomains: string[];
  allowedDomains: string[];
  allowedUriPrefixes: string[];
  writeRoots: string[];
  allowIncludeAncestors: boolean;
  disclosure: string;
};

export type RecallDecision = {
  shouldRecall: boolean;
  forced: boolean;
  cjkException: boolean;
  reasons: string[];
};

export type HostWorkspaceSourceKind = "user-md" | "memory-md" | "daily-memory";
export type DurableSynthesisSourceMode =
  | "assistant_derived"
  | "host_workspace_import"
  | "llm_extracted"
  | "rule_capture";
export type DurableSynthesisEvidence = {
  key: string;
  source: string;
  lineStart: number;
  lineEnd: number;
  snippet: string;
};
export type HostWorkspaceHit = {
  workspaceDir: string;
  workspaceRelativePath: string;
  sourceKind: HostWorkspaceSourceKind;
  absolutePath: string;
  lineStart: number;
  lineEnd: number;
  text: string;
  snippet: string;
  score: number;
  category: string;
  contentHash: string;
  citation: string;
};
export type AssistantDerivedCandidate = {
  category: string;
  summary: string;
  confidence: number;
  evidence: DurableSynthesisEvidence[];
  pending: boolean;
};
export type SmartExtractionCandidate = {
  category: SmartExtractionCategory;
  summary: string;
  confidence: number;
  evidence: DurableSynthesisEvidence[];
  pending: boolean;
};
export type SmartExtractionModelConfig = {
  baseUrl?: string;
  apiKey?: string;
  model?: string;
};

export type SearchScopePlan = {
  domain?: string;
  pathPrefix?: string;
  filters?: JsonRecord;
};

export type MemorySearchResult = {
  path: string;
  startLine: number;
  endLine: number;
  score: number;
  snippet: string;
  source: SearchSource;
  memoryId?: number;
  citation?: string;
  charRange?: { start: number; end: number };
};

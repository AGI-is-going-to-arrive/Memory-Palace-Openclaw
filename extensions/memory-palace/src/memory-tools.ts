import { createHash } from "node:crypto";
import type {
  AnyAgentTool,
  OpenClawPluginToolContext,
} from "openclaw/plugin-sdk/core";
import {
  memoryGetSchema,
  memoryLearnSchema,
  memorySearchSchema,
  memoryStoreVisualSchema,
} from "./config-schema.js";
import type {
  JsonRecord,
  PluginConfig,
  PluginRuntimeCapturePath,
  ProfileBlockName,
  ResolvedAclPolicy,
  SharedClientSession,
  TraceLogger,
  VisualDuplicatePolicy,
} from "./types.js";
import type {
  ResolvedVisualInput,
  VisualContextPayload,
} from "./visual-context.js";

type SearchPayload = {
  results: Array<{ score: number }>;
  [key: string]: unknown;
};

type VisualInput = {
  mediaRef: string;
  summary: string;
  sourceChannel?: string;
  observedAt?: string;
  ocr?: string;
  scene?: string;
  whyRelevant?: string;
  confidence?: number;
  entities?: string[];
  fieldSources: Record<string, unknown>;
  runtimeSource?: string;
  runtimeProbe: string;
};

type AliasIndex = {
  byMemoryId: Map<number, string[]>;
  memoryIdByUri: Map<string, number>;
};

type GuardSummary = {
  action?: string;
  reason?: string;
  targetUri?: string;
  blockedReason: string;
  canRetryWithForce: boolean;
};

type MemoryWriteResult = {
  ok: boolean;
  created: boolean;
  merged: boolean;
  uri: string;
  message?: string;
  raw?: JsonRecord;
  merge_error?: string;
  forced?: boolean;
};

type ProfileMemoryBlockUpsertResult = {
  ok: boolean;
  created: boolean;
  merged: boolean;
  uri: string;
  message?: string;
};

export type MemoryToolDeps = {
  buildUnavailableSearchResult: (error: unknown) => unknown;
  buildVisualMemoryContent: (params: {
    mediaRef: string;
    summary: string;
    [key: string]: unknown;
  }) => string;
  buildVisualMemoryUri: (
    mediaRef: string,
    observedAt: string | undefined,
    defaultDomain: string,
    pathPrefix: string,
  ) => string;
  extractPayloadFailureMessage: (value: unknown) => string;
  extractReadText: (raw: unknown) => {
    text: string;
    selection?: unknown;
    degraded?: boolean;
    error?: string;
  };
  extractRenderedMemoryId: (text: string) => number | undefined;
  formatError: (error: unknown) => string;
  buildAutoCaptureContent: (params: {
    agentId?: string;
    sessionId?: string;
    sessionKey?: string;
    category: string;
    text: string;
  }) => string;
  buildAutoCaptureUri: (
    config: PluginConfig,
    policy: ResolvedAclPolicy,
    category: string,
    text: string,
  ) => string;
  buildProfileMemoryUri: (
    config: PluginConfig,
    policy: ResolvedAclPolicy,
    block: ProfileBlockName,
  ) => string;
  createOrMergeMemoryRecord: (
    client: SharedClientSession["client"],
    targetUri: string,
    content: string,
    options: {
      priority: number;
      disclosure: string;
      lane?: "capture" | "profile" | "reflection";
      forceOnBlocked?: boolean;
      returnGuardFailures?: boolean;
    },
  ) => Promise<MemoryWriteResult>;
  getParam: (record: Record<string, unknown>, name: string) => unknown;
  inferCaptureCategory: (text: string) => string;
  isMemoryIdAllowedByAcl: (
    memoryId: number | undefined,
    aliasIndex: AliasIndex,
    policy: ResolvedAclPolicy,
    defaultDomain: string,
  ) => boolean;
  isTransientSqliteLockError: (value: unknown) => boolean;
  isUriAllowedByAcl: (
    uri: string,
    policy: ResolvedAclPolicy,
    defaultDomain: string,
  ) => boolean;
  isUriWritableByAcl: (
    uri: string,
    policy: ResolvedAclPolicy,
    defaultDomain: string,
  ) => boolean;
  jsonResult: (value: unknown) => unknown;
  loadMemoryAliasIndex: (
    client: SharedClientSession["client"],
    defaultDomain: string,
  ) => Promise<AliasIndex>;
  logTrace: (
    logger: TraceLogger | undefined,
    enabled: boolean,
    eventName: string,
    payload: Record<string, unknown>,
  ) => void;
  maybeEnrichVisualInput: (
    visualConfig: PluginConfig["visualMemory"],
    input: ResolvedVisualInput,
    logger?: TraceLogger,
  ) => Promise<ResolvedVisualInput>;
  parseJsonRecordWithWarning: (
    value: unknown,
    fieldName: string,
    logger?: TraceLogger,
  ) => JsonRecord | undefined;
  payloadIndicatesFailure: (value: unknown) => boolean;
  persistTransportDiagnosticsSnapshot: (
    config: PluginConfig,
    client: SharedClientSession["client"],
  ) => void;
  mapCaptureCategoryToProfileBlock: (category: string) => ProfileBlockName | undefined;
  readBoolean: (value: unknown) => boolean | undefined;
  readPositiveNumber: (value: unknown) => number | undefined;
  readString: (value: unknown) => string | undefined;
  readVisualDuplicatePolicy: (
    value: unknown,
  ) => VisualDuplicatePolicy | undefined;
  recordPluginCapturePath: (
    config: PluginConfig,
    client: SharedClientSession["client"] | undefined,
    payload: PluginRuntimeCapturePath,
  ) => void;
  rememberVisualContext: (
    context: OpenClawPluginToolContext | undefined,
    payload: VisualContextPayload,
    ttlMs: number,
  ) => void;
  resolveContextAgentIdentity: (
    context?: Record<string, unknown>,
  ) => { value?: string };
  resolveAclPolicy: (
    config: PluginConfig,
    agentId?: string,
  ) => ResolvedAclPolicy;
  resolveMemoryIdFromAliasIndex: (
    uri: string,
    renderedMemoryId: number | undefined,
    aliasIndex: AliasIndex,
    defaultDomain: string,
  ) => number | undefined;
  resolvePathLikeToUri: (
    pathOrUri: string,
    mapping: PluginConfig["mapping"],
  ) => string;
  resolveVisualInput: (
    record: Record<string, unknown>,
    visualConfig: PluginConfig["visualMemory"],
    context?: OpenClawPluginToolContext,
  ) => { value?: ResolvedVisualInput; error?: string };
  runScopedSearch: (
    client: SharedClientSession["client"],
    query: string,
    config: PluginConfig,
    policy: ResolvedAclPolicy,
    options?: {
      mode?: string;
      maxResults?: number;
      candidateMultiplier?: number;
      includeSession?: boolean;
      verbose?: boolean;
      scopeHint?: string;
      filters?: JsonRecord;
      includeReflection?: boolean;
    },
  ) => Promise<SearchPayload>;
  shouldIncludeReflection: (
    paramsRecord: Record<string, unknown>,
    config: PluginConfig,
    policy: ResolvedAclPolicy,
    parsedFilters: JsonRecord | undefined,
    logger?: TraceLogger,
  ) => boolean;
  sliceTextByLines: (text: string, from?: number, lines?: number) => string;
  storeVisualMemoryRecord: (
    client: SharedClientSession["client"],
    uri: string,
    content: string,
    mapping: PluginConfig["mapping"],
    duplicatePolicy: VisualDuplicatePolicy,
    disclosure: string,
  ) => Promise<Record<string, unknown>>;
  uriToVirtualPath: (
    uri: string,
    mapping: PluginConfig["mapping"],
  ) => string;
  upsertProfileMemoryBlockWithTransientRetry: (
    client: SharedClientSession["client"],
    config: PluginConfig,
    policy: ResolvedAclPolicy,
    profileBlock: ProfileBlockName,
    text: string,
  ) => Promise<ProfileMemoryBlockUpsertResult>;
  withTransientSqliteLockRetry: <T>(
    operation: () => Promise<T>,
    shouldRetry?: (value: T) => boolean,
    maxAttempts?: number,
    initialDelayMs?: number,
  ) => Promise<T>;
};

function normalizeGuardAction(value: unknown): string | undefined {
  if (typeof value !== "string") {
    return undefined;
  }
  const normalized = value.trim().toUpperCase();
  return normalized || undefined;
}

function normalizeGuardReason(value: unknown): string | undefined {
  if (typeof value !== "string") {
    return undefined;
  }
  const normalized = value.trim();
  return normalized || undefined;
}

function summarizeGuardBlock(params: {
  action?: string;
  reason?: string;
  targetUri?: string;
}): GuardSummary {
  const action = normalizeGuardAction(params.action);
  const reason = normalizeGuardReason(params.reason);
  const targetUri =
    typeof params.targetUri === "string" && params.targetUri.trim().length > 0
      ? params.targetUri.trim()
      : undefined;
  const reasonLower = reason?.toLowerCase() ?? "";
  const invalidGuard = reasonLower.includes("invalid_guard_action");
  const unavailableGuard = reasonLower.includes("write_guard_unavailable");
  const canRetryWithForce = !invalidGuard && !unavailableGuard && (action === "NOOP" || action === "UPDATE");

  if (unavailableGuard) {
    return {
      action,
      reason,
      targetUri,
      blockedReason:
        "The write guard was unavailable, so the memory write was stopped for safety.",
      canRetryWithForce,
    };
  }
  if (invalidGuard) {
    return {
      action,
      reason,
      targetUri,
      blockedReason:
        "The write guard returned an invalid decision, so the memory write was stopped for safety.",
      canRetryWithForce,
    };
  }
  if (action === "UPDATE" && targetUri) {
    return {
      action,
      reason,
      targetUri,
      blockedReason:
        `This content already maps to an existing durable memory at ${targetUri}, so a separate memory was not created.`,
      canRetryWithForce,
    };
  }
  if (targetUri) {
    return {
      action,
      reason,
      targetUri,
      blockedReason:
        `This content looks too close to an existing durable memory at ${targetUri}, so a duplicate memory was not created.`,
      canRetryWithForce,
    };
  }
  if (action === "UPDATE") {
    return {
      action,
      reason,
      targetUri,
      blockedReason:
        "This content already appears to belong to an existing durable memory, so a separate memory was not created.",
      canRetryWithForce,
    };
  }
  return {
    action,
    reason,
    targetUri,
    blockedReason:
      "This content looked duplicate or not distinct enough, so a new durable memory was not created.",
    canRetryWithForce,
  };
}

export function createMemoryTools(
  options: {
    config: PluginConfig;
    context?: OpenClawPluginToolContext;
    deps: MemoryToolDeps;
    logger?: TraceLogger;
    session: SharedClientSession;
  },
): AnyAgentTool[] {
  const { config, context, deps, logger, session } = options;
  const withClient = session.withClient;
  const identity = deps.resolveContextAgentIdentity(context);
  const policy = deps.resolveAclPolicy(config, identity.value);
  const contextRecord = (context ?? {}) as Record<string, unknown>;

  const searchTool: AnyAgentTool = {
    label: "Memory Palace Search",
    name: "memory_search",
    description:
      "Search Memory Palace over MCP and return OpenClaw-compatible memory hits. Accepts URI-backed memory mapped into virtual memory-palace/*.md paths.",
    parameters: memorySearchSchema,
    execute: async (_toolCallId: string, params: unknown) => {
      const paramsRecord = params as Record<string, unknown>;
      const query = deps.readString(deps.getParam(paramsRecord, "query"));
      if (!query) {
        throw new Error("query required");
      }

      const minScore = deps.readPositiveNumber(deps.getParam(paramsRecord, "minScore"));
      const rawFilters = deps.getParam(paramsRecord, "filters");
      const parsedFilters = deps.parseJsonRecordWithWarning(
        rawFilters,
        "tool.memory_search.filters",
        logger,
      );
      const includeReflection = deps.shouldIncludeReflection(
        paramsRecord,
        config,
        policy,
        parsedFilters ?? (rawFilters !== undefined ? {} : undefined),
        logger,
      );

      try {
        const result = await withClient(async (client) => {
          const normalized = await deps.withTransientSqliteLockRetry(
            () =>
              deps.runScopedSearch(client, query, config, policy, {
                mode:
                  deps.readString(deps.getParam(paramsRecord, "mode")) ??
                  config.query.mode,
                maxResults:
                  deps.readPositiveNumber(
                    deps.getParam(paramsRecord, "maxResults"),
                  ) ?? config.query.maxResults,
                candidateMultiplier:
                  deps.readPositiveNumber(
                    deps.getParam(paramsRecord, "candidateMultiplier"),
                  ) ?? config.query.candidateMultiplier,
                includeSession:
                  deps.readBoolean(
                    deps.getParam(paramsRecord, "includeSession"),
                  ) ?? config.query.includeSession,
                verbose:
                  deps.readBoolean(
                    deps.getParam(paramsRecord, "verbose"),
                  ) ?? config.query.verbose,
                scopeHint:
                  deps.readString(deps.getParam(paramsRecord, "scopeHint")) ??
                  config.query.scopeHint,
                filters: parsedFilters ?? config.query.filters,
                includeReflection,
              }),
            (payload) =>
              deps.payloadIndicatesFailure(payload) &&
              deps.isTransientSqliteLockError(
                deps.extractPayloadFailureMessage(payload),
              ),
            5,
            150,
          );
          if (minScore === undefined) {
            return normalized;
          }
          return {
            ...normalized,
            results: normalized.results.filter((entry) => entry.score >= minScore),
          };
        });
        deps.persistTransportDiagnosticsSnapshot(config, session.client);
        return deps.jsonResult(result);
      } catch (error) {
        deps.persistTransportDiagnosticsSnapshot(config, session.client);
        return deps.jsonResult(deps.buildUnavailableSearchResult(error));
      }
    },
  };

  const learnTool: AnyAgentTool = {
    label: "Memory Palace Learn",
    name: "memory_learn",
    description:
      "Persist an explicit durable memory when the user clearly asks you to remember a stable fact, preference, or workflow for future turns.",
    parameters: memoryLearnSchema,
    execute: async (_toolCallId: string, params: unknown) => {
      const paramsRecord = params as Record<string, unknown>;
      const content = deps.readString(deps.getParam(paramsRecord, "content"))?.trim();
      if (!content) {
        throw new Error("content required");
      }
      const requestedCategory =
        deps.readString(deps.getParam(paramsRecord, "category"))?.trim().toLowerCase();
      const category =
        requestedCategory && [
          "profile",
          "preference",
          "workflow",
          "decision",
          "fact",
          "reminder",
          "event",
        ].includes(requestedCategory)
          ? requestedCategory
          : deps.inferCaptureCategory(content);
      const disclosure =
        deps.readString(deps.getParam(paramsRecord, "disclosure")) ?? policy.disclosure;
      const requestedConfirmationPhrase =
        deps.readString(deps.getParam(paramsRecord, "confirmationPhrase"))?.trim() ??
        deps.readString(deps.getParam(paramsRecord, "confirmation_phrase"))?.trim();
      const acknowledgementContext = `${content}\n${requestedConfirmationPhrase ?? ""}`;
      const prefersCjkAcknowledgement =
        /[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}\p{Script=Hangul}]/u.test(
          acknowledgementContext,
        );
      const blockedAcknowledgement = prefersCjkAcknowledgement
        ? "已暂停。尚未存入。"
        : "Paused. Not stored yet.";
      const priority =
        deps.readPositiveNumber(deps.getParam(paramsRecord, "priority")) ??
        (category === "profile" || category === "preference" || category === "workflow"
          ? 1
          : 2);
      const force = deps.readBoolean(deps.getParam(paramsRecord, "force")) ?? false;
      const targetUri = deps.buildAutoCaptureUri(config, policy, category, content);
      const captureWritable = deps.isUriWritableByAcl(
        targetUri,
        policy,
        config.mapping.defaultDomain,
      );
      const profileBlock = config.profileMemory.enabled
        ? deps.mapCaptureCategoryToProfileBlock(category)
        : undefined;
      const profileTargetUri = profileBlock
        ? deps.buildProfileMemoryUri(config, policy, profileBlock)
        : undefined;
      const profileWritable = profileTargetUri
        ? deps.isUriWritableByAcl(profileTargetUri, policy, config.mapping.defaultDomain)
        : false;
      if (!captureWritable && !profileWritable) {
        return deps.jsonResult({
          ok: false,
          error: "ACL denied memory_learn write outside the configured write roots.",
          uri: targetUri,
          category,
        });
      }

      let profileBlockActuallyUpdated = false;
      try {
        const result: MemoryWriteResult = await withClient(async (client): Promise<MemoryWriteResult> => {
          // M-2: For profile_block_only path (no capture write), upsert
          // profile block immediately since there is no guard to block.
          if (!captureWritable) {
            if (profileBlock && profileWritable) {
              const profileResult = await deps.upsertProfileMemoryBlockWithTransientRetry(
                client,
                config,
                policy,
                profileBlock,
                content,
              );
              profileBlockActuallyUpdated = profileResult.ok;
              if (!profileResult.ok) {
                return {
                  ok: false,
                  created: false,
                  merged: false,
                  uri: profileTargetUri ?? targetUri,
                  message: profileResult.message ?? "profile_block_write_failed",
                };
              }
            }
            return {
              ok: true,
              created: false,
              merged: true,
              uri: profileTargetUri ?? targetUri,
              message: "profile_block_only",
            };
          }
          const captureResult = await deps.createOrMergeMemoryRecord(
            client,
            targetUri,
            deps.buildAutoCaptureContent({
              agentId: identity.value,
              sessionId: deps.readString(contextRecord.sessionId),
              sessionKey: deps.readString(contextRecord.sessionKey),
              category,
              text: content,
            }),
            {
              priority,
              disclosure,
              lane: "capture",
              forceOnBlocked: force,
              returnGuardFailures: true,
            },
          );
          // M-2: Only upsert profile block AFTER capture succeeds.
          // If the guard blocked the capture (ok=false), skip the profile
          // block to preserve "block first, confirm later" semantics.
          if (captureResult.ok && profileBlock && profileWritable) {
            try {
              const profileResult = await deps.upsertProfileMemoryBlockWithTransientRetry(
                client,
                config,
                policy,
                profileBlock,
                content,
              );
              profileBlockActuallyUpdated = profileResult.ok;
            } catch {
              // Profile block failed but capture already persisted.
              // Don't mask the successful capture — report ok:true with
              // profileBlockUpdated:false so callers see the real state.
            }
          }
          return captureResult;
        });
        const raw = (result.raw ?? null) as Record<string, unknown> | null;
        const blocked =
          !result.ok &&
          normalizeGuardAction(raw?.guard_action) !== undefined;
        const guardSummary = blocked
          ? summarizeGuardBlock({
              action: deps.readString(raw?.guard_action),
              reason: deps.readString(raw?.guard_reason) ?? result.message,
              targetUri: deps.readString(raw?.guard_target_uri) ?? result.uri,
            })
          : undefined;
        deps.recordPluginCapturePath(config, session.client, {
          at: new Date().toISOString(),
          layer: "manual_learn",
          category,
          uri: result.uri,
          pending: false,
          action: blocked ? "SKIP" : result.merged ? "UPDATE" : "ADD",
          details: content.slice(0, 160),
        });
        deps.persistTransportDiagnosticsSnapshot(config, session.client);
        return deps.jsonResult({
          ok: result.ok,
          created: result.created,
          merged: result.merged,
          forced: Boolean(result.forced),
          explicit: true,
          category,
          uri: result.uri,
          profileBlockUpdated: profileBlockActuallyUpdated,
          message:
            result.message ??
            (guardSummary ? "write_guard_blocked" : "stored"),
          acknowledgement:
            result.ok
              ? (requestedConfirmationPhrase && requestedConfirmationPhrase.length > 0
                ? requestedConfirmationPhrase
                : "Stored.")
              : guardSummary
                ? blockedAcknowledgement
                : undefined,
          assistant_hint: result.ok
            ? "Acknowledge this with a minimal confirmation. If the user explicitly asked for a specific confirmation phrase, use that exact phrase verbatim. Otherwise use 'Stored.' or a brief equivalent in the user's language. Do not repeat the memory unless the user asks."
            : guardSummary
              ? "Acknowledge the blocked write with the acknowledgement string verbatim as your first sentence. Do not imply the memory was stored. Then explain the blocked_reason_human briefly. If the user confirms they still want a separate durable memory saved, rerun memory_learn immediately with force=true and the retry_with_force_payload content instead of asking again."
              : undefined,
          blocked: Boolean(guardSummary),
          blocked_reason_human: guardSummary?.blockedReason,
          guard_action: guardSummary?.action,
          guard_reason: guardSummary?.reason,
          guard_target_uri: guardSummary?.targetUri,
          can_retry_with_force: guardSummary?.canRetryWithForce ?? false,
          force_requires_user_confirmation: guardSummary?.canRetryWithForce ?? false,
          retry_with_force_payload: guardSummary?.canRetryWithForce
            ? {
                content,
                category,
                priority,
                disclosure,
                force: true,
                ...(requestedConfirmationPhrase && requestedConfirmationPhrase.length > 0
                  ? { confirmationPhrase: requestedConfirmationPhrase }
                  : {}),
              }
            : undefined,
          suggested_next_step: guardSummary?.canRetryWithForce
            ? "If the user confirms they still want a separate durable memory saved, rerun memory_learn with force=true and the retry_with_force_payload fields."
            : undefined,
        });
      } catch (error) {
        deps.persistTransportDiagnosticsSnapshot(config, session.client);
        return deps.jsonResult({
          ok: false,
          explicit: true,
          category,
          uri: targetUri,
          error: deps.formatError(error),
        });
      }
    },
  };

  const getTool: AnyAgentTool = {
    label: "Memory Palace Get",
    name: "memory_get",
    description:
      "Read a Memory Palace memory by URI or virtual path after memory_search, returning a safe text payload compatible with OpenClaw memory_get.",
    parameters: memoryGetSchema,
    execute: async (_toolCallId: string, params: unknown) => {
      const paramsRecord = params as Record<string, unknown>;
      const requested =
        deps.readString(deps.getParam(paramsRecord, "path")) ??
        deps.readString(deps.getParam(paramsRecord, "uri"));
      if (!requested) {
        throw new Error("path or uri required");
      }

      const normalizedRequested = requested.replace(/#C\d+(?:-|:)\d+$/i, "");
      const uri = deps.resolvePathLikeToUri(normalizedRequested, config.mapping);
      const virtualPath = deps.uriToVirtualPath(uri, config.mapping);
      const from = deps.readPositiveNumber(deps.getParam(paramsRecord, "from"));
      const lines = deps.readPositiveNumber(deps.getParam(paramsRecord, "lines"));
      const maxChars =
        deps.readPositiveNumber(deps.getParam(paramsRecord, "maxChars")) ??
        config.read.maxChars;
      const includeAncestors =
        deps.readBoolean(deps.getParam(paramsRecord, "includeAncestors")) ??
        config.read.includeAncestors;
      if (!deps.isUriAllowedByAcl(uri, policy, config.mapping.defaultDomain)) {
        return deps.jsonResult({
          path: virtualPath,
          uri,
          text: "",
          disabled: true,
          error: "ACL denied read access to the requested memory URI.",
        });
      }
      if (policy.enabled && includeAncestors && !policy.allowIncludeAncestors) {
        return deps.jsonResult({
          path: virtualPath,
          uri,
          text: "",
          disabled: true,
          error:
            "ACL denied includeAncestors because ancestor expansion may escape the allowed root.",
        });
      }

      try {
        const result = await withClient(async (client) => {
          const raw = await client.readMemory({
            uri,
            ...(maxChars ? { max_chars: maxChars } : {}),
            ...(includeAncestors ? { include_ancestors: true } : {}),
          });
          const extracted = deps.extractReadText(raw);
          if (extracted.error) {
            throw new Error(extracted.error);
          }
          if (policy.enabled) {
            const aliasIndex = await deps.loadMemoryAliasIndex(
              client,
              config.mapping.defaultDomain,
            );
            const memoryId = deps.resolveMemoryIdFromAliasIndex(
              uri,
              deps.extractRenderedMemoryId(extracted.text),
              aliasIndex,
              config.mapping.defaultDomain,
            );
            if (
              !deps.isMemoryIdAllowedByAcl(
                memoryId,
                aliasIndex,
                policy,
                config.mapping.defaultDomain,
              )
            ) {
              throw new Error(
                "ACL denied read access because the requested path resolves to a memory aliased outside the allowed roots.",
              );
            }
          }
          return {
            path: virtualPath,
            uri,
            text: deps.sliceTextByLines(extracted.text, from, lines),
            degraded: extracted.degraded ?? false,
            selection: extracted.selection,
          };
        });
        deps.persistTransportDiagnosticsSnapshot(config, session.client);
        return deps.jsonResult(result);
      } catch (error) {
        deps.persistTransportDiagnosticsSnapshot(config, session.client);
        return deps.jsonResult({
          path: virtualPath,
          uri,
          text: "",
          disabled: true,
          error: deps.formatError(error),
        });
      }
    },
  };

  const storeVisualTool: AnyAgentTool = {
    label: "Memory Palace Store Visual",
    name: "memory_store_visual",
    description:
      "Store a visual-memory text record in Memory Palace using an existing caption/OCR/scene summary.",
    parameters: memoryStoreVisualSchema,
    execute: async (_toolCallId: string, params: unknown) => {
      const record = params as Record<string, unknown>;
      if (!config.visualMemory.enabled) {
        return deps.jsonResult({
          ok: false,
          disabled: true,
          error: "visual memory storage is disabled by plugin config.",
        });
      }

      const resolved = deps.resolveVisualInput(record, config.visualMemory, context);
      if (!resolved.value) {
        throw new Error(resolved.error ?? "visual input resolution failed");
      }
      const visualInput = await deps.maybeEnrichVisualInput(
        config.visualMemory,
        resolved.value,
        logger,
      );
      deps.rememberVisualContext(
        context,
        visualInput,
        config.visualMemory.currentTurnCacheTtlMs,
      );

      const duplicatePolicy =
        deps.readVisualDuplicatePolicy(deps.getParam(record, "duplicatePolicy")) ??
        config.visualMemory.duplicatePolicy;

      const uri = deps.buildVisualMemoryUri(
        visualInput.mediaRef,
        visualInput.observedAt,
        config.visualMemory.defaultDomain,
        config.visualMemory.pathPrefix,
      );
      if (!deps.isUriWritableByAcl(uri, policy, config.mapping.defaultDomain)) {
        return deps.jsonResult({
          ok: false,
          uri,
          error: "ACL denied visual memory write outside the configured write roots.",
        });
      }
      const content = deps.buildVisualMemoryContent({
        mediaRef: visualInput.mediaRef,
        summary: visualInput.summary,
        sourceChannel: visualInput.sourceChannel,
        observedAt: visualInput.observedAt,
        ocr: visualInput.ocr,
        scene: visualInput.scene,
        whyRelevant: visualInput.whyRelevant,
        confidence: visualInput.confidence,
        entities: visualInput.entities,
        maxSummaryChars: config.visualMemory.maxSummaryChars,
        maxOcrChars: config.visualMemory.maxOcrChars,
        duplicatePolicy,
        disclosure: config.visualMemory.disclosure,
        retentionNote: config.visualMemory.retentionNote,
        fieldSources: visualInput.fieldSources,
        runtimeSource: visualInput.runtimeSource,
        runtimeProbe: visualInput.runtimeProbe,
        provenance: {
          storedVia: "openclaw.memory_store_visual",
          storedAt: new Date().toISOString(),
          mediaRefHash: `sha256-${createHash("sha256")
            .update(visualInput.mediaRef)
            .digest("hex")
            .slice(0, 12)}`,
          recordUri: uri,
        },
      });
      deps.logTrace(
        logger,
        config.visualMemory.traceEnabled,
        "memory-palace:store-visual",
        {
          agentId: identity.value,
          uri,
          duplicatePolicy,
          maxSummaryChars: config.visualMemory.maxSummaryChars ?? null,
          maxOcrChars: config.visualMemory.maxOcrChars ?? null,
          summaryLength: visualInput.summary.length,
          ocrLength: visualInput.ocr?.length ?? 0,
          fieldSources: visualInput.fieldSources,
        },
      );

      try {
        const result = await withClient(async (client) =>
          deps.storeVisualMemoryRecord(
            client,
            uri,
            content,
            config.mapping,
            duplicatePolicy,
            config.visualMemory.disclosure,
          ),
        );
        deps.recordPluginCapturePath(config, session.client, {
          at: new Date().toISOString(),
          layer: "visual_memory",
          category: "visual",
          uri,
          pending: false,
          details: visualInput.runtimeProbe,
        });
        deps.persistTransportDiagnosticsSnapshot(config, session.client);
        return deps.jsonResult({
          ...result,
          runtime_source: visualInput.runtimeSource ?? null,
          runtime_visual_probe: visualInput.runtimeProbe,
        });
      } catch (error) {
        deps.persistTransportDiagnosticsSnapshot(config, session.client);
        return deps.jsonResult({
          ok: false,
          uri,
          runtime_source: visualInput.runtimeSource ?? null,
          runtime_visual_probe: visualInput.runtimeProbe,
          error: deps.formatError(error),
        });
      }
    },
  };

  return [searchTool, learnTool, getTool, storeVisualTool];
}

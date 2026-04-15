import { readFile } from "node:fs/promises";
import type { OpenClawPluginApi } from "openclaw/plugin-sdk/core";
import type {
  JsonRecord,
  PluginConfig,
  PluginRuntimeCompactContext,
  ResolvedAclPolicy,
  SharedClientSession,
} from "./types.js";

type ReflectionBaseDeps = {
  buildReflectionContent: (params: {
    agentId?: string;
    sessionId?: string;
    sessionKey?: string;
    source: "agent_end" | "compact_context" | "command_new";
    summary: string;
    trigger?: string;
    summaryMethod?: string;
    compactSourceUri?: string;
    compactSourceHash?: string;
    compactGistMethod?: string;
    messageCount?: number;
    turnCountEstimate?: number;
    decayHintDays?: number;
    retentionClass?: string;
  }) => string;
  buildReflectionUri: (
    config: PluginConfig,
    policy: ResolvedAclPolicy,
    sessionRef: string,
    sourceText: string,
  ) => string;
  createOrMergeMemoryRecord: (
    client: SharedClientSession["client"],
    targetUri: string,
    content: string,
    options: {
      priority: number;
      disclosure: string;
      lane: "capture" | "profile" | "reflection";
    },
  ) => Promise<{ ok: boolean }>;
  formatError: (error: unknown) => string;
  isUriWritableByAcl: (
    uri: string,
    policy: ResolvedAclPolicy,
    defaultDomain: string,
  ) => boolean;
  logPluginTrace: (
    api: OpenClawPluginApi,
    enabled: boolean,
    eventName: string,
    payload: Record<string, unknown>,
  ) => void;
  readString: (value: unknown) => string | undefined;
  resolveAclPolicy: (
    config: PluginConfig,
    agentId?: string,
  ) => ResolvedAclPolicy;
  resolveContextAgentIdentity: (
    ctx: Record<string, unknown>,
  ) => { value?: string; source?: string };
};

function resolveCompactContextDataPersisted(
  payload: JsonRecord,
  readString: ReflectionBaseDeps["readString"],
  readBoolean: (value: unknown) => boolean | undefined,
): boolean {
  const explicit = readBoolean(payload.data_persisted);
  if (explicit !== undefined) {
    return explicit;
  }
  const guardAction = readString(payload.guard_action)?.toUpperCase();
  const reason = readString(payload.reason);
  return (
    readBoolean(payload.flushed) === true &&
    guardAction === "ADD" &&
    reason !== "write_guard_deduped" &&
    Boolean(readString(payload.uri))
  );
}

export function shouldCleanupCompactContextDurableMemory(
  payload: JsonRecord,
  readString: ReflectionBaseDeps["readString"],
  readBoolean: (value: unknown) => boolean | undefined,
): boolean {
  return (
    readBoolean(payload.flushed) === true &&
    resolveCompactContextDataPersisted(payload, readString, readBoolean) &&
    Boolean(readString(payload.uri))
  );
}

export type AgentEndReflectionDeps = ReflectionBaseDeps & {
  buildReflectionSummaryFromMessages: (messages: unknown[]) => string;
  estimateConversationTurnCount: (messages: unknown[]) => number;
  extractMessageTexts: (
    messages: unknown[],
    allowedRoles?: string[],
  ) => string[];
};

export async function runReflectionFromAgentEnd(
  api: OpenClawPluginApi,
  options: {
    config: PluginConfig;
    deps: AgentEndReflectionDeps;
    event: Record<string, unknown>;
    session: SharedClientSession;
    ctx: Record<string, unknown>;
  },
): Promise<void> {
  const { config, deps, event, session, ctx } = options;
  if (!config.reflection.enabled || !Array.isArray(event.messages)) {
    return;
  }
  const eventMessages = event.messages;
  const identity = deps.resolveContextAgentIdentity(ctx);
  const summary = deps.buildReflectionSummaryFromMessages(eventMessages).trim();
  if (!summary) {
    return;
  }
  const policy = deps.resolveAclPolicy(config, identity.value);
  const sessionRef =
    deps.readString(ctx.sessionId) ??
    deps.readString(ctx.sessionKey) ??
    "unknown-session";
  const targetUri = deps.buildReflectionUri(config, policy, sessionRef, summary);
  if (!deps.isUriWritableByAcl(targetUri, policy, config.mapping.defaultDomain)) {
    return;
  }
  try {
    await session.withClient(async (client) =>
      deps.createOrMergeMemoryRecord(
        client,
        targetUri,
        deps.buildReflectionContent({
          agentId: identity.value,
          sessionId: deps.readString(ctx.sessionId),
          sessionKey: deps.readString(ctx.sessionKey),
          source: "agent_end",
          trigger: "agent_end",
          summaryMethod: "message_rollup_v1",
          messageCount: deps.extractMessageTexts(eventMessages, [
            "user",
            "assistant",
          ]).length,
          turnCountEstimate: deps.estimateConversationTurnCount(eventMessages),
          decayHintDays: 14,
          retentionClass: "rolling_session",
          summary,
        }),
        {
          priority: 2,
          disclosure:
            "When recalling cross-session lessons, invariants, or open loops.",
          lane: "reflection",
        },
      ),
    );
    deps.logPluginTrace(
      api,
      config.reflection.traceEnabled,
      "memory-palace:reflection-agent-end",
      {
        agentId: identity.value,
        identitySource: identity.source,
        sessionRef,
        summaryChars: summary.length,
      },
    );
  } catch (error) {
    api.logger.warn(
      `memory-palace reflection(agent_end) failed: ${deps.formatError(error)}`,
    );
  }
}

export type CommandNewReflectionDeps = ReflectionBaseDeps & {
  buildReflectionSummaryFromMessages: (messages: unknown[]) => string;
  estimateConversationTurnCount: (messages: unknown[]) => number;
  extractMessageTexts: (
    messages: unknown[],
    allowedRoles?: string[],
  ) => string[];
  extractTranscriptMessagesFromText: (sessionText: string) => unknown[];
  isRecord: (value: unknown) => value is Record<string, unknown>;
  readSessionFileText?: (sessionFile: string) => Promise<string>;
  resolveCommandNewMessages: (
    event: Record<string, unknown>,
    ctx: Record<string, unknown>,
  ) => unknown[];
  resolvePreviousSessionFile: (
    event: Record<string, unknown>,
    ctx: Record<string, unknown>,
    options?: { preferCurrentSession?: boolean },
  ) => string | undefined;
};

export async function runReflectionFromCommandNew(
  api: OpenClawPluginApi,
  options: {
    config: PluginConfig;
    deps: CommandNewReflectionDeps;
    event: Record<string, unknown>;
    session: SharedClientSession;
    ctx: Record<string, unknown> | undefined;
  },
): Promise<void> {
  const { config, deps, event, session, ctx } = options;
  if (!config.reflection.enabled || config.reflection.source !== "command_new") {
    return;
  }
  const normalizedCtx = deps.isRecord(ctx) ? ctx : {};
  const reflectionContext = {
    ...(deps.isRecord(event.context) ? event.context : {}),
    ...event,
    ...normalizedCtx,
  };
  const messages = deps.resolveCommandNewMessages(event, normalizedCtx);
  let summaryMethod = "message_rollup_v1";
  let messageCount = deps.extractMessageTexts(messages, ["user", "assistant"]).length;
  let turnCountEstimate = deps.estimateConversationTurnCount(messages);
  let summary = deps.buildReflectionSummaryFromMessages(messages).trim();
  deps.logPluginTrace(
    api,
    config.reflection.traceEnabled,
    "memory-palace:reflection-command-new-start",
    {
      eventKeys: Object.keys(event),
      ctxKeys: Object.keys(normalizedCtx),
      messageCount,
      turnCountEstimate,
      summaryChars: summary.length,
      hasEventMessages: Array.isArray(event.messages),
      hasCtxMessages: Array.isArray(normalizedCtx.messages),
      hasPreviousMessages: Array.isArray(normalizedCtx.previousMessages),
    },
  );
  if (!summary) {
    const sessionFile = deps.resolvePreviousSessionFile(event, normalizedCtx);
    if (!sessionFile) {
      deps.logPluginTrace(
        api,
        config.reflection.traceEnabled,
        "memory-palace:reflection-command-new-skip",
        {
          reason: "no_summary_and_no_session_file",
          sessionFile: sessionFile ?? null,
        },
      );
      return;
    }
    try {
      const readSessionFileText =
        deps.readSessionFileText ??
        ((targetPath: string) => readFile(targetPath, "utf8"));
      const transcriptMessages = deps.extractTranscriptMessagesFromText(
        await readSessionFileText(sessionFile),
      );
      summary = deps.buildReflectionSummaryFromMessages(transcriptMessages).trim();
      if (!summary) {
        deps.logPluginTrace(
          api,
          config.reflection.traceEnabled,
          "memory-palace:reflection-command-new-skip",
          {
            reason: "empty_transcript_summary",
            sessionFile,
          },
        );
        return;
      }
      messageCount = deps.extractMessageTexts(transcriptMessages, [
        "user",
        "assistant",
      ]).length;
      turnCountEstimate = deps.estimateConversationTurnCount(transcriptMessages);
      summaryMethod = "transcript_rollup_v1";
      deps.logPluginTrace(
        api,
        config.reflection.traceEnabled,
        "memory-palace:reflection-command-new-transcript",
        {
          sessionFile,
          messageCount,
          turnCountEstimate,
          summaryChars: summary.length,
        },
      );
    } catch (error) {
      const code = (error as NodeJS.ErrnoException | undefined)?.code;
      if (code === "ENOENT" || code === "ENOTDIR") {
        deps.logPluginTrace(
          api,
          config.reflection.traceEnabled,
          "memory-palace:reflection-command-new-skip",
          {
            reason: "no_summary_and_no_session_file",
            sessionFile,
          },
        );
        return;
      }
      api.logger.warn(
        `memory-palace reflection(command:new) transcript fallback failed: ${deps.formatError(error)}`,
      );
      return;
    }
  }
  const identity = deps.resolveContextAgentIdentity(reflectionContext);
  const policy = deps.resolveAclPolicy(config, identity.value);
  const sessionRef =
    deps.readString(reflectionContext.sessionId) ??
    deps.readString(reflectionContext.sessionKey) ??
    deps.readString(event.sessionId) ??
    deps.readString(event.sessionKey) ??
    "command-new";
  const commandReason = deps.readString(event.reason)?.trim().toLowerCase();
  const trigger = commandReason === "reset" ? "command:reset" : "command:new";
  const targetUri = deps.buildReflectionUri(config, policy, sessionRef, summary);
  if (!deps.isUriWritableByAcl(targetUri, policy, config.mapping.defaultDomain)) {
    deps.logPluginTrace(
      api,
      config.reflection.traceEnabled,
      "memory-palace:reflection-command-new-skip",
      {
        reason: "acl_denied",
        targetUri,
        agentId: identity.value ?? null,
      },
    );
    return;
  }
  try {
    await session.withClient(async (client) =>
      deps.createOrMergeMemoryRecord(
        client,
        targetUri,
        deps.buildReflectionContent({
          agentId: identity.value,
          sessionId: deps.readString(reflectionContext.sessionId),
          sessionKey: deps.readString(reflectionContext.sessionKey),
          source: "command_new",
          trigger,
          summaryMethod,
          messageCount,
          turnCountEstimate,
          decayHintDays: 14,
          retentionClass: "session_boundary",
          summary,
        }),
        {
          priority: 2,
          disclosure:
            "When recalling cross-session lessons, invariants, or open loops.",
          lane: "reflection",
        },
      ),
    );
    deps.logPluginTrace(
      api,
      config.reflection.traceEnabled,
      "memory-palace:reflection-command-new",
      {
        agentId: identity.value,
        identitySource: identity.source,
        sessionRef,
        messageCount,
        turnCountEstimate,
        summaryChars: summary.length,
      },
    );
  } catch (error) {
    api.logger.warn(
      `memory-palace reflection(command:new) failed: ${deps.formatError(error)}`,
    );
  }
}

export type CompactContextReflectionDeps = ReflectionBaseDeps & {
  extractCompactContextTrace: (text: string) => string;
  extractReadText: (raw: unknown) => {
    text: string;
    selection?: unknown;
    degraded?: boolean;
    error?: string;
  };
  normalizeCreatePayload: (value: unknown) => JsonRecord;
  recordPluginCompactContextResult: (
    config: PluginConfig,
    client: SharedClientSession["client"] | undefined,
    payload: PluginRuntimeCompactContext,
  ) => void;
  readBoolean: (value: unknown) => boolean | undefined;
};

function isUnsupportedCompactContextReflectionToolError(
  error: unknown,
  formatError: ReflectionBaseDeps["formatError"],
): boolean {
  const message = formatError(error).toLowerCase();
  const unsupportedMarker =
    message.includes("unknown tool") ||
    message.includes("tool not found") ||
    message.includes("method not found") ||
    message.includes("not implemented") ||
    message.includes("unsupported");
  return unsupportedMarker;
}

export async function runReflectionFromCompactContext(
  api: OpenClawPluginApi,
  options: {
    config: PluginConfig;
    deps: CompactContextReflectionDeps;
    event: Record<string, unknown>;
    session: SharedClientSession;
    ctx: Record<string, unknown>;
  },
): Promise<void> {
  const { config, deps, event, session, ctx } = options;
  if (!config.reflection.enabled || config.reflection.source !== "compact_context") {
    return;
  }
  const identity = deps.resolveContextAgentIdentity(ctx);
  const policy = deps.resolveAclPolicy(config, identity.value);
  const sessionRef =
    deps.readString(ctx.sessionId) ??
    deps.readString(ctx.sessionKey) ??
    deps.readString(event.sessionId) ??
    "unknown-session";
  try {
    const reflectionAclProbeUri = deps.buildReflectionUri(
      config,
      policy,
      sessionRef,
      "acl-probe",
    );
    if (
      !deps.isUriWritableByAcl(
        reflectionAclProbeUri,
        policy,
        config.mapping.defaultDomain,
      )
    ) {
      return;
    }

    const atomicPayload = await session.withClient(async (client) => {
      const atomicMethod = (
        client as SharedClientSession["client"] & {
          compactContextReflection?: (args: Record<string, unknown>) => Promise<unknown>;
        }
      ).compactContextReflection;
      if (typeof atomicMethod !== "function") {
        return null;
      }
      try {
        return deps.normalizeCreatePayload(
          await atomicMethod.call(client, {
            reason: "reflection_lane",
            force: true,
            max_lines: config.reflection.compactMaxLines,
            reflection_root_uri: config.reflection.rootUri,
            reflection_agent_key: policy.agentKey,
            reflection_session_ref: sessionRef,
            reflection_agent_id: identity.value,
            reflection_session_id: deps.readString(ctx.sessionId),
            reflection_session_key: deps.readString(ctx.sessionKey),
            reflection_priority: 2,
            reflection_disclosure:
              "When recalling cross-session lessons, invariants, or open loops.",
            reflection_decay_hint_days: 14,
            reflection_retention_class: "rolling_session",
          }),
        );
      } catch (error) {
        if (
          isUnsupportedCompactContextReflectionToolError(
            error,
            deps.formatError,
          )
        ) {
          return null;
        }
        throw error;
      }
    });
    if (atomicPayload) {
      deps.recordPluginCompactContextResult(config, session.client, {
        at: new Date().toISOString(),
        flushed: deps.readBoolean(atomicPayload.flushed) === true,
        dataPersisted: resolveCompactContextDataPersisted(
          atomicPayload,
          deps.readString,
          deps.readBoolean,
        ),
        reason: deps.readString(atomicPayload.reason) ?? "",
        uri:
          deps.readString(atomicPayload.reflection_uri) ??
          deps.readString(atomicPayload.uri),
        guardAction: deps.readString(atomicPayload.guard_action),
        gistMethod: deps.readString(atomicPayload.gist_method),
        sourceHash: deps.readString(atomicPayload.source_hash),
      });
      if (deps.readBoolean(atomicPayload.reflection_written) === true) {
        deps.logPluginTrace(
          api,
          config.reflection.traceEnabled,
          "memory-palace:reflection-compact",
          {
            agentId: identity.value,
            identitySource: identity.source,
            sessionRef,
            compactUri: null,
            reflectionUri:
              deps.readString(atomicPayload.reflection_uri) ??
              deps.readString(atomicPayload.uri),
            cleanedUp: true,
            atomic: true,
            guardAction: deps.readString(atomicPayload.guard_action),
          },
        );
        return;
      }
      if (
        deps.readBoolean(atomicPayload.flushed) !== true ||
        !resolveCompactContextDataPersisted(
          atomicPayload,
          deps.readString,
          deps.readBoolean,
        )
      ) {
        deps.logPluginTrace(
          api,
          config.reflection.traceEnabled,
          "memory-palace:reflection-compact-skip",
          {
            agentId: identity.value,
            identitySource: identity.source,
            sessionRef,
            compactUri:
              deps.readString(atomicPayload.reflection_uri) ??
              deps.readString(atomicPayload.uri),
            reason:
              deps.readString(atomicPayload.reason) ??
              "compact_context_not_persisted",
            dataPersisted: false,
            atomic: true,
          },
        );
        return;
      }
    }

    const compactPayload = await session.withClient(async (client) =>
      deps.normalizeCreatePayload(
        await client.compactContext({
          reason: "reflection_lane",
          force: true,
          max_lines: config.reflection.compactMaxLines,
        }),
      ),
    );
    deps.recordPluginCompactContextResult(config, session.client, {
      at: new Date().toISOString(),
      flushed: deps.readBoolean(compactPayload.flushed) === true,
      dataPersisted: resolveCompactContextDataPersisted(
        compactPayload,
        deps.readString,
        deps.readBoolean,
      ),
      reason: deps.readString(compactPayload.reason) ?? "",
      uri: deps.readString(compactPayload.uri),
      guardAction: deps.readString(compactPayload.guard_action),
      gistMethod: deps.readString(compactPayload.gist_method),
      sourceHash: deps.readString(compactPayload.source_hash),
    });
    const compactUri = deps.readString(compactPayload.uri);
    if (!compactUri || deps.readBoolean(compactPayload.flushed) !== true) {
      return;
    }
    if (
      !resolveCompactContextDataPersisted(
        compactPayload,
        deps.readString,
        deps.readBoolean,
      )
    ) {
      deps.logPluginTrace(
        api,
        config.reflection.traceEnabled,
        "memory-palace:reflection-compact-skip",
        {
          agentId: identity.value,
          identitySource: identity.source,
          sessionRef,
          compactUri,
          reason: deps.readString(compactPayload.reason) ?? "compact_context_not_persisted",
          dataPersisted: false,
        },
      );
      return;
    }
    const summaryTextFromPayload =
      deps.readString(compactPayload.trace_text)?.trim() ||
      deps.readString(compactPayload.gist_text)?.trim();
    const summaryText =
      summaryTextFromPayload ||
      (await session.withClient(async (client) => {
        const raw = await client.readMemory({ uri: compactUri });
        const extracted = deps.extractReadText(raw);
        if (extracted.error) {
          throw new Error(extracted.error);
        }
        return deps.extractCompactContextTrace(extracted.text);
      }));
    if (!summaryText.trim()) {
      return;
    }
    const reflectionUri = deps.buildReflectionUri(
      config,
      policy,
      sessionRef,
      summaryText,
    );
    if (!deps.isUriWritableByAcl(reflectionUri, policy, config.mapping.defaultDomain)) {
      return;
    }
    const reflectionResult = await session.withClient(async (client) =>
      deps.createOrMergeMemoryRecord(
        client,
        reflectionUri,
        deps.buildReflectionContent({
          agentId: identity.value,
          sessionId: deps.readString(ctx.sessionId),
          sessionKey: deps.readString(ctx.sessionKey),
          source: "compact_context",
          trigger: "compact_context",
          summaryMethod: "compact_context_trace_v1",
          compactSourceUri: compactUri,
          compactSourceHash: deps.readString(compactPayload.source_hash),
          compactGistMethod: deps.readString(compactPayload.gist_method),
          decayHintDays: 14,
          retentionClass: "rolling_session",
          summary: summaryText,
        }),
        {
          priority: 2,
          disclosure:
            "When recalling cross-session lessons, invariants, or open loops.",
          lane: "reflection",
        },
      ),
    );
    let cleanedUp = false;
    if (
      reflectionResult.ok &&
      shouldCleanupCompactContextDurableMemory(
        compactPayload,
        deps.readString,
        deps.readBoolean,
      )
    ) {
      try {
        await session.withClient(async (client) => {
          await client.deleteMemory({ uri: compactUri });
        });
        cleanedUp = true;
      } catch (error) {
        api.logger.warn(
          `memory-palace reflection(compact_context) cleanup failed: ${deps.formatError(error)}`,
        );
      }
    }
    deps.logPluginTrace(
      api,
      config.reflection.traceEnabled,
      "memory-palace:reflection-compact",
      {
        agentId: identity.value,
        identitySource: identity.source,
        sessionRef,
        compactUri,
        cleanedUp,
        guardAction: deps.readString(compactPayload.guard_action),
      },
    );
  } catch (error) {
    api.logger.warn(
      `memory-palace reflection(compact_context) failed: ${deps.formatError(error)}`,
    );
  }
}

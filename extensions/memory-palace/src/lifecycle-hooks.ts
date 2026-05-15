import type { OpenClawPluginApi } from "openclaw/plugin-sdk/core";
import type {
  PluginConfig,
  SharedClientSession,
} from "./types.js";

export type RegisterLifecycleHookDeps = {
  cleanMessageTextForReasoning: (text: string) => string;
  extractMessageTexts: (
    messages: unknown[],
    allowedRoles?: string[],
  ) => string[];
  harvestVisualContextFromEvent: (
    api: OpenClawPluginApi,
    config: PluginConfig,
    hookName: string,
    event: Record<string, unknown>,
    ctx: Record<string, unknown>,
  ) => void;
  isCommandNewStartupEvent: (
    event: Record<string, unknown>,
    ctx: Record<string, unknown>,
  ) => boolean;
  normalizeHookContext: (ctx: Record<string, unknown> | undefined) => Record<string, unknown>;
  normalizeText: (value: string | undefined) => string | undefined;
  readString: (value: unknown) => string | undefined;
  runAutoCaptureHook: (
    api: OpenClawPluginApi,
    config: PluginConfig,
    session: SharedClientSession,
    event: Record<string, unknown>,
    ctx: Record<string, unknown>,
  ) => Promise<void>;
  runAutoRecallHook: (
    api: OpenClawPluginApi,
    config: PluginConfig,
    session: SharedClientSession,
    event: Record<string, unknown>,
    ctx: Record<string, unknown>,
  ) => Promise<unknown>;
  runReflectionFromAgentEnd: (
    api: OpenClawPluginApi,
    config: PluginConfig,
    session: SharedClientSession,
    event: Record<string, unknown>,
    ctx: Record<string, unknown>,
  ) => Promise<void>;
  runReflectionFromCommandNew: (
    api: OpenClawPluginApi,
    config: PluginConfig,
    session: SharedClientSession,
    event: Record<string, unknown>,
    ctx: Record<string, unknown>,
  ) => Promise<void>;
  runReflectionFromCompactContext: (
    api: OpenClawPluginApi,
    config: PluginConfig,
    session: SharedClientSession,
    event: Record<string, unknown>,
    ctx: Record<string, unknown>,
  ) => Promise<void>;
};

type HookHandler = (
  event: Record<string, unknown>,
  ctx?: Record<string, unknown>,
) => Promise<unknown> | unknown;

// Circuit breaker: prevent a chronically-failing hook from being retried forever.
// Hooks must never break the host workflow, so we skip repeated failures for a
// short cooldown and then allow one half-open retry that can reset the counter.
const HOOK_FAILURE_THRESHOLD = 5;
const HOOK_FAILURE_COOLDOWN_MS = 60_000;
type HookFailureState = {
  count: number;
  lastFailureAt: number;
};
const hookFailureCounts = new Map<string, HookFailureState>();

function shouldSkipHook(hookName: string): boolean {
  const state = hookFailureCounts.get(hookName);
  if (!state || state.count < HOOK_FAILURE_THRESHOLD) {
    return false;
  }
  return Date.now() - state.lastFailureAt < HOOK_FAILURE_COOLDOWN_MS;
}

function recordHookFailure(hookName: string): void {
  const current = hookFailureCounts.get(hookName);
  hookFailureCounts.set(hookName, {
    count: (current?.count ?? 0) + 1,
    lastFailureAt: Date.now(),
  });
}

function resetHookFailure(hookName: string): void {
  hookFailureCounts.delete(hookName);
}

export function registerLifecycleHooks(
  api: OpenClawPluginApi,
  options: {
    config: PluginConfig;
    deps: RegisterLifecycleHookDeps;
    session: SharedClientSession;
  },
): void {
  const { config, deps, session } = options;
  const COMMAND_NEW_REFLECTION_DEDUPE_WINDOW_MS = 5_000;
  const COMMAND_NEW_REFLECTION_CACHE_TTL_MS = 60_000;
  const MAX_RECENT_COMMAND_NEW_REFLECTIONS = 128;
  const PROMPT_BUILD_RECALL_MARKER_DELAY_MS = 25;

  const isRecord = (value: unknown): value is Record<string, unknown> =>
    typeof value === "object" && value !== null && !Array.isArray(value);
  const flattenPreprocessedText = (value: unknown): string[] => {
    if (typeof value === "string") {
      return [value];
    }
    if (Array.isArray(value)) {
      return value.flatMap((entry) => flattenPreprocessedText(entry));
    }
    if (isRecord(value)) {
      const directKeys = ["text", "bodyForAgent", "body", "content", "value", "caption"];
      const directValues = directKeys
        .map((key) => value[key])
        .flatMap((entry) => flattenPreprocessedText(entry));
      if (directValues.length > 0) {
        return directValues;
      }
      return Object.values(value).flatMap((entry) => flattenPreprocessedText(entry));
    }
    return [];
  };
  const extractPreprocessedUserText = (event: Record<string, unknown>): string | undefined => {
    const text = flattenPreprocessedText(
      isRecord(event.message) ? event.message.bodyForAgent ?? event.message : event.message,
    )
      .map((entry) => entry.trim())
      .filter(Boolean)
      .join("\n");
    const cleaned = text ? deps.cleanMessageTextForReasoning(text) : "";
    return cleaned || undefined;
  };
  const isWebchatContext = (
    event: Record<string, unknown>,
    ctx: Record<string, unknown>,
    fallbackText?: string,
  ): boolean => {
    const channelCandidates = [
      deps.readString(ctx.messageChannel),
      deps.readString(event.messageChannel),
      isRecord(ctx.deliveryContext) ? deps.readString(ctx.deliveryContext.channel) : undefined,
      isRecord(event.deliveryContext) ? deps.readString(event.deliveryContext.channel) : undefined,
      isRecord(ctx.origin) ? deps.readString(ctx.origin.provider) : undefined,
      isRecord(ctx.origin) ? deps.readString(ctx.origin.surface) : undefined,
      isRecord(event.origin) ? deps.readString(event.origin.provider) : undefined,
      isRecord(event.origin) ? deps.readString(event.origin.surface) : undefined,
    ]
      .map((entry) => entry?.trim().toLowerCase())
      .filter((entry): entry is string => Boolean(entry));
    if (
      channelCandidates.some(
        (entry) =>
          entry === "webchat" ||
          entry.includes("wechat") ||
          entry.includes("weixin"),
      )
    ) {
      return true;
    }
    const text = (fallbackText ?? "").toLowerCase();
    return (
      text.includes("openclaw-control-ui") ||
      text.includes("wechat") ||
      text.includes("weixin")
    );
  };
  const resolveLifecycleSessionRef = (
    event: Record<string, unknown>,
    ctx: Record<string, unknown>,
  ): string => {
    const sessionFileCandidates = [
      deps.readString(ctx.sessionFile),
      deps.readString(event.sessionFile),
      isRecord(ctx.sessionEntry) ? deps.readString(ctx.sessionEntry.sessionFile) : undefined,
      isRecord(event.context) && isRecord(event.context.sessionEntry)
        ? deps.readString(event.context.sessionEntry.sessionFile)
        : undefined,
      isRecord(ctx.previousSessionEntry) ? deps.readString(ctx.previousSessionEntry.sessionFile) : undefined,
      isRecord(event.context) && isRecord(event.context.previousSessionEntry)
        ? deps.readString(event.context.previousSessionEntry.sessionFile)
        : undefined,
    ];
    const messageText = deps.normalizeText(
      deps.extractMessageTexts(
        Array.isArray(event.messages) ? event.messages : [],
        ["user", "assistant"],
      ).join("\n"),
    );
    const taggedCandidates: Array<string | undefined> = [
      deps.readString(ctx.sessionKey)
        ? `sessionKey:${deps.readString(ctx.sessionKey)}`
        : undefined,
      deps.readString(ctx.sessionId)
        ? `sessionId:${deps.readString(ctx.sessionId)}`
        : undefined,
      deps.readString(event.sessionKey)
        ? `sessionKey:${deps.readString(event.sessionKey)}`
        : undefined,
      deps.readString(event.sessionId)
        ? `sessionId:${deps.readString(event.sessionId)}`
        : undefined,
      sessionFileCandidates.find((entry): entry is string => Boolean(entry))
        ? `sessionFile:${sessionFileCandidates.find((entry): entry is string => Boolean(entry))}`
        : undefined,
      deps.readString(ctx.agentId) && (messageText ?? deps.normalizeText(deps.readString(event.prompt)))
        ? `agentPrompt:${deps.readString(ctx.agentId)}::${
            messageText ?? deps.normalizeText(deps.readString(event.prompt))
          }`
        : undefined,
      deps.readString(event.agentId) && (messageText ?? deps.normalizeText(deps.readString(event.prompt)))
        ? `agentPrompt:${deps.readString(event.agentId)}::${
            messageText ?? deps.normalizeText(deps.readString(event.prompt))
          }`
        : undefined,
      deps.readString(ctx.agentId)
        ? `agent:${deps.readString(ctx.agentId)}`
        : undefined,
      deps.readString(event.agentId)
        ? `agent:${deps.readString(event.agentId)}`
        : undefined,
      messageText
        ? `message:${messageText}`
        : undefined,
      deps.normalizeText(deps.readString(event.prompt))
        ? `prompt:${deps.normalizeText(deps.readString(event.prompt))}`
        : undefined,
    ];
    return taggedCandidates.find((entry): entry is string => Boolean(entry)) ?? "unknown-session";
  };
  const pruneTimedMap = (
    entries: Map<string, number>,
    now: number,
    maxAgeMs: number,
    maxEntries: number,
  ) => {
    for (const [key, seenAt] of entries) {
      if (now - seenAt > maxAgeMs) {
        entries.delete(key);
      }
    }
    while (entries.size > maxEntries) {
      const oldestKey = entries.keys().next().value;
      if (typeof oldestKey !== "string") {
        break;
      }
      entries.delete(oldestKey);
    }
  };

  const registerNamedHook = (
    hookName: string,
    handler: HookHandler,
    options?: Record<string, unknown>,
  ): boolean => {
    if (typeof api.on === "function") {
      api.on(hookName, handler as never, options as never);
      return true;
    }
    if (typeof api.registerHook === "function") {
      api.registerHook(hookName, handler as never, options as never);
      return true;
    }
    return false;
  };

  if (typeof api.on !== "function" && typeof api.registerHook !== "function") {
    api.logger.warn(
      "memory-palace: typed hooks unavailable; skipping auto recall/capture registration",
    );
    return;
  }

  if (
    (config.profileMemory.enabled &&
      config.profileMemory.injectBeforeAgentStart) ||
    config.autoRecall.enabled ||
    config.hostBridge.enabled ||
    (config.reflection.enabled && config.reflection.autoRecall) ||
    (config.reflection.enabled && config.reflection.source === "command_new")
  ) {
    const recentCommandNewReflections = new Map<string, number>();
    const promptBuildRecallSessions = new Map<string, number>();
    const promptBuildRecallCleanupTimers = new Map<string, ReturnType<typeof setTimeout>>();
    const schedulePromptBuildRecallCleanup = (sessionRef: string) => {
      const existingTimer = promptBuildRecallCleanupTimers.get(sessionRef);
      if (existingTimer) {
        clearTimeout(existingTimer);
      }
      const cleanupTimer = setTimeout(() => {
        promptBuildRecallCleanupTimers.delete(sessionRef);
        promptBuildRecallSessions.delete(sessionRef);
      }, PROMPT_BUILD_RECALL_MARKER_DELAY_MS);
      cleanupTimer.unref?.();
      promptBuildRecallCleanupTimers.set(sessionRef, cleanupTimer);
    };
    const markPromptBuildRecallSession = (sessionRef: string, now: number) => {
      const existingTimer = promptBuildRecallCleanupTimers.get(sessionRef);
      if (existingTimer) {
        clearTimeout(existingTimer);
        promptBuildRecallCleanupTimers.delete(sessionRef);
      }
      promptBuildRecallSessions.delete(sessionRef);
      promptBuildRecallSessions.set(sessionRef, now);
    };
    const consumePromptBuildRecallSession = (sessionRef: string): boolean => {
      const existingTimer = promptBuildRecallCleanupTimers.get(sessionRef);
      if (existingTimer) {
        clearTimeout(existingTimer);
        promptBuildRecallCleanupTimers.delete(sessionRef);
      }
      const seen = promptBuildRecallSessions.has(sessionRef);
      promptBuildRecallSessions.delete(sessionRef);
      return seen;
    };
    const triggerCommandNewReflection = async (
      event: Record<string, unknown>,
      ctx: Record<string, unknown> | undefined,
      explicitReason?: "new" | "reset",
    ) => {
      const normalizedCtx = deps.normalizeHookContext(ctx);
      const eventReason = deps.readString(event.reason)?.trim().toLowerCase();
      const reason =
        explicitReason ?? (eventReason === "reset" ? "reset" : "new");
      const sessionRef = resolveLifecycleSessionRef(event, normalizedCtx);
      const dedupeKey = `${reason}:${sessionRef}`;
      const now = Date.now();
      pruneTimedMap(
        recentCommandNewReflections,
        now,
        COMMAND_NEW_REFLECTION_CACHE_TTL_MS,
        MAX_RECENT_COMMAND_NEW_REFLECTIONS,
      );
      const lastSeenAt = recentCommandNewReflections.get(dedupeKey) ?? 0;
      if (now - lastSeenAt < COMMAND_NEW_REFLECTION_DEDUPE_WINDOW_MS) {
        return;
      }
      recentCommandNewReflections.delete(dedupeKey);
      recentCommandNewReflections.set(dedupeKey, now);
      pruneTimedMap(
        recentCommandNewReflections,
        now,
        COMMAND_NEW_REFLECTION_CACHE_TTL_MS,
        MAX_RECENT_COMMAND_NEW_REFLECTIONS,
      );
      const reflectionEvent =
        explicitReason && !eventReason
          ? {
              ...event,
              reason: explicitReason,
            }
          : event;
      await deps.runReflectionFromCommandNew(
        api,
        config,
        session,
        reflectionEvent,
        normalizedCtx,
      );
    };

    registerNamedHook("before_prompt_build", async (event, ctx) => {
      const hookName = "before_prompt_build";
      if (shouldSkipHook(hookName)) return;
      try {
        const normalizedCtx = deps.normalizeHookContext(ctx);
        const sessionRef = resolveLifecycleSessionRef(event, normalizedCtx);
        if (
          config.reflection.enabled &&
          config.reflection.source === "command_new" &&
          deps.isCommandNewStartupEvent(event, normalizedCtx)
        ) {
          await triggerCommandNewReflection(event, normalizedCtx, "new");
        }
        markPromptBuildRecallSession(sessionRef, Date.now());
        try {
          const result = await deps.runAutoRecallHook(api, config, session, event, normalizedCtx);
          resetHookFailure(hookName);
          return result;
        } finally {
          schedulePromptBuildRecallCleanup(sessionRef);
        }
      } catch (err) {
        // Log but don't propagate - hooks must never break the host
        recordHookFailure(hookName);
        api?.logger?.warn?.(
          `[memory-palace] ${hookName} hook failed: ${err instanceof Error ? err.message : String(err)}`,
        );
        return;
      }
    }, { priority: 100 });

    // Legacy fallback: only runs recall if before_prompt_build was not fired
    // (e.g. on older OpenClaw hosts that lack the newer hook).
    registerNamedHook("before_agent_start", async (event, ctx) => {
      const hookName = "before_agent_start";
      if (shouldSkipHook(hookName)) return;
      try {
        const normalizedCtx = deps.normalizeHookContext(ctx);
        const sessionRef = resolveLifecycleSessionRef(event, normalizedCtx);
        if (consumePromptBuildRecallSession(sessionRef)) {
          return;
        }
        if (
          config.reflection.enabled &&
          config.reflection.source === "command_new" &&
          deps.isCommandNewStartupEvent(event, normalizedCtx)
        ) {
          await triggerCommandNewReflection(event, normalizedCtx, "new");
        }
        const result = await deps.runAutoRecallHook(api, config, session, event, normalizedCtx);
        resetHookFailure(hookName);
        return result;
      } catch (err) {
        // Log but don't propagate - hooks must never break the host
        recordHookFailure(hookName);
        api?.logger?.warn?.(
          `[memory-palace] ${hookName} hook failed: ${err instanceof Error ? err.message : String(err)}`,
        );
        return;
      }
    });

    const handleBeforeReset = async (
      event: Record<string, unknown>,
      ctx?: Record<string, unknown>,
    ) => {
      const hookName = "before_reset";
      if (shouldSkipHook(hookName)) return;
      try {
        const reason = deps.readString(event.reason)?.trim().toLowerCase();
        if (reason && reason !== "new" && reason !== "reset") {
          return;
        }
        await triggerCommandNewReflection(
          event,
          ctx,
          reason === "reset" ? "reset" : "new",
        );
        resetHookFailure(hookName);
      } catch (err) {
        // Log but don't propagate - hooks must never break the host
        recordHookFailure(hookName);
        api?.logger?.warn?.(
          `[memory-palace] ${hookName} hook failed: ${err instanceof Error ? err.message : String(err)}`,
        );
      }
    };

    if (config.reflection.enabled && config.reflection.source === "command_new") {
      registerNamedHook("before_reset", handleBeforeReset);
      if (typeof api.registerHook === "function") {
        api.registerHook(
          "command:new",
          async (event, ctx) => {
            const hookName = "command:new";
            if (shouldSkipHook(hookName)) return;
            try {
              await triggerCommandNewReflection(event, ctx, "new");
              resetHookFailure(hookName);
            } catch (err) {
              // Log but don't propagate - hooks must never break the host
              recordHookFailure(hookName);
              api?.logger?.warn?.(
                `[memory-palace] ${hookName} hook failed: ${err instanceof Error ? err.message : String(err)}`,
              );
            }
          },
          {
            name: "memory-palace-command-new-reflection",
            description:
              "Persist command:new session-boundary reflections into Memory Palace.",
          },
        );
        api.registerHook(
          "command:reset",
          async (event, ctx) => {
            const hookName = "command:reset";
            if (shouldSkipHook(hookName)) return;
            try {
              await triggerCommandNewReflection(event, ctx, "reset");
              resetHookFailure(hookName);
            } catch (err) {
              // Log but don't propagate - hooks must never break the host
              recordHookFailure(hookName);
              api?.logger?.warn?.(
                `[memory-palace] ${hookName} hook failed: ${err instanceof Error ? err.message : String(err)}`,
              );
            }
          },
          {
            name: "memory-palace-command-reset-reflection",
            description:
              "Persist command:reset session-boundary reflections into Memory Palace.",
          },
        );
      }
    }
  }

  const shouldRegisterMessagePreprocessed =
    config.visualMemory.enabled ||
    config.autoCapture.enabled ||
    config.capturePipeline.captureAssistantDerived ||
    config.smartExtraction.enabled;
  if (shouldRegisterMessagePreprocessed) {
    const recentWebchatFallbackCaptures = new Map<string, number>();
    const handleMessagePreprocessed = async (
      event: Record<string, unknown>,
      ctx?: Record<string, unknown>,
    ) => {
      const hookName = "message:preprocessed";
      if (shouldSkipHook(hookName)) return;
      try {
        const normalizedCtx = deps.normalizeHookContext(ctx);
        if (config.visualMemory.enabled) {
          deps.harvestVisualContextFromEvent(
            api,
            config,
            "message:preprocessed",
            event,
            normalizedCtx,
          );
        }
        if (
          !config.autoCapture.enabled &&
          !config.capturePipeline.captureAssistantDerived &&
          !config.smartExtraction.enabled
        ) {
          resetHookFailure(hookName);
          return;
        }
        if (Array.isArray(event.messages) && event.messages.length > 0) {
          resetHookFailure(hookName);
          return;
        }
        const bodyText = extractPreprocessedUserText(event);
        if (!bodyText || !isWebchatContext(event, normalizedCtx, bodyText)) {
          resetHookFailure(hookName);
          return;
        }
        const dedupeText = deps.normalizeText(bodyText) ?? bodyText.trim();
        const sessionRef =
          deps.readString(normalizedCtx.sessionKey) ??
          deps.readString(normalizedCtx.sessionId) ??
          deps.readString(event.sessionKey) ??
          deps.readString(event.sessionId) ??
          "unknown-session";
        const dedupeKey = `${sessionRef}:${dedupeText}`;
        const now = Date.now();
        const lastSeenAt = recentWebchatFallbackCaptures.get(dedupeKey) ?? 0;
        if (now - lastSeenAt < 15_000) {
          resetHookFailure(hookName);
          return;
        }
        recentWebchatFallbackCaptures.set(dedupeKey, now);
        if (recentWebchatFallbackCaptures.size > 128) {
          for (const [key, seenAt] of recentWebchatFallbackCaptures) {
            if (now - seenAt > 60_000) {
              recentWebchatFallbackCaptures.delete(key);
            }
          }
        }
        await deps.runAutoCaptureHook(
          api,
          config,
          session,
          {
            ...event,
            success: true,
            messages: [
              {
                role: "user",
                content: [{ type: "text", text: bodyText }],
              },
            ],
          },
          normalizedCtx,
        );
        resetHookFailure(hookName);
      } catch (err) {
        // Log but don't propagate - hooks must never break the host
        recordHookFailure(hookName);
        api?.logger?.warn?.(
          `[memory-palace] ${hookName} hook failed: ${err instanceof Error ? err.message : String(err)}`,
        );
      }
    };
    if (typeof api.registerHook === "function") {
      api.registerHook("message:preprocessed", handleMessagePreprocessed, {
        name: "memory-palace-visual-harvest",
        description:
          "Harvest visual context from fully preprocessed inbound messages before the agent turn.",
      });
    } else {
      registerNamedHook("message:preprocessed", handleMessagePreprocessed);
    }
    if (config.visualMemory.enabled) {
      registerNamedHook("before_prompt_build", (event, ctx) => {
        const hookName = "before_prompt_build:visual-harvest";
        if (shouldSkipHook(hookName)) return;
        try {
          deps.harvestVisualContextFromEvent(
            api,
            config,
            "before_prompt_build",
            event,
            deps.normalizeHookContext(ctx),
          );
          resetHookFailure(hookName);
        } catch (err) {
          // Log but don't propagate - hooks must never break the host
          recordHookFailure(hookName);
          api?.logger?.warn?.(
            `[memory-palace] ${hookName} hook failed: ${err instanceof Error ? err.message : String(err)}`,
          );
        }
      });
    }
  }

  if (
    config.visualMemory.enabled ||
    config.autoCapture.enabled ||
    config.capturePipeline.captureAssistantDerived ||
    config.smartExtraction.enabled ||
    config.reflection.enabled
  ) {
    registerNamedHook("agent_end", async (event, ctx) => {
      const hookName = "agent_end";
      if (shouldSkipHook(hookName)) return;
      try {
        const normalizedCtx = deps.normalizeHookContext(ctx);
        if (config.visualMemory.enabled) {
          deps.harvestVisualContextFromEvent(
            api,
            config,
            "agent_end",
            event,
            normalizedCtx,
          );
        }
        if (
          !config.autoCapture.enabled &&
          !config.capturePipeline.captureAssistantDerived &&
          !config.smartExtraction.enabled &&
          !config.reflection.enabled
        ) {
          resetHookFailure(hookName);
          return;
        }
        await deps.runAutoCaptureHook(api, config, session, event, normalizedCtx);
        if (!config.reflection.enabled) {
          resetHookFailure(hookName);
          return;
        }
        if (config.reflection.source === "compact_context") {
          await deps.runReflectionFromCompactContext(
            api,
            config,
            session,
            event,
            normalizedCtx,
          );
          resetHookFailure(hookName);
          return;
        }
        if (config.reflection.source === "command_new") {
          resetHookFailure(hookName);
          return;
        }
        await deps.runReflectionFromAgentEnd(
          api,
          config,
          session,
          event,
          normalizedCtx,
        );
        resetHookFailure(hookName);
      } catch (err) {
        // Log but don't propagate - hooks must never break the host
        recordHookFailure(hookName);
        api?.logger?.warn?.(
          `[memory-palace] ${hookName} hook failed: ${err instanceof Error ? err.message : String(err)}`,
        );
      }
    });
  }
}

import { createHash } from "node:crypto";
import type { OpenClawPluginApi } from "openclaw/plugin-sdk/core";
import type {
  DurableSynthesisEvidence,
  DurableSynthesisSourceMode,
  PluginConfig,
  ProfileBlockName,
  PluginRuntimeCapturePath,
  PluginRuntimeRuleCaptureDecision,
  ResolvedAclPolicy,
  SharedClientSession,
} from "./types.js";

type AutoCaptureAnalysis =
  | {
      decision: "direct";
      reason: "capture_signal";
      category: string;
      summary: string;
    }
  | {
      decision: "explicit";
      reason: "explicit_memory_intent";
      category: string;
      summary: string;
    }
  | {
      decision: "pending";
      reason: "recent_future_plan";
      category: "event";
      summary: string;
    }
  | {
      decision: "skip";
      reason: string;
      summary?: string;
      category?: string;
    };

type ProfileMemoryBlockUpsertResult = {
  ok: boolean;
  created: boolean;
  merged: boolean;
  uri: string;
  message?: string;
};

export type AutoCaptureDeps = {
  analyzeAutoCaptureText: (
    text: string,
    config: PluginConfig["autoCapture"],
  ) => AutoCaptureAnalysis;
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
  buildDurableSynthesisContent: (params: {
    category: string;
    sourceMode: DurableSynthesisSourceMode;
    captureLayer: string;
    summary: string;
    confidence: number;
    pending: boolean;
    evidence: DurableSynthesisEvidence[];
  }) => string;
  buildDurableSynthesisUri: (
    config: PluginConfig,
    policy: ResolvedAclPolicy,
    sourceMode: DurableSynthesisSourceMode,
    category: string,
    summary: string,
    pending: boolean,
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
  ) => Promise<{ ok: boolean; merged?: boolean }>;
  extractMessageTexts: (
    messages: unknown[],
    allowedRoles?: string[],
  ) => string[];
  formatError: (error: unknown) => string;
  inferCaptureCategory: (text: string) => string;
  isSuccessfulAgentTurn: (event: Record<string, unknown>) => boolean;
  isUriWritableByAcl: (
    uri: string,
    policy: ResolvedAclPolicy,
    defaultDomain: string,
  ) => boolean;
  isWriteGuardCreateBlockedError: (error: unknown) => boolean;
  isWriteGuardUpdateBlockedError: (error: unknown) => boolean;
  logPluginTrace: (
    api: OpenClawPluginApi,
    enabled: boolean,
    eventName: string,
    payload: Record<string, unknown>,
  ) => void;
  mapCaptureCategoryToProfileBlock: (
    category: string,
  ) => ProfileBlockName | undefined;
  normalizeText: (value: string) => string;
  readString: (value: unknown) => string | undefined;
  recordPluginCapturePath: (
    config: PluginConfig,
    client: SharedClientSession["client"] | undefined,
    payload: PluginRuntimeCapturePath,
  ) => void;
  recordPluginRuleCaptureDecision: (
    config: PluginConfig,
    client: SharedClientSession["client"] | undefined,
    payload: PluginRuntimeRuleCaptureDecision,
  ) => void;
  resolveAclPolicy: (
    config: PluginConfig,
    agentId?: string,
  ) => ResolvedAclPolicy;
  resolveContextAgentIdentity: (
    ctx: Record<string, unknown>,
  ) => { value?: string; source?: string };
  runAssistantDerivedCaptureHook: (
    api: OpenClawPluginApi,
    config: PluginConfig,
    session: SharedClientSession,
    event: Record<string, unknown>,
    ctx: Record<string, unknown>,
  ) => Promise<void>;
  runSmartExtractionCaptureHook: (
    api: OpenClawPluginApi,
    config: PluginConfig,
    session: SharedClientSession,
    event: Record<string, unknown>,
    ctx: Record<string, unknown>,
  ) => Promise<void>;
  shouldAutoCapture: (
    text: string,
    config: PluginConfig["autoCapture"],
  ) => boolean;
  truncate: (text: string, limit: number) => string;
  upsertDurableSynthesisRecordWithTransientRetry: (
    client: SharedClientSession["client"],
    config: PluginConfig,
    targetUri: string,
    params: {
      category: string;
      sourceMode: DurableSynthesisSourceMode;
      captureLayer: string;
      summary: string;
      confidence: number;
      pending: boolean;
      evidence: DurableSynthesisEvidence[];
      summaryStrategy?: "preserve" | "replace";
      disclosure?: string;
    },
  ) => Promise<{ ok: boolean; created: boolean; merged: boolean; uri: string; message?: string }>;
  upsertProfileMemoryBlockWithTransientRetry: (
    client: SharedClientSession["client"],
    config: PluginConfig,
    policy: ResolvedAclPolicy,
    profileBlock: ProfileBlockName,
    text: string,
  ) => Promise<ProfileMemoryBlockUpsertResult>;
  buildProfileMemoryUri: (
    config: PluginConfig,
    policy: ResolvedAclPolicy,
    block: ProfileBlockName,
  ) => string;
};

export async function runAutoCaptureHook(
  api: OpenClawPluginApi,
  options: {
    config: PluginConfig;
    deps: AutoCaptureDeps;
    event: Record<string, unknown>;
    session: SharedClientSession;
    ctx: Record<string, unknown>;
  },
): Promise<void> {
  const { config, deps, event, session, ctx } = options;
  const assistantDerivedEnabled =
    config.capturePipeline.mode === "v2" &&
    config.capturePipeline.captureAssistantDerived;
  const smartExtractionEnabled =
    config.smartExtraction.enabled &&
    config.smartExtraction.effectiveMode !== "off";
  const eventMessages = Array.isArray(event.messages) ? event.messages : [];
  if (
    !deps.isSuccessfulAgentTurn(event) ||
    (!config.autoCapture.enabled &&
      !assistantDerivedEnabled &&
      !smartExtractionEnabled)
  ) {
    return;
  }
  const identity = deps.resolveContextAgentIdentity(ctx);
  const policy = deps.resolveAclPolicy(config, identity.value);
  const uniqueTexts = Array.from(
    new Map(
      deps
        .extractMessageTexts(eventMessages, ["user"])
        .map((text) => [
          createHash("sha256")
            .update(deps.normalizeText(text))
            .digest("hex"),
          text,
        ]),
    ).values(),
  );
  const analyses = config.autoCapture.enabled
    ? uniqueTexts.map((text) => ({
        text,
        analysis: deps.analyzeAutoCaptureText(text, config.autoCapture),
      }))
    : [];
  // "explicit" decisions should NOT be auto-captured.  They indicate the
  // user explicitly asked to remember something, which must flow through
  // the `memory_learn` tool (with its own confirmation chain) rather than
  // being silently persisted by auto-capture.
  const capturePlans = analyses
    .filter(
      (entry) =>
        entry.analysis.decision === "direct" ||
        entry.analysis.decision === "pending",
    )
    .slice(0, config.autoCapture.maxItemsPerRun);
  if (capturePlans.length === 0 && !assistantDerivedEnabled && !smartExtractionEnabled) {
    const firstDecision = analyses.find((entry) => entry.analysis.decision === "skip");
    if (firstDecision) {
      deps.recordPluginRuleCaptureDecision(config, session.client, {
        at: new Date().toISOString(),
        decision: "skipped",
        reason: firstDecision.analysis.reason,
        category: firstDecision.analysis.category,
        details: deps.truncate(firstDecision.text, 160),
      });
    }
    return;
  }

  let stored = 0;
  let lastRuleDecision: PluginRuntimeRuleCaptureDecision | null = null;
  for (const plan of capturePlans) {
    const text = plan.text;
    const category = plan.analysis.category;
    if (!category) {
      continue;
    }
    const pending = plan.analysis.decision === "pending";
    const pendingPlan = plan.analysis.decision === "pending" ? plan.analysis : undefined;
    const effectiveDetails = pendingPlan?.summary ?? text;
    const profileBlock = config.profileMemory.enabled
      ? deps.mapCaptureCategoryToProfileBlock(category)
      : undefined;
    const targetUri = pending
      ? deps.buildDurableSynthesisUri(
          config,
          policy,
          "rule_capture",
          category,
          pendingPlan?.summary ?? text,
          true,
        )
      : deps.buildAutoCaptureUri(config, policy, category, text);
    const captureWritable = deps.isUriWritableByAcl(
      targetUri,
      policy,
      config.mapping.defaultDomain,
    );
    const profileTargetUri = profileBlock
      ? deps.buildProfileMemoryUri(config, policy, profileBlock)
      : undefined;
    const profileWritable = profileTargetUri
      ? deps.isUriWritableByAcl(
          profileTargetUri,
          policy,
          config.mapping.defaultDomain,
        )
      : false;
    if (!captureWritable && !profileWritable) {
      continue;
    }
    try {
      let action = "ADD";
      // When capture path is ACL-blocked but profile path is writable,
      // write only the profile block (the ACL test expects this).
        if (!captureWritable) {
          if (profileBlock && profileWritable && profileTargetUri) {
            try {
              const profileResult = await session.withClient(async (client) =>
                deps.upsertProfileMemoryBlockWithTransientRetry(
                  client,
                  config,
                  policy,
                  profileBlock,
                  text,
                ),
              );
              if (!profileResult.ok) {
                api.logger.warn(
                  `memory-palace profile block capture failed: ${profileResult.message ?? "profile_block_write_failed"}`,
                );
              }
            } catch (error) {
              api.logger.warn(
                `memory-palace profile block capture failed: ${deps.formatError(error)}`,
              );
          }
        }
        continue;
      }
      const result = await session.withClient(async (client) => {
        if (pending) {
          return deps.upsertDurableSynthesisRecordWithTransientRetry(
            client,
            config,
            targetUri,
            {
              category,
              sourceMode: "rule_capture",
              captureLayer: "auto_capture_pending",
              summary: pendingPlan?.summary ?? text,
              confidence: 0.68,
              pending: true,
              evidence: [
                {
                  key: "user[rule-plan]",
                  source: "user_message",
                  lineStart: 1,
                  lineEnd: 1,
                  snippet: deps.truncate(text, 220),
                },
              ],
              disclosure:
                "Pending near-future plan captured from the user's message; confirm or expire if it changes.",
            },
          );
        }
        return deps.createOrMergeMemoryRecord(
          client,
          targetUri,
          deps.buildAutoCaptureContent({
            agentId: identity.value,
            sessionId: deps.readString(ctx.sessionId),
            sessionKey: deps.readString(ctx.sessionKey),
            category,
            text,
          }),
          {
            priority:
              category === "profile" ||
              category === "preference" ||
              category === "workflow"
                ? 1
                : 2,
            disclosure: policy.disclosure,
            lane: "capture",
          },
        );
      });
      if (result.ok) {
        stored += 1;
        action = result.merged ? "UPDATE" : "ADD";
        // Deferred profile block upsert – only runs after capture succeeded.
        if (profileBlock && profileWritable && profileTargetUri) {
          try {
            const profileResult = await session.withClient(async (client) =>
              deps.upsertProfileMemoryBlockWithTransientRetry(
                client,
                config,
                policy,
                profileBlock,
                text,
              ),
            );
            if (!profileResult.ok) {
              api.logger.warn(
                `memory-palace profile block capture failed: ${profileResult.message ?? "profile_block_write_failed"}`,
              );
            }
          } catch (error) {
            api.logger.warn(
              `memory-palace profile block capture failed: ${deps.formatError(error)}`,
            );
          }
        }
        deps.recordPluginCapturePath(config, session.client, {
          at: new Date().toISOString(),
          layer: pending
            ? "auto_capture_pending"
            : plan.analysis.decision === "explicit"
              ? "auto_capture_explicit"
              : "auto_capture",
          category,
          uri: targetUri,
          pending,
          action,
          details: deps.truncate(effectiveDetails, 160),
        });
        lastRuleDecision = {
          at: new Date().toISOString(),
          decision: pending ? "pending" : "captured",
          reason: plan.analysis.reason,
          category,
          uri: targetUri,
          pending,
          details: deps.truncate(effectiveDetails, 160),
        };
      }
    } catch (error) {
      if (
        deps.isWriteGuardCreateBlockedError(error) ||
        deps.isWriteGuardUpdateBlockedError(error)
      ) {
        deps.logPluginTrace(
          api,
          config.autoCapture.traceEnabled,
          "memory-palace:auto-capture-skip",
          {
            agentId: identity.value,
            identitySource: identity.source,
            category,
            targetUri,
            reason: deps.formatError(error),
          },
        );
        lastRuleDecision = {
          at: new Date().toISOString(),
          decision: "skipped",
          reason: "write_guard_blocked",
          category,
          pending,
          details: deps.truncate(text, 160),
        };
        continue;
      }
      api.logger.warn(`memory-palace auto capture failed: ${deps.formatError(error)}`);
      lastRuleDecision = {
        at: new Date().toISOString(),
        decision: "skipped",
        reason: "write_failed",
        category,
        pending,
        details: deps.truncate(text, 160),
      };
    }
  }
  if (!lastRuleDecision) {
    const firstDecision = analyses.find((entry) => entry.analysis.decision === "skip");
    if (firstDecision) {
      lastRuleDecision = {
        at: new Date().toISOString(),
        decision: "skipped",
        reason: firstDecision.analysis.reason,
        category: firstDecision.analysis.category,
        details: deps.truncate(firstDecision.text, 160),
      };
    }
  }
  if (lastRuleDecision) {
    deps.recordPluginRuleCaptureDecision(config, session.client, lastRuleDecision);
  }
  if (assistantDerivedEnabled) {
    try {
      await deps.runAssistantDerivedCaptureHook(api, config, session, event, ctx);
    } catch (error) {
      api.logger.warn(
        `memory-palace assistant-derived capture failed: ${deps.formatError(error)}`,
      );
    }
  }
  if (smartExtractionEnabled) {
    try {
      await deps.runSmartExtractionCaptureHook(api, config, session, event, ctx);
    } catch (error) {
      api.logger.warn(
        `memory-palace smart extraction failed: ${deps.formatError(error)}`,
      );
    }
  }
  deps.logPluginTrace(
    api,
    config.autoCapture.traceEnabled,
    "memory-palace:auto-capture",
    {
      agentId: identity.value,
      identitySource: identity.source,
      candidates: capturePlans.length,
      stored,
    },
  );
}

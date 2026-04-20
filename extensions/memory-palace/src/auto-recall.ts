import type { OpenClawPluginApi } from "openclaw/plugin-sdk/core";
import type {
  HostWorkspaceHit,
  MemorySearchResult,
  PluginConfig,
  ProfileBlockName,
  ResolvedAclPolicy,
  SharedClientSession,
} from "./types.js";

type ProfilePromptEntry = {
  block: ProfileBlockName;
  text: string;
};

const WORKFLOW_RECALL_HINT_PATTERNS = [
  /\b(default workflow|workflow|process|review order|delivery order|coding habits?|programming habits?|code first|tests? immediately|docs? last)\b/iu,
  /(默认工作流|默认流程|工作流|顺序|编程习惯|代码习惯|先写代码|先做代码|立刻跑测试|马上跑测试|文档最后)/u,
] as const;

const PREFERENCE_RECALL_HINT_PATTERNS = [
  /\b(prefer|preference|like to use|usually use|code review)\b/iu,
  /(偏好|喜欢用|习惯用|code review|评审习惯|review 偏好)/u,
] as const;

function collectRequestedHostBridgeCategories(prompt: string): Set<string> {
  const categories = new Set<string>();
  if (WORKFLOW_RECALL_HINT_PATTERNS.some((pattern) => pattern.test(prompt))) {
    categories.add("workflow");
  }
  if (PREFERENCE_RECALL_HINT_PATTERNS.some((pattern) => pattern.test(prompt))) {
    categories.add("preference");
  }
  return categories;
}

function collectDurableRecallCategories(results: MemorySearchResult[]): Set<string> {
  const categories = new Set<string>();
  for (const result of results) {
    const haystack = `${result.path} ${result.citation ?? ""} ${result.snippet}`.toLowerCase();
    if (
      haystack.includes("/profile/workflow") ||
      haystack.includes("/captured/workflow/") ||
      haystack.includes("default workflow") ||
      haystack.includes("workflow")
    ) {
      categories.add("workflow");
    }
    if (
      haystack.includes("/profile/preferences") ||
      haystack.includes("/captured/preference/") ||
      haystack.includes("preferences") ||
      haystack.includes("preference")
    ) {
      categories.add("preference");
    }
  }
  return categories;
}

export type AutoRecallDeps = {
  buildRecallQueryVariants: (prompt: string) => string[];
  decideAutoRecall: (
    prompt: string,
    config: PluginConfig["autoRecall"],
  ) => { shouldRecall: boolean; forced: boolean; reasons: string[] };
  formatError: (error: unknown) => string;
  formatHostBridgePromptContext: (hits: HostWorkspaceHit[]) => string;
  formatProfilePromptContext: (entries: ProfilePromptEntry[]) => string;
  formatPromptContext: (
    tag: string,
    lane: string,
    results: MemorySearchResult[],
  ) => string;
  importHostBridgeHits: (
    api: OpenClawPluginApi,
    config: PluginConfig,
    session: SharedClientSession,
    policy: ResolvedAclPolicy,
    hits: HostWorkspaceHit[],
  ) => Promise<unknown>;
  loadProfilePromptEntries: (
    client: SharedClientSession["client"],
    config: PluginConfig,
    policy: ResolvedAclPolicy,
  ) => Promise<ProfilePromptEntry[]>;
  logPluginTrace: (
    api: OpenClawPluginApi,
    enabled: boolean,
    eventName: string,
    payload: Record<string, unknown>,
  ) => void;
  parseReflectionSearchPrefix: (
    config: PluginConfig,
    policy: ResolvedAclPolicy,
  ) => string;
  readString: (value: unknown) => string | undefined;
  resolveAclPolicy: (
    config: PluginConfig,
    agentId?: string,
  ) => ResolvedAclPolicy;
  resolveContextAgentIdentity: (
    ctx: Record<string, unknown>,
  ) => { value?: string; source?: string };
  resolveHostWorkspaceDir: (
    ctx: Record<string, unknown>,
    agentId?: string,
  ) => string | undefined;
  runScopedSearch: (
    client: SharedClientSession["client"],
    query: string,
    config: PluginConfig,
    policy: ResolvedAclPolicy,
    options?: {
      maxResults?: number;
      includeSession?: boolean;
      includeReflection?: boolean;
      filters?: Record<string, unknown>;
    },
  ) => Promise<{ results: MemorySearchResult[] }>;
  scanHostWorkspaceForQuery: (
    query: string,
    workspaceDir: string,
    config: PluginConfig["hostBridge"],
  ) => HostWorkspaceHit[] | Promise<HostWorkspaceHit[]>;
  shouldSkipHostBridgeRecall: (
    workspaceDir: string,
    agentKey: string,
    prompt: string,
    cooldownMs: number,
  ) => boolean;
};

export async function runAutoRecallHook(
  api: OpenClawPluginApi,
  options: {
    config: PluginConfig;
    deps: AutoRecallDeps;
    event: Record<string, unknown>;
    session: SharedClientSession;
    ctx: Record<string, unknown>;
  },
): Promise<{ prependContext?: string } | void> {
  const { config, deps, event, session, ctx } = options;
  const prompt = deps.readString(event.prompt);
  const profileRecallEnabled =
    config.profileMemory.enabled && config.profileMemory.injectBeforeAgentStart;
  const durableRecallEnabled = config.autoRecall.enabled;
  const reflectionRecallEnabled =
    config.reflection.enabled && config.reflection.autoRecall;
  const hostBridgeRecallEnabled = config.hostBridge.enabled;
  if (
    !prompt ||
    (!profileRecallEnabled &&
      !durableRecallEnabled &&
      !reflectionRecallEnabled &&
      !hostBridgeRecallEnabled)
  ) {
    return;
  }
  const identity = deps.resolveContextAgentIdentity(ctx);
  const decision = deps.decideAutoRecall(prompt, config.autoRecall);
  deps.logPluginTrace(api, config.autoRecall.traceEnabled, "memory-palace:auto-recall", {
    agentId: identity.value,
    identitySource: identity.source,
    profileEnabled: profileRecallEnabled,
    hostBridgeEnabled: hostBridgeRecallEnabled,
    shouldRecall: decision.shouldRecall,
    forced: decision.forced,
    reasons: decision.reasons,
  });
  if (!profileRecallEnabled && !decision.shouldRecall) {
    return;
  }

  const policy = deps.resolveAclPolicy(config, identity.value);
  try {
    const sections: string[] = [];
    let hostBridgeHits: HostWorkspaceHit[] = [];
    let hasNonProfileRecallContext = false;
    let missingHostBridgeCategories = new Set<string>();

    if (profileRecallEnabled) {
      const profileEntries = await session.withClient(async (client) =>
        deps.loadProfilePromptEntries(client, config, policy),
      );
      if (profileEntries.length > 0) {
        sections.push(deps.formatProfilePromptContext(profileEntries));
      }
    }

    if (durableRecallEnabled && decision.shouldRecall) {
      let payload = await session.withClient(async (client) =>
        deps.runScopedSearch(client, prompt, config, policy, {
          maxResults: config.autoRecall.maxResults,
          includeSession: true,
          includeReflection: false,
          filters: config.query.filters,
        }),
      );
      if (payload.results.length === 0) {
        for (const variant of deps.buildRecallQueryVariants(prompt).slice(1)) {
          payload = await session.withClient(async (client) =>
            deps.runScopedSearch(client, variant, config, policy, {
              maxResults: config.autoRecall.maxResults,
              includeSession: true,
              includeReflection: false,
              filters: config.query.filters,
            }),
          );
          if (payload.results.length > 0) {
            break;
          }
        }
      }
      if (payload.results.length > 0) {
        sections.push(
          deps.formatPromptContext(
            "memory-palace-recall",
            "durable-memory",
            payload.results,
          ),
        );
        hasNonProfileRecallContext = true;
        const requestedHostBridgeCategories =
          collectRequestedHostBridgeCategories(prompt);
        if (requestedHostBridgeCategories.size > 0) {
          const durableRecallCategories =
            collectDurableRecallCategories(payload.results);
          missingHostBridgeCategories = new Set(
            Array.from(requestedHostBridgeCategories).filter(
              (category) => !durableRecallCategories.has(category),
            ),
          );
        }
      }
    }

    if (reflectionRecallEnabled && decision.shouldRecall) {
      const reflectionPayload = await session.withClient(async (client) =>
        deps.runScopedSearch(client, prompt, config, policy, {
          maxResults: config.reflection.maxResults,
          includeSession: true,
          includeReflection: true,
          filters: {
            path_prefix: deps.parseReflectionSearchPrefix(config, policy),
          },
        }),
      );
      if (reflectionPayload.results.length > 0) {
        sections.push(
          deps.formatPromptContext(
            "memory-palace-reflection",
            "reflection-lane",
            reflectionPayload.results,
          ),
        );
        hasNonProfileRecallContext = true;
      }
    }

    if (
      hostBridgeRecallEnabled &&
      decision.shouldRecall &&
      (!hasNonProfileRecallContext || missingHostBridgeCategories.size > 0)
    ) {
      const workspaceDir = deps.resolveHostWorkspaceDir(ctx, identity.value);
      if (
        workspaceDir &&
        !deps.shouldSkipHostBridgeRecall(
          workspaceDir,
          policy.agentKey,
          prompt,
          15_000,
        )
      ) {
        hostBridgeHits = await deps.scanHostWorkspaceForQuery(
          prompt,
          workspaceDir,
          config.hostBridge,
        );
        if (missingHostBridgeCategories.size > 0) {
          hostBridgeHits = hostBridgeHits.filter((entry) =>
            missingHostBridgeCategories.has(entry.category),
          );
        }
        if (hostBridgeHits.length > 0) {
          const hostBridgeContext = deps.formatHostBridgePromptContext(hostBridgeHits);
          if (hostBridgeContext) {
            sections.push(hostBridgeContext);
          }
          const imported = await deps.importHostBridgeHits(
            api,
            config,
            session,
            policy,
            hostBridgeHits,
          );
          deps.logPluginTrace(
            api,
            config.hostBridge.traceEnabled,
            "memory-palace:host-bridge-import",
            {
              agentId: identity.value,
              identitySource: identity.source,
              workspaceDir,
              imported,
              hits: hostBridgeHits.length,
              missingCategories: Array.from(missingHostBridgeCategories),
            },
          );
        }
      }
    }

    if (sections.length === 0) {
      return;
    }
    deps.logPluginTrace(
      api,
      config.autoRecall.traceEnabled,
      "memory-palace:auto-recall-result",
      {
        agentId: identity.value,
        identitySource: identity.source,
        sectionCount: sections.length,
        hostBridgeHits: hostBridgeHits.length,
      },
    );
    return {
      prependContext: sections.join("\n\n"),
    };
  } catch (error) {
    api.logger.warn(`memory-palace auto recall failed: ${deps.formatError(error)}`);
    return;
  }
}

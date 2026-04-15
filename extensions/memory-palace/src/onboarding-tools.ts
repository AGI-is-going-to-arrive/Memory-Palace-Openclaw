import { spawn } from "node:child_process";
import path from "node:path";
import type {
  AnyAgentTool,
  OpenClawPluginToolContext,
} from "openclaw/plugin-sdk/core";
import {
  memoryOnboardingApplySchema,
  memoryOnboardingProviderProbeSchema,
  memoryOnboardingStatusSchema,
} from "./config-schema.js";
import type { PluginRuntimeLayout, TraceLogger } from "./types.js";

type JsonRecord = Record<string, unknown>;

export type OnboardingToolDeps = {
  formatError: (error: unknown) => string;
  jsonResult: (value: unknown) => unknown;
  readBoolean: (value: unknown) => boolean | undefined;
  readString: (value: unknown) => string | undefined;
  runLauncherCommand?: (
    layout: PluginRuntimeLayout,
    commandArgs: string[],
    childEnv?: Record<string, string>,
  ) => Promise<LauncherResult>;
};

type LauncherResult = {
  exitCode: number;
  payload?: JsonRecord;
  stdout: string;
  stderr: string;
};

const ONBOARDING_STATUS_TOOL = "memory_onboarding_status";
const ONBOARDING_PROBE_TOOL = "memory_onboarding_probe";
const ONBOARDING_APPLY_TOOL = "memory_onboarding_apply";
const ONBOARDING_TIMEOUT_MS = 300_000;

function isChineseLocale(value: unknown): boolean {
  return typeof value === "string" && /^zh\b/i.test(value.trim());
}

function localizedText(locale: string | undefined, zh: string, en: string): string {
  return isChineseLocale(locale) ? zh : en;
}

function pickValue<T = unknown>(record: Record<string, unknown>, ...keys: string[]): T | undefined {
  for (const key of keys) {
    if (Object.prototype.hasOwnProperty.call(record, key) && record[key] !== undefined) {
      return record[key] as T;
    }
  }
  return undefined;
}

function parseJsonPayload(stdout: string, stderr: string): JsonRecord | undefined {
  for (const candidate of [stdout, stderr]) {
    const rendered = candidate.trim();
    if (!rendered) {
      continue;
    }
    try {
      const parsed = JSON.parse(rendered);
      if (parsed && typeof parsed === "object") {
        return parsed as JsonRecord;
      }
    } catch {
      // Ignore non-JSON streams and surface them via the fallback error payload.
    }
  }
  return undefined;
}

function resolveLauncherPath(layout: PluginRuntimeLayout): string {
  return layout.isPackagedPluginLayout
    ? path.resolve(layout.packagedScriptsRoot, "openclaw_memory_palace_launcher.mjs")
    : path.resolve(layout.pluginProjectRoot, "scripts", "openclaw_memory_palace_launcher.mjs");
}

async function runLauncherCommand(
  layout: PluginRuntimeLayout,
  commandArgs: string[],
  childEnv?: Record<string, string>,
): Promise<LauncherResult> {
  const launcherPath = resolveLauncherPath(layout);
  return new Promise<LauncherResult>((resolve, reject) => {
    const minimalEnv: Record<string, string> = {};
    const ALLOWED_ENV_KEYS = [
      "PATH",
      "HOME",
      "USERPROFILE",
      "HOMEDRIVE",
      "HOMEPATH",
      "TMPDIR",
      "TMP",
      "TEMP",
      "LANG",
      "NODE_ENV",
      "PYTHONPATH",
      "VIRTUAL_ENV",
      "OPENCLAW_BIN",
      "OPENCLAW_CONFIG_PATH",
      "OPENCLAW_CONFIG",
      "OPENCLAW_STATE_DIR",
      "OPENCLAW_MEMORY_PALACE_RUNTIME_PYTHON",
      "HTTP_PROXY",
      "HTTPS_PROXY",
      "NO_PROXY",
      "http_proxy",
      "https_proxy",
      "no_proxy",
      "ROUTER_API_BASE",
      "ROUTER_API_KEY",
      "ROUTER_EMBEDDING_MODEL",
      "RETRIEVAL_EMBEDDING_API_BASE",
      "RETRIEVAL_EMBEDDING_API_KEY",
      "RETRIEVAL_EMBEDDING_MODEL",
      "RETRIEVAL_EMBEDDING_DIM",
      "RETRIEVAL_RERANKER_API_BASE",
      "RETRIEVAL_RERANKER_API_KEY",
      "RETRIEVAL_RERANKER_MODEL",
      "LLM_API_BASE",
      "LLM_API_KEY",
      "LLM_MODEL_NAME",
      "WRITE_GUARD_LLM_API_BASE",
      "WRITE_GUARD_LLM_API_KEY",
      "WRITE_GUARD_LLM_MODEL",
      "COMPACT_GIST_LLM_API_BASE",
      "COMPACT_GIST_LLM_API_KEY",
      "COMPACT_GIST_LLM_MODEL",
      "INTENT_LLM_API_BASE",
      "INTENT_LLM_API_KEY",
      "INTENT_LLM_MODEL",
      "XDG_CONFIG_HOME",
      "APPDATA",
      "LOCALAPPDATA",
      "COMSPEC",
      "ComSpec",
      "PATHEXT",
      "SystemRoot",
    ];
    for (const key of ALLOWED_ENV_KEYS) {
      if (process.env[key]) minimalEnv[key] = process.env[key]!;
    }
    const child = spawn(process.execPath, [launcherPath, ...commandArgs], {
      cwd: layout.pluginProjectRoot,
      env: {
        ...minimalEnv,
        OPENCLAW_MEMORY_PALACE_PLUGIN_ROOT_HINT: layout.pluginExtensionRoot,
        ...(childEnv ?? {}),
      },
      stdio: ["ignore", "pipe", "pipe"],
    });
    const stdoutChunks: string[] = [];
    const stderrChunks: string[] = [];
    let settled = false;
    const finish = (callback: () => void) => {
      if (settled) {
        return;
      }
      settled = true;
      clearTimeout(timer);
      callback();
    };
    const timer = setTimeout(() => {
      finish(() => {
        try {
          child.kill("SIGTERM");
        } catch {
          // Ignore late termination races once the launcher has exited.
        }
        reject(new Error(`Onboarding command timed out after ${ONBOARDING_TIMEOUT_MS}ms.`));
      });
    }, ONBOARDING_TIMEOUT_MS);
    timer.unref?.();

    child.stdout.on("data", (chunk) => {
      stdoutChunks.push(String(chunk));
    });
    child.stderr.on("data", (chunk) => {
      stderrChunks.push(String(chunk));
    });
    child.once("error", (error) => {
      finish(() => reject(error));
    });
    child.once("close", (code) => {
      finish(() => {
        const stdout = stdoutChunks.join("");
        const stderr = stderrChunks.join("");
        resolve({
          exitCode: typeof code === "number" ? code : 1,
          payload: parseJsonPayload(stdout, stderr),
          stdout,
          stderr,
        });
      });
    });
  });
}

function buildGuide(locale?: string) {
  return {
    recommendedPath: {
      summary: localizedText(
        locale,
        "只要真实 embedding 与 reranker 已经可用，就优先走 Profile C 或 D；Profile B 只保留为最安全的 bootstrap 回退基线。",
        "Prefer Profile C or D whenever real embedding and reranker providers are available. Keep Profile B only as the safest bootstrap fallback.",
      ),
      why: localizedText(
        locale,
        "Profile C/D 能解锁真实 provider-backed 检索质量、reranking，以及当前插件的最大能力边界。",
        "Profile C/D unlock real provider-backed retrieval quality, reranking, and the strongest capability envelope of this plugin.",
      ),
    },
    profiles: {
      b: {
        role: localizedText(locale, "安全起步基线。", "Safe bootstrap baseline."),
        retrieval: localizedText(
          locale,
          "混合检索 + 本地 hash embedding（64 维）+ 关闭 reranker。",
          "Hybrid search + local hash embedding (64 dim) + reranker disabled.",
        ),
        llmOptional: localizedText(
          locale,
          "可以。如果你提供了有效 LLM，Profile B 仍可使用可选的 WRITE_GUARD_LLM / COMPACT_GIST_LLM / INTENT_LLM 链路。",
          "Yes. Profile B can still use optional WRITE_GUARD_LLM / COMPACT_GIST_LLM / INTENT_LLM chains if you provide them.",
        ),
        boundary: localizedText(
          locale,
          "如果没有可选 LLM 配置，写入质量过滤和意图/gist 辅助会停留在保守的非 LLM 回退路径。",
          "Without optional LLM configuration, write quality filtering and intent/gist helpers stay on conservative non-LLM fallbacks.",
        ),
      },
      c: {
        role: localizedText(locale, "强烈推荐的本地/自托管高级路径。", "Strongly recommended local/self-hosted advanced path."),
        retrieval: localizedText(
          locale,
          "混合检索 + 真实 embedding provider + 开启 reranker。",
          "Hybrid search + real embedding provider + reranker enabled.",
        ),
        llmOptional: localizedText(
          locale,
          "可选但推荐。配置真实 chat 模型后，write guard 和 compact gist 才能发挥最大价值。",
          "Optional but recommended. Write guard and compact gist become most useful when a real chat model is configured.",
        ),
        boundary: localizedText(
          locale,
          "签收前至少需要 embedding 与 reranker 可用；LLM 仅在你要启用写入增强和 gist 增强时才需要补齐。",
          "Requires working embedding and reranker settings before signoff. Add LLM settings only when you want write-guard and compact-gist assists enabled.",
        ),
      },
      d: {
        role: localizedText(locale, "高级远程/API 客户环境路径。", "Advanced remote/customer-environment path."),
        retrieval: localizedText(
          locale,
          "混合检索 + 远程 embedding/reranker/LLM providers。",
          "Hybrid search + remote embedding/reranker/LLM providers.",
        ),
        llmOptional: localizedText(
          locale,
          "期望启用。Profile D 把 write guard / compact gist LLM 视为完整高级路径的一部分。",
          "Expected. Profile D treats write guard / compact gist LLM configuration as part of the full advanced path.",
        ),
        boundary: localizedText(
          locale,
          "当 provider 所在位置或部署边界是远程而不是本地，并且你准备好 embedding、reranker、LLM 三类高级 provider 时使用。",
          "Use when the provider location or deployment boundary is remote rather than local, and you are ready to supply embedding, reranker, and LLM providers together.",
        ),
      },
    },
    providerFormats: {
      embedding: {
        requiredForProfiles: ["c", "d"],
        acceptedBaseUrlForms: [
          "https://provider.example.com/v1",
          "https://provider.example.com/v1/embeddings",
        ],
        requiredFields: isChineseLocale(locale)
          ? ["embedding API base URL", "embedding API key", "embedding 模型名"]
          : ["embedding API base URL", "embedding API key", "embedding model name"],
        optionalFields: [localizedText(locale, "embedding 维度", "embedding dimension")],
        dimensionPolicy: localizedText(
          locale,
          "provider probe 成功后，安装器会探测该 provider 返回的原生最大 embedding 维度，并建议把相同值写进 RETRIEVAL_EMBEDDING_DIM。",
          "When the provider probe succeeds, the installer detects the native maximum embedding dimension returned by the provider and recommends writing that same value into RETRIEVAL_EMBEDDING_DIM.",
        ),
      },
      reranker: {
        requiredForProfiles: ["c", "d"],
        acceptedBaseUrlForms: [
          "https://provider.example.com/v1",
          "https://provider.example.com/v1/rerank",
        ],
        requiredFields: isChineseLocale(locale)
          ? ["reranker API base URL", "reranker API key", "reranker 模型名"]
          : ["reranker API base URL", "reranker API key", "reranker model name"],
      },
      llm: {
        requiredForProfiles: ["d"],
        optionalForProfiles: ["b", "c"],
        acceptedBaseUrlForms: [
          "https://provider.example.com/v1",
          "https://provider.example.com/v1/chat/completions",
          "https://provider.example.com/v1/responses",
        ],
        requiredFields: isChineseLocale(locale)
          ? ["LLM API base URL", "LLM API key", "LLM 模型名"]
          : ["LLM API base URL", "LLM API key", "LLM model name"],
        runtimeNote: localizedText(
          locale,
          "当前运行时接受 OpenAI-compatible base URL。`/responses` 形式输入会作为别名接收并归一化回 base URL，但主 LLM 流程当前仍调用 `/chat/completions`。",
          "The current runtime accepts OpenAI-compatible base URLs. `/responses` input is accepted as an alias and normalized to the base URL, while the main LLM flows currently invoke `/chat/completions`.",
        ),
      },
    },
    conversationHints: [
      localizedText(locale, "先检查 onboarding 当前状态。", "Start by checking onboarding status."),
      localizedText(locale, "如果用户要最强路径，先收齐 C/D provider 输入，再在 apply 前做 provider probe。", "If the user wants the strongest setup, collect C/D provider inputs first and run a provider probe before apply."),
      localizedText(locale, "如果 provider 字段缺失或 probe 失败，要用通俗语言说清具体缺口，再决定是否回退到 Profile B。", "If provider fields are missing or a probe fails, explain the exact gap in plain language and only then fall back to Profile B."),
      localizedText(locale, "高级 probe 成功后，要明确建议使用探测到的最大 embedding 维度。", "After a successful advanced probe, recommend applying the detected maximum embedding dimension."),
    ],
  };
}

// --- IMP-2: Install guidance rendering ----------

function renderInstallGuidance(guidance: JsonRecord, locale?: string): string {
  const recommended = String(guidance.recommendedMethod || "source-checkout");
  const commands = guidance.installCommands && typeof guidance.installCommands === "object"
    ? (guidance.installCommands as Record<string, string>)
    : {};
  const stepMap = guidance.installSteps && typeof guidance.installSteps === "object"
    ? (guidance.installSteps as Record<string, unknown>)
    : {};
  const repoUrlSupported = Boolean(guidance.repoUrlDirectInstallSupported);
  const note = typeof guidance.recommendedMethodNote === "string"
    ? guidance.recommendedMethodNote.trim()
    : "";
  const parts: string[] = [
    localizedText(
      locale,
      `推荐安装方式：${recommended}。`,
      `Recommended install method: ${recommended}.`,
    ),
  ];
  const cmd = commands[recommended];
  if (cmd) {
    parts.push(
      localizedText(locale, `命令：\`${cmd}\``, `Command: \`${cmd}\``),
    );
  }
  const rawSteps = stepMap[recommended];
  const steps = Array.isArray(rawSteps)
    ? rawSteps.filter((item): item is string => typeof item === "string" && item.trim().length > 0)
    : [];
  if (steps.length > 0) {
    const renderedSteps = steps.map((step, index) => `${index + 1}. \`${step}\``).join(" ");
    parts.push(
      localizedText(locale, `完整步骤：${renderedSteps}`, `Full steps: ${renderedSteps}`),
    );
  }
  if (note) {
    parts.push(
      localizedText(locale, `说明：${note}`, `Note: ${note}`),
    );
  }
  if (!repoUrlSupported) {
    parts.push(
      localizedText(
        locale,
        "注意：OpenClaw 不支持直接从 repo URL 安装插件。",
        "Note: OpenClaw does not support installing plugins directly from a repo URL.",
      ),
    );
  }
  return parts.join(" ");
}

function buildStatusNarrative(payload: JsonRecord, locale?: string) {
  const setup = payload.setup && typeof payload.setup === "object" ? (payload.setup as JsonRecord) : {};
  const providerProbe =
    setup.providerProbe && typeof setup.providerProbe === "object"
      ? (setup.providerProbe as JsonRecord)
      : {};
  const requestedProfile = String(setup.requestedProfile || setup.effectiveProfile || "b").toUpperCase();
  const effectiveProfile = String(setup.effectiveProfile || requestedProfile || "B").toUpperCase();
  const fallbackApplied = Boolean(providerProbe.fallbackApplied);
  const requiresOnboarding = Boolean(setup.requiresOnboarding);
  const summaryParts = [
    requiresOnboarding
      ? localizedText(locale, "OpenClaw 里还没有完成 Memory Palace bootstrap。", "OpenClaw is not bootstrapped for Memory Palace yet.")
      : localizedText(locale, `Memory Palace 已经完成 bootstrap，当前生效档位是 Profile ${effectiveProfile}。`, `Memory Palace is already bootstrapped with effective Profile ${effectiveProfile}.`),
    localizedText(locale, `请求档位：${requestedProfile}。`, `Requested profile: ${requestedProfile}.`),
  ];
  if (fallbackApplied) {
    summaryParts.push(localizedText(locale, "最近一次高级 setup 已回退到 Profile B。", "The last advanced setup fell back to Profile B."));
  }

  // IMP-2: Render install guidance when present in the payload
  const installGuidance =
    setup.installGuidance && typeof setup.installGuidance === "object"
      ? (setup.installGuidance as JsonRecord)
      : payload.installGuidance && typeof payload.installGuidance === "object"
        ? (payload.installGuidance as JsonRecord)
        : undefined;
  let installGuidanceText: string | undefined;
  if (installGuidance) {
    installGuidanceText = renderInstallGuidance(installGuidance, locale);
  }

  return {
    summary: summaryParts.join(" "),
    requestedProfile,
    effectiveProfile,
    fallbackApplied,
    requiresOnboarding,
    installGuidanceText,
  };
}

function buildProbeNarrative(payload: JsonRecord, locale?: string) {
  const providers =
    payload.providers && typeof payload.providers === "object"
      ? (payload.providers as Record<string, JsonRecord>)
      : {};
  const embedding = providers.embedding && typeof providers.embedding === "object" ? providers.embedding : {};
  const detectedMaxDim = String(
    embedding.recommendedDim || embedding.detectedMaxDim || embedding.detectedDim || "",
  ).trim();
  const requestedProfile = String(payload.requestedProfile || "b").toUpperCase();
  const effectiveProfile = String(payload.effectiveProfile || requestedProfile).toUpperCase();
  const fallbackApplied = Boolean(payload.fallbackApplied);
  const summaryParts = [String(payload.summaryMessage || localizedText(locale, "Provider probe 已完成。", "Provider probe completed."))];
  if (detectedMaxDim) {
    summaryParts.push(
      localizedText(
        locale,
        `已探测到最大 embedding 维度：${detectedMaxDim}。建议把相同值写入 RETRIEVAL_EMBEDDING_DIM。`,
        `Detected maximum embedding dimension: ${detectedMaxDim}. Recommend applying the same value to RETRIEVAL_EMBEDDING_DIM.`,
      ),
    );
  }
  return {
    summary: summaryParts.join(" "),
    requestedProfile,
    effectiveProfile,
    fallbackApplied,
    detectedMaxDim: detectedMaxDim || null,
  };
}

function buildApplyNarrative(payload: JsonRecord, locale?: string) {
  const requestedProfile = String(payload.requested_profile || "b").toUpperCase();
  const effectiveProfile = String(payload.effective_profile || requestedProfile).toUpperCase();
  const fallbackApplied = Boolean(payload.fallback_applied);
  const profileProbe =
    payload.setup && typeof payload.setup === "object" && payload.setup !== null
      ? ((payload.setup as JsonRecord).providerProbe as JsonRecord | undefined)
      : undefined;
  const embeddingProvider =
    profileProbe?.providers && typeof profileProbe.providers === "object"
      ? ((profileProbe.providers as Record<string, JsonRecord>).embedding ?? {})
      : {};
  const detectedMaxDim = String(
    embeddingProvider.recommendedDim ||
      embeddingProvider.detectedMaxDim ||
      embeddingProvider.detectedDim ||
      "",
  ).trim();
  const summaryParts = [String(payload.summary || localizedText(locale, "Setup 已完成。", "Setup completed."))];
  const validation =
    payload.validation && typeof payload.validation === "object"
      ? (payload.validation as JsonRecord)
      : {};
  if (validation.ok === false) {
    summaryParts.unshift(
      localizedText(
        locale,
        "Setup 已返回结果，但 validation 失败；请先修复 verify/doctor/smoke 的失败项。",
        "Setup returned a payload, but validation failed. Fix the verify/doctor/smoke failures first.",
      ),
    );
  }
  if (detectedMaxDim) {
    summaryParts.push(
      localizedText(locale, `已应用/探测到的 embedding 维度：${detectedMaxDim}。`, `Applied/detected embedding dimension: ${detectedMaxDim}.`),
    );
  }
  if (fallbackApplied) {
    summaryParts.push(localizedText(locale, "高级 setup 没有停留在请求档位，而是回退到了 Profile B。", "Advanced setup did not stay on the requested profile and fell back to Profile B."));
  }
  return {
    summary: summaryParts.join(" "),
    requestedProfile,
    effectiveProfile,
    fallbackApplied,
    detectedMaxDim: detectedMaxDim || null,
  };
}

function pushStringArg(args: string[], flag: string, value: unknown, deps: OnboardingToolDeps) {
  if (typeof value === "number" && Number.isFinite(value)) {
    args.push(flag, String(value));
    return;
  }
  const rendered = deps.readString(value);
  if (rendered) {
    args.push(flag, rendered);
  }
}

function pushBooleanFlag(
  args: string[],
  flag: string,
  value: unknown,
  deps: OnboardingToolDeps,
) {
  if (deps.readBoolean(value)) {
    args.push(flag);
  }
}

function buildCommonArgs(paramsRecord: Record<string, unknown>, deps: OnboardingToolDeps): string[] {
  const args: string[] = [];
  pushStringArg(args, "--config", pickValue(paramsRecord, "config"), deps);
  pushStringArg(args, "--setup-root", pickValue(paramsRecord, "setupRoot", "setup_root"), deps);
  pushStringArg(args, "--env-file", pickValue(paramsRecord, "envFile", "env_file"), deps);
  return args;
}

function needsRuntimeDefaults(paramsRecord: Record<string, unknown>, deps: OnboardingToolDeps): boolean {
  return !deps.readString(pickValue(paramsRecord, "mode"))
    || !deps.readString(pickValue(paramsRecord, "transport"))
    || !deps.readString(pickValue(paramsRecord, "sseUrl", "sse_url"));
}

async function mergeCurrentSetupDefaults(
  layout: PluginRuntimeLayout,
  paramsRecord: Record<string, unknown>,
  deps: OnboardingToolDeps,
  runCommand: (
    layout: PluginRuntimeLayout,
    commandArgs: string[],
    childEnv?: Record<string, string>,
  ) => Promise<LauncherResult>,
): Promise<Record<string, unknown>> {
  if (!needsRuntimeDefaults(paramsRecord, deps)) {
    return paramsRecord;
  }
  const statusResult = await executeJsonCommand(
    layout,
    ["bootstrap-status", ...buildCommonArgs(paramsRecord, deps), "--json"],
    { ...deps, runLauncherCommand: runCommand },
  );
  const setup =
    statusResult.payload?.setup && typeof statusResult.payload.setup === "object"
      ? (statusResult.payload.setup as JsonRecord)
      : null;
  if (!setup) {
    return paramsRecord;
  }
  const merged: Record<string, unknown> = { ...paramsRecord };
  if (!deps.readString(pickValue(paramsRecord, "mode"))) {
    const inheritedMode = deps.readString(pickValue(setup, "mode"));
    if (inheritedMode) {
      merged.mode = inheritedMode;
    }
  }
  if (!deps.readString(pickValue(paramsRecord, "transport"))) {
    const inheritedTransport = deps.readString(pickValue(setup, "transport"));
    if (inheritedTransport) {
      merged.transport = inheritedTransport;
    }
  }
  const effectiveTransport =
    deps.readString(pickValue(merged, "transport"))
    || deps.readString(pickValue(setup, "transport"));
  if (!deps.readString(pickValue(paramsRecord, "sseUrl", "sse_url"))) {
    const inheritedSseUrl = deps.readString(pickValue(setup, "sseUrl", "sse_url"));
    if (effectiveTransport === "sse" && inheritedSseUrl) {
      merged.sseUrl = inheritedSseUrl;
    }
  }
  return merged;
}

function collectSecretEnv(
  paramsRecord: Record<string, unknown>,
  deps: OnboardingToolDeps,
): Record<string, string> {
  const env: Record<string, string> = {};
  const secretMappings: Array<{ envKey: string; paramKeys: string[] }> = [
    { envKey: "RETRIEVAL_EMBEDDING_API_KEY", paramKeys: ["embeddingApiKey", "embedding_api_key"] },
    { envKey: "RETRIEVAL_RERANKER_API_KEY", paramKeys: ["rerankerApiKey", "reranker_api_key"] },
    { envKey: "LLM_API_KEY", paramKeys: ["llmApiKey", "llm_api_key"] },
    { envKey: "WRITE_GUARD_LLM_API_KEY", paramKeys: ["writeGuardLlmApiKey", "write_guard_llm_api_key"] },
    { envKey: "COMPACT_GIST_LLM_API_KEY", paramKeys: ["compactGistLlmApiKey", "compact_gist_llm_api_key"] },
    { envKey: "MCP_API_KEY", paramKeys: ["mcpApiKey", "mcp_api_key"] },
  ];
  for (const mapping of secretMappings) {
    const value = deps.readString(pickValue(paramsRecord, ...mapping.paramKeys));
    if (value) {
      env[mapping.envKey] = value;
    }
  }
  return env;
}

function buildProviderProbeArgs(paramsRecord: Record<string, unknown>, deps: OnboardingToolDeps): { args: string[]; env: Record<string, string> } {
  const args = ["provider-probe", ...buildCommonArgs(paramsRecord, deps), "--json"];
  pushStringArg(args, "--mode", pickValue(paramsRecord, "mode"), deps);
  pushStringArg(args, "--profile", pickValue(paramsRecord, "profile"), deps);
  pushStringArg(args, "--transport", pickValue(paramsRecord, "transport"), deps);
  pushStringArg(args, "--sse-url", pickValue(paramsRecord, "sseUrl", "sse_url"), deps);
  pushBooleanFlag(
    args,
    "--allow-insecure-local",
    pickValue(paramsRecord, "allowInsecureLocal", "allow_insecure_local"),
    deps,
  );
  pushStringArg(args, "--embedding-api-base", pickValue(paramsRecord, "embeddingApiBase", "embedding_api_base"), deps);
  pushStringArg(args, "--embedding-model", pickValue(paramsRecord, "embeddingModel", "embedding_model"), deps);
  pushStringArg(args, "--embedding-dim", pickValue(paramsRecord, "embeddingDim", "embedding_dim"), deps);
  pushStringArg(args, "--reranker-api-base", pickValue(paramsRecord, "rerankerApiBase", "reranker_api_base"), deps);
  pushStringArg(args, "--reranker-model", pickValue(paramsRecord, "rerankerModel", "reranker_model"), deps);
  pushStringArg(args, "--llm-api-base", pickValue(paramsRecord, "llmApiBase", "llm_api_base"), deps);
  pushStringArg(args, "--llm-model", pickValue(paramsRecord, "llmModel", "llm_model"), deps);
  pushStringArg(
    args,
    "--write-guard-llm-api-base",
    pickValue(paramsRecord, "writeGuardLlmApiBase", "write_guard_llm_api_base"),
    deps,
  );
  pushStringArg(
    args,
    "--write-guard-llm-model",
    pickValue(paramsRecord, "writeGuardLlmModel", "write_guard_llm_model"),
    deps,
  );
  pushStringArg(
    args,
    "--compact-gist-llm-api-base",
    pickValue(paramsRecord, "compactGistLlmApiBase", "compact_gist_llm_api_base"),
    deps,
  );
  pushStringArg(
    args,
    "--compact-gist-llm-model",
    pickValue(paramsRecord, "compactGistLlmModel", "compact_gist_llm_model"),
    deps,
  );
  const env = collectSecretEnv(paramsRecord, deps);
  return { args, env };
}

function buildApplyArgs(paramsRecord: Record<string, unknown>, deps: OnboardingToolDeps): { args: string[]; env: Record<string, string> } {
  const args = ["setup", ...buildCommonArgs(paramsRecord, deps), "--json"];
  pushStringArg(args, "--mode", pickValue(paramsRecord, "mode"), deps);
  pushStringArg(args, "--profile", pickValue(paramsRecord, "profile"), deps);
  pushStringArg(args, "--transport", pickValue(paramsRecord, "transport"), deps);
  pushBooleanFlag(args, "--validate", pickValue(paramsRecord, "validate"), deps);
  pushBooleanFlag(args, "--strict-profile", pickValue(paramsRecord, "strictProfile", "strict_profile"), deps);
  pushBooleanFlag(args, "--reconfigure", pickValue(paramsRecord, "reconfigure"), deps);
  pushBooleanFlag(args, "--no-activate", pickValue(paramsRecord, "noActivate", "no_activate"), deps);
  pushStringArg(args, "--sse-url", pickValue(paramsRecord, "sseUrl", "sse_url"), deps);
  pushBooleanFlag(
    args,
    "--allow-insecure-local",
    pickValue(paramsRecord, "allowInsecureLocal", "allow_insecure_local"),
    deps,
  );
  pushBooleanFlag(
    args,
    "--allow-generate-remote-api-key",
    pickValue(paramsRecord, "allowGenerateRemoteApiKey", "allow_generate_remote_api_key"),
    deps,
  );
  pushStringArg(args, "--embedding-api-base", pickValue(paramsRecord, "embeddingApiBase", "embedding_api_base"), deps);
  pushStringArg(args, "--embedding-model", pickValue(paramsRecord, "embeddingModel", "embedding_model"), deps);
  pushStringArg(args, "--embedding-dim", pickValue(paramsRecord, "embeddingDim", "embedding_dim"), deps);
  pushStringArg(args, "--reranker-api-base", pickValue(paramsRecord, "rerankerApiBase", "reranker_api_base"), deps);
  pushStringArg(args, "--reranker-model", pickValue(paramsRecord, "rerankerModel", "reranker_model"), deps);
  pushStringArg(args, "--llm-api-base", pickValue(paramsRecord, "llmApiBase", "llm_api_base"), deps);
  pushStringArg(args, "--llm-model", pickValue(paramsRecord, "llmModel", "llm_model"), deps);
  pushStringArg(
    args,
    "--write-guard-llm-api-base",
    pickValue(paramsRecord, "writeGuardLlmApiBase", "write_guard_llm_api_base"),
    deps,
  );
  pushStringArg(
    args,
    "--write-guard-llm-model",
    pickValue(paramsRecord, "writeGuardLlmModel", "write_guard_llm_model"),
    deps,
  );
  pushStringArg(
    args,
    "--compact-gist-llm-api-base",
    pickValue(paramsRecord, "compactGistLlmApiBase", "compact_gist_llm_api_base"),
    deps,
  );
  pushStringArg(
    args,
    "--compact-gist-llm-model",
    pickValue(paramsRecord, "compactGistLlmModel", "compact_gist_llm_model"),
    deps,
  );
  const env = collectSecretEnv(paramsRecord, deps);
  return { args, env };
}

async function executeJsonCommand(
  layout: PluginRuntimeLayout,
  commandArgs: string[],
  deps: OnboardingToolDeps,
  childEnv?: Record<string, string>,
): Promise<{ ok: boolean; payload?: JsonRecord; error?: string; stdout?: string; stderr?: string }> {
  try {
    const result = await (deps.runLauncherCommand ?? runLauncherCommand)(layout, commandArgs, childEnv);
    if (result.payload) {
      const validation =
        result.payload.validation && typeof result.payload.validation === "object"
          ? (result.payload.validation as JsonRecord)
          : {};
      const payloadOk = result.payload.ok !== false && validation.ok !== false;
      const ok = result.exitCode === 0 && payloadOk;
      return { ok, payload: result.payload, stdout: result.stdout, stderr: result.stderr };
    }
    return {
      ok: false,
      error:
        result.stderr.trim() ||
        result.stdout.trim() ||
        `Command failed with exit code ${result.exitCode}.`,
      stdout: result.stdout,
      stderr: result.stderr,
    };
  } catch (error) {
    return { ok: false, error: deps.formatError(error) };
  }
}

export function createOnboardingTools(options: {
  deps: OnboardingToolDeps;
  layout: PluginRuntimeLayout;
  logger?: TraceLogger;
  context?: OpenClawPluginToolContext;
}): AnyAgentTool[] {
  const { deps, layout } = options;
  const runCommand = deps.runLauncherCommand ?? runLauncherCommand;

  const statusTool: AnyAgentTool = {
    label: "Memory Palace Onboarding Status",
    name: ONBOARDING_STATUS_TOOL,
    description:
      "Inspect whether Memory Palace bootstrap is already wired into OpenClaw and return the current onboarding status plus plain-language setup guidance.",
    parameters: memoryOnboardingStatusSchema,
    execute: async (_toolCallId: string, params: unknown) => {
      const paramsRecord = (params as Record<string, unknown>) || {};
      const locale = deps.readString(pickValue(paramsRecord, "locale")) || undefined;
      const guide = buildGuide(locale);
      const result = await executeJsonCommand(
        layout,
        ["bootstrap-status", ...buildCommonArgs(paramsRecord, deps), "--json"],
        { ...deps, runLauncherCommand: runCommand },
      );
      if (!result.ok || !result.payload) {
        return deps.jsonResult({
          ok: false,
          error: result.error || localizedText(locale, "读取 onboarding 状态失败。", "Failed to load onboarding status."),
          guide,
        });
      }
      return deps.jsonResult({
        ok: true,
        narrative: buildStatusNarrative(result.payload, locale),
        status: result.payload,
        guide,
      });
    },
  };

  const probeTool: AnyAgentTool = {
    label: "Memory Palace Onboarding Provider Probe",
    name: ONBOARDING_PROBE_TOOL,
    description:
      "Probe Profile C/D provider readiness from a chat flow, including embedding dimension detection and fallback-aware guidance.",
    parameters: memoryOnboardingProviderProbeSchema,
    execute: async (_toolCallId: string, params: unknown) => {
      const rawParamsRecord = (params as Record<string, unknown>) || {};
      const paramsRecord = await mergeCurrentSetupDefaults(layout, rawParamsRecord, deps, runCommand);
      const locale = deps.readString(pickValue(paramsRecord, "locale")) || undefined;
      const guide = buildGuide(locale);
      const probeCmd = buildProviderProbeArgs(paramsRecord, deps);
      const result = await executeJsonCommand(
        layout,
        probeCmd.args,
        { ...deps, runLauncherCommand: runCommand },
        probeCmd.env,
      );
      if (!result.ok || !result.payload) {
        return deps.jsonResult({
          ok: false,
          error: result.error || localizedText(locale, "Provider probe 失败。", "Provider probe failed."),
          guide,
        });
      }
      return deps.jsonResult({
        ok: true,
        narrative: buildProbeNarrative(result.payload, locale),
        providerProbe: result.payload,
        guide,
      });
    },
  };

  const applyTool: AnyAgentTool = {
    label: "Memory Palace Onboarding Apply",
    name: ONBOARDING_APPLY_TOOL,
    description:
      "Run Memory Palace setup directly from a chat-guided onboarding conversation and return the effective profile, fallback outcome, and next steps.",
    parameters: memoryOnboardingApplySchema,
    execute: async (_toolCallId: string, params: unknown) => {
      const rawParamsRecord = (params as Record<string, unknown>) || {};
      const paramsRecord = await mergeCurrentSetupDefaults(layout, rawParamsRecord, deps, runCommand);
      const locale = deps.readString(pickValue(paramsRecord, "locale")) || undefined;
      const guide = buildGuide(locale);
      const applyCmd = buildApplyArgs(paramsRecord, deps);
      const result = await executeJsonCommand(
        layout,
        applyCmd.args,
        { ...deps, runLauncherCommand: runCommand },
        applyCmd.env,
      );
      if (!result.payload) {
        return deps.jsonResult({
          ok: false,
          error: result.error || localizedText(locale, "Setup apply 失败。", "Setup apply failed."),
          guide,
        });
      }
      if (!result.ok) {
        return deps.jsonResult({
          ok: false,
          error: result.error || localizedText(locale, "Setup apply 失败。", "Setup apply failed."),
          narrative: buildApplyNarrative(result.payload, locale),
          apply: result.payload,
          guide,
        });
      }
      return deps.jsonResult({
        ok: true,
        narrative: buildApplyNarrative(result.payload, locale),
        apply: result.payload,
        guide,
      });
    },
  };

  return [statusTool, probeTool, applyTool];
}

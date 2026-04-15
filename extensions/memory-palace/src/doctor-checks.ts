import { existsSync } from "node:fs";
import path from "node:path";
import {
  buildDoctorActions as buildDoctorActionsModule,
  buildLegacyDoctorActions as buildLegacyDoctorActionsModule,
  collectPhaseRuntimeChecks as collectPhaseRuntimeChecksModule,
  collectStaticHostConfigChecks as collectStaticHostConfigChecksModule,
} from "./static-diagnostics.js";
import { getConfiguredRuntimePythonPath } from "./runtime-layout.js";
import type {
  DiagnosticCheck,
  HostPlatform,
  PluginConfig,
  PluginRuntimeSnapshot,
} from "./types.js";
import { readString } from "./utils.js";

export type LegacyVerifyCheck = {
  name: string;
  status: "PASS" | "WARN" | "FAIL";
  summary: string;
  detail?: unknown;
};

export type DoctorCheckDeps = {
  bundledSkillRoot: string;
  configPath?: string;
  currentHostPlatform: HostPlatform;
  defaultStdioWrapper: string;
  defaultWindowsMcpWrapper: string;
  getTransportFallbackOrder: (config: PluginConfig) => string[];
  isPackagedPluginLayout: boolean;
  packagedBackendRoot: string;
  parseConfigFile: (configPath: string) => unknown;
  pathExists?: (inputPath: string) => boolean;
  pluginExtensionRoot: string;
  pluginProjectRoot: string;
  snapshotPluginRuntimeState: (config: PluginConfig) => PluginRuntimeSnapshot;
  usesDefaultStdioWrapper: (config: PluginConfig) => boolean;
};

export function collectStaticDoctorChecks(
  config: PluginConfig,
  deps: DoctorCheckDeps,
): DiagnosticCheck[] {
  const pathExists = deps.pathExists ?? existsSync;
  const checks: DiagnosticCheck[] = [
    ...collectStaticHostConfigChecksModule(config, {
      configPath: readString(deps.configPath),
      currentHostPlatform: deps.currentHostPlatform,
      parseConfigFile: deps.parseConfigFile,
      pathExists,
      pluginExtensionRoot: deps.pluginExtensionRoot,
    }),
    ...collectPhaseRuntimeChecksModule(config, deps.snapshotPluginRuntimeState(config)),
  ];
  const fallbackOrder = deps.getTransportFallbackOrder(config);
  if (fallbackOrder.length === 0) {
    checks.push({
      id: "transport-config",
      status: "fail",
      message: "No usable transport is configured.",
      action: "Configure stdio or sse in plugins.entries.memory-palace.config.",
    });
    return checks;
  }

  checks.push({
    id: "transport-order",
    status: "pass",
    message: `Configured transport order: ${fallbackOrder.join(" -> ")}.`,
  });
  checks.push({
    id: "stable-entry",
    status: "pass",
    message:
      "Stable user entry is `openclaw memory-palace ...`; host-owned `openclaw memory ...` remains a separate command surface.",
  });

  const bundledSkillPaths = [
    path.resolve(
      deps.bundledSkillRoot,
      "memory-palace-openclaw",
      "SKILL.md",
    ),
    path.resolve(
      deps.bundledSkillRoot,
      "memory-palace-openclaw-onboarding",
      "SKILL.md",
    ),
  ];
  const bundledSkillPresent = bundledSkillPaths.every((skillPath) => pathExists(skillPath));
  checks.push({
    id: "bundled-skill",
    status: bundledSkillPresent ? "pass" : "warn",
    message: bundledSkillPresent
      ? "Plugin-bundled OpenClaw skills are present."
      : "One or more plugin-bundled OpenClaw skill directories are missing.",
    action: bundledSkillPresent
      ? undefined
      : "Repack/reinstall the plugin so both bundled OpenClaw skill directories are shipped with the plugin.",
  });
  checks.push({
    id: "visual-auto-harvest",
    status: config.visualMemory.enabled ? "pass" : "warn",
    message: config.visualMemory.enabled
      ? "Visual auto-harvest hooks are enabled (message:preprocessed / before_prompt_build / agent_end)."
      : "Visual auto-harvest hooks are disabled by config.",
    action: config.visualMemory.enabled
      ? undefined
      : "Enable plugins.entries.memory-palace.config.visualMemory.enabled or use `memory_store_visual` manually.",
  });
  checks.push({
    id: "profile-memory",
    status: config.profileMemory.enabled
      ? config.profileMemory.injectBeforeAgentStart
        ? "pass"
        : "warn"
      : "warn",
    message: config.profileMemory.enabled
      ? `Profile block is configured for ${config.profileMemory.blocks.join(", ")} with max ${config.profileMemory.maxCharsPerBlock} chars per block.`
      : "Profile block is disabled by config.",
    action: config.profileMemory.enabled
      ? config.profileMemory.injectBeforeAgentStart
        ? undefined
        : "Enable plugins.entries.memory-palace.config.profileMemory.injectBeforeAgentStart to prepend profile context before recall."
      : "Enable plugins.entries.memory-palace.config.profileMemory.enabled to persist stable identity / preferences / workflow blocks.",
  });
  checks.push({
    id: "auto-recall",
    status: config.autoRecall.enabled ? "pass" : "warn",
    message: config.autoRecall.enabled
      ? "Automatic durable recall before agent start is enabled."
      : "Automatic durable recall before agent start is disabled by config.",
    action: config.autoRecall.enabled
      ? undefined
      : "Enable plugins.entries.memory-palace.config.autoRecall.enabled to restore the default recall path.",
  });
  checks.push({
    id: "auto-capture",
    status: config.autoCapture.enabled ? "pass" : "warn",
    message: config.autoCapture.enabled
      ? "Automatic durable capture after successful turns is enabled."
      : "Automatic durable capture after successful turns is disabled by config.",
    action: config.autoCapture.enabled
      ? undefined
      : "Enable plugins.entries.memory-palace.config.autoCapture.enabled to restore the default capture path.",
  });
  checks.push({
    id: "host-bridge",
    status: config.hostBridge.enabled ? "pass" : "warn",
    message: config.hostBridge.enabled
      ? "Host bridge fallback is enabled for USER.md / MEMORY.md / memory/*.md import candidates."
      : "Host bridge fallback is disabled by config.",
    action: config.hostBridge.enabled
      ? undefined
      : "Enable plugins.entries.memory-palace.config.hostBridge.enabled to bridge host workspace facts into plugin-owned memory on recall misses.",
  });
  checks.push({
    id: "assistant-derived",
    status: config.capturePipeline.captureAssistantDerived ? "pass" : "warn",
    message: config.capturePipeline.captureAssistantDerived
      ? `Assistant-derived workflow candidates are enabled (${config.capturePipeline.mode}, max ${config.capturePipeline.maxAssistantDerivedPerRun} per run, profile ${config.capturePipeline.effectiveProfile ?? "unknown"}).`
      : "Assistant-derived workflow candidates are disabled by config.",
    action: config.capturePipeline.captureAssistantDerived
      ? undefined
      : "Enable plugins.entries.memory-palace.config.capturePipeline.captureAssistantDerived to persist multi-turn workflow candidates.",
  });
  checks.push({
    id: "transport-retry",
    status: "pass",
    message:
      `Configured retry policy: ${config.connection.connectRetries} reconnect ` +
      `${config.connection.connectRetries === 1 ? "retry" : "retries"} / ` +
      `base backoff ${config.connection.connectBackoffMs}ms / ` +
      `request retries ${config.connection.requestRetries}.`,
  });

  const stdioRelevant = config.transport === "stdio" || config.transport === "auto";
  if (stdioRelevant) {
    if (!config.stdio?.command) {
      checks.push({
        id: "stdio-command",
        status: config.transport === "stdio" ? "fail" : "warn",
        message: "Stdio transport is missing a command.",
        action: "Set stdio.command or switch to sse transport.",
      });
    } else {
      checks.push({
        id: "stdio-command",
        status: "pass",
        message: `Stdio command is configured: ${config.stdio.command}.`,
      });
    }

    if (deps.usesDefaultStdioWrapper(config)) {
      const wrapperPath =
        deps.currentHostPlatform === "windows"
          ? deps.defaultWindowsMcpWrapper
          : deps.defaultStdioWrapper;
      const wrapperExists = pathExists(wrapperPath);
      checks.push({
        id: "stdio-wrapper",
        status: wrapperExists ? "pass" : config.transport === "stdio" ? "fail" : "warn",
        message: wrapperExists
          ? "Default stdio launcher is present."
          : "Default stdio launcher is missing.",
        action: wrapperExists
          ? undefined
          : `Ensure ${wrapperPath} exists or override stdio.command.`,
      });

      const runtimePythonPath = getConfiguredRuntimePythonPath(config.stdio?.env, {
        currentHostPlatform: deps.currentHostPlatform,
        isPackagedPluginLayout: deps.isPackagedPluginLayout,
        packagedBackendRoot: deps.packagedBackendRoot,
        pluginProjectRoot: deps.pluginProjectRoot,
      });
      const backendPythonExists = pathExists(runtimePythonPath);
      checks.push({
        id: "stdio-backend-python",
        status:
          backendPythonExists ? "pass" : config.transport === "stdio" ? "fail" : "warn",
        message: backendPythonExists
          ? "Configured runtime python for the stdio wrapper is present."
          : "Configured runtime python for the stdio wrapper is missing.",
        action: backendPythonExists
          ? undefined
          : "Run setup again to bootstrap the dedicated runtime venv, or override OPENCLAW_MEMORY_PALACE_RUNTIME_PYTHON.",
      });
    }
  }

  const sseRelevant = config.transport === "sse" || config.transport === "auto";
  if (sseRelevant) {
    const sseUrl = readString(config.sse?.url);
    checks.push({
      id: "sse-url",
      status: sseUrl ? "pass" : config.transport === "sse" ? "fail" : "warn",
      message: sseUrl
        ? `SSE endpoint is configured: ${sseUrl}.`
        : "SSE endpoint is not configured.",
      action: sseUrl ? undefined : "Set sse.url or switch to stdio transport.",
    });
  }

  return checks;
}

export function collectHostConfigChecks(
  config: PluginConfig,
  deps: DoctorCheckDeps,
): DiagnosticCheck[] {
  return collectStaticHostConfigChecksModule(config, {
    configPath: readString(deps.configPath),
    currentHostPlatform: deps.currentHostPlatform,
    parseConfigFile: deps.parseConfigFile,
    pathExists: deps.pathExists ?? existsSync,
    pluginExtensionRoot: deps.pluginExtensionRoot,
  });
}

export function collectLegacyHostConfigChecks(
  config: PluginConfig,
  deps: DoctorCheckDeps,
): LegacyVerifyCheck[] {
  return collectHostConfigChecks(config, deps).map((entry) => ({
    name: entry.id.replace(/-/g, "_"),
    status: entry.status.toUpperCase() as "PASS" | "WARN" | "FAIL",
    summary: entry.message,
    ...(entry.details !== undefined ? { detail: entry.details } : {}),
  }));
}

export function buildDoctorActions(
  config: PluginConfig,
  report: {
    checks: Array<{
      id?: string;
      name?: string;
      status: string;
      action?: string;
    }>;
  },
  deps: Pick<
    DoctorCheckDeps,
    "currentHostPlatform" | "defaultStdioWrapper" | "defaultWindowsMcpWrapper"
  >,
): string[] {
  return buildDoctorActionsModule(config, report, {
    currentHostPlatform: deps.currentHostPlatform,
    defaultStdioWrapper: deps.defaultStdioWrapper,
    defaultWindowsMcpWrapper: deps.defaultWindowsMcpWrapper,
  });
}

export function buildLegacyDoctorActions(
  report: {
    checks: Array<{
      name: string;
      status: "PASS" | "WARN" | "FAIL";
      summary: string;
    }>;
  },
): string[] {
  return buildLegacyDoctorActionsModule(report);
}

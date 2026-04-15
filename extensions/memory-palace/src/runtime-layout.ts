import { existsSync } from "node:fs";
import path from "node:path";
import type { DefaultStdioLaunch, HostPlatform, PluginRuntimeLayout } from "./types.js";
import { readString } from "./utils.js";

export type ResolveDefaultStdioLaunchFactoryOptions = {
  currentHostPlatform: HostPlatform;
  pluginProjectRoot: string;
  packagedBackendRoot: string;
  isPackagedPluginLayout: boolean;
  defaultStdioWrapper: string;
  pathExists?: (inputPath: string) => boolean;
};

export function resolvePluginRuntimeLayout(
  moduleDir: string,
  pathExists: (inputPath: string) => boolean = existsSync,
): PluginRuntimeLayout {
  const normalizedModuleDir = path.resolve(moduleDir);
  const repoExtensionRootCandidate = path.resolve(normalizedModuleDir, "extensions", "memory-palace");
  const pluginExtensionRoot =
    path.basename(normalizedModuleDir) === "dist" &&
    pathExists(path.resolve(normalizedModuleDir, "..", "openclaw.plugin.json"))
      ? path.resolve(normalizedModuleDir, "..")
      : pathExists(path.resolve(normalizedModuleDir, "openclaw.plugin.json"))
        ? normalizedModuleDir
        : pathExists(path.resolve(repoExtensionRootCandidate, "openclaw.plugin.json"))
          ? repoExtensionRootCandidate
          : normalizedModuleDir;
  const repoProjectRootCandidate = path.resolve(pluginExtensionRoot, "..", "..");
  const isRepoExtensionLayout =
    path.basename(path.dirname(pluginExtensionRoot)) === "extensions" &&
    pathExists(path.resolve(repoProjectRootCandidate, "scripts", "run_memory_palace_mcp_stdio.sh")) &&
    pathExists(path.resolve(repoProjectRootCandidate, "backend"));
  const packagedScriptsRoot = path.resolve(pluginExtensionRoot, "release", "scripts");
  const packagedBackendRoot = path.resolve(pluginExtensionRoot, "release", "backend");
  const isPackagedPluginLayout = !isRepoExtensionLayout && pathExists(packagedScriptsRoot);
  const pluginProjectRoot = isPackagedPluginLayout
    ? pluginExtensionRoot
    : repoProjectRootCandidate;
  const defaultStdioWrapper = isPackagedPluginLayout
    ? path.resolve(packagedScriptsRoot, "run_memory_palace_mcp_stdio.sh")
    : path.resolve(pluginProjectRoot, "scripts", "run_memory_palace_mcp_stdio.sh");
  const defaultTransportDiagnosticsPath = path.resolve(
    pluginProjectRoot,
    ".tmp",
    "observability",
    "openclaw_transport_diagnostics.json",
  );
  const bundledSkillRoot = path.resolve(pluginExtensionRoot, "skills");
  return {
    pluginExtensionRoot,
    isRepoExtensionLayout,
    packagedScriptsRoot,
    packagedBackendRoot,
    isPackagedPluginLayout,
    pluginProjectRoot,
    defaultStdioWrapper,
    defaultTransportDiagnosticsPath,
    bundledSkillRoot,
  };
}

export function shellQuote(value: string): string {
  return `'${value.replace(/'/g, `'"'"'`)}'`;
}

export function getConfiguredRuntimePythonPath(
  runtimeEnv: Record<string, string> | undefined,
  options: Pick<
    ResolveDefaultStdioLaunchFactoryOptions,
    "currentHostPlatform" | "isPackagedPluginLayout" | "packagedBackendRoot" | "pluginProjectRoot"
  >,
): string {
  const pythonSegments =
    options.currentHostPlatform === "windows" ? ["Scripts", "python.exe"] : ["bin", "python"];
  return (
    readString(runtimeEnv?.OPENCLAW_MEMORY_PALACE_RUNTIME_PYTHON) ??
    readString(process.env.OPENCLAW_MEMORY_PALACE_RUNTIME_PYTHON) ??
    (options.isPackagedPluginLayout && existsSync(options.packagedBackendRoot)
      ? path.resolve(options.packagedBackendRoot, ".venv", ...pythonSegments)
      : path.resolve(options.pluginProjectRoot, "backend", ".venv", ...pythonSegments))
  );
}

export function createResolveDefaultStdioLaunch(
  options: ResolveDefaultStdioLaunchFactoryOptions,
): (
  runtimeEnv: Record<string, string> | undefined,
  hostPlatform?: HostPlatform,
) => DefaultStdioLaunch {
  const defaultPythonMcpWrapper = options.isPackagedPluginLayout
    ? path.resolve(options.packagedBackendRoot, "mcp_wrapper.py")
    : path.resolve(options.pluginProjectRoot, "backend", "mcp_wrapper.py");
  const pathExists = options.pathExists ?? existsSync;

  return (
    runtimeEnv: Record<string, string> | undefined,
    hostPlatform: HostPlatform = options.currentHostPlatform,
  ): DefaultStdioLaunch => {
    if (hostPlatform === "windows") {
      return {
        command: getConfiguredRuntimePythonPath(runtimeEnv, {
          currentHostPlatform: hostPlatform,
          isPackagedPluginLayout: options.isPackagedPluginLayout,
          packagedBackendRoot: options.packagedBackendRoot,
          pluginProjectRoot: options.pluginProjectRoot,
        }),
        args: [defaultPythonMcpWrapper],
        cwd: path.dirname(defaultPythonMcpWrapper),
      };
    }
    const shellEnv = readString(process.env.SHELL);
    const zshCandidates = [
      shellEnv && /zsh$/i.test(path.basename(shellEnv)) ? shellEnv : undefined,
      "/bin/zsh",
      "/usr/bin/zsh",
    ].filter((value, index, items): value is string => Boolean(value) && items.indexOf(value) === index);
    const bashCandidates = [
      shellEnv && /bash$/i.test(path.basename(shellEnv)) ? shellEnv : undefined,
      "/bin/bash",
      "/usr/bin/bash",
    ].filter((value, index, items): value is string => Boolean(value) && items.indexOf(value) === index);
    const shCandidates = [
      shellEnv && /^sh$/i.test(path.basename(shellEnv)) ? shellEnv : undefined,
      "/bin/sh",
      "/usr/bin/sh",
    ].filter((value, index, items): value is string => Boolean(value) && items.indexOf(value) === index);
    const resolvedBash = bashCandidates.find((candidate) => pathExists(candidate));
    const resolvedZsh = zshCandidates.find((candidate) => pathExists(candidate));
    const resolvedSh = shCandidates.find((candidate) => pathExists(candidate));
    if (resolvedZsh && resolvedBash) {
      const launchCommand = `${shellQuote(resolvedBash)} ${shellQuote(options.defaultStdioWrapper)}`;
      return {
        command: resolvedZsh,
        args: ["-lc", launchCommand],
        cwd: options.pluginProjectRoot,
      };
    }
    if (resolvedBash) {
      return {
        command: resolvedBash,
        args: [options.defaultStdioWrapper],
        cwd: options.pluginProjectRoot,
      };
    }
    if (resolvedSh) {
      return {
        command: resolvedSh,
        args: [options.defaultStdioWrapper],
        cwd: options.pluginProjectRoot,
      };
    }
    return {
      command: getConfiguredRuntimePythonPath(runtimeEnv, {
        currentHostPlatform: hostPlatform,
        isPackagedPluginLayout: options.isPackagedPluginLayout,
        packagedBackendRoot: options.packagedBackendRoot,
        pluginProjectRoot: options.pluginProjectRoot,
      }),
      args: [defaultPythonMcpWrapper],
      cwd: path.dirname(defaultPythonMcpWrapper),
    };
  };
}

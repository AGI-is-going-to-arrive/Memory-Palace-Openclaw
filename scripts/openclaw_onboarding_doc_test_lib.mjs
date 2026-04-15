import { existsSync } from "node:fs";
import { cp, mkdir, mkdtemp, readFile, readdir, rm, stat, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { spawn } from "node:child_process";
import { fileURLToPath, pathToFileURL } from "node:url";

const scriptDir = path.dirname(fileURLToPath(import.meta.url));
export const repoRoot = path.resolve(scriptDir, "..");
export const docZhPath = path.join(
  repoRoot,
  "docs",
  "openclaw-doc",
  "18-CONVERSATIONAL_ONBOARDING.md",
);
export const docEnPath = path.join(
  repoRoot,
  "docs",
  "openclaw-doc",
  "18-CONVERSATIONAL_ONBOARDING.en.md",
);
export const docZhRef = path.relative(repoRoot, docZhPath).split(path.sep).join("/");
export const docEnRef = path.relative(repoRoot, docEnPath).split(path.sep).join("/");
export const frontendRoot = path.join(repoRoot, "frontend");
export const tempRoot = String(process.env.OPENCLAW_ONBOARDING_TEMP_ROOT || '').trim()
  || path.join(process.env.TMPDIR || process.env.TEMP || process.env.TMP || os.tmpdir(), "openclaw-onboarding-doc-chat-flow");

export const OPENCLAW_BIN = process.env.OPENCLAW_BIN || "openclaw";
const WINDOWS_POWERSHELL_BIN = process.env.SystemRoot
  ? path.join(process.env.SystemRoot, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
  : "powershell.exe";
function resolveScenarioPythonBin() {
  const explicit = String(process.env.PYTHON || process.env.PYTHON3 || "").trim();
  if (explicit && (process.platform === "win32" || path.isAbsolute(explicit))) {
    return explicit;
  }
  const preferredCandidates = [
    path.join(repoRoot, ".venv", "Scripts", "python.exe"),
    path.join(repoRoot, "backend", ".venv", "Scripts", "python.exe"),
    path.join(repoRoot, ".venv", "bin", "python"),
    path.join(repoRoot, "backend", ".venv", "bin", "python"),
    "/opt/homebrew/bin/python3",
  ];
  for (const candidate of preferredCandidates) {
    if (candidate && existsSync(candidate)) {
      return candidate;
    }
  }
  if (process.platform === "win32") {
    return "py";
  }
  return "python3";
}
export const PYTHON_BIN = resolveScenarioPythonBin();
const CONTROL_TOKEN = "status-probe-local-only";
const ONBOARDING_CHAT_MODEL_OVERRIDE = String(
  process.env.OPENCLAW_ONBOARDING_CHAT_MODEL || "",
).trim();
const ONBOARDING_CHAT_MODEL_ALIAS = String(
  process.env.OPENCLAW_ONBOARDING_CHAT_MODEL_ALIAS || "",
).trim();
const ONBOARDING_CHAT_PROVIDER_OVERRIDE = String(
  process.env.OPENCLAW_ONBOARDING_CHAT_PROVIDER || "",
).trim();
const ONBOARDING_CHAT_BASE_URL_OVERRIDE = String(
  process.env.OPENCLAW_ONBOARDING_CHAT_BASE_URL || "",
).trim();
const SCENARIO_SETUP_TIMEOUT_MS = Number.parseInt(
  process.env.OPENCLAW_SCENARIO_SETUP_TIMEOUT_MS || "420000",
  10,
);
const sharedPipCacheDir = path.join(tempRoot, ".pip-cache");

function expandHome(value) {
  if (!(value.startsWith("~/") || value.startsWith("~\\"))) {
    return value;
  }
  const homeDir = process.env.USERPROFILE || process.env.HOME || os.homedir();
  return path.join(homeDir, value.slice(2));
}

async function readConfigSnapshot(configPath) {
  const raw = await readFile(configPath, "utf8");
  const payload = JSON.parse(raw);
  if (!payload.models) {
    throw new Error(`Main OpenClaw config at ${configPath} has no models block`);
  }
  return {
    configPath,
    payload,
  };
}

function buildSyntheticConfigSnapshot() {
  const baseUrl = normalizeChatBaseUrl(ONBOARDING_CHAT_BASE_URL_OVERRIDE);
  const providerModelId = normalizeProviderModelId(ONBOARDING_CHAT_MODEL_OVERRIDE);
  if (!baseUrl || !providerModelId) {
    return null;
  }
  const providerId = ONBOARDING_CHAT_PROVIDER_OVERRIDE || "onboarding-openai";
  const primaryModel = `${providerId}/${providerModelId}`;
  return {
    configPath: "<synthetic:onboarding-env>",
    payload: {
      models: {
        mode: "replace",
        providers: {
          [providerId]: {
            baseUrl,
            api: "openai-completions",
            models: [
              {
                id: providerModelId,
                name: providerModelId,
                contextWindow: 256000,
              },
            ],
          },
        },
      },
      agents: {
        defaults: {
          model: {
            primary: primaryModel,
          },
        },
      },
    },
  };
}

export async function runCommand(
  command,
  args,
  {
    cwd = repoRoot,
    env = process.env,
    timeoutMs = 120_000,
    allowFailure = false,
  } = {},
) {
  const quotePowerShellArg = (value) => `'${String(value || "").replaceAll("'", "''")}'`;
  const normalizeCommandInvocation = (program, argv) => {
    if (process.platform !== "win32" || program !== OPENCLAW_BIN) {
      return { program, argv };
    }
    const commandLine = ["&", quotePowerShellArg(program), ...argv.map(quotePowerShellArg)].join(" ");
    return {
      program: WINDOWS_POWERSHELL_BIN,
      argv: ["-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", commandLine],
    };
  };
  const invocation = normalizeCommandInvocation(command, args);
  return await new Promise((resolve, reject) => {
    const killTree = () => {
      if (!child.pid) {
        return;
      }
      if (process.platform === "win32") {
        const killer = spawn("taskkill", ["/PID", String(child.pid), "/T", "/F"], {
          stdio: "ignore",
        });
        killer.unref();
        return;
      }
      try {
        process.kill(-child.pid, "SIGKILL");
      } catch {
        child.kill("SIGKILL");
      }
    };
    const child = spawn(invocation.program, invocation.argv, {
      cwd,
      env,
      stdio: ["ignore", "pipe", "pipe"],
      detached: process.platform !== "win32",
    });
    let stdout = "";
    let stderr = "";
    let settled = false;
    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      killTree();
      reject(
        new Error(
          `${command} ${args.join(" ")} timed out after ${timeoutMs}ms\nSTDOUT:\n${stdout}\nSTDERR:\n${stderr}`,
        ),
      );
    }, timeoutMs);
    child.stdout.on("data", (chunk) => {
      stdout += chunk.toString();
    });
    child.stderr.on("data", (chunk) => {
      stderr += chunk.toString();
    });
    child.on("close", (code) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      const result = { code: code ?? 0, stdout, stderr };
      if (!allowFailure && result.code !== 0) {
        reject(
          new Error(
            `${invocation.program} ${invocation.argv.join(" ")} failed with code ${result.code}\nSTDOUT:\n${stdout}\nSTDERR:\n${stderr}`,
          ),
        );
        return;
      }
      resolve(result);
    });
    child.on("error", (error) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      reject(error);
    });
  });
}

export function parseJsonOutput(text) {
  const trimmed = text.trim();
  if (!trimmed) {
    throw new Error("Expected JSON output, got empty string");
  }
  try {
    return JSON.parse(trimmed);
  } catch {
    const lines = trimmed.split(/\r?\n/);
    for (let index = 0; index < lines.length; index += 1) {
      const candidate = lines[index].trimStart();
      if (!candidate.startsWith("{") && !candidate.startsWith("[")) {
        continue;
      }
      if (/^\[[A-Za-z-]+\]/.test(candidate)) {
        continue;
      }
      try {
        return JSON.parse(lines.slice(index).join("\n"));
      } catch {
        // Keep searching.
      }
    }
    throw new Error(`Could not locate JSON payload in output:\n${trimmed}`);
  }
}

export async function loadMainConfigSnapshot() {
  for (const envName of [
    "OPENCLAW_ONBOARDING_BASE_CONFIG_PATH",
    "OPENCLAW_CONFIG_PATH",
    "OPENCLAW_CONFIG",
  ]) {
    const configured = String(process.env[envName] || "").trim();
    if (!configured) {
      continue;
    }
    const configPath = expandHome(configured);
    return await readConfigSnapshot(configPath);
  }
  try {
    const pathResult = await runCommand(OPENCLAW_BIN, ["config", "file"], {
      timeoutMs: 15_000,
    });
    const configPath = expandHome(pathResult.stdout.trim());
    return await readConfigSnapshot(configPath);
  } catch (error) {
    const synthetic = buildSyntheticConfigSnapshot();
    if (synthetic) {
      return synthetic;
    }
    throw error;
  }
}

function resolveProviderKeys(models) {
  const providers = models?.providers;
  if (providers && typeof providers === "object" && !Array.isArray(providers)) {
    return Object.keys(providers).filter(Boolean);
  }
  return [];
}

function qualifyOverrideModel(models, modelName) {
  const rendered = String(modelName || "").trim();
  if (!rendered || rendered.includes("/")) {
    return rendered;
  }
  const providerKey =
    ONBOARDING_CHAT_PROVIDER_OVERRIDE ||
    resolveProviderKeys(models)[0] ||
    "";
  return providerKey ? `${providerKey}/${rendered}` : rendered;
}

function applyOnboardingProviderOverrides(models) {
  if (!models || typeof models !== "object" || !ONBOARDING_CHAT_BASE_URL_OVERRIDE) {
    return models;
  }

  const cloned = JSON.parse(JSON.stringify(models));
  const providerKeys = resolveProviderKeys(cloned);
  if (!providerKeys.length) {
    return cloned;
  }

  const targetProviderKey =
    ONBOARDING_CHAT_PROVIDER_OVERRIDE && providerKeys.includes(ONBOARDING_CHAT_PROVIDER_OVERRIDE)
      ? ONBOARDING_CHAT_PROVIDER_OVERRIDE
      : providerKeys[0];
  const provider = cloned?.providers?.[targetProviderKey];
  if (!provider || typeof provider !== "object") {
    return cloned;
  }

  provider.baseUrl = normalizeChatBaseUrl(ONBOARDING_CHAT_BASE_URL_OVERRIDE);
  const providerModelId = normalizeProviderModelId(
    qualifyOverrideModel(cloned, ONBOARDING_CHAT_MODEL_OVERRIDE),
  );
  if (providerModelId) {
    const existingModels = Array.isArray(provider.models) ? provider.models : [];
    const alreadyRegistered = existingModels.some((model) => (
      model
      && typeof model === "object"
      && (model.id === providerModelId || model.name === providerModelId)
    ));
    if (!alreadyRegistered) {
      const template =
        existingModels.find((model) => model && typeof model === "object")
        || {};
      provider.models = [
        {
          ...template,
          id: providerModelId,
          name: providerModelId,
        },
        ...existingModels,
      ];
    }
  }
  return cloned;
}

function normalizeChatBaseUrl(value) {
  const rendered = String(value || "").trim().replace(/\/+$/, "");
  if (!rendered) {
    return rendered;
  }
  for (const suffix of ["/chat/completions", "/responses"]) {
    if (rendered.endsWith(suffix)) {
      return rendered.slice(0, -suffix.length);
    }
  }
  return rendered;
}

function normalizeProviderModelId(value) {
  const rendered = String(value || "").trim();
  if (!rendered) {
    return "";
  }
  if (!rendered.includes("/")) {
    return rendered;
  }
  const [, ...rest] = rendered.split("/");
  return rest.join("/").trim();
}

function normalizeRuntimeEnvValue(key, value) {
  const rendered = String(value || "").trim();
  if (!rendered) {
    return "";
  }
  if (
    key === "LLM_API_BASE"
    || key === "WRITE_GUARD_LLM_API_BASE"
    || key === "COMPACT_GIST_LLM_API_BASE"
  ) {
    return normalizeChatBaseUrl(rendered);
  }
  if (
    key === "RETRIEVAL_EMBEDDING_API_BASE"
    || key === "RETRIEVAL_RERANKER_API_BASE"
  ) {
    return rendered.replace(/\/+$/, "");
  }
  return rendered;
}

function parseRuntimeEnvText(envText) {
  return Object.fromEntries(
    String(envText || "")
      .split(/\r?\n/)
      .map((line) => line.trim())
      .filter((line) => line && !line.startsWith("#") && line.includes("="))
      .map((line) => {
        const index = line.indexOf("=");
        return [line.slice(0, index), line.slice(index + 1)];
      }),
  );
}

function buildSetupArgExpectations(setupArgs = []) {
  const expectations = new Map();
  const flagToKeys = new Map([
    ["--embedding-api-base", ["RETRIEVAL_EMBEDDING_API_BASE"]],
    ["--embedding-api-key", ["RETRIEVAL_EMBEDDING_API_KEY"]],
    ["--embedding-model", ["RETRIEVAL_EMBEDDING_MODEL"]],
    ["--embedding-dim", ["RETRIEVAL_EMBEDDING_DIM"]],
    ["--reranker-api-base", ["RETRIEVAL_RERANKER_API_BASE"]],
    ["--reranker-api-key", ["RETRIEVAL_RERANKER_API_KEY"]],
    ["--reranker-model", ["RETRIEVAL_RERANKER_MODEL"]],
    ["--llm-api-base", ["LLM_API_BASE"]],
    ["--llm-api-key", ["LLM_API_KEY"]],
    ["--llm-model", ["LLM_MODEL_NAME"]],
    ["--write-guard-llm-api-base", ["WRITE_GUARD_LLM_API_BASE"]],
    ["--write-guard-llm-api-key", ["WRITE_GUARD_LLM_API_KEY"]],
    ["--write-guard-llm-model", ["WRITE_GUARD_LLM_MODEL"]],
    ["--compact-gist-llm-api-base", ["COMPACT_GIST_LLM_API_BASE"]],
    ["--compact-gist-llm-api-key", ["COMPACT_GIST_LLM_API_KEY"]],
    ["--compact-gist-llm-model", ["COMPACT_GIST_LLM_MODEL"]],
  ]);

  for (let index = 0; index < setupArgs.length; index += 2) {
    const flag = String(setupArgs[index] || "").trim();
    const value = String(setupArgs[index + 1] || "").trim();
    const keys = flagToKeys.get(flag);
    if (!keys || !value) {
      continue;
    }
    for (const key of keys) {
      expectations.set(key, normalizeRuntimeEnvValue(key, value));
    }
  }
  return expectations;
}

function runtimeEnvMatchesSetupArgs(envText, setupArgs = []) {
  const expectations = buildSetupArgExpectations(setupArgs);
  if (expectations.size === 0) {
    return true;
  }
  const envValues = parseRuntimeEnvText(envText);
  for (const [key, expectedValue] of expectations.entries()) {
    const actualValue = normalizeRuntimeEnvValue(key, envValues[key]);
    if (actualValue !== expectedValue) {
      return false;
    }
  }
  return true;
}

function overlayRuntimeEnvText(envText, setupArgs = []) {
  const expectations = buildSetupArgExpectations(setupArgs);
  if (expectations.size === 0) {
    return envText;
  }
  const lines = String(envText || "").split(/\r?\n/);
  const seenKeys = new Set();
  const nextLines = lines.map((line) => {
    if (!line || line.trimStart().startsWith("#") || !line.includes("=")) {
      return line;
    }
    const index = line.indexOf("=");
    const key = line.slice(0, index);
    if (!expectations.has(key)) {
      return line;
    }
    seenKeys.add(key);
    return `${key}=${expectations.get(key)}`;
  });
  for (const [key, value] of expectations.entries()) {
    if (!seenKeys.has(key)) {
      nextLines.push(`${key}=${value}`);
    }
  }
  return nextLines.join("\n");
}

export function buildBaseConfig({
  models,
  agentDefaultsModel,
  workspaceDir,
  port,
  extraAgents = [],
}) {
  const effectiveModels = applyOnboardingProviderOverrides(models);
  const primaryModel =
    qualifyOverrideModel(effectiveModels, ONBOARDING_CHAT_MODEL_OVERRIDE) ||
    agentDefaultsModel?.primary ||
    "localgpt54/gpt-5.4";
  const agentModel = ONBOARDING_CHAT_MODEL_OVERRIDE
    ? { primary: primaryModel }
    : (agentDefaultsModel || { primary: "localgpt54/gpt-5.4" });
  const modelAlias =
    ONBOARDING_CHAT_MODEL_ALIAS ||
    primaryModel.split("/").pop() ||
    primaryModel;
  const normalizedExtraAgents = Array.from(
    new Set(
      extraAgents
        .map((value) => String(value || "").trim())
        .filter((value) => value && value !== "main"),
    ),
  );
  return {
    meta: {
      lastTouchedVersion: "onboarding-doc-chat-flow",
    },
    agents: {
      defaults: {
        model: agentModel,
        models:
          primaryModel || !agentDefaultsModel || Boolean(ONBOARDING_CHAT_MODEL_OVERRIDE)
            ? {
                [primaryModel]: {
                  alias: modelAlias,
                },
              }
            : undefined,
        workspace: workspaceDir,
        skipBootstrap: true,
      },
      list: [
        {
          id: "main",
          default: true,
          model: primaryModel,
          workspace: workspaceDir,
          identity: {
            name: "Onboarding Doc Test",
            theme: "protocol droid",
            emoji: "🧪",
          },
        },
        ...normalizedExtraAgents.map((agentId) => ({
          id: agentId,
          default: false,
          model: primaryModel,
          workspace: workspaceDir,
          identity: {
            name: `Scenario ${agentId}`,
            theme: "protocol droid",
            emoji: "🧪",
          },
        })),
      ],
    },
    models: effectiveModels,
    commands: {
      native: "auto",
      nativeSkills: "auto",
      restart: true,
      ownerDisplay: "raw",
    },
    gateway: {
      port,
      mode: "local",
      bind: "loopback",
      controlUi: {
        enabled: true,
      },
      auth: {
        mode: "none",
        token: CONTROL_TOKEN,
      },
      http: {
        endpoints: {
          chatCompletions: {
            enabled: true,
          },
        },
      },
    },
  };
}

export async function writeJson(filePath, payload) {
  await mkdir(path.dirname(filePath), { recursive: true });
  await writeFile(filePath, `${JSON.stringify(payload, null, 2)}\n`, "utf8");
}

async function ensureScenarioSessionDirs(stateDir, extraAgents = []) {
  const agentIds = Array.from(new Set(["main", ...extraAgents]));
  await Promise.all(
    agentIds.map(async (agentId) => {
      await mkdir(path.join(stateDir, "agents", agentId, "sessions"), { recursive: true });
    }),
  );
}

async function loadJsonIfExists(filePath) {
  try {
    const raw = await readFile(filePath, "utf8");
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

function scenarioConfigHasMemoryPalace(config) {
  const plugins = config?.plugins;
  const entries = plugins?.entries;
  const slot = plugins?.slots?.memory;
  return Boolean(
    slot === "memory-palace"
      && entries
      && typeof entries === "object"
      && entries["memory-palace"],
  );
}

function replaceSetupRootInObject(value, sourceRoot, targetRoot) {
  if (typeof value === "string") {
    return value.split(sourceRoot).join(targetRoot);
  }
  if (Array.isArray(value)) {
    return value.map((item) => replaceSetupRootInObject(item, sourceRoot, targetRoot));
  }
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value).map(([key, entryValue]) => [
        key,
        replaceSetupRootInObject(entryValue, sourceRoot, targetRoot),
      ]),
    );
  }
  return value;
}

async function findPreparedScenarioTemplate({ name, profile, setupMode, setupArgs = [], currentRoot }) {
  const prefix = `${name}-`;
  const candidates = (await Promise.all(
    (await readDirSafe(tempRoot)).map(async (entry) => {
      const candidatePath = path.join(tempRoot, entry);
      try {
        const details = await stat(candidatePath);
        return {
          path: candidatePath,
          mtimeMs: details.mtimeMs,
        };
      } catch {
        return null;
      }
    }),
  ))
    .filter((entry) => (
      entry
      && entry.path !== currentRoot
      && path.basename(entry.path).startsWith(prefix)
      && existsSync(path.join(entry.path, "memory-palace", "runtime.env"))
      && existsSync(path.join(entry.path, "openclaw.json"))
    ))
    .sort((left, right) => right.mtimeMs - left.mtimeMs)
    .map((entry) => entry.path);

  for (const candidate of candidates) {
    try {
      const config = JSON.parse(await readFile(path.join(candidate, "openclaw.json"), "utf8"));
      const envPath = path.join(candidate, "memory-palace", "runtime.env");
      const envText = await readFile(envPath, "utf8");
      if (!scenarioConfigHasMemoryPalace(config)) {
        continue;
      }
      const requestedProfileMatch = envText.match(/^OPENCLAW_MEMORY_PALACE_PROFILE_REQUESTED=(.+)$/m);
      const modeMatch = envText.match(/^OPENCLAW_MEMORY_PALACE_MODE=(.+)$/m);
      if (
        String(requestedProfileMatch?.[1] || "").trim().toLowerCase() === String(profile).trim().toLowerCase()
        && String(modeMatch?.[1] || "").trim().toLowerCase() === String(setupMode).trim().toLowerCase()
      ) {
        if (!runtimeEnvMatchesSetupArgs(envText, setupArgs)) {
          continue;
        }
        return {
          root: candidate,
          setupRoot: path.join(candidate, "memory-palace"),
          config,
          envText,
        };
      }
    } catch {
      // Keep scanning.
    }
  }
  return null;
}

async function readDirSafe(dirPath) {
  try {
    return await readdir(dirPath);
  } catch {
    return [];
  }
}

async function clonePreparedScenarioTemplate({
  template,
  setupRoot,
  configPath,
  baseConfig,
  setupArgs = [],
}) {
  await cp(template.setupRoot, setupRoot, { recursive: true });
  const sourceSetupRoot = template.setupRoot;
  const targetSetupRoot = setupRoot;
  const clonedConfig = replaceSetupRootInObject(template.config, sourceSetupRoot, targetSetupRoot);
  const runtimeEnvPath = path.join(setupRoot, "runtime.env");
  const dbPath = path.join(setupRoot, "data", "memory-palace.db");
  await rm(dbPath, { force: true }).catch(() => {});
  const rewrittenEnv = overlayRuntimeEnvText(
    template.envText
    .split(sourceSetupRoot).join(targetSetupRoot)
    .replace(
      /^DATABASE_URL=.*$/m,
      `DATABASE_URL=sqlite+aiosqlite:////${dbPath.replace(/^\/+/, "")}`,
    ),
    setupArgs,
  );
  await writeFile(runtimeEnvPath, rewrittenEnv, "utf8");
  const mergedConfig = {
    ...baseConfig,
    plugins: clonedConfig.plugins,
    hooks: clonedConfig.hooks,
  };
  await writeJson(configPath, mergedConfig);
  return mergedConfig;
}

export async function prepareScenario({
  name,
  port,
  installPlugin,
  profile = "b",
  setupMode = "basic",
  extraEnv = {},
  extraAgents = [],
  setupArgs = [],
  configMutator = null,
}) {
  await mkdir(tempRoot, { recursive: true });
  const root = await mkdtemp(path.join(tempRoot, `${name}-`));
  const homeDir = path.join(root, "home");
  const stateDir = path.join(root, "state");
  const workspaceDir = path.join(root, "workspace");
  const configPath = path.join(root, "openclaw.json");
  const setupRoot = path.join(root, "memory-palace");
  await mkdir(homeDir, { recursive: true });
  await mkdir(stateDir, { recursive: true });
  await mkdir(workspaceDir, { recursive: true });
  await ensureScenarioSessionDirs(stateDir, extraAgents);

  const mainConfig = await loadMainConfigSnapshot();
  let payload = buildBaseConfig({
    models: mainConfig.payload.models,
    agentDefaultsModel: mainConfig.payload.agents?.defaults?.model,
    workspaceDir,
    port,
    extraAgents,
  });
  if (typeof configMutator === "function") {
    const mutated = await configMutator(payload);
    if (mutated && typeof mutated === "object") {
      payload = mutated;
    }
  }
  await writeJson(configPath, payload);

  const env = {
    ...process.env,
    ...extraEnv,
    HOME: homeDir,
    USERPROFILE: homeDir,
    OPENCLAW_CONFIG_PATH: configPath,
    OPENCLAW_STATE_DIR: stateDir,
    PYTHON: PYTHON_BIN,
    PYTHON3: PYTHON_BIN,
    PIP_CACHE_DIR: String(extraEnv.PIP_CACHE_DIR || sharedPipCacheDir),
    PIP_DISABLE_PIP_VERSION_CHECK: "1",
  };
  for (const key of ["SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "PIP_CERT"]) {
    delete env[key];
  }

  let resolvedConfig = payload;
  if (installPlugin) {
    const reusableTemplate = await findPreparedScenarioTemplate({
      name,
      profile,
      setupMode,
      setupArgs,
      currentRoot: root,
    });

    if (reusableTemplate) {
      resolvedConfig = await clonePreparedScenarioTemplate({
        template: reusableTemplate,
        setupRoot,
        configPath,
        baseConfig: payload,
        setupArgs,
      });
    } else {
      await runCommand(
        PYTHON_BIN,
        [
          path.join(repoRoot, "scripts", "openclaw_memory_palace.py"),
          "setup",
          "--config",
          configPath,
          "--setup-root",
          setupRoot,
          "--mode",
          setupMode,
          "--profile",
          profile,
          "--transport",
          "stdio",
          ...setupArgs,
          "--json",
        ],
        { env, timeoutMs: SCENARIO_SETUP_TIMEOUT_MS },
      );

      resolvedConfig = await loadJsonIfExists(configPath) || payload;

      // OpenClaw 2026.4.5 can finish setup without backfilling the explicit
      // scenario config file, while install --config still patches it
      // deterministically. Keep isolated test scenarios self-contained.
      if (!scenarioConfigHasMemoryPalace(resolvedConfig)) {
        await runCommand(
          PYTHON_BIN,
          [
            path.join(repoRoot, "scripts", "openclaw_memory_palace.py"),
            "install",
            "--config",
            configPath,
            "--json",
          ],
          { env, timeoutMs: 120_000 },
        );
        resolvedConfig = await loadJsonIfExists(configPath) || payload;
      }
    }

    if (!scenarioConfigHasMemoryPalace(resolvedConfig)) {
      throw new Error(
        `Scenario config was not patched with memory-palace after setup/install: ${configPath}`,
      );
    }
  }

  if (typeof configMutator === "function") {
    const nextConfig = await configMutator(JSON.parse(JSON.stringify(resolvedConfig)));
    if (nextConfig && typeof nextConfig === "object") {
      resolvedConfig = nextConfig;
      await writeJson(configPath, resolvedConfig);
    }
  }

  return {
    name,
    root,
    homeDir,
    stateDir,
    workspaceDir,
    configPath,
    setupRoot,
    env,
    port,
    primaryModel: ONBOARDING_CHAT_MODEL_OVERRIDE || resolvedConfig?.agents?.defaults?.model?.primary,
    config: resolvedConfig,
  };
}

export function assertIncludes(haystack, needle, context) {
  if (!haystack.includes(needle)) {
    throw new Error(`${context}: expected output to include "${needle}"\n\n${haystack}`);
  }
}

export function assertExcludes(haystack, needle, context) {
  if (haystack.includes(needle)) {
    throw new Error(`${context}: expected output to exclude "${needle}"\n\n${haystack}`);
  }
}

export async function runLocalAgent({
  scenario,
  message,
  sessionId,
}) {
  const primaryModel =
    scenario.primaryModel ||
    scenario.config?.agents?.defaults?.model?.primary ||
    "localgpt54/gpt-5.4";
  const result = await runCommand(
    OPENCLAW_BIN,
    [
      "agent",
      "--local",
      "--model",
      primaryModel,
      "--session-id",
      sessionId,
      "--message",
      message,
      "--json",
    ],
    {
      env: scenario.env,
      timeoutMs: 240_000,
    },
  );
  const rawOutput = result.stdout.trim() ? result.stdout : result.stderr;
  if (!rawOutput.trim()) {
    throw new Error(
      `Local agent returned no JSON output.\nCONFIG: ${scenario.configPath}\nSTATE: ${scenario.stateDir}\nSTDOUT:\n${result.stdout}\nSTDERR:\n${result.stderr}`,
    );
  }
  const payload = parseJsonOutput(rawOutput);
  const texts = payload.payloads || payload.result?.payloads || [];
  const text = texts.map((item) => item.text || "").join("\n").trim();
  return { payload, text };
}

export async function runGatewayAgent({
  scenario,
  message,
  sessionId,
}) {
  const result = await runCommand(
    OPENCLAW_BIN,
    [
      "agent",
      "--agent",
      "main",
      "--session-id",
      sessionId,
      "--message",
      message,
      "--json",
    ],
    {
      env: scenario.env,
      timeoutMs: 240_000,
    },
  );
  const rawOutput = result.stdout.trim() ? result.stdout : result.stderr;
  if (!rawOutput.trim()) {
    throw new Error(
      `Gateway agent returned no JSON output.\nCONFIG: ${scenario.configPath}\nSTATE: ${scenario.stateDir}\nSTDOUT:\n${result.stdout}\nSTDERR:\n${result.stderr}`,
    );
  }
  const payload = parseJsonOutput(rawOutput);
  const texts =
    payload.result?.payloads ||
    payload.payloads ||
    [];
  const text = texts.map((item) => item.text || "").join("\n").trim();
  return { payload, text };
}

export async function getDashboardUrl(scenario) {
  if (scenario?.port) {
    return `http://127.0.0.1:${scenario.port}/#token=${CONTROL_TOKEN}`;
  }
  const result = await runCommand(
    OPENCLAW_BIN,
    ["dashboard", "--no-open"],
    {
      env: scenario.env,
      timeoutMs: 30_000,
    },
  );
  const match = result.stdout.match(/Dashboard URL:\s+(https?:\/\/\S+)/);
  if (!match) {
    throw new Error(`Could not parse dashboard URL from output:\n${result.stdout}`);
  }
  return match[1];
}

export async function startGateway(scenario) {
  const gatewayArgs = [
    "gateway",
    "run",
    "--allow-unconfigured",
    "--force",
    "--port",
    String(scenario.port),
    "--verbose",
  ];
  const invocation = process.platform === "win32"
    ? {
        program: WINDOWS_POWERSHELL_BIN,
        argv: [
          "-NoProfile",
          "-ExecutionPolicy",
          "Bypass",
          "-Command",
          ["&", `'${OPENCLAW_BIN.replaceAll("'", "''")}'`, ...gatewayArgs.map((value) => `'${String(value || "").replaceAll("'", "''")}'`)].join(" "),
        ],
      }
    : {
        program: OPENCLAW_BIN,
        argv: gatewayArgs,
      };
  const child = spawn(invocation.program, invocation.argv, {
    cwd: repoRoot,
    env: scenario.env,
    stdio: ["ignore", "pipe", "pipe"],
  });
  let stdout = "";
  let stderr = "";
  child.stdout.on("data", (chunk) => {
    stdout += chunk.toString();
  });
  child.stderr.on("data", (chunk) => {
    stderr += chunk.toString();
  });

  const normalizeGatewayLogs = (text) => String(text || "")
    .replace(/\x1b\[[0-9;?]*[ -/]*[@-~]/gu, "")
    .replace(/\x1b\][^\u0007]*(?:\u0007|\x1b\\)/gu, "")
    .replace(/\r/g, "");

  const gatewayReady = () => {
    const logs = normalizeGatewayLogs(`${stdout}\n${stderr}`);
    return (
      logs.includes("[gateway] listening on ws://127.0.0.1:")
      || logs.includes("[gateway] ready (")
      || logs.includes("[gateway] MCP loopback server listening on http://127.0.0.1:")
    );
  };

  const gatewayHttpReady = async () => {
    for (const candidateUrl of [
      `http://127.0.0.1:${scenario.port}/`,
      `http://127.0.0.1:${scenario.port}/chat?session=main`,
    ]) {
      try {
        const response = await fetch(candidateUrl, {
          signal: AbortSignal.timeout(2_000),
        });
        if (response.ok || response.status === 304 || response.status === 401 || response.status === 404) {
          return true;
        }
      } catch {
        // Try the next local endpoint.
      }
    }
    return false;
  };

  const deadline = Date.now() + 45_000;
  let stableSince = 0;
  const stabilityWindowMs = 2_500;
  while (Date.now() < deadline) {
    const readyByLog = gatewayReady();
    const readyByHttp = await gatewayHttpReady();
    if (readyByLog || readyByHttp) {
      if (!stableSince) {
        stableSince = Date.now();
      }
      if (Date.now() - stableSince >= stabilityWindowMs) {
        break;
      }
    } else {
      stableSince = 0;
    }
    if (child.exitCode !== null) {
      throw new Error(
        `Gateway exited early with code ${child.exitCode}\nSTDOUT:\n${stdout}\nSTDERR:\n${stderr}`,
      );
    }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  if (!gatewayReady() && !await gatewayHttpReady()) {
    throw new Error(`Timed out waiting for gateway start\nSTDOUT:\n${stdout}\nSTDERR:\n${stderr}`);
  }
  return {
    child,
    getLogs() {
      return { stdout, stderr };
    },
  };
}

export async function stopGateway(handle) {
  if (!handle?.child) {
    return;
  }
  if (handle.child.exitCode !== null) {
    return;
  }
  handle.child.kill("SIGINT");
  await new Promise((resolve) => {
    const timer = setTimeout(resolve, 5_000);
    handle.child.once("close", () => {
      clearTimeout(timer);
      resolve();
    });
  });
}

export async function loadPlaywright() {
  if (!process.env.PLAYWRIGHT_BROWSERS_PATH) {
    const hostHomeDir = (() => {
      try {
        return os.userInfo().homedir;
      } catch {
        return os.homedir();
      }
    })();
    const cacheCandidates = [
      String(process.env.OPENCLAW_PLAYWRIGHT_BROWSERS_PATH || "").trim(),
      process.platform === "darwin"
        ? path.join(hostHomeDir, "Library", "Caches", "ms-playwright")
        : process.platform === "win32"
          ? path.join(hostHomeDir, "AppData", "Local", "ms-playwright")
          : path.join(hostHomeDir, ".cache", "ms-playwright"),
    ].filter(Boolean);
    const browserCache = cacheCandidates.find((candidate) => existsSync(candidate));
    if (browserCache) {
      process.env.PLAYWRIGHT_BROWSERS_PATH = browserCache;
    }
  }
  const playwrightUrl = pathToFileURL(
    path.join(frontendRoot, "node_modules", "playwright", "index.js"),
  ).href;
  const mod = await import(playwrightUrl);
  return mod.default ?? mod;
}

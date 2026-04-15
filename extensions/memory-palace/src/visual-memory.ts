import { spawn } from "node:child_process";
import type {
  PluginConfig,
  TraceLogger,
  VisualEnrichmentCommandConfig,
  VisualEnrichmentField,
  VisualEnrichmentProviderName,
  VisualFieldSource,
} from "./types.js";
import {
  formatError,
  isRecord,
  readLooseStringArray,
  readString,
} from "./utils.js";
import {
  ResolvedVisualInput,
  VisualContextPayload,
  chooseVisualField,
  clearVisualTurnContextCache,
  collapseRuntimeVisualProbe,
  deriveVisualScene,
  deriveVisualSummary,
  extractVisualContextCandidatesFromUnknown,
  extractVisualContextFromMessages,
  extractVisualContextFromToolContext,
  getCachedVisualContext,
  getVisualTurnContextCacheSizeForTesting,
  harvestVisualContextForTesting,
  hasCliVisualPayloadData,
  hasVisualPayloadData,
  normalizeVisualPathPrefix,
  parseVisualContext,
  readVisualDuplicatePolicy,
  rememberVisualContext,
  rememberVisualContexts,
  resolveVisualInput,
  resolveVisualLocalPath,
  selectVisualContextCandidate,
} from "./visual-context.js";
import {
  DEFAULT_VISUAL_MEMORY_DISCLOSURE,
  DEFAULT_VISUAL_MEMORY_RETENTION_NOTE,
} from "./visual-defaults.js";
import {
  scheduleVisualForceKill,
  setVisualTerminationPlatformForTesting,
  setVisualWindowsProcessTreeTerminatorForTesting,
  terminateVisualChildProcess,
} from "./visual-process.js";
import {
  buildUnavailableSearchResult,
  buildVisualMemoryContent,
  buildVisualMemoryUri,
  buildVisualNamespaceContent,
  buildVisualNamespaceForceBarrierContent,
  buildVisualNamespaceMachineTagContent,
  buildVisualNamespaceRetryContent,
  normalizeVisualSearchPayload,
  normalizeVisualSnippet,
  type VisualSearchNormalizationDeps,
} from "./visual-render-search.js";
import {
  looksLikeImageMediaRef,
  normalizeVisualPayload,
  redactVisualSensitiveText,
  sanitizeVisualMediaRef,
} from "./visual-redaction.js";

export {
  chooseVisualField,
  clearVisualTurnContextCache,
  collapseRuntimeVisualProbe,
  deriveVisualScene,
  deriveVisualSummary,
  extractVisualContextCandidatesFromUnknown,
  extractVisualContextFromMessages,
  extractVisualContextFromToolContext,
  getCachedVisualContext,
  getVisualTurnContextCacheSizeForTesting,
  harvestVisualContextForTesting,
  hasCliVisualPayloadData,
  hasVisualPayloadData,
  normalizeVisualPathPrefix,
  parseVisualContext,
  readVisualDuplicatePolicy,
  rememberVisualContext,
  rememberVisualContexts,
  resolveVisualInput,
  resolveVisualLocalPath,
  selectVisualContextCandidate,
} from "./visual-context.js";

export {
  DEFAULT_VISUAL_MEMORY_DISCLOSURE,
  DEFAULT_VISUAL_MEMORY_RETENTION_NOTE,
} from "./visual-defaults.js";

export {
  buildUnavailableSearchResult,
  buildVisualMemoryContent,
  buildVisualMemoryUri,
  buildVisualNamespaceContent,
  buildVisualNamespaceForceBarrierContent,
  buildVisualNamespaceMachineTagContent,
  buildVisualNamespaceRetryContent,
  normalizeVisualSearchPayload,
  normalizeVisualSnippet,
} from "./visual-render-search.js";

export {
  looksLikeImageMediaRef,
  normalizeVisualPayload,
  redactVisualSensitiveText,
  sanitizeVisualMediaRef,
} from "./visual-redaction.js";

export {
  setVisualTerminationPlatformForTesting,
  setVisualWindowsProcessTreeTerminatorForTesting,
} from "./visual-process.js";

export type {
  ResolvedVisualInput,
  VisualContextPayload,
} from "./visual-context.js";

export type { VisualSearchNormalizationDeps } from "./visual-render-search.js";

export function parseVisualEnrichmentOutput(
  stdout: string,
  defaultField: VisualEnrichmentField,
  depth = 0,
): Partial<VisualContextPayload> {
  if (depth > 4) {
    return {};
  }
  const trimmed = stdout.trim();
  if (!trimmed) {
    return {};
  }
  try {
    const parsed = JSON.parse(trimmed) as unknown;
    if (Array.isArray(parsed) && defaultField === "entities") {
      const entities = parsed
        .filter((entry): entry is string => typeof entry === "string")
        .map((entry) => redactVisualSensitiveText(entry) ?? entry)
        .filter(Boolean);
      return entities.length > 0 ? { entities } : {};
    }
    if (isRecord(parsed)) {
      const context = parseVisualContext(parsed);
      return {
        ...(context.summary ? { summary: context.summary } : {}),
        ...(context.ocr ? { ocr: context.ocr } : {}),
        ...(context.scene ? { scene: context.scene } : {}),
        ...(context.whyRelevant ? { whyRelevant: context.whyRelevant } : {}),
        ...(context.entities?.length ? { entities: context.entities } : {}),
        ...(context.sourceChannel ? { sourceChannel: context.sourceChannel } : {}),
        ...(context.observedAt ? { observedAt: context.observedAt } : {}),
        ...(context.confidence !== undefined ? { confidence: context.confidence } : {}),
      };
    }
    if (typeof parsed === "string") {
      return parseVisualEnrichmentOutput(parsed, defaultField, depth + 1);
    }
  } catch {
    // Plain-text fallback.
  }

  if (defaultField === "entities") {
    const entities = readLooseStringArray(trimmed)
      ?.map((entry) => redactVisualSensitiveText(entry) ?? entry)
      .filter(Boolean);
    return entities && entities.length > 0 ? { entities } : {};
  }
  const sanitized = redactVisualSensitiveText(trimmed) ?? trimmed;
  if (defaultField === "ocr") {
    return { ocr: sanitized };
  }
  if (defaultField === "scene") {
    return { scene: sanitized };
  }
  if (defaultField === "whyRelevant") {
    return { whyRelevant: sanitized };
  }
  return { summary: sanitized };
}

async function runVisualEnrichmentProvider(
  providerName: VisualEnrichmentProviderName,
  commandConfig: VisualEnrichmentCommandConfig,
  requestedFields: VisualEnrichmentField[],
  input: ResolvedVisualInput,
): Promise<Partial<VisualContextPayload>> {
  const command = readString(commandConfig.command);
  if (!command) {
    return {};
  }
  const defaultField = requestedFields[0] ?? (providerName === "ocr" ? "ocr" : "summary");
  const payload = {
    provider: providerName,
    requestedFields,
    mediaRef: sanitizeVisualMediaRef(input.mediaRef),
    summary: redactVisualSensitiveText(input.summary),
    ocr: redactVisualSensitiveText(input.ocr),
    scene: redactVisualSensitiveText(input.scene),
    whyRelevant: redactVisualSensitiveText(input.whyRelevant),
    entities: input.entities?.map((entry) => redactVisualSensitiveText(entry) ?? entry),
    sourceChannel: input.sourceChannel,
    observedAt: input.observedAt,
    runtimeSource: input.runtimeSource,
    runtimeProbe: input.runtimeProbe,
  };

  // Treat `command` as the full executable path (may contain spaces, e.g.
  // "C:\Program Files\tool.exe").  Only `commandConfig.args` supplies argv.
  const program = command;
  const splitArgs = [...(commandConfig.args ?? [])];

  const safeEnv: Record<string, string> = {};
  const ALLOWED_ENV_KEYS = [
    "PATH",
    "HOME",
    "USERPROFILE",
    "TMPDIR",
    "TMP",
    "TEMP",
    "LANG",
    "NODE_ENV",
    "SystemRoot",
  ];
  for (const key of ALLOWED_ENV_KEYS) {
    if (process.env[key]) safeEnv[key] = process.env[key]!;
  }

  const stdout = await new Promise<string>((resolve, reject) => {
    const child = spawn(program, splitArgs, {
      cwd: commandConfig.cwd,
      detached: process.platform !== "win32",
      env: {
        ...safeEnv,
        ...(commandConfig.env ?? {}),
      },
      shell: false,
      stdio: ["pipe", "pipe", "pipe"],
    });
    const stdoutChunks: string[] = [];
    const stderrChunks: string[] = [];
    let settled = false;
    let timedOut = false;
    let forceKillTimer: ReturnType<typeof setTimeout> | null = null;
    const timeoutMs = commandConfig.timeoutMs ?? 8_000;
    const clearForceKillTimer = () => {
      if (forceKillTimer) {
        clearTimeout(forceKillTimer);
        forceKillTimer = null;
      }
    };
    const terminateChild = () => {
      terminateVisualChildProcess(child, { force: false });
      forceKillTimer = scheduleVisualForceKill(child, timeoutMs, () => settled);
    };
    const timer = setTimeout(() => {
      if (settled || timedOut) {
        return;
      }
      timedOut = true;
      terminateChild();
      reject(new Error(`visual ${providerName} adapter timed out after ${timeoutMs}ms`));
    }, timeoutMs);
    timer.unref?.();
    const finish = (callback: () => void) => {
      if (settled) {
        return;
      }
      settled = true;
      clearTimeout(timer);
      clearForceKillTimer();
      callback();
    };

    child.stdout.on("data", (chunk) => {
      stdoutChunks.push(String(chunk));
    });
    child.stderr.on("data", (chunk) => {
      stderrChunks.push(String(chunk));
    });
    child.once("error", (error) =>
      finish(() => {
        if (!timedOut) {
          reject(error);
        }
      }));
    child.once("close", (code, signal) => {
      finish(() => {
        if (timedOut) {
          return;
        }
        if (code === 0) {
          resolve(stdoutChunks.join(""));
          return;
        }
        const stderr = redactVisualSensitiveText(stderrChunks.join("").trim());
        reject(
          new Error(
            `visual ${providerName} adapter failed (${signal ?? code ?? "unknown"}): ${stderr || "no stderr"}`,
          ),
        );
      });
    });
    child.stdin.on("error", () => {});
    child.stdin.end(JSON.stringify(payload));
  });

  return parseVisualEnrichmentOutput(stdout, defaultField);
}

export function shouldAdoptAdapterField(
  field: VisualEnrichmentField,
  currentSource: VisualFieldSource | undefined,
): boolean {
  if (currentSource === "missing") {
    return true;
  }
  return (field === "summary" || field === "scene") && currentSource === "derived";
}

export function mergeVisualEnrichmentResult(
  input: ResolvedVisualInput,
  partial: Partial<VisualContextPayload>,
): ResolvedVisualInput {
  const next: ResolvedVisualInput = {
    ...input,
    fieldSources: { ...input.fieldSources },
  };

  if (partial.summary && shouldAdoptAdapterField("summary", next.fieldSources.summary)) {
    next.summary = partial.summary;
    next.fieldSources.summary = "adapter";
  }
  if (partial.ocr && shouldAdoptAdapterField("ocr", next.fieldSources.ocr)) {
    next.ocr = partial.ocr;
    next.fieldSources.ocr = "adapter";
  }
  if (partial.scene && shouldAdoptAdapterField("scene", next.fieldSources.scene)) {
    next.scene = partial.scene;
    next.fieldSources.scene = "adapter";
  }
  if (partial.entities?.length && shouldAdoptAdapterField("entities", next.fieldSources.entities)) {
    next.entities = partial.entities;
    next.fieldSources.entities = "adapter";
  }
  if (partial.whyRelevant && shouldAdoptAdapterField("whyRelevant", next.fieldSources.whyRelevant)) {
    next.whyRelevant = partial.whyRelevant;
    next.fieldSources.whyRelevant = "adapter";
  }
  if (!next.sourceChannel && partial.sourceChannel) {
    next.sourceChannel = partial.sourceChannel;
  }
  if (!next.observedAt && partial.observedAt) {
    next.observedAt = partial.observedAt;
  }
  if (next.confidence === undefined && partial.confidence !== undefined) {
    next.confidence = partial.confidence;
  }
  return next;
}

export async function maybeEnrichVisualInput(
  config: PluginConfig["visualMemory"],
  input: ResolvedVisualInput,
  logger?: TraceLogger,
): Promise<ResolvedVisualInput> {
  if (!config.enrichment.enabled) {
    return input;
  }

  let next = input;
  const ocrRequestedFields: VisualEnrichmentField[] =
    next.fieldSources.ocr === "missing" ? ["ocr"] : [];
  if (ocrRequestedFields.length > 0 && config.enrichment.ocr?.command) {
    try {
      next = mergeVisualEnrichmentResult(
        next,
        await runVisualEnrichmentProvider("ocr", config.enrichment.ocr, ocrRequestedFields, next),
      );
    } catch (error) {
      logger?.warn?.(`memory-palace visual ocr adapter failed: ${formatError(error)}`);
    }
  }

  const analyzerRequestedFields: VisualEnrichmentField[] = [];
  if (shouldAdoptAdapterField("summary", next.fieldSources.summary)) {
    analyzerRequestedFields.push("summary");
  }
  if (shouldAdoptAdapterField("scene", next.fieldSources.scene)) {
    analyzerRequestedFields.push("scene");
  }
  if (shouldAdoptAdapterField("entities", next.fieldSources.entities)) {
    analyzerRequestedFields.push("entities");
  }
  if (shouldAdoptAdapterField("whyRelevant", next.fieldSources.whyRelevant)) {
    analyzerRequestedFields.push("whyRelevant");
  }
  if (analyzerRequestedFields.length > 0 && config.enrichment.analyzer?.command) {
    try {
      next = mergeVisualEnrichmentResult(
        next,
        await runVisualEnrichmentProvider(
          "analyzer",
          config.enrichment.analyzer,
          analyzerRequestedFields,
          next,
        ),
      );
    } catch (error) {
      logger?.warn?.(`memory-palace visual analyzer adapter failed: ${formatError(error)}`);
    }
  }

  return next;
}

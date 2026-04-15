import {
  normalizeVisualPathPrefix,
  normalizeVisualSnippet,
} from "./visual-memory.js";
import { uriToVirtualPath, splitUri } from "./mapping.js";
import type {
  JsonRecord,
  MemorySearchResult,
  PluginConfig,
  SearchSource,
} from "./types.js";
import {
  isRecord,
  parseJsonRecord,
  readBoolean,
  readFlexibleNumber,
  readPositiveNumber,
  readString,
} from "./utils.js";

function countLines(text: string): number {
  return Math.max(1, text.split(/\r?\n/).length);
}

function parseCharRange(value: unknown): { start: number; end: number } | undefined {
  if (Array.isArray(value) && value.length >= 2) {
    const start = readPositiveNumber(value[0]) ?? (typeof value[0] === "number" ? value[0] : undefined);
    const end = readPositiveNumber(value[1]) ?? (typeof value[1] === "number" ? value[1] : undefined);
    if (start !== undefined && end !== undefined) {
      return { start, end };
    }
  }
  if (isRecord(value)) {
    const start = readPositiveNumber(value.start) ?? (typeof value.start === "number" ? value.start : undefined);
    const end = readPositiveNumber(value.end) ?? (typeof value.end === "number" ? value.end : undefined);
    if (start !== undefined && end !== undefined) {
      return { start, end };
    }
  }
  if (typeof value === "string") {
    const matched = value.match(/(\d+)\D+(\d+)/);
    if (matched) {
      return { start: Number(matched[1]), end: Number(matched[2]) };
    }
  }
  return undefined;
}

function isVisualNamespaceSnippet(text: string): boolean {
  return /visual namespace container|kind:\s*internal namespace container|visual_namespace_container:\s*true|VISUAL_NS_URI=/iu.test(
    text,
  );
}

function isVisualRecordSnippet(text: string): boolean {
  return /#\s*Visual Memory\b|kind:\s*visual-memory\b|- media_ref:\s|media_ref:\s|summary:\s/iu.test(
    text,
  );
}

function isVisualVariantSnippet(text: string): boolean {
  return /duplicate_variant:\s*new-\d+|provenance_variant_uri:\s*/iu.test(text);
}

export function isStructuredNamespaceContent(rawSnippet: string): boolean {
  return (
    /#\s*Memory Palace Namespace\b/iu.test(rawSnippet) ||
    /namespace_uri:\s*/iu.test(rawSnippet) ||
    /Container node for (reflection|capture|profile) records\./iu.test(rawSnippet)
  );
}

function hasPathPrefix(pathSegments: string[], pathPrefix: string): boolean {
  const prefixSegments = normalizeVisualPathPrefix(pathPrefix)
    .split("/")
    .filter(Boolean);
  if (prefixSegments.length === 0 || pathSegments.length < prefixSegments.length) {
    return false;
  }
  return prefixSegments.every((segment, index) => pathSegments[index] === segment);
}

function classifyVisualSearchResult(
  uri: string | undefined,
  rawSnippet: string,
  mapping: PluginConfig["mapping"],
  visualConfig?: Pick<PluginConfig["visualMemory"], "pathPrefix">,
): "container" | "variant" | "record" | "other" {
  if (!uri) {
    return "other";
  }
  const pathSegments = splitUri(uri, mapping.defaultDomain).path.split("/").filter(Boolean);
  const pathPrefix = visualConfig?.pathPrefix ?? "visual";
  if (!hasPathPrefix(pathSegments, pathPrefix)) {
    return "other";
  }
  const prefixSegments = normalizeVisualPathPrefix(pathPrefix).split("/").filter(Boolean);
  const remainder = pathSegments.slice(prefixSegments.length);
  if (isVisualNamespaceSnippet(rawSnippet)) {
    return "container";
  }
  if (isVisualVariantSnippet(rawSnippet)) {
    return "variant";
  }
  if (isVisualRecordSnippet(rawSnippet)) {
    return "record";
  }
  if (remainder.length === 0) {
    return "container";
  }
  if (remainder.length >= 4 && /^sha256-/i.test(remainder[remainder.length - 1] ?? "")) {
    return "record";
  }
  return "other";
}

function adjustVisualSearchScore(
  score: number,
  uri: string | undefined,
  rawSnippet: string,
  mapping: PluginConfig["mapping"],
  visualConfig?: Pick<PluginConfig["visualMemory"], "pathPrefix">,
): number {
  const classification = classifyVisualSearchResult(uri, rawSnippet, mapping, visualConfig);
  if (classification === "container") {
    return score - 0.35;
  }
  if (classification === "variant") {
    return score + 0.12;
  }
  if (classification === "record") {
    return score + 0.05;
  }
  return score;
}

function adjustStructuredNamespaceScore(score: number, rawSnippet: string): number {
  if (isStructuredNamespaceContent(rawSnippet)) {
    return score - 0.35;
  }
  return score;
}

function normalizeSearchSource(uri: string | undefined): SearchSource {
  return typeof uri === "string" && /^sessions?:\/\//i.test(uri) ? "sessions" : "memory";
}

function normalizeSearchResult(
  entry: unknown,
  mapping: PluginConfig["mapping"],
  visualConfig?: Pick<PluginConfig["visualMemory"], "pathPrefix">,
): MemorySearchResult | null {
  if (!isRecord(entry)) {
    return null;
  }
  const metadata = isRecord(entry.metadata) ? entry.metadata : {};
  const uri =
    readString(entry.uri) ??
    (readString(metadata.domain) || readString(metadata.path)
      ? `${readString(metadata.domain) ?? mapping.defaultDomain}://${readString(metadata.path) ?? ""}`
      : undefined);
  const rawPathValue = readString(entry.path)?.trim().replaceAll("\\", "/");
  const pathValue =
    uri && !/^sessions?:\/\//i.test(uri)
      ? uriToVirtualPath(uri, mapping)
      : rawPathValue ?? (uri ?? `${mapping.virtualRoot}/unknown.md`);
  const rawSnippet = readString(entry.snippet) ?? readString(entry.text) ?? "";
  const snippet = normalizeVisualSnippet(rawSnippet);
  const charRange = parseCharRange(entry.char_range);
  const scores = isRecord(entry.scores) ? entry.scores : {};
  const memoryId =
    readPositiveNumber(entry.memory_id) ??
    (typeof entry.memory_id === "number" ? entry.memory_id : undefined) ??
    readPositiveNumber(entry.id) ??
    (typeof entry.id === "number" ? entry.id : undefined);
  const score =
    readFlexibleNumber(scores.final) ??
    readFlexibleNumber(entry.score) ??
    0;
  const visualClassification = classifyVisualSearchResult(uri, rawSnippet, mapping, visualConfig);
  const visualAdjustedScore = adjustVisualSearchScore(score, uri, rawSnippet, mapping, visualConfig);
  const adjustedScore = visualClassification === "other"
    ? adjustStructuredNamespaceScore(visualAdjustedScore, rawSnippet)
    : visualAdjustedScore;

  return {
    path: pathValue,
    startLine: 1,
    endLine: countLines(snippet),
    score: adjustedScore,
    snippet,
    source: normalizeSearchSource(uri),
    ...(typeof memoryId === "number" ? { memoryId } : {}),
    citation: uri ? uriToVirtualPath(uri, mapping) : pathValue,
    ...(charRange ? { charRange } : {}),
  };
}

export function unwrapResultRecord(value: unknown): JsonRecord {
  let current: unknown = value;
  for (let depth = 0; depth < 4; depth += 1) {
    if (!isRecord(current)) {
      return {};
    }
    const wrapperRecord = Object.fromEntries(
      Object.entries(current).filter(([key]) => key !== "result"),
    ) as JsonRecord;
    if (isRecord(current.result)) {
      current = Object.keys(wrapperRecord).length > 0
        ? { ...current.result, ...wrapperRecord }
        : current.result;
      continue;
    }
    if (typeof current.result === "string") {
      const parsed = parseJsonRecord(current.result);
      if (parsed) {
        current = Object.keys(wrapperRecord).length > 0
          ? { ...parsed, ...wrapperRecord }
          : parsed;
        continue;
      }
    }
    return current;
  }
  return isRecord(current) ? current : {};
}

export function normalizeSearchPayload(
  raw: unknown,
  mapping: PluginConfig["mapping"],
  visualConfig?: Pick<PluginConfig["visualMemory"], "pathPrefix">,
) {
  const payload = unwrapResultRecord(raw);
  const payloadError =
    payload.ok === false
      ? readString(payload.error) ?? readString(payload.message) ?? readString(payload.reason) ?? "search_failed"
      : undefined;
  const results = Array.isArray(payload.results)
    ? payload.results
        .map((entry) => normalizeSearchResult(entry, mapping, visualConfig))
        .filter((entry): entry is MemorySearchResult => Boolean(entry))
    : [];

  return {
    results,
    provider: "memory-palace",
    model: readString(payload.intent_profile) ?? readString(payload.mode_applied) ?? undefined,
    mode: readString(payload.mode_applied) ?? readString(payload.mode_requested) ?? undefined,
    degraded: readBoolean(payload.degraded) ?? Boolean(payloadError),
    semanticSearchUnavailable:
      readBoolean(payload.semantic_search_unavailable) ??
      (Array.isArray(payload.degrade_reasons)
        ? payload.degrade_reasons.includes("embedding_fallback_hash")
        : false),
    backendMethod: readString(payload.backend_method),
    intent: readString(payload.intent),
    strategyTemplate: readString(payload.strategy_template),
    ...(payloadError
      ? {
          disabled: readBoolean(payload.disabled) ?? true,
          unavailable: readBoolean(payload.unavailable) ?? false,
          error: payloadError,
        }
      : {}),
    raw: payload,
  };
}

export function normalizeIndexStatusPayload(raw: unknown) {
  return unwrapResultRecord(raw);
}

export function normalizeCreatePayload(raw: unknown) {
  return unwrapResultRecord(raw);
}

export function extractReadText(
  raw: unknown,
): { text: string; selection?: unknown; degraded?: boolean; error?: string } {
  const normalizeReadString = (value: string, selection?: unknown, degraded?: boolean) => {
    if (/^Error:/i.test(value.trim())) {
      return {
        text: "",
        selection,
        degraded: true,
        error: value.trim().replace(/^Error:\s*/i, "") || "read_memory_failed",
      };
    }
    return {
      text: value,
      selection,
      degraded,
    };
  };
  if (typeof raw === "string") {
    return normalizeReadString(raw);
  }
  if (!isRecord(raw)) {
    return { text: raw === undefined ? "" : JSON.stringify(raw, null, 2) };
  }
  if (raw.ok === false) {
    return {
      text: "",
      selection: raw.selection,
      degraded: true,
      error:
        readString(raw.error) ??
        readString(raw.message) ??
        readString(raw.reason) ??
        "read_memory_failed",
    };
  }
  if (typeof raw.result === "string") {
    return normalizeReadString(raw.result, raw.selection, readBoolean(raw.degraded));
  }
  if (typeof raw.content === "string") {
    return normalizeReadString(raw.content, raw.selection, readBoolean(raw.degraded));
  }
  if (typeof raw.text === "string") {
    return normalizeReadString(raw.text, raw.selection, readBoolean(raw.degraded));
  }
  return {
    text: JSON.stringify(raw, null, 2),
    selection: raw.selection,
    degraded: readBoolean(raw.degraded),
  };
}

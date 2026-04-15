import { createHash } from "node:crypto";
import type {
  JsonRecord,
  MemorySearchResult,
  PluginConfig,
  RuntimeVisualProbe,
  RuntimeVisualSource,
  SearchSource,
  VisualDuplicatePolicy,
  VisualFieldSource,
} from "./types.js";
import {
  formatError,
  isRecord,
  readFlexibleNumber,
  readString,
  truncateWithEllipsis,
} from "./utils.js";
import {
  DEFAULT_VISUAL_MEMORY_DISCLOSURE,
  DEFAULT_VISUAL_MEMORY_RETENTION_NOTE,
  VISUAL_FORCE_CONTROL_BEGIN,
  VISUAL_FORCE_CONTROL_END,
} from "./visual-defaults.js";
import { normalizeVisualPathPrefix } from "./visual-context.js";
import { redactVisualSensitiveText } from "./visual-redaction.js";

export type VisualSearchNormalizationDeps = {
  unwrapResultRecord: (value: unknown) => JsonRecord;
  uriToVirtualPath: (uri: string, mapping: PluginConfig["mapping"]) => string;
  splitUri: (uri: string, defaultDomain: string) => { domain: string; path: string };
  parseCharRange: (value: unknown) => { start: number; end: number } | undefined;
  countLines: (text: string) => number;
};

function appendForceControlBlock(content: string, lines: Array<string | undefined>): string {
  const controlLines = lines
    .map((entry) => entry?.trim())
    .filter((entry): entry is string => Boolean(entry));
  if (controlLines.length === 0) {
    return content;
  }
  const normalized = content.replace(/\s+$/u, "");
  return [
    normalized,
    "",
    VISUAL_FORCE_CONTROL_BEGIN,
    ...controlLines,
    VISUAL_FORCE_CONTROL_END,
  ].join("\n");
}

function pickVisualField(text: string, key: string): string | undefined {
  const patterns = [
    new RegExp(`^-?\\s*${key}:\\s*(.+)$`, "im"),
    new RegExp(`"${key}"\\s*:\\s*"([^"]+)"`, "i"),
  ];
  for (const pattern of patterns) {
    const matched = pattern.exec(text);
    if (matched?.[1]?.trim()) {
      return matched[1].trim();
    }
  }
  return undefined;
}

export function normalizeVisualSnippet(rawSnippet: string): string {
  const trimmed = rawSnippet.trim();
  if (!trimmed) {
    return "";
  }
  const parts = [
    pickVisualField(trimmed, "summary"),
    pickVisualField(trimmed, "caption"),
    pickVisualField(trimmed, "ocr"),
    pickVisualField(trimmed, "entities"),
    pickVisualField(trimmed, "scene"),
    pickVisualField(trimmed, "why_relevant"),
    pickVisualField(trimmed, "media_ref"),
  ].filter((entry): entry is string => Boolean(entry));
  if (parts.length === 0) {
    return truncateWithEllipsis(trimmed.replace(/\s+/g, " "), 280);
  }
  return truncateWithEllipsis(parts.join(" | "), 280);
}

function isVisualNamespaceSnippet(text: string): boolean {
  return /visual namespace container|kind:\s*internal namespace container|visual_namespace_container:\s*true|VISUAL_NS_URI=/iu.test(
    text,
  );
}

function isVisualRecordSnippet(text: string): boolean {
  return /#\s*Visual Memory\b|kind:\s*visual-memory\b|- media_ref:\s|media_ref:\s|summary:\s/iu.test(text);
}

function isVisualVariantSnippet(text: string): boolean {
  return /duplicate_variant:\s*new-\d+|provenance_variant_uri:\s*/iu.test(text);
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
  splitUri: VisualSearchNormalizationDeps["splitUri"] = (currentUri, defaultDomain) => {
    const splitIndex = currentUri.indexOf("://");
    if (splitIndex < 0) {
      return { domain: defaultDomain, path: currentUri.replace(/^\/+/, "") };
    }
    return {
      domain: currentUri.slice(0, splitIndex),
      path: currentUri.slice(splitIndex + 3).replace(/^\/+/, ""),
    };
  },
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
  if (remainder.length === 0 || remainder.length <= 3) {
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
  splitUri?: VisualSearchNormalizationDeps["splitUri"],
): number {
  const classification = classifyVisualSearchResult(uri, rawSnippet, mapping, visualConfig, splitUri);
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
  if (/#\s*Memory Palace Namespace\b/iu.test(rawSnippet) || /namespace_uri:\s*/iu.test(rawSnippet) || /Container node for (reflection|capture|profile) records\./iu.test(rawSnippet)) {
    return score - 0.35;
  }
  return score;
}

export function buildVisualMemoryUri(
  mediaRef: string,
  observedAt?: string,
  domain = "core",
  pathPrefix = "visual",
): string {
  const date = observedAt ? new Date(observedAt) : new Date();
  const safeDate = Number.isNaN(date.getTime()) ? new Date() : date;
  const y = String(safeDate.getUTCFullYear());
  const m = String(safeDate.getUTCMonth() + 1).padStart(2, "0");
  const d = String(safeDate.getUTCDate()).padStart(2, "0");
  const hash = createHash("sha256").update(mediaRef).digest("hex").slice(0, 12);
  const normalizedPathPrefix = normalizeVisualPathPrefix(pathPrefix);
  return `${domain}://${normalizedPathPrefix}/${y}/${m}/${d}/sha256-${hash}`;
}

function sanitizeVisualFieldValue(value: string | undefined, fallback: string): string {
  const trimmed = value?.trim();
  if (!trimmed) {
    return fallback;
  }
  return (redactVisualSensitiveText(trimmed) ?? trimmed)
    .replace(/\r\n?/g, "\n")
    .split("\n")
    .map((entry) => entry.trim())
    .filter(Boolean)
    .join(" \\n ");
}

function sanitizeVisualEntities(values?: string[]): string {
  const sanitized = (values ?? []).map((entry) => sanitizeVisualFieldValue(entry, "")).filter(Boolean);
  return sanitized.length > 0 ? sanitized.join(", ") : "(none)";
}

function formatVisualFieldSource(source: VisualFieldSource | undefined): string {
  return source ?? "missing";
}

function displayVisualField(
  value: string | undefined,
  fallback: string,
  source: VisualFieldSource | undefined,
): string {
  if (source === "policy_disabled") {
    return "(policy-disabled)";
  }
  return sanitizeVisualFieldValue(value, fallback);
}

export function buildVisualMemoryContent(input: {
  mediaRef: string;
  summary: string;
  sourceChannel?: string;
  observedAt?: string;
  ocr?: string;
  scene?: string;
  whyRelevant?: string;
  confidence?: number;
  entities?: string[];
  maxSummaryChars?: number;
  maxOcrChars?: number;
  duplicatePolicy?: VisualDuplicatePolicy;
  disclosure?: string;
  retentionNote?: string;
  provenance?: {
    storedVia?: string;
    storedAt?: string;
    mediaRefHash?: string;
    recordUri?: string;
  };
  fieldSources?: Partial<Record<"summary" | "ocr" | "scene" | "entities" | "whyRelevant", VisualFieldSource>>;
  runtimeSource?: RuntimeVisualSource;
  runtimeProbe?: RuntimeVisualProbe;
}): string {
  const observedAt = input.observedAt ? new Date(input.observedAt) : new Date();
  const safeObservedAt = Number.isNaN(observedAt.getTime()) ? new Date().toISOString() : observedAt.toISOString();
  const storedAt = sanitizeVisualFieldValue(input.provenance?.storedAt, new Date().toISOString());
  const mediaRefHash = sanitizeVisualFieldValue(
    input.provenance?.mediaRefHash,
    `sha256-${createHash("sha256").update(input.mediaRef).digest("hex").slice(0, 12)}`,
  );
  return [
    "# Visual Memory",
    "",
    "- kind: visual-memory",
    `- media_ref: ${sanitizeVisualFieldValue(input.mediaRef, "(unknown)")}`,
    `- source_channel: ${sanitizeVisualFieldValue(input.sourceChannel, "unknown")}`,
    `- observed_at: ${safeObservedAt}`,
    `- summary: ${truncateWithEllipsis(displayVisualField(input.summary, "(none)", input.fieldSources?.summary), input.maxSummaryChars ?? Number.MAX_SAFE_INTEGER)}`,
    `- ocr: ${truncateWithEllipsis(displayVisualField(input.ocr, "(none)", input.fieldSources?.ocr), input.maxOcrChars ?? Number.MAX_SAFE_INTEGER)}`,
    `- entities: ${input.fieldSources?.entities === "policy_disabled" ? "(policy-disabled)" : sanitizeVisualEntities(input.entities)}`,
    `- scene: ${displayVisualField(input.scene, "(unknown)", input.fieldSources?.scene)}`,
    `- why_relevant: ${displayVisualField(input.whyRelevant, "(unspecified)", input.fieldSources?.whyRelevant)}`,
    `- confidence: ${typeof input.confidence === "number" && Number.isFinite(input.confidence) ? input.confidence.toFixed(2) : "0.50"}`,
    `- duplicate_policy: ${input.duplicatePolicy ?? "merge"}`,
    `- disclosure: ${sanitizeVisualFieldValue(input.disclosure, DEFAULT_VISUAL_MEMORY_DISCLOSURE)}`,
    `- retention_note: ${sanitizeVisualFieldValue(input.retentionNote, DEFAULT_VISUAL_MEMORY_RETENTION_NOTE)}`,
    `- provenance_source: ${sanitizeVisualFieldValue(input.provenance?.storedVia, "openclaw.memory_store_visual")}`,
    `- provenance_stored_at: ${storedAt}`,
    `- provenance_media_ref_sha256: ${mediaRefHash}`,
    `- provenance_summary_source: ${formatVisualFieldSource(input.fieldSources?.summary)}`,
    `- provenance_ocr_source: ${formatVisualFieldSource(input.fieldSources?.ocr)}`,
    `- provenance_scene_source: ${formatVisualFieldSource(input.fieldSources?.scene)}`,
    `- provenance_entities_source: ${formatVisualFieldSource(input.fieldSources?.entities)}`,
    `- provenance_why_relevant_source: ${formatVisualFieldSource(input.fieldSources?.whyRelevant)}`,
    `- provenance_runtime_source: ${input.runtimeSource ?? "none"}`,
    `- provenance_runtime_probe: ${input.runtimeProbe ?? "none"}`,
    ...(input.provenance?.recordUri
      ? [`- provenance_record_uri: ${sanitizeVisualFieldValue(input.provenance.recordUri, "(unknown)")}`]
      : []),
  ].join("\n");
}

function normalizeSearchSource(uri: string | undefined): SearchSource {
  return typeof uri === "string" && /^sessions?:\/\//i.test(uri) ? "sessions" : "memory";
}

function normalizeSearchResult(
  entry: unknown,
  mapping: PluginConfig["mapping"],
  visualConfig: Pick<PluginConfig["visualMemory"], "pathPrefix"> | undefined,
  deps: VisualSearchNormalizationDeps,
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
  const pathValue = uri ? deps.uriToVirtualPath(uri, mapping) : `${mapping.virtualRoot}/unknown.md`;
  const rawSnippet = readString(entry.snippet) ?? readString(entry.text) ?? "";
  const snippet = normalizeVisualSnippet(rawSnippet);
  const charRange = deps.parseCharRange(entry.char_range);
  const scores = isRecord(entry.scores) ? entry.scores : {};
  const memoryId =
    readFlexibleNumber(entry.memory_id) ??
    (typeof entry.memory_id === "number" ? entry.memory_id : undefined) ??
    readFlexibleNumber(entry.id) ??
    (typeof entry.id === "number" ? entry.id : undefined);
  const score =
    readFlexibleNumber(scores.final) ??
    (typeof scores.final === "number" ? scores.final : undefined) ??
    (typeof entry.score === "number" ? entry.score : undefined) ??
    0;
  const adjustedScore = adjustStructuredNamespaceScore(
    adjustVisualSearchScore(score, uri, rawSnippet, mapping, visualConfig, deps.splitUri),
    rawSnippet,
  );

  return {
    path: pathValue,
    startLine: 1,
    endLine: deps.countLines(snippet),
    score: adjustedScore,
    snippet,
    source: normalizeSearchSource(uri),
    ...(typeof memoryId === "number" ? { memoryId } : {}),
    citation: uri ? deps.uriToVirtualPath(uri, mapping) : pathValue,
    ...(charRange ? { charRange } : {}),
  };
}

export function buildUnavailableSearchResult(error: unknown) {
  const message = formatError(error);
  return {
    results: [],
    disabled: true,
    unavailable: true,
    error: message,
    warning: "Memory Palace memory search is unavailable.",
    action: "Check the configured stdio/sse transport and retry memory_search.",
  };
}

export function normalizeVisualSearchPayload(
  raw: unknown,
  mapping: PluginConfig["mapping"],
  deps: VisualSearchNormalizationDeps,
  visualConfig?: Pick<PluginConfig["visualMemory"], "pathPrefix">,
) {
  const payload = deps.unwrapResultRecord(raw);
  const payloadError =
    payload.ok === false
      ? readString(payload.error) ?? readString(payload.message) ?? readString(payload.reason) ?? "search_failed"
      : undefined;
  const results = Array.isArray(payload.results)
    ? payload.results
        .map((entry) => normalizeSearchResult(entry, mapping, visualConfig, deps))
        .filter((entry): entry is MemorySearchResult => Boolean(entry))
    : [];

  return {
    results,
    provider: "memory-palace",
    model: readString(payload.intent_profile) ?? readString(payload.mode_applied) ?? undefined,
    mode: readString(payload.mode_applied) ?? readString(payload.mode_requested) ?? undefined,
    degraded: payload.degraded === true || Boolean(payloadError),
    backendMethod: readString(payload.backend_method),
    intent: readString(payload.intent),
    strategyTemplate: readString(payload.strategy_template),
    ...(payloadError
      ? {
          disabled: payload.disabled === true || true,
          unavailable: payload.unavailable === true,
          error: payloadError,
        }
      : {}),
    raw: payload,
  };
}

export function buildVisualNamespaceContent(domain: string, segments: string[]): string {
  const namespaceUri = `${domain}://${segments.join("/")}`;
  const hierarchy = segments.join(" > ");
  if (segments.length === 1) {
    return [
      "# Visual Namespace Container",
      `Namespace URI: ${namespaceUri}`,
      `Hierarchy: ${hierarchy}`,
      "Purpose: group image-derived memories by capture date.",
      "Kind: internal namespace container",
    ].join("\n");
  }
  if (segments.length === 2) {
    return [
      "# Visual Namespace Container",
      `Namespace URI: ${namespaceUri}`,
      `Hierarchy: ${hierarchy}`,
      `Calendar Scope: year ${segments[1]}`,
      "Purpose: group image-derived memories by month.",
      "Kind: internal namespace container",
    ].join("\n");
  }
  if (segments.length === 3) {
    return [
      "# Visual Namespace Container",
      `Namespace URI: ${namespaceUri}`,
      `Hierarchy: ${hierarchy}`,
      `Calendar Scope: ${segments[1]}-${segments[2]}`,
      "Purpose: group image-derived memories by day.",
      "Kind: internal namespace container",
    ].join("\n");
  }
  if (segments.length === 4) {
    return [
      "# Visual Namespace Container",
      `Namespace URI: ${namespaceUri}`,
      `Hierarchy: ${hierarchy}`,
      `Calendar Scope: ${segments[1]}-${segments[2]}-${segments[3]}`,
      "Purpose: store image-derived memories captured on this date.",
      "Kind: internal namespace container",
    ].join("\n");
  }
  return [
    "# Visual Namespace Container",
    `Namespace URI: ${namespaceUri}`,
    `Hierarchy: ${hierarchy}`,
    "Purpose: group image-derived memories.",
    "Kind: internal namespace container",
  ].join("\n");
}

export function buildVisualNamespaceRetryContent(
  domain: string,
  segments: string[],
  currentUri: string,
): string {
  const namespaceHash = createHash("sha256").update(currentUri).digest("hex").slice(0, 12);
  const parentSegments = segments.slice(0, -1);
  const parentUri = parentSegments.length > 0 ? `${domain}://${parentSegments.join("/")}` : `${domain}://`;
  const bucketType =
    segments.length === 1 ? "root" : segments.length === 2 ? "year" : segments.length === 3 ? "month" : segments.length === 4 ? "day" : "branch";
  return [
    "visual_namespace_container: true",
    `namespace_uri: ${currentUri}`,
    `parent_uri: ${parentUri}`,
    `bucket_type: ${bucketType}`,
    `segment_value: ${segments[segments.length - 1]}`,
    `segment_depth: ${segments.length}`,
    `namespace_key: ${namespaceHash}`,
    `uniqueness_token: visual-namespace-${segments.join("-")}-${namespaceHash}`,
    "merge_policy: never merge with parent or sibling namespace containers",
    "purpose: internal structural node for dated visual-memory hierarchy",
    `distinction_note: this ${bucketType} container is distinct from parent ${parentUri} and from sibling containers`,
  ].join("\n");
}

export function buildVisualNamespaceMachineTagContent(
  domain: string,
  segments: string[],
  currentUri: string,
): string {
  const namespaceHash = createHash("sha256").update(currentUri).digest("hex").slice(0, 12);
  const parentSegments = segments.slice(0, -1);
  const parentUri = parentSegments.length > 0 ? `${domain}://${parentSegments.join("/")}` : `${domain}://`;
  const bucketType =
    segments.length === 1 ? "root" : segments.length === 2 ? "year" : segments.length === 3 ? "month" : segments.length === 4 ? "day" : "branch";
  return [
    `VISUAL_NS_URI=${currentUri}`,
    `VISUAL_NS_PARENT=${parentUri}`,
    `VISUAL_NS_BUCKET=${bucketType}`,
    `VISUAL_NS_SEGMENT=${segments[segments.length - 1]}`,
    `VISUAL_NS_DEPTH=${segments.length}`,
    `VISUAL_NS_TOKEN=${namespaceHash}`,
    "VISUAL_NS_ROLE=REAL_PARENT_NODE",
    "VISUAL_NS_MERGE=FORBID_PARENT_SIBLING_COLLAPSE",
  ].join("\n");
}

export function buildVisualNamespaceForceBarrierContent(
  domain: string,
  segments: string[],
  currentUri: string,
): string {
  const namespaceHash = createHash("sha256").update(`force:${currentUri}`).digest("hex").slice(0, 16);
  const parentSegments = segments.slice(0, -1);
  const parentUri = parentSegments.length > 0 ? `${domain}://${parentSegments.join("/")}` : `${domain}://`;
  const visibleContent = [
    "# Visual Namespace Container",
    "visual_namespace_container: true",
    `namespace_uri: ${currentUri}`,
    `parent_uri: ${parentUri}`,
  ].join("\n");
  return appendForceControlBlock(visibleContent, [
    `VISUAL_NS_FORCE_URI=${currentUri}`,
    `VISUAL_NS_FORCE_PARENT=${parentUri}`,
    `VISUAL_NS_FORCE_PATH=${segments.join("::")}`,
    `VISUAL_NS_FORCE_SEGMENT=${segments[segments.length - 1]}`,
    `VISUAL_NS_FORCE_HASH=${namespaceHash}`,
    "VISUAL_NS_FORCE_REASON=SEPARATE_NAMESPACE_CONTAINER",
    "VISUAL_NS_FORCE_RULE=NO_DEDUP_WITH_PARENT_OR_SIBLING",
    `VISUAL_NS_FORCE_MARKER=${domain.toUpperCase()}::${segments.join("::")}::${namespaceHash}`,
  ]);
}

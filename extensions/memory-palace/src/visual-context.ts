import { fileURLToPath } from "node:url";
import type { OpenClawPluginToolContext } from "openclaw/plugin-sdk/core";
import type {
  JsonRecord,
  PluginConfig,
  RuntimeVisualProbe,
  RuntimeVisualSource,
  VisualDuplicatePolicy,
  VisualFieldProvider,
  VisualFieldSource,
} from "./types.js";
import {
  cleanMessageTextForReasoning as cleanMessageTextForReasoningShared,
  extractMessageTexts as extractNormalizedMessageTexts,
  getParam,
  isRecord,
  normalizeTextPreservingLines,
  parseJsonRecord,
  readFlexibleNumber,
  readLooseStringArray,
  readString,
  readStringArray,
  stripInjectedMemoryPromptBlocks,
  truncateWithEllipsis,
} from "./utils.js";
import {
  looksLikeImageMediaRef,
  normalizeVisualPayload,
  redactVisualSensitiveText,
  sanitizeVisualMediaRef,
} from "./visual-redaction.js";

export type VisualContextPayload = {
  mediaRef?: string;
  summary?: string;
  sourceChannel?: string;
  observedAt?: string;
  ocr?: string;
  scene?: string;
  whyRelevant?: string;
  confidence?: number;
  entities?: string[];
  runtimeSource?: RuntimeVisualSource;
};

export type ResolvedVisualInput = VisualContextPayload & {
  mediaRef: string;
  summary: string;
  fieldSources: Record<"summary" | "ocr" | "scene" | "entities" | "whyRelevant", VisualFieldSource>;
  runtimeProbe: RuntimeVisualProbe;
};

type VisualTurnCacheEntry = {
  expiresAt: number;
  updatedAt: number;
  payloads: VisualContextPayload[];
};

const visualTurnContextCache = new Map<string, VisualTurnCacheEntry>();
const VISUAL_CONTEXT_CONTAINER_KEYS = [
  "messages",
  "message",
  "content",
  "attachments",
  "items",
  "blocks",
  "parts",
  "input",
  "inputs",
  "currentTurn",
  "latestMessage",
  "request",
  "invocation",
  "payload",
  "data",
  "event",
] as const;
const VISUAL_MEDIA_REF_KEYS = [
  "mediaRef",
  "media_ref",
  "mediaPath",
  "media_path",
  "mediaUrl",
  "media_url",
  "imageUrl",
  "image_url",
  "url",
  "uri",
  "src",
  "path",
  "file",
  "media",
  "MediaPath",
  "MediaUrl",
] as const;
const VISUAL_SUMMARY_KEYS = ["summary", "caption", "alt", "description"] as const;
const VISUAL_OCR_KEYS = ["ocr", "ocrText", "recognizedText"] as const;
const VISUAL_SCENE_KEYS = ["scene", "label"] as const;
const VISUAL_ENTITY_KEYS = ["entities", "objects", "people", "labels"] as const;
const VISUAL_BODY_FOR_AGENT_KEYS = [
  "bodyForAgent",
  "body_for_agent",
  "body",
  "body_for_model",
] as const;
const VISUAL_SOURCE_CHANNEL_KEYS = [
  "sourceChannel",
  "source_channel",
  "messageChannel",
  "message_channel",
  "channel",
] as const;
const VISUAL_OBSERVED_AT_KEYS = [
  "observedAt",
  "observed_at",
  "createdAt",
  "created_at",
  "timestamp",
] as const;
const VISUAL_TURN_CACHE_MAX_KEYS = 256;
const MAX_VISUAL_TRAVERSAL_DEPTH = 6;
const MAX_VISUAL_TRAVERSAL_NODES = 1000;

type TraversalBudget = {
  remaining: number;
  seen: WeakSet<object>;
};

function pruneVisualTurnContextCache(now = Date.now()): void {
  for (const [key, entry] of visualTurnContextCache.entries()) {
    if (entry.expiresAt <= now) {
      visualTurnContextCache.delete(key);
    }
  }
  if (visualTurnContextCache.size <= VISUAL_TURN_CACHE_MAX_KEYS) {
    return;
  }
  const overflow = visualTurnContextCache.size - VISUAL_TURN_CACHE_MAX_KEYS;
  const staleEntries = Array.from(visualTurnContextCache.entries())
    .sort((left, right) => left[1].updatedAt - right[1].updatedAt)
    .slice(0, overflow);
  for (const [key] of staleEntries) {
    visualTurnContextCache.delete(key);
  }
}

function cleanMessageTextForReasoning(text: string): string {
  return cleanMessageTextForReasoningShared(text, {
    preprocessText: stripInjectedMemoryPromptBlocks,
    normalizeText: normalizeTextPreservingLines,
  });
}

function extractMessageTexts(messages: unknown[], allowedRoles?: string[]): string[] {
  return extractNormalizedMessageTexts(messages, {
    allowedRoles,
    cleanText: cleanMessageTextForReasoning,
  });
}

function normalizeFriendlyWindowsLocalPath(localPath: string): string {
  return /^\/[A-Za-z]:(?:[\\/]|$)/u.test(localPath) ? localPath.slice(1) : localPath;
}

function decodeVisualFileUrlPathname(pathname: string): string {
  try {
    return decodeURIComponent(pathname);
  } catch {
    return pathname;
  }
}

function resolveVisualFileUrlFallbackPath(mediaRef: string): string {
  const stripped = normalizeFriendlyWindowsLocalPath(mediaRef.replace(/^file:/i, ""));
  try {
    const parsed = new URL(mediaRef);
    if (parsed.protocol !== "file:") {
      return stripped;
    }
    const pathname = normalizeFriendlyWindowsLocalPath(
      decodeVisualFileUrlPathname(parsed.pathname),
    );
    if (parsed.hostname && parsed.hostname !== "localhost") {
      return `//${parsed.hostname}${pathname}`;
    }
    return pathname || stripped;
  } catch {
    return stripped;
  }
}

export function normalizeVisualPathPrefix(value: string | undefined, fallback = "visual"): string {
  const normalized = (value ?? "").trim().replace(/^\/+|\/+$/g, "");
  return normalized || fallback;
}

export function readVisualDuplicatePolicy(value: unknown): VisualDuplicatePolicy | undefined {
  const normalized = readString(value);
  return normalized === "merge" || normalized === "reject" || normalized === "new"
    ? normalized
    : undefined;
}

export function hasVisualPayloadData(payload: VisualContextPayload | undefined): boolean {
  if (!payload) {
    return false;
  }
  return Boolean(
    payload.mediaRef ||
      payload.summary ||
      payload.ocr ||
      payload.scene ||
      payload.whyRelevant ||
      payload.entities?.length,
  );
}

export function hasCliVisualPayloadData(payload: VisualContextPayload | undefined): boolean {
  if (!payload) {
    return false;
  }
  return Boolean(payload.summary || payload.ocr || payload.scene || payload.whyRelevant || payload.entities?.length);
}

export function collapseRuntimeVisualProbe(
  source: RuntimeVisualSource | undefined,
): RuntimeVisualProbe {
  if (!source) {
    return "none";
  }
  if (source === "message_preprocessed") {
    return "message_preprocessed";
  }
  return "tool_context_only";
}

function runtimeVisualSourceRank(source: RuntimeVisualSource | undefined): number {
  if (source === "message_preprocessed") {
    return 4;
  }
  if (source === "before_prompt_build") {
    return 3;
  }
  if (source === "agent_end") {
    return 2;
  }
  if (source === "tool_context_only") {
    return 1;
  }
  return 0;
}

function scoreVisualPayload(payload: VisualContextPayload | undefined): number {
  if (!payload) {
    return -1;
  }
  return [
    payload.mediaRef,
    payload.summary,
    payload.ocr,
    payload.scene,
    payload.whyRelevant,
    payload.sourceChannel,
    payload.observedAt,
    payload.entities?.length ? "entities" : undefined,
  ].filter(Boolean).length;
}

function mergeVisualPayload(
  existing: VisualContextPayload | undefined,
  incoming: VisualContextPayload,
): VisualContextPayload {
  if (!existing) {
    return incoming;
  }
  const preferred = scoreVisualPayload(incoming) >= scoreVisualPayload(existing) ? incoming : existing;
  const fallback = preferred === incoming ? existing : incoming;
  const preferredRuntimeSource =
    runtimeVisualSourceRank(incoming.runtimeSource) >= runtimeVisualSourceRank(existing.runtimeSource)
      ? incoming.runtimeSource
      : existing.runtimeSource;
  return {
    mediaRef: preferred.mediaRef ?? fallback.mediaRef,
    summary: preferred.summary ?? fallback.summary,
    sourceChannel: preferred.sourceChannel ?? fallback.sourceChannel,
    observedAt: preferred.observedAt ?? fallback.observedAt,
    ocr: preferred.ocr ?? fallback.ocr,
    scene: preferred.scene ?? fallback.scene,
    whyRelevant: preferred.whyRelevant ?? fallback.whyRelevant,
    confidence: preferred.confidence ?? fallback.confidence,
    entities: preferred.entities ?? fallback.entities,
    runtimeSource: preferredRuntimeSource,
  };
}

function getVisualParam(params: Record<string, unknown>, key: string): unknown {
  const direct = getParam(params, key);
  if (direct !== undefined) {
    return direct;
  }
  const pascal = key.charAt(0).toUpperCase() + key.slice(1);
  if (pascal !== key && Object.hasOwn(params, pascal)) {
    return params[pascal];
  }
  const normalizedKey = key.replace(/[_-]/g, "").toLowerCase();
  for (const [entryKey, entryValue] of Object.entries(params)) {
    if (entryKey.replace(/[_-]/g, "").toLowerCase() === normalizedKey) {
      return entryValue;
    }
  }
  return undefined;
}

function pickFirstVisualParam(raw: Record<string, unknown>, keys: readonly string[]): unknown {
  for (const key of keys) {
    const value = getVisualParam(raw, key);
    if (value !== undefined) {
      return value;
    }
  }
  return undefined;
}

function readVisualJoinedText(value: unknown): string | undefined {
  if (typeof value === "string") {
    return readString(value);
  }
  if (Array.isArray(value)) {
    const parts = value
      .map((entry) => readVisualJoinedText(entry))
      .filter((entry): entry is string => Boolean(entry));
    return parts.length > 0 ? parts.join("\n") : undefined;
  }
  if (!isRecord(value)) {
    return undefined;
  }
  return (
    readString(getVisualParam(value, "text")) ??
    readString(getVisualParam(value, "bodyForAgent")) ??
    readString(getVisualParam(value, "body_for_agent")) ??
    readString(getVisualParam(value, "description")) ??
    readString(getVisualParam(value, "caption"))
  );
}

function readVisualMediaRef(value: unknown): string | undefined {
  if (typeof value === "string") {
    return sanitizeVisualMediaRef(value);
  }
  if (Array.isArray(value)) {
    for (const entry of value) {
      const resolved = readVisualMediaRef(entry);
      if (resolved) {
        return resolved;
      }
    }
    return undefined;
  }
  if (!isRecord(value)) {
    return undefined;
  }
  for (const key of ["url", "uri", "src", "path", "file", "mediaRef", "media_ref"]) {
    const resolved = readVisualMediaRef(getVisualParam(value, key));
    if (resolved) {
      return resolved;
    }
  }
  return undefined;
}

function extractVisualBodyHints(
  text: string | undefined,
  options?: { allowStructuredLabels?: boolean },
): Partial<VisualContextPayload> {
  const normalized = redactVisualSensitiveText(readString(text));
  if (!normalized) {
    return {};
  }
  const canonicalText = normalized.replace(/\s*(?:\\r)?\\+n\s*/g, "\n");
  const shouldParseStructuredLabels =
    options?.allowStructuredLabels === true ||
    /#\s*visual memory\b|kind:\s*visual-memory\b/iu.test(canonicalText) ||
    /(?:^|\n)\s*(?:media path|media_path|mediapath|media url|media_url|mediaurl)\s*[:=-]\s*/iu.test(
      canonicalText,
    );
  let mediaRef: string | undefined;
  let summary: string | undefined;
  let ocr: string | undefined;
  let scene: string | undefined;
  let whyRelevant: string | undefined;
  let sourceChannel: string | undefined;
  let observedAt: string | undefined;
  const entities = new Set<string>();
  const unlabeledLines: string[] = [];

  for (const rawLine of canonicalText.split(/\r?\n+/)) {
    const line = rawLine.replace(/^[-*]\s*/, "").trim();
    if (!line) {
      continue;
    }
    let matched: RegExpMatchArray | null = null;
    if (
      shouldParseStructuredLabels &&
      (matched = line.match(/^(?:summary|caption|description)\s*[:=-]\s*(.+)$/iu))
    ) {
      summary = matched[1].trim();
      continue;
    }
    if (
      (matched = line.match(
        /^(?:media path|media_path|mediapath|media url|media_url|mediaurl)\s*[:=-]\s*(.+)$/iu,
      ))
    ) {
      mediaRef = sanitizeVisualMediaRef(matched[1].trim());
      continue;
    }
    if (
      shouldParseStructuredLabels &&
      (matched = line.match(/^(?:ocr|ocr text|recognized text)\s*[:=-]\s*(.+)$/iu))
    ) {
      ocr = matched[1].trim();
      continue;
    }
    if (
      shouldParseStructuredLabels &&
      (matched = line.match(/^(?:scene|label)\s*[:=-]\s*(.+)$/iu))
    ) {
      scene = matched[1].trim();
      continue;
    }
    if (
      shouldParseStructuredLabels &&
      (matched = line.match(/^(?:entities|people|objects|labels)\s*[:=-]\s*(.+)$/iu))
    ) {
      for (const entry of matched[1].split(/[,;]\s*/)) {
        const cleaned = redactVisualSensitiveText(entry.trim());
        if (cleaned) {
          entities.add(cleaned);
        }
      }
      continue;
    }
    if (
      shouldParseStructuredLabels &&
      (matched = line.match(/^(?:why relevant|why_relevant|rationale)\s*[:=-]\s*(.+)$/iu))
    ) {
      whyRelevant = matched[1].trim();
      continue;
    }
    if (
      shouldParseStructuredLabels &&
      (matched = line.match(/^(?:source channel|source_channel|channel)\s*[:=-]\s*(.+)$/iu))
    ) {
      sourceChannel = matched[1].trim();
      continue;
    }
    if (
      shouldParseStructuredLabels &&
      (matched = line.match(
        /^(?:observed at|observed_at|created at|created_at|timestamp)\s*[:=-]\s*(.+)$/iu,
      ))
    ) {
      observedAt = matched[1].trim();
      continue;
    }
    unlabeledLines.push(line);
  }

  mediaRef ??= sanitizeVisualMediaRef(
    canonicalText.match(
      /(?:^|\n)\s*(?:media path|media_path|mediapath|media url|media_url|mediaurl)\s*[:=-]\s*([^\n]+)/iu,
    )?.[1]?.trim(),
  );
  if (shouldParseStructuredLabels) {
    summary ??= canonicalText.match(/(?:^|\n)\s*(?:summary|caption|description)\s*[:=-]\s*([^\n]+)/iu)?.[1]?.trim();
    ocr ??= canonicalText.match(/(?:^|\n)\s*(?:ocr|ocr text|recognized text)\s*[:=-]\s*([^\n]+)/iu)?.[1]?.trim();
    scene ??= canonicalText.match(/(?:^|\n)\s*(?:scene|label)\s*[:=-]\s*([^\n]+)/iu)?.[1]?.trim();
    whyRelevant ??= canonicalText.match(/(?:^|\n)\s*(?:why relevant|why_relevant|rationale)\s*[:=-]\s*([^\n]+)/iu)?.[1]?.trim();
    sourceChannel ??= canonicalText.match(/(?:^|\n)\s*(?:source channel|source_channel|channel)\s*[:=-]\s*([^\n]+)/iu)?.[1]?.trim();
    observedAt ??= canonicalText.match(/(?:^|\n)\s*(?:observed at|observed_at|created at|created_at|timestamp)\s*[:=-]\s*([^\n]+)/iu)?.[1]?.trim();
  }

  if (shouldParseStructuredLabels && entities.size === 0) {
    const entitiesRaw = canonicalText.match(/(?:^|\n)\s*(?:entities|people|objects|labels)\s*[:=-]\s*([^\n]+)/iu)?.[1];
    if (entitiesRaw) {
      for (const entry of entitiesRaw.split(/[,;]\s*/)) {
        const cleaned = redactVisualSensitiveText(entry.trim());
        if (cleaned) {
          entities.add(cleaned);
        }
      }
    }
  }

  if (!summary && unlabeledLines.length > 0) {
    summary = truncateWithEllipsis(unlabeledLines.join(" "), 240);
  }
  return {
    ...(mediaRef ? { mediaRef } : {}),
    ...(summary ? { summary } : {}),
    ...(ocr ? { ocr } : {}),
    ...(scene ? { scene } : {}),
    ...(whyRelevant ? { whyRelevant } : {}),
    ...(sourceChannel ? { sourceChannel } : {}),
    ...(observedAt ? { observedAt } : {}),
    ...(entities.size > 0 ? { entities: Array.from(entities) } : {}),
  };
}

function containsStructuredVisualLabel(text: string | undefined): boolean {
  const normalized = readString(text);
  if (!normalized) {
    return false;
  }
  return /\b(summary|caption|description|ocr|scene|entities|why relevant|source channel|observed at)\s*[:=-]/i.test(
    normalized,
  );
}

function humanizeMediaRef(mediaRef: string | undefined): string | undefined {
  if (!mediaRef) {
    return undefined;
  }
  const normalized = mediaRef
    .replace(/^file:/i, "")
    .split(/[/?#]/)
    .filter(Boolean)
    .pop()
    ?.replace(/\.[a-z0-9]{2,8}$/i, "")
    .replace(/[-_]+/g, " ")
    .trim();
  return normalized || undefined;
}

function looksLikeImageRef(value: string | undefined): boolean {
  const normalized = readString(value)?.toLowerCase();
  if (!normalized) {
    return false;
  }
  return (
    normalized.startsWith("data:image/") ||
    /\.(png|jpe?g|webp|gif|bmp|svg|heic|heif)(?:[?#].*)?$/i.test(normalized)
  );
}

function isImageLikeRecord(raw: JsonRecord): boolean {
  const type = readString(raw.type)?.toLowerCase();
  const directMediaRef =
    readVisualMediaRef(pickFirstVisualParam(raw, VISUAL_MEDIA_REF_KEYS)) ??
    readVisualMediaRef(raw);
  const bodyHints = extractVisualBodyHints(
    readVisualJoinedText(pickFirstVisualParam(raw, VISUAL_BODY_FOR_AGENT_KEYS)),
    {
      allowStructuredLabels:
        Boolean(directMediaRef) ||
        Boolean(type && /(image|photo|screenshot|attachment)/.test(type)),
    },
  );
  const mimeType =
    readString(getVisualParam(raw, "mimeType")) ??
    readString(getVisualParam(raw, "mime_type")) ??
    readString(getVisualParam(raw, "contentType")) ??
    readString(getVisualParam(raw, "content_type"));
  if (type && /(image|photo|screenshot|attachment)/.test(type)) {
    return true;
  }
  if (mimeType && /^image\//i.test(mimeType)) {
    return true;
  }
  return (
    type === "mediapath" ||
    type === "mediaurl" ||
    Boolean(bodyHints.mediaRef && looksLikeImageRef(bodyHints.mediaRef)) ||
    Boolean(directMediaRef && looksLikeImageRef(directMediaRef))
  );
}

function pickFirstVisualString(raw: JsonRecord, keys: readonly string[]): string | undefined {
  for (const key of keys) {
    const value = readVisualJoinedText(getVisualParam(raw, key));
    if (value) {
      return redactVisualSensitiveText(value) ?? value;
    }
  }
  return undefined;
}

function pickFirstVisualArray(raw: JsonRecord, keys: readonly string[]): string[] | undefined {
  for (const key of keys) {
    const values = readLooseStringArray(getVisualParam(raw, key));
    if (values && values.length > 0) {
      return values.map((entry) => redactVisualSensitiveText(entry) ?? entry);
    }
  }
  return undefined;
}

function scoreVisualContextCandidate(payload: VisualContextPayload | undefined): number {
  if (!payload) {
    return -1;
  }
  let score = scoreVisualPayload(payload) * 10;
  if (containsStructuredVisualLabel(payload.summary)) {
    score -= 3;
  }
  return score;
}

function extractVisualPayloadFromRecord(
  raw: JsonRecord,
  summaryFallback?: string,
): VisualContextPayload | undefined {
  const directMediaRef =
    readVisualMediaRef(pickFirstVisualParam(raw, VISUAL_MEDIA_REF_KEYS)) ??
    readVisualMediaRef(raw);
  const type = readString(raw.type)?.toLowerCase();
  const mimeType =
    readString(getVisualParam(raw, "mimeType")) ??
    readString(getVisualParam(raw, "mime_type")) ??
    readString(getVisualParam(raw, "contentType")) ??
    readString(getVisualParam(raw, "content_type"));
  const bodyHints = extractVisualBodyHints(
    readVisualJoinedText(pickFirstVisualParam(raw, VISUAL_BODY_FOR_AGENT_KEYS)),
    {
      allowStructuredLabels:
        Boolean(directMediaRef) ||
        Boolean(type && /(image|photo|screenshot|attachment)/.test(type)) ||
        Boolean(mimeType && /^image\//i.test(mimeType)),
    },
  );
  if (!isImageLikeRecord(raw)) {
    return undefined;
  }
  const mediaRef = directMediaRef ?? bodyHints.mediaRef;
  if (!mediaRef) {
    return undefined;
  }
  const summary = pickFirstVisualString(raw, VISUAL_SUMMARY_KEYS) ?? bodyHints.summary ?? summaryFallback;
  return {
    mediaRef,
    summary,
    sourceChannel: pickFirstVisualString(raw, VISUAL_SOURCE_CHANNEL_KEYS) ?? bodyHints.sourceChannel,
    observedAt: pickFirstVisualString(raw, VISUAL_OBSERVED_AT_KEYS) ?? bodyHints.observedAt,
    ocr: pickFirstVisualString(raw, VISUAL_OCR_KEYS) ?? bodyHints.ocr,
    scene: pickFirstVisualString(raw, VISUAL_SCENE_KEYS) ?? bodyHints.scene,
    whyRelevant:
      pickFirstVisualString(raw, ["whyRelevant", "why_relevant", "rationale"]) ??
      bodyHints.whyRelevant,
    confidence: readFlexibleNumber(getVisualParam(raw, "confidence")),
    entities: pickFirstVisualArray(raw, VISUAL_ENTITY_KEYS) ?? bodyHints.entities,
  };
}

function consumeTraversalBudget(budget: TraversalBudget): boolean {
  if (budget.remaining <= 0) {
    return false;
  }
  budget.remaining -= 1;
  return true;
}

function shouldVisitTraversalNode(node: object, budget: TraversalBudget): boolean {
  if (budget.seen.has(node)) {
    return false;
  }
  if (!consumeTraversalBudget(budget)) {
    return false;
  }
  budget.seen.add(node);
  return true;
}

function collectVisualPayloadsFromUnknown(
  value: unknown,
  summaryFallback?: string,
  depth = 0,
  budget: TraversalBudget = {
    remaining: MAX_VISUAL_TRAVERSAL_NODES,
    seen: new WeakSet<object>(),
  },
): VisualContextPayload[] {
  if (depth > MAX_VISUAL_TRAVERSAL_DEPTH) {
    return [];
  }
  if (Array.isArray(value)) {
    if (!shouldVisitTraversalNode(value, budget)) {
      return [];
    }
    return value.flatMap((entry) =>
      collectVisualPayloadsFromUnknown(entry, summaryFallback, depth + 1, budget)
    );
  }
  if (!isRecord(value)) {
    return [];
  }
  if (!shouldVisitTraversalNode(value, budget)) {
    return [];
  }
  const payloads: VisualContextPayload[] = [];
  const directPayload = extractVisualPayloadFromRecord(value, summaryFallback);
  if (directPayload) {
    payloads.push(directPayload);
  }
  for (const key of VISUAL_CONTEXT_CONTAINER_KEYS) {
    const child = getParam(value, key);
    if (child !== undefined) {
      payloads.push(...collectVisualPayloadsFromUnknown(child, summaryFallback, depth + 1, budget));
    }
  }
  return payloads;
}

export function extractVisualContextFromMessages(messages: unknown[]): VisualContextPayload {
  const assistantSummary = truncateWithEllipsis(
    extractMessageTexts(messages, ["assistant"]).slice(-1)[0] ?? "",
    240,
  );
  return selectVisualContextCandidate(
    collectVisualPayloadsFromUnknown(messages, assistantSummary || undefined),
    undefined,
  );
}

export function extractVisualContextFromToolContext(
  context?: OpenClawPluginToolContext | Record<string, unknown>,
): VisualContextPayload {
  if (!context || !isRecord(context)) {
    return {};
  }
  const configRaw = isRecord(context.config) ? context.config : {};
  const assistantSummary = truncateWithEllipsis(
    extractMessageTexts(
      Array.isArray((context as Record<string, unknown>).messages)
        ? ((context as Record<string, unknown>).messages as unknown[])
        : Array.isArray(configRaw.messages)
          ? (configRaw.messages as unknown[])
          : [],
      ["assistant"],
    ).slice(-1)[0] ?? "",
    240,
  );
  const scopedValue = {
    messages: (context as Record<string, unknown>).messages,
    message: (context as Record<string, unknown>).message,
    attachments: (context as Record<string, unknown>).attachments,
    currentTurn: (context as Record<string, unknown>).currentTurn,
    latestMessage: (context as Record<string, unknown>).latestMessage,
    request: (context as Record<string, unknown>).request,
    invocation: (context as Record<string, unknown>).invocation,
    input: (context as Record<string, unknown>).input,
    inputs: (context as Record<string, unknown>).inputs,
    content: (context as Record<string, unknown>).content,
    config: {
      messages: configRaw.messages,
      attachments: configRaw.attachments,
      input: configRaw.input,
      currentTurn: configRaw.currentTurn,
      latestMessage: configRaw.latestMessage,
    },
  };
  return selectVisualContextCandidate(
    collectVisualPayloadsFromUnknown(scopedValue, assistantSummary || undefined),
    undefined,
  );
}

export function parseVisualContext(value: unknown): VisualContextPayload {
  const raw = isRecord(value) ? value : parseJsonRecord(value) ?? {};
  if (!isRecord(raw)) {
    const summary = readString(value);
    return summary ? { summary: redactVisualSensitiveText(summary) } : {};
  }
  const bodyHints = extractVisualBodyHints(
    readVisualJoinedText(pickFirstVisualParam(raw, VISUAL_BODY_FOR_AGENT_KEYS)),
    { allowStructuredLabels: true },
  );
  return {
    mediaRef: readVisualMediaRef(pickFirstVisualParam(raw, VISUAL_MEDIA_REF_KEYS)) ?? bodyHints.mediaRef,
    summary: redactVisualSensitiveText(
      readVisualJoinedText(pickFirstVisualParam(raw, VISUAL_SUMMARY_KEYS)) ?? bodyHints.summary,
    ),
    sourceChannel:
      readVisualJoinedText(pickFirstVisualParam(raw, VISUAL_SOURCE_CHANNEL_KEYS)) ?? bodyHints.sourceChannel,
    observedAt:
      readVisualJoinedText(pickFirstVisualParam(raw, VISUAL_OBSERVED_AT_KEYS)) ?? bodyHints.observedAt,
    ocr: redactVisualSensitiveText(
      readVisualJoinedText(pickFirstVisualParam(raw, VISUAL_OCR_KEYS)) ?? bodyHints.ocr,
    ),
    scene: redactVisualSensitiveText(
      readVisualJoinedText(pickFirstVisualParam(raw, VISUAL_SCENE_KEYS)) ?? bodyHints.scene,
    ),
    whyRelevant: redactVisualSensitiveText(
      readVisualJoinedText(pickFirstVisualParam(raw, ["whyRelevant", "why_relevant", "rationale"])) ??
        bodyHints.whyRelevant,
    ),
    confidence: readFlexibleNumber(getVisualParam(raw, "confidence")),
    entities:
      readLooseStringArray(pickFirstVisualParam(raw, VISUAL_ENTITY_KEYS))?.map((entry) => redactVisualSensitiveText(entry) ?? entry) ??
      bodyHints.entities,
  };
}

export function extractVisualContextCandidatesFromUnknown(
  value: unknown,
  runtimeSource?: RuntimeVisualSource,
): VisualContextPayload[] {
  const budget: TraversalBudget = {
    remaining: MAX_VISUAL_TRAVERSAL_NODES,
    seen: new WeakSet<object>(),
  };
  const candidates: VisualContextPayload[] = [];
  const messageSource =
    Array.isArray(value)
      ? value
      : isRecord(value) && Array.isArray(value.messages)
        ? value.messages
        : [];
  const assistantTexts = extractMessageTexts(messageSource, ["assistant"]);
  const defaultSummary = assistantTexts.at(-1);

  const pushCandidate = (payload: VisualContextPayload) => {
    const normalized = normalizeVisualPayload(payload);
    if (!normalized.mediaRef && !normalized.summary && !normalized.ocr && !normalized.scene && !normalized.entities?.length) {
      return;
    }
    candidates.push(normalized);
  };

  const visit = (node: unknown, inheritedMediaRef?: string, depth = 0) => {
    if (depth > MAX_VISUAL_TRAVERSAL_DEPTH) {
      return;
    }
    if (Array.isArray(node)) {
      if (!shouldVisitTraversalNode(node, budget)) {
        return;
      }
      for (const item of node) {
        visit(item, inheritedMediaRef, depth + 1);
      }
      return;
    }
    if (!isRecord(node)) {
      return;
    }
    if (!shouldVisitTraversalNode(node, budget)) {
      return;
    }

    const typeValue = readString(node.type)?.toLowerCase();
    const directMediaRef =
      readVisualMediaRef(pickFirstVisualParam(node, VISUAL_MEDIA_REF_KEYS)) ??
      readVisualMediaRef(node);
    const bodyHints = extractVisualBodyHints(
      readVisualJoinedText(pickFirstVisualParam(node, VISUAL_BODY_FOR_AGENT_KEYS)),
      {
        allowStructuredLabels:
          Boolean(directMediaRef) || Boolean(typeValue?.includes("image")),
      },
    );
    const mediaRefCandidate =
      directMediaRef ??
      bodyHints.mediaRef ??
      sanitizeVisualMediaRef(inheritedMediaRef) ??
      undefined;
    const mediaRef =
      mediaRefCandidate && (looksLikeImageMediaRef(mediaRefCandidate) || typeValue?.includes("image"))
        ? mediaRefCandidate
        : inheritedMediaRef;
    const summary =
      readVisualJoinedText(pickFirstVisualParam(node, VISUAL_SUMMARY_KEYS)) ??
      bodyHints.summary ??
      (typeValue?.includes("image") ? readVisualJoinedText(getVisualParam(node, "text")) : undefined) ??
      (mediaRef ? defaultSummary : undefined);
    const candidate: VisualContextPayload = {
      mediaRef,
      summary,
      sourceChannel:
        readVisualJoinedText(pickFirstVisualParam(node, VISUAL_SOURCE_CHANNEL_KEYS)) ??
        bodyHints.sourceChannel,
      observedAt:
        readVisualJoinedText(pickFirstVisualParam(node, VISUAL_OBSERVED_AT_KEYS)) ??
        bodyHints.observedAt,
      ocr: readVisualJoinedText(pickFirstVisualParam(node, VISUAL_OCR_KEYS)) ?? bodyHints.ocr,
      scene: readVisualJoinedText(pickFirstVisualParam(node, VISUAL_SCENE_KEYS)) ?? bodyHints.scene,
      whyRelevant:
        readVisualJoinedText(pickFirstVisualParam(node, ["whyRelevant", "why_relevant", "rationale"])) ??
        bodyHints.whyRelevant,
      confidence: readFlexibleNumber(getVisualParam(node, "confidence")),
      entities:
        readLooseStringArray(pickFirstVisualParam(node, [...VISUAL_ENTITY_KEYS, "tags"])) ??
        bodyHints.entities,
      runtimeSource,
    };
    pushCandidate(candidate);

    for (const entry of Object.values(node)) {
      if (entry !== node) {
        visit(entry, mediaRef, depth + 1);
      }
    }
  };

  visit(value);
  return candidates;
}

export function selectVisualContextCandidate(
  candidates: VisualContextPayload[],
  mediaRef: string | undefined,
): VisualContextPayload {
  if (candidates.length === 0) {
    return {};
  }
  const normalizedMediaRef = sanitizeVisualMediaRef(mediaRef);
  if (normalizedMediaRef) {
    const exact = candidates
      .filter((candidate) => candidate.mediaRef === normalizedMediaRef)
      .reduce<VisualContextPayload | undefined>((best, candidate) => {
        if (!best) {
          return candidate;
        }
        return scoreVisualContextCandidate(candidate) >= scoreVisualContextCandidate(best)
          ? candidate
          : best;
      }, undefined);
    if (exact) {
      return exact;
    }
  }
  return (
    candidates
      .slice()
      .sort((left, right) => scoreVisualContextCandidate(right) - scoreVisualContextCandidate(left))
      .find((candidate) => candidate.mediaRef) ??
    candidates.at(-1) ??
    {}
  );
}

function buildVisualCacheKey(context?: OpenClawPluginToolContext): string | undefined {
  const parts = [
    readString(context?.sessionId)
      ? `session:${readString(context?.sessionId)}`
      : undefined,
    readString(context?.sessionKey)
      ? `session-key:${readString(context?.sessionKey)}`
      : undefined,
    readString(context?.agentId)
      ? `agent:${readString(context?.agentId)}`
      : undefined,
  ].filter((value): value is string => Boolean(value));
  return parts.length > 0 ? `visual:${parts.join("|")}` : undefined;
}

export function getCachedVisualContext(
  context: OpenClawPluginToolContext | undefined,
  mediaRef: string | undefined,
): VisualContextPayload {
  const cacheKey = buildVisualCacheKey(context);
  if (!cacheKey) {
    return {};
  }
  const cached = visualTurnContextCache.get(cacheKey);
  if (!cached) {
    return {};
  }
  if (cached.expiresAt <= Date.now()) {
    visualTurnContextCache.delete(cacheKey);
    return {};
  }
  return selectVisualContextCandidate(cached.payloads, mediaRef);
}

export function rememberVisualContext(
  context: OpenClawPluginToolContext | undefined,
  payload: VisualContextPayload,
  ttlMs: number,
): void {
  rememberVisualContexts(context, [payload], ttlMs);
}

export function rememberVisualContexts(
  context: OpenClawPluginToolContext | undefined,
  payloads: VisualContextPayload[],
  ttlMs: number,
): void {
  const cacheKey = buildVisualCacheKey(context);
  if (!cacheKey) {
    return;
  }
  const now = Date.now();
  pruneVisualTurnContextCache(now);
  const existing =
    visualTurnContextCache.get(cacheKey)?.payloads.filter((payload) => payload.mediaRef || payload.summary) ?? [];
  const nextMap = new Map<string, VisualContextPayload>();
  for (const payload of [...existing, ...payloads]) {
    const normalized = normalizeVisualPayload(payload);
    const key = `${normalized.mediaRef ?? ""}::${normalized.summary ?? ""}::${normalized.ocr ?? ""}`;
    if (!key.replace(/[:]/g, "")) {
      continue;
    }
    nextMap.set(key, mergeVisualPayload(nextMap.get(key), normalized));
  }
  visualTurnContextCache.set(cacheKey, {
    expiresAt: now + Math.max(0, ttlMs),
    updatedAt: now,
    payloads: Array.from(nextMap.values()).slice(-12),
  });
  pruneVisualTurnContextCache(now);
}

export function deriveVisualSummary(input: VisualContextPayload): string | undefined {
  const fromScene = readString(input.scene);
  if (fromScene) {
    return truncateWithEllipsis(fromScene, 160);
  }
  const fromOcr = readString(input.ocr);
  if (fromOcr) {
    return truncateWithEllipsis(fromOcr.replace(/\s+/g, " "), 160);
  }
  const fromMediaRef = humanizeMediaRef(input.mediaRef);
  return fromMediaRef ? `visual capture ${fromMediaRef}` : undefined;
}

export function deriveVisualScene(input: VisualContextPayload): string | undefined {
  const fromSummary = readString(input.summary);
  if (fromSummary) {
    if (containsStructuredVisualLabel(fromSummary)) {
      return undefined;
    }
    return truncateWithEllipsis(fromSummary, 120);
  }
  return humanizeMediaRef(input.mediaRef);
}

export function chooseVisualField<T>(
  direct: T | undefined,
  contextValue: T | undefined,
  runtimeValue: T | undefined,
  cachedValue: T | undefined,
  fallback: T | undefined,
): { value: T | undefined; source: VisualFieldSource; provider: VisualFieldProvider } {
  if (direct !== undefined) {
    return { value: direct, source: "direct", provider: "direct" };
  }
  if (contextValue !== undefined) {
    return { value: contextValue, source: "context", provider: "explicit_context" };
  }
  if (runtimeValue !== undefined) {
    return { value: runtimeValue, source: "context", provider: "runtime_context" };
  }
  if (cachedValue !== undefined) {
    return { value: cachedValue, source: "context", provider: "cached_context" };
  }
  if (fallback !== undefined) {
    return { value: fallback, source: "derived", provider: "derived" };
  }
  return { value: undefined, source: "missing", provider: "missing" };
}

export function resolveVisualInput(
  record: Record<string, unknown>,
  config: PluginConfig["visualMemory"],
  context?: OpenClawPluginToolContext,
): { value?: ResolvedVisualInput; error?: string } {
  const direct = parseVisualContext(record);
  const explicitContext = parseVisualContext(
    getParam(record, "visualContext") ?? getParam(record, "visual_context"),
  );
  const runtimeContext = selectVisualContextCandidate(
    extractVisualContextCandidatesFromUnknown(context, "tool_context_only"),
    direct.mediaRef ?? explicitContext.mediaRef,
  );
  const cachedContext = getCachedVisualContext(
    context,
    direct.mediaRef ?? explicitContext.mediaRef ?? runtimeContext.mediaRef,
  );

  const mediaRefField = chooseVisualField(
    direct.mediaRef,
    explicitContext.mediaRef,
    runtimeContext.mediaRef,
    cachedContext.mediaRef,
    undefined,
  );
  if (!mediaRefField.value) {
    return { error: "mediaRef required (directly or via visualContext)." };
  }
  const mediaRef = mediaRefField.value;

  const summaryField = chooseVisualField(
    direct.summary,
    explicitContext.summary,
    runtimeContext.summary,
    cachedContext.summary,
    deriveVisualSummary({
      mediaRef,
      scene: direct.scene ?? explicitContext.scene ?? runtimeContext.scene ?? cachedContext.scene,
      ocr: direct.ocr ?? explicitContext.ocr ?? runtimeContext.ocr ?? cachedContext.ocr,
    }),
  );
  if (!summaryField.value) {
    return { error: "summary required (directly, via visualContext, or derivable from OCR/scene/mediaRef)." };
  }

  const sceneField = chooseVisualField(
    direct.scene,
    explicitContext.scene,
    runtimeContext.scene,
    cachedContext.scene,
    deriveVisualScene({ mediaRef, summary: summaryField.value }),
  );
  const ocrField = chooseVisualField(direct.ocr, explicitContext.ocr, runtimeContext.ocr, cachedContext.ocr, undefined);
  const entitiesField = chooseVisualField(
    direct.entities,
    explicitContext.entities,
    runtimeContext.entities,
    cachedContext.entities,
    undefined,
  );
  const whyRelevantField = chooseVisualField(
    direct.whyRelevant,
    explicitContext.whyRelevant,
    runtimeContext.whyRelevant,
    cachedContext.whyRelevant,
    undefined,
  );

  const usesCliVisualInput = hasCliVisualPayloadData(direct) || hasCliVisualPayloadData(explicitContext);
  const usesRuntimeContext =
    mediaRefField.provider === "runtime_context" ||
    summaryField.provider === "runtime_context" ||
    sceneField.provider === "runtime_context" ||
    ocrField.provider === "runtime_context" ||
    entitiesField.provider === "runtime_context" ||
    whyRelevantField.provider === "runtime_context";
  const usesCachedRuntimeContext =
    mediaRefField.provider === "cached_context" ||
    summaryField.provider === "cached_context" ||
    sceneField.provider === "cached_context" ||
    ocrField.provider === "cached_context" ||
    entitiesField.provider === "cached_context" ||
    whyRelevantField.provider === "cached_context";
  const usesCliAnchoredContent =
    direct.summary !== undefined ||
    direct.ocr !== undefined ||
    direct.scene !== undefined ||
    direct.whyRelevant !== undefined ||
    direct.entities !== undefined ||
    explicitContext.summary !== undefined ||
    explicitContext.ocr !== undefined ||
    explicitContext.scene !== undefined ||
    explicitContext.whyRelevant !== undefined ||
    explicitContext.entities !== undefined;
  const resolvedRuntimeSource =
    usesRuntimeContext || usesCachedRuntimeContext
      ? runtimeVisualSourceRank(runtimeContext.runtimeSource) >= runtimeVisualSourceRank(cachedContext.runtimeSource)
        ? runtimeContext.runtimeSource ?? cachedContext.runtimeSource
        : cachedContext.runtimeSource ?? runtimeContext.runtimeSource
      : undefined;
  const runtimeProbe = usesCliAnchoredContent
    ? "cli_store_visual_only"
    : resolvedRuntimeSource
      ? collapseRuntimeVisualProbe(resolvedRuntimeSource)
      : usesCliVisualInput
        ? "cli_store_visual_only"
        : "none";

  return {
    value: {
      mediaRef,
      summary: summaryField.value,
      sourceChannel:
        direct.sourceChannel ?? explicitContext.sourceChannel ?? runtimeContext.sourceChannel ?? cachedContext.sourceChannel,
      observedAt:
        direct.observedAt ?? explicitContext.observedAt ?? runtimeContext.observedAt ?? cachedContext.observedAt,
      ocr: config.storeOcr ? ocrField.value : undefined,
      scene: config.storeScene ? sceneField.value : undefined,
      whyRelevant: config.storeWhyRelevant ? whyRelevantField.value : undefined,
      confidence:
        direct.confidence ?? explicitContext.confidence ?? runtimeContext.confidence ?? cachedContext.confidence,
      entities: config.storeEntities ? entitiesField.value : undefined,
      fieldSources: {
        summary: summaryField.source,
        ocr: config.storeOcr ? ocrField.source : "policy_disabled",
        scene: config.storeScene ? sceneField.source : "policy_disabled",
        entities: config.storeEntities ? entitiesField.source : "policy_disabled",
        whyRelevant: config.storeWhyRelevant ? whyRelevantField.source : "policy_disabled",
      },
      runtimeSource: resolvedRuntimeSource,
      runtimeProbe,
    },
  };
}

export function resolveVisualLocalPath(mediaRef: string | undefined): string | undefined {
  const normalized = readString(mediaRef);
  if (!normalized || !normalized.startsWith("file:")) {
    return undefined;
  }
  try {
    return normalizeFriendlyWindowsLocalPath(fileURLToPath(normalized));
  } catch {
    return resolveVisualFileUrlFallbackPath(normalized);
  }
}

export function clearVisualTurnContextCache(): void {
  visualTurnContextCache.clear();
}

export function getVisualTurnContextCacheSizeForTesting(): number {
  return visualTurnContextCache.size;
}

export function harvestVisualContextForTesting(
  hookName: string,
  event: Record<string, unknown>,
  ctx: Record<string, unknown>,
  ttlMs: number,
  hookNameToRuntimeVisualSource: (hookName: string) => RuntimeVisualSource,
  buildVisualHarvestContext: (event: Record<string, unknown>, ctx: Record<string, unknown>) => OpenClawPluginToolContext,
): VisualContextPayload[] {
  const payloads = extractVisualContextCandidatesFromUnknown(event, hookNameToRuntimeVisualSource(hookName));
  if (payloads.length === 0) {
    return [];
  }
  const harvestContext = buildVisualHarvestContext(event, ctx);
  rememberVisualContexts(harvestContext, payloads, ttlMs);
  return payloads;
}

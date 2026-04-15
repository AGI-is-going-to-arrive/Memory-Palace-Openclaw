import { createHash } from "node:crypto";
import type { RuntimeVisualSource } from "./types.js";
import { readString } from "./utils.js";

export type VisualContextPayloadLike = {
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

const VISUAL_REDACTION_PATTERNS: Array<[RegExp, string]> = [
  [/\b([A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,})\b/giu, "[REDACTED_EMAIL]"],
  [/\b(authorization\s*[:=]\s*bearer\s+)[^\s,;]+/giu, "$1[REDACTED]"],
  [/\b(x-mcp-api-key\s*[:=]\s*)[^\s,;]+/giu, "$1[REDACTED]"],
  [/\b(api[-_ ]?key\s*[:=]\s*)[^\s,;]+/giu, "$1[REDACTED]"],
  [/\b(token\s*[:=]\s*)[^\s,;]+/giu, "$1[REDACTED]"],
] as const;
const VISUAL_BLOB_PATTERN =
  /(?<![A-Za-z0-9+/=])[A-Za-z0-9+/]{48,}={0,2}(?![A-Za-z0-9+/=])/g;
const VISUAL_PHONE_PATTERN = /(?<!\w)(\+?\d[\d().\s-]*[().\s-][\d().\s-]*\d)(?!\w)/gu;
const VISUAL_PHONE_EXCLUSION_PATTERN =
  /^\d{4}-\d{2}-\d{2}(?:[T\s]\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?)?$/u;
const VISUAL_PHONE_DATE_RANGE_EXCLUSION_PATTERN =
  /^\d{4}-\d{2}-\d{2}\s*[-–—]\s*\d{4}-\d{2}-\d{2}$/u;
const VISUAL_PHONE_VERSION_EXCLUSION_PATTERN = /^v\d+(?:[.-]\d+){2,}$/iu;
const VISUAL_PHONE_DOTTED_DATE_RANGE_EXCLUSION_PATTERN =
  /^\d{4}\.\d{1,2}\.\d{1,2}\s*[-–—]\s*\d{4}\.\d{1,2}\.\d{1,2}$/u;
const VISUAL_PHONE_IPV4_EXCLUSION_PATTERN =
  /^(?:25[0-5]|2[0-4]\d|1?\d?\d)(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3}$/u;
const VISUAL_BLOB_DIGEST_EXCLUSION_PATTERN =
  /^(?:sha(?:1|224|256|384|512)[-:])?[A-Fa-f0-9]{64,128}$/u;
const VISUAL_BLOB_DIGEST_CONTEXT_PATTERN =
  /sha(?:1|224|256|384|512)(?:\s+digest)?(?:\s*[:=]\s*|\s+$)$/iu;

export function redactVisualSensitiveText(value: string | undefined): string | undefined {
  if (!value) {
    return value;
  }
  const patternRedacted = VISUAL_REDACTION_PATTERNS.reduce(
    (current, [pattern, replacement]) => current.replace(pattern, replacement),
    value,
  );
  const redacted = patternRedacted.replace(VISUAL_BLOB_PATTERN, (match, offset, source) => {
    const prefix = source.slice(Math.max(0, offset - 32), offset);
    return VISUAL_BLOB_DIGEST_EXCLUSION_PATTERN.test(match) ||
      VISUAL_BLOB_DIGEST_CONTEXT_PATTERN.test(prefix)
      ? match
      : "[REDACTED_BLOB]";
  });
  return redacted.replace(VISUAL_PHONE_PATTERN, (match) => {
    const normalized = match.trim();
    if (
      VISUAL_PHONE_EXCLUSION_PATTERN.test(normalized) ||
      VISUAL_PHONE_DATE_RANGE_EXCLUSION_PATTERN.test(normalized) ||
      VISUAL_PHONE_DOTTED_DATE_RANGE_EXCLUSION_PATTERN.test(normalized) ||
      VISUAL_PHONE_IPV4_EXCLUSION_PATTERN.test(normalized) ||
      VISUAL_PHONE_VERSION_EXCLUSION_PATTERN.test(normalized)
    ) {
      return match;
    }
    return match.replace(/\D/g, "").length >= 10 ? "[REDACTED_PHONE]" : match;
  });
}

export function looksLikeImageMediaRef(value: string | undefined): boolean {
  if (!value) {
    return false;
  }
  const normalized = value.trim().toLowerCase();
  return (
    normalized.startsWith("data:image/") ||
    normalized.startsWith("file:") ||
    /\.(png|jpe?g|gif|webp|bmp|svg|heic|heif)(?:[?#].*)?$/i.test(normalized) ||
    /(image|screenshot|photo|picture|snapshot|scan)/i.test(normalized)
  );
}

export function sanitizeVisualMediaRef(value: string | undefined): string | undefined {
  const mediaRef = readString(value);
  if (!mediaRef) {
    return undefined;
  }
  if (/^data:/i.test(mediaRef)) {
    const mime = /^data:([^;,]+)/i.exec(mediaRef)?.[1] ?? "application/octet-stream";
    const digest = createHash("sha256").update(mediaRef).digest("hex").slice(0, 12);
    return `data:${mime};sha256-${digest}`;
  }
  if (mediaRef.length > 512) {
    const digest = createHash("sha256").update(mediaRef).digest("hex").slice(0, 12);
    return `sha256-${digest}`;
  }
  return redactVisualSensitiveText(mediaRef);
}

export function normalizeVisualPayload<T extends VisualContextPayloadLike>(payload: T): T {
  const overrides: Partial<VisualContextPayloadLike> = {};
  if (payload.mediaRef) {
    overrides.mediaRef = sanitizeVisualMediaRef(payload.mediaRef);
  }
  if (payload.summary) {
    overrides.summary = redactVisualSensitiveText(payload.summary);
  }
  if (payload.sourceChannel) {
    overrides.sourceChannel = payload.sourceChannel;
  }
  if (payload.observedAt) {
    overrides.observedAt = payload.observedAt;
  }
  if (payload.ocr) {
    overrides.ocr = redactVisualSensitiveText(payload.ocr);
  }
  if (payload.scene) {
    overrides.scene = redactVisualSensitiveText(payload.scene);
  }
  if (payload.whyRelevant) {
    overrides.whyRelevant = redactVisualSensitiveText(payload.whyRelevant);
  }
  if (payload.confidence !== undefined) {
    overrides.confidence = payload.confidence;
  }
  if (payload.entities) {
    overrides.entities = payload.entities.map(
      (entry) => redactVisualSensitiveText(entry) ?? entry,
    );
  }
  if (payload.runtimeSource) {
    overrides.runtimeSource = payload.runtimeSource;
  }
  return {
    ...payload,
    ...overrides,
  } as T;
}

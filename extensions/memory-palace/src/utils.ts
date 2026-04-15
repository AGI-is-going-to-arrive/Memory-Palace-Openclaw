import { createHash } from "node:crypto";
import { MemoryPalaceConnectionError } from "./client.js";
import { PROFILE_BLOCK_NAMES } from "./types.js";
import type { JsonRecord, ProfileBlockName, TraceLogger } from "./types.js";

const OPENCLAW_REPLY_TAG_PATTERNS = [/\[\[reply_to_[^\]]+\]\]/giu];
const INJECTED_MEMORY_PROMPT_BLOCK_PATTERNS = [
  /<memory-palace-profile>[\s\S]*?<\/memory-palace-profile>/giu,
  /<memory-palace-recall>[\s\S]*?<\/memory-palace-recall>/giu,
  /<memory-palace-reflection>[\s\S]*?<\/memory-palace-reflection>/giu,
  /<memory-palace-host-bridge>[\s\S]*?<\/memory-palace-host-bridge>/giu,
];

export type MessageTextExtractionOptions = {
  allowedRoles?: readonly string[];
  cleanText?: (text: string) => string;
};

export type TruncateWithEllipsisOptions = {
  preserveInputWhenLimitNonPositive?: boolean;
  preserveShortLimitWithoutEllipsis?: boolean;
  trimEnd?: boolean;
};

export function isRecord(value: unknown): value is JsonRecord {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return false;
  }
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

export function jsonResult(payload: unknown) {
  return {
    content: [
      {
        type: "text",
        text: JSON.stringify(payload, null, 2),
      },
    ],
    details: payload,
  };
}

export function getParam(params: Record<string, unknown>, key: string): unknown {
  if (Object.hasOwn(params, key)) {
    return params[key];
  }
  const snake = key.replace(/([a-z0-9])([A-Z])/g, "$1_$2").toLowerCase();
  if (snake !== key && Object.hasOwn(params, snake)) {
    return params[snake];
  }
  return undefined;
}

export function readString(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value.trim() : undefined;
}

export function readBoolean(value: unknown): boolean | undefined {
  return typeof value === "boolean" ? value : undefined;
}

export function readPositiveNumber(value: unknown): number | undefined {
  if (typeof value === "number" && Number.isFinite(value) && value > 0) {
    return value;
  }
  if (typeof value === "string" && value.trim()) {
    const parsed = Number(value.trim());
    if (Number.isFinite(parsed) && parsed > 0) {
      return parsed;
    }
  }
  return undefined;
}

export function readNonNegativeNumber(value: unknown): number | undefined {
  if (typeof value === "number" && Number.isFinite(value) && value >= 0) {
    return value;
  }
  if (typeof value === "string" && value.trim()) {
    const parsed = Number(value.trim());
    if (Number.isFinite(parsed) && parsed >= 0) {
      return parsed;
    }
  }
  return undefined;
}

export function readStringArray(value: unknown): string[] | undefined {
  if (!Array.isArray(value)) {
    return undefined;
  }
  const entries = value
    .filter((entry): entry is string => typeof entry === "string")
    .map((entry) => entry.trim())
    .filter(Boolean);
  return entries.length > 0 ? entries : undefined;
}

export function readProfileBlockArray(value: unknown): ProfileBlockName[] | undefined {
  const entries = readStringArray(value);
  if (!entries) {
    return undefined;
  }
  return entries.filter(
    (entry): entry is ProfileBlockName =>
      (PROFILE_BLOCK_NAMES as readonly string[]).includes(entry),
  );
}

export function readStringMap(value: unknown): Record<string, string> | undefined {
  if (!isRecord(value)) {
    return undefined;
  }
  const result: Record<string, string> = {};
  for (const [key, entry] of Object.entries(value)) {
    const resolved = readString(entry);
    if (resolved) {
      result[key] = resolved;
    }
  }
  return Object.keys(result).length > 0 ? result : undefined;
}

export function readFlexibleNumber(value: unknown): number | undefined {
  if (typeof value === "number" && Number.isFinite(value)) {
    return value;
  }
  if (typeof value === "string" && value.trim()) {
    const parsed = Number(value.trim());
    if (Number.isFinite(parsed)) {
      return parsed;
    }
  }
  return undefined;
}

export function readLooseStringArray(value: unknown): string[] | undefined {
  const fromArray = readStringArray(value);
  if (fromArray && fromArray.length > 0) {
    return fromArray;
  }
  const fromString = readString(value);
  if (!fromString) {
    return undefined;
  }
  const values = fromString
    .split(",")
    .map((entry) => entry.trim())
    .filter(Boolean);
  return values.length > 0 ? values : undefined;
}

export function parseJsonRecord(value: unknown): JsonRecord | undefined {
  if (isRecord(value)) {
    return value;
  }
  if (typeof value !== "string" || !value.trim()) {
    return undefined;
  }
  let current: unknown = value;
  for (let depth = 0; depth < 4 && typeof current === "string"; depth += 1) {
    try {
      current = JSON.parse(current) as unknown;
    } catch {
      return undefined;
    }
  }
  return isRecord(current) ? current : undefined;
}

export function parseJsonRecordWithWarning(
  value: unknown,
  context: string,
  logger?: TraceLogger,
): JsonRecord | undefined {
  if (isRecord(value)) {
    return value;
  }
  if (typeof value !== "string" || !value.trim()) {
    return undefined;
  }
  let current: unknown = value;
  for (let depth = 0; depth < 4 && typeof current === "string"; depth += 1) {
    try {
      current = JSON.parse(current) as unknown;
    } catch (error) {
      logger?.warn?.(`memory-palace ignored invalid JSON for ${context}: ${formatError(error)}`);
      return undefined;
    }
  }
  if (isRecord(current)) {
    return current;
  }
  logger?.warn?.(`memory-palace ignored non-object JSON for ${context}.`);
  return undefined;
}

export function stripWrappingQuotes(value: string): string {
  const trimmed = value.trim();
  if (trimmed.length >= 2) {
    const quote = trimmed[0];
    if ((quote === '"' || quote === "'") && trimmed.slice(-1) === quote) {
      return trimmed.slice(1, -1);
    }
  }
  return trimmed;
}

export function pickFirstNonBlank(...values: Array<unknown>): string | undefined {
  for (const value of values) {
    const text = readString(value)?.trim();
    if (text) {
      return text;
    }
  }
  return undefined;
}

export function mergeStringRecords(...records: Array<Record<string, string> | undefined>): Record<string, string> {
  const merged: Record<string, string> = {};
  for (const record of records) {
    if (!record) {
      continue;
    }
    for (const [key, value] of Object.entries(record)) {
      const normalized = readString(value);
      if (normalized !== undefined) {
        merged[key] = normalized;
      }
    }
  }
  return merged;
}

export function normalizeBaseUrl(value: string | undefined): string {
  return readString(value)?.trim().replace(/\/+$/u, "") ?? "";
}

export function normalizeChatApiBase(value: string | undefined): string {
  const normalized = normalizeBaseUrl(value);
  const lowered = normalized.toLowerCase();
  for (const suffix of ["/chat/completions", "/responses"]) {
    if (lowered.endsWith(suffix)) {
      return normalized.slice(0, normalized.length - suffix.length);
    }
  }
  return normalized;
}

export function safeSegment(value: string | undefined): string {
  const raw = (value ?? "").trim();
  if (!raw) {
    return "anonymous";
  }
  const normalized = raw.replace(/[^A-Za-z0-9._-]+/g, "-").replace(/-+/g, "-").replace(/^-|-$/g, "");
  return normalized || `agent-${createHash("sha256").update(raw).digest("hex").slice(0, 8)}`;
}

export function containsCjk(text: string): boolean {
  return /[\u1100-\u11FF\u3000-\u30FF\u3130-\u318F\u31F0-\u31FF\u3400-\u9FFF\uA960-\uA97F\uAC00-\uD7AF\uD7B0-\uD7FF\uF900-\uFAFF\uFF65-\uFF9F\u{20000}-\u{2FA1F}]/u.test(text);
}

export function normalizeText(text: string): string {
  return text.replace(/\s+/g, " ").trim();
}

export function normalizeTextPreservingLines(text: string): string {
  return text.replace(/\r\n?/g, "\n").replace(/[ \t]+/g, " ").trim();
}

export function truncateWithEllipsis(
  text: string,
  limit: number,
  options: TruncateWithEllipsisOptions = {},
): string {
  if (text.length <= limit) {
    return text;
  }
  if (limit <= 0 && options.preserveInputWhenLimitNonPositive) {
    return text;
  }
  if (limit <= 1 && options.preserveShortLimitWithoutEllipsis) {
    return text.slice(0, Math.max(0, limit));
  }
  const truncated = text.slice(0, Math.max(0, limit - 1));
  return `${options.trimEnd === false ? truncated : truncated.trimEnd()}…`;
}

export function stripOpenClawReplyTags(text: string): string {
  return OPENCLAW_REPLY_TAG_PATTERNS.reduce(
    (current, pattern) => current.replace(pattern, ""),
    text,
  );
}

export function stripInjectedMemoryPromptBlocks(text: string): string {
  return INJECTED_MEMORY_PROMPT_BLOCK_PATTERNS.reduce(
    (current, pattern) => current.replace(pattern, ""),
    text,
  );
}

export function cleanMessageTextForReasoning(
  text: string,
  options: {
    preprocessText?: (text: string) => string;
    normalizeText?: (text: string) => string;
  } = {},
): string {
  const preprocessed = options.preprocessText ? options.preprocessText(text) : text;
  const stripped = stripOpenClawReplyTags(preprocessed);
  return options.normalizeText ? options.normalizeText(stripped) : stripped.trim();
}

export function extractTextBlocks(content: unknown): string[] {
  if (typeof content === "string") {
    return [content];
  }
  if (Array.isArray(content)) {
    const texts: string[] = [];
    for (const block of content) {
      if (typeof block === "string") {
        texts.push(block);
        continue;
      }
      if (isRecord(block) && typeof block.text === "string") {
        texts.push(block.text);
      }
    }
    return texts;
  }
  if (isRecord(content) && typeof content.text === "string") {
    return [content.text];
  }
  return [];
}

export function extractMessageTexts(
  messages: unknown[],
  options: MessageTextExtractionOptions = {},
): string[] {
  const allowedRoles = options.allowedRoles
    ? new Set(options.allowedRoles.map((entry) => entry.toLowerCase()))
    : null;
  const cleanText = options.cleanText ?? ((text: string) => text);
  const texts: string[] = [];

  for (const message of messages) {
    if (!isRecord(message)) {
      continue;
    }
    const role = readString(message.role)?.toLowerCase();
    if (allowedRoles && (!role || !allowedRoles.has(role))) {
      continue;
    }
    for (const text of extractTextBlocks(message.content)) {
      const cleaned = cleanText(text);
      if (cleaned) {
        texts.push(cleaned);
      }
    }
  }

  return texts;
}

export function isEmojiOnly(text: string): boolean {
  const normalized = text.replace(/[\s.,!?;:'"()\-_/\\[\]{}]+/g, "");
  return Boolean(normalized) && /^[\p{Extended_Pictographic}\p{Emoji_Presentation}]+$/u.test(normalized);
}

export function formatError(error: unknown): string {
  if (error instanceof MemoryPalaceConnectionError) {
    return [error.message, ...error.causes].filter(Boolean).join(" | ");
  }
  return error instanceof Error ? error.message : String(error);
}

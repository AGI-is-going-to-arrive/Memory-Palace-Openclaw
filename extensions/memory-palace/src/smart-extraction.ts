import { createHash } from "node:crypto";
import type {
  DurableSynthesisEvidence,
  JsonRecord,
  PluginConfig,
  ResolvedAclPolicy,
  SmartExtractionCandidate,
  SmartExtractionCategory,
} from "./types.js";
import {
  isRecord,
  normalizeText,
  readString,
  truncateWithEllipsis,
} from "./utils.js";

export type SmartExtractionLlmConfig = {
  baseUrl: string;
  apiKey?: string;
  model: string;
};

export type SmartExtractionResolveDeps = {
  normalizeChatApiBase: (value: string | undefined) => string;
  resolveRuntimeEnvValue: (config: PluginConfig, ...keys: string[]) => string | undefined;
};

export type SmartExtractionTranscriptDeps = {
  extractTextBlocks: (content: unknown) => string[];
  cleanMessageTextForReasoning: (text: string) => string;
  normalizeText?: (text: string) => string;
};

export type SmartExtractionEvidenceDeps = SmartExtractionTranscriptDeps & {
  tokenizeForHostBridge: (text: string) => string[];
  splitProfileCaptureSegments: (text: string) => string[];
  looksLikePromptInjection: (text: string) => boolean;
  isSensitiveHostBridgeText: (text: string) => boolean;
  profileCaptureEphemeralPatterns: RegExp[];
  countTokenOverlap: (left: string[], right: string[]) => number;
  truncate?: (text: string, limit: number) => string;
  countLines?: (text: string) => number;
};

export type SmartExtractionCandidateDeps = SmartExtractionEvidenceDeps & {
  normalizeSmartExtractionCategory: (value: string | undefined) => SmartExtractionCategory | undefined;
  sanitizeDurableSynthesisSummary: (
    category: SmartExtractionCategory,
    text: string,
  ) => string | undefined;
  synthesizeWorkflowSummary: (messages: unknown[], preferredSummary: string) => string;
};

export type SmartExtractionTargetUriDeps = {
  appendUriPath: (baseUri: string, ...segments: Array<string | undefined>) => string;
  renderTemplate: (template: string, replacements: Record<string, string>) => string;
  buildDurableSynthesisUri: (
    config: PluginConfig,
    policy: ResolvedAclPolicy,
    sourceMode: "llm_extracted",
    category: SmartExtractionCategory,
    summary: string,
    pending: boolean,
  ) => string;
};

const WORKFLOW_SUMMARY_HINT_PATTERN =
  /\b(default workflow|workflow|process|review order|delivery order|coding habits?|programming habits?|code changes first|code first|tests? immediately|docs? last)\b/iu;
const WORKFLOW_SUMMARY_HINT_PATTERN_CJK =
  /(默认工作流|工作流|流程|先改代码|先写代码|先跑测试|测试随后|文档最后)/u;

function countLines(text: string): number {
  return text ? text.split(/\r?\n/).length : 0;
}

function normalizeWith(
  value: string,
  normalizer: ((text: string) => string) | undefined,
): string {
  return (normalizer ?? normalizeText)(value);
}

function truncateTranscriptLine(line: string, maxChars: number): string {
  if (maxChars <= 0) {
    return "";
  }
  if (line.length <= maxChars) {
    return line;
  }
  const prefixMatch = line.match(/^(user|assistant)\[\d+\]:\s*/u);
  if (!prefixMatch) {
    return line.slice(0, maxChars);
  }
  const prefix = prefixMatch[0];
  if (prefix.length >= maxChars) {
    return prefix.slice(0, maxChars);
  }
  const suffixBudget = maxChars - prefix.length;
  const content = line.slice(prefix.length).trim();
  if (!content) {
    return prefix.trimEnd();
  }
  if (content.length <= suffixBudget) {
    return `${prefix}${content}`;
  }
  const words = content.split(/\s+/u).filter(Boolean);
  const selected: string[] = [];
  let selectedChars = 0;
  for (let index = words.length - 1; index >= 0; index -= 1) {
    const word = words[index] ?? "";
    const addition = selected.length === 0 ? word.length : word.length + 1;
    if (selected.length > 0 && selectedChars + addition > suffixBudget) {
      break;
    }
    if (selected.length === 0 && addition > suffixBudget) {
      return `${prefix}${content.slice(-suffixBudget)}`;
    }
    selected.unshift(word);
    selectedChars += addition;
  }
  return `${prefix}${selected.join(" ")}`;
}

function extractTranscriptTextBlocks(
  message: JsonRecord,
  role: "user" | "assistant",
  deps: SmartExtractionTranscriptDeps,
): string[] {
  const content = message.content;
  if (role === "assistant" && Array.isArray(content)) {
    const assistantReplyBlocks = content.filter((block) => {
      if (typeof block === "string") {
        return true;
      }
      if (!isRecord(block) || typeof block.text !== "string") {
        return false;
      }
      const blockType = readString(block.type)?.toLowerCase();
      return !blockType || blockType === "text";
    });
    if (assistantReplyBlocks.length > 0) {
      return deps.extractTextBlocks(assistantReplyBlocks);
    }
  }
  return deps.extractTextBlocks(content);
}

export function resolveSmartExtractionLlmConfig(
  config: PluginConfig,
  deps: SmartExtractionResolveDeps,
): SmartExtractionLlmConfig | undefined {
  const baseUrl = deps.normalizeChatApiBase(
    deps.resolveRuntimeEnvValue(
      config,
      "SMART_EXTRACTION_LLM_API_BASE",
      "WRITE_GUARD_LLM_API_BASE",
      "OPENAI_BASE_URL",
      "OPENAI_API_BASE",
      "LLM_API_BASE",
      "LLM_RESPONSES_URL",
      "COMPACT_GIST_LLM_API_BASE",
    ),
  );
  const model = deps.resolveRuntimeEnvValue(
    config,
    "SMART_EXTRACTION_LLM_MODEL",
    "WRITE_GUARD_LLM_MODEL",
    "OPENAI_MODEL",
    "LLM_MODEL_NAME",
    "LLM_MODEL",
    "COMPACT_GIST_LLM_MODEL",
  );
  const apiKey = deps.resolveRuntimeEnvValue(
    config,
    "SMART_EXTRACTION_LLM_API_KEY",
    "WRITE_GUARD_LLM_API_KEY",
    "OPENAI_API_KEY",
    "LLM_API_KEY",
    "COMPACT_GIST_LLM_API_KEY",
  );
  if (!baseUrl || !model) {
    return undefined;
  }
  return {
    baseUrl,
    apiKey,
    model,
  };
}

export function extractChatMessageText(payload: unknown): string {
  if (!isRecord(payload)) {
    return "";
  }
  const choices = Array.isArray(payload.choices) ? payload.choices : [];
  for (const choice of choices) {
    if (!isRecord(choice)) {
      continue;
    }
    const message = isRecord(choice.message) ? choice.message : {};
    if (typeof message.content === "string" && message.content.trim()) {
      return message.content.trim();
    }
    if (Array.isArray(message.content)) {
      const parts = message.content
        .filter((entry): entry is JsonRecord => isRecord(entry))
        .map((entry) => readString(entry.text))
        .filter((entry): entry is string => Boolean(entry?.trim()))
        .map((entry) => entry.trim());
      if (parts.length > 0) {
        return parts.join("\n");
      }
    }
  }
  const output = Array.isArray(payload.output) ? payload.output : [];
  const parts: string[] = [];
  for (const item of output) {
    if (!isRecord(item) || !Array.isArray(item.content)) {
      continue;
    }
    for (const contentItem of item.content) {
      if (!isRecord(contentItem)) {
        continue;
      }
      const textValue = readString(contentItem.text);
      if (textValue?.trim()) {
        parts.push(textValue.trim());
      }
    }
  }
  return parts.join("\n").trim();
}

export function parseChatJsonObject(
  rawText: string,
  normalizer: (text: string) => string = normalizeText,
): JsonRecord | undefined {
  const candidate = normalizer(rawText);
  if (!candidate) {
    return undefined;
  }
  const parseCandidates = [candidate];
  if (candidate.startsWith("```")) {
    parseCandidates.push(
      candidate
        .replace(/^```(?:json)?\s*/iu, "")
        .replace(/\s*```$/u, "")
        .trim(),
    );
  }
  const start = candidate.indexOf("{");
  const end = candidate.lastIndexOf("}");
  if (start >= 0 && end > start) {
    parseCandidates.push(candidate.slice(start, end + 1));
  }
  for (const item of parseCandidates) {
    try {
      const parsed = JSON.parse(item) as unknown;
      if (isRecord(parsed)) {
        return parsed;
      }
    } catch {
      const normalized = item
        .replace(/([{,]\s*)([A-Za-z_][A-Za-z0-9_\-]*)(\s*:)/gu, '$1"$2"$3')
        .replace(/,\s*([}\]])/gu, "$1");
      if (normalized === item) {
        continue;
      }
      try {
        const parsed = JSON.parse(normalized) as unknown;
        if (isRecord(parsed)) {
          return parsed;
        }
      } catch {
        continue;
      }
    }
  }
  return undefined;
}

export function buildSmartExtractionTranscript(
  messages: unknown[],
  maxChars: number,
  deps: SmartExtractionTranscriptDeps,
): string {
  const lines: string[] = [];
  let userIndex = 0;
  let assistantIndex = 0;
  for (const message of messages) {
    if (!isRecord(message)) {
      continue;
    }
    const role = readString(message.role)?.trim().toLowerCase();
    if (role !== "user" && role !== "assistant") {
      continue;
    }
    const blocks = extractTranscriptTextBlocks(message, role, deps)
      .map((entry) => deps.cleanMessageTextForReasoning(entry))
      .map((entry) => normalizeWith(entry, deps.normalizeText))
      .filter(Boolean);
    if (blocks.length === 0) {
      continue;
    }
    const index = role === "user" ? (userIndex += 1) : (assistantIndex += 1);
    lines.push(`${role}[${index}]: ${blocks.join(" ")}`);
  }
  const transcript = lines.join("\n");
  if (transcript.length <= maxChars) {
    return transcript;
  }
  const selected: string[] = [];
  let selectedChars = 0;
  for (let index = lines.length - 1; index >= 0; index -= 1) {
    const line = lines[index] ?? "";
    const addition = selected.length === 0 ? line.length : line.length + 1;
    if (selected.length > 0 && selectedChars + addition > maxChars) {
      break;
    }
    if (selected.length === 0 && addition > maxChars) {
      return truncateTranscriptLine(line, maxChars);
    }
    selected.unshift(line);
    selectedChars += addition;
  }
  return selected.join("\n");
}

export function buildSmartExtractionEvidence(
  messages: unknown[],
  summary: string,
  deps: SmartExtractionEvidenceDeps,
): DurableSynthesisEvidence[] {
  const summaryTokens = deps.tokenizeForHostBridge(summary);
  const evidence: DurableSynthesisEvidence[] = [];
  let userIndex = 0;
  for (const message of messages) {
    if (!isRecord(message)) {
      continue;
    }
    const role = readString(message.role)?.trim().toLowerCase();
    if (role !== "user") {
      continue;
    }
    const index = (userIndex += 1);
    for (const segment of deps.extractTextBlocks(message.content)
      .map((entry) => deps.cleanMessageTextForReasoning(entry))
      .flatMap((entry) => deps.splitProfileCaptureSegments(entry))
      .map((entry) => normalizeWith(entry, deps.normalizeText))
      .filter(Boolean)) {
      if (
        deps.looksLikePromptInjection(segment) ||
        deps.isSensitiveHostBridgeText(segment) ||
        deps.profileCaptureEphemeralPatterns.some((pattern) => pattern.test(segment))
      ) {
        continue;
      }
      const overlap = deps.countTokenOverlap(summaryTokens, deps.tokenizeForHostBridge(segment));
      if (overlap < 2 && !segment.toLowerCase().includes(summary.toLowerCase())) {
        continue;
      }
      evidence.push({
        key: `${role}[${index}] sha256-${createHash("sha256").update(segment).digest("hex").slice(0, 12)}`,
        source: `${role}[${index}]`,
        lineStart: 1,
        lineEnd: (deps.countLines ?? countLines)(segment),
        snippet:
          deps.truncate?.(segment, 220) ??
          truncateWithEllipsis(segment, 220, {
            preserveInputWhenLimitNonPositive: true,
            preserveShortLimitWithoutEllipsis: true,
            trimEnd: false,
          }),
      });
      if (evidence.length >= 4) {
        return evidence;
      }
      break;
    }
  }
  return evidence;
}

export function computeTextSimilarity(
  left: string,
  right: string,
  deps: Pick<
    SmartExtractionEvidenceDeps,
    "tokenizeForHostBridge" | "countTokenOverlap" | "normalizeText"
  >,
): number {
  const leftTokens = deps.tokenizeForHostBridge(left);
  const rightTokens = deps.tokenizeForHostBridge(right);
  if (leftTokens.length === 0 || rightTokens.length === 0) {
    return normalizeWith(left, deps.normalizeText).toLowerCase() ===
      normalizeWith(right, deps.normalizeText).toLowerCase()
      ? 1
      : 0;
  }
  const overlap = deps.countTokenOverlap(leftTokens, rightTokens);
  const union = new Set([...leftTokens, ...rightTokens]).size;
  if (union <= 0) {
    return 0;
  }
  return overlap / union;
}

export function parseSmartExtractionCandidates(
  parsed: JsonRecord,
  config: PluginConfig,
  messages: unknown[],
  deps: SmartExtractionCandidateDeps,
): SmartExtractionCandidate[] {
  const rows = Array.isArray(parsed.candidates) ? parsed.candidates : [];
  const allowed = new Set(config.smartExtraction.categories);
  const seen = new Set<string>();
  const candidates: SmartExtractionCandidate[] = [];
  for (const row of rows) {
    if (!isRecord(row)) {
      continue;
    }
    const rawCategory = deps.normalizeSmartExtractionCategory(readString(row.category) ?? undefined);
    if (!rawCategory || !allowed.has(rawCategory)) {
      continue;
    }
    let category = rawCategory;
    const summary =
      deps.sanitizeDurableSynthesisSummary(category, readString(row.summary) ?? "") ?? "";
    if (!summary) {
      continue;
    }
    const normalizedSummaryText = normalizeWith(summary, deps.normalizeText);
    if (
      category !== "workflow" &&
      normalizedSummaryText &&
      allowed.has("workflow") &&
      (
        WORKFLOW_SUMMARY_HINT_PATTERN.test(normalizedSummaryText) ||
        WORKFLOW_SUMMARY_HINT_PATTERN_CJK.test(normalizedSummaryText)
      )
    ) {
      category = "workflow";
    }
    const normalizedSummary =
      category === "workflow"
        ? deps.sanitizeDurableSynthesisSummary(
            category,
            deps.synthesizeWorkflowSummary(messages, summary),
          ) ?? summary
        : summary;
    let confidence =
      typeof row.confidence === "number" && Number.isFinite(row.confidence)
        ? row.confidence
        : 0.62;
    confidence = Math.max(0, Math.min(1, confidence));
    if (category === "workflow" && config.reconcile.enabled) {
      confidence = Math.max(confidence, config.capturePipeline.minConfidence);
    }
    const pending = confidence < config.capturePipeline.minConfidence;
    if (
      pending &&
      (!config.capturePipeline.pendingOnFailure ||
        confidence < config.capturePipeline.pendingConfidence)
    ) {
      continue;
    }
    const key = `${category}:${normalizedSummary.toLowerCase()}`;
    if (seen.has(key)) {
      continue;
    }
    seen.add(key);
    const evidence = buildSmartExtractionEvidence(messages, normalizedSummary, deps);
    if (evidence.length === 0) {
      continue;
    }
    candidates.push({
      category,
      summary: normalizedSummary,
      confidence,
      evidence,
      pending,
    });
  }
  return candidates;
}

export function buildSmartExtractionTargetUri(
  config: PluginConfig,
  policy: ResolvedAclPolicy,
  category: SmartExtractionCategory,
  summary: string,
  pending: boolean,
  deps: SmartExtractionTargetUriDeps,
): string {
  if (
    config.reconcile.enabled &&
    (category === "profile" || category === "preference" || category === "workflow")
  ) {
    const root = deps.appendUriPath(
      deps.renderTemplate(config.acl.defaultPrivateRootTemplate, { agentId: policy.agentKey }),
      pending ? "pending" : "captured",
    );
    return deps.appendUriPath(root, "llm-extracted", category, "current");
  }
  return deps.buildDurableSynthesisUri(
    config,
    policy,
    "llm_extracted",
    category,
    summary,
    pending,
  );
}

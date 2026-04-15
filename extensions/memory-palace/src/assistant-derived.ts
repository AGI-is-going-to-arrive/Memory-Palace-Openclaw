import { createHash } from "node:crypto";
import type {
  AssistantDerivedCandidate,
  DurableSynthesisEvidence,
  PluginConfig,
} from "./types.js";

const ASSISTANT_DERIVED_WORKFLOW_PATTERNS = [
  /\b(code|coding|test|tests|testing|review|reviews|reviewing|findings|fix|patch|docs?|document|documentation|verify|retest)\b/iu,
  /(代码|测试|评审|review|findings|修复|补文档|文档|复测|验证)/u,
] as const;
const ASSISTANT_DERIVED_META_PATTERNS = [
  /\b(this turn|this session|for now|temporary|temp|one[- ]off|meta)\b/iu,
  /(本轮|当前这次|临时|暂时|这次先|元对话|只是示例)/u,
] as const;
const ASSISTANT_DERIVED_UNCERTAIN_PATTERNS = [
  /\b(maybe|perhaps|probably|guess|seems?)\b/iu,
  /(可能|也许|大概|似乎)/u,
] as const;
const ASSISTANT_DERIVED_SUMMARY_PATTERNS = [
  /\b(default workflow|default process|usual process|workflow|review order|delivery order)\b/iu,
  /\b(prefer|preference|likes?|wants?|default delivery preference)\b/iu,
  /(默认工作流|默认流程|固定流程|工作流|默认交付习惯|默认顺序|交付顺序|协作顺序|偏好|习惯)/u,
] as const;

type WorkflowSummarySegment = { userIndex: number; text: string };

export type AssistantDerivedDeps = {
  cleanMessageTextForReasoning: (text: string) => string;
  computeTextSimilarity: (left: string, right: string) => number;
  countLines: (text: string) => number;
  countTokenOverlap: (left: string[], right: string[]) => number;
  extractMessageTexts: (messages: unknown[], allowedRoles?: string[]) => string[];
  extractTextBlocks: (content: unknown) => string[];
  inferCaptureCategory: (text: string) => string;
  isRecord: (value: unknown) => value is Record<string, unknown>;
  isSensitiveHostBridgeText: (text: string) => boolean;
  looksLikePromptInjection: (text: string) => boolean;
  normalizeText: (text: string) => string;
  normalizeWorkflowProfileStep: (text: string) => string;
  shouldAutoCapture: (text: string, config: PluginConfig["autoCapture"]) => boolean;
  splitProfileCaptureSegments: (text: string) => string[];
  stripCodeBlocks: (text: string) => string;
  tokenizeForHostBridge: (text: string) => string[];
  truncate: (text: string, limit: number) => string;
  workflowStableHintPatterns: readonly RegExp[];
  profileCaptureEphemeralPatterns: readonly RegExp[];
};

export function extractAssistantDerivedSegments(
  text: string,
  deps: Pick<
    AssistantDerivedDeps,
    "cleanMessageTextForReasoning" | "looksLikePromptInjection" | "normalizeText" | "stripCodeBlocks"
  >,
): string[] {
  return deps.stripCodeBlocks(deps.cleanMessageTextForReasoning(text))
    .split(/[\n。！？!?]+/u)
    .map((entry) => deps.normalizeText(entry))
    .filter(Boolean)
    .filter((entry) => entry.length >= 10 && entry.length <= 240)
    .filter((entry) => !ASSISTANT_DERIVED_META_PATTERNS.some((pattern) => pattern.test(entry)))
    .filter((entry) => !ASSISTANT_DERIVED_UNCERTAIN_PATTERNS.some((pattern) => pattern.test(entry)))
    .filter((entry) => !deps.looksLikePromptInjection(entry));
}

export function collectAssistantDerivedEvidence(
  userMessages: string[],
  summary: string,
  deps: Pick<
    AssistantDerivedDeps,
    | "cleanMessageTextForReasoning"
    | "countTokenOverlap"
    | "looksLikePromptInjection"
    | "profileCaptureEphemeralPatterns"
    | "splitProfileCaptureSegments"
    | "stripCodeBlocks"
    | "tokenizeForHostBridge"
    | "truncate"
  >,
): { evidence: DurableSynthesisEvidence[]; overlapCount: number } {
  const summaryTokens = deps.tokenizeForHostBridge(summary);
  const evidence: DurableSynthesisEvidence[] = [];
  let overlapCount = 0;
  userMessages.forEach((message, index) => {
    const cleaned = deps.stripCodeBlocks(deps.cleanMessageTextForReasoning(message));
    for (const segment of deps.splitProfileCaptureSegments(cleaned)) {
      if (
        deps.profileCaptureEphemeralPatterns.some((pattern) => pattern.test(segment)) ||
        deps.looksLikePromptInjection(segment)
      ) {
        continue;
      }
      const overlap = deps.countTokenOverlap(
        summaryTokens,
        deps.tokenizeForHostBridge(segment),
      );
      if (overlap < 2) {
        continue;
      }
      overlapCount += overlap;
      evidence.push({
        key: `user-${index + 1}-${evidence.length + 1}`,
        source: `user_message[${index + 1}]`,
        lineStart: index + 1,
        lineEnd: index + 1,
        snippet: deps.truncate(segment, 220),
      });
      break;
    }
  });
  return { evidence, overlapCount };
}

export function buildAssistantDerivedWorkflowEvidence(
  messageIndex: number,
  text: string,
  deps: Pick<AssistantDerivedDeps, "countLines" | "normalizeText" | "truncate">,
): DurableSynthesisEvidence {
  const normalized = deps.normalizeText(text);
  const hash = createHash("sha256").update(normalized).digest("hex").slice(0, 12);
  return {
    key: `user_message[${messageIndex + 1}] sha256-${hash}`,
    source: `user_message[${messageIndex + 1}]`,
    lineStart: 1,
    lineEnd: deps.countLines(normalized),
    snippet: deps.truncate(normalized, 220),
  };
}

export function collectWorkflowSummarySegments(
  messages: unknown[],
  deps: Pick<
    AssistantDerivedDeps,
    | "cleanMessageTextForReasoning"
    | "extractTextBlocks"
    | "isRecord"
    | "isSensitiveHostBridgeText"
    | "looksLikePromptInjection"
    | "normalizeText"
    | "profileCaptureEphemeralPatterns"
    | "splitProfileCaptureSegments"
    | "workflowStableHintPatterns"
  >,
): WorkflowSummarySegment[] {
  const workflowSegments: WorkflowSummarySegment[] = [];
  let userIndex = 0;
  messages.forEach((message) => {
    if (!deps.isRecord(message) || message.role !== "user") {
      return;
    }
    userIndex += 1;
    for (const text of deps.extractTextBlocks(message.content).map((entry) =>
      deps.cleanMessageTextForReasoning(entry)
    )) {
      for (const segment of deps.splitProfileCaptureSegments(text)) {
        for (const rawPart of segment.split(/[;；]+/u).flatMap((entry) => entry.split(/[.。！？!?]+/u))) {
          const normalized = deps.normalizeText(rawPart);
          if (
            !normalized ||
            deps.looksLikePromptInjection(normalized) ||
            deps.isSensitiveHostBridgeText(normalized) ||
            deps.profileCaptureEphemeralPatterns.some((pattern) => pattern.test(normalized)) ||
            (!ASSISTANT_DERIVED_WORKFLOW_PATTERNS.some((pattern) => pattern.test(normalized)) &&
              !deps.workflowStableHintPatterns.some((pattern) => pattern.test(normalized)))
          ) {
            continue;
          }
          workflowSegments.push({ userIndex, text: normalized });
        }
      }
    }
  });
  return workflowSegments;
}

export function synthesizeWorkflowSummary(
  messages: unknown[],
  preferredSummary: string,
  deps: Pick<
    AssistantDerivedDeps,
    "cleanMessageTextForReasoning" | "extractTextBlocks" | "isRecord" | "isSensitiveHostBridgeText" | "looksLikePromptInjection" | "normalizeText" | "profileCaptureEphemeralPatterns" | "splitProfileCaptureSegments" | "workflowStableHintPatterns"
  >,
): string {
  const workflowSegments = collectWorkflowSummarySegments(messages, deps);
  const orderedSegments = Array.from(
    new Map(
      workflowSegments.map((entry) => [deps.normalizeText(entry.text).toLowerCase(), entry]),
    ).values(),
  )
    .sort((left, right) => left.userIndex - right.userIndex)
    .slice(0, 4);
  if (orderedSegments.length < 2) {
    return preferredSummary;
  }
  const prefixSource = `${preferredSummary} ${orderedSegments.map((entry) => entry.text).join(" ")}`;
  const useCjk = /[\u3400-\u9fff]/u.test(prefixSource);
  const prefix = useCjk ? "默认工作流：" : "Default workflow: ";
  const separator = useCjk ? "；" : "; ";
  return `${prefix}${orderedSegments.map((entry) => entry.text).join(separator)}`;
}

export function extractWorkflowSummarySteps(
  text: string,
  deps: Pick<AssistantDerivedDeps, "normalizeText" | "splitProfileCaptureSegments" | "workflowStableHintPatterns">,
): string[] {
  return Array.from(
    new Map(
      deps.splitProfileCaptureSegments(text)
        .flatMap((entry) => entry.split(/[;；]+/u))
        .map((entry) => deps.normalizeText(entry))
        .filter(
          (entry) =>
            Boolean(entry) &&
            (ASSISTANT_DERIVED_WORKFLOW_PATTERNS.some((pattern) => pattern.test(entry)) ||
              deps.workflowStableHintPatterns.some((pattern) => pattern.test(entry))),
        )
        .map((entry) => [entry.toLowerCase(), entry] as const),
    ).values(),
  );
}

export function countWorkflowSummarySteps(
  text: string,
  deps: Pick<AssistantDerivedDeps, "normalizeText" | "splitProfileCaptureSegments" | "workflowStableHintPatterns">,
): number {
  return extractWorkflowSummarySteps(text, deps).length;
}

export function workflowSummaryCovers(
  existingSummary: string,
  candidateSummary: string,
  deps: Pick<
    AssistantDerivedDeps,
    "computeTextSimilarity" | "normalizeText" | "splitProfileCaptureSegments" | "workflowStableHintPatterns"
  >,
): boolean {
  const existingSteps = extractWorkflowSummarySteps(existingSummary, deps);
  const candidateSteps = extractWorkflowSummarySteps(candidateSummary, deps);
  if (candidateSteps.length === 0 || existingSteps.length <= candidateSteps.length) {
    return false;
  }
  return candidateSteps.every((candidateStep) =>
    existingSteps.some((existingStep) => {
      const normalizedExisting = deps.normalizeText(existingStep).toLowerCase();
      const normalizedCandidate = deps.normalizeText(candidateStep).toLowerCase();
      return (
        normalizedExisting === normalizedCandidate ||
        normalizedExisting.includes(normalizedCandidate) ||
        normalizedCandidate.includes(normalizedExisting) ||
        deps.computeTextSimilarity(existingStep, candidateStep) >= 0.45
      );
    }),
  );
}

export function mergeWorkflowSummaries(
  existingSummary: string,
  candidateSummary: string,
  deps: Pick<
    AssistantDerivedDeps,
    | "computeTextSimilarity"
    | "normalizeText"
    | "normalizeWorkflowProfileStep"
    | "splitProfileCaptureSegments"
    | "workflowStableHintPatterns"
  >,
): string {
  const mergedSteps: string[] = [];
  const mergeStep = (step: string) => {
    const normalizedStep = deps.normalizeWorkflowProfileStep(step);
    if (!normalizedStep) {
      return;
    }
    const existingIndex = mergedSteps.findIndex((current) => {
      const normalizedCurrent = deps.normalizeText(current);
      return (
        normalizedCurrent.toLowerCase() === normalizedStep.toLowerCase() ||
        normalizedCurrent.toLowerCase().includes(normalizedStep.toLowerCase()) ||
        normalizedStep.toLowerCase().includes(normalizedCurrent.toLowerCase()) ||
        deps.computeTextSimilarity(normalizedCurrent, normalizedStep) >= 0.45
      );
    });
    if (existingIndex === -1) {
      mergedSteps.push(normalizedStep);
      return;
    }
    if (normalizedStep.length > mergedSteps[existingIndex].length) {
      mergedSteps[existingIndex] = normalizedStep;
    }
  };

  extractWorkflowSummarySteps(existingSummary, deps).forEach(mergeStep);
  extractWorkflowSummarySteps(candidateSummary, deps).forEach(mergeStep);

  if (mergedSteps.length === 0) {
    return candidateSummary || existingSummary;
  }
  if (mergedSteps.length === 1) {
    return mergedSteps[0];
  }
  const useCjk = /[\u3400-\u9fff]/u.test(`${existingSummary} ${candidateSummary}`);
  const prefix = useCjk ? "默认工作流：" : "Default workflow: ";
  const separator = useCjk ? "；" : "; ";
  return `${prefix}${mergedSteps.join(separator)}`;
}

export function buildAssistantDerivedWorkflowFallback(
  messages: unknown[],
  config: PluginConfig["capturePipeline"],
  deps: Pick<
    AssistantDerivedDeps,
    | "cleanMessageTextForReasoning"
    | "countLines"
    | "extractMessageTexts"
    | "extractTextBlocks"
    | "isRecord"
    | "isSensitiveHostBridgeText"
    | "looksLikePromptInjection"
    | "normalizeText"
    | "profileCaptureEphemeralPatterns"
    | "splitProfileCaptureSegments"
    | "truncate"
    | "workflowStableHintPatterns"
  >,
): AssistantDerivedCandidate | undefined {
  const assistantText = deps.normalizeText(
    deps.extractMessageTexts(messages, ["assistant"]).slice(-1)[0] ?? "",
  );
  const workflowSegments = collectWorkflowSummarySegments(messages, deps).filter((entry) =>
    ASSISTANT_DERIVED_WORKFLOW_PATTERNS.some((pattern) => pattern.test(entry.text))
  );
  const distinctMessages = new Set(workflowSegments.map((entry) => entry.userIndex));
  const uniqueSegments = Array.from(
    new Map(
      workflowSegments.map((entry) => [deps.normalizeText(entry.text).toLowerCase(), entry]),
    ).values(),
  );
  if (uniqueSegments.length < 2) {
    return undefined;
  }
  const orderedSegments = uniqueSegments
    .sort((left, right) => left.userIndex - right.userIndex)
    .slice(0, 4);
  const assistantMatchesWorkflow = ASSISTANT_DERIVED_WORKFLOW_PATTERNS.some((pattern) =>
    pattern.test(assistantText)
  );
  const stableHint = orderedSegments.some((entry) =>
    deps.workflowStableHintPatterns.some((pattern) => pattern.test(entry.text))
  );
  const sequenceHint = orderedSegments.some((entry) =>
    /\b(first|then|after|finally|next)\b/iu.test(entry.text) || /(先|然后|再|最后|下一步)/u.test(entry.text)
  );
  const singleMessageStructuredWorkflow =
    distinctMessages.size === 1 && orderedSegments.length >= 2 && stableHint && sequenceHint;
  if (distinctMessages.size < 2 && !singleMessageStructuredWorkflow) {
    return undefined;
  }
  let confidence = Math.max(
    0.05,
    Math.min(
      0.92,
      0.42 +
        Math.min(0.18, (distinctMessages.size - 1) * 0.12) +
        Math.min(0.12, (orderedSegments.length - 1) * 0.08) +
        (assistantMatchesWorkflow ? 0.08 : 0) +
        (stableHint ? 0.08 : 0) +
        (sequenceHint ? 0.08 : 0),
    ),
  );
  if (singleMessageStructuredWorkflow) {
    confidence = Math.max(
      confidence,
      Math.min(0.92, Math.max(config.minConfidence, 0.72)),
    );
  }
  const pending = confidence < config.minConfidence;
  if (pending && (!config.pendingOnFailure || confidence < config.pendingConfidence)) {
    return undefined;
  }
  return {
    category: "workflow",
    summary: synthesizeWorkflowSummary(
      messages,
      `默认工作流：${orderedSegments.map((entry) => entry.text).join("；")}`,
      deps,
    ),
    confidence,
    evidence: orderedSegments.map((entry) =>
      buildAssistantDerivedWorkflowEvidence(entry.userIndex - 1, entry.text, deps),
    ),
    pending,
  };
}

export function buildAssistantDerivedCandidates(
  messages: unknown[],
  config: PluginConfig,
  deps: AssistantDerivedDeps,
): AssistantDerivedCandidate[] {
  if (config.capturePipeline.mode !== "v2" || !config.capturePipeline.captureAssistantDerived) {
    return [];
  }
  const userMessages = deps.extractMessageTexts(messages, ["user"]);
  const assistantMessages = deps.extractMessageTexts(messages, ["assistant"]).slice(-3);
  if (userMessages.length === 0 || assistantMessages.length === 0) {
    return [];
  }
  const directCaptureCategories = config.autoCapture.enabled
    ? new Set(
        userMessages
          .filter((text) => deps.shouldAutoCapture(text, config.autoCapture))
          .map((text) => deps.inferCaptureCategory(text)),
      )
    : new Set<string>();
  const candidates: AssistantDerivedCandidate[] = [];
  const seen = new Set<string>();
  for (const assistantMessage of assistantMessages) {
    for (const segment of extractAssistantDerivedSegments(assistantMessage, deps)) {
      if (!ASSISTANT_DERIVED_SUMMARY_PATTERNS.some((pattern) => pattern.test(segment))) {
        continue;
      }
      const category = deps.inferCaptureCategory(segment);
      if (!["workflow", "preference", "profile"].includes(category)) {
        continue;
      }
      if (directCaptureCategories.has(category)) {
        continue;
      }
      const normalizedSummary = deps.normalizeText(segment);
      const key = `${category}:${normalizedSummary.toLowerCase()}`;
      if (seen.has(key)) {
        continue;
      }
      const { evidence, overlapCount } = collectAssistantDerivedEvidence(
        userMessages,
        normalizedSummary,
        deps,
      );
      if (evidence.length === 0 || overlapCount < 3) {
        continue;
      }
      const summaryHasWorkflowSignal =
        category === "workflow" &&
        ASSISTANT_DERIVED_WORKFLOW_PATTERNS.some((pattern) => pattern.test(normalizedSummary));
      const confidence = Math.max(
        0,
        Math.min(
          0.98,
          0.34 +
            Math.min(0.24, evidence.length * 0.12) +
            Math.min(0.26, overlapCount * 0.04) +
            (summaryHasWorkflowSignal ? 0.12 : 0.04),
        ),
      );
      const pending = confidence < config.capturePipeline.minConfidence;
      if (
        pending &&
        (!config.capturePipeline.pendingOnFailure ||
          confidence < config.capturePipeline.pendingConfidence)
      ) {
        continue;
      }
      seen.add(key);
      candidates.push({
        category,
        summary: normalizedSummary,
        confidence,
        evidence,
        pending,
      });
    }
  }
  if (!directCaptureCategories.has("workflow")) {
    const fallbackCandidate = buildAssistantDerivedWorkflowFallback(
      messages,
      config.capturePipeline,
      deps,
    );
    if (fallbackCandidate) {
      const fallbackKey = `${fallbackCandidate.category}:${deps.normalizeText(fallbackCandidate.summary).toLowerCase()}`;
      if (!seen.has(fallbackKey)) {
        seen.add(fallbackKey);
        candidates.push(fallbackCandidate);
      }
    }
  }
  const mergedByCategory = new Map<string, AssistantDerivedCandidate>();
  for (const candidate of candidates) {
    const existing = mergedByCategory.get(candidate.category);
    if (!existing) {
      mergedByCategory.set(candidate.category, candidate);
      continue;
    }
    mergedByCategory.set(candidate.category, {
      category: candidate.category,
      summary: Array.from(
        new Set(
          [existing.summary, candidate.summary]
            .map((entry) => deps.normalizeText(entry))
            .filter(Boolean),
        ),
      ).join("；"),
      confidence: Math.max(existing.confidence, candidate.confidence),
      evidence: Array.from(
        new Map(
          [...existing.evidence, ...candidate.evidence].map((entry) => [
            deps.normalizeText(entry.snippet),
            entry,
          ]),
        ).values(),
      ),
      pending: existing.pending && candidate.pending,
    });
  }
  return Array.from(mergedByCategory.values()).slice(
    0,
    config.capturePipeline.maxAssistantDerivedPerRun,
  );
}

export function countAssistantDerivedConversationMessages(
  messages: unknown[],
  deps: Pick<AssistantDerivedDeps, "extractMessageTexts">,
): number {
  return deps.extractMessageTexts(messages, ["user", "assistant"]).length;
}

export function trimAssistantDerivedMessages(
  messages: unknown[],
  maxChars: number,
  deps: Pick<
    AssistantDerivedDeps,
    "cleanMessageTextForReasoning" | "extractTextBlocks" | "isRecord" | "normalizeText"
  >,
): unknown[] {
  const budget = Math.max(0, Math.trunc(maxChars));
  if (budget <= 0) {
    return [];
  }

  const selected: Array<{ role: "user" | "assistant"; content: string[] }> = [];
  let remaining = budget;

  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index];
    if (!deps.isRecord(message)) {
      continue;
    }
    const role = typeof message.role === "string" ? message.role.trim().toLowerCase() : "";
    if (role !== "user" && role !== "assistant") {
      continue;
    }
    const normalizedText = deps.extractTextBlocks(message.content)
      .map((entry) => deps.cleanMessageTextForReasoning(entry))
      .map((entry) => deps.normalizeText(entry))
      .filter(Boolean)
      .join(" ")
      .trim();
    if (!normalizedText) {
      continue;
    }

    if (selected.length > 0 && normalizedText.length + 1 > remaining) {
      break;
    }

    const boundedText =
      normalizedText.length > remaining
        ? normalizedText.slice(0, remaining).trim()
        : normalizedText;
    if (!boundedText) {
      break;
    }

    selected.unshift({
      role: role as "user" | "assistant",
      content: [boundedText],
    });
    remaining -= boundedText.length + (selected.length > 1 ? 1 : 0);
    if (remaining <= 0) {
      break;
    }
  }

  return selected;
}

export function buildAssistantDerivedUri(
  policy: { agentKey: string },
  category: string,
  summary: string,
  pending: boolean,
  deps: Pick<AssistantDerivedDeps, "normalizeText"> & {
    appendUriPath: (baseUri: string, ...segments: Array<string | undefined>) => string;
    profileBlockRootUri: string;
  },
): string {
  return deps.appendUriPath(
    deps.profileBlockRootUri,
    policy.agentKey,
    "assistant-derived",
    pending ? "pending" : "committed",
    category,
    `sha256-${createHash("sha256").update(deps.normalizeText(summary)).digest("hex").slice(0, 12)}`,
  );
}

export function isPendingAssistantDerivedUri(uri: string, defaultDomain: string): boolean {
  if (!uri.startsWith("core://agents") && !uri.startsWith(`${defaultDomain}://agents`)) {
    return false;
  }
  const normalized = uri.replace(/\\/g, "/");
  return /\/pending\/assistant-derived\//u.test(normalized) || /\/assistant-derived\/pending\//u.test(normalized);
}

export function buildAssistantDerivedContent(
  candidate: AssistantDerivedCandidate,
  evidenceLines: string[],
): string {
  const title = candidate.pending ? "# Assistant Derived Candidate" : "# Memory Palace Durable Fact";
  const contentHeading = candidate.pending ? "## Content" : "## Summary";
  return [
    title,
    `- category: ${candidate.category}`,
    "- capture_layer: assistant_derived_candidate",
    "- source_mode: assistant_derived",
    `- confidence: ${candidate.confidence.toFixed(2)}`,
    `- pending: ${candidate.pending ? "true" : "false"}`,
    "",
    contentHeading,
    candidate.summary,
    "",
    "## User Evidence",
    ...evidenceLines.map((entry) => `- ${entry}`),
  ].join("\n");
}

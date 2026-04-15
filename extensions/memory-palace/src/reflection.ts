import { createHash } from "node:crypto";
import type { PluginConfig, ResolvedAclPolicy } from "./types.js";

const OPEN_LOOP_PATTERNS = [
  /\b(todo|follow up|next step|blocked|pending|need to|action item)\b/iu,
  /(待办|后续|阻塞|未完成|下一步|需要继续)/u,
] as const;
const LESSON_PATTERNS = [
  /\b(learned|lesson|avoid|should|better to|next time)\b/iu,
  /(经验|教训|下次|应该|避免|最好)/u,
] as const;
const INVARIANT_PATTERNS = [
  /\b(always|never|must|policy|rule)\b/iu,
  /(必须|不要|永远|规则|原则)/u,
] as const;

export type ReflectionDeps = {
  appendUriPath: (baseUri: string, ...segments: Array<string | undefined>) => string;
  extractMessageTexts: (messages: unknown[], allowedRoles?: string[]) => string[];
  isRecord: (value: unknown) => value is Record<string, unknown>;
  normalizeText: (text: string) => string;
  safeSegment: (value: string | undefined) => string;
};

export function buildReflectionUri(
  config: PluginConfig,
  policy: ResolvedAclPolicy,
  sessionRef: string,
  sourceText: string,
  deps: Pick<ReflectionDeps, "appendUriPath" | "normalizeText" | "safeSegment">,
): string {
  const rootUri = deps.appendUriPath(config.reflection.rootUri, policy.agentKey);
  const timestamp = new Date().toISOString().slice(0, 10).split("-");
  const digest = createHash("sha256")
    .update(`${sessionRef}:${deps.normalizeText(sourceText)}`)
    .digest("hex")
    .slice(0, 12);
  return deps.appendUriPath(rootUri, ...timestamp, `session-${deps.safeSegment(sessionRef)}-${digest}`);
}

export function bucketReflectionLines(summary: string): {
  event: string[];
  invariant: string[];
  derived: string[];
  openLoops: string[];
  lessons: string[];
} {
  const event: string[] = [];
  const invariant: string[] = [];
  const derived: string[] = [];
  const openLoops: string[] = [];
  const lessons: string[] = [];
  const lines = summary
    .split(/\r?\n/)
    .map((line) => line.replace(/^[-*]\s*/, "").trim())
    .filter(Boolean);
  for (const line of lines) {
    if (OPEN_LOOP_PATTERNS.some((pattern) => pattern.test(line))) {
      openLoops.push(line);
      continue;
    }
    const matchesInvariant = INVARIANT_PATTERNS.some((pattern) => pattern.test(line));
    if (matchesInvariant) {
      invariant.push(line);
      continue;
    }
    if (LESSON_PATTERNS.some((pattern) => pattern.test(line))) {
      lessons.push(line);
      continue;
    }
    if (event.length < 3) {
      event.push(line);
      continue;
    }
    derived.push(line);
  }
  if (event.length === 0 && lines[0]) {
    event.push(lines[0]);
  }
  return { event, invariant, derived, openLoops, lessons };
}

export function buildReflectionContent(params: {
  agentId?: string;
  sessionId?: string;
  sessionKey?: string;
  source: "agent_end" | "compact_context" | "command_new";
  summary: string;
  trigger?: string;
  summaryMethod?: string;
  compactSourceUri?: string;
  compactSourceHash?: string;
  compactGistMethod?: string;
  messageCount?: number;
  turnCountEstimate?: number;
  decayHintDays?: number;
  retentionClass?: string;
}): string {
  const buckets = bucketReflectionLines(params.summary);
  return [
    "# Reflection Lane",
    `- source: ${params.source}`,
    `- generated_at: ${new Date().toISOString()}`,
    `- trigger: ${params.trigger ?? params.source}`,
    `- summary_method: ${params.summaryMethod ?? "message_rollup_v1"}`,
    ...(params.agentId ? [`- agent_id: ${params.agentId}`] : []),
    ...(params.sessionId ? [`- session_id: ${params.sessionId}`] : []),
    ...(params.sessionKey ? [`- session_key: ${params.sessionKey}`] : []),
    ...(params.compactSourceUri
      ? [`- compact_source_uri: ${params.compactSourceUri}`]
      : []),
    ...(params.compactSourceHash
      ? [`- compact_source_hash: ${params.compactSourceHash}`]
      : []),
    ...(params.compactGistMethod
      ? [`- compact_gist_method: ${params.compactGistMethod}`]
      : []),
    ...(params.messageCount !== undefined ? [`- message_count: ${params.messageCount}`] : []),
    ...(params.turnCountEstimate !== undefined ? [`- turn_count_estimate: ${params.turnCountEstimate}`] : []),
    ...(params.decayHintDays !== undefined ? [`- decay_hint_days: ${params.decayHintDays}`] : []),
    ...(params.retentionClass ? [`- retention_class: ${params.retentionClass}`] : []),
    "",
    "## event",
    ...(buckets.event.length > 0 ? buckets.event.map((line) => `- ${line}`) : ["- (none)"]),
    "",
    "## invariant",
    ...(buckets.invariant.length > 0 ? buckets.invariant.map((line) => `- ${line}`) : ["- (none)"]),
    "",
    "## derived",
    ...(buckets.derived.length > 0 ? buckets.derived.map((line) => `- ${line}`) : ["- (none)"]),
    "",
    "## open_loops",
    ...(buckets.openLoops.length > 0 ? buckets.openLoops.map((line) => `- ${line}`) : ["- (none)"]),
    "",
    "## lessons",
    ...(buckets.lessons.length > 0 ? buckets.lessons.map((line) => `- ${line}`) : ["- (none)"]),
  ].join("\n");
}

export function buildReflectionSummaryFromMessages(
  messages: unknown[],
  maxMessages = 8,
  deps: Pick<ReflectionDeps, "extractMessageTexts">,
): string {
  const extracted = deps.extractMessageTexts(messages, ["user", "assistant"]).slice(
    -Math.max(1, maxMessages),
  );
  if (extracted.length === 0) {
    return "";
  }
  return extracted.join("\n- ");
}

export function estimateConversationTurnCount(
  messages: unknown[],
  deps: Pick<ReflectionDeps, "isRecord">,
): number {
  let userCount = 0;
  let assistantCount = 0;
  for (const message of messages) {
    if (!deps.isRecord(message) || typeof message.role !== "string") {
      continue;
    }
    if (message.role === "user") {
      userCount += 1;
      continue;
    }
    if (message.role === "assistant") {
      assistantCount += 1;
    }
  }
  return Math.max(userCount, assistantCount, userCount > 0 || assistantCount > 0 ? 1 : 0);
}

export function isCommandNewStartupEvent(
  event: Record<string, unknown>,
  ctx: Record<string, unknown>,
  deps: Pick<ReflectionDeps, "extractMessageTexts" | "isRecord">,
): boolean {
  const startupPattern =
    /a new session was started via \/new or \/reset|run your session startup sequence/iu;
  const messageCandidates = [
    event.messages,
    ctx.messages,
    deps.isRecord(event.context) ? event.context.messages : undefined,
  ];
  for (const candidate of messageCandidates) {
    if (!Array.isArray(candidate)) {
      continue;
    }
    const text = deps.extractMessageTexts(candidate, ["user", "assistant"]).join("\n");
    if (startupPattern.test(text)) {
      return true;
    }
  }
  return false;
}

export function extractCompactContextTrace(text: string): string {
  const matched = text.match(/## Trace\s+([\s\S]*)$/i);
  if (matched?.[1]?.trim()) {
    return matched[1].trim();
  }
  const gistMatched = text.match(/## Gist\s+([\s\S]*?)\n## /i);
  if (gistMatched?.[1]?.trim()) {
    return gistMatched[1].trim();
  }
  return text.trim();
}

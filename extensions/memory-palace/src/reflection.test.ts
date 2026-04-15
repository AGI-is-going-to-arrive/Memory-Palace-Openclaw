import { describe, expect, test } from "bun:test";
import {
  bucketReflectionLines,
  buildReflectionContent,
  estimateConversationTurnCount,
} from "./reflection.ts";

/* ---------- bucketReflectionLines ---------- */

describe("bucketReflectionLines", () => {
  test("classifies open-loop lines by keyword", () => {
    const result = bucketReflectionLines("- TODO: fix the parser\n- follow up on the API design");
    expect(result.openLoops.length).toBe(2);
    expect(result.openLoops[0]).toContain("TODO");
  });

  test("classifies invariant lines by keyword", () => {
    const result = bucketReflectionLines("- Always validate inputs\n- Never store plaintext passwords");
    expect(result.invariant.length).toBe(2);
  });

  test("classifies lesson lines by keyword", () => {
    const result = bucketReflectionLines("- Learned that caching helps\n- Next time use batch inserts");
    expect(result.lessons.length).toBe(2);
  });

  test("puts initial unclassified lines into event bucket", () => {
    const result = bucketReflectionLines("Deployed version 2.0\nFixed a bug in search");
    expect(result.event.length).toBe(2);
    expect(result.event[0]).toBe("Deployed version 2.0");
  });

  test("overflows unclassified lines beyond 3 into derived bucket", () => {
    const lines = "Line A\nLine B\nLine C\nLine D\nLine E";
    const result = bucketReflectionLines(lines);
    expect(result.event.length).toBe(3);
    expect(result.derived.length).toBe(2);
  });

  test("handles empty string with fallback", () => {
    const result = bucketReflectionLines("");
    expect(result.event).toEqual([]);
    expect(result.invariant).toEqual([]);
    expect(result.derived).toEqual([]);
    expect(result.openLoops).toEqual([]);
    expect(result.lessons).toEqual([]);
  });

  test("classifies Chinese open-loop keywords", () => {
    const result = bucketReflectionLines("- 待办：完成文档\n- 需要继续优化性能");
    expect(result.openLoops.length).toBe(2);
  });
});

/* ---------- buildReflectionContent ---------- */

describe("buildReflectionContent", () => {
  test("produces structured markdown with all sections", () => {
    const content = buildReflectionContent({
      source: "agent_end",
      summary: "Deployed v2\nAlways test first\nTODO: update docs\nLearned to use cache",
    });
    expect(content).toContain("# Reflection Lane");
    expect(content).toContain("- source: agent_end");
    expect(content).toContain("## event");
    expect(content).toContain("## invariant");
    expect(content).toContain("## open_loops");
    expect(content).toContain("## lessons");
  });

  test("includes optional metadata fields when provided", () => {
    const content = buildReflectionContent({
      source: "compact_context",
      summary: "Some summary",
      agentId: "agent-x",
      sessionId: "sess-123",
      messageCount: 42,
      turnCountEstimate: 21,
      decayHintDays: 7,
      retentionClass: "permanent",
    });
    expect(content).toContain("- agent_id: agent-x");
    expect(content).toContain("- session_id: sess-123");
    expect(content).toContain("- message_count: 42");
    expect(content).toContain("- turn_count_estimate: 21");
    expect(content).toContain("- decay_hint_days: 7");
    expect(content).toContain("- retention_class: permanent");
  });

  test("omits optional fields when not provided", () => {
    const content = buildReflectionContent({
      source: "command_new",
      summary: "Quick note",
    });
    expect(content).not.toContain("agent_id:");
    expect(content).not.toContain("session_id:");
    expect(content).not.toContain("message_count:");
  });

  test("shows (none) for empty buckets", () => {
    const content = buildReflectionContent({
      source: "agent_end",
      summary: "Just one event line",
    });
    expect(content).toContain("- (none)");
  });
});

/* ---------- estimateConversationTurnCount ---------- */

describe("estimateConversationTurnCount", () => {
  const deps = {
    isRecord: (v: unknown): v is Record<string, unknown> =>
      Boolean(v) && typeof v === "object" && !Array.isArray(v),
  };

  test("counts user-assistant pairs as turns", () => {
    const messages = [
      { role: "user", content: "hi" },
      { role: "assistant", content: "hello" },
      { role: "user", content: "bye" },
      { role: "assistant", content: "goodbye" },
    ];
    expect(estimateConversationTurnCount(messages, deps)).toBe(2);
  });

  test("returns 0 for empty messages array", () => {
    expect(estimateConversationTurnCount([], deps)).toBe(0);
  });

  test("returns 1 for a single user message", () => {
    expect(estimateConversationTurnCount([{ role: "user", content: "hello" }], deps)).toBe(1);
  });

  test("ignores non-record entries", () => {
    expect(estimateConversationTurnCount(["not a record", 42, null], deps)).toBe(0);
  });

  test("handles messages missing role field", () => {
    const messages = [{ content: "no role" }, { role: "user", content: "valid" }];
    expect(estimateConversationTurnCount(messages, deps)).toBe(1);
  });

  test("returns max of user and assistant counts", () => {
    const messages = [
      { role: "user", content: "1" },
      { role: "user", content: "2" },
      { role: "assistant", content: "a" },
    ];
    expect(estimateConversationTurnCount(messages, deps)).toBe(2);
  });
});

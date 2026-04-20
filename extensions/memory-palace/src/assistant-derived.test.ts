import { describe, expect, it } from "bun:test";
import type { PluginConfig } from "./types.js";
import {
  buildAssistantDerivedWorkflowFallback,
  trimAssistantDerivedMessages,
} from "./assistant-derived.ts";

const deps = {
  cleanMessageTextForReasoning: (text: string) => text,
  extractTextBlocks: (content: unknown) =>
    Array.isArray(content)
      ? content.flatMap((entry) => {
          if (typeof entry === "string") {
            return [entry];
          }
          if (
            entry &&
            typeof entry === "object" &&
            !Array.isArray(entry) &&
            "type" in entry &&
            "text" in entry &&
            entry.type === "text" &&
            typeof entry.text === "string"
          ) {
            return [entry.text];
          }
          return [];
        })
      : [],
  isRecord: (value: unknown): value is Record<string, unknown> =>
    Boolean(value) && typeof value === "object" && !Array.isArray(value),
  normalizeText: (text: string) => text.replace(/\s+/gu, " ").trim(),
};

const workflowDeps = {
  ...deps,
  countLines: (text: string) => Math.max(1, text.split(/\r?\n/u).length),
  extractMessageTexts: (messages: unknown[], allowedRoles = ["user", "assistant"]) =>
    messages.flatMap((message) => {
      if (!deps.isRecord(message) || typeof message.role !== "string") {
        return [];
      }
      if (!allowedRoles.includes(message.role)) {
        return [];
      }
      return deps.extractTextBlocks(message.content);
    }),
  isSensitiveHostBridgeText: () => false,
  looksLikePromptInjection: () => false,
  profileCaptureEphemeralPatterns: [] as const,
  splitProfileCaptureSegments: (text: string) =>
    text
      .split(/[\n。！？!?]+/u)
      .map((entry) => deps.normalizeText(entry))
      .filter(Boolean),
  truncate: (text: string, limit: number) => text.slice(0, limit),
  workflowStableHintPatterns: [
    /\b(default workflow|default process|workflow|playbook|runbook)\b/iu,
    /(默认工作流|默认流程|工作流|协作顺序)/u,
  ] as const,
};

const captureConfig: PluginConfig["capturePipeline"] = {
  mode: "v2",
  captureAssistantDerived: true,
  maxAssistantDerivedPerRun: 3,
  pendingOnFailure: false,
  minConfidence: 0.6,
  pendingConfidence: 0.45,
  traceEnabled: false,
};

describe("trimAssistantDerivedMessages", () => {
  it("keeps the most recent user/assistant messages inside the transcript budget", () => {
    const trimmed = trimAssistantDerivedMessages(
      [
        { role: "user", content: ["old preference note"] },
        { role: "assistant", content: ["older workflow summary"] },
        { role: "user", content: ["new workflow rule"] },
        { role: "assistant", content: ["latest durable summary"] },
      ],
      48,
      deps,
    );

    expect(trimmed).toEqual([
      { role: "user", content: ["new workflow rule"] },
      { role: "assistant", content: ["latest durable summary"] },
    ]);
  });

  it("truncates an oversized newest message instead of returning the full transcript", () => {
    const trimmed = trimAssistantDerivedMessages(
      [
        {
          role: "assistant",
          content: ["Default workflow: " + "a".repeat(80)],
        },
      ],
      32,
      deps,
    );

    expect(trimmed).toHaveLength(1);
    expect(trimmed[0]).toEqual({
      role: "assistant",
      content: ["Default workflow: " + "a".repeat(14)],
    });
  });
});

describe("buildAssistantDerivedWorkflowFallback", () => {
  it("ignores a single-message workflow that only quotes a documentation example", () => {
    const candidate = buildAssistantDerivedWorkflowFallback(
      [
        {
          role: "user",
          content: [
            {
              type: "text",
              text: "The onboarding docs show this example default workflow: code first; then run tests.",
            },
          ],
        },
        {
          role: "assistant",
          content: [{ type: "text", text: "I will remember that workflow." }],
        },
      ],
      captureConfig,
      workflowDeps,
    );

    expect(candidate).toBeUndefined();
  });

  it("keeps legitimate doc-first workflow steps instead of dropping them as noise", () => {
    const candidate = buildAssistantDerivedWorkflowFallback(
      [
        {
          role: "user",
          content: [{ type: "text", text: "Default workflow: read docs first; then code." }],
        },
        {
          role: "assistant",
          content: [{ type: "text", text: "Understood." }],
        },
      ],
      captureConfig,
      workflowDeps,
    );

    expect(candidate).toEqual(
      expect.objectContaining({
        category: "workflow",
        pending: false,
      }),
    );
    expect(candidate?.summary.toLowerCase()).toContain("read docs first");
    expect(candidate?.summary.toLowerCase()).toContain("then code");
    expect(candidate?.evidence).toHaveLength(2);
  });
});

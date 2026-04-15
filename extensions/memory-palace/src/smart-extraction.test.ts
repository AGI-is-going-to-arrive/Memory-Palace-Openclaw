import { describe, expect, it } from "bun:test";
import {
  buildSmartExtractionEvidence,
  buildSmartExtractionTranscript,
  parseSmartExtractionCandidates,
} from "./smart-extraction.ts";
import { truncateWithEllipsis } from "./utils.ts";

describe("smart-extraction truncate fallback", () => {
  it("keeps the shared default ellipsis behavior intact", () => {
    expect(truncateWithEllipsis("abc  def", 5)).toBe("abc…");
    expect(truncateWithEllipsis("abcdef", 1)).toBe("…");
    expect(truncateWithEllipsis("abcdef", 0)).toBe("…");
  });

  it("supports the smart-extraction legacy truncate semantics via shared utils", () => {
    expect(
      truncateWithEllipsis("abc  def", 5, {
        preserveInputWhenLimitNonPositive: true,
        preserveShortLimitWithoutEllipsis: true,
        trimEnd: false,
      }),
    ).toBe("abc …");
    expect(
      truncateWithEllipsis("abcdef", 1, {
        preserveInputWhenLimitNonPositive: true,
        preserveShortLimitWithoutEllipsis: true,
        trimEnd: false,
      }),
    ).toBe("a");
    expect(
      truncateWithEllipsis("abcdef", 0, {
        preserveInputWhenLimitNonPositive: true,
        preserveShortLimitWithoutEllipsis: true,
        trimEnd: false,
      }),
    ).toBe("abcdef");
  });

  it("uses the shared fallback without changing smart-extraction evidence snippets", () => {
    const segment = `alpha beta ${"x".repeat(207)} yz`;
    const evidence = buildSmartExtractionEvidence(
      [
        {
          role: "user",
          content: [segment],
        },
      ],
      "alpha beta",
      {
        extractTextBlocks: (content) => (Array.isArray(content) ? content.filter((entry): entry is string => typeof entry === "string") : []),
        cleanMessageTextForReasoning: (text) => text,
        normalizeText: (text) => text,
        tokenizeForHostBridge: (text) => text.toLowerCase().split(/\s+/).filter(Boolean),
        splitProfileCaptureSegments: (text) => [text],
        looksLikePromptInjection: () => false,
        isSensitiveHostBridgeText: () => false,
        profileCaptureEphemeralPatterns: [],
        countTokenOverlap: (left, right) => left.filter((entry) => right.includes(entry)).length,
      },
    );

    expect(evidence).toHaveLength(1);
    expect(evidence[0]?.snippet).toBe(`${"alpha beta "}${"x".repeat(207)} …`);
  });

  it("keeps transcript role markers intact when truncation is required", () => {
    const transcript = buildSmartExtractionTranscript(
      [
        { role: "user", content: ["first message"] },
        { role: "assistant", content: ["second message with extra detail"] },
        { role: "user", content: ["third message with extra detail"] },
      ],
      24,
      {
        extractTextBlocks: (content) => (
          Array.isArray(content) ? content.filter((entry): entry is string => typeof entry === "string") : []
        ),
        cleanMessageTextForReasoning: (text) => text,
        normalizeText: (text) => text,
      },
    );

    expect(transcript.startsWith("user[2]: ")).toBe(true);
    expect(transcript.startsWith("ser[2]: ")).toBe(false);
  });

  it("ignores assistant thinking blocks so later workflow steps stay in the transcript budget", () => {
    const transcript = buildSmartExtractionTranscript(
      [
        {
          role: "user",
          content: [{ type: "text", text: "Default workflow: code changes first." }],
        },
        {
          role: "assistant",
          content: [
            { type: "thinking", text: "x".repeat(200) },
            { type: "text", text: "I will keep code changes first." },
          ],
        },
        {
          role: "user",
          content: [{ type: "text", text: "Then run tests immediately after the code changes." }],
        },
        {
          role: "user",
          content: [{ type: "text", text: "Docs should come at the end." }],
        },
      ],
      140,
      {
        extractTextBlocks: (content) => {
          if (!Array.isArray(content)) {
            return [];
          }
          return content.flatMap((entry) => {
            if (typeof entry === "string") {
              return [entry];
            }
            if (entry && typeof entry === "object" && "text" in entry && typeof entry.text === "string") {
              return [entry.text];
            }
            return [];
          });
        },
        cleanMessageTextForReasoning: (text) => text,
        normalizeText: (text) => text,
      },
    );

    expect(transcript).toContain("user[2]: Then run tests immediately after the code changes.");
    expect(transcript).toContain("user[3]: Docs should come at the end.");
    expect(transcript).not.toContain("x".repeat(20));
  });

  it("reclassifies workflow-like preference summaries into the workflow lane", () => {
    const config = {
      smartExtraction: {
        categories: ["profile", "preference", "workflow"],
      },
      reconcile: {
        enabled: true,
      },
      capturePipeline: {
        minConfidence: 0.8,
        pendingOnFailure: true,
        pendingConfidence: 0.5,
      },
    } as any;

    const candidates = parseSmartExtractionCandidates(
      {
        candidates: [
          {
            category: "preference",
            summary:
              "The user's stable long-term workflow preference is: make code changes first, run the tests immediately after the code changes, and keep docs last.",
            confidence: 0.91,
          },
        ],
      },
      config,
      [
        {
          role: "user",
          content: [
            "Default workflow: make code changes first.",
            "Run the tests immediately after the code changes.",
            "Docs should come last.",
          ],
        },
      ],
      {
        extractTextBlocks: (content) =>
          Array.isArray(content) ? content.filter((entry): entry is string => typeof entry === "string") : [],
        cleanMessageTextForReasoning: (text) => text,
        normalizeText: (text) => text,
        tokenizeForHostBridge: (text) => text.toLowerCase().split(/\s+/).filter(Boolean),
        splitProfileCaptureSegments: (text) => [text],
        looksLikePromptInjection: () => false,
        isSensitiveHostBridgeText: () => false,
        profileCaptureEphemeralPatterns: [],
        countTokenOverlap: (left, right) => left.filter((entry) => right.includes(entry)).length,
        normalizeSmartExtractionCategory: (value) => value as any,
        sanitizeDurableSynthesisSummary: (_category, text) => text.trim(),
        synthesizeWorkflowSummary: (_messages, preferredSummary) => preferredSummary,
      },
    );

    expect(candidates).toHaveLength(1);
    expect(candidates[0]?.category).toBe("workflow");
  });
});

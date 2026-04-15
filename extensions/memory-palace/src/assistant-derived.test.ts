import { describe, expect, it } from "bun:test";
import { trimAssistantDerivedMessages } from "./assistant-derived.ts";

const deps = {
  cleanMessageTextForReasoning: (text: string) => text,
  extractTextBlocks: (content: unknown) =>
    Array.isArray(content)
      ? content.filter((entry): entry is string => typeof entry === "string")
      : [],
  isRecord: (value: unknown): value is Record<string, unknown> =>
    Boolean(value) && typeof value === "object" && !Array.isArray(value),
  normalizeText: (text: string) => text.replace(/\s+/gu, " ").trim(),
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

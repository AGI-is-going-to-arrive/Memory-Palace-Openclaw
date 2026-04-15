import { describe, expect, test } from "bun:test";
import {
  redactVisualSensitiveText,
  sanitizeVisualMediaRef,
  normalizeVisualPayload,
} from "./visual-redaction.ts";

describe("redactVisualSensitiveText", () => {
  test("redacts email addresses", () => {
    const result = redactVisualSensitiveText("Contact alice@example.com for info");
    expect(result).toContain("[REDACTED_EMAIL]");
    expect(result).not.toContain("alice@example.com");
  });

  test("redacts Bearer token values", () => {
    const result = redactVisualSensitiveText("authorization: bearer sk-abc123secret");
    expect(result).toContain("[REDACTED]");
    expect(result).not.toContain("sk-abc123secret");
  });

  test("redacts api-key values", () => {
    const result = redactVisualSensitiveText("api_key = my-secret-token-12345");
    expect(result).toContain("[REDACTED]");
    expect(result).not.toContain("my-secret-token-12345");
  });

  test("redacts phone numbers (10+ digits)", () => {
    const result = redactVisualSensitiveText("Call me at +1 (555) 123-4567 please");
    expect(result).toContain("[REDACTED_PHONE]");
  });

  test("does not redact ISO date strings as phone numbers", () => {
    const result = redactVisualSensitiveText("Date: 2025-07-28");
    expect(result).not.toContain("[REDACTED_PHONE]");
    expect(result).toContain("2025-07-28");
  });

  test("returns undefined for undefined input", () => {
    expect(redactVisualSensitiveText(undefined)).toBeUndefined();
  });

  test("returns clean text unchanged", () => {
    const clean = "This is a normal sentence without sensitive data.";
    expect(redactVisualSensitiveText(clean)).toBe(clean);
  });

  test("returns empty string for empty input", () => {
    expect(redactVisualSensitiveText("")).toBe("");
  });
});

describe("sanitizeVisualMediaRef", () => {
  test("hashes data URI and returns digest placeholder", () => {
    const dataUri = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAA";
    const result = sanitizeVisualMediaRef(dataUri);
    expect(result).toMatch(/^data:image\/png;sha256-[a-f0-9]{12}$/);
  });

  test("truncates very long references to sha256 digest", () => {
    const longRef = "https://example.com/" + "a".repeat(600);
    const result = sanitizeVisualMediaRef(longRef);
    expect(result).toMatch(/^sha256-[a-f0-9]{12}$/);
  });

  test("returns undefined for undefined input", () => {
    expect(sanitizeVisualMediaRef(undefined)).toBeUndefined();
  });

  test("returns undefined for empty string input", () => {
    expect(sanitizeVisualMediaRef("")).toBeUndefined();
  });

  test("passes through short clean ref with redaction applied", () => {
    const ref = "https://example.com/image.png";
    const result = sanitizeVisualMediaRef(ref);
    expect(result).toBe("https://example.com/image.png");
  });

  test("redacts email in short media ref", () => {
    const ref = "user@example.com/avatar.png";
    const result = sanitizeVisualMediaRef(ref);
    expect(result).toContain("[REDACTED_EMAIL]");
  });
});

describe("normalizeVisualPayload", () => {
  test("redacts sensitive data in summary field", () => {
    const payload = { summary: "Saw email alice@corp.io on screen" };
    const result = normalizeVisualPayload(payload);
    expect(result.summary).toContain("[REDACTED_EMAIL]");
  });

  test("sanitizes mediaRef data URI", () => {
    const payload = { mediaRef: "data:image/jpeg;base64,/9j/4AAQSkZJRg==" };
    const result = normalizeVisualPayload(payload);
    expect(result.mediaRef).toMatch(/^data:image\/jpeg;sha256-/);
  });

  test("redacts sensitive data in ocr field", () => {
    const payload = { ocr: "token = secret123abc" };
    const result = normalizeVisualPayload(payload);
    expect(result.ocr).toContain("[REDACTED]");
  });

  test("preserves non-sensitive fields unchanged", () => {
    const payload = {
      sourceChannel: "webcam",
      observedAt: "2025-01-01T00:00:00Z",
      confidence: 0.95,
    };
    const result = normalizeVisualPayload(payload);
    expect(result.sourceChannel).toBe("webcam");
    expect(result.observedAt).toBe("2025-01-01T00:00:00Z");
    expect(result.confidence).toBe(0.95);
  });

  test("redacts entities array entries", () => {
    const payload = { entities: ["user@test.com", "clean text"] };
    const result = normalizeVisualPayload(payload);
    expect(result.entities![0]).toContain("[REDACTED_EMAIL]");
    expect(result.entities![1]).toBe("clean text");
  });

  test("handles empty payload", () => {
    const result = normalizeVisualPayload({});
    expect(result).toEqual({});
  });
});

import { describe, expect, it } from "bun:test";
import { SSEClientTransport } from "@modelcontextprotocol/sdk/client/sse.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
import { __testing, MemoryPalaceMcpClient } from "./client.ts";
import { containsCjk, isRecord } from "./utils.ts";

const TEST_STDIO_COMMAND = process.execPath;
const TEST_STDIO_ARGS = ["-e", "console.log('ok')"];

describe("memory-palace client helpers", () => {
  it("parses JSON-ish tool text", () => {
    expect(__testing.parseMaybeJson('{"ok":true}')).toEqual({ ok: true });
    expect(__testing.parseMaybeJson("plain text")).toBe("plain text");
  });

  it("extracts structuredContent first", () => {
    expect(
      __testing.extractToolPayload({
        structuredContent: { ok: true, mode: "hybrid" },
        content: [{ type: "text", text: "ignored" }],
      }),
    ).toEqual({ ok: true, mode: "hybrid" });
  });

  it("extracts text content and auto-parses JSON", () => {
    expect(
      __testing.extractToolPayload({
        content: [{ type: "text", text: '{"ok":true,"count":1}' }],
      }),
    ).toEqual({ ok: true, count: 1 });
  });

  it("normalizes non-empty headers only", () => {
    expect(
      __testing.normalizeHeaderRecord({
        Authorization: " Bearer token ",
        "X-Test": " value ",
        Empty: "   ",
      }),
    ).toEqual({
      Authorization: "Bearer token",
      "X-Test": "value",
    });
  });

  it("extracts MCP error payloads", () => {
    expect(
      __testing.extractToolError({
        isError: true,
        content: [{ type: "text", text: "backend exploded" }],
      }),
    ).toBe("backend exploded");
  });

  it("extracts business-layer payload errors", () => {
    expect(
      __testing.extractPayloadError({
        ok: false,
        error: "backend boom",
      }),
    ).toBe("backend boom");
  });

  it("extracts nested result-wrapped payload errors", () => {
    expect(
      __testing.extractPayloadError({
        result: '{"ok":false,"message":"wrapped boom"}',
      }),
    ).toBe("wrapped boom");
  });

  it("extracts doubly nested result-wrapped payload errors", () => {
    expect(
      __testing.extractPayloadError({
        transport: "stdio",
        result: JSON.stringify({
          result: JSON.stringify({
            ok: false,
            message: "deep wrapped boom",
          }),
        }),
      }),
    ).toBe("deep wrapped boom");
  });

  it("extracts Error-prefixed string payload errors", () => {
    expect(__testing.extractPayloadError("Error: alias failed")).toBe("alias failed");
    expect(
      __testing.extractPayloadError({
        result: "Error: alias failed",
      }),
    ).toBe("alias failed");
  });

  it("redacts sensitive transport diagnostics text", () => {
    expect(
      __testing.redactSensitiveText("Authorization: Bearer secret-token and apiKey=abc123"),
    ).toBe("Authorization: Bearer [REDACTED] and apiKey=[REDACTED]");
    expect(__testing.redactSensitiveText("X-MCP-API-Key: hidden")).toBe(
      "X-MCP-API-Key: [REDACTED]",
    );
  });

  it("builds auto transport candidates with stdio first and sse fallback", () => {
    const candidates = __testing.resolveTransportCandidates({
      transport: "auto",
      stdio: {
        command: TEST_STDIO_COMMAND,
        args: TEST_STDIO_ARGS,
      },
      sse: {
        url: "http://127.0.0.1:8010/sse",
        apiKey: "dev-key",
        headers: {
          "X-Extra": "1",
        },
      },
    });

    expect(candidates).toHaveLength(2);
    expect(candidates[0]?.kind).toBe("stdio");
    expect(candidates[1]?.kind).toBe("sse");
    expect(
      (candidates[1] as { requestInit?: { headers?: HeadersInit } }).requestInit?.headers,
    ).toBeDefined();
  });

  it("honors forced sse mode", () => {
    const candidates = __testing.resolveTransportCandidates({
      transport: "sse",
      stdio: {
        command: TEST_STDIO_COMMAND,
      },
      sse: {
        url: "http://127.0.0.1:8010/sse",
      },
    });

    expect(candidates).toHaveLength(1);
    expect(candidates[0]?.kind).toBe("sse");
  });

  it("normalizes retry defaults and computes bounded exponential backoff", () => {
    expect(__testing.normalizeRetryConfig(undefined)).toEqual({
      attempts: 2,
      baseDelayMs: 250,
      maxDelayMs: 1000,
    });
    expect(
      __testing.normalizeRetryConfig({
        attempts: 3,
        baseDelayMs: 200,
        maxDelayMs: 400,
      }),
    ).toEqual({
      attempts: 3,
      baseDelayMs: 200,
      maxDelayMs: 400,
    });
    expect(
      __testing.backoffDelayForAttempt(1, {
        attempts: 3,
        baseDelayMs: 200,
        maxDelayMs: 400,
      }),
    ).toBe(200);
    expect(
      __testing.backoffDelayForAttempt(2, {
        attempts: 3,
        baseDelayMs: 200,
        maxDelayMs: 400,
      }),
    ).toBe(400);
    expect(
      __testing.backoffDelayForAttempt(4, {
        attempts: 3,
        baseDelayMs: 200,
        maxDelayMs: 400,
      }),
    ).toBe(400);
  });

  it("recognizes retriable transport failures", () => {
    expect(__testing.isRetryableTransportError(new Error("connect timeout after 1000ms"))).toBe(true);
    expect(__testing.isRetryableTransportError(new Error("socket hang up"))).toBe(true);
    expect(__testing.isRetryableTransportError(new Error("business validation failed"))).toBe(false);
  });

  it("only retries safe idempotent tools", () => {
    expect(__testing.isSafeRetryTool("search_memory")).toBe(true);
    expect(__testing.isSafeRetryTool("read_memory")).toBe(true);
    expect(__testing.isSafeRetryTool("index_status")).toBe(true);
    expect(__testing.isSafeRetryTool("rebuild_index")).toBe(false);
    expect(__testing.isSafeRetryTool("compact_context", { force: false })).toBe(true);
    expect(__testing.isSafeRetryTool("compact_context", { force: true })).toBe(false);
    expect(__testing.isSafeRetryTool("create_memory")).toBe(false);
    expect(__testing.isSafeRetryTool("update_memory")).toBe(false);
  });

  it("does not return a cached success while the first health check is still in flight", async () => {
    const client = new MemoryPalaceMcpClient({
      transport: "stdio",
      stdio: { command: TEST_STDIO_COMMAND, args: TEST_STDIO_ARGS },
      healthcheckTtlMs: 5000,
    });
    let releaseConnect: (() => void) | undefined;

    (client as unknown as { ensureConnected: () => Promise<{}> }).ensureConnected = () =>
      new Promise((resolve) => {
        releaseConnect = () => resolve({});
      });
    (client as unknown as { invokeTool: () => Promise<{ ok: true }> }).invokeTool = async () => ({ ok: true });

    const first = client.healthCheck(false);
    let secondSettled = false;
    const second = client.healthCheck(false).then((report) => {
      secondSettled = true;
      return report;
    });

    await Promise.resolve();
    expect(secondSettled).toBe(false);

    releaseConnect?.();
    const [firstReport, secondReport] = await Promise.all([first, second]);

    expect(firstReport.ok).toBe(true);
    expect(secondReport.ok).toBe(true);
  });

  it("does not revive a stale transport after close races with connect", async () => {
    const client = new MemoryPalaceMcpClient({
      transport: "stdio",
      stdio: { command: TEST_STDIO_COMMAND, args: TEST_STDIO_ARGS },
    });
    let releaseConnect: (() => void) | undefined;

    (client as unknown as { connectWithTimeout: () => Promise<void> }).connectWithTimeout = async () =>
      await new Promise<void>((resolve) => {
        releaseConnect = resolve;
      });
    (client as unknown as { disposePendingConnection: () => Promise<void> }).disposePendingConnection =
      async () => undefined;

    const connecting = (client as unknown as { ensureConnected: () => Promise<unknown> }).ensureConnected();
    await Promise.resolve();

    const closePromise = client.close();
    releaseConnect?.();

    await closePromise;
    await expect(connecting).rejects.toThrow("Connection was reset while connecting.");
    expect(client.activeTransportKind).toBeNull();
  });

  it("tracks connect latency summary and event timing after a successful connection", async () => {
    const client = new MemoryPalaceMcpClient({
      transport: "stdio",
      stdio: { command: TEST_STDIO_COMMAND, args: TEST_STDIO_ARGS },
    });

    (client as unknown as { connectWithTimeout: () => Promise<void> }).connectWithTimeout = async () => {
      await new Promise((resolve) => setTimeout(resolve, 5));
    };
    (client as unknown as { disposePendingConnection: () => Promise<void> }).disposePendingConnection =
      async () => undefined;

    await (client as unknown as { ensureConnected: () => Promise<unknown> }).ensureConnected();

    const diagnostics = client.diagnostics;
    expect(diagnostics.connectLatencyMs.samples).toBe(1);
    expect(diagnostics.connectLatencyMs.last).not.toBeNull();
    expect(diagnostics.connectLatencyMs.avg).not.toBeNull();
    expect(diagnostics.connectLatencyMs.p95).not.toBeNull();
    expect(diagnostics.connectLatencyMs.max).not.toBeNull();
    expect(diagnostics.recentEvents.at(-1)?.category).toBe("connect");
    expect(diagnostics.recentEvents.at(-1)?.latencyMs).not.toBeUndefined();

    await client.close();
  });

  it("records recent transport events with redacted failures", async () => {
    const client = new MemoryPalaceMcpClient({
      transport: "stdio",
      stdio: { command: TEST_STDIO_COMMAND, args: TEST_STDIO_ARGS },
      requestRetries: 1,
    });
    (client as unknown as { ensureHealthyBeforeCall: () => Promise<void> }).ensureHealthyBeforeCall =
      async () => undefined;
    (client as unknown as { ensureConnected: () => Promise<{}> }).ensureConnected = async () => ({});
    (client as unknown as { invokeTool: () => Promise<never> }).invokeTool = async () => {
      throw new Error("Authorization: Bearer super-secret");
    };

    await expect(client.searchMemory({ query: "hello" })).rejects.toThrow(
      "Authorization: Bearer super-secret",
    );

    const recentEvent = client.diagnostics.recentEvents.at(-1);
    expect(recentEvent?.category).toBe("tool_call");
    expect(recentEvent?.status).toBe("fail");
    expect(recentEvent?.message).toBe("Authorization: Bearer [REDACTED]");
  });

  it("falls back from stdio to sse after a connect failure", async () => {
    const client = new MemoryPalaceMcpClient({
      transport: "auto",
      stdio: { command: TEST_STDIO_COMMAND, args: TEST_STDIO_ARGS },
      sse: { url: "http://127.0.0.1:8010/sse" },
    });
    const connectAttempts: string[] = [];

    (client as unknown as { connectWithTimeout: (_client: unknown, transport: unknown) => Promise<void> }).connectWithTimeout =
      async (_unusedClient, transport) => {
        if (transport instanceof StdioClientTransport) {
          connectAttempts.push("stdio");
          throw new Error("connect timeout after 1000ms");
        }
        if (transport instanceof SSEClientTransport) {
          connectAttempts.push("sse");
          return;
        }
        throw new Error("unexpected transport");
      };
    (client as unknown as { disposePendingConnection: () => Promise<void> }).disposePendingConnection =
      async () => undefined;

    await (client as unknown as { ensureConnected: () => Promise<unknown> }).ensureConnected();

    expect(connectAttempts).toEqual(["stdio", "sse"]);
    expect(client.activeTransportKind).toBe("sse");
    expect(client.diagnostics.fallbackCount).toBe(1);

    await client.close();
  });

  it("retries stdio connects after timeout and surfaces guidance when all attempts fail", async () => {
    const client = new MemoryPalaceMcpClient({
      transport: "stdio",
      stdio: { command: TEST_STDIO_COMMAND, args: TEST_STDIO_ARGS },
      connectRetries: 1,
      connectBackoffMs: 1,
      connectBackoffMaxMs: 1,
    });

    (client as unknown as { connectWithTimeout: () => Promise<void> }).connectWithTimeout = async () => {
      throw new Error("connect timeout after 1000ms");
    };
    (client as unknown as { disposePendingConnection: () => Promise<void> }).disposePendingConnection =
      async () => undefined;

    let capturedError: unknown;
    try {
      await (client as unknown as { ensureConnected: () => Promise<unknown> }).ensureConnected();
    } catch (error) {
      capturedError = error;
    }

    expect(capturedError).toBeInstanceOf(Error);
    expect((capturedError as Error).message).toBe(
      "Unable to connect to Memory Palace MCP over the configured transports.",
    );
    expect(String((capturedError as { causes?: string[] }).causes?.[0] ?? "")).toContain(
      "attempt 1 stdio: connect timeout after 1000ms",
    );
    expect(String((capturedError as { causes?: string[] }).causes?.[1] ?? "")).toContain(
      "attempt 2 stdio: connect timeout after 1000ms",
    );
    expect(String((capturedError as { causes?: string[] }).causes?.at(-1) ?? "")).toContain(
      "Configured transport order: stdio.",
    );
    expect(client.diagnostics.connectAttempts).toBe(2);
    expect(client.diagnostics.connectRetryCount).toBe(1);
  });

  it("returns the operation result when it finishes before the timeout", async () => {
    const client = new MemoryPalaceMcpClient({
      transport: "stdio",
      timeoutMs: 50,
      stdio: { command: TEST_STDIO_COMMAND, args: TEST_STDIO_ARGS },
    });

    await expect(
      (client as unknown as {
        withOperationTimeout: <T>(operation: Promise<T>, timeoutMessage: string) => Promise<T>;
      }).withOperationTimeout(Promise.resolve("ok"), "request timeout after 50ms"),
    ).resolves.toBe("ok");
  });

  it("defaults timeoutMs to a safe operation timeout when config omits it", () => {
    const client = new MemoryPalaceMcpClient({
      transport: "stdio",
      stdio: { command: TEST_STDIO_COMMAND, args: TEST_STDIO_ARGS },
    });

    expect((client as unknown as { config: { timeoutMs: number } }).config.timeoutMs).toBe(30_000);
  });

  it("applies the default timeout to read-only tool calls when config omits it", async () => {
    const client = new MemoryPalaceMcpClient({
      transport: "stdio",
      requestRetries: 1,
      stdio: { command: TEST_STDIO_COMMAND, args: TEST_STDIO_ARGS },
    });
    let attempts = 0;
    let resetCount = 0;
    let observedDelayMs: number | undefined;
    const originalSetTimeout = globalThis.setTimeout;
    const fakeClient = {
      async callTool() {
        attempts += 1;
        return await new Promise(() => undefined);
      },
    };

    globalThis.setTimeout = ((handler: TimerHandler, delay?: number, ...args: unknown[]) => {
      observedDelayMs = Number(delay ?? 0);
      return originalSetTimeout(handler, 0, ...args);
    }) as typeof setTimeout;

    (client as unknown as { ensureHealthyBeforeCall: () => Promise<void> }).ensureHealthyBeforeCall =
      async () => undefined;
    (client as unknown as { ensureConnected: () => Promise<unknown> }).ensureConnected = async () => fakeClient;
    (client as unknown as { resetConnection: () => Promise<void> }).resetConnection = async () => {
      resetCount += 1;
    };

    try {
      await expect(client.searchMemory({ query: "hello" })).rejects.toThrow(
        "request timeout after 30000ms during search_memory",
      );
    } finally {
      globalThis.setTimeout = originalSetTimeout;
    }

    expect(observedDelayMs).toBe(30_000);
    expect(attempts).toBe(1);
    expect(resetCount).toBe(1);
  });

  it("applies the default timeout to connect attempts when config omits it", async () => {
    const client = new MemoryPalaceMcpClient({
      transport: "stdio",
      stdio: { command: TEST_STDIO_COMMAND, args: TEST_STDIO_ARGS },
    });
    let observedDelayMs: number | undefined;
    const originalSetTimeout = globalThis.setTimeout;
    const fakeSdkClient = {
      connect() {
        return new Promise<void>(() => undefined);
      },
    };

    globalThis.setTimeout = ((handler: TimerHandler, delay?: number, ...args: unknown[]) => {
      observedDelayMs = Number(delay ?? 0);
      return originalSetTimeout(handler, 0, ...args);
    }) as typeof setTimeout;

    try {
      await expect(
        (client as unknown as {
          connectWithTimeout: (
            sdkClient: { connect: () => Promise<void> },
            transport: unknown,
          ) => Promise<void>;
        }).connectWithTimeout(fakeSdkClient, {}),
      ).rejects.toThrow("connect timeout after 30000ms");
    } finally {
      globalThis.setTimeout = originalSetTimeout;
    }

    expect(observedDelayMs).toBe(30_000);
  });

  it("times out stuck read-only tool calls and retries them", async () => {
    const client = new MemoryPalaceMcpClient({
      transport: "stdio",
      timeoutMs: 5,
      requestRetries: 3,
      stdio: { command: TEST_STDIO_COMMAND, args: TEST_STDIO_ARGS },
    });
    let attempts = 0;
    let resetCount = 0;
    const fakeClient = {
      async callTool() {
        attempts += 1;
        return await new Promise(() => undefined);
      },
    };

    (client as unknown as { ensureHealthyBeforeCall: () => Promise<void> }).ensureHealthyBeforeCall =
      async () => undefined;
    (client as unknown as { ensureConnected: () => Promise<unknown> }).ensureConnected = async () => fakeClient;
    (client as unknown as { resetConnection: () => Promise<void> }).resetConnection = async () => {
      resetCount += 1;
    };

    await expect(client.searchMemory({ query: "hello" })).rejects.toThrow(
      "request timeout after 5ms during search_memory",
    );
    expect(attempts).toBe(3);
    expect(resetCount).toBe(3);
    expect(client.diagnostics.callRetryCount).toBe(2);
  });

  it("does not retry write tools when the request itself times out", async () => {
    const client = new MemoryPalaceMcpClient({
      transport: "stdio",
      timeoutMs: 5,
      requestRetries: 3,
      stdio: { command: TEST_STDIO_COMMAND, args: TEST_STDIO_ARGS },
    });
    let attempts = 0;
    let resetCount = 0;
    const fakeClient = {
      async callTool() {
        attempts += 1;
        return await new Promise(() => undefined);
      },
    };

    (client as unknown as { ensureHealthyBeforeCall: () => Promise<void> }).ensureHealthyBeforeCall =
      async () => undefined;
    (client as unknown as { ensureConnected: () => Promise<unknown> }).ensureConnected = async () => fakeClient;
    (client as unknown as { resetConnection: () => Promise<void> }).resetConnection = async () => {
      resetCount += 1;
    };

    await expect(client.createMemory({ content: "hello" })).rejects.toThrow(
      "request timeout after 5ms during create_memory",
    );
    expect(attempts).toBe(1);
    expect(resetCount).toBe(1);
    expect(client.diagnostics.callRetryCount).toBe(0);
  });

  it("swallows late operation rejections after a timeout", async () => {
    const client = new MemoryPalaceMcpClient({
      transport: "stdio",
      timeoutMs: 5,
      stdio: { command: TEST_STDIO_COMMAND, args: TEST_STDIO_ARGS },
    });
    let rejectOperation: ((reason?: unknown) => void) | undefined;
    const lateFailures: unknown[] = [];
    const onUnhandledRejection = (reason: unknown) => {
      lateFailures.push(reason);
    };
    const operation = new Promise<never>((_, reject) => {
      rejectOperation = reject;
    });

    process.on("unhandledRejection", onUnhandledRejection);
    try {
      await expect(
        (client as unknown as {
          withOperationTimeout: <T>(operation: Promise<T>, timeoutMessage: string) => Promise<T>;
        }).withOperationTimeout(operation, "request timeout after 5ms"),
      ).rejects.toThrow("request timeout after 5ms");

      rejectOperation?.(new Error("late failure"));
      await new Promise((resolve) => setTimeout(resolve, 20));

      expect(lateFailures).toEqual([]);
    } finally {
      process.off("unhandledRejection", onUnhandledRejection);
    }
  });

  it("runs timeout cleanup hooks before surfacing the timeout error", async () => {
    const client = new MemoryPalaceMcpClient({
      transport: "stdio",
      timeoutMs: 5,
      stdio: { command: TEST_STDIO_COMMAND, args: TEST_STDIO_ARGS },
    });
    let cleanupCalls = 0;

    await expect(
      (client as unknown as {
        withOperationTimeout: <T>(
          operation: Promise<T>,
          timeoutMessage: string,
          onTimeout?: () => Promise<void> | void,
        ) => Promise<T>;
      }).withOperationTimeout(
        new Promise(() => undefined),
        "request timeout after 5ms during test",
        async () => {
          cleanupCalls += 1;
        },
      ),
    ).rejects.toThrow("request timeout after 5ms during test");

    expect(cleanupCalls).toBe(1);
  });

  it("surfaces timeout errors without waiting for slow cleanup hooks", async () => {
    const client = new MemoryPalaceMcpClient({
      transport: "stdio",
      timeoutMs: 5,
      stdio: { command: TEST_STDIO_COMMAND, args: TEST_STDIO_ARGS },
    });
    let resolveCleanup: (() => void) | undefined;
    let cleanupStarted = 0;
    let cleanupFinished = false;
    const timeoutAttempt = (
      (client as unknown as {
        withOperationTimeout: <T>(
          operation: Promise<T>,
          timeoutMessage: string,
          onTimeout?: () => Promise<void> | void,
        ) => Promise<T>;
      }).withOperationTimeout(
        new Promise(() => undefined),
        "request timeout after 5ms during slow cleanup",
        async () => {
          cleanupStarted += 1;
          await new Promise<void>((resolve) => {
            resolveCleanup = () => {
              cleanupFinished = true;
              resolve();
            };
          });
        },
      )
    );

    const surfaced = await Promise.race([
      timeoutAttempt.then(
        () => "resolved",
        (error: Error) => error.message,
      ),
      new Promise<string>((resolve) => setTimeout(() => resolve("pending"), 50)),
    ]);

    expect(surfaced).toBe("request timeout after 5ms during slow cleanup");
    expect(cleanupStarted).toBe(1);
    expect(cleanupFinished).toBe(false);

    resolveCleanup?.();
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(cleanupFinished).toBe(true);

    await expect(timeoutAttempt).rejects.toThrow("request timeout after 5ms during slow cleanup");
  });

  it("surfaces connect timeout errors without waiting for slow pending-connection cleanup", async () => {
    const client = new MemoryPalaceMcpClient({
      transport: "stdio",
      timeoutMs: 5,
      stdio: { command: TEST_STDIO_COMMAND, args: TEST_STDIO_ARGS },
    });
    let resolveCleanup: (() => void) | undefined;
    let cleanupStarted = 0;
    let cleanupFinished = false;
    const fakeSdkClient = {
      connect() {
        return new Promise<void>(() => undefined);
      },
    };

    (client as unknown as {
      disposePendingConnection: (_client: unknown, _transport: unknown) => Promise<void>;
    }).disposePendingConnection = async () => {
      cleanupStarted += 1;
      await new Promise<void>((resolve) => {
        resolveCleanup = () => {
          cleanupFinished = true;
          resolve();
        };
      });
    };

    const connectAttempt = (client as unknown as {
      connectWithTimeout: (
        sdkClient: { connect: () => Promise<void> },
        transport: unknown,
      ) => Promise<void>;
    }).connectWithTimeout(fakeSdkClient, {});

    const surfaced = await Promise.race([
      connectAttempt.then(
        () => "resolved",
        (error: Error) => error.message,
      ),
      new Promise<string>((resolve) => setTimeout(() => resolve("pending"), 50)),
    ]);

    expect(surfaced).toBe("connect timeout after 5ms");
    expect(cleanupStarted).toBe(1);
    expect(cleanupFinished).toBe(false);

    resolveCleanup?.();
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(cleanupFinished).toBe(true);

    await expect(connectAttempt).rejects.toThrow("connect timeout after 5ms");
  });

  it("invalidates cached health check failures after close", async () => {
    const client = new MemoryPalaceMcpClient({
      transport: "stdio",
      stdio: { command: TEST_STDIO_COMMAND, args: TEST_STDIO_ARGS },
      healthcheckTtlMs: 5000,
    });
    let mode: "fail" | "pass" = "fail";

    (client as unknown as { ensureConnected: () => Promise<unknown> }).ensureConnected = async () => ({});
    (client as unknown as { invokeTool: () => Promise<unknown> }).invokeTool = async () => {
      if (mode === "fail") {
        throw new Error("socket hang up");
      }
      return { ok: true };
    };

    const first = await client.healthCheck(false);
    expect(first.ok).toBe(false);

    mode = "pass";
    await client.close();

    const second = await client.healthCheck(false);
    expect(second.ok).toBe(true);
  });

  it("allows a new forced health check after close clears an in-flight promise", async () => {
    const client = new MemoryPalaceMcpClient({
      transport: "stdio",
      stdio: { command: TEST_STDIO_COMMAND, args: TEST_STDIO_ARGS },
      healthcheckTtlMs: 5000,
    });
    let callCount = 0;
    let releaseFirst: (() => void) | undefined;

    (client as unknown as { ensureConnected: () => Promise<unknown> }).ensureConnected = async () => ({});
    (client as unknown as { invokeTool: () => Promise<unknown> }).invokeTool = async () => {
      callCount += 1;
      if (callCount === 1) {
        return await new Promise((resolve) => {
          releaseFirst = () => resolve({ ok: true });
        });
      }
      return { ok: true };
    };

    const first = client.healthCheck(true);
    await Promise.resolve();
    await client.close();

    const second = await client.healthCheck(true);
    expect(second.ok).toBe(true);

    releaseFirst?.();
    const firstResult = await first;
    expect(firstResult.ok).toBe(false);
    expect(callCount).toBe(2);
  });

  it("treats only plain objects as records", () => {
    expect(isRecord({ ok: true })).toBe(true);
    expect(isRecord(Object.create(null))).toBe(true);
    expect(isRecord(new Date())).toBe(false);
    expect(isRecord(new Map())).toBe(false);
    expect(isRecord(/abc/u)).toBe(false);
    expect(isRecord([])).toBe(false);
  });

  it("detects Han, Kana, Hangul, extended ranges, and punctuation", () => {
    expect(containsCjk("plain ascii")).toBe(false);
    expect(containsCjk("中文")).toBe(true);
    expect(containsCjk("かな")).toBe(true);
    expect(containsCjk("カナ")).toBe(true);
    expect(containsCjk("한글")).toBe(true);
    expect(containsCjk("𠀀")).toBe(true);
    expect(containsCjk("、")).toBe(true);
  });
});

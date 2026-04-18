import { describe, expect, it } from "bun:test";
import type { OpenClawPluginApi } from "openclaw/plugin-sdk/core";
import { parsePluginConfig } from "./config.ts";
import {
  registerLifecycleHooks,
  type RegisterLifecycleHookDeps,
} from "./lifecycle-hooks.ts";
import type {
  PluginConfig,
  SharedClientSession,
} from "./types.ts";

type HookHandler = (
  event: Record<string, unknown>,
  ctx?: Record<string, unknown>,
) => Promise<unknown> | unknown;

const flushTimers = async (delayMs = 40) => {
  await new Promise((resolve) => setTimeout(resolve, delayMs));
};

const chainHook = (
  existing: HookHandler | undefined,
  next: HookHandler,
): HookHandler => {
  if (!existing) {
    return next;
  }
  return async (event, ctx) => {
    const existingResult = await existing(event, ctx);
    const nextResult = await next(event, ctx);
    return nextResult ?? existingResult;
  };
};

const createHarness = (options?: {
  config?: Partial<PluginConfig>;
  isCommandNewStartupEvent?: boolean;
  runAutoRecallHook?: RegisterLifecycleHookDeps["runAutoRecallHook"];
  runReflectionFromCommandNew?: RegisterLifecycleHookDeps["runReflectionFromCommandNew"];
  withTypedHooks?: boolean;
  withRegisterHook?: boolean;
}) => {
  const typedHooks = new Map<string, HookHandler>();
  const internalHooks = new Map<string, HookHandler>();
  const recallCalls: string[] = [];
  const reflectionCalls: string[] = [];
  const api = {
    logger: {
      warn() {},
      error() {},
      info() {},
      debug() {},
    },
    resolvePath(input: string) {
      return input;
    },
  } as unknown as OpenClawPluginApi;
  if (options?.withTypedHooks !== false) {
    (api as OpenClawPluginApi & { on: (hookName: string, handler: HookHandler) => void }).on = (
      hookName: string,
      handler: HookHandler,
    ) => {
      typedHooks.set(hookName, chainHook(typedHooks.get(hookName), handler));
    };
  }
  if (options?.withRegisterHook !== false) {
    (api as OpenClawPluginApi & {
      registerHook: (events: string | string[], handler: HookHandler) => void;
    }).registerHook = (
      events: string | string[],
      handler: HookHandler,
    ) => {
      for (const eventName of Array.isArray(events) ? events : [events]) {
        internalHooks.set(eventName, chainHook(internalHooks.get(eventName), handler));
      }
    };
  }

  const config = parsePluginConfig({
    reflection: {
      enabled: true,
      source: "command_new",
    },
    ...(options?.config ?? {}),
  }, api, {
    hostPlatform: "posix",
    transportDiagnosticsPathEnv: "OPENCLAW_TEST_TRANSPORT_DIAGNOSTICS_PATH",
    defaultTransportDiagnosticsPath: "/tmp/transport-diagnostics.json",
    defaultVisualMemoryDisclosure: "test disclosure",
    defaultVisualMemoryRetentionNote: "test retention",
    resolveDefaultStdioLaunch: () => ({
      command: process.execPath,
      args: [],
      cwd: process.cwd(),
    }),
  });

  const deps: RegisterLifecycleHookDeps = {
    extractMessageTexts(messages) {
      return messages.flatMap((entry) => {
        if (!entry || typeof entry !== "object") {
          return [];
        }
        const content = (entry as { content?: unknown }).content;
        if (!Array.isArray(content)) {
          return [];
        }
        return content.flatMap((block) => {
          if (
            block &&
            typeof block === "object" &&
            "text" in block &&
            typeof (block as { text?: unknown }).text === "string"
          ) {
            return [(block as { text: string }).text];
          }
          return [];
        });
      });
    },
    harvestVisualContextFromEvent() {},
    isCommandNewStartupEvent() {
      return options?.isCommandNewStartupEvent ?? false;
    },
    normalizeHookContext(ctx) {
      return ctx ?? {};
    },
    normalizeText(value) {
      const trimmed = value?.trim();
      return trimmed ? trimmed : undefined;
    },
    readString(value) {
      return typeof value === "string" && value.trim() ? value : undefined;
    },
    async runAutoCaptureHook() {},
    async runAutoRecallHook(_api, _config, _session, event, ctx) {
      if (options?.runAutoRecallHook) {
        return options.runAutoRecallHook(_api, _config, _session, event, ctx);
      }
      const sessionRef =
        (ctx.sessionId as string | undefined) ??
        (ctx.sessionKey as string | undefined) ??
        (event.sessionId as string | undefined) ??
        (event.sessionKey as string | undefined) ??
        "unknown-session";
      recallCalls.push(sessionRef);
      return { sessionRef };
    },
    async runReflectionFromAgentEnd() {},
    async runReflectionFromCommandNew(_api, _config, _session, event, ctx) {
      if (options?.runReflectionFromCommandNew) {
        return options.runReflectionFromCommandNew(_api, _config, _session, event, ctx);
      }
      const sessionRef =
        (ctx.sessionId as string | undefined) ??
        (ctx.sessionKey as string | undefined) ??
        (event.sessionId as string | undefined) ??
        (event.sessionKey as string | undefined) ??
        "unknown-session";
      reflectionCalls.push(sessionRef);
    },
    async runReflectionFromCompactContext() {},
  };

  const session = {
    client: {} as SharedClientSession["client"],
    async withClient<T>(run: (client: SharedClientSession["client"]) => Promise<T>) {
      return run({} as SharedClientSession["client"]);
    },
    async close() {},
  } satisfies SharedClientSession;

  registerLifecycleHooks(api, { config, deps, session });

  return {
    typedHooks,
    internalHooks,
    recallCalls,
    reflectionCalls,
  };
};

describe("registerLifecycleHooks", () => {
  it("registers lifecycle fallbacks through registerHook when typed hooks are unavailable", async () => {
    const harness = createHarness({ withTypedHooks: false });

    expect(harness.typedHooks.size).toBe(0);
    expect(harness.internalHooks.has("before_prompt_build")).toBe(true);
    expect(harness.internalHooks.has("before_agent_start")).toBe(true);
    expect(harness.internalHooks.has("before_reset")).toBe(true);
    expect(harness.internalHooks.has("agent_end")).toBe(true);

    const beforePromptBuild = harness.internalHooks.get("before_prompt_build");
    await beforePromptBuild?.(
      { sessionKey: "register-hook-only" },
      { sessionKey: "register-hook-only" },
    );

    expect(harness.recallCalls).toEqual(["register-hook-only"]);
  });

  it("evicts oldest recent command:new reflection entries once the cache cap is exceeded", async () => {
    const originalNow = Date.now;
    let now = 10_000;
    Date.now = () => now;

    try {
      const harness = createHarness();
      const commandNewHook = harness.internalHooks.get("command:new");
      expect(commandNewHook).toBeDefined();

      for (let index = 0; index < 140; index += 1) {
        await commandNewHook?.(
          { sessionKey: `session-${index}` },
          { sessionKey: `session-${index}` },
        );
      }

      expect(harness.reflectionCalls).toHaveLength(140);

      now += 1_000;
      await commandNewHook?.(
        { sessionKey: "session-0" },
        { sessionKey: "session-0" },
      );

      expect(harness.reflectionCalls).toHaveLength(141);
      expect(harness.reflectionCalls.at(-1)).toBe("session-0");
    } finally {
      Date.now = originalNow;
    }
  });

  it("expires stale recent command:new reflection entries after the cleanup TTL", async () => {
    const originalNow = Date.now;
    let now = 10_000;
    Date.now = () => now;

    try {
      const harness = createHarness();
      const commandNewHook = harness.internalHooks.get("command:new");
      expect(commandNewHook).toBeDefined();

      await commandNewHook?.(
        { sessionKey: "session-stale" },
        { sessionKey: "session-stale" },
      );
      expect(harness.reflectionCalls).toEqual(["session-stale"]);

      now += 61_000;
      await commandNewHook?.(
        { sessionKey: "session-stale" },
        { sessionKey: "session-stale" },
      );

      expect(harness.reflectionCalls).toEqual([
        "session-stale",
        "session-stale",
      ]);
    } finally {
      Date.now = originalNow;
    }
  });

  it("keeps command:new reflection dedupe session-scoped when agentId and prompt match", async () => {
    const originalNow = Date.now;
    const now = 10_000;
    Date.now = () => now;

    try {
      const harness = createHarness();
      const commandNewHook = harness.internalHooks.get("command:new");
      expect(commandNewHook).toBeDefined();

      const sharedEvent = {
        agentId: "shared-agent",
        prompt: "shared prompt",
      };
      const sharedCtx = {
        agentId: "shared-agent",
      };

      await commandNewHook?.(
        { ...sharedEvent, sessionKey: "session-a" },
        sharedCtx,
      );
      await commandNewHook?.(
        { ...sharedEvent, sessionKey: "session-b" },
        sharedCtx,
      );
      await commandNewHook?.(
        { ...sharedEvent, sessionKey: "session-a" },
        sharedCtx,
      );

      expect(harness.reflectionCalls).toEqual([
        "session-a",
        "session-b",
      ]);
    } finally {
      Date.now = originalNow;
    }
  });

  it("consumes prompt-build recall markers only for the matching session", async () => {
    const harness = createHarness();
    const beforePromptBuild = harness.typedHooks.get("before_prompt_build");
    const beforeAgentStart = harness.typedHooks.get("before_agent_start");
    expect(beforePromptBuild).toBeDefined();
    expect(beforeAgentStart).toBeDefined();

    await beforePromptBuild?.(
      { prompt: "session-a prompt" },
      { sessionId: "session-a" },
    );
    const skipped = await beforeAgentStart?.(
      { prompt: "session-a prompt" },
      { sessionId: "session-a" },
    );
    const executed = await beforeAgentStart?.(
      { prompt: "session-b prompt" },
      { sessionId: "session-b" },
    );

    expect(skipped).toBeUndefined();
    expect(executed).toEqual({ sessionRef: "session-b" });
    expect(harness.recallCalls).toEqual(["session-a", "session-b"]);
  });

  it("keeps prompt-build recall markers session-scoped when agentId and prompt match", async () => {
    const harness = createHarness();
    const beforePromptBuild = harness.typedHooks.get("before_prompt_build");
    const beforeAgentStart = harness.typedHooks.get("before_agent_start");
    expect(beforePromptBuild).toBeDefined();
    expect(beforeAgentStart).toBeDefined();

    const sharedEvent = {
      agentId: "shared-agent",
      prompt: "shared prompt",
    };
    const sharedCtx = {
      agentId: "shared-agent",
    };

    await beforePromptBuild?.(
      sharedEvent,
      { ...sharedCtx, sessionId: "session-a" },
    );
    const otherSessionExecuted = await beforeAgentStart?.(
      sharedEvent,
      { ...sharedCtx, sessionId: "session-b" },
    );
    const originalSessionSkipped = await beforeAgentStart?.(
      sharedEvent,
      { ...sharedCtx, sessionId: "session-a" },
    );

    expect(otherSessionExecuted).toEqual({ sessionRef: "session-b" });
    expect(originalSessionSkipped).toBeUndefined();
    expect(harness.recallCalls).toEqual(["session-a", "session-b"]);
  });

  it("clears prompt-build recall markers after completion when before_agent_start never fires", async () => {
    const harness = createHarness();
    const beforePromptBuild = harness.typedHooks.get("before_prompt_build");
    const beforeAgentStart = harness.typedHooks.get("before_agent_start");
    expect(beforePromptBuild).toBeDefined();
    expect(beforeAgentStart).toBeDefined();

    await beforePromptBuild?.(
      { prompt: "session-repeat prompt" },
      { sessionId: "session-repeat" },
    );
    await flushTimers();
    const fallbackResult = await beforeAgentStart?.(
      { prompt: "session-repeat prompt" },
      { sessionId: "session-repeat" },
    );

    expect(fallbackResult).toEqual({ sessionRef: "session-repeat" });
    expect(harness.recallCalls).toEqual([
      "session-repeat",
      "session-repeat",
    ]);
  });

  it("does not let a different sessionFile consume the prompt-build recall marker when session ids are missing", async () => {
    const recallCalls: string[] = [];
    const harness = createHarness({
      async runAutoRecallHook() {
        recallCalls.push("called");
        return { ok: true };
      },
    });
    const beforePromptBuild = harness.typedHooks.get("before_prompt_build");
    const beforeAgentStart = harness.typedHooks.get("before_agent_start");
    expect(beforePromptBuild).toBeDefined();
    expect(beforeAgentStart).toBeDefined();

    await beforePromptBuild?.(
      { prompt: "same prompt", agentId: "agent-alpha", sessionFile: "/tmp/session-a.jsonl" },
      { agentId: "agent-alpha", sessionFile: "/tmp/session-a.jsonl" },
    );
    const fallbackResult = await beforeAgentStart?.(
      { prompt: "same prompt", agentId: "agent-alpha", sessionFile: "/tmp/session-b.jsonl" },
      { agentId: "agent-alpha", sessionFile: "/tmp/session-b.jsonl" },
    );

    expect(fallbackResult).toEqual({ ok: true });
    expect(recallCalls).toHaveLength(2);
  });

  it("clears prompt-build recall markers after exceptions so later fallback is not poisoned", async () => {
    const error = new Error("recall failed");
    const recallCalls: string[] = [];
    const harness = createHarness({
      async runAutoRecallHook(_api, _config, _session, event, ctx) {
        const sessionRef =
          (ctx.sessionId as string | undefined) ??
          (event.sessionId as string | undefined) ??
          "unknown-session";
        recallCalls.push(sessionRef);
        if (sessionRef === "session-error") {
          throw error;
        }
        return { sessionRef };
      },
    });
    const beforePromptBuild = harness.typedHooks.get("before_prompt_build");
    const beforeAgentStart = harness.typedHooks.get("before_agent_start");
    expect(beforePromptBuild).toBeDefined();
    expect(beforeAgentStart).toBeDefined();

    await expect(
      beforePromptBuild?.(
        { prompt: "session-error prompt" },
        { sessionId: "session-error" },
      ),
    ).rejects.toBe(error);

    await flushTimers();

    const fallbackResult = await beforeAgentStart?.(
      { prompt: "session-after-error prompt" },
      { sessionId: "session-after-error" },
    );

    expect(fallbackResult).toEqual({ sessionRef: "session-after-error" });
    expect(recallCalls).toEqual([
      "session-error",
      "session-after-error",
    ]);
  });
});

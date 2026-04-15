#!/usr/bin/env bun

import plugin from "../extensions/memory-palace/index.ts";
import { MemoryPalaceMcpClient } from "../extensions/memory-palace/src/client.ts";

type HookName = "message:preprocessed" | "before_prompt_build" | "agent_end";
type ToolFactory = (
  ctx: Record<string, unknown>,
) => Array<{
  name: string;
  execute: (toolCallId: string, params: unknown) => Promise<{ details?: Record<string, unknown> }>;
}>;

function readArg(name: string): string | undefined {
  const index = process.argv.indexOf(name);
  if (index >= 0 && index + 1 < process.argv.length) {
    return process.argv[index + 1];
  }
  return undefined;
}

function parseHookName(): HookName {
  const hook = readArg("--hook");
  if (
    hook === "message:preprocessed" ||
    hook === "before_prompt_build" ||
    hook === "agent_end"
  ) {
    return hook;
  }
  throw new Error("--hook must be one of: message:preprocessed, before_prompt_build, agent_end");
}

function buildProbeEvent(hook: HookName): Record<string, unknown> {
  if (hook === "message:preprocessed") {
    return {
      message: {
        MediaPath: "file:/tmp/runtime-probe.png",
        bodyForAgent: [
          "Summary: Runtime visual summary",
          "OCR: stage freeze checklist",
          "Scene: runtime probe wall",
        ].join("\n"),
      },
    };
  }
  if (hook === "before_prompt_build") {
    return {
      messages: [
        {
          role: "user",
          content: [
            {
              type: "image_url",
              imageUrl: "file:/tmp/runtime-probe.png",
              description: "Prompt-build rollout board",
            },
          ],
        },
      ],
    };
  }
  return {
    success: true,
    messages: [
      {
        role: "user",
        content: [
          {
            type: "image_url",
            imageUrl: "file:/tmp/runtime-probe.png",
          },
        ],
      },
      {
        role: "assistant",
        content: [
          {
            type: "text",
            text: "Agent-end rollout board summary",
          },
        ],
      },
    ],
  };
}

async function main(): Promise<void> {
  const hook = parseHookName();
  const sessionId = `runtime-probe-${hook.replace(/[:]/g, "-")}`;
  const ctx = {
    sessionId,
    agentId: "agent-runtime-probe",
  };

  const hooks = new Map<
    string,
    (event: Record<string, unknown>, context: Record<string, unknown>) => unknown
  >();
  const factories: ToolFactory[] = [];

  const originalCreate = MemoryPalaceMcpClient.prototype.createMemory;
  const originalRead = MemoryPalaceMcpClient.prototype.readMemory;
  const originalClose = MemoryPalaceMcpClient.prototype.close;

  MemoryPalaceMcpClient.prototype.createMemory = async function (
    args: Record<string, unknown>,
  ): Promise<unknown> {
    return {
      ok: true,
      created: true,
      uri: "core://visual/2026/03/10/sha256-runtime-probe",
      path: "memory-palace/core/visual/2026/03/10/sha256-runtime-probe.md",
      content: args.content,
    };
  };
  MemoryPalaceMcpClient.prototype.readMemory = async function (): Promise<unknown> {
    return { content: "namespace ready" };
  };
  MemoryPalaceMcpClient.prototype.close = async function (): Promise<void> {};

  try {
    plugin.register({
      pluginConfig: {},
      logger: { warn() {}, error() {}, info() {}, debug() {} },
      resolvePath(input: string) {
        return input;
      },
      registerTool(factory: ToolFactory) {
        factories.push(factory);
      },
      registerCli() {},
      on(
        hookName: string,
        handler: (event: Record<string, unknown>, context: Record<string, unknown>) => unknown,
      ) {
        hooks.set(hookName, handler);
      },
    } as never);

    const handler = hooks.get(hook);
    if (!handler) {
      throw new Error(`hook_not_registered:${hook}`);
    }
    await handler(buildProbeEvent(hook), ctx);

    const tools = factories[0]!(ctx);
    const storeVisualTool = tools.find((tool) => tool.name === "memory_store_visual");
    if (!storeVisualTool) {
      throw new Error("memory_store_visual_not_registered");
    }

    const result = await storeVisualTool.execute("runtime-visual-probe", {
      mediaRef: "file:/tmp/runtime-probe.png",
    });
    const details = result.details ?? result;
    const payload = {
      ok: Boolean(details.ok),
      hook,
      runtime_source: details.runtime_source ?? null,
      runtime_visual_probe: details.runtime_visual_probe ?? "none",
      summary: details.summary ?? null,
      error: details.error ?? null,
    };

    console.log(JSON.stringify(payload, null, 2));
    if (!payload.ok) {
      process.exitCode = 1;
    }
  } finally {
    MemoryPalaceMcpClient.prototype.createMemory = originalCreate;
    MemoryPalaceMcpClient.prototype.readMemory = originalRead;
    MemoryPalaceMcpClient.prototype.close = originalClose;
  }
}

void main();

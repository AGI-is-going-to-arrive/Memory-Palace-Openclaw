import { describe, expect, it } from "bun:test";
import { createOnboardingTools } from "./onboarding-tools.ts";

const layout = {
  pluginExtensionRoot: "/repo/extensions/memory-palace",
  isRepoExtensionLayout: true,
  packagedScriptsRoot: "/repo/extensions/memory-palace/release/scripts",
  packagedBackendRoot: "/repo/extensions/memory-palace/release/backend",
  isPackagedPluginLayout: false,
  pluginProjectRoot: "/repo",
  defaultStdioWrapper: "/repo/scripts/run_memory_palace_mcp_stdio.sh",
  defaultTransportDiagnosticsPath: "/repo/.tmp/observability/openclaw_transport_diagnostics.json",
  bundledSkillRoot: "/repo/extensions/memory-palace/skills",
} as const;

const baseDeps = {
  formatError(error: unknown) {
    return error instanceof Error ? error.message : String(error);
  },
  jsonResult<T>(value: T) {
    return value;
  },
  readBoolean(value: unknown) {
    return typeof value === "boolean" ? value : undefined;
  },
  readString(value: unknown) {
    return typeof value === "string" && value.trim() ? value.trim() : undefined;
  },
};

describe("onboarding tools", () => {
  it("returns onboarding status guidance and bootstrap narrative", async () => {
    const tools = createOnboardingTools({
      layout: layout as never,
      deps: {
        ...baseDeps,
        async runLauncherCommand(_layout, args) {
          expect(args).toEqual(["bootstrap-status", "--json"]);
          return {
            exitCode: 0,
            stdout: "",
            stderr: "",
            payload: {
              ok: true,
              setup: {
                requiresOnboarding: true,
                requestedProfile: "b",
                effectiveProfile: "b",
                providerProbe: {
                  fallbackApplied: false,
                },
              },
            },
          };
        },
      },
    });

    const tool = tools.find((item) => item.name === "memory_onboarding_status");
    const result = await tool!.execute("call-1", {});
    expect(result.ok).toBe(true);
    expect(result.narrative.requiresOnboarding).toBe(true);
    expect(result.narrative.summary).toContain("not bootstrapped");
    expect(result.guide.profiles.b.llmOptional).toContain("Profile B can still use optional");
  });

  it("surfaces detected maximum embedding dimension from provider probe", async () => {
    const tools = createOnboardingTools({
      layout: layout as never,
      deps: {
        ...baseDeps,
        async runLauncherCommand(_layout, args) {
          expect(args).toContain("provider-probe");
          return {
            exitCode: 0,
            stdout: "",
            stderr: "",
            payload: {
              requestedProfile: "c",
              effectiveProfile: "c",
              fallbackApplied: false,
              summaryMessage: "Advanced provider checks passed for the current profile.",
              providers: {
                embedding: {
                  status: "pass",
                  detectedDim: "1024",
                  detectedMaxDim: "1024",
                  recommendedDim: "1024",
                },
              },
            },
          };
        },
      },
    });

    const tool = tools.find((item) => item.name === "memory_onboarding_probe");
    const result = await tool!.execute("call-2", {
      profile: "c",
      mode: "full",
      transport: "stdio",
      embeddingApiBase: "https://embedding.example/v1",
    });
    expect(result.ok).toBe(true);
    expect(result.narrative.detectedMaxDim).toBe("1024");
    expect(result.narrative.summary).toContain("Detected maximum embedding dimension: 1024");
    expect(result.guide.providerFormats.llm.runtimeNote).toContain("/chat/completions");
  });

  it("supports chinese locale for onboarding guidance", async () => {
    const tools = createOnboardingTools({
      layout: layout as never,
      deps: {
        ...baseDeps,
        async runLauncherCommand(_layout, args) {
          expect(args).toEqual(["bootstrap-status", "--json"]);
          return {
            exitCode: 0,
            stdout: "",
            stderr: "",
            payload: {
              ok: true,
              setup: {
                requiresOnboarding: true,
                requestedProfile: "b",
                effectiveProfile: "b",
                providerProbe: {
                  fallbackApplied: false,
                },
              },
            },
          };
        },
      },
    });

    const tool = tools.find((item) => item.name === "memory_onboarding_status");
    const result = await tool!.execute("call-zh", { locale: "zh-CN" });
    expect(result.ok).toBe(true);
    expect(result.narrative.summary).toContain("还没有完成 Memory Palace bootstrap");
    expect(result.guide.recommendedPath.summary).toContain("只要真实 embedding");
  });

  it("passes setup flags through apply and reports fallback when setup degrades to b", async () => {
    const seenArgs: string[][] = [];
    const tools = createOnboardingTools({
      layout: layout as never,
      deps: {
        ...baseDeps,
        async runLauncherCommand(_layout, args) {
          seenArgs.push(args);
          return {
            exitCode: 0,
            stdout: "",
            stderr: "",
            payload: {
              ok: true,
              summary: "Setup completed for mode=full, requested profile=C, effective profile=B.",
              requested_profile: "c",
              effective_profile: "b",
              fallback_applied: true,
              setup: {
                providerProbe: {
                  providers: {
                    embedding: {
                      recommendedDim: "1024",
                    },
                  },
                },
              },
            },
          };
        },
      },
    });

    const tool = tools.find((item) => item.name === "memory_onboarding_apply");
    const result = await tool!.execute("call-3", {
      profile: "c",
      mode: "full",
      transport: "stdio",
      validate: true,
      strictProfile: true,
      reconfigure: true,
    });

    expect(seenArgs[0]).toEqual(["bootstrap-status", "--json"]);
    expect(seenArgs[1]).toEqual([
      "setup",
      "--json",
      "--mode",
      "full",
      "--profile",
      "c",
      "--transport",
      "stdio",
      "--validate",
      "--strict-profile",
      "--reconfigure",
    ]);
    expect(result.ok).toBe(true);
    expect(result.narrative.fallbackApplied).toBe(true);
    expect(result.narrative.summary).toContain("fell back to Profile B");
    expect(result.narrative.detectedMaxDim).toBe("1024");
  });

  it("passes mcp api key through env for sse provider probe", async () => {
    const seenCalls: Array<{ args: string[]; env?: Record<string, string> }> = [];
    const tools = createOnboardingTools({
      layout: layout as never,
      deps: {
        ...baseDeps,
        async runLauncherCommand(_layout, args, env) {
          seenCalls.push({ args, env });
          return {
            exitCode: 0,
            stdout: "",
            stderr: "",
            payload: {
              requestedProfile: "c",
              effectiveProfile: "c",
              fallbackApplied: false,
              summaryMessage: "Advanced provider checks passed for the current profile.",
              providers: {
                embedding: {
                  status: "pass",
                  detectedDim: "1024",
                  detectedMaxDim: "1024",
                  recommendedDim: "1024",
                },
              },
            },
          };
        },
      },
    });

    const tool = tools.find((item) => item.name === "memory_onboarding_probe");
    const result = await tool!.execute("call-probe-sse", {
      profile: "c",
      transport: "sse",
      sseUrl: "https://memory.example/sse",
      mcpApiKey: "mp-secret",
      allowInsecureLocal: true,
    });

    expect(result.ok).toBe(true);
    expect(seenCalls[0]?.args).toEqual(["bootstrap-status", "--json"]);
    expect(seenCalls[1]?.args).toEqual([
      "provider-probe",
      "--json",
      "--profile",
      "c",
      "--transport",
      "sse",
      "--sse-url",
      "https://memory.example/sse",
      "--allow-insecure-local",
    ]);
    expect(seenCalls[1]?.env?.MCP_API_KEY).toBe("mp-secret");
  });

  it("passes mcp api key through env for sse onboarding apply", async () => {
    const seenCalls: Array<{ args: string[]; env?: Record<string, string> }> = [];
    const tools = createOnboardingTools({
      layout: layout as never,
      deps: {
        ...baseDeps,
        async runLauncherCommand(_layout, args, env) {
          seenCalls.push({ args, env });
          return {
            exitCode: 0,
            stdout: "",
            stderr: "",
            payload: {
              ok: true,
              summary: "Setup completed for mode=basic, requested profile=C, effective profile=C.",
              requested_profile: "c",
              effective_profile: "c",
              fallback_applied: false,
              setup: {
                providerProbe: {
                  providers: {
                    embedding: {
                      recommendedDim: "1024",
                    },
                  },
                },
              },
            },
          };
        },
      },
    });

    const tool = tools.find((item) => item.name === "memory_onboarding_apply");
    const result = await tool!.execute("call-apply-sse", {
      profile: "c",
      transport: "sse",
      sseUrl: "https://memory.example/sse",
      mcpApiKey: "mp-secret",
    });

    expect(result.ok).toBe(true);
    expect(seenCalls[0]?.args).toEqual(["bootstrap-status", "--json"]);
    expect(seenCalls[1]?.args).toEqual([
      "setup",
      "--json",
      "--profile",
      "c",
      "--transport",
      "sse",
      "--sse-url",
      "https://memory.example/sse",
    ]);
    expect(seenCalls[1]?.env?.MCP_API_KEY).toBe("mp-secret");
  });

  it("inherits current setup mode and sse transport defaults for provider probe", async () => {
    const seenCalls: Array<{ args: string[]; env?: Record<string, string> }> = [];
    const tools = createOnboardingTools({
      layout: layout as never,
      deps: {
        ...baseDeps,
        async runLauncherCommand(_layout, args, env) {
          seenCalls.push({ args, env });
          if (args[0] === "bootstrap-status") {
            return {
              exitCode: 0,
              stdout: "",
              stderr: "",
              payload: {
                ok: true,
                setup: {
                  mode: "dev",
                  transport: "sse",
                  sseUrl: "https://memory.example/current-sse",
                },
              },
            };
          }
          return {
            exitCode: 0,
            stdout: "",
            stderr: "",
            payload: {
              requestedProfile: "c",
              effectiveProfile: "c",
              fallbackApplied: false,
              summaryMessage: "Advanced provider checks passed for the current profile.",
              providers: {},
            },
          };
        },
      },
    });

    const tool = tools.find((item) => item.name === "memory_onboarding_probe");
    const result = await tool!.execute("call-probe-defaults", {
      profile: "c",
    });

    expect(result.ok).toBe(true);
    expect(seenCalls[0]?.args).toEqual(["bootstrap-status", "--json"]);
    expect(seenCalls[1]?.args).toEqual([
      "provider-probe",
      "--json",
      "--mode",
      "dev",
      "--profile",
      "c",
      "--transport",
      "sse",
      "--sse-url",
      "https://memory.example/current-sse",
    ]);
  });

  it("inherits current setup transport defaults for onboarding apply unless explicitly overridden", async () => {
    const seenCalls: Array<{ args: string[]; env?: Record<string, string> }> = [];
    const tools = createOnboardingTools({
      layout: layout as never,
      deps: {
        ...baseDeps,
        async runLauncherCommand(_layout, args, env) {
          seenCalls.push({ args, env });
          if (args[0] === "bootstrap-status") {
            return {
              exitCode: 0,
              stdout: "",
              stderr: "",
              payload: {
                ok: true,
                setup: {
                  mode: "dev",
                  transport: "sse",
                  sseUrl: "https://memory.example/current-sse",
                },
              },
            };
          }
          return {
            exitCode: 0,
            stdout: "",
            stderr: "",
            payload: {
              ok: true,
              summary: "Setup completed for mode=dev, requested profile=C, effective profile=C.",
              requested_profile: "c",
              effective_profile: "c",
              fallback_applied: false,
              setup: {
                providerProbe: {
                  providers: {},
                },
              },
            },
          };
        },
      },
    });

    const tool = tools.find((item) => item.name === "memory_onboarding_apply");
    const inheritedResult = await tool!.execute("call-apply-defaults", {
      profile: "c",
    });
    expect(inheritedResult.ok).toBe(true);
    expect(seenCalls[0]?.args).toEqual(["bootstrap-status", "--json"]);
    expect(seenCalls[1]?.args).toEqual([
      "setup",
      "--json",
      "--mode",
      "dev",
      "--profile",
      "c",
      "--transport",
      "sse",
      "--sse-url",
      "https://memory.example/current-sse",
    ]);

    seenCalls.length = 0;
    const overriddenResult = await tool!.execute("call-apply-override", {
      profile: "c",
      transport: "stdio",
    });
    expect(overriddenResult.ok).toBe(true);
    expect(seenCalls[0]?.args).toEqual(["bootstrap-status", "--json"]);
    expect(seenCalls[1]?.args).toEqual([
      "setup",
      "--json",
      "--mode",
      "dev",
      "--profile",
      "c",
      "--transport",
      "stdio",
    ]);
  });

  it("treats non-zero setup exits as onboarding apply failures even when a payload exists", async () => {
    const tools = createOnboardingTools({
      layout: layout as never,
      deps: {
        ...baseDeps,
        async runLauncherCommand() {
          return {
            exitCode: 1,
            stdout: "",
            stderr: "",
            payload: {
              ok: true,
              summary: "Setup reported a partial payload but validation failed.",
              requested_profile: "c",
              effective_profile: "c",
              validation: {
                ok: false,
                failed_step: "doctor",
              },
            },
          };
        },
      },
    });

    const tool = tools.find((item) => item.name === "memory_onboarding_apply");
    const result = await tool!.execute("call-apply-failed", {
      profile: "c",
      transport: "stdio",
      validate: true,
    });

    expect(result.ok).toBe(false);
    expect(result.apply.validation.ok).toBe(false);
    expect(result.narrative.summary).toContain("validation failed");
  });

  it("renders install guidance with the current public package boundary", async () => {
    const tools = createOnboardingTools({
      layout: layout as never,
      deps: {
        ...baseDeps,
        async runLauncherCommand(_layout, args) {
          expect(args).toEqual(["bootstrap-status", "--json"]);
          return {
            exitCode: 0,
            stdout: "",
            stderr: "",
            payload: {
              ok: true,
              setup: {
                requiresOnboarding: true,
                requestedProfile: "b",
                effectiveProfile: "b",
                installGuidance: {
                  recommendedMethod: "source-checkout",
                  installCommands: {
                    "source-checkout":
                      "python3 scripts/openclaw_memory_palace.py setup --mode basic --profile b --transport stdio --json",
                  },
                  repoUrlDirectInstallSupported: false,
                  recommendedMethodNote:
                    "The public npm spec `@openclaw/memory-palace` returned `Package not found on npm`, and `openclaw plugins install memory-palace` resolved to a skill rather than a plugin.",
                },
                providerProbe: {
                  fallbackApplied: false,
                },
              },
            },
          };
        },
      },
    });

    const tool = tools.find((item) => item.name === "memory_onboarding_status");
    const result = await tool!.execute("call-install-guidance", {});

    expect(result.ok).toBe(true);
    expect(result.narrative.installGuidanceText).toContain(
      "Recommended install method: source-checkout.",
    );
    expect(result.narrative.installGuidanceText).toContain(
      "Package not found on npm",
    );
    expect(result.narrative.installGuidanceText).toContain(
      "does not support installing plugins directly from a repo URL",
    );
  });
});

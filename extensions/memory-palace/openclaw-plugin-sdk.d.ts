declare module "openclaw/plugin-sdk/core" {
  export type PluginLogger = {
    debug?: (message: string) => void;
    info?: (message: string) => void;
    warn: (message: string) => void;
    error: (message: string) => void;
  };

  export type CommandBuilder = {
    description(text: string): CommandBuilder;
    command(name: string): CommandBuilder;
    option(flags: string, description?: string, defaultValue?: unknown): CommandBuilder;
    requiredOption(flags: string, description?: string, defaultValue?: unknown): CommandBuilder;
    argument(spec: string, description?: string): CommandBuilder;
    action(handler: (...args: any[]) => unknown): CommandBuilder;
  };

  export type CliProgram = {
    command(name: string): CommandBuilder;
  };

  export type CliLogger = {
    error(message: string): void;
  };

  export type AnyAgentTool = {
    label: string;
    name: string;
    description: string;
    parameters: unknown;
    execute: (toolCallId: string, params: unknown) => Promise<unknown>;
  };

  export type OpenClawPluginToolContext = {
    config?: unknown;
    workspaceDir?: string;
    agentDir?: string;
    agentId?: string;
    sessionKey?: string;
    sessionId?: string;
    messageChannel?: string;
    agentAccountId?: string;
    requesterSenderId?: string;
    senderIsOwner?: boolean;
    sandboxed?: boolean;
  };

  export type MemoryEmbeddingProbeResult = {
    ok: boolean;
    error?: string;
  };

  export type MemoryCitationsMode = string;

  export type MemoryProviderStatus = {
    backend: "builtin" | "qmd";
    provider: string;
    model?: string;
    requestedProvider?: string;
    files?: number;
    chunks?: number;
    fts?: {
      enabled: boolean;
      available: boolean;
      error?: string;
    };
    vector?: {
      enabled: boolean;
      available?: boolean;
      dims?: number;
    };
    custom?: Record<string, unknown>;
  };

  export type MemoryPromptSectionBuilder = (params: {
    availableTools: Set<string>;
    citationsMode?: MemoryCitationsMode;
  }) => string[];

  export type MemoryFlushPlan = {
    softThresholdTokens: number;
    forceFlushTranscriptBytes: number;
    reserveTokensFloor: number;
    prompt: string;
    systemPrompt: string;
    relativePath: string;
  };

  export type MemoryFlushPlanResolver = (params: {
    cfg?: unknown;
    nowMs?: number;
  }) => MemoryFlushPlan | null;

  export type RegisteredMemorySearchManager = {
    status(): MemoryProviderStatus;
    probeEmbeddingAvailability(): Promise<MemoryEmbeddingProbeResult>;
    probeVectorAvailability(): Promise<boolean>;
    sync?(params?: {
      reason?: string;
      force?: boolean;
      sessionFiles?: string[];
      progress?: (update: { completed: number; total: number; label?: string }) => void;
    }): Promise<void>;
    close?(): Promise<void>;
  };

  export type MemoryPluginRuntime = {
    getMemorySearchManager(params: {
      cfg: unknown;
      agentId: string;
      purpose?: "default" | "status";
    }): Promise<{
      manager: RegisteredMemorySearchManager | null;
      error?: string;
    }>;
    resolveMemoryBackendConfig(params: {
      cfg: unknown;
      agentId: string;
    }): {
      backend: "builtin";
    } | {
      backend: "qmd";
      qmd?: {
        command?: string;
      };
    };
    closeAllMemorySearchManagers?(): Promise<void>;
  };

  export type MemoryPluginCapability = {
    promptBuilder?: MemoryPromptSectionBuilder;
    flushPlanResolver?: MemoryFlushPlanResolver;
    runtime?: MemoryPluginRuntime;
    publicArtifacts?: {
      listArtifacts?: (params: { cfg: unknown }) => Promise<unknown[]>;
    };
  };

  export type OpenClawPluginApi = {
    pluginConfig: unknown;
    logger: PluginLogger;
    resolvePath(input: string): string;
    registerHook?(
      events: string | string[],
      handler: (event: Record<string, unknown>, context: Record<string, unknown>) => unknown,
      options?: {
        priority?: number;
        name?: string;
        description?: string;
        register?: boolean;
      },
    ): void;
    registerTool(
      factory:
        | AnyAgentTool
        | ((context: OpenClawPluginToolContext) => AnyAgentTool[] | AnyAgentTool | null | undefined),
      options: { names?: string[]; name?: string; optional?: boolean },
    ): void;
    registerCli(
      register: (context: { program: CliProgram; logger: CliLogger }) => void,
      options?: { commands?: unknown[] },
    ): void;
    registerMemoryCapability?(capability: MemoryPluginCapability): void;
    registerMemoryPromptSection?(builder: MemoryPromptSectionBuilder): void;
    registerMemoryFlushPlan?(resolver: MemoryFlushPlanResolver): void;
    registerMemoryRuntime?(runtime: MemoryPluginRuntime): void;
    on(
      hookName: string,
      handler: (event: Record<string, unknown>, context: Record<string, unknown>) => unknown,
      options?: { priority?: number },
    ): void;
  };
}

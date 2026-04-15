import type {
  CliLogger,
  CliProgram,
  CommandBuilder,
  OpenClawPluginApi,
} from "openclaw/plugin-sdk/core";
import { registerDiagnosticCommands } from "./cli-diagnostics.js";
import { registerIndexCommands } from "./cli-index.js";
import { registerMemoryIoCommands } from "./cli-memory-io.js";
import { registerSearchCommand } from "./cli-search.js";
import { registerStoreVisualCommand } from "./cli-store-visual.js";
import type {
  DiagnosticReport,
  PluginConfig,
  ResolvedAclPolicy,
  SharedClientSession,
  SmartExtractionCategory,
  VisualDuplicatePolicy,
} from "./types.js";

export type RegisterMemoryCliDeps = {
  buildExportPayload: (
    uri: string,
    text: string,
    mapping: PluginConfig["mapping"],
  ) => Record<string, unknown>;
  createMemoryTools: (
    config: PluginConfig,
    session: SharedClientSession,
  ) => Array<{
    name: string;
    execute: (toolCallId: string, params: unknown) => Promise<unknown>;
  }>;
  displaySmartExtractionCategory: (value: SmartExtractionCategory) => string;
  extractPayloadFailureMessage: (value: unknown) => string;
  extractReadText: (raw: unknown) => {
    text: string;
    selection?: unknown;
    degraded?: boolean;
    error?: string;
  };
  formatError: (error: unknown) => string;
  getTransportFallbackOrder: (config: PluginConfig) => string[];
  isTransientSqliteLockError: (value: unknown) => boolean;
  normalizeImportRecords: (raw: unknown) => Record<string, unknown>[];
  normalizeIndexStatusPayload: (value: unknown) => unknown;
  payloadIndicatesFailure: (value: unknown) => boolean;
  persistTransportDiagnosticsSnapshot: (
    config: PluginConfig,
    client: SharedClientSession["client"],
  ) => void;
  printCliValue: (value: unknown, json: boolean) => void;
  probeProfileMemoryState: (
    client: SharedClientSession["client"],
    config: PluginConfig,
  ) => Promise<{ blockCount: number; paths: string[] } | null>;
  readVisualDuplicatePolicy: (value: unknown) => VisualDuplicatePolicy | undefined;
  resolveAdminPolicy: (config: PluginConfig) => ResolvedAclPolicy;
  resolvePathLikeToUri: (pathOrUri: string, mapping: PluginConfig["mapping"]) => string;
  runDoctorReport: (
    config: PluginConfig,
    session: SharedClientSession,
    query: string,
  ) => Promise<DiagnosticReport>;
  runScopedSearch: (
    client: SharedClientSession["client"],
    query: string,
    config: PluginConfig,
    policy: ResolvedAclPolicy,
    options?: Record<string, unknown>,
  ) => Promise<unknown>;
  runSmokeReport: (
    config: PluginConfig,
    session: SharedClientSession,
    options: {
      query: string;
      pathOrUri?: string;
      expectHit: boolean;
    },
  ) => Promise<DiagnosticReport>;
  runVerifyReport: (
    config: PluginConfig,
    session: SharedClientSession,
  ) => Promise<DiagnosticReport>;
  sliceTextByLines: (text: string, from?: number, lines?: number) => string;
  snapshotPluginRuntimeState: (config: PluginConfig) => unknown;
  unwrapResultRecord: (value: unknown) => Record<string, unknown>;
  uriToVirtualPath: (uri: string, mapping: PluginConfig["mapping"]) => string;
  withTransientSqliteLockRetry: <T>(
    operation: () => Promise<T>,
    shouldRetry?: (value: T) => boolean,
    maxAttempts?: number,
    initialDelayMs?: number,
  ) => Promise<T>;
};

export function registerMemoryCli(
  api: OpenClawPluginApi,
  options: {
    config: PluginConfig;
    deps: RegisterMemoryCliDeps;
    rootCommand?: string;
    session: SharedClientSession;
  },
): void {
  const { config, deps, rootCommand = "memory", session } = options;

  api.registerCli(
    ({ program, logger }: { program: CliProgram; logger: CliLogger }) => {
      const memory: CommandBuilder = program
        .command(rootCommand)
        .description(
          rootCommand === "memory-palace"
            ? "Memory Palace stable command surface"
            : "Compatibility alias for memory-palace commands",
        );
      const withCliSession = async <T>(task: () => Promise<T>): Promise<T> => {
        try {
          return await task();
        } finally {
          await session.close();
        }
      };

      registerDiagnosticCommands(memory, {
        config,
        deps: {
          displaySmartExtractionCategory: deps.displaySmartExtractionCategory,
          extractReadText: deps.extractReadText,
          formatError: deps.formatError,
          getTransportFallbackOrder: deps.getTransportFallbackOrder,
          normalizeIndexStatusPayload: deps.normalizeIndexStatusPayload,
          payloadIndicatesFailure: deps.payloadIndicatesFailure,
          persistTransportDiagnosticsSnapshot:
            deps.persistTransportDiagnosticsSnapshot,
          printCliValue: deps.printCliValue,
          probeProfileMemoryState: deps.probeProfileMemoryState,
          runDoctorReport: deps.runDoctorReport,
          runSmokeReport: deps.runSmokeReport,
          runVerifyReport: deps.runVerifyReport,
          snapshotPluginRuntimeState: deps.snapshotPluginRuntimeState,
          withTransientSqliteLockRetry: deps.withTransientSqliteLockRetry,
        },
        logger,
        session,
        withCliSession,
      });

      registerSearchCommand(memory, {
        config,
        deps: {
          payloadIndicatesFailure: deps.payloadIndicatesFailure,
          printCliValue: deps.printCliValue,
          resolveAdminPolicy: deps.resolveAdminPolicy,
          runScopedSearch: deps.runScopedSearch,
        },
        logger,
        session,
        withCliSession,
      });

      registerMemoryIoCommands(memory, {
        config,
        deps: {
          buildExportPayload: deps.buildExportPayload,
          extractReadText: deps.extractReadText,
          normalizeImportRecords: deps.normalizeImportRecords,
          payloadIndicatesFailure: deps.payloadIndicatesFailure,
          printCliValue: deps.printCliValue,
          resolvePathLikeToUri: deps.resolvePathLikeToUri,
          sliceTextByLines: deps.sliceTextByLines,
          unwrapResultRecord: deps.unwrapResultRecord,
          uriToVirtualPath: deps.uriToVirtualPath,
        },
        session,
        withCliSession,
      });

      registerIndexCommands(memory, {
        deps: {
          extractPayloadFailureMessage: deps.extractPayloadFailureMessage,
          isTransientSqliteLockError: deps.isTransientSqliteLockError,
          payloadIndicatesFailure: deps.payloadIndicatesFailure,
          printCliValue: deps.printCliValue,
          unwrapResultRecord: deps.unwrapResultRecord,
          withTransientSqliteLockRetry: deps.withTransientSqliteLockRetry,
        },
        session,
        withCliSession,
      });

      registerStoreVisualCommand(memory, {
        config,
        deps: {
          createMemoryTools: deps.createMemoryTools,
          payloadIndicatesFailure: deps.payloadIndicatesFailure,
          printCliValue: deps.printCliValue,
          readVisualDuplicatePolicy: deps.readVisualDuplicatePolicy,
        },
        session,
        withCliSession,
      });
    },
    { commands: [rootCommand] },
  );
}

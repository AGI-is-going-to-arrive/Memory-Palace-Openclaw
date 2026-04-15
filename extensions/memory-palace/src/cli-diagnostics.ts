import type { CliLogger } from "openclaw/plugin-sdk/core";
import type {
  DiagnosticReport,
  PluginConfig,
  SharedClientSession,
  SmartExtractionCategory,
} from "./types.js";

type RetryRunner = <T>(
  operation: () => Promise<T>,
  shouldRetry?: (value: T) => boolean,
  maxAttempts?: number,
  initialDelayMs?: number,
) => Promise<T>;

export type RegisterDiagnosticCliDeps = {
  displaySmartExtractionCategory: (value: SmartExtractionCategory) => string;
  extractReadText: (raw: unknown) => {
    text: string;
    selection?: unknown;
    degraded?: boolean;
    error?: string;
  };
  formatError: (error: unknown) => string;
  getTransportFallbackOrder: (config: PluginConfig) => string[];
  normalizeIndexStatusPayload: (value: unknown) => unknown;
  payloadIndicatesFailure: (value: unknown) => boolean;
  persistTransportDiagnosticsSnapshot: (
    config: PluginConfig,
    client: SharedClientSession["client"],
    report?: DiagnosticReport,
  ) => void;
  printCliValue: (value: unknown, json: boolean) => void;
  probeProfileMemoryState: (
    client: SharedClientSession["client"],
    config: PluginConfig,
  ) => Promise<{ blockCount: number; paths: string[] } | null>;
  runDoctorReport: (
    config: PluginConfig,
    session: SharedClientSession,
    query: string,
  ) => Promise<DiagnosticReport>;
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
  snapshotPluginRuntimeState: (config: PluginConfig) => unknown;
  withTransientSqliteLockRetry: RetryRunner;
};

export function registerDiagnosticCommands(
  memory: { command(name: string): any },
  options: {
    config: PluginConfig;
    deps: RegisterDiagnosticCliDeps;
    logger: CliLogger;
    session: SharedClientSession;
    withCliSession: <T>(task: () => Promise<T>) => Promise<T>;
  },
): void {
  const { config, deps, logger, session, withCliSession } = options;

  const extractSleepConsolidationStatus = (value: unknown): unknown => {
    if (
      value &&
      typeof value === "object" &&
      "runtime" in (value as Record<string, unknown>)
    ) {
      const runtime = (value as Record<string, unknown>).runtime;
      if (
        runtime &&
        typeof runtime === "object" &&
        "sleep_consolidation" in (runtime as Record<string, unknown>)
      ) {
        return (runtime as Record<string, unknown>).sleep_consolidation;
      }
    }
    return null;
  };

  const extractFlushTrackerStatus = (value: unknown): unknown => {
    if (!value || typeof value !== "object") {
      return null;
    }
    const runtime = (value as Record<string, unknown>).runtime;
    if (!runtime || typeof runtime !== "object") {
      return null;
    }
    const smLite = (runtime as Record<string, unknown>).sm_lite;
    if (!smLite || typeof smLite !== "object") {
      return null;
    }
    const flushTracker = (smLite as Record<string, unknown>).flush_tracker;
    return flushTracker && typeof flushTracker === "object" ? flushTracker : null;
  };

  memory
    .command("status")
    .option("--json", "print json payload")
    .action(async (cliOptions: { json?: boolean }) => {
      await withCliSession(async () => {
        try {
          const payload = await deps.withTransientSqliteLockRetry(
            () =>
              session.withClient(async (client) => {
                const status = deps.normalizeIndexStatusPayload(await client.indexStatus());
                const profileState = config.profileMemory.enabled
                  ? await deps.probeProfileMemoryState(client, config)
                  : null;
                return {
                  stableEntrypoint: "openclaw memory-palace ...",
                  hostNativeCommand: "openclaw memory ...",
                  transport: client.activeTransportKind,
                  connectionModel: "persistent-client",
                  sleepConsolidation: extractSleepConsolidationStatus(status),
                  profileMemory: {
                    enabled: config.profileMemory.enabled,
                    injectBeforeAgentStart: config.profileMemory.injectBeforeAgentStart,
                    maxCharsPerBlock: config.profileMemory.maxCharsPerBlock,
                    blocks: config.profileMemory.blocks,
                    detectedBlockCount: profileState?.blockCount ?? 0,
                    detectedPaths: profileState?.paths ?? [],
                  },
                  hostBridge: {
                    enabled: config.hostBridge.enabled,
                    importUserMd: config.hostBridge.importUserMd,
                    importMemoryMd: config.hostBridge.importMemoryMd,
                    importDailyMemory: config.hostBridge.importDailyMemory,
                    writeBackSummary: config.hostBridge.writeBackSummary,
                    maxHits: config.hostBridge.maxHits,
                    maxImportPerRun: config.hostBridge.maxImportPerRun,
                  },
                  smartExtraction: {
                    enabled: config.smartExtraction.enabled,
                    mode: config.smartExtraction.mode,
                    minConversationMessages: config.smartExtraction.minConversationMessages,
                    maxTranscriptChars: config.smartExtraction.maxTranscriptChars,
                    timeoutMs: config.smartExtraction.timeoutMs,
                    retryAttempts: config.smartExtraction.retryAttempts,
                    circuitBreakerFailures: config.smartExtraction.circuitBreakerFailures,
                    circuitBreakerCooldownMs: config.smartExtraction.circuitBreakerCooldownMs,
                    categories: config.smartExtraction.categories.map((entry) =>
                      deps.displaySmartExtractionCategory(entry),
                    ),
                    effectiveProfile: config.smartExtraction.effectiveProfile ?? null,
                  },
                  reconcile: {
                    enabled: config.reconcile.enabled,
                    profileMergePolicy: config.reconcile.profileMergePolicy,
                    eventMergePolicy: config.reconcile.eventMergePolicy,
                    similarityThreshold: config.reconcile.similarityThreshold,
                    actions: config.reconcile.actions,
                  },
                  capturePipeline: {
                    mode: config.capturePipeline.mode,
                    captureAssistantDerived: config.capturePipeline.captureAssistantDerived,
                    maxAssistantDerivedPerRun: config.capturePipeline.maxAssistantDerivedPerRun,
                    pendingOnFailure: config.capturePipeline.pendingOnFailure,
                    minConfidence: config.capturePipeline.minConfidence,
                    pendingConfidence: config.capturePipeline.pendingConfidence,
                    effectiveProfile: config.capturePipeline.effectiveProfile ?? null,
                  },
                  fallbackOrder: deps.getTransportFallbackOrder(config),
                  runtimeState: deps.snapshotPluginRuntimeState(config),
                  diagnostics: client.diagnostics,
                  status,
                };
              }),
            (result) => deps.payloadIndicatesFailure(result.status),
          );
          deps.persistTransportDiagnosticsSnapshot(config, session.client);
          deps.printCliValue(payload, cliOptions.json === true);
          if (deps.payloadIndicatesFailure(payload)) {
            process.exitCode = 1;
          }
        } catch (error) {
          logger.error(deps.formatError(error));
          process.exitCode = 1;
        }
      });
    });

  memory
    .command("verify")
    .option("--json", "print json payload")
    .action(async (cliOptions: { json?: boolean }) => {
      await withCliSession(async () => {
        const report = await deps.runVerifyReport(config, session);
        deps.printCliValue(report, cliOptions.json === true);
        if (!report.ok) {
          process.exitCode = 1;
        }
      });
    });

  memory
    .command("doctor")
    .option("--query <text>", "search probe query")
    .option("--json", "print json payload")
    .action(async (cliOptions: { query?: string; json?: boolean }) => {
      await withCliSession(async () => {
        const report = await deps.runDoctorReport(
          config,
          session,
          (cliOptions.query ?? "memory palace").trim() || "memory palace",
        );
        deps.printCliValue(report, cliOptions.json === true);
        if (!report.ok) {
          process.exitCode = 1;
        }
      });
    });

  memory
    .command("smoke")
    .option("--query <text>", "search probe query")
    .option("--path-or-uri <value>", "known path or URI for the follow-up read probe")
    .option("--expect-hit", "fail when the search probe returns no hit")
    .option("--json", "print json payload")
    .action(
      async (cliOptions: {
        query?: string;
        pathOrUri?: string;
        expectHit?: boolean;
        json?: boolean;
      }) => {
        await withCliSession(async () => {
          const report = await deps.runSmokeReport(config, session, {
            query: (cliOptions.query ?? "memory palace").trim() || "memory palace",
            pathOrUri: cliOptions.pathOrUri,
            expectHit: cliOptions.expectHit === true,
          });
          deps.printCliValue(report, cliOptions.json === true);
          if (!report.ok) {
            process.exitCode = 1;
          }
        });
      },
    );

  memory
    .command("probe-compact-reflection")
    .option("--seed-event <text>", "seed event text for compact_context reflection")
    .option("--agent-key <value>", "reflection agent key")
    .option("--session-ref <value>", "reflection session ref")
    .option("--json", "print json payload")
    .action(
      async (cliOptions: {
        seedEvent?: string;
        agentKey?: string;
        sessionRef?: string;
        json?: boolean;
      }) => {
        await withCliSession(async () => {
          try {
            const seedEvent =
              (cliOptions.seedEvent ?? "").trim() ||
              `compact reflection probe ${Date.now()}`;
            const agentKey = (cliOptions.agentKey ?? "main").trim() || "main";
            const sessionRef =
              (cliOptions.sessionRef ?? "").trim() ||
              `compact-reflection-probe-${Date.now()}`;
            const payload = await session.withClient(async (client) => {
              const result = deps.normalizeIndexStatusPayload(
                await client.compactContextReflection({
                  reason: "reflection_lane",
                  force: true,
                  max_lines: 8,
                  seed_event: seedEvent,
                  reflection_root_uri: config.reflection.rootUri,
                  reflection_agent_key: agentKey,
                  reflection_session_ref: sessionRef,
                  reflection_agent_id: agentKey,
                  reflection_session_id: sessionRef,
                  reflection_priority: 2,
                  reflection_disclosure:
                    "When recalling cross-session lessons, invariants, or open loops.",
                  reflection_decay_hint_days: 14,
                  reflection_retention_class: "rolling_session",
                }),
              );
              const record =
                result && typeof result === "object"
                  ? (result as Record<string, unknown>)
                  : {};
              const reflectionUri =
                (typeof record.reflection_uri === "string" && record.reflection_uri) ||
                (typeof record.uri === "string" && record.uri) ||
                "";
              let reflectionText = "";
              if (reflectionUri) {
                const readRaw = await client.readMemory({ uri: reflectionUri });
                const extracted = deps.extractReadText(readRaw);
                if (extracted.error) {
                  throw new Error(extracted.error);
                }
                reflectionText = extracted.text;
              }
              return {
                transport: client.activeTransportKind,
                seedEvent,
                sessionRef,
                result: record,
                reflectionUri,
                usedAtomicPath:
                  reflectionText.includes("- compact_source_hash:") &&
                  !reflectionText.includes("- compact_source_uri:"),
                reflectionTextExcerpt: reflectionText.slice(0, 800),
              };
            });
            deps.printCliValue(payload, cliOptions.json === true);
            if (
              deps.payloadIndicatesFailure(payload.result) ||
              payload.usedAtomicPath !== true
            ) {
              process.exitCode = 1;
            }
          } catch (error) {
            logger.error(deps.formatError(error));
            process.exitCode = 1;
          }
        });
      },
    );

  memory
    .command("probe-high-value-flush")
    .option("--first-query <text>", "first high-value query")
    .option("--second-query <text>", "second high-value query")
    .option("--reason <text>", "compact_context reason")
    .option("--json", "print json payload")
    .action(
      async (cliOptions: {
        firstQuery?: string;
        secondQuery?: string;
        reason?: string;
        json?: boolean;
      }) => {
        await withCliSession(async () => {
          try {
            const firstQuery =
              (cliOptions.firstQuery ?? "").trim() ||
              `remember workflow preference marker ${Date.now()} for future recall`;
            const secondQuery =
              (cliOptions.secondQuery ?? "").trim() ||
              `remember default workflow marker ${Date.now()} for this short session`;
            const reason =
              (cliOptions.reason ?? "").trim() || "probe_high_value_flush";

            const payload = await session.withClient(async (client) => {
              const searchOne = deps.normalizeIndexStatusPayload(
                await client.searchMemory({
                  query: firstQuery,
                  mode: "keyword",
                  max_results: 10,
                }),
              );
              const searchTwo = deps.normalizeIndexStatusPayload(
                await client.searchMemory({
                  query: secondQuery,
                  mode: "keyword",
                  max_results: 10,
                }),
              );
              const result = deps.normalizeIndexStatusPayload(
                await client.compactContext({
                  reason,
                  force: false,
                  max_lines: 12,
                }),
              );
              const status = deps.normalizeIndexStatusPayload(await client.indexStatus());
              return {
                transport: client.activeTransportKind,
                queries: [firstQuery, secondQuery],
                result,
                statusRuntime: extractFlushTrackerStatus(status),
                status,
                searchOne,
                searchTwo,
              };
            });
            deps.printCliValue(payload, cliOptions.json === true);
            if (
              deps.payloadIndicatesFailure(payload.result) ||
              !payload.statusRuntime
            ) {
              process.exitCode = 1;
            }
          } catch (error) {
            logger.error(deps.formatError(error));
            process.exitCode = 1;
          }
        });
      },
    );
}

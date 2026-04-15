import type { SharedClientSession } from "./types.js";
import { readPositiveNumber } from "./utils.js";

type RetryRunner = <T>(
  operation: () => Promise<T>,
  shouldRetry?: (value: T) => boolean,
  maxAttempts?: number,
  initialDelayMs?: number,
) => Promise<T>;

export type RegisterIndexCliDeps = {
  extractPayloadFailureMessage: (value: unknown) => string;
  isTransientSqliteLockError: (value: unknown) => boolean;
  payloadIndicatesFailure: (value: unknown) => boolean;
  printCliValue: (value: unknown, json: boolean) => void;
  unwrapResultRecord: (value: unknown) => Record<string, unknown>;
  withTransientSqliteLockRetry: RetryRunner;
};

export function registerIndexCommands(
  memory: { command(name: string): any },
  options: {
    deps: RegisterIndexCliDeps;
    session: SharedClientSession;
    withCliSession: <T>(task: () => Promise<T>) => Promise<T>;
  },
): void {
  const { deps, session, withCliSession } = options;

  for (const commandName of ["index", "reindex"] as const) {
    memory
      .command(commandName)
      .option("--memory-id <id>", "rebuild specific memory id")
      .option("--reason <reason>", "reason label")
      .option("--timeout-seconds <n>", "wait timeout")
      .option("--wait", "wait for completion")
      .option("--sleep-consolidation", "enqueue sleep consolidation")
      .option("--json", "print json payload")
      .action(
        async (cliOptions: {
          memoryId?: string;
          reason?: string;
          timeoutSeconds?: string;
          wait?: boolean;
          sleepConsolidation?: boolean;
          json?: boolean;
        }) => {
          await withCliSession(async () => {
            const payload = await deps.withTransientSqliteLockRetry(
              () =>
                session.withClient(async (client) => {
                  const result = deps.unwrapResultRecord(
                    await client.rebuildIndex({
                      ...(cliOptions.memoryId
                        ? { memory_id: cliOptions.memoryId }
                        : {}),
                      ...(cliOptions.reason ? { reason: cliOptions.reason } : {}),
                      ...(cliOptions.wait ? { wait: true } : {}),
                      ...(cliOptions.sleepConsolidation
                        ? { sleep_consolidation: true }
                        : {}),
                      ...(readPositiveNumber(cliOptions.timeoutSeconds)
                        ? {
                            timeout_seconds: readPositiveNumber(
                              cliOptions.timeoutSeconds,
                            ),
                          }
                        : {}),
                    }),
                  );
                  return {
                    transport: client.activeTransportKind,
                    result,
                  };
                }),
              (value) =>
                deps.payloadIndicatesFailure(value.result) &&
                deps.isTransientSqliteLockError(
                  deps.extractPayloadFailureMessage(value.result),
                ),
              5,
              150,
            );
            deps.printCliValue(payload, cliOptions.json === true);
            if (deps.payloadIndicatesFailure(payload)) {
              process.exitCode = 1;
            }
          });
        },
      );
  }
}

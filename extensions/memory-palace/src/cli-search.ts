import type { CliLogger } from "openclaw/plugin-sdk/core";
import type {
  JsonRecord,
  PluginConfig,
  ResolvedAclPolicy,
  SharedClientSession,
} from "./types.js";
import { readPositiveNumber } from "./utils.js";

export type RegisterSearchCliDeps = {
  payloadIndicatesFailure: (value: unknown) => boolean;
  printCliValue: (value: unknown, json: boolean) => void;
  resolveAdminPolicy: (config: PluginConfig) => ResolvedAclPolicy;
  runScopedSearch: (
    client: SharedClientSession["client"],
    query: string,
    config: PluginConfig,
    policy: ResolvedAclPolicy,
    options?: {
      filters?: JsonRecord;
      mode?: string;
      maxResults?: number;
      candidateMultiplier?: number;
      includeSession?: boolean;
      scopeHint?: string;
      includeReflection?: boolean;
    },
  ) => Promise<unknown>;
};

export function registerSearchCommand(
  memory: { command(name: string): any },
  options: {
    config: PluginConfig;
    deps: RegisterSearchCliDeps;
    logger: CliLogger;
    session: SharedClientSession;
    withCliSession: <T>(task: () => Promise<T>) => Promise<T>;
  },
): void {
  const { config, deps, logger, session, withCliSession } = options;

  memory
    .command("search")
    .argument("[query]", "search query")
    .option("--query <text>", "search query override")
    .option("--mode <mode>", "search mode")
    .option("--max-results <n>", "max results")
    .option("--candidate-multiplier <n>", "candidate multiplier")
    .option("--include-session", "include session memory")
    .option("--include-reflection", "include reflection lane in search results")
    .option("--scope-hint <hint>", "scope hint")
    .option("--json", "print json payload")
    .action(
      async (
        query: string | undefined,
        cliOptions: {
          query?: string;
          mode?: string;
          maxResults?: string;
          candidateMultiplier?: string;
          includeSession?: boolean;
          includeReflection?: boolean;
          scopeHint?: string;
          json?: boolean;
        },
      ) => {
        const effectiveQuery = (cliOptions.query ?? query ?? "").trim();
        if (!effectiveQuery) {
          logger.error("query required");
          process.exitCode = 1;
          return;
        }
        await withCliSession(async () => {
          const payload = await session.withClient(async (client) => {
            return deps.runScopedSearch(
              client,
              effectiveQuery,
              config,
              deps.resolveAdminPolicy(config),
              {
                mode: cliOptions.mode ?? config.query.mode,
                maxResults:
                  readPositiveNumber(cliOptions.maxResults) ?? config.query.maxResults,
                candidateMultiplier:
                  readPositiveNumber(cliOptions.candidateMultiplier) ??
                  config.query.candidateMultiplier,
                includeSession:
                  cliOptions.includeSession ?? config.query.includeSession,
                scopeHint: cliOptions.scopeHint ?? config.query.scopeHint,
                filters: config.query.filters,
                includeReflection: cliOptions.includeReflection === true,
              },
            );
          });
          deps.printCliValue(payload, cliOptions.json === true);
          if (deps.payloadIndicatesFailure(payload)) {
            process.exitCode = 1;
          }
        });
      },
    );
}

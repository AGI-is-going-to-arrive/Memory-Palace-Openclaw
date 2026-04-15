import fs from "node:fs";
import path from "node:path";
import type { PluginConfig, SharedClientSession } from "./types.js";
import { readPositiveNumber, readString } from "./utils.js";

export type RegisterMemoryIoCliDeps = {
  buildExportPayload: (
    uri: string,
    text: string,
    mapping: PluginConfig["mapping"],
  ) => Record<string, unknown>;
  extractReadText: (raw: unknown) => {
    text: string;
    selection?: unknown;
    degraded?: boolean;
    error?: string;
  };
  normalizeImportRecords: (raw: unknown) => Record<string, unknown>[];
  payloadIndicatesFailure: (value: unknown) => boolean;
  printCliValue: (value: unknown, json: boolean) => void;
  resolvePathLikeToUri: (pathOrUri: string, mapping: PluginConfig["mapping"]) => string;
  sliceTextByLines: (text: string, from?: number, lines?: number) => string;
  unwrapResultRecord: (value: unknown) => Record<string, unknown>;
  uriToVirtualPath: (uri: string, mapping: PluginConfig["mapping"]) => string;
};

export function registerMemoryIoCommands(
  memory: { command(name: string): any },
  options: {
    config: PluginConfig;
    deps: RegisterMemoryIoCliDeps;
    session: SharedClientSession;
    withCliSession: <T>(task: () => Promise<T>) => Promise<T>;
  },
): void {
  const { config, deps, session, withCliSession } = options;

  memory
    .command("get")
    .argument("<pathOrUri>", "virtual path or Memory Palace URI")
    .option("--from <n>", "starting line")
    .option("--lines <n>", "number of lines")
    .option("--max-chars <n>", "max chars for read_memory")
    .option("--include-ancestors", "include ancestor chain")
    .option("--json", "print json payload")
    .action(
      async (
        pathOrUri: string,
        cliOptions: {
          from?: string;
          lines?: string;
          maxChars?: string;
          includeAncestors?: boolean;
          json?: boolean;
        },
      ) => {
        await withCliSession(async () => {
          const payload = await session.withClient(async (client) => {
            const uri = deps.resolvePathLikeToUri(pathOrUri, config.mapping);
            const raw = await client.readMemory({
              uri,
              ...(readPositiveNumber(cliOptions.maxChars) ?? config.read.maxChars
                ? { max_chars: readPositiveNumber(cliOptions.maxChars) ?? config.read.maxChars }
                : {}),
              ...(cliOptions.includeAncestors ?? config.read.includeAncestors
                ? { include_ancestors: true }
                : {}),
            });
            const extracted = deps.extractReadText(raw);
            if (extracted.error) {
              throw new Error(extracted.error);
            }
            return {
              path: deps.uriToVirtualPath(uri, config.mapping),
              uri,
              text: deps.sliceTextByLines(
                extracted.text,
                readPositiveNumber(cliOptions.from),
                readPositiveNumber(cliOptions.lines),
              ),
              degraded: extracted.degraded ?? false,
              selection: extracted.selection,
            };
          });
          deps.printCliValue(payload, cliOptions.json === true);
          if (deps.payloadIndicatesFailure(payload)) {
            process.exitCode = 1;
          }
        });
      },
    );

  memory
    .command("export")
    .argument("<pathOrUri>", "virtual path or Memory Palace URI")
    .option("--output <file>", "write exported payload to file")
    .option("--json", "print json payload")
    .action(
      async (
        pathOrUri: string,
        cliOptions: {
          output?: string;
          json?: boolean;
        },
      ) => {
        await withCliSession(async () => {
          const payload = await session.withClient(async (client) => {
            const uri = deps.resolvePathLikeToUri(pathOrUri, config.mapping);
            const raw = await client.readMemory({
              uri,
              ...(config.read.maxChars ? { max_chars: config.read.maxChars } : {}),
              ...(config.read.includeAncestors ? { include_ancestors: true } : {}),
            });
            const extracted = deps.extractReadText(raw);
            if (extracted.error) {
              throw new Error(extracted.error);
            }
            const exported = deps.buildExportPayload(uri, extracted.text, config.mapping);
            if (cliOptions.output) {
              fs.writeFileSync(
                cliOptions.output,
                `${JSON.stringify(exported, null, 2)}\n`,
                "utf8",
              );
            }
            return {
              ...exported,
              ...(cliOptions.output
                ? { output: path.resolve(cliOptions.output) }
                : {}),
            };
          });
          deps.printCliValue(payload, cliOptions.json === true);
        });
      },
    );

  memory
    .command("import")
    .requiredOption("--input <file>", "path to import json")
    .option("--execute", "apply writes; default is dry-run validation only")
    .option("--json", "print json payload")
    .action(async (cliOptions: { input: string; execute?: boolean; json?: boolean }) => {
      await withCliSession(async () => {
        const rawText = fs.readFileSync(cliOptions.input, "utf8");
        const parsed = JSON.parse(rawText) as unknown;
        const records = deps.normalizeImportRecords(parsed);
        if (records.length === 0) {
          throw new Error("No import records found.");
        }

        const payload = await session.withClient(async (client) => {
          const results: unknown[] = [];
          for (const record of records) {
            const uri = readString(record.uri);
            const append = readString(record.append);
            const oldString = readString(record.oldString);
            const newString =
              record.newString === undefined ? undefined : String(record.newString);
            const parentUri = readString(record.parentUri);
            const title = readString(record.title);
            const content = readString(record.content);
            const disclosure = readString(record.disclosure);
            const priority = readPositiveNumber(record.priority);

            if (uri && (append || oldString !== undefined || newString !== undefined)) {
              if (!cliOptions.execute) {
                results.push({
                  mode: append ? "append" : "patch",
                  uri,
                  ok: true,
                  dryRun: true,
                });
                continue;
              }
              const result = await client.updateMemory({
                uri,
                ...(append ? { append } : {}),
                ...(oldString ? { old_string: oldString } : {}),
                ...(newString !== undefined ? { new_string: newString } : {}),
                ...(priority ? { priority } : {}),
                ...(disclosure ? { disclosure } : {}),
              });
              results.push({
                mode: append ? "append" : "patch",
                uri,
                result: deps.unwrapResultRecord(result),
              });
              continue;
            }

            if (!parentUri || !title || !content) {
              throw new Error(
                "Each create record must provide `parentUri`, `title`, and `content`, or provide `uri` plus patch/append fields.",
              );
            }

            const effectivePriority = priority ?? 5;
            if (!cliOptions.execute) {
              results.push({
                mode: "create",
                parentUri,
                title,
                priority: effectivePriority,
                ok: true,
                dryRun: true,
              });
              continue;
            }

            const result = await client.createMemory({
              parent_uri: parentUri,
              title,
              content,
              priority: effectivePriority,
              ...(disclosure ? { disclosure } : {}),
            });
            results.push({
              mode: "create",
              parentUri,
              title,
              priority: effectivePriority,
              result: deps.unwrapResultRecord(result),
            });
          }

          return {
            ok: true,
            dryRun: cliOptions.execute !== true,
            input: path.resolve(cliOptions.input),
            count: records.length,
            results,
          };
        });
        deps.printCliValue(payload, cliOptions.json === true);
      });
    });
}

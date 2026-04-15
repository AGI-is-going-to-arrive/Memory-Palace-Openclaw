import type { PluginConfig, SharedClientSession, VisualDuplicatePolicy } from "./types.js";

type StoreVisualTool = {
  name: string;
  execute: (toolCallId: string, params: unknown) => Promise<unknown>;
};

export type RegisterStoreVisualCliDeps = {
  createMemoryTools: (
    config: PluginConfig,
    session: SharedClientSession,
  ) => StoreVisualTool[];
  payloadIndicatesFailure: (value: unknown) => boolean;
  printCliValue: (value: unknown, json: boolean) => void;
  readVisualDuplicatePolicy: (
    value: unknown,
  ) => VisualDuplicatePolicy | undefined;
};

export function registerStoreVisualCommand(
  memory: { command(name: string): any },
  options: {
    config: PluginConfig;
    deps: RegisterStoreVisualCliDeps;
    session: SharedClientSession;
    withCliSession: <T>(task: () => Promise<T>) => Promise<T>;
  },
): void {
  const { config, deps, session, withCliSession } = options;

  memory
    .command("store-visual")
    .requiredOption("--media-ref <value>", "media ref")
    .option("--summary <value>", "visual summary")
    .option("--source-channel <value>", "source channel")
    .option("--observed-at <value>", "observation timestamp")
    .option("--ocr <value>", "OCR text")
    .option("--scene <value>", "scene label")
    .option("--why-relevant <value>", "why this matters")
    .option("--duplicate-policy <value>", "duplicate policy (merge|reject|new)")
    .option("--confidence <value>", "confidence score")
    .option("--entities <items>", "comma-separated entities")
    .option("--visual-context <json>", "best-effort current-turn visual context JSON")
    .option("--json", "print json payload")
    .action(
      async (cliOptions: {
        mediaRef: string;
        summary?: string;
        sourceChannel?: string;
        observedAt?: string;
        ocr?: string;
        scene?: string;
        whyRelevant?: string;
        duplicatePolicy?: string;
        confidence?: string;
        entities?: string;
        visualContext?: string;
        json?: boolean;
      }) => {
        await withCliSession(async () => {
          const tool = deps
            .createMemoryTools(config, session)
            .find((entry) => entry.name === "memory_store_visual");
          if (!tool) {
            throw new Error("memory_store_visual unavailable");
          }
          const payload = await tool.execute("cli-memory-store-visual", {
            mediaRef: cliOptions.mediaRef,
            summary: cliOptions.summary,
            sourceChannel: cliOptions.sourceChannel,
            observedAt: cliOptions.observedAt,
            ocr: cliOptions.ocr,
            scene: cliOptions.scene,
            whyRelevant: cliOptions.whyRelevant,
            duplicatePolicy: deps.readVisualDuplicatePolicy(
              cliOptions.duplicatePolicy,
            ),
            confidence:
              cliOptions.confidence !== undefined
                ? Number.parseFloat(cliOptions.confidence)
                : undefined,
            entities: cliOptions.entities
              ? cliOptions.entities
                  .split(",")
                  .map((entry) => entry.trim())
                  .filter(Boolean)
              : undefined,
            visualContext: cliOptions.visualContext,
          });
          const details = (payload as { details?: unknown }).details ?? payload;
          deps.printCliValue(details, cliOptions.json === true);
          if (deps.payloadIndicatesFailure(details)) {
            process.exitCode = 1;
          }
        });
      },
    );
}

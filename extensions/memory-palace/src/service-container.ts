import type { AutoCaptureDeps } from "./auto-capture.js";
import type { AutoRecallDeps } from "./auto-recall.js";
import type { RegisterMemoryCliDeps } from "./cli-root.js";
import type { RegisterLifecycleHookDeps } from "./lifecycle-hooks.js";
import type { MemoryToolDeps } from "./memory-tools.js";
import type {
  AgentEndReflectionDeps,
  CommandNewReflectionDeps,
  CompactContextReflectionDeps,
} from "./reflection-runners.js";

export type PluginServices = {
  memoryTools: MemoryToolDeps;
  autoRecall: AutoRecallDeps;
  autoCapture: AutoCaptureDeps;
  reflection: {
    agentEnd: AgentEndReflectionDeps;
    commandNew: CommandNewReflectionDeps;
    compactContext: CompactContextReflectionDeps;
  };
  lifecycle: RegisterLifecycleHookDeps;
  cli: RegisterMemoryCliDeps;
};

export function createPluginServices(services: PluginServices): PluginServices {
  return services;
}

import { spawn } from "node:child_process";

const VISUAL_ENRICHMENT_FORCE_KILL_GRACE_MS = 250;

export type VisualProcessTreeTerminator = (pid: number, force: boolean) => void | Promise<void>;
export type TerminableVisualChild = {
  pid?: number;
  kill(signal?: NodeJS.Signals | number): boolean;
};

let visualTerminationPlatformOverride: NodeJS.Platform | null = null;
let visualWindowsProcessTreeTerminatorOverride: VisualProcessTreeTerminator | null = null;

function currentVisualTerminationPlatform(): NodeJS.Platform {
  return visualTerminationPlatformOverride ?? process.platform;
}

async function killVisualProcessTreeWindows(pid: number, force: boolean): Promise<void> {
  if (pid <= 0) {
    return;
  }
  if (visualWindowsProcessTreeTerminatorOverride) {
    await visualWindowsProcessTreeTerminatorOverride(pid, force);
    return;
  }
  await new Promise<void>((resolve) => {
    const taskkill = spawn(
      "taskkill",
      ["/PID", String(pid), "/T", ...(force ? ["/F"] : [])],
      {
        stdio: "ignore",
        windowsHide: true,
      },
    );
    const finish = () => resolve();
    taskkill.once("error", finish);
    taskkill.once("close", finish);
    taskkill.unref?.();
  });
}

function killVisualProcessTreePosix(pid: number, force: boolean): boolean {
  if (pid <= 0) {
    return false;
  }
  try {
    process.kill(-pid, force ? "SIGKILL" : "SIGTERM");
    return true;
  } catch (error) {
    const code = (error as NodeJS.ErrnoException | undefined)?.code;
    if (code === "ESRCH") {
      return true;
    }
    return false;
  }
}

export function terminateVisualChildProcess(
  child: TerminableVisualChild,
  options: { force: boolean },
): void {
  const { force } = options;
  const pid = typeof child.pid === "number" && Number.isInteger(child.pid) ? child.pid : 0;
  if (currentVisualTerminationPlatform() === "win32" && pid > 0) {
    void killVisualProcessTreeWindows(pid, force).catch(() => {
      try {
        child.kill(force ? "SIGKILL" : "SIGTERM");
      } catch {
        // Ignore termination races once the adapter has already exited.
      }
    });
    return;
  }
  if (pid > 0 && killVisualProcessTreePosix(pid, force)) {
    return;
  }
  try {
    child.kill(force ? "SIGKILL" : "SIGTERM");
  } catch {
    // Ignore termination races once the adapter has already exited.
  }
}

export function setVisualTerminationPlatformForTesting(
  platform?: NodeJS.Platform,
): void {
  visualTerminationPlatformOverride = platform ?? null;
}

export function setVisualWindowsProcessTreeTerminatorForTesting(
  terminator?: VisualProcessTreeTerminator,
): void {
  visualWindowsProcessTreeTerminatorOverride = terminator ?? null;
}

export function scheduleVisualForceKill(
  child: TerminableVisualChild,
  timeoutMs: number,
  isSettled: () => boolean,
): ReturnType<typeof setTimeout> {
  const forceKillTimer = setTimeout(() => {
    if (isSettled()) {
      return;
    }
    terminateVisualChildProcess(child, { force: true });
  }, Math.max(VISUAL_ENRICHMENT_FORCE_KILL_GRACE_MS, Math.min(1_000, timeoutMs)));
  forceKillTimer.unref?.();
  return forceKillTimer;
}

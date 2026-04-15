import { once } from 'node:events';
import { rm } from 'node:fs/promises';
import { setTimeout as sleep } from 'node:timers/promises';

const REMOVE_RETRYABLE_CODES = new Set(['EBUSY', 'EMFILE', 'ENOTEMPTY', 'EPERM']);

export const removePathWithRetries = async (
  targetPath,
  {
    attempts = process.platform === 'win32' ? 10 : 4,
    delayMs = process.platform === 'win32' ? 250 : 75,
  } = {}
) => {
  if (!targetPath) return;

  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    try {
      await rm(targetPath, { recursive: true, force: true, maxRetries: 0 });
      return;
    } catch (error) {
      if (
        attempt === attempts
        || !REMOVE_RETRYABLE_CODES.has(error?.code)
      ) {
        throw error;
      }
      await sleep(delayMs * attempt);
    }
  }
};

export const terminateChildProcess = async (
  childProcess,
  {
    softKillTimeoutMs = process.platform === 'win32' ? 3_000 : 1_500,
    hardKillTimeoutMs = process.platform === 'win32' ? 3_000 : 1_500,
  } = {}
) => {
  if (!childProcess) return;
  if (childProcess.exitCode !== null || childProcess.signalCode !== null) return;

  const waitForExit = once(childProcess, 'exit').catch(() => null);

  try {
    childProcess.kill('SIGTERM');
  } catch (_error) {
    return;
  }

  const exitedAfterSoftKill = await Promise.race([
    waitForExit.then(() => true),
    sleep(softKillTimeoutMs).then(() => false),
  ]);
  if (exitedAfterSoftKill) {
    return;
  }

  try {
    childProcess.kill(process.platform === 'win32' ? undefined : 'SIGKILL');
  } catch (_error) {
    return;
  }

  await Promise.race([
    waitForExit,
    sleep(hardKillTimeoutMs),
  ]);
};

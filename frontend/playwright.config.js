import { existsSync, mkdirSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { defineConfig, devices } from '@playwright/test';

const frontendRoot = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(frontendRoot, '..');
const backendRoot = path.join(repoRoot, 'backend');
const resolveBackendPython = () => {
  const override = (process.env.PLAYWRIGHT_E2E_BACKEND_PYTHON || '').trim();
  if (override.length > 0) {
    return override;
  }

  const candidates = process.platform === 'win32'
    ? [
        path.join(repoRoot, '.venv', 'Scripts', 'python.exe'),
        path.join(backendRoot, '.venv', 'Scripts', 'python.exe'),
      ]
    : [
        path.join(repoRoot, '.venv', 'bin', 'python'),
        path.join(backendRoot, '.venv', 'bin', 'python'),
      ];

  return candidates.find((candidate) => existsSync(candidate))
    || (process.platform === 'win32' ? 'python' : 'python3');
};
const backendPython = resolveBackendPython();
const runSuffix = `${process.pid}-${Date.now()}`;
const externalBaseUrl = (process.env.PLAYWRIGHT_E2E_EXTERNAL_BASE_URL || '').trim();
const useExternalBaseUrl = externalBaseUrl.length > 0;
const tempRoot = process.env.PLAYWRIGHT_E2E_TEMP_ROOT
  || path.join(repoRoot, '.tmp', 'playwright-e2e', runSuffix);
const ownsTempRoot = !process.env.PLAYWRIGHT_E2E_TEMP_ROOT;
// Use a stable invocation seed derived from the shared temp root so the
// config evaluates to the same ports across Playwright's main and worker
// processes, while still avoiding collisions across separate invocations.
const workerOffset = parseInt(process.env.PLAYWRIGHT_TEST_WORKER_INDEX || '0', 10);
const invocationSeedMatch = path.basename(tempRoot).match(/^(\d+)-/);
const invocationSeed = parseInt(
  process.env.PLAYWRIGHT_E2E_PROCESS_OFFSET
    || invocationSeedMatch?.[1]
    || String(process.pid),
  10,
);
const sharedPortOffset = (Number.isNaN(invocationSeed) ? 0 : (invocationSeed % 1000) * 10)
  + (Number.isNaN(workerOffset) ? 0 : workerOffset);
const apiPort = process.env.PLAYWRIGHT_E2E_API_PORT || String(18080 + sharedPortOffset);
const uiPort = process.env.PLAYWRIGHT_E2E_UI_PORT || String(4173 + sharedPortOffset);
const backendUrl = `http://127.0.0.1:${apiPort}`;
const frontendUrl = useExternalBaseUrl ? externalBaseUrl : `http://127.0.0.1:${uiPort}`;
const databasePath = path.join(tempRoot, 'playwright-e2e.db');
const transportDiagnosticsPath =
  process.env.PLAYWRIGHT_E2E_TRANSPORT_DIAGNOSTICS_PATH
  || path.join(tempRoot, 'playwright-e2e-transport.json');
const databaseUrl =
  process.env.PLAYWRIGHT_E2E_DATABASE_URL
  || `sqlite+aiosqlite:///${databasePath.replace(/\\/g, '/')}`;
const apiKey = process.env.PLAYWRIGHT_E2E_API_KEY || 'playwright-e2e-key';
const outputDir =
  process.env.PLAYWRIGHT_E2E_OUTPUT_DIR
  || path.join(tempRoot, 'test-results');
const e2eHome = path.join(tempRoot, 'home');
const bootstrapSetupRoot = path.join(e2eHome, '.openclaw', 'memory-palace');
const bootstrapEnvPath = path.join(bootstrapSetupRoot, 'runtime.env');

mkdirSync(tempRoot, { recursive: true });
mkdirSync(bootstrapSetupRoot, { recursive: true });
writeFileSync(
  bootstrapEnvPath,
  [
    'OPENCLAW_MEMORY_PALACE_PROFILE_EFFECTIVE=b',
    'OPENCLAW_MEMORY_PALACE_PROFILE_REQUESTED=b',
    `DATABASE_URL=${databaseUrl}`,
    `MCP_API_KEY=${apiKey}`,
    '',
  ].join('\n'),
  'utf8'
);

process.env.PLAYWRIGHT_E2E_TEMP_ROOT = tempRoot;
process.env.PLAYWRIGHT_E2E_OWNS_TEMP_ROOT = ownsTempRoot ? '1' : '0';
process.env.PLAYWRIGHT_E2E_TRANSPORT_DIAGNOSTICS_PATH = transportDiagnosticsPath;

export default defineConfig({
  testDir: './e2e',
  timeout: 60_000,
  expect: {
    timeout: 10_000,
  },
  fullyParallel: false,
  // Tests share a single backend + DB instance; parallel workers cause
  // port collisions and bootstrap state races.  Use --workers=N to
  // override if port-offset logic proves sufficient in the future.
  workers: 1,
  retries: 0,
  reporter: 'list',
  outputDir,
  globalTeardown: path.join(frontendRoot, 'e2e', 'playwright.globalTeardown.js'),
  use: {
    baseURL: frontendUrl,
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
    ...devices['Desktop Chrome'],
  },
  webServer: useExternalBaseUrl
    ? undefined
    : [
        {
          command: `"${backendPython}" -m uvicorn main:app --host 127.0.0.1 --port ${apiPort}`,
          cwd: backendRoot,
          url: `${backendUrl}/health`,
          reuseExistingServer: false,
          stdout: 'pipe',
          stderr: 'pipe',
          env: {
            ...process.env,
            DATABASE_URL: databaseUrl,
            MCP_API_KEY: apiKey,
            OPENCLAW_TRANSPORT_DIAGNOSTICS_PATH: transportDiagnosticsPath,
            HOME: e2eHome,
            USERPROFILE: e2eHome,
          },
        },
        {
          command: `npm run dev -- --host 127.0.0.1 --port ${uiPort}`,
          cwd: frontendRoot,
          url: frontendUrl,
          reuseExistingServer: false,
          stdout: 'pipe',
          stderr: 'pipe',
          env: {
            ...process.env,
            MEMORY_PALACE_API_PROXY_TARGET: backendUrl,
          },
        },
      ],
});

import { spawnSync } from 'node:child_process';
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { setTimeout as sleep } from 'node:timers/promises';
import { fileURLToPath } from 'node:url';
import { expect, test } from '@playwright/test';
import { removePathWithRetries } from './tempArtifacts.js';

const dashboardApiKey =
  process.env.PLAYWRIGHT_E2E_API_KEY || 'playwright-e2e-key';
const transportDiagnosticsPath =
  process.env.PLAYWRIGHT_E2E_TRANSPORT_DIAGNOSTICS_PATH || '';
const backendApiBaseUrl =
  process.env.PLAYWRIGHT_E2E_API_BASE_URL
  || `http://127.0.0.1:${process.env.PLAYWRIGHT_E2E_API_PORT || '18080'}`;
const openclawBin =
  process.env.PLAYWRIGHT_E2E_OPENCLAW_BIN
  || (process.platform === 'win32' ? 'openclaw.CMD' : 'openclaw');
const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const pluginRoot = path.join(repoRoot, 'extensions', 'memory-palace');
const quoteShellArg = (value: string) => {
  if (!/[\s"]/u.test(value)) return value;
  return `"${value.replaceAll('"', '\\"')}"`;
};

const runOpenClawSync = (args: string[], options: Parameters<typeof spawnSync>[2] = {}) => {
  if (process.platform !== 'win32') {
    return spawnSync(openclawBin, args, options);
  }
  const commandLine = [quoteShellArg(openclawBin), ...args.map(quoteShellArg)].join(' ');
  return spawnSync(
    process.env.ComSpec || 'C:\\Windows\\System32\\cmd.exe',
    ['/d', '/s', '/c', commandLine],
    options
  );
};

const buildOpenClawConfig = (configPath: string, sseUrl: string) => ({
  plugins: {
    allow: ['memory-palace'],
    load: { paths: [pluginRoot] },
    slots: { memory: 'memory-palace' },
    entries: {
      'memory-palace': {
        enabled: true,
        config: {
          transport: 'sse',
          timeoutMs: 1_000,
          connection: {
            connectRetries: 1,
            connectBackoffMs: 50,
            requestRetries: 1,
            healthcheckTtlMs: 0,
          },
          sse: {
            url: sseUrl,
            apiKey: 'playwright-invalid-api-key',
          },
          observability: {
            enabled: true,
            transportDiagnosticsPath,
            maxRecentTransportEvents: 12,
          },
        },
      },
    },
  },
  agents: {
    list: [
      {
        id: 'main',
        default: true,
        workspace: path.join(path.dirname(configPath), 'workspace'),
      },
    ],
  },
});

const clearTransportDiagnosticsArtifacts = () => {
  if (!transportDiagnosticsPath) return;
  rmSync(transportDiagnosticsPath, { force: true });
  rmSync(
    path.join(
      path.dirname(transportDiagnosticsPath),
      `${path.parse(transportDiagnosticsPath).name}.instances`
    ),
    { recursive: true, force: true }
  );
};

const parseOpenClawJson = (raw: string) => {
  const start = raw.indexOf('{');
  const end = raw.lastIndexOf('}');
  if (start === -1 || end === -1 || end < start) {
    throw new Error(`Unable to parse OpenClaw JSON payload:\n${raw}`);
  }
  return JSON.parse(raw.slice(start, end + 1));
};

const waitForEmbeddedSse = async (url: string) => {
  const deadline = Date.now() + 15_000;
  let lastError = '';
  while (Date.now() < deadline) {
    try {
      const response = await fetch(url, {
        headers: {
          'X-MCP-API-Key': 'playwright-invalid-api-key',
        },
      });
      if (response.status === 200 || response.status === 401) {
        return;
      }
      lastError = `unexpected_status:${response.status}`;
    } catch (error) {
      lastError = error instanceof Error ? error.message : String(error);
    }
    await sleep(250);
  }
  throw new Error(`Timed out waiting for embedded SSE readiness: ${lastError}`);
};

const triggerRealTransportIncident = async () => {
  if (!transportDiagnosticsPath) {
    throw new Error('PLAYWRIGHT_E2E_TRANSPORT_DIAGNOSTICS_PATH is required');
  }

  clearTransportDiagnosticsArtifacts();
  const sseUrl = `${backendApiBaseUrl}/sse`;
  const tempRoots: string[] = [];
  let lastResult: ReturnType<typeof spawnSync> | null = null;
  try {
    await waitForEmbeddedSse(sseUrl);

    for (let attempt = 0; attempt < 2; attempt += 1) {
      const tempRoot = mkdtempSync(path.join(os.tmpdir(), 'openclaw-transport-incident-'));
      tempRoots.push(tempRoot);
      const configPath = path.join(tempRoot, `openclaw-${attempt + 1}.json`);
      const stateDir = path.join(tempRoot, 'state');
      mkdirSync(stateDir, { recursive: true });
      writeFileSync(configPath, JSON.stringify(buildOpenClawConfig(configPath, sseUrl), null, 2));

      lastResult = runOpenClawSync(['memory-palace', 'doctor', '--json'], {
        cwd: repoRoot,
        env: {
          ...process.env,
          OPENCLAW_CONFIG_PATH: configPath,
          OPENCLAW_STATE_DIR: stateDir,
          OPENCLAW_TRANSPORT_DIAGNOSTICS_PATH: transportDiagnosticsPath,
          MCP_API_KEY: dashboardApiKey,
        },
        encoding: 'utf8',
        timeout: 60_000,
      });

      if (lastResult.error) {
        throw lastResult.error;
      }
    }

    if (lastResult?.status === null) {
      throw new Error(
        [
          'openclaw doctor terminated unexpectedly.',
          String(lastResult?.stdout || '').trim(),
          String(lastResult?.stderr || '').trim(),
        ]
          .filter(Boolean)
          .join('\n\n')
      );
    }

    const stdout = String(lastResult?.stdout || '').trim();
    const stderr = String(lastResult?.stderr || '').trim();
    return parseOpenClawJson(stdout || stderr);
  } finally {
    for (const tempRoot of tempRoots) {
      await removePathWithRetries(tempRoot);
    }
  }
};

test('redirects to memory, persists locale, and unlocks protected pages with API key', async ({
  page,
}) => {
  await page.goto('/');
  await page.waitForURL(/\/(memory|setup)$/);

  await expect(page).toHaveTitle('Memory Palace Dashboard');
  const setupRoute = /\/setup$/.test(page.url());
  if (setupRoute) {
    await expect(page.getByRole('heading', { name: 'Bootstrap Setup', exact: true })).toBeVisible();
  } else {
    await expect(page.getByRole('heading', { name: 'Memory Hall', exact: true })).toBeVisible();
  }

  await page.getByTestId('language-toggle').click();

  await expect(page).toHaveTitle('Memory Palace 控制台');
  if (setupRoute) {
    await expect(page.getByRole('heading', { name: 'Bootstrap 配置', exact: true })).toBeVisible();
  } else {
    await expect(page.getByRole('heading', { name: '记忆大厅', exact: true })).toBeVisible();
  }
  await expect
    .poll(async () => page.evaluate(() => document.documentElement.lang))
    .toBe('zh-CN');

  await page.reload();
  await page.waitForURL(setupRoute ? '**/setup' : '**/memory');

  const setApiKeyButton = page.getByTestId('auth-set-api-key');
  const usesManualAuth = (await setApiKeyButton.count()) > 0;
  if (usesManualAuth) {
    await setApiKeyButton.click();
    // PromptDialog replaces native window.prompt — fill the password input and confirm.
    const promptInput = page.locator('div[role="dialog"] input[type="password"]');
    await expect(promptInput).toBeVisible();
    await promptInput.fill(dashboardApiKey);
    await page.locator('div[role="dialog"] button[type="submit"]').click();
    await expect(page.getByRole('button', { name: '更新 API 密钥' })).toBeVisible();
  } else {
    await expect(page.getByText('运行时密钥已启用')).toBeVisible();
  }

  await expect(page.getByText('加载节点失败')).toHaveCount(0);

  await page.getByRole(setupRoute ? 'button' : 'link', { name: '维护' }).click();

  if (setupRoute) {
    await expect(page).toHaveURL(/\/setup$/);
    await expect(page.getByRole('heading', { name: 'Bootstrap 配置', exact: true })).toBeVisible();
    await expect(page.getByRole('heading', { name: /记忆清理|Brain Cleanup/ })).toHaveCount(0);

    await page.getByRole('button', { name: '观测' }).click();
    await expect(page).toHaveURL(/\/setup$/);
    await expect(page.getByRole('heading', { name: 'Bootstrap 配置', exact: true })).toBeVisible();
    await expect
      .poll(async () => page.evaluate(() => window.localStorage.getItem('memory-palace.locale')))
      .toBe('zh-CN');
    if (usesManualAuth) {
      await expect(page.getByRole('button', { name: '更新 API 密钥' })).toBeVisible();
    }
    return;
  }

  await expect(page).toHaveURL(/\/maintenance$/);
  await expect(page.getByRole('heading', { name: /记忆清理|Brain Cleanup/ })).toBeVisible({ timeout: 20_000 });
  await expect(page.getByRole('heading', { name: /维护控制台|Maintenance Console/ })).toBeVisible({ timeout: 20_000 });
  await expect(page.getByText(/未检测到孤儿记忆。|No orphaned memories detected\./)).toBeVisible({ timeout: 20_000 });
  await expect(page.getByText(/API 密钥缺失或无效/)).toHaveCount(0);

  if (!usesManualAuth) {
    // Manual auth is memory-only, so reloading here would intentionally drop the
    // key and turn the rest of the observability checks into false negatives.
    await page.reload();
    await page.waitForURL(/\/maintenance$/);
    await expect(page.getByRole('heading', { name: /记忆清理|Brain Cleanup/ })).toBeVisible({ timeout: 20_000 });
    await expect(page.getByRole('heading', { name: /维护控制台|Maintenance Console/ })).toBeVisible({ timeout: 20_000 });
  }

  const doctorReport = await triggerRealTransportIncident();
  const doctorChecks = Array.isArray(doctorReport?.checks) ? doctorReport.checks : [];
  const authFailureCheck = doctorChecks.find(
    (entry) => entry?.id === 'transport-health' && String(entry?.message || '').includes('401')
  );
  expect(doctorReport?.status).toBe('fail');
  expect(authFailureCheck).toBeTruthy();

  const summaryResponse = await page.evaluate(async (apiKey) => {
    const response = await fetch('/api/maintenance/observability/summary', {
      headers: {
        'X-MCP-API-Key': apiKey,
      },
    });
    const body = await response.json();
    return {
      ok: response.ok,
      status: response.status,
      body,
    };
  }, dashboardApiKey);
  expect(summaryResponse.ok).toBeTruthy();
  const summary = summaryResponse.body;
  const transport = summary?.transport || {};
  const transportBreakdown = transport?.diagnostics?.exception_breakdown || {};
  const snapshotCount = Number(transport?.snapshot_count || 0);
  const hasBreakdown = snapshotCount >= 1 && Number(transportBreakdown?.total || 0) > 0;
  const signatureCounts = transportBreakdown?.signature_breakdown?.signature_counts || {};
  const authSignature = Object.keys(signatureCounts).find(
    (entry) => entry.includes('healthcheck') && entry.includes('401')
  );
  const authCause = Object.keys(
    transportBreakdown?.incident_breakdown?.canonical_cause_counts || {}
  ).find((entry) => entry.includes('healthcheck') || entry.includes('401'));
  const visibleInstanceIds = Array.isArray(transport?.instances)
    ? transport.instances
        .map((entry) => entry?.instance_id)
        .filter((entry) => typeof entry === 'string' && entry.length > 0)
        .slice(0, 2)
    : [];

  await page.getByRole('link', { name: '观测' }).click();
  await page.waitForURL(/\/observability$/);
  await expect(page.getByRole('heading', { name: '检索观测控制台', exact: true })).toBeVisible();
  await expect(page.getByText('Transport 诊断')).toBeVisible();
  if (hasBreakdown) {
    expect(String(transport?.status || '')).not.toBe('pass');
    expect(authSignature).toBeTruthy();
    expect(authCause).toBeTruthy();
    expect(visibleInstanceIds.length).toBeGreaterThanOrEqual(1);

    await expect(page.getByText('异常拆解')).toBeVisible();
    await expect(page.getByText('Canonical Causes')).toBeVisible();
    await expect(page.getByText(authCause || '', { exact: false }).first()).toBeVisible();
    await expect(page.getByText('Top Signatures')).toBeVisible();
    await expect(page.getByText(authSignature || '', { exact: false }).first()).toBeVisible();
    await expect(page.getByText('Transport 实例')).toBeVisible();
    for (const instanceId of visibleInstanceIds) {
      await expect(page.getByText(instanceId, { exact: false }).first()).toBeVisible();
    }
  } else {
    expect(String(transport?.reason || '')).toBe('transport_trace_unavailable');
    await expect(page.getByText('当前没有 transport 快照（transport_trace_unavailable）。')).toBeVisible();
  }

  await expect
    .poll(async () => page.evaluate(() => window.localStorage.getItem('memory-palace.locale')))
    .toBe('zh-CN');
  if (usesManualAuth) {
    // Auth is now memory-only (not persisted to sessionStorage), so verify
    // the UI still shows the authenticated button after navigation.
    await expect(page.getByRole('button', { name: '更新 API 密钥' })).toBeVisible();
  }
});

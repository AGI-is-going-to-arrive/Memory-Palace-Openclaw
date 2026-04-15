/**
 * P3-3 Quarantine Write Guard — E2E Playwright Tests
 *
 * Validates the observability API contract and confirms quarantine payloads
 * do not break existing pages before P3-4 adds dedicated UI rendering.
 *
 * Run with:
 *   cd frontend && pnpm exec playwright test e2e/p3-3-quarantine-write-guard.spec.ts --reporter=list
 */
import { expect, test } from '@playwright/test';

const dashboardApiKey =
  process.env.PLAYWRIGHT_E2E_API_KEY || 'playwright-e2e-key';

const expectNonNegativeInteger = (value) => {
  expect(Number.isInteger(value)).toBeTruthy();
  expect(value).toBeGreaterThanOrEqual(0);
};

async function ensureMaintenanceAuth(page) {
  const setApiKeyButton = page.getByTestId('auth-set-api-key');
  if ((await setApiKeyButton.count()) === 0) {
    await expect(page.getByText('Runtime key enabled')).toBeVisible();
    return;
  }

  await setApiKeyButton.click();
  const promptInput = page.locator('div[role="dialog"] input[type="password"]');
  await expect(promptInput).toBeVisible();
  await promptInput.fill(dashboardApiKey);
  await page.locator('div[role="dialog"] button[type="submit"]').click();
  await expect(page.getByRole('button', { name: 'Update API key' })).toBeVisible();
}

test.describe('P3-3 Quarantine Write Guard — Observability E2E', () => {

  test('backend observability summary exposes a stable quarantine contract', async ({ page }) => {
    // Navigate to any page first so we can evaluate fetch in browser context
    await page.goto('/');
    await page.waitForURL(/\/(memory|setup)$/);

    // If we land on /setup, complete minimal bootstrap
    if (page.url().endsWith('/setup')) {
      await page.route('**/api/bootstrap/apply', async (route) => {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({
            ok: true,
            summary: 'Bootstrap configuration saved for Profile B',
            effectiveProfile: 'b',
            fallbackApplied: false,
            restartRequired: false,
            restartSupported: true,
            maintenanceApiKey: dashboardApiKey,
            maintenanceApiKeyMode: 'header',
            warnings: [],
            actions: [],
            nextSteps: [],
            setup: { requestedProfile: 'b', effectiveProfile: 'b', requiresOnboarding: false, restartSupported: true },
          }),
        });
      });
      await page.getByRole('button', { name: 'Apply', exact: true }).click({ timeout: 5000 }).catch(() => {});
      await page.goto('/memory');
      await page.waitForURL('**/memory');
    }

    await ensureMaintenanceAuth(page);

    // Directly call the observability summary API and verify quarantine key exists
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

    // Verify quarantine section exists in the response
    expect(summary).toHaveProperty('quarantine');
    const quarantine = summary.quarantine;

    // Verify quarantine has the expected fields
    expect(quarantine).toHaveProperty('total');
    expect(quarantine).toHaveProperty('pending');
    expect(quarantine).toHaveProperty('replayed');
    expect(quarantine).toHaveProperty('expired');
    expect(quarantine).toHaveProperty('dismissed');
    expect(quarantine).toHaveProperty('degraded');

    // Verify types and non-negative counts without assuming a fresh empty DB.
    expectNonNegativeInteger(quarantine.total);
    expectNonNegativeInteger(quarantine.pending);
    expectNonNegativeInteger(quarantine.replayed);
    expectNonNegativeInteger(quarantine.expired);
    expectNonNegativeInteger(quarantine.dismissed);
    expect(typeof quarantine.degraded).toBe('boolean');
  });

  test('observability page loads and displays expected panels', async ({ page }, testInfo) => {
    await page.goto('/');
    await page.waitForURL(/\/(memory|setup)$/);

    // If we land on /setup, complete minimal bootstrap
    if (page.url().endsWith('/setup')) {
      await page.route('**/api/bootstrap/apply', async (route) => {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({
            ok: true,
            summary: 'Bootstrap configuration saved for Profile B',
            effectiveProfile: 'b',
            fallbackApplied: false,
            restartRequired: false,
            restartSupported: true,
            maintenanceApiKey: dashboardApiKey,
            maintenanceApiKeyMode: 'header',
            warnings: [],
            actions: [],
            nextSteps: [],
            setup: { requestedProfile: 'b', effectiveProfile: 'b', requiresOnboarding: false, restartSupported: true },
          }),
        });
      });
      await page.getByRole('button', { name: 'Apply', exact: true }).click({ timeout: 5000 }).catch(() => {});
      await page.goto('/memory');
      await page.waitForURL('**/memory');
    }

    await ensureMaintenanceAuth(page);

    // Navigate to observability
    await page.getByRole('link', { name: 'Observability' }).click();
    await expect(page).toHaveURL(/\/observability$/);

    // Verify main heading
    await expect(
      page.getByRole('heading', { name: 'Retrieval Observability Console', exact: true }),
    ).toBeVisible();

    // Verify key sections present
    await expect(page.getByText('Search Console', { exact: true })).toBeVisible();

    // Take screenshot
    await page.screenshot({
      path: testInfo.outputPath('observability-page-loaded.png'),
      fullPage: true,
    });
  });

  test('observability page tolerates quarantine data in API response', async ({ page }, testInfo) => {
    // Intercept the observability summary API to inject quarantine data
    await page.route('**/api/maintenance/observability/summary', async (route) => {
      // First, get the real response
      let realResponse;
      try {
        realResponse = await route.fetch();
      } catch {
        // If backend is not available, create a mock response
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({
            status: 'degraded',
            timestamp: new Date().toISOString(),
            health: { index: { capabilities: {}, counts: {} }, runtime: {} },
            search_stats: {},
            cleanup_query_stats: {},
            guard_stats: {},
            index_latency: {},
            transport: {},
            quarantine: {
              total: 5,
              pending: 2,
              replayed: 1,
              expired: 1,
              dismissed: 1,
              degraded: true,
            },
          }),
        });
        return;
      }

      const body = await realResponse.json();
      // Inject non-zero quarantine data to simulate quarantined events
      body.quarantine = {
        total: 5,
        pending: 2,
        replayed: 1,
        expired: 1,
        dismissed: 1,
        degraded: true,
      };

      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(body),
      });
    });

    await page.goto('/');
    await page.waitForURL(/\/(memory|setup)$/);

    if (page.url().endsWith('/setup')) {
      await page.route('**/api/bootstrap/apply', async (route) => {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({
            ok: true,
            summary: 'Bootstrap configuration saved for Profile B',
            effectiveProfile: 'b',
            fallbackApplied: false,
            restartRequired: false,
            restartSupported: true,
            maintenanceApiKey: dashboardApiKey,
            maintenanceApiKeyMode: 'header',
            warnings: [],
            actions: [],
            nextSteps: [],
            setup: { requestedProfile: 'b', effectiveProfile: 'b', requiresOnboarding: false, restartSupported: true },
          }),
        });
      });
      await page.getByRole('button', { name: 'Apply', exact: true }).click({ timeout: 5000 }).catch(() => {});
      await page.goto('/memory');
      await page.waitForURL('**/memory');
    }

    await ensureMaintenanceAuth(page);

    // Navigate to observability page
    await page.getByRole('link', { name: 'Observability' }).click();
    await expect(page).toHaveURL(/\/observability$/);

    // Wait for the summary to load (the route intercept will inject quarantine data)
    await expect(
      page.getByRole('heading', { name: 'Retrieval Observability Console', exact: true }),
    ).toBeVisible();

    // Capture the page state to confirm the legacy UI still renders cleanly.
    await page.screenshot({
      path: testInfo.outputPath('observability-quarantine-injected.png'),
      fullPage: true,
    });

    // P3-4 will add dedicated quarantine UI. For now, ensure the extra payload
    // does not cause rendering or runtime failures.
    const consoleLogs: string[] = [];
    page.on('console', (msg) => {
      if (msg.type() === 'error') {
        consoleLogs.push(msg.text());
      }
    });

    // Refresh to re-trigger the intercepted API call
    await page.reload();
    await expect(
      page.getByRole('heading', { name: 'Retrieval Observability Console', exact: true }),
    ).toBeVisible();

    // Verify no JS errors from the quarantine data being in the response
    const jsErrors = consoleLogs.filter(
      (msg) => msg.includes('quarantine') || msg.includes('TypeError') || msg.includes('Cannot read properties'),
    );
    expect(jsErrors).toHaveLength(0);
  });

  test('memory page loads correctly after quarantine feature', async ({ page }, testInfo) => {
    await page.goto('/');
    await page.waitForURL(/\/(memory|setup)$/);

    if (page.url().endsWith('/setup')) {
      await page.route('**/api/bootstrap/apply', async (route) => {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({
            ok: true,
            summary: 'Bootstrap configuration saved for Profile B',
            effectiveProfile: 'b',
            fallbackApplied: false,
            restartRequired: false,
            restartSupported: true,
            maintenanceApiKey: dashboardApiKey,
            maintenanceApiKeyMode: 'header',
            warnings: [],
            actions: [],
            nextSteps: [],
            setup: { requestedProfile: 'b', effectiveProfile: 'b', requiresOnboarding: false, restartSupported: true },
          }),
        });
      });
      await page.getByRole('button', { name: 'Apply', exact: true }).click({ timeout: 5000 }).catch(() => {});
      await page.goto('/memory');
      await page.waitForURL('**/memory');
    }

    await ensureMaintenanceAuth(page);

    // Verify memory page renders correctly
    await expect(page.getByRole('heading', { name: 'Memory Hall', exact: true })).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Conversation Vault', exact: true })).toBeVisible();
    await expect(page.getByTestId('memory-store-button')).toBeVisible();

    await page.screenshot({
      path: testInfo.outputPath('memory-page-after-quarantine.png'),
      fullPage: true,
    });
  });
});

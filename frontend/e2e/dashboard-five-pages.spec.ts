import { expect, test } from '@playwright/test';

const dashboardApiKey =
  process.env.PLAYWRIGHT_E2E_API_KEY || 'playwright-e2e-key';

async function ensureMaintenanceAuth(page) {
  const setApiKeyButton = page.getByTestId('auth-set-api-key');
  if ((await setApiKeyButton.count()) === 0) {
    await expect(page.getByText('Runtime key enabled')).toBeVisible();
    return;
  }

  await setApiKeyButton.click();
  // PromptDialog replaces native window.prompt — fill the password input and confirm.
  const promptInput = page.locator('div[role="dialog"] input[type="password"]');
  await expect(promptInput).toBeVisible();
  await promptInput.fill(dashboardApiKey);
  await page.locator('div[role="dialog"] button[type="submit"]').click();
  await expect(page.getByRole('button', { name: 'Update API key' })).toBeVisible();
}

test('covers setup, memory, review, maintenance, and observability pages', async ({ page }) => {
  let requiresOnboarding = true;
  await page.route('**/api/bootstrap/status', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        ok: true,
        summary: requiresOnboarding ? 'Bootstrap not initialized yet' : 'Bootstrap configuration is ready.',
        setup: {
          requestedProfile: 'b',
          effectiveProfile: 'b',
          requiresOnboarding,
          restartSupported: true,
        },
      }),
    });
  });
  await page.route('**/api/bootstrap/apply', async (route) => {
    requiresOnboarding = false;
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

  await page.goto('/');
  // The app redirects to /memory when onboarded, but to /setup when the
  // backend requires onboarding.  Accept either landing page.
  await page.waitForURL(/\/(memory|setup)$/);

  // If we landed on /setup, complete a minimal bootstrap apply so that
  // OnboardingGate lets us through to the other pages.
  if (page.url().endsWith('/setup')) {
    await page.getByRole('button', { name: 'Apply', exact: true }).click({ timeout: 5000 }).catch(() => {
      // Apply button may not be immediately available on all setup states;
      // fall-through: the test navigates to /memory next anyway.
    });
    await expect
      .poll(() => requiresOnboarding, { timeout: 5000 })
      .toBe(false);
    await page.goto('/memory');
    await page.waitForURL('**/memory');
  }

  await ensureMaintenanceAuth(page);

  await expect(page.getByRole('heading', { name: 'Memory Hall', exact: true })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Conversation Vault', exact: true })).toBeVisible();
  await expect(page.getByTestId('memory-store-button')).toBeVisible();

  await page.getByRole('link', { name: 'Setup' }).click();
  await expect(page).toHaveURL(/\/setup$/);
  await expect(page.getByRole('heading', { name: 'Bootstrap Setup', exact: true })).toBeVisible();
  await expect(page.getByText('Most Users')).toBeVisible();

  await page.getByRole('link', { name: 'Review' }).click();
  await expect(page).toHaveURL(/\/review$/);
  await expect(page.getByText('Review Ledger', { exact: true })).toBeVisible();

  await page.getByRole('link', { name: 'Maintenance' }).click();
  await expect(page).toHaveURL(/\/maintenance$/);
  await expect(page.getByRole('heading', { name: 'Maintenance Console', exact: true })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Orphan Cleanup', exact: true })).toBeVisible();
  await expect(
    page.getByRole('heading', { name: 'Vitality Cleanup Candidates', exact: true }),
  ).toBeVisible();
  await expect(page.getByText(/API key missing or invalid/i)).toHaveCount(0);

  await page.getByRole('link', { name: 'Observability' }).click();
  await expect(page).toHaveURL(/\/observability$/);
  await expect(
    page.getByRole('heading', { name: 'Retrieval Observability Console', exact: true }),
  ).toBeVisible();
  await expect(page.getByText('Search Console', { exact: true })).toBeVisible();

  await page.getByRole('link', { name: 'Memory' }).click();
  await expect(page).toHaveURL(/\/memory$/);
  await expect(page.getByRole('heading', { name: 'Memory Hall', exact: true })).toBeVisible();
});

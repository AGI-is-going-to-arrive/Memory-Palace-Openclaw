import { expect, test } from '@playwright/test';

const gotoSetup = async (page: Parameters<typeof test>[0]['page']) => {
  await page.goto('/setup');
  await page.waitForURL('**/setup');
  await expect(page.getByRole('heading', { name: 'Bootstrap Setup', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Refresh Status', exact: true })).toBeEnabled();
};

const fillInput = async (
  page: Parameters<typeof test>[0]['page'],
  selector: string,
  value: string
) => {
  await page.locator(selector).fill(value);
};

test('setup profile C provider probe and apply+validate work through the browser flow', async ({
  page,
}) => {
  await gotoSetup(page);
  let probePayload: Record<string, unknown> | null = null;
  let applyPayload: Record<string, unknown> | null = null;
  const pageErrors: string[] = [];
  const consoleErrors: string[] = [];
  page.on('pageerror', (error) => pageErrors.push(error.message));
  page.on('console', (message) => {
    if (message.type() === 'error') {
      consoleErrors.push(message.text());
    }
  });

  await page.route('**/api/bootstrap/provider-probe', async (route) => {
    probePayload = route.request().postDataJSON();

    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        ok: true,
        summary: 'Provider checks passed. You can apply Profile C now.',
        providerProbe: {
          requestedProfile: 'c',
          effectiveProfile: 'c',
          probedProfile: 'c',
          requiresProviders: true,
          fallbackApplied: false,
          summaryStatus: 'pass',
          summaryMessage: 'Advanced provider checks passed for the current profile.',
          checkedAt: '2026-04-03T10:00:00Z',
          missingFields: [],
          providers: {
            embedding: {
              configured: true,
              status: 'pass',
              detail: 'Probe passed.',
              baseUrl: 'https://embedding.example/v1',
              model: 'embed-large',
              missingFields: [],
              detectedDim: '1024',
            },
            reranker: {
              configured: true,
              status: 'pass',
              detail: 'Probe passed.',
              baseUrl: 'https://reranker.example/v1',
              model: 'rerank-large',
              missingFields: [],
            },
            llm: {
              configured: true,
              status: 'pass',
              detail: 'Probe passed.',
              baseUrl: 'https://llm.example/v1',
              model: 'gpt-5.4-mini',
              missingFields: [],
            },
          },
        },
      }),
    });
  });

  await page.route('**/api/bootstrap/apply', async (route) => {
    applyPayload = route.request().postDataJSON();

    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        ok: true,
        summary: 'Bootstrap configuration saved for Profile C',
        effectiveProfile: 'c',
        fallbackApplied: false,
        restartRequired: false,
        restartSupported: true,
        maintenanceApiKey: 'setup-session-key-c',
        maintenanceApiKeyMode: 'header',
        warnings: [],
        actions: ['Updated runtime.env'],
        nextSteps: ['Run openclaw memory-palace smoke --json'],
        validation: {
          ok: true,
          failed_step: null,
          steps: [
            { name: 'verify', ok: true, summary: 'verify passed' },
            { name: 'doctor', ok: true, summary: 'doctor completed with warnings.' },
            { name: 'smoke', ok: true, summary: 'smoke completed with warnings.' },
          ],
        },
        setup: {
          requestedProfile: 'c',
          effectiveProfile: 'c',
          requiresOnboarding: false,
          restartSupported: true,
        },
      }),
    });
  });

  await page.getByRole('button', { name: 'Full', exact: true }).click();
  await page.getByRole('button', { name: 'Mode C', exact: true }).click();

  await fillInput(page, 'input[name="mcpApiKey"]', 'setup-session-key-c');
  await fillInput(page, '#embedding-api-base', 'https://embedding.example/v1');
  await fillInput(page, '#embedding-api-key', 'embed-secret');
  await fillInput(page, '#embedding-model', 'embed-large');
  await fillInput(page, '#embedding-dim', '1024');
  await fillInput(page, '#reranker-api-base', 'https://reranker.example/v1');
  await fillInput(page, '#reranker-api-key', 'rerank-secret');
  await fillInput(page, '#reranker-model', 'rerank-large');
  await fillInput(page, '#llm-api-base', 'https://llm.example/v1');
  await fillInput(page, '#llm-api-key', 'llm-secret');
  await fillInput(page, '#llm-model', 'gpt-5.4-mini');

  await page.getByRole('button', { name: 'Re-detect', exact: true }).click();

  await expect.poll(() => probePayload).not.toBeNull();
  expect(probePayload?.profile).toBe('c');
  expect(probePayload?.embeddingApiBase).toBe('https://embedding.example/v1');
  expect(probePayload?.embeddingModel).toBe('embed-large');
  expect(probePayload?.embeddingDim).toBe(1024);
  expect(probePayload?.rerankerApiBase).toBe('https://reranker.example/v1');
  expect(probePayload?.rerankerModel).toBe('rerank-large');
  expect(probePayload?.llmApiBase).toBe('https://llm.example/v1');
  expect(probePayload?.llmModel).toBe('gpt-5.4-mini');
  await expect(page.getByText('Provider checks passed. You can apply Profile C now.')).toBeVisible();
  await expect(page.getByText('Detected embedding dim: 1024')).toBeVisible();

  await page.getByRole('button', { name: 'Apply + Validate', exact: true }).click();

  await expect.poll(() => applyPayload).not.toBeNull();
  expect(applyPayload?.validate).toBe(true);
  expect(applyPayload?.profile).toBe('c');
  expect(applyPayload?.mcpApiKey).toBe('setup-session-key-c');
  expect(applyPayload?.embeddingApiBase).toBe('https://embedding.example/v1');
  expect(applyPayload?.embeddingDim).toBe(1024);
  expect(applyPayload?.rerankerApiBase).toBe('https://reranker.example/v1');
  expect(applyPayload?.llmApiBase).toBe('https://llm.example/v1');
  expect(applyPayload?.reconfigure).toBe(true);
  expect(pageErrors, pageErrors.join('\n')).toEqual([]);
  expect(consoleErrors, consoleErrors.join('\n')).toEqual([]);
  // Auth is now memory-only (not persisted to sessionStorage), so verify
  // that the UI reflects a successful auth state after apply.
  await expect(page.getByRole('button', { name: 'Update API key' })).toBeVisible();
});

test('setup profile D submits dedicated write-guard and compact-gist providers', async ({
  page,
}) => {
  await gotoSetup(page);
  let probePayload: Record<string, unknown> | null = null;
  let applyPayload: Record<string, unknown> | null = null;
  const pageErrors: string[] = [];
  const consoleErrors: string[] = [];
  page.on('pageerror', (error) => pageErrors.push(error.message));
  page.on('console', (message) => {
    if (message.type() === 'error') {
      consoleErrors.push(message.text());
    }
  });

  await page.route('**/api/bootstrap/provider-probe', async (route) => {
    probePayload = route.request().postDataJSON();

    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        ok: true,
        summary: 'Provider checks passed. You can apply Profile D now.',
        providerProbe: {
          requestedProfile: 'd',
          effectiveProfile: 'd',
          probedProfile: 'd',
          requiresProviders: true,
          fallbackApplied: false,
          summaryStatus: 'pass',
          summaryMessage: 'Advanced provider checks passed for the current profile.',
          checkedAt: '2026-04-03T10:05:00Z',
          missingFields: [],
          providers: {
            embedding: {
              configured: true,
              status: 'pass',
              detail: 'Probe passed.',
              baseUrl: 'https://embedding.example/v1',
              model: 'embed-large',
              missingFields: [],
              detectedDim: '1024',
            },
            reranker: {
              configured: true,
              status: 'pass',
              detail: 'Probe passed.',
              baseUrl: 'https://reranker.example/v1',
              model: 'rerank-large',
              missingFields: [],
            },
            llm: {
              configured: true,
              status: 'pass',
              detail: 'Probe passed.',
              baseUrl: 'https://llm.example/v1',
              model: 'gpt-5.4-mini',
              missingFields: [],
            },
          },
        },
      }),
    });
  });

  await page.route('**/api/bootstrap/apply', async (route) => {
    applyPayload = route.request().postDataJSON();

    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        ok: true,
        summary: 'Bootstrap configuration saved for Profile D',
        effectiveProfile: 'd',
        fallbackApplied: false,
        restartRequired: true,
        restartSupported: true,
        maintenanceApiKey: 'setup-session-key-d',
        maintenanceApiKeyMode: 'header',
        warnings: ['Restart the backend to pick up new settings'],
        actions: ['Updated runtime.env'],
        nextSteps: ['Restart backend'],
        setup: {
          requestedProfile: 'd',
          effectiveProfile: 'd',
          requiresOnboarding: false,
          restartSupported: true,
        },
      }),
    });
  });

  await page.getByRole('button', { name: 'Full', exact: true }).click();
  await page.getByRole('button', { name: 'Mode D', exact: true }).click();

  await fillInput(page, 'input[name="mcpApiKey"]', 'setup-session-key-d');
  await fillInput(page, '#embedding-api-base', 'https://embedding.example/v1');
  await fillInput(page, '#embedding-api-key', 'embed-secret');
  await fillInput(page, '#embedding-model', 'embed-large');
  await fillInput(page, '#embedding-dim', '1024');
  await fillInput(page, '#reranker-api-base', 'https://reranker.example/v1');
  await fillInput(page, '#reranker-api-key', 'rerank-secret');
  await fillInput(page, '#reranker-model', 'rerank-large');
  await fillInput(page, '#llm-api-base', 'https://llm.example/v1');
  await fillInput(page, '#llm-api-key', 'llm-secret');
  await fillInput(page, '#llm-model', 'gpt-5.4-mini');
  await fillInput(page, '#write-guard-llm-api-base', 'https://llm.example/v1');
  await fillInput(page, '#write-guard-llm-api-key', 'wg-secret');
  await fillInput(page, '#write-guard-llm-model', 'gpt-5.4-mini');
  await fillInput(page, '#compact-gist-llm-api-base', 'https://llm.example/v1');
  await fillInput(page, '#compact-gist-llm-api-key', 'compact-secret');
  await fillInput(page, '#compact-gist-llm-model', 'gpt-5.4-mini');

  await page.getByRole('button', { name: 'Re-detect', exact: true }).click();

  await expect.poll(() => probePayload).not.toBeNull();
  expect(probePayload?.profile).toBe('d');
  expect(probePayload?.embeddingApiBase).toBe('https://embedding.example/v1');
  expect(probePayload?.rerankerApiBase).toBe('https://reranker.example/v1');
  expect(probePayload?.writeGuardLlmApiBase).toBe('https://llm.example/v1');
  expect(probePayload?.writeGuardLlmModel).toBe('gpt-5.4-mini');
  expect(probePayload?.compactGistLlmApiBase).toBe('https://llm.example/v1');
  expect(probePayload?.compactGistLlmModel).toBe('gpt-5.4-mini');
  await expect(page.getByText('Provider checks passed. You can apply Profile D now.')).toBeVisible();

  await page.getByRole('button', { name: 'Apply', exact: true }).click();

  await expect.poll(() => applyPayload).not.toBeNull();
  expect(applyPayload?.profile).toBe('d');
  expect(applyPayload?.validate).toBe(false);
  expect(applyPayload?.mcpApiKey).toBe('setup-session-key-d');
  expect(applyPayload?.writeGuardLlmApiBase).toBe('https://llm.example/v1');
  expect(applyPayload?.writeGuardLlmModel).toBe('gpt-5.4-mini');
  expect(applyPayload?.compactGistLlmApiBase).toBe('https://llm.example/v1');
  expect(applyPayload?.compactGistLlmModel).toBe('gpt-5.4-mini');
  expect(pageErrors, pageErrors.join('\n')).toEqual([]);
  expect(consoleErrors, consoleErrors.join('\n')).toEqual([]);
  // Auth is now memory-only (not persisted to sessionStorage), so verify
  // that the UI reflects a successful auth state after apply.
  await expect(page.getByRole('button', { name: 'Update API key' })).toBeVisible();
});

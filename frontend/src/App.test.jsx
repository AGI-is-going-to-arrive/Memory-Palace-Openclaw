import React from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import userEvent from '@testing-library/user-event';
import { render, screen, waitFor } from '@testing-library/react';
import { clearStoredMaintenanceAuth } from './lib/api';

const { memoryMountCounter, bootstrapApi, setupApi } = vi.hoisted(() => ({
  memoryMountCounter: { current: 0 },
  bootstrapApi: {
    getBootstrapStatus: vi.fn(),
  },
  setupApi: {
    applyBootstrapConfiguration: vi.fn(),
    requestBootstrapRestart: vi.fn(),
  },
}));

vi.mock('./lib/api', async () => {
  const actual = await vi.importActual('./lib/api');
  return {
    ...actual,
    getBootstrapStatus: bootstrapApi.getBootstrapStatus,
    applyBootstrapConfiguration: setupApi.applyBootstrapConfiguration,
    requestBootstrapRestart: setupApi.requestBootstrapRestart,
  };
});

import App, { buildRoutesKey } from './App';
import i18n, { LOCALE_STORAGE_KEY } from './i18n';

vi.mock('./features/memory/MemoryBrowser', () => ({
  default: () => {
    React.useEffect(() => {
      memoryMountCounter.current += 1;
    }, []);
    return <div>memory-page</div>;
  },
}));

vi.mock('./features/review/ReviewPage', () => ({
  default: () => <div>review-page</div>,
}));

vi.mock('./features/maintenance/MaintenancePage', () => ({
  default: () => <div>maintenance-page</div>,
}));

vi.mock('./features/observability/ObservabilityPage', () => ({
  default: () => <div>observability-page</div>,
}));

const createBootstrapStatus = (overrides = {}) => ({
  ok: true,
  summary: 'Bootstrap ready',
  setup: {
    requiresOnboarding: false,
    restartRequired: false,
    envFile: '.env',
    configPath: 'config/bootstrap.json',
    mode: 'basic',
    requestedProfile: 'a',
    effectiveProfile: 'a',
    transport: 'stdio',
    mcpApiKeyConfigured: false,
    embeddingConfigured: false,
    rerankerConfigured: false,
    llmConfigured: false,
    frontendAvailable: true,
    warnings: [],
    ...overrides.setup,
  },
  profileOptions: ['a', 'b', 'c', 'd'],
  modeOptions: ['basic', 'full', 'dev'],
  transportOptions: ['stdio', 'sse'],
  ...overrides,
});

describe('App routing', () => {
  beforeEach(async () => {
    memoryMountCounter.current = 0;
    bootstrapApi.getBootstrapStatus.mockReset();
    bootstrapApi.getBootstrapStatus.mockResolvedValue(createBootstrapStatus());
    setupApi.applyBootstrapConfiguration.mockReset();
    setupApi.applyBootstrapConfiguration.mockResolvedValue({
      ok: true,
      summary: 'Bootstrap configuration saved',
      effectiveProfile: 'a',
      fallbackApplied: false,
      restartRequired: false,
      restartSupported: true,
      warnings: [],
      actions: [],
      nextSteps: [],
    });
    setupApi.requestBootstrapRestart.mockReset();
    setupApi.requestBootstrapRestart.mockResolvedValue({
      ok: true,
      restartAccepted: true,
      message: 'Local backend restart scheduled.',
      restartSupported: true,
    });
    clearStoredMaintenanceAuth();
    window.sessionStorage?.removeItem?.('memory-palace.dashboardAuth');
    window.localStorage?.removeItem?.('memory-palace.dashboardAuth');
    window.localStorage?.removeItem?.(LOCALE_STORAGE_KEY);
    delete window.__MEMORY_PALACE_RUNTIME__;
    await i18n.changeLanguage('en');
    window.localStorage?.removeItem?.(LOCALE_STORAGE_KEY);
  });

  afterEach(() => {
    window.history.pushState({}, '', '/');
    vi.restoreAllMocks();
  });

  it('redirects root path to memory', async () => {
    window.history.pushState({}, '', '/');

    render(<App />);

    expect(await screen.findByText('memory-page')).toBeInTheDocument();
    await waitFor(() => expect(window.location.pathname).toBe('/memory'));
  });

  it('redirects root path to setup when onboarding is required', async () => {
    bootstrapApi.getBootstrapStatus.mockResolvedValue(
      createBootstrapStatus({
        summary: 'Onboarding required',
        setup: {
          requiresOnboarding: true,
        },
      })
    );
    window.history.pushState({}, '', '/');

    render(<App />);

    expect(await screen.findByText('Bootstrap Setup')).toBeInTheDocument();
    await waitFor(() => expect(window.location.pathname).toBe('/setup'));
  });

  it('redirects protected deep links to setup when onboarding is required', async () => {
    bootstrapApi.getBootstrapStatus.mockResolvedValue(
      createBootstrapStatus({
        summary: 'Onboarding required',
        setup: {
          requiresOnboarding: true,
        },
      })
    );
    window.history.pushState({}, '', '/review');

    render(<App />);

    expect(await screen.findByText('Bootstrap Setup')).toBeInTheDocument();
    expect(screen.queryByText('review-page')).not.toBeInTheDocument();
    await waitFor(() => expect(window.location.pathname).toBe('/setup'));
  });

  it('keeps protected nav actions pinned to setup when onboarding is required', async () => {
    const user = userEvent.setup();
    bootstrapApi.getBootstrapStatus.mockResolvedValue(
      createBootstrapStatus({
        summary: 'Onboarding required',
        setup: {
          requiresOnboarding: true,
        },
      })
    );
    window.history.pushState({}, '', '/setup');

    render(<App />);

    expect(await screen.findByText('Bootstrap Setup')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: i18n.t('app.nav.maintenance') }));

    expect(screen.queryByText('maintenance-page')).not.toBeInTheDocument();
    expect(screen.getByText('Bootstrap Setup')).toBeInTheDocument();
    await waitFor(() => expect(window.location.pathname).toBe('/setup'));
  });

  it('fails closed to setup when bootstrap status cannot be loaded', async () => {
    bootstrapApi.getBootstrapStatus.mockRejectedValueOnce(new Error('bootstrap unavailable'));
    window.history.pushState({}, '', '/review');

    render(<App />);

    expect(await screen.findByText('Bootstrap Setup')).toBeInTheDocument();
    expect(screen.queryByText('review-page')).not.toBeInTheDocument();
    await waitFor(() => expect(window.location.pathname).toBe('/setup'));
  });

  it('redirects unknown paths to memory', async () => {
    window.history.pushState({}, '', '/unknown-route');

    render(<App />);

    expect(await screen.findByText('memory-page')).toBeInTheDocument();
    await waitFor(() => expect(window.location.pathname).toBe('/memory'));
  });

  it('stores API key through header action when runtime config is absent', async () => {
    const user = userEvent.setup();
    window.history.pushState({}, '', '/memory');

    render(<App />);

    await user.click(screen.getByRole('button', { name: i18n.t('app.auth.setApiKey') }));

    const dialog = await screen.findByRole('dialog');
    const input = dialog.querySelector('input');
    await user.clear(input);
    await user.type(input, 'stored-key');
    await user.click(screen.getByRole('button', { name: 'OK' }));

    expect(window.sessionStorage.getItem('memory-palace.dashboardAuth')).toBeNull();
    expect(window.localStorage.getItem('memory-palace.dashboardAuth')).toBeNull();
    expect(await screen.findByRole('button', { name: i18n.t('app.auth.updateApiKey') })).toBeInTheDocument();
  });

  it('remounts routes after stored auth changes without depending on raw key text', async () => {
    const user = userEvent.setup();
    window.history.pushState({}, '', '/memory');

    render(<App />);
    expect(await screen.findByText('memory-page')).toBeInTheDocument();
    await waitFor(() => expect(memoryMountCounter.current).toBe(1));

    await user.click(screen.getByRole('button', { name: i18n.t('app.auth.setApiKey') }));

    const dialog = await screen.findByRole('dialog');
    const input = dialog.querySelector('input');
    await user.clear(input);
    await user.type(input, 'stored-key');
    await user.click(screen.getByRole('button', { name: 'OK' }));

    await waitFor(() => expect(memoryMountCounter.current).toBe(2));
  });

  it('defaults to english when no stored locale exists', async () => {
    window.history.pushState({}, '', '/memory');

    render(<App />);

    expect(await screen.findByRole('button', { name: 'Set API key' })).toBeInTheDocument();
    expect(document.documentElement.lang).toBe('en');
  });

  it('shows runtime status badge when runtime config is present', async () => {
    window.history.pushState({}, '', '/memory');
    window.__MEMORY_PALACE_RUNTIME__ = {
      maintenanceApiKey: 'runtime-key',
      maintenanceApiKeyMode: 'header',
    };

    render(<App />);

    expect(await screen.findByText(i18n.t('app.auth.runtimeBadge'))).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: i18n.t('app.auth.setApiKey') })).not.toBeInTheDocument();
  });

  it('toggles language and persists the selection across remounts', async () => {
    const user = userEvent.setup();
    window.history.pushState({}, '', '/memory');

    const firstRender = render(<App />);

    expect(await screen.findByRole('button', { name: 'Set API key' })).toBeInTheDocument();
    await user.click(screen.getByTestId('language-toggle'));

    expect(await screen.findByRole('button', { name: '设置 API 密钥' })).toBeInTheDocument();
    expect(window.localStorage.getItem(LOCALE_STORAGE_KEY)).toBe('zh-CN');

    firstRender.unmount();

    render(<App />);

    expect(await screen.findByRole('button', { name: '设置 API 密钥' })).toBeInTheDocument();
  });

  it('updates app auth controls immediately when setup provisions a maintenance key', async () => {
    const user = userEvent.setup();
    setupApi.applyBootstrapConfiguration.mockResolvedValue({
      ok: true,
      summary: 'Bootstrap configuration saved',
      effectiveProfile: 'a',
      fallbackApplied: false,
      restartRequired: false,
      restartSupported: true,
      maintenanceApiKey: 'setu************',
      maintenanceApiKeySet: true,
      maintenanceApiKeyMode: 'header',
      warnings: [],
      actions: [],
      nextSteps: [],
    });
    window.history.pushState({}, '', '/setup');

    render(<App />);

    expect(await screen.findByRole('button', { name: 'Set API key' })).toBeInTheDocument();

    // Enter mcpApiKey in setup form before applying
    const mcpKeyInput = document.querySelector('input[name="mcpApiKey"]');
    if (mcpKeyInput) {
      await user.clear(mcpKeyInput);
      await user.type(mcpKeyInput, 'setup-session-key');
    }
    await user.click(screen.getByRole('button', { name: 'Apply' }));

    // Auth is now in-memory only — sessionStorage must be empty
    expect(window.sessionStorage.getItem('memory-palace.dashboardAuth')).toBeNull();
    if (mcpKeyInput) {
      expect(await screen.findByRole('button', { name: 'Update API key' })).toBeInTheDocument();
    }
  });

  it('keeps prompting for a key when setup only confirms a masked backend key', async () => {
    const user = userEvent.setup();
    setupApi.applyBootstrapConfiguration.mockResolvedValue({
      ok: true,
      summary: 'Bootstrap configuration saved',
      effectiveProfile: 'a',
      fallbackApplied: false,
      restartRequired: false,
      restartSupported: true,
      maintenanceApiKey: 'boot************',
      maintenanceApiKeySet: true,
      maintenanceApiKeyMode: 'header',
      warnings: [],
      actions: [],
      nextSteps: [],
    });
    window.history.pushState({}, '', '/setup');

    render(<App />);

    expect(await screen.findByRole('button', { name: 'Set API key' })).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Apply' }));

    expect(await screen.findByRole('button', { name: 'Set API key' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Update API key' })).not.toBeInTheDocument();
  });

  it('does not embed the raw api key in the routes key', () => {
    const routesKey = buildRoutesKey(
      {
        source: 'stored',
        mode: 'header',
        key: 'super-secret-key',
      },
      3
    );

    expect(routesKey).toBe('stored:header:3');
    expect(routesKey).not.toContain('super-secret-key');
    expect(buildRoutesKey(null, 4)).toBe('no-auth:4');
  });
});

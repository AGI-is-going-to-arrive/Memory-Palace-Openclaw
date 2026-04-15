import React from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import userEvent from '@testing-library/user-event';
import { render, screen, waitFor } from '@testing-library/react';

import i18n from '../../i18n';
import SetupPage from './SetupPage';
import {
  applyBootstrapConfiguration,
  clearStoredMaintenanceAuth,
  getMaintenanceAuthState,
  probeBootstrapProviders,
  requestBootstrapRestart,
} from '../../lib/api';

vi.mock('../../lib/api', async () => {
  const actual = await vi.importActual('../../lib/api');
  return {
    ...actual,
    applyBootstrapConfiguration: vi.fn(),
    probeBootstrapProviders: vi.fn(),
    requestBootstrapRestart: vi.fn(),
  };
});

const createBootstrapStatus = (overrides = {}) => ({
  ok: true,
  summary: 'Onboarding required',
  setup: {
    requiresOnboarding: true,
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
    warnings: ['Missing bootstrap profile'],
    ...overrides.setup,
  },
  checks: [
    {
      id: 'bundled-skill',
      status: 'PASS',
      message: 'Plugin-bundled OpenClaw skill is present.',
    },
    {
      id: 'openclaw-version',
      status: 'WARN',
      message: 'Detected OpenClaw 2026.3.1; hook-capable host requirement is >= 2026.3.2.',
      details: {
        detected: '2026.3.1',
        required: '2026.3.2',
      },
    },
  ],
  profileOptions: ['a', 'b', 'c', 'd'],
  modeOptions: ['basic', 'full', 'dev'],
  transportOptions: ['stdio', 'sse'],
  ...overrides,
});

describe('SetupPage', () => {
  beforeEach(async () => {
    await i18n.changeLanguage('en');
    applyBootstrapConfiguration.mockReset();
    probeBootstrapProviders.mockReset();
    requestBootstrapRestart.mockReset();
    clearStoredMaintenanceAuth();
    window.sessionStorage?.removeItem?.('memory-palace.dashboardAuth');
  });

  it('defaults to profile B and highlights the advanced path recommendation when bootstrap status is unavailable', () => {
    render(
      <SetupPage
        bootstrapStatus={null}
        statusLoading={false}
        statusError={null}
        onRefreshStatus={vi.fn()}
      />
    );

    expect(screen.getByRole('button', { name: 'Mode B' })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getAllByText('Profile B baseline').length).toBeGreaterThan(0);
    expect(screen.getAllByText('Strongly recommended').length).toBeGreaterThan(0);
  });

  it('submits bootstrap configuration and renders the result summary', async () => {
    const user = userEvent.setup();
    const onRefreshStatus = vi.fn().mockResolvedValue(createBootstrapStatus());

    applyBootstrapConfiguration.mockResolvedValue({
      ok: true,
      summary: 'Bootstrap configuration saved',
      effectiveProfile: 'dev',
      fallbackApplied: false,
      restartRequired: true,
      restartSupported: true,
      maintenanceApiKey: 'setu********',
      maintenanceApiKeySet: true,
      maintenanceApiKeyMode: 'header',
      warnings: ['Restart the backend to pick up new settings'],
      actions: ['Updated .env'],
      nextSteps: ['Restart backend'],
      setup: {
        requiresOnboarding: false,
        restartSupported: true,
      },
    });
    requestBootstrapRestart.mockResolvedValue({
      ok: true,
      restartAccepted: true,
      message: 'Local backend restart scheduled.',
      restartSupported: true,
    });

    render(
      <SetupPage
        bootstrapStatus={createBootstrapStatus()}
        statusLoading={false}
        statusError={null}
        onRefreshStatus={onRefreshStatus}
      />
    );

    await user.click(screen.getByRole('button', { name: 'Full' }));
    await user.click(screen.getByRole('button', { name: 'Mode C' }));
    await user.click(screen.getByRole('button', { name: 'sse' }));
    await user.type(screen.getByLabelText('Database Path'), '/tmp/memory-palace.db');
    await user.type(screen.getByLabelText('SSE URL'), 'http://127.0.0.1:8765/sse');
    await user.type(screen.getByLabelText('MCP API Key'), 'setup-secret');
    await user.type(screen.getByLabelText('Embedding Dim'), '1536');
    await user.click(screen.getByLabelText('Allow insecure local loopback transport'));
    await user.click(screen.getByRole('button', { name: 'Apply' }));

    await waitFor(() => {
      expect(applyBootstrapConfiguration).toHaveBeenCalledWith(
        expect.objectContaining({
          profile: 'c',
          mode: 'full',
          transport: 'sse',
          reconfigure: true,
          databasePath: '/tmp/memory-palace.db',
          sseUrl: 'http://127.0.0.1:8765/sse',
          mcpApiKey: 'setup-secret',
          allowInsecureLocal: true,
          embeddingDim: 1536,
        })
      );
    });

    expect(await screen.findByText('Bootstrap configuration saved')).toBeInTheDocument();
    expect(screen.getByText('Updated .env')).toBeInTheDocument();
    expect(screen.getByText('Restart backend')).toBeInTheDocument();
    expect(onRefreshStatus).toHaveBeenCalledTimes(1);
    expect(getMaintenanceAuthState()).toEqual({
      key: 'setup-secret',
      mode: 'header',
      source: 'stored',
    });
    // Auth stored in-memory only — sessionStorage must be empty
    expect(window.sessionStorage.getItem('memory-palace.dashboardAuth')).toBeNull();

    await user.click(screen.getByRole('button', { name: 'Restart Backend' }));

    await waitFor(() => {
      expect(requestBootstrapRestart).toHaveBeenCalledTimes(1);
    });
    expect(await screen.findByText('Local backend restart scheduled.')).toBeInTheDocument();
  });

  it('can apply and validate in one action', async () => {
    const user = userEvent.setup();

    applyBootstrapConfiguration.mockResolvedValue({
      ok: true,
      summary: 'Bootstrap configuration saved',
      effectiveProfile: 'b',
      fallbackApplied: false,
      restartRequired: false,
      warnings: [],
      actions: [],
      nextSteps: [],
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
        requiresOnboarding: false,
      },
    });

    render(
      <SetupPage
        bootstrapStatus={createBootstrapStatus()}
        statusLoading={false}
        statusError={null}
        onRefreshStatus={vi.fn()}
      />
    );

    await user.click(screen.getByRole('button', { name: 'Apply + Validate' }));

    await waitFor(() => {
      expect(applyBootstrapConfiguration).toHaveBeenCalledWith(
        expect.objectContaining({
          validate: true,
        })
      );
    });

    expect(await screen.findByText('Validation Chain')).toBeInTheDocument();
    expect(screen.getByText('verify passed')).toBeInTheDocument();
    expect(screen.getByText('doctor completed with warnings.')).toBeInTheDocument();
    expect(screen.getByText('smoke completed with warnings.')).toBeInTheDocument();
  });

  it('clears secret provider inputs after a successful apply even when bootstrap status refreshes', async () => {
    const user = userEvent.setup();
    const onRefreshStatus = vi.fn().mockResolvedValue(
      createBootstrapStatus({
        setup: {
          requestedProfile: 'd',
          effectiveProfile: 'd',
          requiresOnboarding: false,
        },
      })
    );

    applyBootstrapConfiguration.mockResolvedValue({
      ok: true,
      summary: 'Bootstrap configuration saved',
      effectiveProfile: 'd',
      fallbackApplied: false,
      restartRequired: false,
      warnings: [],
      actions: [],
      nextSteps: [],
      maintenanceApiKey: 'setu********',
      maintenanceApiKeySet: true,
      maintenanceApiKeyMode: 'header',
      setup: {
        requestedProfile: 'd',
        effectiveProfile: 'd',
        requiresOnboarding: false,
      },
    });

    render(
      <SetupPage
        bootstrapStatus={createBootstrapStatus()}
        statusLoading={false}
        statusError={null}
        onRefreshStatus={onRefreshStatus}
      />
    );

    await user.click(screen.getByRole('button', { name: 'Mode D' }));
    await user.type(screen.getByLabelText('MCP API Key'), 'setup-secret');
    await user.type(screen.getAllByLabelText('API Key')[0], 'embed-secret');
    await user.type(screen.getAllByLabelText('API Key')[1], 'rerank-secret');
    await user.type(screen.getAllByLabelText('API Key')[2], 'llm-secret');
    await user.type(screen.getAllByLabelText('API Key')[3], 'write-guard-secret');
    await user.type(screen.getAllByLabelText('API Key')[4], 'compact-secret');

    await user.click(screen.getByRole('button', { name: 'Apply' }));

    await waitFor(() => {
      expect(applyBootstrapConfiguration).toHaveBeenCalledTimes(1);
      expect(onRefreshStatus).toHaveBeenCalledTimes(1);
    });

    await waitFor(() => {
      expect(screen.getByLabelText('MCP API Key')).toHaveValue('');
      expect(screen.getAllByLabelText('API Key')[0]).toHaveValue('');
      expect(screen.getAllByLabelText('API Key')[1]).toHaveValue('');
      expect(screen.getAllByLabelText('API Key')[2]).toHaveValue('');
      expect(screen.getAllByLabelText('API Key')[3]).toHaveValue('');
      expect(screen.getAllByLabelText('API Key')[4]).toHaveValue('');
    });
  });

  it('shows manual auth guidance when bootstrap only returns a masked maintenance key', async () => {
    const user = userEvent.setup();

    applyBootstrapConfiguration.mockResolvedValue({
      ok: true,
      summary: 'Bootstrap configuration saved',
      effectiveProfile: 'b',
      fallbackApplied: false,
      restartRequired: false,
      restartSupported: true,
      maintenanceApiKey: 'boot************',
      maintenanceApiKeySet: true,
      maintenanceApiKeyMode: 'header',
      warnings: [],
      actions: [],
      nextSteps: [],
      setup: {
        requiresOnboarding: false,
      },
    });

    render(
      <SetupPage
        bootstrapStatus={createBootstrapStatus()}
        statusLoading={false}
        statusError={null}
        onRefreshStatus={vi.fn()}
      />
    );

    await user.click(screen.getByRole('button', { name: 'Apply' }));

    expect(await screen.findByText('Bootstrap configuration saved')).toBeInTheDocument();
    expect(getMaintenanceAuthState()).toBeNull();
    expect(
      screen.getByText(
        'The backend now requires MCP API authentication, but the dashboard only received a masked key and cannot reuse it automatically.'
      )
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        'Enter the exact MCP API key in this form before applying again, or click "Set API key" in the top-right corner to unlock protected pages for this browser session.'
      )
    ).toBeInTheDocument();
  });

  it('resyncs status-driven fields from refreshed bootstrap status without wiping manual inputs', async () => {
    const user = userEvent.setup();
    const { rerender } = render(
      <SetupPage
        bootstrapStatus={createBootstrapStatus()}
        statusLoading={false}
        statusError={null}
        onRefreshStatus={vi.fn()}
      />
    );

    await user.type(screen.getByLabelText('Database Path'), '/tmp/custom.db');

    rerender(
      <SetupPage
        bootstrapStatus={createBootstrapStatus({
          setup: {
            requiresOnboarding: false,
            mode: 'full',
            effectiveProfile: 'd',
            transport: 'sse',
          },
        })}
        statusLoading={false}
        statusError={null}
        onRefreshStatus={vi.fn()}
      />
    );

    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Full' })).toHaveAttribute('aria-pressed', 'true');
      expect(screen.getByRole('button', { name: 'Mode D' })).toHaveAttribute('aria-pressed', 'true');
      expect(screen.getByRole('button', { name: 'sse' })).toHaveAttribute('aria-pressed', 'true');
      expect(screen.getByLabelText('Force reconfigure on apply')).not.toBeChecked();
      expect(screen.getByLabelText('Database Path')).toHaveValue('/tmp/custom.db');
    });
  });

  it('does not let a late bootstrap refresh overwrite a manual C/D path selection', async () => {
    const user = userEvent.setup();
    const { rerender } = render(
      <SetupPage
        bootstrapStatus={createBootstrapStatus()}
        statusLoading={false}
        statusError={null}
        onRefreshStatus={vi.fn()}
      />
    );

    await user.click(screen.getByRole('button', { name: 'Full' }));
    await user.click(screen.getByRole('button', { name: 'Mode C' }));

    rerender(
      <SetupPage
        bootstrapStatus={createBootstrapStatus({
          setup: {
            requiresOnboarding: true,
            mode: 'basic',
            requestedProfile: 'b',
            effectiveProfile: 'b',
            transport: 'stdio',
          },
        })}
        statusLoading={false}
        statusError={null}
        onRefreshStatus={vi.fn()}
      />
    );

    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Full' })).toHaveAttribute('aria-pressed', 'true');
      expect(screen.getByRole('button', { name: 'Mode C' })).toHaveAttribute('aria-pressed', 'true');
      expect(screen.getByRole('button', { name: 'Re-detect' })).toBeEnabled();
    });
  });

  it('applies a guided preset to the setup form', async () => {
    const user = userEvent.setup();

    render(
      <SetupPage
        bootstrapStatus={createBootstrapStatus()}
        statusLoading={false}
        statusError={null}
        onRefreshStatus={vi.fn()}
      />
    );

    await user.click(screen.getByRole('button', { name: /Local Dashboard/i }));

    expect(screen.getByRole('button', { name: 'Full' })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByRole('button', { name: 'Mode B' })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByRole('button', { name: 'stdio' })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByLabelText('Force reconfigure on apply')).toBeChecked();
  });

  it('renders guided strategy copy without leaking translation keys', () => {
    render(
      <SetupPage
        bootstrapStatus={createBootstrapStatus({
          summary: 'Bootstrap configuration is ready.',
          setup: {
            requiresOnboarding: false,
            requestedProfile: 'b',
            effectiveProfile: 'b',
          },
        })}
        statusLoading={false}
        statusError={null}
        onRefreshStatus={vi.fn()}
      />
    );

    expect(screen.getAllByText('Path Strategy').length).toBeGreaterThan(0);
    expect(screen.getAllByText('Profile B baseline').length).toBeGreaterThan(0);
    expect(screen.getAllByText('Profile C / D target').length).toBeGreaterThan(0);
    expect(screen.queryByText(/setup\.guided\./i)).not.toBeInTheDocument();
  });

  it('does not submit hidden advanced profile fields after switching back to a basic profile', async () => {
    const user = userEvent.setup();

    applyBootstrapConfiguration.mockResolvedValue({
      ok: true,
      summary: 'Bootstrap configuration saved',
      effectiveProfile: 'b',
      fallbackApplied: false,
      restartRequired: false,
      warnings: [],
      actions: [],
      nextSteps: [],
      setup: {
        requiresOnboarding: false,
      },
    });

    render(
      <SetupPage
        bootstrapStatus={createBootstrapStatus()}
        statusLoading={false}
        statusError={null}
        onRefreshStatus={vi.fn()}
      />
    );

    await user.click(screen.getByRole('button', { name: 'Mode C' }));
    await user.type(screen.getAllByLabelText('API Base')[0], 'https://embedding.example/v1');
    await user.type(screen.getAllByLabelText('Model')[0], 'embed-large');
    await user.click(screen.getByRole('button', { name: 'Mode B' }));
    await user.click(screen.getByRole('button', { name: 'Apply' }));

    await waitFor(() => {
      expect(applyBootstrapConfiguration).toHaveBeenCalledTimes(1);
    });
    expect(applyBootstrapConfiguration).toHaveBeenCalledWith(
      expect.not.objectContaining({
        embeddingApiBase: 'https://embedding.example/v1',
        embeddingModel: 'embed-large',
      })
    );
  });

  it('rejects invalid embedding dimensions before submitting', async () => {
    const user = userEvent.setup();

    render(
      <SetupPage
        bootstrapStatus={createBootstrapStatus({
          setup: {
            requestedProfile: 'c',
            effectiveProfile: 'c',
          },
        })}
        statusLoading={false}
        statusError={null}
        onRefreshStatus={vi.fn()}
      />
    );

    await user.clear(screen.getByLabelText('Embedding Dim'));
    await user.type(screen.getByLabelText('Embedding Dim'), 'abc');
    await user.click(screen.getByRole('button', { name: 'Apply' }));

    expect(applyBootstrapConfiguration).not.toHaveBeenCalled();
    expect(await screen.findByText('Embedding Dim must be a positive integer.')).toBeInTheDocument();
  });

  it('localizes installer warnings in the english UI', () => {
    render(
      <SetupPage
        bootstrapStatus={createBootstrapStatus({
          summary: 'Bootstrap configuration is ready.',
          setup: {
            warnings: [
              '当前运行中的后端环境与刚写入的 bootstrap 配置不一致；需要重启相关进程后才会完全按新配置运行。',
            ],
          },
        })}
        statusLoading={false}
        statusError={null}
        onRefreshStatus={vi.fn()}
      />
    );

    expect(
      screen.getByText(
        'The running backend environment does not yet match the latest bootstrap configuration. Restart the relevant processes to fully apply the new settings.'
      )
    ).toBeInTheDocument();
    expect(
      screen.queryByText(
        '当前运行中的后端环境与刚写入的 bootstrap 配置不一致；需要重启相关进程后才会完全按新配置运行。'
      )
    ).not.toBeInTheDocument();
  });

  it('localizes setup summary and restart result in the chinese UI', async () => {
    await i18n.changeLanguage('zh-CN');
    const user = userEvent.setup();

    requestBootstrapRestart.mockResolvedValue({
      ok: true,
      restartAccepted: true,
      message: 'Local backend restart scheduled.',
      restartSupported: true,
    });

    render(
      <SetupPage
        bootstrapStatus={createBootstrapStatus({
          summary: 'Bootstrap configuration is ready.',
          setup: {
            restartRequired: true,
            restartSupported: true,
            warnings: [],
          },
        })}
        statusLoading={false}
        statusError={null}
        onRefreshStatus={vi.fn()}
      />
    );

    expect(screen.getByText('Bootstrap 配置已就绪。')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: '重启 Backend' }));

    expect(await screen.findByText('本地 backend 重启已加入队列。')).toBeInTheDocument();
  });

  it('renders the revised path strategy copy in chinese', async () => {
    await i18n.changeLanguage('zh-CN');

    render(
      <SetupPage
        bootstrapStatus={null}
        statusLoading={false}
        statusError={null}
        onRefreshStatus={vi.fn()}
      />
    );

    expect(screen.getAllByText('路径策略').length).toBeGreaterThan(0);
    expect(screen.getAllByText('Profile B 基线').length).toBeGreaterThan(0);
    expect(screen.getAllByText('强烈推荐').length).toBeGreaterThan(0);
  });

  it('renders preflight checks ahead of the apply form', () => {
    render(
      <SetupPage
        bootstrapStatus={createBootstrapStatus({
          checks: [
            {
              id: 'provider-profile',
              status: 'WARN',
              message: 'Requested Profile C fell back to Profile B after provider checks.',
              details: 'requested=c | effective=b | checked_at=2026-03-25T00:00:00Z',
              action:
                'Finish the missing provider fields or rerun setup after fixing provider connectivity.',
            },
            {
              id: 'provider-embedding',
              status: 'WARN',
              message: 'Embedding provider probe failed.',
              details: 'base=https://embedding.example/v1 | model=embed-large | HTTP 401',
            },
          ],
        })}
        statusLoading={false}
        statusError={null}
        onRefreshStatus={vi.fn()}
      />
    );

    expect(screen.getByText('Preflight Checks')).toBeInTheDocument();
    expect(screen.getAllByText('Provider Readiness').length).toBeGreaterThan(0);
    expect(screen.getByText('Embedding Provider')).toBeInTheDocument();
    expect(screen.getByText('Requested Profile C fell back to Profile B after provider checks.')).toBeInTheDocument();
    expect(screen.getByText('Embedding provider probe failed.')).toBeInTheDocument();
    expect(screen.getByText('base=https://embedding.example/v1 | model=embed-large | HTTP 401')).toBeInTheDocument();
    expect(
      screen.getByText(
        'Finish the missing provider fields or rerun setup after fixing provider connectivity.'
      )
    ).toBeInTheDocument();
  });

  it('re-detects provider readiness from the current form values', async () => {
    const user = userEvent.setup();
    const onRefreshStatus = vi.fn().mockResolvedValue(createBootstrapStatus());

    probeBootstrapProviders.mockResolvedValue({
      ok: true,
      providerProbe: {
        requestedProfile: 'c',
        effectiveProfile: 'c',
        probedProfile: 'c',
        requiresProviders: true,
        fallbackApplied: false,
        summaryStatus: 'pass',
        summaryMessage: 'Advanced provider checks passed for the current profile.',
        checkedAt: '2026-03-25T10:00:00Z',
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
    });

    render(
      <SetupPage
        bootstrapStatus={createBootstrapStatus({
          setup: {
            requestedProfile: 'c',
            effectiveProfile: 'c',
          },
        })}
        statusLoading={false}
        statusError={null}
        onRefreshStatus={onRefreshStatus}
      />
    );

    await user.click(screen.getByRole('button', { name: 'Mode C' }));
    await user.type(screen.getAllByLabelText('API Base')[0], 'https://embedding.example/v1');
    await user.type(screen.getAllByLabelText('Model')[0], 'embed-large');
    await user.type(screen.getByLabelText('Embedding Dim'), '1024');
    await user.type(screen.getAllByLabelText('API Base')[1], 'https://reranker.example/v1');
    await user.type(screen.getAllByLabelText('Model')[1], 'rerank-large');
    await user.type(screen.getAllByLabelText('API Base')[2], 'https://llm.example/v1');
    await user.type(screen.getAllByLabelText('Model')[2], 'gpt-5.4-mini');
    await user.click(screen.getByRole('button', { name: 'Re-detect' }));

    await waitFor(() => {
      expect(probeBootstrapProviders).toHaveBeenCalledWith(
        expect.objectContaining({
          profile: 'c',
          embeddingApiBase: 'https://embedding.example/v1',
          embeddingModel: 'embed-large',
          embeddingDim: 1024,
          rerankerApiBase: 'https://reranker.example/v1',
          rerankerModel: 'rerank-large',
          llmApiBase: 'https://llm.example/v1',
          llmModel: 'gpt-5.4-mini',
        })
      );
    });
    expect(onRefreshStatus).toHaveBeenCalledTimes(1);

    expect(await screen.findByText('Provider checks passed. You can apply Profile C now.')).toBeInTheDocument();
    expect(screen.getByText('Detected embedding dim: 1024')).toBeInTheDocument();
    expect(screen.getByTestId('guided-step-providers')).toHaveAttribute('data-state', 'done');
  });

  it('keeps the provider step pending when the current profile fell back after probe', async () => {
    const user = userEvent.setup();

    probeBootstrapProviders.mockResolvedValue({
      ok: true,
      providerProbe: {
        requestedProfile: 'c',
        effectiveProfile: 'b',
        probedProfile: 'c',
        requiresProviders: true,
        fallbackApplied: true,
        summaryStatus: 'warn',
        summaryMessage: 'Requested Profile C fell back to Profile B after provider checks.',
        checkedAt: '2026-03-25T10:00:00Z',
        missingFields: ['RETRIEVAL_EMBEDDING_API_BASE'],
        providers: {
          embedding: {
            configured: false,
            status: 'missing',
            detail: 'Missing base URL.',
            missingFields: ['RETRIEVAL_EMBEDDING_API_BASE'],
          },
        },
      },
    });

    render(
      <SetupPage
        bootstrapStatus={createBootstrapStatus({
          setup: {
            requestedProfile: 'c',
            effectiveProfile: 'b',
          },
        })}
        statusLoading={false}
        statusError={null}
        onRefreshStatus={vi.fn()}
      />
    );

    await user.click(screen.getByRole('button', { name: 'Mode C' }));
    await user.click(screen.getByRole('button', { name: 'Re-detect' }));

    expect(await screen.findByText('Requested Profile C fell back to Profile B after provider checks.')).toBeInTheDocument();
    expect(screen.getByTestId('guided-step-providers')).toHaveAttribute('data-state', 'pending');
  });
});

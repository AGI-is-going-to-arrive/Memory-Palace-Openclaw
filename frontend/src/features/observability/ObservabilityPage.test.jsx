import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import * as api from '../../lib/api';
import i18n, { LOCALE_STORAGE_KEY } from '../../i18n';
import ObservabilityPage from './ObservabilityPage';

vi.mock('../../lib/api', () => ({
  cancelIndexJob: vi.fn(),
  extractApiError: vi.fn((error, fallback = 'Request failed') => error?.message || fallback),
  getIndexJob: vi.fn(),
  getObservabilitySummary: vi.fn(),
  retryIndexJob: vi.fn(),
  runObservabilitySearch: vi.fn(),
  triggerIndexRebuild: vi.fn(),
  triggerMemoryReindex: vi.fn(),
  triggerSleepConsolidation: vi.fn(),
}));

const buildSummary = ({
  activeJobId = null,
  recentJobs = [],
  timestamp = '2026-01-01T00:00:00Z',
  queueDepth = recentJobs.length,
  lastError = null,
  searchStats = {
    total_queries: 0,
    degraded_queries: 0,
    cache_hit_ratio: 0,
    cache_hit_queries: 0,
    latency_ms: { avg: 0, p95: 0 },
    mode_breakdown: {},
    intent_breakdown: {},
    strategy_hit_breakdown: {},
    search_trace: {
      backend_method_breakdown: {},
      candidate_multiplier_requested: {},
      candidate_multiplier_applied: {},
      stage_timings_ms: {},
      candidate_counts: {},
      mmr: {},
      rerank: {},
      vector_engine: {},
      recent_events: [],
    },
  },
  writeLanes = {
    global_concurrency: 1,
    global_active: 0,
    global_waiting: 0,
    session_waiting_count: 0,
    session_waiting_sessions: 0,
    max_session_waiting: 0,
    wait_warn_ms: 2000,
    writes_total: 0,
    writes_success: 0,
    writes_failed: 0,
    failure_rate: 0,
    session_wait_ms_p95: 0,
    global_wait_ms_p95: 0,
    duration_ms_p95: 0,
    last_error: null,
  },
  smLite = {
    storage: 'runtime_ephemeral',
    promotion_path: 'compact_context + auto_flush',
    session_cache: { session_count: 0, total_hits: 0 },
    flush_tracker: { session_count: 0, pending_events: 0 },
  },
  transport = {
    available: false,
    degraded: false,
    reason: 'transport_trace_unavailable',
    status: 'unavailable',
    active_transport: null,
    configured_transport: null,
    fallback_order: [],
    diagnostics: {
      connect_attempts: 0,
      connect_latency_ms: null,
      connect_retry_count: 0,
      call_retry_count: 0,
      request_retries: 0,
      fallback_count: 0,
      reuse_count: 0,
      last_connected_at: null,
      last_error: null,
      last_health_check_at: null,
      last_health_check_error: null,
      healthcheck_tool: null,
      healthcheck_ttl_ms: null,
      recent_events: [],
      exception_breakdown: {
        total: 0,
        status_counts: {},
        source_counts: {},
        category_counts: {},
        tool_counts: {},
        check_id_counts: {},
        last_exception_at: null,
        signature_breakdown: {
          total: 0,
          signature_counts: {},
          items: [],
        },
        incident_breakdown: {
          incident_count: 0,
          canonical_cause_counts: {},
          items: [],
        },
        items: [],
      },
    },
    last_report: null,
  },
} = {}) => ({
  status: 'ok',
  timestamp,
  search_stats: searchStats,
  transport,
  health: {
    index: { degraded: false },
    runtime: {
      write_lanes: writeLanes,
      index_worker: {
        active_job_id: activeJobId,
        recent_jobs: recentJobs,
        queue_depth: queueDepth,
        cancelling_jobs: 0,
        sleep_pending: false,
        last_error: lastError,
      },
      sleep_consolidation: {},
      sm_lite: smLite,
    },
  },
  index_latency: {},
  cleanup_query_stats: {},
});

const createDeferred = () => {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
};

describe('ObservabilityPage', () => {
  const getJobCardById = async (jobId) => {
    const jobLabel = await screen.findByText(jobId);
    const card = jobLabel.closest('article');
    expect(card).not.toBeNull();
    return card;
  };

  beforeEach(async () => {
    vi.clearAllMocks();
    window.localStorage?.removeItem?.(LOCALE_STORAGE_KEY);
    await i18n.changeLanguage('en');
    api.getObservabilitySummary.mockResolvedValue(buildSummary());
    api.getIndexJob.mockResolvedValue({ job: null });
    api.retryIndexJob.mockResolvedValue({ job_id: 'retry-default' });
    api.runObservabilitySearch.mockResolvedValue({ results: [] });
    api.triggerIndexRebuild.mockResolvedValue({ job_id: 'rebuild-default' });
    api.triggerMemoryReindex.mockResolvedValue({ job_id: 'reindex-default' });
    api.triggerSleepConsolidation.mockResolvedValue({ job_id: 'sleep-default' });
    api.cancelIndexJob.mockResolvedValue({});
  });

  it('shows translated validation errors in zh-CN for invalid integer inputs', async () => {
    const user = userEvent.setup();
    await i18n.changeLanguage('zh-CN');
    render(<ObservabilityPage />);

    const maxResultsInput = await screen.findByLabelText(i18n.t('observability.maxResults'));
    await user.clear(maxResultsInput);
    await user.type(maxResultsInput, '999');
    await user.click(screen.getByRole('button', { name: i18n.t('observability.runDiagnosticSearch') }));

    expect(
      await screen.findByText((content) => content.includes('最大结果数') && content.includes('[1, 50]'))
    ).toBeInTheDocument();
    expect(api.runObservabilitySearch).not.toHaveBeenCalled();
  });

  it('does not refetch summary when only the language changes', async () => {
    render(<ObservabilityPage />);

    await screen.findByText('Search Console');
    const initialCalls = api.getObservabilitySummary.mock.calls.length;
    expect(initialCalls).toBeGreaterThan(0);

    await act(async () => {
      await i18n.changeLanguage('zh-CN');
    });

    await screen.findByText('搜索控制台');
    expect(api.getObservabilitySummary.mock.calls.length).toBe(initialCalls);
  });

  it('relocalizes visible errors and untouched default query when only the language changes', async () => {
    api.getObservabilitySummary.mockRejectedValueOnce({ message: 'Network Error' });
    api.extractApiError.mockImplementation((error, fallback = 'Request failed') => {
      const locale = i18n.resolvedLanguage || i18n.language || 'en';
      return `${locale}:${error?.message || fallback}`;
    });

    render(<ObservabilityPage />);

    expect(await screen.findByText('en:Network Error')).toBeInTheDocument();
    expect(screen.getByLabelText('Query')).toHaveValue('memory flush queue');
    const initialCalls = api.getObservabilitySummary.mock.calls.length;

    await act(async () => {
      await i18n.changeLanguage('zh-CN');
    });

    expect(api.getObservabilitySummary.mock.calls.length).toBe(initialCalls);
    expect(await screen.findByText('zh-CN:Network Error')).toBeInTheDocument();
    expect(screen.getByLabelText('查询词')).toHaveValue(i18n.t('observability.defaultQuery'));
  });

  it('renders the transport unavailable branch without instance cards', async () => {
    render(<ObservabilityPage />);

    expect(
      await screen.findByText('Transport snapshot unavailable (transport_trace_unavailable).')
    ).toBeInTheDocument();
    expect(screen.queryByText('Transport Instances')).not.toBeInTheDocument();
  });

  it('renders transport diagnostics, connect latency summary, and truncation hints from observability summary', async () => {
    api.getObservabilitySummary.mockResolvedValueOnce(
      buildSummary({
        transport: {
          available: true,
          degraded: true,
          status: 'warn',
          active_transport: 'stdio',
          configured_transport: 'auto',
          snapshot_count: 2,
          fallback_order: ['stdio', 'sse'],
          instances: [
            ...Array.from({ length: 7 }, (_, index) => ({
              instance_id: `pid-10${index + 1}`,
              status: index === 0 ? 'warn' : 'pass',
              active_transport: index % 2 === 0 ? 'sse' : 'stdio',
              updated_at: `2026-01-01T00:0${Math.min(index + 1, 9)}:00Z`,
              source_path: `/tmp/transport-${index + 1}.json`,
            })),
          ],
          diagnostics: {
            connect_attempts: 3,
            connect_latency_ms: {
              last: 11.2,
              avg: 12.3,
              p95: 23.4,
              max: 45.6,
              samples: 8,
            },
            connect_retry_count: 1,
            call_retry_count: 2,
            request_retries: 2,
            fallback_count: 1,
            reuse_count: 5,
            last_connected_at: '2026-01-01T00:00:00Z',
            last_error: 'Authorization: Bearer [REDACTED]',
            last_health_check_at: '2026-01-01T00:01:00Z',
            last_health_check_error: 'token=[REDACTED]',
            recent_events: [
              {
                at: '2026-01-01T00:01:00Z',
                category: 'healthcheck',
                status: 'fail',
                transport: 'sse',
                tool: 'index_status',
                message: 'token=[REDACTED]',
              },
            ],
            exception_breakdown: {
              total: 4,
              status_counts: {
                fail: 3,
                warn: 1,
              },
              source_counts: {
                recent_events: 1,
                last_report_checks: 1,
                last_error: 1,
                last_health_check_error: 1,
              },
              category_counts: {
                healthcheck: 2,
                report_check: 1,
                transport: 1,
              },
              tool_counts: {
                index_status: 2,
              },
              check_id_counts: {
                'transport-health': 1,
              },
              last_exception_at: '2026-01-01T00:01:00Z',
              signature_breakdown: {
                total: 4,
                signature_counts: {
                  'fail | healthcheck | index_status | X-MCP-API-Key: [REDACTED]': 1,
                  'warn | healthcheck | index_status | token=[REDACTED]': 1,
                  'fail | transport | stdio | Authorization: Bearer [REDACTED]': 1,
                  'fail | report_check | transport-health | Transport health check failed.': 1,
                },
                items: [
                  {
                    signature: 'fail | healthcheck | index_status | X-MCP-API-Key: [REDACTED]',
                    status: 'fail',
                    category: 'healthcheck',
                    tool: 'index_status',
                    message: 'X-MCP-API-Key: [REDACTED]',
                    signal_count: 1,
                    sources: ['recent_events'],
                  },
                  {
                    signature:
                      'fail | report_check | transport-health | Transport health check failed.',
                    status: 'fail',
                    category: 'report_check',
                    check_id: 'transport-health',
                    message: 'Transport health check failed.',
                    signal_count: 1,
                    sources: ['last_report_checks'],
                  },
                ],
              },
              incident_breakdown: {
                incident_count: 3,
                canonical_cause_counts: {
                  healthcheck_auth_failure: 2,
                  'transport | stdio | Authorization: Bearer [REDACTED]': 1,
                  'report_check | transport-health | Transport health check failed.': 1,
                },
                items: [
                  {
                    canonical_cause: 'healthcheck_auth_failure',
                    cause_family: 'auth',
                    highest_status: 'fail',
                    category: 'healthcheck',
                    tool: 'index_status',
                    sample_message: 'token=[REDACTED]',
                    signal_count: 2,
                    sources: ['recent_events', 'last_health_check_error'],
                    last_seen_at: '2026-01-01T00:01:00Z',
                  },
                ],
              },
              items: [
                {
                  source: 'recent_events',
                  status: 'fail',
                  category: 'healthcheck',
                  tool: 'index_status',
                  transport: 'sse',
                  count: 1,
                  message: 'token=[REDACTED]',
                },
                {
                  source: 'last_report_checks',
                  status: 'fail',
                  category: 'report_check',
                  check_id: 'transport-health',
                  count: 1,
                  message: 'Transport health check failed.',
                },
              ],
            },
          },
          last_report: {
            command: 'doctor',
            status: 'warn',
            summary: 'doctor completed with warnings.',
            checks: [
              {
                id: 'transport-health',
                status: 'fail',
                message: 'Transport health check failed.',
              },
              {
                id: 'connectivity',
                status: 'warn',
                message: 'Connectivity recovered after retry.',
              },
              {
                id: 'healthcheck',
                status: 'pass',
                message: 'Healthcheck recovered.',
              },
              {
                id: 'fallback',
                status: 'warn',
                message: 'Fallback engaged once.',
              },
              {
                id: 'reuse',
                status: 'pass',
                message: 'Client reuse stable.',
              },
              {
                id: 'ttl',
                status: 'pass',
                message: 'Healthcheck TTL within range.',
              },
            ],
          },
        },
      })
    );

    render(<ObservabilityPage />);

    expect(await screen.findByText('Transport Diagnostics')).toBeInTheDocument();
    expect(screen.getByText(/active: stdio/i)).toBeInTheDocument();
    expect(screen.getByText(/snapshots: 2/i)).toBeInTheDocument();
    expect(screen.getByText(/connect attempts: 3/i)).toBeInTheDocument();
    expect(
      screen.getByText((content) =>
        content.includes('connect latency:') &&
        content.includes('last 11.2 ms') &&
        content.includes('avg 12.3 ms') &&
        content.includes('p95 23.4 ms') &&
        content.includes('max 45.6 ms') &&
        content.includes('n 8')
      )
    ).toBeInTheDocument();
    expect(screen.getAllByText(/Authorization: Bearer \[REDACTED\]/i).length).toBeGreaterThan(0);
    expect(screen.getByText(/doctor completed with warnings\./i)).toBeInTheDocument();
    expect(screen.getByText('Exception Breakdown')).toBeInTheDocument();
    expect(screen.getByText(/fail 3/i)).toBeInTheDocument();
    expect(screen.getByText(/warn 1/i)).toBeInTheDocument();
    expect(screen.getByText(/last exception/i)).toBeInTheDocument();
    expect(screen.getByText(/healthcheck: 2/i)).toBeInTheDocument();
    expect(screen.getByText(/index_status: 2/i)).toBeInTheDocument();
    expect(screen.getByText(/incidents 3/i)).toBeInTheDocument();
    expect(screen.getByText('Canonical Causes')).toBeInTheDocument();
    expect(screen.getAllByText(/healthcheck_auth_failure: 2/i).length).toBeGreaterThan(0);
    expect(screen.getByText('Top Signatures')).toBeInTheDocument();
    expect(screen.getByText(/fail \| transport \| stdio \| Authorization: Bearer \[REDACTED\]: 1/i)).toBeInTheDocument();
    expect(screen.getAllByText(/signals 1/i).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/healthcheck_auth_failure/i).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/sources: recent_events/i).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/token=\[REDACTED\]/i).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/transport health check failed\./i).length).toBeGreaterThan(0);
    expect(screen.getByText('Transport Instances')).toBeInTheDocument();
    expect(screen.getByText('pid-101')).toBeInTheDocument();
    expect(screen.getByText(/debug path: \/tmp\/transport-1\.json/i)).toBeInTheDocument();
    expect(screen.getByText(/healthcheck: pass · healthcheck recovered\./i)).toBeInTheDocument();
    expect(screen.getByText('+2 more')).toBeInTheDocument();
    expect(screen.getByText('+1 more')).toBeInTheDocument();
    expect(screen.queryByText('pid-107')).not.toBeInTheDocument();
  });

  it('renders an explicit no-exceptions state when transport is healthy and has no breakdown items', async () => {
    api.getObservabilitySummary.mockResolvedValueOnce(
      buildSummary({
        transport: {
          available: true,
          degraded: false,
          status: 'pass',
          active_transport: 'stdio',
          configured_transport: 'stdio',
          snapshot_count: 1,
          fallback_order: ['stdio'],
          diagnostics: {
            connect_attempts: 1,
            recent_events: [
              {
                at: '2026-01-01T00:00:00Z',
                category: 'connect',
                status: 'pass',
                transport: 'stdio',
                message: 'connected',
              },
            ],
          },
          last_report: {
            command: 'doctor',
            status: 'pass',
            summary: 'doctor clean.',
            checks: [
              {
                id: 'connectivity',
                status: 'pass',
                message: 'Connectivity healthy.',
              },
            ],
          },
        },
      })
    );

    render(<ObservabilityPage />);

    expect(await screen.findByText('Transport Diagnostics')).toBeInTheDocument();
    expect(screen.getByText('Exception Breakdown')).toBeInTheDocument();
    expect(screen.getByText('No aggregated warn/fail exceptions.')).toBeInTheDocument();
    expect(screen.queryByText('Canonical Causes')).not.toBeInTheDocument();
    expect(screen.queryByText('Top Signatures')).not.toBeInTheDocument();
  });

  it('derives canonical incident causes from fallback transport signals when breakdown payload is missing', async () => {
    api.getObservabilitySummary.mockResolvedValueOnce(
      buildSummary({
        transport: {
          available: true,
          degraded: true,
          status: 'fail',
          active_transport: 'sse',
          configured_transport: 'sse',
          snapshot_count: 1,
          fallback_order: ['sse'],
          diagnostics: {
            connect_attempts: 2,
            last_health_check_error: '401 Unauthorized token=[REDACTED]',
            healthcheck_tool: 'index_status',
            recent_events: [
              {
                at: '2026-01-01T00:00:00Z',
                category: 'connect',
                status: 'fail',
                transport: 'sse',
                message: 'connect timeout after 1000ms',
              },
              {
                at: '2026-01-01T00:01:00Z',
                category: 'connect',
                status: 'fail',
                transport: 'sse',
                message: 'dial tcp 10.10.0.8:443: no route to host',
              },
              {
                at: '2026-01-01T00:02:00Z',
                category: 'transport',
                status: 'fail',
                transport: 'sse',
                message: 'socket hang up while streaming tool results',
              },
              {
                at: '2026-01-01T00:03:00Z',
                category: 'transport',
                status: 'fail',
                transport: 'sse',
                message: '503 Service Unavailable from upstream',
              },
            ],
            exception_breakdown: null,
          },
          last_report: {
            command: 'doctor',
            status: 'fail',
            summary: 'doctor failed.',
            checks: [
              {
                id: 'transport-health',
                status: 'fail',
                message: 'Transport health check failed.',
              },
              {
                id: 'auth',
                status: 'fail',
                message: '401 Unauthorized token=[REDACTED]',
              },
              {
                id: 'connectivity',
                status: 'fail',
                message: 'ECONNREFUSED while contacting upstream',
              },
              {
                id: 'payload',
                status: 'fail',
                message: '413 Payload Too Large',
              },
              {
                id: 'protocol',
                status: 'fail',
                message: 'protocol error: unexpected content-type text/html; invalid json',
              },
            ],
          },
        },
      })
    );

    render(<ObservabilityPage />);

    expect(await screen.findByText('Canonical Causes')).toBeInTheDocument();
    expect(screen.getAllByText(/transport_timeout/i).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/transport_upstream_unavailable/i).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/healthcheck_auth_failure/i).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/transport_connection_refused/i).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/transport_network_unreachable/i).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/transport_connection_reset/i).length).toBeGreaterThan(0);
    expect(
      screen.queryByText(/report_check \| transport-health \| transport health check failed\./i)
    ).not.toBeInTheDocument();
  });

  it('derives payload and protocol canonical incident causes from fallback transport signals', async () => {
    api.getObservabilitySummary.mockResolvedValueOnce(
      buildSummary({
        transport: {
          available: true,
          degraded: true,
          status: 'fail',
          active_transport: 'sse',
          configured_transport: 'sse',
          snapshot_count: 1,
          fallback_order: ['sse'],
          diagnostics: {
            connect_attempts: 1,
            recent_events: [
              {
                at: '2026-01-01T00:00:00Z',
                category: 'transport',
                status: 'fail',
                transport: 'sse',
                message: 'HTTP 413 Payload Too Large',
              },
              {
                at: '2026-01-01T00:01:00Z',
                category: 'transport',
                status: 'fail',
                transport: 'sse',
                message: 'protocol error: unexpected content-type text/html; invalid json',
              },
            ],
            exception_breakdown: null,
          },
          last_report: {
            command: 'doctor',
            status: 'fail',
            summary: 'doctor failed.',
            checks: [],
          },
        },
      })
    );

    render(<ObservabilityPage />);

    expect(await screen.findByText('Canonical Causes')).toBeInTheDocument();
    expect(screen.getAllByText(/transport_payload_too_large/i).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/transport_protocol_error/i).length).toBeGreaterThan(0);
  });

  it('classifies generic connection reset messages in fallback transport signals', async () => {
    api.getObservabilitySummary.mockResolvedValueOnce(
      buildSummary({
        transport: {
          available: true,
          degraded: true,
          status: 'fail',
          active_transport: 'sse',
          configured_transport: 'sse',
          snapshot_count: 1,
          fallback_order: ['sse'],
          diagnostics: {
            connect_attempts: 1,
            last_error: 'connection reset',
            recent_events: [
              {
                at: '2026-01-01T00:00:00Z',
                category: 'transport',
                status: 'fail',
                transport: 'sse',
                message: 'connection reset',
              },
            ],
            exception_breakdown: null,
          },
          last_report: {
            command: 'doctor',
            ok: false,
            status: 'fail',
            summary: 'doctor failed.',
            checks: [
              {
                id: 'transport-health',
                status: 'fail',
                message: 'connection reset',
              },
            ],
          },
        },
      })
    );

    render(<ObservabilityPage />);

    expect(await screen.findByText('Canonical Causes')).toBeInTheDocument();
    expect(screen.getAllByText(/transport_connection_reset/i).length).toBeGreaterThan(0);
  });

  it('renders remaining canonical incident causes from incident breakdown payload', async () => {
    const incidentItems = [
      {
        canonical_cause: 'transport_connect_fallback',
        cause_family: 'latency',
        highest_status: 'warn',
        category: 'connect',
        transport: 'sse',
        sample_message: 'connected after fallback from stdio to sse',
        signal_count: 2,
        sources: ['recent_events'],
        last_seen_at: '2026-01-01T00:00:00Z',
      },
      {
        canonical_cause: 'transport_dns_failure',
        cause_family: 'network',
        highest_status: 'fail',
        category: 'connect',
        transport: 'sse',
        sample_message: 'getaddrinfo ENOTFOUND gateway.internal',
        signal_count: 1,
        sources: ['recent_events'],
        last_seen_at: '2026-01-01T00:01:00Z',
      },
      {
        canonical_cause: 'transport_tls_failure',
        cause_family: 'tls',
        highest_status: 'fail',
        category: 'transport',
        transport: 'sse',
        sample_message: 'tls: certificate verify failed for upstream endpoint',
        signal_count: 1,
        sources: ['recent_events'],
        last_seen_at: '2026-01-01T00:02:00Z',
      },
      {
        canonical_cause: 'transport_rate_limited',
        cause_family: 'upstream',
        highest_status: 'warn',
        category: 'transport',
        transport: 'sse',
        sample_message: '429 Too Many Requests from upstream',
        signal_count: 3,
        sources: ['recent_events', 'last_report_checks'],
        last_seen_at: '2026-01-01T00:03:00Z',
      },
      {
        canonical_cause: 'sqlite_database_locked',
        cause_family: 'storage',
        highest_status: 'fail',
        category: 'transport',
        transport: 'stdio',
        sample_message: 'sqlite3.OperationalError: database is locked',
        signal_count: 1,
        sources: ['last_error'],
        last_seen_at: '2026-01-01T00:04:00Z',
      },
      {
        canonical_cause: 'transport_snapshot_load_failed',
        cause_family: 'observability',
        highest_status: 'warn',
        category: 'snapshot_load',
        transport: 'stdio',
        sample_message: 'failed to parse snapshot /tmp/transport-bad.json',
        signal_count: 1,
        sources: ['recent_events'],
        last_seen_at: '2026-01-01T00:05:00Z',
      },
    ];

    api.getObservabilitySummary.mockResolvedValueOnce(
      buildSummary({
        transport: {
          available: true,
          degraded: true,
          status: 'warn',
          active_transport: 'sse',
          configured_transport: 'auto',
          snapshot_count: 2,
          fallback_order: ['stdio', 'sse'],
          diagnostics: {
            connect_attempts: 4,
            recent_events: [],
            exception_breakdown: {
              total: incidentItems.length,
              status_counts: {
                fail: 3,
                warn: 3,
              },
              source_counts: {
                recent_events: 5,
                last_error: 1,
              },
              category_counts: {
                connect: 2,
                transport: 3,
                snapshot_load: 1,
              },
              tool_counts: {},
              check_id_counts: {},
              last_exception_at: '2026-01-01T00:05:00Z',
              signature_breakdown: {
                total: 0,
                signature_counts: {},
                items: [],
              },
              incident_breakdown: {
                incident_count: incidentItems.length,
                canonical_cause_counts: {
                  transport_connect_fallback: 2,
                  transport_dns_failure: 1,
                  transport_tls_failure: 1,
                  transport_rate_limited: 3,
                  sqlite_database_locked: 1,
                  transport_snapshot_load_failed: 1,
                },
                items: incidentItems,
              },
              items: [],
            },
          },
          last_report: {
            command: 'doctor',
            status: 'warn',
            summary: 'doctor completed with warnings.',
            checks: [],
          },
        },
      })
    );

    render(<ObservabilityPage />);

    expect(await screen.findByText('Canonical Causes')).toBeInTheDocument();
    expect(screen.getByText(/incidents 6/i)).toBeInTheDocument();
    expect(screen.getAllByText(/^latency$/i).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/^network$/i).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/^tls$/i).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/^upstream$/i).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/^storage$/i).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/^observability$/i).length).toBeGreaterThan(0);

    [
      {
        label: 'Connected only after transport fallback',
        action:
          'recommended action: Review the preferred transport health; the system is currently relying on fallback.',
        rawCode: 'raw cause code: transport_connect_fallback',
        sampleMessage: 'connected after fallback from stdio to sse',
      },
      {
        label: 'DNS / host resolution failure',
        action:
          'recommended action: Verify DNS and host mapping for the configured endpoint, especially in Docker.',
        rawCode: 'raw cause code: transport_dns_failure',
        sampleMessage: 'getaddrinfo ENOTFOUND gateway.internal',
      },
      {
        label: 'TLS / certificate failure',
        action:
          'recommended action: Check certificate trust, expiry, and whether the endpoint should use HTTP instead of HTTPS.',
        rawCode: 'raw cause code: transport_tls_failure',
        sampleMessage: 'tls: certificate verify failed for upstream endpoint',
      },
      {
        label: 'Rate limited by upstream',
        action:
          'recommended action: Reduce request burst or switch to a higher-quota key before retrying.',
        rawCode: 'raw cause code: transport_rate_limited',
        sampleMessage: '429 Too Many Requests from upstream',
      },
      {
        label: 'SQLite database lock contention',
        action:
          'recommended action: Reduce concurrent SQLite activity or inspect lock holders before retrying.',
        rawCode: 'raw cause code: sqlite_database_locked',
        sampleMessage: 'sqlite3.OperationalError: database is locked',
      },
      {
        label: 'Transport snapshot load failed',
        action:
          'recommended action: Remove corrupted snapshot files and rerun doctor to regenerate diagnostics.',
        rawCode: 'raw cause code: transport_snapshot_load_failed',
        sampleMessage: 'failed to parse snapshot /tmp/transport-bad.json',
      },
    ].forEach(({ label, action, rawCode, sampleMessage }) => {
      expect(screen.getByText(label)).toBeInTheDocument();
      expect(screen.getByText(action)).toBeInTheDocument();
      expect(screen.getByText(rawCode)).toBeInTheDocument();
      expect(screen.getByText(sampleMessage)).toBeInTheDocument();
    });
  });

  it('prefers backend-provided cause_family over local canonical fallback', async () => {
    api.getObservabilitySummary.mockResolvedValueOnce(
      buildSummary({
        transport: {
          available: true,
          degraded: true,
          status: 'warn',
          active_transport: 'sse',
          configured_transport: 'auto',
          snapshot_count: 1,
          fallback_order: ['sse'],
          diagnostics: {
            connect_attempts: 1,
            recent_events: [],
            exception_breakdown: {
              total: 1,
              status_counts: { warn: 1 },
              source_counts: { recent_events: 1 },
              category_counts: { healthcheck: 1 },
              tool_counts: {},
              check_id_counts: {},
              last_exception_at: '2026-01-01T00:00:00Z',
              signature_breakdown: {
                total: 0,
                signature_counts: {},
                items: [],
              },
              incident_breakdown: {
                incident_count: 1,
                canonical_cause_counts: {
                  backend_custom_cause: 1,
                },
                items: [
                  {
                    canonical_cause: 'backend_custom_cause',
                    cause_family: 'healthcheck',
                    highest_status: 'warn',
                    category: 'healthcheck',
                    transport: 'sse',
                    sample_message: 'custom backend sample',
                    signal_count: 1,
                    sources: ['recent_events'],
                    last_seen_at: '2026-01-01T00:00:00Z',
                  },
                ],
              },
              items: [],
            },
          },
          last_report: null,
        },
      })
    );

    render(<ObservabilityPage />);

    expect(await screen.findByText('Canonical Causes')).toBeInTheDocument();
    expect(screen.getAllByText(/^healthcheck$/i).length).toBeGreaterThan(0);
    expect(screen.getByText(/backend custom cause/i)).toBeInTheDocument();
    expect(screen.getByText('raw cause code: backend_custom_cause')).toBeInTheDocument();
  });

  it('renders a single degraded transport instance card', async () => {
    api.getObservabilitySummary.mockResolvedValueOnce(
      buildSummary({
        transport: {
          available: true,
          degraded: true,
          status: 'warn',
          active_transport: 'stdio',
          configured_transport: 'auto',
          snapshot_count: 1,
          fallback_order: ['stdio'],
          instances: [
            {
              instance_id: 'pid-single',
              status: 'warn',
              active_transport: 'stdio',
              updated_at: '2026-01-01T00:01:00Z',
              source_path: '/tmp/transport-single.json',
            },
          ],
          diagnostics: {
            connect_attempts: 1,
            recent_events: [],
          },
          last_report: null,
        },
      })
    );

    render(<ObservabilityPage />);

    expect(await screen.findByText('Transport Instances')).toBeInTheDocument();
    expect(screen.getByText('pid-single')).toBeInTheDocument();
    expect(screen.getByText(/debug path: \/tmp\/transport-single\.json/i)).toBeInTheDocument();
  });

  it('renders search trace rollups and write-lane metrics from observability summary', async () => {
    const baseSummary = buildSummary();
    api.getObservabilitySummary.mockResolvedValueOnce(
      buildSummary({
        searchStats: {
          ...baseSummary.search_stats,
          search_trace: {
            ...baseSummary.search_stats.search_trace,
            backend_method_breakdown: {
              sqlite_fts: 3,
              semantic_scan: 1,
            },
            candidate_multiplier_requested: {
              last: 4,
              avg: 4,
              p95: 4,
              max: 4,
              samples: 4,
            },
            candidate_multiplier_applied: {
              last: 3,
              avg: 3,
              p95: 3,
              max: 3,
              samples: 4,
            },
            stage_timings_ms: {
              vector_lookup: {
                last: 8,
                avg: 7.5,
                p95: 10,
                max: 10,
                samples: 4,
              },
            },
            candidate_counts: {
              retrieved: {
                last: 48,
                avg: 42,
                p95: 48,
                max: 48,
                samples: 4,
              },
            },
            rerank: {
              provider: {
                top_values: [{ value: 'qwen-reranker', count: 4 }],
              },
            },
            vector_engine: {
              backend: {
                top_values: [{ value: 'sqlite-vec', count: 4 }],
              },
            },
            recent_events: [
              {
                timestamp: '2026-01-01T00:01:00Z',
                mode_applied: 'hybrid',
                intent_applied: 'factual',
                latency_ms: 12.5,
                degraded: false,
                session_count: 1,
                global_count: 8,
                returned_count: 4,
                degrade_reasons: [],
                search_trace: {
                  backend_method: 'sqlite_fts',
                },
              },
            ],
          },
        },
        writeLanes: {
          global_concurrency: 2,
          global_active: 1,
          global_waiting: 1,
          session_waiting_count: 2,
          session_waiting_sessions: 1,
          max_session_waiting: 2,
          wait_warn_ms: 750,
          writes_total: 12,
          writes_success: 11,
          writes_failed: 1,
          failure_rate: 1 / 12,
          session_wait_ms_p95: 420,
          global_wait_ms_p95: 180,
          duration_ms_p95: 96,
          last_error: 'queue_full',
        },
      })
    );

    render(<ObservabilityPage />);

    expect(await screen.findByText('Search Trace')).toBeInTheDocument();
    expect(screen.getByText('Write Lanes')).toBeInTheDocument();
    expect(screen.getByText(/utilization 50%/i)).toBeInTheDocument();
    expect(screen.getByText(/failure rate 8.3%/i)).toBeInTheDocument();
    expect(screen.getByText(/last error queue_full/i)).toBeInTheDocument();
    expect(screen.getByText('Lane Metrics')).toBeInTheDocument();
    expect(screen.getByText(/writes total/i)).toBeInTheDocument();
    expect(screen.getByText(/session wait ms p95/i)).toBeInTheDocument();
    expect(screen.getByText(/sqlite_fts: 3/i)).toBeInTheDocument();
    expect(screen.getByText(/semantic_scan: 1/i)).toBeInTheDocument();
    expect(
      screen.getByText((content) =>
        content.includes('avg 7.5 ms') &&
        content.includes('p95 10.0 ms') &&
        content.includes('n 4')
      )
    ).toBeInTheDocument();
    expect(screen.getByText(/sqlite-vec x4/i)).toBeInTheDocument();
    expect(screen.getByText('Recent Search Events')).toBeInTheDocument();
    expect(screen.getByText(/counts s:1 g:8 r:4/i)).toBeInTheDocument();
  });

  it('renders the latest diagnostic-search trace details when a search returns trace metadata', async () => {
    const user = userEvent.setup();
    api.runObservabilitySearch.mockResolvedValueOnce({
      results: [],
      latency_ms: 18.4,
      mode_applied: 'hybrid',
      intent_applied: 'factual',
      strategy_template_applied: 'default',
      degraded: false,
      counts: {
        session: 1,
        global: 2,
        returned: 0,
      },
      search_trace: {
        backend_method: 'sqlite_fts',
        candidate_multiplier_requested: 4,
        candidate_multiplier_applied: 3,
        stage_timings_ms: {
          vector_lookup: 9.2,
        },
        candidate_counts: {
          retrieved: 24,
        },
        mmr: {
          applied: true,
        },
        rerank: {
          model: 'qwen-reranker',
        },
        vector_engine: {
          backend: 'sqlite-vec',
        },
      },
    });

    render(<ObservabilityPage />);

    await user.click(await screen.findByRole('button', { name: /Run Diagnostic Search/i }));

    expect(await screen.findByText('Latest Diagnostic Search')).toBeInTheDocument();
    expect(screen.getByText(/backend sqlite_fts/i)).toBeInTheDocument();
    expect(screen.getByText(/requested 4/i)).toBeInTheDocument();
    expect(screen.getByText(/applied 3/i)).toBeInTheDocument();
    expect(screen.getAllByText(/vector lookup/i).length).toBeGreaterThan(0);
    expect(screen.getAllByText('9.2 ms').length).toBeGreaterThan(0);
    expect(screen.getAllByText(/retrieved/i).length).toBeGreaterThan(0);
    expect(screen.getAllByText('24').length).toBeGreaterThan(0);
    expect(screen.getByText(/qwen-reranker/i)).toBeInTheDocument();
    expect(screen.getAllByText(/sqlite-vec/i).length).toBeGreaterThan(0);
  });

  it('uses unified retry endpoint when retry API is available', async () => {
    const failedJob = {
      job_id: 'job-unified',
      status: 'failed',
      task_type: 'reindex_memory',
      memory_id: 12,
      reason: 'failed-job',
    };
    api.getObservabilitySummary
      .mockResolvedValueOnce(buildSummary({ recentJobs: [failedJob], timestamp: '2026-01-01T00:00:00Z' }))
      .mockResolvedValueOnce(buildSummary({ recentJobs: [failedJob], timestamp: '2026-01-01T00:00:01Z' }));
    api.retryIndexJob.mockResolvedValueOnce({ job_id: 'job-unified-retry' });

    const user = userEvent.setup();
    render(<ObservabilityPage />);

    const jobCard = await getJobCardById('job-unified');
    await user.click(within(jobCard).getByRole('button', { name: 'Retry' }));

    await waitFor(() => {
      expect(api.retryIndexJob).toHaveBeenCalledWith('job-unified', { reason: 'retry:job-unified' });
    });
    expect(api.triggerMemoryReindex).not.toHaveBeenCalled();
    expect(api.triggerIndexRebuild).not.toHaveBeenCalled();
    expect(api.triggerSleepConsolidation).not.toHaveBeenCalled();
    expect(await screen.findByText(/Retry requested/i)).toBeInTheDocument();
  });

  it('falls back to old backend endpoint when retry API is unsupported', async () => {
    const legacyJob = {
      job_id: 'job-legacy',
      status: 'failed',
      task_type: 'reindex_memory',
      memory_id: 77,
      reason: 'legacy-backend',
    };
    api.getObservabilitySummary
      .mockResolvedValueOnce(buildSummary({ recentJobs: [legacyJob], timestamp: '2026-01-01T00:00:00Z' }))
      .mockResolvedValueOnce(buildSummary({ recentJobs: [legacyJob], timestamp: '2026-01-01T00:00:01Z' }));
    api.retryIndexJob.mockRejectedValueOnce({
      response: { status: 404, data: { detail: 'Not Found' } },
    });
    api.triggerMemoryReindex.mockResolvedValueOnce({ job_id: 'job-legacy-retry' });

    const user = userEvent.setup();
    render(<ObservabilityPage />);

    const jobCard = await getJobCardById('job-legacy');
    await user.click(within(jobCard).getByRole('button', { name: 'Retry' }));

    await waitFor(() => {
      expect(api.retryIndexJob).toHaveBeenCalledWith('job-legacy', { reason: 'retry:job-legacy' });
      expect(api.triggerMemoryReindex).toHaveBeenCalledWith(77, { reason: 'retry:job-legacy', wait: false });
    });
    expect(api.triggerIndexRebuild).not.toHaveBeenCalled();
    expect(api.triggerSleepConsolidation).not.toHaveBeenCalled();
    expect(await screen.findByText(/Retry requested/i)).toBeInTheDocument();
  });

  it('falls back to legacy rebuild endpoint when retry endpoint returns 405', async () => {
    const legacyJob = {
      job_id: 'job-legacy-rebuild',
      status: 'failed',
      task_type: 'rebuild_index',
      reason: 'legacy-rebuild',
    };
    api.getObservabilitySummary
      .mockResolvedValueOnce(buildSummary({ recentJobs: [legacyJob], timestamp: '2026-01-01T00:00:00Z' }))
      .mockResolvedValueOnce(buildSummary({ recentJobs: [legacyJob], timestamp: '2026-01-01T00:00:01Z' }));
    api.retryIndexJob.mockRejectedValueOnce({
      response: { status: 405, data: { detail: 'Method Not Allowed' } },
    });
    api.triggerIndexRebuild.mockResolvedValueOnce({ job_id: 'job-legacy-rebuild-retry' });

    const user = userEvent.setup();
    render(<ObservabilityPage />);

    const jobCard = await getJobCardById('job-legacy-rebuild');
    await user.click(within(jobCard).getByRole('button', { name: 'Retry' }));

    await waitFor(() => {
      expect(api.retryIndexJob).toHaveBeenCalledWith('job-legacy-rebuild', { reason: 'retry:job-legacy-rebuild' });
      expect(api.triggerIndexRebuild).toHaveBeenCalledWith({ reason: 'retry:job-legacy-rebuild', wait: false });
    });
    expect(api.triggerMemoryReindex).not.toHaveBeenCalled();
    expect(api.triggerSleepConsolidation).not.toHaveBeenCalled();
    expect(await screen.findByText(/Retry requested/i)).toBeInTheDocument();
  });

  it('does not fallback to legacy endpoints when retry returns job_not_found', async () => {
    const failedJob = {
      job_id: 'job-not-found',
      status: 'failed',
      task_type: 'reindex_memory',
      memory_id: 88,
      reason: 'not-found-case',
    };
    api.getObservabilitySummary.mockResolvedValue(buildSummary({ recentJobs: [failedJob] }));
    api.retryIndexJob.mockRejectedValueOnce({
      message: 'job not found',
      response: {
        status: 404,
        data: {
          detail: { error: 'job_not_found', message: 'job not found' },
        },
      },
    });

    const user = userEvent.setup();
    render(<ObservabilityPage />);

    const jobCard = await getJobCardById('job-not-found');
    await user.click(within(jobCard).getByRole('button', { name: 'Retry' }));

    await waitFor(() => {
      expect(api.retryIndexJob).toHaveBeenCalledWith('job-not-found', { reason: 'retry:job-not-found' });
    });
    expect(api.triggerMemoryReindex).not.toHaveBeenCalled();
    expect(api.triggerIndexRebuild).not.toHaveBeenCalled();
    expect(api.triggerSleepConsolidation).not.toHaveBeenCalled();
    expect(await screen.findByText(/Retry failed \(job-not-found\): job not found/i)).toBeInTheDocument();
  });

  it('does not fallback when 404 detail message reports job not found', async () => {
    const failedJob = {
      job_id: 'job-not-found-message',
      status: 'failed',
      task_type: 'rebuild_index',
      reason: 'not-found-message-case',
    };
    api.getObservabilitySummary.mockResolvedValue(buildSummary({ recentJobs: [failedJob] }));
    api.retryIndexJob.mockRejectedValueOnce({
      message: 'job missing',
      response: {
        status: 404,
        data: {
          detail: {
            error: 'request_failed',
            reason: 'backend_error',
            message: 'job not found',
          },
        },
      },
    });

    const user = userEvent.setup();
    render(<ObservabilityPage />);

    const jobCard = await getJobCardById('job-not-found-message');
    await user.click(within(jobCard).getByRole('button', { name: 'Retry' }));

    await waitFor(() => {
      expect(api.retryIndexJob).toHaveBeenCalledWith('job-not-found-message', { reason: 'retry:job-not-found-message' });
    });
    expect(api.triggerMemoryReindex).not.toHaveBeenCalled();
    expect(api.triggerIndexRebuild).not.toHaveBeenCalled();
    expect(api.triggerSleepConsolidation).not.toHaveBeenCalled();
    expect(await screen.findByText(/Retry failed \(job-not-found-message\): job missing/i)).toBeInTheDocument();
  });

  it('falls back to legacy sleep consolidation endpoint when retry endpoint returns 405', async () => {
    const legacyJob = {
      job_id: 'job-legacy-sleep',
      status: 'failed',
      task_type: 'sleep_consolidation',
      reason: 'legacy-sleep',
    };
    api.getObservabilitySummary
      .mockResolvedValueOnce(buildSummary({ recentJobs: [legacyJob], timestamp: '2026-01-01T00:00:00Z' }))
      .mockResolvedValueOnce(buildSummary({ recentJobs: [legacyJob], timestamp: '2026-01-01T00:00:01Z' }));
    api.retryIndexJob.mockRejectedValueOnce({
      response: { status: 405, data: { detail: 'Method Not Allowed' } },
    });
    api.triggerSleepConsolidation.mockResolvedValueOnce({ job_id: 'job-legacy-sleep-retry' });

    const user = userEvent.setup();
    render(<ObservabilityPage />);

    const jobCard = await getJobCardById('job-legacy-sleep');
    await user.click(within(jobCard).getByRole('button', { name: 'Retry' }));

    await waitFor(() => {
      expect(api.retryIndexJob).toHaveBeenCalledWith('job-legacy-sleep', { reason: 'retry:job-legacy-sleep' });
      expect(api.triggerSleepConsolidation).toHaveBeenCalledWith({ reason: 'retry:job-legacy-sleep', wait: false });
    });
    expect(api.triggerMemoryReindex).not.toHaveBeenCalled();
    expect(api.triggerIndexRebuild).not.toHaveBeenCalled();
    expect(await screen.findByText(/Retry requested/i)).toBeInTheDocument();
  });

  it('shows explicit error when fallback task type is unsupported', async () => {
    const unknownTaskJob = {
      job_id: 'job-unknown-task',
      status: 'failed',
      task_type: 'unknown_task_type',
      reason: 'unknown-task',
    };
    api.getObservabilitySummary.mockResolvedValue(buildSummary({ recentJobs: [unknownTaskJob] }));
    api.retryIndexJob.mockRejectedValueOnce({
      response: { status: 405, data: { detail: 'Method Not Allowed' } },
    });

    const user = userEvent.setup();
    render(<ObservabilityPage />);

    const jobCard = await getJobCardById('job-unknown-task');
    await user.click(within(jobCard).getByRole('button', { name: 'Retry' }));

    expect(api.triggerMemoryReindex).not.toHaveBeenCalled();
    expect(api.triggerIndexRebuild).not.toHaveBeenCalled();
    expect(api.triggerSleepConsolidation).not.toHaveBeenCalled();
    expect(
      await screen.findByText(
        /Retry failed \(job-unknown-task\): retry for task type 'unknown_task_type' is not supported/i,
      ),
    ).toBeInTheDocument();
  });

  it('renders runtime queue depth and last worker error', async () => {
    api.getObservabilitySummary.mockResolvedValue(
      buildSummary({
        queueDepth: 9,
        lastError: 'queue_full',
      }),
    );

    render(<ObservabilityPage />);

    expect(await screen.findByText(/queue depth:\s*9/i)).toBeInTheDocument();
    expect(screen.getByText(/last worker error:\s*queue_full/i)).toBeInTheDocument();
  });

  it('keeps latest summary state when refresh requests race', async () => {
    const user = userEvent.setup();
    const first = createDeferred();
    const second = createDeferred();
    api.getObservabilitySummary
      .mockImplementationOnce(() => first.promise)
      .mockImplementationOnce(() => second.promise);

    render(<ObservabilityPage />);

    await user.click(screen.getByRole('button', { name: 'Rebuild Index' }));
    await waitFor(() => {
      expect(api.getObservabilitySummary).toHaveBeenCalledTimes(2);
    });

    second.resolve(buildSummary({ queueDepth: 2, timestamp: '2026-01-01T00:00:02Z' }));
    await screen.findByText(/queue depth:\s*2/i);

    first.resolve(buildSummary({ queueDepth: 9, timestamp: '2026-01-01T00:00:01Z' }));
    await waitFor(() => {
      expect(screen.getByText(/queue depth:\s*2/i)).toBeInTheDocument();
      expect(screen.queryByText(/queue depth:\s*9/i)).not.toBeInTheDocument();
    });
  });

  it('blocks diagnostic search when max priority is not an integer', async () => {
    const user = userEvent.setup();
    render(<ObservabilityPage />);

    const input = await screen.findByLabelText('Max priority filter');
    await user.type(input, '1.9');
    await user.click(screen.getByRole('button', { name: /Run Diagnostic Search/i }));

    expect(api.runObservabilitySearch).not.toHaveBeenCalled();
    expect(await screen.findByText(/max priority filter must be a non-negative integer/i)).toBeInTheDocument();
  });

  it('sends max priority as an integer filter', async () => {
    const user = userEvent.setup();
    render(<ObservabilityPage />);

    const input = await screen.findByLabelText('Max priority filter');
    await user.type(input, '3');
    await user.click(screen.getByRole('button', { name: /Run Diagnostic Search/i }));

    await waitFor(() => {
      expect(api.runObservabilitySearch).toHaveBeenCalledWith(
        expect.objectContaining({
          filters: expect.objectContaining({ max_priority: 3 }),
        }),
      );
    });
  });

  it('sends scope_hint when provided', async () => {
    const user = userEvent.setup();
    render(<ObservabilityPage />);

    const input = await screen.findByLabelText('Scope hint');
    await user.type(input, 'core://agent');
    await user.click(screen.getByRole('button', { name: /Run Diagnostic Search/i }));

    await waitFor(() => {
      expect(api.runObservabilitySearch).toHaveBeenCalledWith(
        expect.objectContaining({
          scope_hint: 'core://agent',
        }),
      );
    });
  });

  it('renders sm-lite runtime metrics', async () => {
    api.getObservabilitySummary.mockResolvedValue(
      buildSummary({
        smLite: {
          storage: 'runtime_ephemeral',
          promotion_path: 'compact_context + auto_flush',
          session_cache: { session_count: 2, total_hits: 6 },
          flush_tracker: { session_count: 1, pending_events: 3 },
          degraded: false,
        },
      }),
    );

    render(<ObservabilityPage />);

    expect(await screen.findByText(/sm-lite sessions:\s*2/i)).toBeInTheDocument();
    expect(screen.getByText(/sm-lite pending events:\s*3/i)).toBeInTheDocument();
  });

  it('shows explicit message when cancel returns 404', async () => {
    const runningJob = {
      job_id: 'job-cancel-missing',
      status: 'running',
      task_type: 'reindex_memory',
      memory_id: 91,
      reason: 'cancel-missing',
    };
    api.getObservabilitySummary
      .mockResolvedValueOnce(buildSummary({ recentJobs: [runningJob], timestamp: '2026-01-01T00:00:00Z' }))
      .mockResolvedValueOnce(buildSummary({ recentJobs: [runningJob], timestamp: '2026-01-01T00:00:01Z' }));
    api.cancelIndexJob.mockRejectedValueOnce({
      message: "job 'job-cancel-missing' not found.",
      response: {
        status: 404,
        data: {
          detail: "job 'job-cancel-missing' not found.",
        },
      },
    });

    const user = userEvent.setup();
    render(<ObservabilityPage />);

    const jobCard = await getJobCardById('job-cancel-missing');
    await user.click(within(jobCard).getByRole('button', { name: 'Cancel' }));

    await waitFor(() => {
      expect(api.cancelIndexJob).toHaveBeenCalledWith('job-cancel-missing', {
        reason: 'observability_console_cancel',
      });
    });
    expect(api.getObservabilitySummary).toHaveBeenCalledTimes(2);
    expect(await screen.findByText(/Cancel skipped \(job-cancel-missing\): job not found/i)).toBeInTheDocument();
  });

  it('shows explicit message when cancel returns 409', async () => {
    const runningJob = {
      job_id: 'job-cancel-finalized',
      status: 'running',
      task_type: 'reindex_memory',
      memory_id: 92,
      reason: 'cancel-finalized',
    };
    api.getObservabilitySummary
      .mockResolvedValueOnce(buildSummary({ recentJobs: [runningJob], timestamp: '2026-01-01T00:00:00Z' }))
      .mockResolvedValueOnce(buildSummary({ recentJobs: [runningJob], timestamp: '2026-01-01T00:00:01Z' }));
    api.cancelIndexJob.mockRejectedValueOnce({
      message: 'job_already_finalized',
      response: {
        status: 409,
        data: {
          detail: 'job_already_finalized',
        },
      },
    });

    const user = userEvent.setup();
    render(<ObservabilityPage />);

    const jobCard = await getJobCardById('job-cancel-finalized');
    await user.click(within(jobCard).getByRole('button', { name: 'Cancel' }));

    await waitFor(() => {
      expect(api.cancelIndexJob).toHaveBeenCalledWith('job-cancel-finalized', {
        reason: 'observability_console_cancel',
      });
    });
    expect(api.getObservabilitySummary).toHaveBeenCalledTimes(2);
    expect(await screen.findByText(/Cancel skipped \(job-cancel-finalized\): already finalized/i)).toBeInTheDocument();
  });

  it('treats unknown 409 conflicts as cancel failure', async () => {
    const runningJob = {
      job_id: 'job-cancel-conflict',
      status: 'running',
      task_type: 'reindex_memory',
      memory_id: 93,
      reason: 'cancel-conflict',
    };
    api.getObservabilitySummary.mockResolvedValue(buildSummary({ recentJobs: [runningJob] }));
    api.cancelIndexJob.mockRejectedValueOnce({
      message: 'running_job_handle_unavailable',
      response: {
        status: 409,
        data: {
          detail: 'running_job_handle_unavailable',
        },
      },
    });

    const user = userEvent.setup();
    render(<ObservabilityPage />);

    const jobCard = await getJobCardById('job-cancel-conflict');
    await user.click(within(jobCard).getByRole('button', { name: 'Cancel' }));

    await waitFor(() => {
      expect(api.cancelIndexJob).toHaveBeenCalledWith('job-cancel-conflict', {
        reason: 'observability_console_cancel',
      });
    });
    expect(api.getObservabilitySummary).toHaveBeenCalledTimes(1);
    expect(
      await screen.findByText(/Cancel failed \(job-cancel-conflict\): running_job_handle_unavailable/i),
    ).toBeInTheDocument();
  });

  it('switches inspect detail between selected job and active job', async () => {
    const activeJobId = 'job-active';
    const inspectedJobId = 'job-inspect';
    const summary = buildSummary({
      activeJobId,
      recentJobs: [
        {
          job_id: inspectedJobId,
          status: 'failed',
          task_type: 'reindex_memory',
          memory_id: 42,
          reason: 'inspect-target',
        },
      ],
    });
    const jobDetails = {
      [activeJobId]: {
        job_id: activeJobId,
        status: 'running',
        task_type: 'rebuild_index',
        reason: 'active-reason',
      },
      [inspectedJobId]: {
        job_id: inspectedJobId,
        status: 'failed',
        task_type: 'reindex_memory',
        memory_id: 42,
        reason: 'inspect-reason',
      },
    };
    api.getObservabilitySummary.mockResolvedValue(summary);
    api.getIndexJob.mockImplementation(async (jobId) => ({ job: jobDetails[jobId] }));

    const user = userEvent.setup();
    render(<ObservabilityPage />);

    await screen.findByText(/reason:\s*active-reason/i);
    const inspectCard = await getJobCardById(inspectedJobId);
    await user.click(within(inspectCard).getByRole('button', { name: 'Inspect' }));
    await screen.findByText(/reason:\s*inspect-reason/i);

    expect(api.getIndexJob).toHaveBeenCalledWith(inspectedJobId);

    await user.click(screen.getByRole('button', { name: 'Back to Active' }));
    await screen.findByText(/reason:\s*active-reason/i);
    expect(api.getIndexJob).toHaveBeenLastCalledWith(activeJobId);
  });

  describe('AggregatedHealthPanel', () => {
    it('renders healthy state when system is not degraded', async () => {
      api.getObservabilitySummary.mockResolvedValueOnce(
        buildSummary()
      );

      render(<ObservabilityPage />);

      const panel = await screen.findByTestId('aggregated-health-panel');
      expect(panel).toBeInTheDocument();
      expect(within(panel).getByText('System Health Overview')).toBeInTheDocument();
      expect(within(panel).getByText('All systems operational')).toBeInTheDocument();
      expect(within(panel).queryByText('Degradation Reasons')).not.toBeInTheDocument();
      expect(within(panel).queryByRole('button', { name: /Reindex/i })).not.toBeInTheDocument();
    });

    it('renders degraded state with degrade reasons when index is degraded', async () => {
      const summary = buildSummary();
      summary.status = 'degraded';
      summary.health.index = {
        degraded: true,
        reason: 'query_preprocess_failed',
      };
      api.getObservabilitySummary.mockResolvedValueOnce(summary);

      render(<ObservabilityPage />);

      const panel = await screen.findByTestId('aggregated-health-panel');
      expect(within(panel).getByText('Partial degradation detected')).toBeInTheDocument();
      expect(within(panel).getByText('Degradation Reasons')).toBeInTheDocument();
      expect(within(panel).getByText(/query preprocess failed/i)).toBeInTheDocument();
    });

    it('renders severe state with mixed dimensions warning and reindex button', async () => {
      const user = userEvent.setup();
      const summary = buildSummary();
      summary.status = 'degraded';
      summary.health.index = {
        degraded: true,
        reason: 'embedding_dim_mismatch_requires_reindex',
        semantic_vector_block_reason: 'embedding_dim_mismatch_requires_reindex',
        semantic_vector_detected_dims: [384, 768],
      };
      api.getObservabilitySummary.mockResolvedValueOnce(summary);

      render(<ObservabilityPage />);

      const panel = await screen.findByTestId('aggregated-health-panel');
      expect(within(panel).getByText(/Severe degradation/i)).toBeInTheDocument();
      expect(within(panel).getByTestId('health-mixed-dims-warning')).toBeInTheDocument();
      expect(within(panel).getByText(/Mixed embedding dimensions detected/i)).toBeInTheDocument();
      expect(within(panel).getByTestId('health-detected-dims')).toHaveTextContent('384, 768');
      expect(within(panel).getByTestId('health-block-reason')).toHaveTextContent(
        'embedding_dim_mismatch_requires_reindex'
      );

      const reindexBtn = within(panel).getByRole('button', { name: /Reindex All Memories/i });
      expect(reindexBtn).toBeInTheDocument();

      api.getObservabilitySummary.mockResolvedValueOnce(buildSummary());
      await user.click(reindexBtn);

      await waitFor(() => {
        expect(api.triggerIndexRebuild).toHaveBeenCalledWith({
          reason: 'health_panel_mixed_dims_reindex',
          wait: false,
        });
      });
    });

    it('renders top degrade reasons from search stats', async () => {
      const summary = buildSummary();
      summary.search_stats.top_degrade_reasons = [
        { reason: 'intent_classification_unavailable', count: 5 },
        { reason: 'query_preprocess_unavailable', count: 3 },
      ];
      api.getObservabilitySummary.mockResolvedValueOnce(summary);

      render(<ObservabilityPage />);

      const panel = await screen.findByTestId('aggregated-health-panel');
      expect(within(panel).getByText(/Top Degrade Reasons/i)).toBeInTheDocument();
      expect(within(panel).getByText(/intent classification unavailable: 5/i)).toBeInTheDocument();
      expect(within(panel).getByText(/query preprocess unavailable: 3/i)).toBeInTheDocument();
    });
  });
});

import React, { useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import Badge from './components/Badge';
import GlassCard from '../../components/GlassCard';
import {
  AlertTriangle,
  Radar,
  RefreshCw,
  TimerReset,
  Wrench,
} from 'lucide-react';
import { formatDateTime as formatDateTimeValue, formatNumber as formatNumberValue } from '../../lib/format';
import AggregatedHealthPanel from './components/AggregatedHealthPanel';
import TransportDiagnosticsPanel from './components/TransportDiagnosticsPanel';
import SearchTracePanel from './components/SearchTracePanel';
import RuntimeQueuePanel from './components/RuntimeQueuePanel';
import JobInspectorPanel from './components/JobInspectorPanel';
import StatCards from './components/StatCards';
import SearchConsoleCard from './components/SearchConsoleCard';
import SearchResultsList from './components/SearchResultsList';
import { useObservabilitySummary } from './hooks/useObservabilitySummary';
import { useObservabilitySearch } from './hooks/useObservabilitySearch';
import { useObservabilityJobs } from './hooks/useObservabilityJobs';
import {
  buildTransportExceptionBreakdownFallback,
  getTransportCauseDetails,
  normalizeTransportExceptionBreakdown,
} from './lib/transportDiagnostics';

const PANEL_CLASS =
  'rounded-2xl border border-[color:var(--palace-line)] bg-[rgba(255,250,244,0.9)] p-4 shadow-[var(--palace-shadow-sm)] backdrop-blur-sm';

const formatNumber = (value, lng) => {
  if (value === null || value === undefined || Number.isNaN(Number(value))) {
    return '-';
  }
  return formatNumberValue(value, lng) || '-';
};

const formatMs = (value) => {
  if (value === null || value === undefined || Number.isNaN(Number(value))) {
    return '-';
  }
  return `${Number(value).toFixed(1)} ms`;
};

const formatDateTime = (value, lng) => {
  if (!value || typeof value !== 'string') {
    return '-';
  }
  return formatDateTimeValue(value, lng, {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }) || value;
};

const formatMetricLabel = (value) =>
  String(value || '')
    .replace(/_/g, ' ')
    .trim();

const getMetricEntries = (metrics) =>
  Object.entries(metrics || {}).filter(([, value]) => {
    if (value === null || value === undefined) return false;
    if (typeof value === 'object' && !Array.isArray(value)) {
      return Object.keys(value).length > 0;
    }
    return true;
  });

const formatTraceValue = (value, key = '') => {
  if (value === null || value === undefined || value === '') {
    return '-';
  }
  if (typeof value === 'boolean') {
    return value ? 'true' : 'false';
  }
  if (typeof value === 'number') {
    const normalizedKey = String(key || '').toLowerCase();
    if (normalizedKey.endsWith('ms') || normalizedKey.includes('latency')) {
      return formatMs(value);
    }
    if (Number.isInteger(value)) {
      return formatNumber(value);
    }
    return String(Number(value).toFixed(3)).replace(/\.?0+$/, '');
  }
  return String(value);
};

const formatTraceSummaryValue = (value, key = '') => {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    return formatTraceValue(value, key);
  }

  const parts = [];
  if (value.last !== undefined) {
    parts.push(`last ${formatTraceValue(value.last, key)}`);
  }
  if (value.avg !== undefined) {
    parts.push(`avg ${formatTraceValue(value.avg, key)}`);
  }
  if (value.p95 !== undefined) {
    parts.push(`p95 ${formatTraceValue(value.p95, key)}`);
  }
  if (value.max !== undefined) {
    parts.push(`max ${formatTraceValue(value.max, key)}`);
  }
  if (Array.isArray(value.top_values) && value.top_values.length > 0) {
    parts.push(
      value.top_values
        .map((item) => `${item.value} x${formatNumber(item.count)}`)
        .join(', ')
    );
  }
  if (value.samples !== undefined) {
    parts.push(`n ${formatNumber(value.samples)}`);
  }
  return parts.join(' · ') || '-';
};

const getJobStatusTone = (status) => {
  if (status === 'succeeded') return 'good';
  if (status === 'failed' || status === 'dropped') return 'danger';
  if (status === 'cancelled' || status === 'cancelling') return 'warn';
  return 'neutral';
};

function TraceMetricSection({ title, metrics, summary = false, numbersAsMs = false }) {
  const entries = getMetricEntries(metrics);
  if (entries.length === 0) {
    return null;
  }

  return (
    <div>
      <div className="mb-2 text-[11px] uppercase tracking-[0.14em] text-[color:var(--palace-muted)]">
        {title}
      </div>
      <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
        {entries.map(([key, value]) => {
          const metricKey = numbersAsMs ? `${key}_ms` : key;
          return (
          <div
            key={key}
            className="rounded-lg border border-[color:var(--palace-line)] bg-[rgba(255,250,244,0.72)] px-3 py-2"
          >
            <div className="text-[11px] uppercase tracking-[0.12em] text-[color:var(--palace-muted)]">
              {formatMetricLabel(key)}
            </div>
            <div className="mt-1 text-xs text-[color:var(--palace-ink)]">
              {summary
                ? formatTraceSummaryValue(value, metricKey)
                : formatTraceValue(value, metricKey)}
            </div>
          </div>
          );
        })}
      </div>
    </div>
  );
}

export default function ObservabilityPage() {
  const { t, i18n } = useTranslation();

  // --- Hooks ---
  const { summary, summaryLoading, summaryError, summaryTimestamp, loadSummary } =
    useObservabilitySummary();

  const {
    rebuilding,
    rebuildMessage,
    setRebuildMessage,
    sleepConsolidating,
    healthReindexing,
    jobActionKey,
    activeJob,
    activeJobLoading,
    detailJobError,
    inspectedJobId,
    detailJobId,
    activeJobId,
    handleCancelJob,
    handleRetryJob,
    handleRebuild,
    handleSleepConsolidation,
    handleHealthReindex,
    setInspectedJobId,
  } = useObservabilityJobs({ summary, summaryTimestamp, loadSummary });

  const { form, searching, searchError, searchResult, onFieldChange, runSearch } =
    useObservabilitySearch({ loadSummary, setRebuildMessage });

  // --- Derived values ---
  const searchStats = summary?.search_stats || {};
  const health = summary?.health || {};
  const indexHealth = health.index || {};
  const runtime = health.runtime || {};
  const worker = runtime.index_worker || {};
  const writeLanes = runtime.write_lanes || {};
  const sleepConsolidation = runtime.sleep_consolidation || summary?.sleep_consolidation || {};
  const smLite = runtime.sm_lite || {};
  const smSession = smLite.session_cache || {};
  const smFlush = smLite.flush_tracker || {};
  const cleanupQueryStats = summary?.cleanup_query_stats || {};
  const searchTraceSummary = searchStats.search_trace || {};
  const searchTrace = searchResult?.search_trace || {};
  const transport = summary?.transport || {};
  const transportDiagnostics = transport.diagnostics || {};
  const transportLastReport = transport.last_report || null;
  const transportInstances = Array.isArray(transport.instances) ? transport.instances : [];
  const transportLastReportChecks = Array.isArray(transportLastReport?.checks) ? transportLastReport.checks : [];
  const showTransportInstances =
    transportInstances.length > 1 ||
    (transportInstances.length === 1 && (Boolean(transport.degraded) || transport.status === 'fail'));
  const visibleTransportInstances = transportInstances.slice(0, 6);
  const hiddenTransportInstanceCount = Math.max(transportInstances.length - visibleTransportInstances.length, 0);
  const visibleTransportChecks = transportLastReportChecks.slice(0, 4);
  const hiddenTransportCheckCount = Math.max(transportLastReportChecks.length - visibleTransportChecks.length, 0);
  const recentJobs = Array.isArray(worker.recent_jobs) ? worker.recent_jobs : [];
  const viewingActiveJob = Boolean(detailJobId && activeJobId && detailJobId === activeJobId);
  const recentTraceEvents = useMemo(() => {
    const events = searchTraceSummary.recent_events;
    return Array.isArray(events) ? events : [];
  }, [searchTraceSummary.recent_events]);
  const recentTransportEvents = useMemo(() => {
    const events = transportDiagnostics.recent_events;
    return Array.isArray(events) ? events : [];
  }, [transportDiagnostics.recent_events]);
  const transportExceptionBreakdown = useMemo(() => {
    return (
      normalizeTransportExceptionBreakdown(transportDiagnostics.exception_breakdown) ||
      buildTransportExceptionBreakdownFallback({
        diagnostics: transportDiagnostics,
        lastReportChecks: transportLastReportChecks,
        activeTransport: transport?.active_transport,
        updatedAt: transport?.updated_at,
      })
    );
  }, [
    transportDiagnostics,
    transportLastReportChecks,
    transport?.active_transport,
    transport?.updated_at,
  ]);
  const searchTraceBackendMethods = useMemo(() => {
    const breakdown = searchTraceSummary.backend_method_breakdown;
    return Object.entries(breakdown || {});
  }, [searchTraceSummary.backend_method_breakdown]);
  const writeLaneUtilization = useMemo(() => {
    const active = Number(writeLanes.global_active || 0);
    const concurrency = Number(writeLanes.global_concurrency || 0);
    if (concurrency <= 0) return 0;
    return active / concurrency;
  }, [writeLanes.global_active, writeLanes.global_concurrency]);
  const writeLaneMetrics = useMemo(
    () => ({
      global_concurrency: writeLanes.global_concurrency,
      global_active: writeLanes.global_active,
      global_waiting: writeLanes.global_waiting,
      session_waiting_count: writeLanes.session_waiting_count,
      session_waiting_sessions: writeLanes.session_waiting_sessions,
      max_session_waiting: writeLanes.max_session_waiting,
      wait_warn_ms: writeLanes.wait_warn_ms,
      writes_total: writeLanes.writes_total,
      writes_success: writeLanes.writes_success,
      writes_failed: writeLanes.writes_failed,
      failure_rate: writeLanes.failure_rate,
      session_wait_ms_p95: writeLanes.session_wait_ms_p95,
      global_wait_ms_p95: writeLanes.global_wait_ms_p95,
      duration_ms_p95: writeLanes.duration_ms_p95,
    }),
    [
      writeLanes.global_concurrency,
      writeLanes.global_active,
      writeLanes.global_waiting,
      writeLanes.session_waiting_count,
      writeLanes.session_waiting_sessions,
      writeLanes.max_session_waiting,
      writeLanes.wait_warn_ms,
      writeLanes.writes_total,
      writeLanes.writes_success,
      writeLanes.writes_failed,
      writeLanes.failure_rate,
      writeLanes.session_wait_ms_p95,
      writeLanes.global_wait_ms_p95,
      writeLanes.duration_ms_p95,
    ]
  );
  const writeLaneTone =
    Number(writeLanes.writes_failed || 0) > 0 ||
    Number(writeLanes.failure_rate || 0) > 0 ||
    Boolean(writeLanes.last_error) ||
    Number(writeLanes.global_waiting || 0) > 0 ||
    Number(writeLanes.session_waiting_count || 0) > 0 ||
    writeLaneUtilization >= 0.8
      ? 'warn'
      : 'good';
  const hasWriteLaneMetrics = getMetricEntries(writeLaneMetrics).length > 0;
  const hasSearchTraceSummary =
    searchTraceBackendMethods.length > 0 ||
    getMetricEntries({
      candidate_multiplier_requested: searchTraceSummary.candidate_multiplier_requested,
      candidate_multiplier_applied: searchTraceSummary.candidate_multiplier_applied,
    }).length > 0 ||
    getMetricEntries(searchTraceSummary.stage_timings_ms).length > 0 ||
    getMetricEntries(searchTraceSummary.candidate_counts).length > 0 ||
    getMetricEntries(searchTraceSummary.mmr).length > 0 ||
    getMetricEntries(searchTraceSummary.rerank).length > 0 ||
    getMetricEntries(searchTraceSummary.vector_engine).length > 0 ||
    recentTraceEvents.length > 0;
  const hasLatestSearchTrace =
    Boolean(searchTrace.backend_method) ||
    searchTrace.candidate_multiplier_requested !== undefined ||
    searchTrace.candidate_multiplier_applied !== undefined ||
    getMetricEntries(searchTrace.stage_timings_ms).length > 0 ||
    getMetricEntries(searchTrace.candidate_counts).length > 0 ||
    getMetricEntries(searchTrace.mmr).length > 0 ||
    getMetricEntries(searchTrace.rerank).length > 0 ||
    getMetricEntries(searchTrace.vector_engine).length > 0;
  const transportExceptionCategoryEntries = Object.entries(
    transportExceptionBreakdown?.category_counts || {}
  ).slice(0, 4);
  const transportExceptionToolEntries = Object.entries(
    transportExceptionBreakdown?.tool_counts || {}
  ).slice(0, 4);
  const transportExceptionItems = Array.isArray(transportExceptionBreakdown?.items)
    ? transportExceptionBreakdown.items
    : [];
  const visibleTransportExceptionItems = transportExceptionItems.slice(0, 5);
  const hiddenTransportExceptionCount = Math.max(
    transportExceptionItems.length - visibleTransportExceptionItems.length,
    0
  );
  const transportSignatureEntries = Object.entries(
    transportExceptionBreakdown?.signature_breakdown?.signature_counts || {}
  ).slice(0, 4);
  const transportSignatureItems = Array.isArray(
    transportExceptionBreakdown?.signature_breakdown?.items
  )
    ? transportExceptionBreakdown.signature_breakdown.items
    : [];
  const visibleTransportSignatureItems = transportSignatureItems.slice(0, 3);
  const hiddenTransportSignatureCount = Math.max(
    transportSignatureItems.length - visibleTransportSignatureItems.length,
    0
  );
  const transportIncidentEntries = Object.entries(
    transportExceptionBreakdown?.incident_breakdown?.canonical_cause_counts || {}
  ).slice(0, 4);
  const transportIncidentItems = Array.isArray(
    transportExceptionBreakdown?.incident_breakdown?.items
  )
    ? transportExceptionBreakdown.incident_breakdown.items
    : [];
  const visibleTransportIncidentItems = transportIncidentItems.slice(0, 10);
  const hiddenTransportIncidentCount = Math.max(
    transportIncidentItems.length - visibleTransportIncidentItems.length,
    0
  );

  const modeBreakdown = useMemo(() => {
    const breakdown = searchStats.mode_breakdown || {};
    return Object.entries(breakdown);
  }, [searchStats.mode_breakdown]);

  const intentBreakdown = useMemo(() => {
    const breakdown = searchStats.intent_breakdown || {};
    return Object.entries(breakdown);
  }, [searchStats.intent_breakdown]);

  const strategyBreakdown = useMemo(() => {
    const breakdown = searchStats.strategy_hit_breakdown || {};
    return Object.entries(breakdown);
  }, [searchStats.strategy_hit_breakdown]);

  return (
    <div className="palace-harmonized flex h-full flex-col overflow-hidden bg-[color:var(--palace-bg)] text-[color:var(--palace-ink)] selection:bg-[rgba(179,133,79,0.28)] selection:text-[color:var(--palace-ink)]">
      <header className="border-b border-[color:var(--palace-line)] bg-[radial-gradient(circle_at_top_right,rgba(198,165,126,0.24),rgba(241,232,220,0.72),rgba(246,242,234,0.92)_58%)] px-6 py-5">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <h1 className="font-display flex items-center gap-2 text-lg text-[color:var(--palace-ink)]">
              <Radar size={18} className="text-[color:var(--palace-accent)]" />
              {t('observability.title')}
            </h1>
            <p className="mt-1 text-sm text-[color:var(--palace-muted)]">
              {t('observability.subtitle')}
            </p>
          </div>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={loadSummary}
              disabled={summaryLoading}
              className="inline-flex cursor-pointer items-center gap-2 rounded-lg border border-[color:var(--palace-line)] bg-white/88 px-3 py-2 text-xs font-medium text-[color:var(--palace-muted)] transition-colors hover:border-[color:var(--palace-accent)] hover:text-[color:var(--palace-ink)] disabled:cursor-not-allowed disabled:opacity-50 focus:outline-none focus:ring-2 focus:ring-[color:var(--palace-accent)]/35"
            >
              <RefreshCw size={14} className={summaryLoading ? 'animate-spin' : ''} />
              {t('observability.refresh')}
            </button>
            <button
              type="button"
              onClick={handleRebuild}
              disabled={rebuilding}
              className="inline-flex cursor-pointer items-center gap-2 rounded-lg border border-[color:var(--palace-accent)] bg-[linear-gradient(135deg,rgba(198,165,126,0.38),rgba(255,250,244,0.9))] px-3 py-2 text-xs font-medium text-[color:var(--palace-ink)] transition-colors hover:border-[color:var(--palace-accent-2)] hover:bg-[linear-gradient(135deg,rgba(190,154,112,0.42),rgba(255,250,244,0.95))] disabled:cursor-not-allowed disabled:opacity-50 focus:outline-none focus:ring-2 focus:ring-[color:var(--palace-accent)]/35"
            >
              {rebuilding ? (
                <RefreshCw size={14} className="animate-spin" />
              ) : (
                <Wrench size={14} />
              )}
              {t('observability.rebuildIndex')}
            </button>
            <button
              type="button"
              onClick={handleSleepConsolidation}
              disabled={sleepConsolidating}
              className="inline-flex cursor-pointer items-center gap-2 rounded-lg border border-[color:var(--palace-line)] bg-white/88 px-3 py-2 text-xs font-medium text-[color:var(--palace-muted)] transition-colors hover:border-[color:var(--palace-accent)] hover:text-[color:var(--palace-ink)] disabled:cursor-not-allowed disabled:opacity-50 focus:outline-none focus:ring-2 focus:ring-[color:var(--palace-accent)]/35"
            >
              {sleepConsolidating ? (
                <RefreshCw size={14} className="animate-spin" />
              ) : (
                <TimerReset size={14} />
              )}
              {t('observability.sleepConsolidation')}
            </button>
          </div>
        </div>
        {rebuildMessage && (
          <p className="mt-3 text-xs text-[color:var(--palace-muted)]">{rebuildMessage}</p>
        )}
        {summaryError && (
          <div className="mt-3 inline-flex items-center gap-2 rounded-md border border-[rgba(143,106,69,0.45)] bg-[rgba(232,218,198,0.88)] px-3 py-2 text-xs text-[color:var(--palace-accent-2)]">
            <AlertTriangle size={13} />
            {summaryError}
            <button
              type="button"
              onClick={loadSummary}
              className="ml-1 rounded px-1.5 py-0.5 text-[10px] font-medium underline hover:no-underline"
            >
              {t('common.actions.retry')}
            </button>
          </div>
        )}
      </header>

      <main className="flex-1 overflow-y-auto px-6 py-5">
        {summary && (
          <AggregatedHealthPanel
            summary={summary}
            onReindex={handleHealthReindex}
            reindexing={healthReindexing}
          />
        )}

        <StatCards summary={summary} formatNumber={formatNumber} formatMs={formatMs} />

        <section className="grid gap-4 xl:grid-cols-[360px_1fr]">
          <div className="space-y-4">
            <SearchConsoleCard
              form={form}
              onFieldChange={onFieldChange}
              runSearch={runSearch}
              searching={searching}
              searchError={searchError}
              t={t}
            />

            <RuntimeQueuePanel
              summary={summary}
              indexHealth={indexHealth}
              worker={worker}
              writeLanes={writeLanes}
              sleepConsolidation={sleepConsolidation}
              smLite={smLite}
              smSession={smSession}
              smFlush={smFlush}
              cleanupQueryStats={cleanupQueryStats}
              writeLaneUtilization={writeLaneUtilization}
              writeLaneMetrics={writeLaneMetrics}
              writeLaneTone={writeLaneTone}
              hasWriteLaneMetrics={hasWriteLaneMetrics}
              formatNumber={formatNumber}
              TraceMetricSection={TraceMetricSection}
            />

            <TransportDiagnosticsPanel
              transport={transport}
              transportDiagnostics={transportDiagnostics}
              transportLastReport={transportLastReport}
              transportLastReportChecks={transportLastReportChecks}
              transportExceptionBreakdown={transportExceptionBreakdown}
              transportExceptionCategoryEntries={transportExceptionCategoryEntries}
              transportExceptionToolEntries={transportExceptionToolEntries}
              transportExceptionItems={transportExceptionItems}
              visibleTransportExceptionItems={visibleTransportExceptionItems}
              hiddenTransportExceptionCount={hiddenTransportExceptionCount}
              transportSignatureEntries={transportSignatureEntries}
              visibleTransportSignatureItems={visibleTransportSignatureItems}
              hiddenTransportSignatureCount={hiddenTransportSignatureCount}
              transportIncidentEntries={transportIncidentEntries}
              visibleTransportIncidentItems={visibleTransportIncidentItems}
              hiddenTransportIncidentCount={hiddenTransportIncidentCount}
              showTransportInstances={showTransportInstances}
              visibleTransportInstances={visibleTransportInstances}
              hiddenTransportInstanceCount={hiddenTransportInstanceCount}
              visibleTransportChecks={visibleTransportChecks}
              hiddenTransportCheckCount={hiddenTransportCheckCount}
              recentTransportEvents={recentTransportEvents}
              formatDateTime={formatDateTime}
              formatTraceSummaryValue={formatTraceSummaryValue}
              getTransportCauseDetails={getTransportCauseDetails}
            />

            <JobInspectorPanel
              activeJobLoading={activeJobLoading}
              detailJobError={detailJobError}
              detailJobId={detailJobId}
              activeJob={activeJob}
              activeJobId={activeJobId}
              viewingActiveJob={viewingActiveJob}
              inspectedJobId={inspectedJobId}
              setInspectedJobId={setInspectedJobId}
              jobActionKey={jobActionKey}
              recentJobs={recentJobs}
              handleCancelJob={handleCancelJob}
              handleRetryJob={handleRetryJob}
              getJobStatusTone={getJobStatusTone}
              formatDateTime={formatDateTime}
            />

            {modeBreakdown.length > 0 && (
              <div className={PANEL_CLASS}>
                <h3 className="mb-3 text-sm font-semibold text-[color:var(--palace-ink)]">{t('observability.breakdown.mode')}</h3>
                <div className="flex flex-wrap gap-2">
                  {modeBreakdown.map(([mode, count]) => (
                    <Badge key={mode} tone="neutral">
                      {mode}: {count}
                    </Badge>
                  ))}
                </div>
              </div>
            )}

            {intentBreakdown.length > 0 && (
              <div className={PANEL_CLASS}>
                <h3 className="mb-3 text-sm font-semibold text-[color:var(--palace-ink)]">{t('observability.breakdown.intent')}</h3>
                <div className="flex flex-wrap gap-2">
                  {intentBreakdown.map(([intent, count]) => (
                    <Badge key={intent} tone="neutral">
                      {intent}: {count}
                    </Badge>
                  ))}
                </div>
              </div>
            )}

            {strategyBreakdown.length > 0 && (
              <div className={PANEL_CLASS}>
                <h3 className="mb-3 text-sm font-semibold text-[color:var(--palace-ink)]">{t('observability.breakdown.strategy')}</h3>
                <div className="flex flex-wrap gap-2">
                  {strategyBreakdown.map(([strategy, count]) => (
                    <Badge key={strategy} tone="neutral">
                      {strategy}: {count}
                    </Badge>
                  ))}
                </div>
              </div>
            )}
          </div>

          <div className="space-y-4">
            <div className={PANEL_CLASS}>
              <h2 className="mb-3 text-sm font-semibold text-[color:var(--palace-ink)]">{t('observability.searchDiagnostics')}</h2>
              {!searchResult ? (
                <p className="text-sm text-[color:var(--palace-muted)]">
                  {t('observability.noSearchRun')}
                </p>
              ) : (
                <div className="space-y-3 text-xs text-[color:var(--palace-muted)]">
                  <div className="flex flex-wrap gap-2">
                    <Badge tone="neutral">{t('observability.diagnostics.latency', { value: formatMs(searchResult.latency_ms) })}</Badge>
                    <Badge tone="neutral">{t('observability.diagnostics.mode', { value: searchResult.mode_applied })}</Badge>
                    <Badge tone="neutral">
                      {t('observability.diagnostics.intent', {
                        value: searchResult.intent_applied || searchResult.intent || 'unknown',
                      })}
                    </Badge>
                    <Badge tone="neutral">
                      {t('observability.diagnostics.strategy', {
                        value: searchResult.strategy_template_applied || searchResult.strategy_template || searchResult.intent_profile?.strategy_template || 'default',
                      })}
                    </Badge>
                    <Badge tone={searchResult.degraded ? 'warn' : 'good'}>
                      {t('observability.diagnostics.degraded', { value: String(Boolean(searchResult.degraded)) })}
                    </Badge>
                    <Badge tone="neutral">
                      {t('observability.diagnostics.counts', {
                        session: searchResult.counts?.session ?? 0,
                        global: searchResult.counts?.global ?? 0,
                        returned: searchResult.counts?.returned ?? 0,
                      })}
                    </Badge>
                  </div>
                  {Array.isArray(searchResult.degrade_reasons) && searchResult.degrade_reasons.length > 0 && (
                    <div className="rounded-lg border border-[rgba(198,165,126,0.55)] bg-[rgba(240,230,215,0.78)] p-3">
                      <div className="mb-2 text-[11px] uppercase tracking-[0.14em] text-[color:var(--palace-accent-2)]">
                        {t('observability.diagnostics.degradeReasons')}
                      </div>
                      <div className="flex flex-wrap gap-2">
                        {searchResult.degrade_reasons.map((reason) => (
                          <Badge key={reason} tone="warn">
                            {reason}
                          </Badge>
                        ))}
                      </div>
                    </div>
                  )}
                </div>
              )}
            </div>

            <SearchTracePanel
              hasSearchTraceSummary={hasSearchTraceSummary}
              hasLatestSearchTrace={hasLatestSearchTrace}
              searchTraceSummary={searchTraceSummary}
              searchTraceBackendMethods={searchTraceBackendMethods}
              searchTrace={searchTrace}
              recentTraceEvents={recentTraceEvents}
              formatTraceValue={formatTraceValue}
              formatDateTime={formatDateTime}
              formatMs={formatMs}
              TraceMetricSection={TraceMetricSection}
            />

            <SearchResultsList
              searchResult={searchResult}
              searching={searching}
              t={t}
            />
          </div>
        </section>
      </main>
    </div>
  );
}

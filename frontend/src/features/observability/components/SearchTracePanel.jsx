import React from 'react';
import { useTranslation } from 'react-i18next';
import Badge, { MemoTraceBadgeList as TraceBadgeList } from './Badge';
import {
  localizeObservabilityStatus,
} from '../observabilityI18n';

const PANEL_CLASS =
  'rounded-2xl border border-[color:var(--palace-line)] bg-[rgba(255,250,244,0.9)] p-4 shadow-[var(--palace-shadow-sm)] backdrop-blur-sm';

/**
 * SearchTracePanel renders the search trace rollups and details section.
 *
 * All data is passed as props from the parent ObservabilityPage which owns
 * the state and memoized values.
 */
function SearchTracePanel({
  hasSearchTraceSummary,
  hasLatestSearchTrace,
  searchTraceSummary,
  searchTraceBackendMethods,
  searchTrace,
  recentTraceEvents,
  formatTraceValue,
  formatDateTime,
  formatMs,
  TraceMetricSection,
}) {
  const { t, i18n } = useTranslation();

  return (
    <div className={PANEL_CLASS}>
      <h2 className="mb-3 text-sm font-semibold text-[color:var(--palace-ink)]">
        {t('observability.searchTrace.title')}
      </h2>
      {!hasSearchTraceSummary && !hasLatestSearchTrace ? (
        <p className="text-sm text-[color:var(--palace-muted)]">
          {t('observability.searchTrace.empty')}
        </p>
      ) : (
        <div className="space-y-3 text-xs text-[color:var(--palace-muted)]">
          {hasSearchTraceSummary && (
            <div className="rounded-lg border border-[color:var(--palace-line)] bg-[rgba(255,250,244,0.72)] p-3">
              <div className="mb-2 text-[11px] uppercase tracking-[0.14em] text-[color:var(--palace-muted)]">
                {t('observability.searchTrace.summaryWindow')}
              </div>
              <div className="space-y-3">
                <TraceBadgeList
                  title={t('observability.searchTrace.backendMethodBreakdown')}
                  entries={searchTraceBackendMethods}
                />
                <TraceMetricSection
                  title={t('observability.searchTrace.candidateMultipliers')}
                  metrics={{
                    candidate_multiplier_requested:
                      searchTraceSummary.candidate_multiplier_requested,
                    candidate_multiplier_applied:
                      searchTraceSummary.candidate_multiplier_applied,
                  }}
                  summary
                />
                <TraceMetricSection
                  title={t('observability.searchTrace.stageTimings')}
                  metrics={searchTraceSummary.stage_timings_ms}
                  summary
                  numbersAsMs
                />
                <TraceMetricSection
                  title={t('observability.searchTrace.candidateCounts')}
                  metrics={searchTraceSummary.candidate_counts}
                  summary
                />
                <TraceMetricSection
                  title={t('observability.searchTrace.mmr')}
                  metrics={searchTraceSummary.mmr}
                  summary
                />
                <TraceMetricSection
                  title={t('observability.searchTrace.rerank')}
                  metrics={searchTraceSummary.rerank}
                  summary
                />
                <TraceMetricSection
                  title={t('observability.searchTrace.vectorEngine')}
                  metrics={searchTraceSummary.vector_engine}
                  summary
                />
              </div>
            </div>
          )}

          {hasLatestSearchTrace ? (
            <div className="rounded-lg border border-[color:var(--palace-line)] bg-[rgba(255,250,244,0.72)] p-3">
              <div className="mb-2 text-[11px] uppercase tracking-[0.14em] text-[color:var(--palace-muted)]">
                {t('observability.searchTrace.latestSearch')}
              </div>
              <div className="mb-3 flex flex-wrap gap-2">
                {searchTrace.backend_method && (
                  <Badge tone="neutral">
                    {t('observability.searchTrace.backendMethod', {
                      value: searchTrace.backend_method,
                    })}
                  </Badge>
                )}
                {searchTrace.candidate_multiplier_requested !== undefined && (
                  <Badge tone="neutral">
                    {t('observability.searchTrace.requested', {
                      value: formatTraceValue(
                        searchTrace.candidate_multiplier_requested,
                        'candidate_multiplier_requested'
                      ),
                    })}
                  </Badge>
                )}
                {searchTrace.candidate_multiplier_applied !== undefined && (
                  <Badge tone="neutral">
                    {t('observability.searchTrace.applied', {
                      value: formatTraceValue(
                        searchTrace.candidate_multiplier_applied,
                        'candidate_multiplier_applied'
                      ),
                    })}
                  </Badge>
                )}
              </div>
              <div className="space-y-3">
                <TraceMetricSection
                  title={t('observability.searchTrace.stageTimings')}
                  metrics={searchTrace.stage_timings_ms}
                  numbersAsMs
                />
                <TraceMetricSection
                  title={t('observability.searchTrace.candidateCounts')}
                  metrics={searchTrace.candidate_counts}
                />
                <TraceMetricSection
                  title={t('observability.searchTrace.mmr')}
                  metrics={searchTrace.mmr}
                />
                <TraceMetricSection
                  title={t('observability.searchTrace.rerank')}
                  metrics={searchTrace.rerank}
                />
                <TraceMetricSection
                  title={t('observability.searchTrace.vectorEngine')}
                  metrics={searchTrace.vector_engine}
                />
              </div>
            </div>
          ) : (
            <p>{t('observability.searchTrace.noLatestSearch')}</p>
          )}

          {recentTraceEvents.length > 0 && (
            <div className="rounded-lg border border-[color:var(--palace-line)] bg-[rgba(255,250,244,0.72)] p-3">
              <div className="mb-2 text-[11px] uppercase tracking-[0.14em] text-[color:var(--palace-muted)]">
                {t('observability.searchTrace.recentEvents')}
              </div>
              <div className="space-y-2">
                {recentTraceEvents.map((event, index) => (
                  <div
                    key={`${event.timestamp || 'search-trace'}-${index}`}
                    className="rounded border border-[color:var(--palace-line)] bg-white/70 px-2 py-2"
                  >
                    <div className="flex flex-wrap gap-2">
                      <Badge tone={event.degraded ? 'warn' : 'good'}>
                        {t('observability.diagnostics.degraded', {
                          value: String(Boolean(event.degraded)),
                        })}
                      </Badge>
                      <Badge tone="neutral">
                        {t('observability.diagnostics.mode', {
                          value: event.mode_applied || event.mode_requested || 'unknown',
                        })}
                      </Badge>
                      <Badge tone="neutral">
                        {t('observability.diagnostics.intent', {
                          value: event.intent_applied || event.intent || 'unknown',
                        })}
                      </Badge>
                      {event.search_trace?.backend_method && (
                        <Badge tone="neutral">
                          {t('observability.searchTrace.backendMethod', {
                            value: event.search_trace.backend_method,
                          })}
                        </Badge>
                      )}
                      <Badge tone="neutral">
                        {t('observability.diagnostics.latency', {
                          value: formatMs(event.latency_ms),
                        })}
                      </Badge>
                    </div>
                    <div className="mt-1 space-y-1">
                      <p>{formatDateTime(event.timestamp, i18n.resolvedLanguage)}</p>
                      <p>
                        {t('observability.diagnostics.counts', {
                          session: event.session_count ?? 0,
                          global: event.global_count ?? 0,
                          returned: event.returned_count ?? 0,
                        })}
                      </p>
                      {Array.isArray(event.degrade_reasons) &&
                        event.degrade_reasons.length > 0 && (
                          <div className="flex flex-wrap gap-2">
                            {event.degrade_reasons.map((reason) => (
                              <Badge key={reason} tone="warn">
                                {reason}
                              </Badge>
                            ))}
                          </div>
                        )}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export default React.memo(SearchTracePanel);

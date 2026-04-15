import React from 'react';
import { useTranslation } from 'react-i18next';
import { CheckCircle2, AlertTriangle, Database, Gauge } from 'lucide-react';
import Badge from './Badge';
import {
  localizeObservabilityStatus,
} from '../observabilityI18n';

const PANEL_CLASS =
  'rounded-2xl border border-[color:var(--palace-line)] bg-[rgba(255,250,244,0.9)] p-4 shadow-[var(--palace-shadow-sm)] backdrop-blur-sm';

/**
 * RuntimeQueuePanel renders the runtime snapshot and write lanes panels.
 *
 * All data is passed as props from the parent ObservabilityPage which owns
 * the state and memoized values.
 */
function RuntimeQueuePanel({
  summary,
  indexHealth,
  worker,
  writeLanes,
  sleepConsolidation,
  smLite,
  smSession,
  smFlush,
  cleanupQueryStats,
  writeLaneUtilization,
  writeLaneMetrics,
  writeLaneTone,
  hasWriteLaneMetrics,
  formatNumber,
  TraceMetricSection,
}) {
  const { t, i18n } = useTranslation();

  return (
    <>
      <div className={PANEL_CLASS}>
        <h3 className="mb-3 flex items-center gap-2 text-sm font-semibold text-[color:var(--palace-ink)]">
          <Database size={15} className="text-[color:var(--palace-accent)]" />
          {t('observability.runtimeSnapshot')}
        </h3>
        <div className="space-y-2 text-xs text-[color:var(--palace-muted)]">
          <p className="flex items-center gap-2">
            {summary?.status === 'ok' ? (
              <CheckCircle2 size={13} className="text-[color:var(--palace-accent)]" />
            ) : (
              <AlertTriangle size={13} className="text-[color:var(--palace-accent-2)]" />
            )}
            {t('observability.runtime.status', {
              value: localizeObservabilityStatus(summary?.status || 'unknown', t),
            })}
          </p>
          <p>{t('observability.runtime.indexDegraded', { value: String(Boolean(indexHealth.degraded)) })}</p>
          <p>{t('observability.runtime.queueDepth', { value: worker.queue_depth ?? '-' })}</p>
          <p>{t('observability.runtime.activeJob', { value: worker.active_job_id || '-' })}</p>
          <p>{t('observability.runtime.cancellingJobs', { value: worker.cancelling_jobs ?? 0 })}</p>
          <p>{t('observability.runtime.lastWorkerError', { value: worker.last_error || '-' })}</p>
          <p>{t('observability.runtime.sleepPending', { value: String(Boolean(worker.sleep_pending)) })}</p>
          <p>{t('observability.runtime.sleepLastReason', { value: sleepConsolidation.reason || '-' })}</p>
          <p>{t('observability.runtime.smLiteSessions', { value: smSession.session_count ?? '-' })}</p>
          <p>{t('observability.runtime.smLitePendingEvents', { value: smFlush.pending_events ?? '-' })}</p>
          <p>{t('observability.runtime.smLiteDegraded', { value: String(Boolean(smLite.degraded)) })}</p>
          <p>{t('observability.runtime.smLiteReason', { value: smLite.reason || '-' })}</p>
          <p>{t('observability.runtime.cleanupQueries', { value: formatNumber(cleanupQueryStats.total_queries, i18n.resolvedLanguage) })}</p>
          <p>{t('observability.runtime.updatedAt', { value: summary?.timestamp || '-' })}</p>
        </div>
      </div>

      <div className={PANEL_CLASS}>
        <h3 className="mb-3 flex items-center gap-2 text-sm font-semibold text-[color:var(--palace-ink)]">
          <Gauge size={15} className="text-[color:var(--palace-accent)]" />
          {t('observability.writeLanes.title')}
        </h3>
        <div className="space-y-3 text-xs text-[color:var(--palace-muted)]">
          <div className="flex flex-wrap gap-2">
            <Badge tone={writeLaneTone}>
              {t('observability.writeLanes.utilization', {
                value: (writeLaneUtilization * 100).toFixed(0) + '%',
              })}
            </Badge>
            {writeLanes.failure_rate !== undefined && (
              <Badge tone={Number(writeLanes.failure_rate || 0) > 0 ? 'warn' : 'neutral'}>
                {t('observability.writeLanes.failureRate', {
                  value: (Number(writeLanes.failure_rate || 0) * 100).toFixed(1) + '%',
                })}
              </Badge>
            )}
            {writeLanes.last_error && (
              <Badge tone="warn">
                {t('observability.writeLanes.lastError', { value: writeLanes.last_error })}
              </Badge>
            )}
          </div>
          {hasWriteLaneMetrics ? (
            <TraceMetricSection
              title={t('observability.writeLanes.metrics')}
              metrics={writeLaneMetrics}
            />
          ) : (
            <p>{t('observability.writeLanes.empty')}</p>
          )}
        </div>
      </div>
    </>
  );
}

export default React.memo(RuntimeQueuePanel);

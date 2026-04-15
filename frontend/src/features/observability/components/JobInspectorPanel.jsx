import React from 'react';
import { useTranslation } from 'react-i18next';
import { AlertTriangle, RefreshCw, Wrench } from 'lucide-react';
import Badge from './Badge';

const PANEL_CLASS =
  'rounded-2xl border border-[color:var(--palace-line)] bg-[rgba(255,250,244,0.9)] p-4 shadow-[var(--palace-shadow-sm)] backdrop-blur-sm';

/**
 * JobInspectorPanel renders the index task queue with job details,
 * cancel/retry/inspect actions.
 *
 * All data is passed as props from the parent ObservabilityPage which owns
 * the state and memoized values.
 */
function JobInspectorPanel({
  activeJobLoading,
  detailJobError,
  detailJobId,
  activeJob,
  activeJobId,
  viewingActiveJob,
  inspectedJobId,
  setInspectedJobId,
  jobActionKey,
  recentJobs,
  handleCancelJob,
  handleRetryJob,
  getJobStatusTone,
  formatDateTime,
}) {
  const { t, i18n } = useTranslation();

  return (
    <div className={PANEL_CLASS}>
      <h3 className="mb-3 flex items-center gap-2 text-sm font-semibold text-[color:var(--palace-ink)]">
        <Wrench size={15} className="text-[color:var(--palace-accent)]" />
        {t('observability.indexTaskQueue')}
      </h3>
      {activeJobLoading && (
        <p className="mb-2 text-xs text-[color:var(--palace-muted)]">
          {t('observability.job.loadingActive')}
        </p>
      )}
      {detailJobError && (
        <p className="mb-2 text-xs text-[color:var(--palace-accent-2)]">
          {detailJobError}
        </p>
      )}
      {detailJobId && activeJob && (
        (() => {
          const jobId = String(activeJob.job_id || detailJobId);
          const status = String(activeJob.status || 'unknown');
          const taskType = String(activeJob.task_type || 'unknown');
          const canCancel = ['queued', 'running', 'cancelling'].includes(status);
          const canRetry = ['failed', 'dropped', 'cancelled'].includes(status);
          const cancelPending = jobActionKey === `cancel:${jobId}`;
          const retryPending = jobActionKey === `retry:${jobId}`;
          const errorText = activeJob?.error || activeJob?.result?.error || '-';
          const degradeReasons = Array.isArray(activeJob?.result?.degrade_reasons)
            ? activeJob.result.degrade_reasons.join(', ')
            : '-';
          return (
            <article className="mb-3 rounded-xl border border-[color:var(--palace-accent)]/45 bg-[rgba(255,248,238,0.9)] p-3 text-xs text-[color:var(--palace-muted)]">
              <div className="mb-2 flex flex-wrap items-center gap-2">
                <Badge tone={viewingActiveJob ? 'good' : 'neutral'}>
                  {viewingActiveJob ? t('observability.job.active') : t('observability.job.detail')}
                </Badge>
                <code className="text-[11px] text-[color:var(--palace-accent-2)]">{jobId}</code>
                <Badge tone={getJobStatusTone(status)}>{status}</Badge>
                <Badge tone="neutral">{taskType}</Badge>
              </div>
              <div className="space-y-1">
                <p>{t('observability.job.reason', { value: activeJob?.reason || '-' })}</p>
                <p>{t('observability.job.memory', { value: activeJob?.memory_id ?? '-' })}</p>
                <p>{t('observability.job.error', { value: errorText })}</p>
                <p>{t('observability.job.cancelReason', { value: activeJob?.cancel_reason || '-' })}</p>
                <p>{t('observability.job.degradeReasons', { value: degradeReasons || '-' })}</p>
                <p>{t('observability.job.requested', { value: formatDateTime(activeJob?.requested_at, i18n.resolvedLanguage) })}</p>
                <p>{t('observability.job.started', { value: formatDateTime(activeJob?.started_at, i18n.resolvedLanguage) })}</p>
                <p>{t('observability.job.finished', { value: formatDateTime(activeJob?.finished_at, i18n.resolvedLanguage) })}</p>
              </div>
              <div className="mt-3 flex flex-wrap gap-2">
                <button
                  type="button"
                  disabled={!canCancel || cancelPending}
                  onClick={() => handleCancelJob(jobId)}
                  className="inline-flex cursor-pointer items-center gap-1 rounded border border-[color:var(--palace-line)] bg-white/90 px-2 py-1 text-[11px] text-[color:var(--palace-muted)] transition-colors hover:border-[color:var(--palace-accent)] hover:text-[color:var(--palace-ink)] disabled:cursor-not-allowed disabled:opacity-45"
                >
                  {cancelPending ? <RefreshCw size={12} className="animate-spin" /> : <AlertTriangle size={12} />}
                  {t('observability.job.cancel')}
                </button>
                <button
                  type="button"
                  disabled={!canRetry || retryPending}
                  onClick={() => handleRetryJob(activeJob)}
                  className="inline-flex cursor-pointer items-center gap-1 rounded border border-[color:var(--palace-line)] bg-white/90 px-2 py-1 text-[11px] text-[color:var(--palace-muted)] transition-colors hover:border-[color:var(--palace-accent)] hover:text-[color:var(--palace-ink)] disabled:cursor-not-allowed disabled:opacity-45"
                >
                  {retryPending ? <RefreshCw size={12} className="animate-spin" /> : <RefreshCw size={12} />}
                  {t('observability.job.retry')}
                </button>
                {inspectedJobId && (
                  <button
                    type="button"
                    onClick={() => setInspectedJobId(null)}
                    className="inline-flex cursor-pointer items-center gap-1 rounded border border-[color:var(--palace-line)] bg-white/90 px-2 py-1 text-[11px] text-[color:var(--palace-muted)] transition-colors hover:border-[color:var(--palace-accent)] hover:text-[color:var(--palace-ink)]"
                  >
                    {activeJobId ? t('observability.job.backToActive') : t('observability.job.clearDetail')}
                  </button>
                )}
              </div>
            </article>
          );
        })()
      )}
      {recentJobs.length === 0 ? (
        <p className="text-xs text-[color:var(--palace-muted)]">
          {t('observability.job.noRecentJobs')}
        </p>
      ) : (
        <div className="space-y-2">
          {recentJobs.map((job) => {
            const jobId = String(job?.job_id || 'unknown-job');
            const status = String(job?.status || 'unknown');
            const taskType = String(job?.task_type || 'unknown');
            const canCancel = ['queued', 'running', 'cancelling'].includes(status);
            const canRetry = ['failed', 'dropped', 'cancelled'].includes(status);
            const cancelPending = jobActionKey === `cancel:${jobId}`;
            const retryPending = jobActionKey === `retry:${jobId}`;
            const errorText = job?.error || job?.result?.error || '-';

            return (
              <article
                key={jobId}
                className="rounded-xl border border-[color:var(--palace-line)] bg-[rgba(255,250,244,0.84)] p-3 text-xs text-[color:var(--palace-muted)]"
              >
                <div className="mb-2 flex flex-wrap items-center gap-2">
                  <code className="text-[11px] text-[color:var(--palace-accent-2)]">{jobId}</code>
                  <Badge tone={getJobStatusTone(status)}>{status}</Badge>
                  <Badge tone="neutral">{taskType}</Badge>
                </div>
                <div className="space-y-1">
                  <p>{t('observability.job.reason', { value: job?.reason || '-' })}</p>
                  <p>{t('observability.job.memory', { value: job?.memory_id ?? '-' })}</p>
                  <p>{t('observability.job.error', { value: errorText })}</p>
                  <p>{t('observability.job.requested', { value: formatDateTime(job?.requested_at, i18n.resolvedLanguage) })}</p>
                  <p>{t('observability.job.started', { value: formatDateTime(job?.started_at, i18n.resolvedLanguage) })}</p>
                  <p>{t('observability.job.finished', { value: formatDateTime(job?.finished_at, i18n.resolvedLanguage) })}</p>
                </div>
                <div className="mt-3 flex flex-wrap gap-2">
                  <button
                    type="button"
                    disabled={!canCancel || cancelPending}
                    onClick={() => handleCancelJob(jobId)}
                    className="inline-flex cursor-pointer items-center gap-1 rounded border border-[color:var(--palace-line)] bg-white/90 px-2 py-1 text-[11px] text-[color:var(--palace-muted)] transition-colors hover:border-[color:var(--palace-accent)] hover:text-[color:var(--palace-ink)] disabled:cursor-not-allowed disabled:opacity-45"
                  >
                    {cancelPending ? <RefreshCw size={12} className="animate-spin" /> : <AlertTriangle size={12} />}
                    {t('observability.job.cancel')}
                  </button>
                  <button
                    type="button"
                    disabled={!canRetry || retryPending}
                    onClick={() => handleRetryJob(job)}
                    className="inline-flex cursor-pointer items-center gap-1 rounded border border-[color:var(--palace-line)] bg-white/90 px-2 py-1 text-[11px] text-[color:var(--palace-muted)] transition-colors hover:border-[color:var(--palace-accent)] hover:text-[color:var(--palace-ink)] disabled:cursor-not-allowed disabled:opacity-45"
                  >
                    {retryPending ? <RefreshCw size={12} className="animate-spin" /> : <RefreshCw size={12} />}
                    {t('observability.job.retry')}
                  </button>
                  <button
                    type="button"
                    onClick={() => setInspectedJobId(jobId)}
                    className="inline-flex cursor-pointer items-center gap-1 rounded border border-[color:var(--palace-line)] bg-white/90 px-2 py-1 text-[11px] text-[color:var(--palace-muted)] transition-colors hover:border-[color:var(--palace-accent)] hover:text-[color:var(--palace-ink)]"
                  >
                    {t('observability.job.inspect')}
                  </button>
                </div>
              </article>
            );
          })}
        </div>
      )}
    </div>
  );
}

export default React.memo(JobInspectorPanel);

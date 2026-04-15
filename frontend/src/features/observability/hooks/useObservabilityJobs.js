import { useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  cancelIndexJob,
  extractApiError,
  getIndexJob,
  retryIndexJob,
  triggerIndexRebuild,
  triggerMemoryReindex,
  triggerSleepConsolidation,
} from '../../../lib/api';

const isRetryEndpointUnsupported = (error) => {
  const statusCode = error?.response?.status;
  if (statusCode === 405) return true;
  if (statusCode !== 404) return false;

  const detail = error?.response?.data?.detail;
  const detailParts = [];
  const pushDetailPart = (value) => {
    if (typeof value !== 'string') return;
    const normalized = value.trim().toLowerCase();
    if (!normalized || detailParts.includes(normalized)) return;
    detailParts.push(normalized);
  };

  if (typeof detail === 'string') {
    pushDetailPart(detail);
  } else if (detail && typeof detail === 'object') {
    pushDetailPart(detail.error);
    pushDetailPart(detail.reason);
    pushDetailPart(detail.message);
    if (detailParts.length === 0) {
      try {
        pushDetailPart(JSON.stringify(detail));
      } catch (_error) {
        // ignore non-serializable details
      }
    }
  }
  const detailText = detailParts.join(' | ');
  const hasNotFoundSignature =
    detailText.includes('not found') || detailText.includes('not_found');
  if (!hasNotFoundSignature) return false;

  // New retry endpoint and old backend route mismatch should fallback to legacy calls.
  // But explicit job-not-found from new backend should not fallback.
  if (detailText.includes('job_not_found')) return false;
  if (detailText.includes('job') && detailText.includes('not found')) return false;
  return true;
};

/**
 * Manages index job actions (rebuild, sleep-consolidation, health-reindex, cancel, retry)
 * and the job detail inspector state.
 *
 * @param {{ summary, summaryTimestamp: string, loadSummary: () => Promise<void> }} deps
 */
export function useObservabilityJobs({ summary, summaryTimestamp, loadSummary }) {
  const { t, i18n } = useTranslation();
  const localeKey = i18n.resolvedLanguage || i18n.language || 'en';

  const [rebuilding, setRebuilding] = useState(false);
  const [rebuildMessage, setRebuildMessage] = useState(null);
  const [sleepConsolidating, setSleepConsolidating] = useState(false);
  const [healthReindexing, setHealthReindexing] = useState(false);
  const [jobActionKey, setJobActionKey] = useState(null);
  const [activeJob, setActiveJob] = useState(null);
  const [activeJobLoading, setActiveJobLoading] = useState(false);
  const [detailJobErrorState, setDetailJobErrorState] = useState(null);
  const [inspectedJobId, setInspectedJobId] = useState(null);

  const activeJobId = summary?.health?.runtime?.index_worker?.active_job_id || null;
  const detailJobId = inspectedJobId || activeJobId || null;

  const detailJobError = useMemo(() => {
    if (!detailJobErrorState) return null;
    return extractApiError(
      detailJobErrorState.error,
      i18n.t(detailJobErrorState.fallbackKey, {
        lng: localeKey,
        ...(detailJobErrorState.fallbackValues || {}),
      }),
    );
  }, [detailJobErrorState, i18n, localeKey]);

  // Load the active/inspected job detail when the target job changes.
  useEffect(() => {
    let disposed = false;
    if (!detailJobId) {
      setActiveJob(null);
      setActiveJobLoading(false);
      setDetailJobErrorState(null);
      return () => {
        disposed = true;
      };
    }

    const loadActiveJob = async () => {
      setActiveJob(null);
      setActiveJobLoading(true);
      setDetailJobErrorState(null);
      try {
        const payload = await getIndexJob(detailJobId);
        if (!disposed) {
          setActiveJob(payload?.job || null);
        }
      } catch (err) {
        if (!disposed) {
          setActiveJob(null);
          setDetailJobErrorState({
            error: err,
            fallbackKey: 'observability.messages.activeJobLoadFailed',
            fallbackValues: { job: detailJobId },
          });
          const statusCode = err?.response?.status;
          if (statusCode === 404) {
            setInspectedJobId((prev) => (prev === detailJobId ? null : prev));
          }
        }
      } finally {
        if (!disposed) {
          setActiveJobLoading(false);
        }
      }
    };

    loadActiveJob();
    return () => {
      disposed = true;
    };
  }, [detailJobId, summaryTimestamp]);

  const handleRebuild = async () => {
    setRebuilding(true);
    setRebuildMessage(null);
    try {
      const data = await triggerIndexRebuild({
        reason: 'observability_console',
        wait: false,
      });
      const jobId = data?.job_id ? `job ${data.job_id}` : 'sync';
      setRebuildMessage(t('observability.messages.rebuildRequested', { job: jobId }));
      await loadSummary();
    } catch (err) {
      setRebuildMessage(
        t('observability.messages.rebuildFailed', { detail: extractApiError(err) }),
      );
    } finally {
      setRebuilding(false);
    }
  };

  const handleSleepConsolidation = async () => {
    setSleepConsolidating(true);
    setRebuildMessage(null);
    try {
      const data = await triggerSleepConsolidation({
        reason: 'observability_console',
        wait: false,
      });
      const jobId = data?.job_id ? `job ${data.job_id}` : 'sync';
      setRebuildMessage(t('observability.messages.sleepRequested', { job: jobId }));
      await loadSummary();
    } catch (err) {
      setRebuildMessage(
        t('observability.messages.sleepFailed', { detail: extractApiError(err) }),
      );
    } finally {
      setSleepConsolidating(false);
    }
  };

  const handleHealthReindex = async () => {
    setHealthReindexing(true);
    setRebuildMessage(null);
    try {
      const data = await triggerIndexRebuild({
        reason: 'health_panel_mixed_dims_reindex',
        wait: false,
      });
      const jobId = data?.job_id ? `job ${data.job_id}` : 'sync';
      setRebuildMessage(t('observability.messages.rebuildRequested', { job: jobId }));
      await loadSummary();
    } catch (err) {
      setRebuildMessage(
        t('observability.messages.rebuildFailed', { detail: extractApiError(err) }),
      );
    } finally {
      setHealthReindexing(false);
    }
  };

  const handleCancelJob = async (jobId) => {
    if (!jobId) return;
    const actionKey = `cancel:${jobId}`;
    setJobActionKey(actionKey);
    setRebuildMessage(null);
    try {
      await cancelIndexJob(jobId, { reason: 'observability_console_cancel' });
      setRebuildMessage(t('observability.messages.cancelRequested', { job: jobId }));
      await loadSummary();
    } catch (err) {
      const statusCode = err?.response?.status;
      const detail = extractApiError(err, t('observability.messages.cancelRequestFailed'));
      const normalizedDetail = detail.trim().toLowerCase();
      const isJobNotFound =
        normalizedDetail.includes('job_not_found') ||
        (normalizedDetail.includes('job') && normalizedDetail.includes('not found'));
      const isAlreadyFinalized =
        normalizedDetail.includes('job_already_finalized') ||
        (normalizedDetail.includes('already') && normalizedDetail.includes('final'));
      if (statusCode === 404) {
        if (isJobNotFound) {
          setRebuildMessage(
            t('observability.messages.cancelSkipped', { job: jobId, detail: 'job not found' }),
          );
          await loadSummary();
        } else {
          setRebuildMessage(t('observability.messages.cancelFailed', { job: jobId, detail }));
        }
      } else if (statusCode === 409) {
        if (isAlreadyFinalized) {
          setRebuildMessage(
            t('observability.messages.cancelSkipped', {
              job: jobId,
              detail: 'already finalized',
            }),
          );
          await loadSummary();
        } else {
          setRebuildMessage(t('observability.messages.cancelFailed', { job: jobId, detail }));
        }
      } else {
        setRebuildMessage(t('observability.messages.cancelFailed', { job: jobId, detail }));
      }
    } finally {
      setJobActionKey(null);
    }
  };

  const handleRetryJob = async (job) => {
    const jobId = job?.job_id;
    if (!jobId) return;
    const actionKey = `retry:${jobId}`;
    setJobActionKey(actionKey);
    setRebuildMessage(null);

    const retryReason = `retry:${jobId}`;
    const taskType = String(job?.task_type || '');
    const retryMemoryId = Number(job?.memory_id);
    try {
      let payload = null;
      try {
        payload = await retryIndexJob(jobId, { reason: retryReason });
      } catch (err) {
        if (isRetryEndpointUnsupported(err)) {
          if (taskType === 'reindex_memory' && Number.isInteger(retryMemoryId) && retryMemoryId > 0) {
            payload = await triggerMemoryReindex(retryMemoryId, {
              reason: retryReason,
              wait: false,
            });
          } else if (taskType === 'rebuild_index') {
            payload = await triggerIndexRebuild({
              reason: retryReason,
              wait: false,
            });
          } else if (taskType === 'sleep_consolidation') {
            payload = await triggerSleepConsolidation({
              reason: retryReason,
              wait: false,
            });
          } else {
            throw new Error(
              t('observability.messages.retryUnsupported', {
                taskType: taskType || 'unknown',
              }),
            );
          }
        } else {
          throw err;
        }
      }
      const requestedJob = payload?.job_id ? `job ${payload.job_id}` : 'sync';
      setRebuildMessage(t('observability.messages.retryRequested', { job: requestedJob }));
      await loadSummary();
    } catch (err) {
      setRebuildMessage(
        t('observability.messages.retryFailed', {
          job: jobId,
          detail: extractApiError(err),
        }),
      );
    } finally {
      setJobActionKey(null);
    }
  };

  return {
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
  };
}

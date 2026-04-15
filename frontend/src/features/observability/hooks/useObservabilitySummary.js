import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { extractApiError, getObservabilitySummary } from '../../../lib/api';

/**
 * Manages the observability summary lifecycle: fetch, race-guard, and error localization.
 *
 * @returns {{ summary, summaryLoading, summaryError, summaryTimestamp, loadSummary }}
 */
export function useObservabilitySummary() {
  const { i18n } = useTranslation();
  const [summary, setSummary] = useState(null);
  const [summaryLoading, setSummaryLoading] = useState(false);
  const [summaryErrorState, setSummaryErrorState] = useState(null);
  const summaryRequestSeqRef = useRef(0);

  const localeKey = i18n.resolvedLanguage || i18n.language || 'en';

  const summaryTimestamp = summary?.timestamp || '';

  const summaryError = useMemo(() => {
    if (!summaryErrorState) return null;
    return extractApiError(
      summaryErrorState.error,
      i18n.t(summaryErrorState.fallbackKey, { lng: localeKey }),
    );
  }, [summaryErrorState, i18n, localeKey]);

  const loadSummary = useCallback(async () => {
    const requestSeq = summaryRequestSeqRef.current + 1;
    summaryRequestSeqRef.current = requestSeq;
    setSummaryLoading(true);
    setSummaryErrorState(null);
    try {
      const data = await getObservabilitySummary();
      if (requestSeq !== summaryRequestSeqRef.current) return;
      setSummary(data);
    } catch (err) {
      if (requestSeq !== summaryRequestSeqRef.current) return;
      setSummaryErrorState({
        error: err,
        fallbackKey: 'observability.summaryError',
      });
    } finally {
      if (requestSeq !== summaryRequestSeqRef.current) return;
      setSummaryLoading(false);
    }
  }, []);

  useEffect(() => {
    loadSummary();
  }, [loadSummary]);

  return { summary, summaryLoading, summaryError, summaryTimestamp, loadSummary };
}

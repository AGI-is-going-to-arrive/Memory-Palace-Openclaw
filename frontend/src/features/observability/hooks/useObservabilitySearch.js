import { useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { extractApiError, runObservabilitySearch } from '../../../lib/api';

const parseOptionalNonNegativeInteger = (rawValue, label, t) => {
  const normalized = String(rawValue ?? '').trim();
  if (!normalized) return null;
  if (!/^\d+$/.test(normalized)) {
    throw new Error(t('observability.validation.nonNegativeInteger', { label }));
  }
  const parsed = Number(normalized);
  if (!Number.isSafeInteger(parsed)) {
    throw new Error(t('observability.validation.nonNegativeInteger', { label }));
  }
  return parsed;
};

const parseRequiredIntegerInRange = (
  rawValue,
  label,
  t,
  { min = 1, max = Number.MAX_SAFE_INTEGER } = {},
) => {
  const normalized = String(rawValue ?? '').trim();
  if (!normalized) {
    throw new Error(t('observability.validation.required', { label }));
  }
  if (!/^\d+$/.test(normalized)) {
    throw new Error(t('observability.validation.integer', { label }));
  }
  const parsed = Number(normalized);
  if (!Number.isSafeInteger(parsed) || parsed < min || parsed > max) {
    throw new Error(t('observability.validation.range', { label, min, max }));
  }
  return parsed;
};

/**
 * Manages the diagnostic search form state, i18n default-query sync, and search execution.
 *
 * @param {{ loadSummary: () => Promise<void>, setRebuildMessage: (msg: string|null) => void }} deps
 * @returns {{ form, searching, searchError, searchResult, onFieldChange, runSearch }}
 */
export function useObservabilitySearch({ loadSummary, setRebuildMessage }) {
  const { t, i18n } = useTranslation();

  const localeKey = i18n.resolvedLanguage || i18n.language || 'en';

  const initialDefaultQuery = i18n.t('observability.defaultQuery', {
    lng: i18n.resolvedLanguage || i18n.language || 'en',
  });
  const previousDefaultQueryRef = useRef(initialDefaultQuery);

  const [searching, setSearching] = useState(false);
  const [searchErrorState, setSearchErrorState] = useState(null);
  const [searchResult, setSearchResult] = useState(null);
  const [form, setForm] = useState(() => ({
    query: initialDefaultQuery,
    mode: 'hybrid',
    maxResults: '8',
    candidateMultiplier: '4',
    includeSession: true,
    sessionId: 'api-observability',
    domain: '',
    pathPrefix: '',
    scopeHint: '',
    maxPriority: '',
  }));

  const searchError = useMemo(() => {
    if (!searchErrorState) return null;
    return extractApiError(
      searchErrorState.error,
      i18n.t(searchErrorState.fallbackKey, { lng: localeKey }),
    );
  }, [searchErrorState, i18n, localeKey]);

  // Sync the default query text when locale changes (only if user hasn't touched it).
  useEffect(() => {
    const nextDefaultQuery = i18n.t('observability.defaultQuery', { lng: localeKey });
    setForm((prev) => {
      const shouldUpdate = prev.query === previousDefaultQueryRef.current;
      previousDefaultQueryRef.current = nextDefaultQuery;
      if (!shouldUpdate) return prev;
      return { ...prev, query: nextDefaultQuery };
    });
  }, [i18n, localeKey]);

  const onFieldChange = (name, value) => {
    setForm((prev) => ({ ...prev, [name]: value }));
  };

  const runSearch = async (event) => {
    event.preventDefault();
    setSearching(true);
    setSearchErrorState(null);
    setRebuildMessage(null);
    try {
      const filters = {};
      if (form.domain.trim()) filters.domain = form.domain.trim();
      if (form.pathPrefix.trim()) filters.path_prefix = form.pathPrefix.trim();
      const maxPriority = parseOptionalNonNegativeInteger(
        form.maxPriority,
        t('observability.maxPriorityFilter'),
        t,
      );
      if (maxPriority !== null) {
        filters.max_priority = maxPriority;
      }

      const payload = {
        query: form.query,
        mode: form.mode,
        max_results: parseRequiredIntegerInRange(form.maxResults, t('observability.maxResults'), t, {
          min: 1,
          max: 50,
        }),
        candidate_multiplier: parseRequiredIntegerInRange(
          form.candidateMultiplier,
          t('observability.candidateMultiplier'),
          t,
          { min: 1, max: 20 },
        ),
        include_session: form.includeSession,
        session_id: form.sessionId.trim() || null,
        filters,
      };
      if (form.scopeHint.trim()) {
        payload.scope_hint = form.scopeHint.trim();
      }

      const data = await runObservabilitySearch(payload);
      setSearchResult(data);
      await loadSummary();
    } catch (err) {
      setSearchErrorState({
        error: err,
        fallbackKey: 'observability.diagnosticSearchFailed',
      });
    } finally {
      setSearching(false);
    }
  };

  return { form, searching, searchError, searchResult, onFieldChange, runSearch };
}

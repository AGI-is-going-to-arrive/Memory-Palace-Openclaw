import { act, renderHook, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import * as api from '../../../lib/api';
import i18n from '../../../i18n';
import { useObservabilitySearch } from './useObservabilitySearch';

vi.mock('../../../lib/api', () => ({
  extractApiError: vi.fn((error, fallback = 'Request failed') => error?.message || fallback),
  runObservabilitySearch: vi.fn(),
}));

const createSubmitEvent = () => ({
  preventDefault: vi.fn(),
});

describe('useObservabilitySearch', () => {
  beforeEach(async () => {
    vi.clearAllMocks();
    await i18n.changeLanguage('en');
    api.runObservabilitySearch.mockResolvedValue({ ok: true, results: [] });
  });

  it('relocalizes the untouched default query when the language changes', async () => {
    const { result } = renderHook(() =>
      useObservabilitySearch({
        loadSummary: vi.fn().mockResolvedValue(undefined),
        setRebuildMessage: vi.fn(),
      })
    );

    expect(result.current.form.query).toBe('memory flush queue');

    await act(async () => {
      await i18n.changeLanguage('zh-CN');
    });

    await waitFor(() => {
      expect(result.current.form.query).toBe(i18n.t('observability.defaultQuery'));
    });
  });

  it('preserves a user-edited query when the language changes', async () => {
    const { result } = renderHook(() =>
      useObservabilitySearch({
        loadSummary: vi.fn().mockResolvedValue(undefined),
        setRebuildMessage: vi.fn(),
      })
    );

    act(() => {
      result.current.onFieldChange('query', 'custom diagnostic query');
    });

    await act(async () => {
      await i18n.changeLanguage('zh-CN');
    });

    expect(result.current.form.query).toBe('custom diagnostic query');
  });

  it('blocks search when candidate multiplier falls outside the allowed range', async () => {
    const { result } = renderHook(() =>
      useObservabilitySearch({
        loadSummary: vi.fn().mockResolvedValue(undefined),
        setRebuildMessage: vi.fn(),
      })
    );

    act(() => {
      result.current.onFieldChange('candidateMultiplier', '0');
    });

    await act(async () => {
      await result.current.runSearch(createSubmitEvent());
    });

    expect(api.runObservabilitySearch).not.toHaveBeenCalled();
    expect(result.current.searchError).toContain('must be in range');
  });

  it('blocks search when max priority is not a non-negative integer', async () => {
    const { result } = renderHook(() =>
      useObservabilitySearch({
        loadSummary: vi.fn().mockResolvedValue(undefined),
        setRebuildMessage: vi.fn(),
      })
    );

    act(() => {
      result.current.onFieldChange('maxPriority', '1.5');
    });

    await act(async () => {
      await result.current.runSearch(createSubmitEvent());
    });

    expect(api.runObservabilitySearch).not.toHaveBeenCalled();
    expect(result.current.searchError).toContain(i18n.t('observability.maxPriorityFilter'));
  });

  it('assembles a trimmed payload with numeric filters and null session id', async () => {
    const loadSummary = vi.fn().mockResolvedValue(undefined);
    const setRebuildMessage = vi.fn();
    const { result } = renderHook(() =>
      useObservabilitySearch({ loadSummary, setRebuildMessage })
    );

    act(() => {
      result.current.onFieldChange('query', 'memory recall');
      result.current.onFieldChange('mode', 'semantic');
      result.current.onFieldChange('maxResults', '12');
      result.current.onFieldChange('candidateMultiplier', '5');
      result.current.onFieldChange('includeSession', false);
      result.current.onFieldChange('sessionId', '   ');
      result.current.onFieldChange('domain', ' core ');
      result.current.onFieldChange('pathPrefix', ' core://agent ');
      result.current.onFieldChange('scopeHint', ' system://recent ');
      result.current.onFieldChange('maxPriority', '7');
    });

    await act(async () => {
      await result.current.runSearch(createSubmitEvent());
    });

    expect(api.runObservabilitySearch).toHaveBeenCalledWith({
      query: 'memory recall',
      mode: 'semantic',
      max_results: 12,
      candidate_multiplier: 5,
      include_session: false,
      session_id: null,
      scope_hint: 'system://recent',
      filters: {
        domain: 'core',
        path_prefix: 'core://agent',
        max_priority: 7,
      },
    });
    expect(loadSummary).toHaveBeenCalledTimes(1);
    expect(setRebuildMessage).toHaveBeenCalledWith(null);
  });
});

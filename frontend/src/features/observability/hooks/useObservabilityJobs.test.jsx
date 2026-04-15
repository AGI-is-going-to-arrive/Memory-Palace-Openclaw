import React from 'react';
import { I18nextProvider } from 'react-i18next';
import { act, renderHook, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import i18n from '../../../i18n';
import * as api from '../../../lib/api';
import { useObservabilityJobs } from './useObservabilityJobs';

vi.mock('../../../lib/api', () => ({
  cancelIndexJob: vi.fn(),
  extractApiError: vi.fn((error, fallback = 'Request failed') => error?.message || fallback),
  getIndexJob: vi.fn(),
  retryIndexJob: vi.fn(),
  triggerIndexRebuild: vi.fn(),
  triggerMemoryReindex: vi.fn(),
  triggerSleepConsolidation: vi.fn(),
}));

const wrapper = ({ children }) => (
  <I18nextProvider i18n={i18n}>{children}</I18nextProvider>
);

const buildSummary = ({ activeJobId = null } = {}) => ({
  health: {
    runtime: {
      index_worker: {
        active_job_id: activeJobId,
      },
    },
  },
});

describe('useObservabilityJobs', () => {
  beforeEach(async () => {
    vi.clearAllMocks();
    await i18n.changeLanguage('en');
    api.getIndexJob.mockResolvedValue({ job: null });
    api.retryIndexJob.mockResolvedValue({ job_id: 'job-retry-default' });
    api.triggerMemoryReindex.mockResolvedValue({ job_id: 'job-reindex-default' });
    api.triggerIndexRebuild.mockResolvedValue({ job_id: 'job-rebuild-default' });
    api.triggerSleepConsolidation.mockResolvedValue({ job_id: 'job-sleep-default' });
    api.cancelIndexJob.mockResolvedValue({});
  });

  it('switches detail state between the active job and an inspected job', async () => {
    api.getIndexJob.mockImplementation(async (jobId) => ({
      job: {
        job_id: jobId,
        reason: jobId === 'job-active' ? 'active-reason' : 'inspect-reason',
      },
    }));

    const { result } = renderHook(
      () =>
        useObservabilityJobs({
          summary: buildSummary({ activeJobId: 'job-active' }),
          summaryTimestamp: '2026-01-01T00:00:00Z',
          loadSummary: vi.fn().mockResolvedValue(undefined),
        }),
      { wrapper }
    );

    await waitFor(() => {
      expect(result.current.activeJob?.reason).toBe('active-reason');
    });

    act(() => {
      result.current.setInspectedJobId('job-inspect');
    });
    await waitFor(() => {
      expect(result.current.detailJobId).toBe('job-inspect');
      expect(result.current.activeJob?.reason).toBe('inspect-reason');
    });

    act(() => {
      result.current.setInspectedJobId(null);
    });
    await waitFor(() => {
      expect(result.current.detailJobId).toBe('job-active');
      expect(result.current.activeJob?.reason).toBe('active-reason');
    });
  });

  it('clears the inspected job when the detail endpoint returns 404', async () => {
    api.getIndexJob.mockImplementation(async (jobId) => {
      if (jobId === 'job-missing') {
        throw {
          message: 'job missing',
          response: { status: 404, data: { detail: 'job missing' } },
        };
      }
      return { job: { job_id: jobId, reason: 'active-reason' } };
    });

    const { result } = renderHook(
      () =>
        useObservabilityJobs({
          summary: buildSummary({ activeJobId: 'job-active' }),
          summaryTimestamp: '2026-01-01T00:00:00Z',
          loadSummary: vi.fn().mockResolvedValue(undefined),
        }),
      { wrapper }
    );

    await waitFor(() => {
      expect(result.current.activeJob?.reason).toBe('active-reason');
    });

    act(() => {
      result.current.setInspectedJobId('job-missing');
    });

    await waitFor(() => {
      expect(result.current.inspectedJobId).toBe(null);
      expect(result.current.detailJobId).toBe('job-active');
      expect(result.current.activeJob?.reason).toBe('active-reason');
    });
  });

  it('falls back to the legacy reindex endpoint when retry API is unsupported', async () => {
    const loadSummary = vi.fn().mockResolvedValue(undefined);
    api.retryIndexJob.mockRejectedValueOnce({
      response: { status: 404, data: { detail: 'Not Found' } },
    });
    api.triggerMemoryReindex.mockResolvedValueOnce({ job_id: 'job-legacy-retry' });

    const { result } = renderHook(
      () =>
        useObservabilityJobs({
          summary: buildSummary(),
          summaryTimestamp: '2026-01-01T00:00:00Z',
          loadSummary,
        }),
      { wrapper }
    );

    await act(async () => {
      await result.current.handleRetryJob({
        job_id: 'job-legacy',
        task_type: 'reindex_memory',
        memory_id: 77,
      });
    });

    expect(api.retryIndexJob).toHaveBeenCalledWith('job-legacy', { reason: 'retry:job-legacy' });
    expect(api.triggerMemoryReindex).toHaveBeenCalledWith(77, {
      reason: 'retry:job-legacy',
      wait: false,
    });
    expect(result.current.rebuildMessage).toMatch(/retry requested/i);
    expect(loadSummary).toHaveBeenCalledTimes(1);
  });

  it('uses the unified retry endpoint when it succeeds', async () => {
    const loadSummary = vi.fn().mockResolvedValue(undefined);

    const { result } = renderHook(
      () =>
        useObservabilityJobs({
          summary: buildSummary(),
          summaryTimestamp: '2026-01-01T00:00:00Z',
          loadSummary,
        }),
      { wrapper }
    );

    await act(async () => {
      await result.current.handleRetryJob({
        job_id: 'job-unified',
        task_type: 'rebuild_index',
      });
    });

    expect(api.retryIndexJob).toHaveBeenCalledWith('job-unified', {
      reason: 'retry:job-unified',
    });
    expect(api.triggerMemoryReindex).not.toHaveBeenCalled();
    expect(api.triggerIndexRebuild).not.toHaveBeenCalled();
    expect(api.triggerSleepConsolidation).not.toHaveBeenCalled();
    expect(result.current.rebuildMessage).toMatch(/job-retry-default/i);
    expect(loadSummary).toHaveBeenCalledTimes(1);
  });

  it('falls back to the legacy rebuild endpoint when retry returns 405', async () => {
    const loadSummary = vi.fn().mockResolvedValue(undefined);
    api.retryIndexJob.mockRejectedValueOnce({
      response: { status: 405, data: { detail: 'Method Not Allowed' } },
    });
    api.triggerIndexRebuild.mockResolvedValueOnce({ job_id: 'job-rebuild-legacy' });

    const { result } = renderHook(
      () =>
        useObservabilityJobs({
          summary: buildSummary(),
          summaryTimestamp: '2026-01-01T00:00:00Z',
          loadSummary,
        }),
      { wrapper }
    );

    await act(async () => {
      await result.current.handleRetryJob({
        job_id: 'job-rebuild',
        task_type: 'rebuild_index',
      });
    });

    expect(api.retryIndexJob).toHaveBeenCalledWith('job-rebuild', {
      reason: 'retry:job-rebuild',
    });
    expect(api.triggerIndexRebuild).toHaveBeenCalledWith({
      reason: 'retry:job-rebuild',
      wait: false,
    });
    expect(result.current.rebuildMessage).toMatch(/job-rebuild-legacy/i);
    expect(loadSummary).toHaveBeenCalledTimes(1);
  });

  it('falls back to the legacy sleep endpoint when retry returns 405', async () => {
    const loadSummary = vi.fn().mockResolvedValue(undefined);
    api.retryIndexJob.mockRejectedValueOnce({
      response: { status: 405, data: { detail: 'Method Not Allowed' } },
    });
    api.triggerSleepConsolidation.mockResolvedValueOnce({ job_id: 'job-sleep-legacy' });

    const { result } = renderHook(
      () =>
        useObservabilityJobs({
          summary: buildSummary(),
          summaryTimestamp: '2026-01-01T00:00:00Z',
          loadSummary,
        }),
      { wrapper }
    );

    await act(async () => {
      await result.current.handleRetryJob({
        job_id: 'job-sleep',
        task_type: 'sleep_consolidation',
      });
    });

    expect(api.retryIndexJob).toHaveBeenCalledWith('job-sleep', {
      reason: 'retry:job-sleep',
    });
    expect(api.triggerSleepConsolidation).toHaveBeenCalledWith({
      reason: 'retry:job-sleep',
      wait: false,
    });
    expect(result.current.rebuildMessage).toMatch(/job-sleep-legacy/i);
    expect(loadSummary).toHaveBeenCalledTimes(1);
  });

  it('does not fall back to legacy retry paths when the backend reports job_not_found', async () => {
    const loadSummary = vi.fn().mockResolvedValue(undefined);
    api.retryIndexJob.mockRejectedValueOnce({
      message: 'job not found',
      response: {
        status: 404,
        data: {
          detail: {
            error: 'job_not_found',
            message: 'job not found',
          },
        },
      },
    });

    const { result } = renderHook(
      () =>
        useObservabilityJobs({
          summary: buildSummary(),
          summaryTimestamp: '2026-01-01T00:00:00Z',
          loadSummary,
        }),
      { wrapper }
    );

    await act(async () => {
      await result.current.handleRetryJob({
        job_id: 'job-missing',
        task_type: 'reindex_memory',
        memory_id: 77,
      });
    });

    expect(api.triggerMemoryReindex).not.toHaveBeenCalled();
    expect(api.triggerIndexRebuild).not.toHaveBeenCalled();
    expect(api.triggerSleepConsolidation).not.toHaveBeenCalled();
    expect(result.current.rebuildMessage).toMatch(/retry failed \(job-missing\): job not found/i);
    expect(loadSummary).not.toHaveBeenCalled();
  });

  it('maps 404 and 409 cancel responses into explicit skipped messages', async () => {
    const loadSummary = vi.fn().mockResolvedValue(undefined);
    const { result } = renderHook(
      () =>
        useObservabilityJobs({
          summary: buildSummary(),
          summaryTimestamp: '2026-01-01T00:00:00Z',
          loadSummary,
        }),
      { wrapper }
    );

    api.cancelIndexJob.mockRejectedValueOnce({
      message: "job 'job-missing' not found.",
      response: { status: 404, data: { detail: "job 'job-missing' not found." } },
    });
    await act(async () => {
      await result.current.handleCancelJob('job-missing');
    });
    expect(result.current.rebuildMessage).toMatch(/cancel skipped \(job-missing\): job not found/i);

    api.cancelIndexJob.mockRejectedValueOnce({
      message: 'job_already_finalized',
      response: { status: 409, data: { detail: 'job_already_finalized' } },
    });
    await act(async () => {
      await result.current.handleCancelJob('job-finalized');
    });
    expect(result.current.rebuildMessage).toMatch(
      /cancel skipped \(job-finalized\): already finalized/i
    );
    expect(loadSummary).toHaveBeenCalledTimes(2);
  });

  it('surfaces unknown 409 conflicts as cancel failures', async () => {
    const loadSummary = vi.fn().mockResolvedValue(undefined);
    const { result } = renderHook(
      () =>
        useObservabilityJobs({
          summary: buildSummary(),
          summaryTimestamp: '2026-01-01T00:00:00Z',
          loadSummary,
        }),
      { wrapper }
    );

    api.cancelIndexJob.mockRejectedValueOnce({
      message: 'running_job_handle_unavailable',
      response: { status: 409, data: { detail: 'running_job_handle_unavailable' } },
    });

    await act(async () => {
      await result.current.handleCancelJob('job-conflict');
    });

    expect(result.current.rebuildMessage).toMatch(
      /cancel failed \(job-conflict\): running_job_handle_unavailable/i
    );
    expect(loadSummary).not.toHaveBeenCalled();
  });
});

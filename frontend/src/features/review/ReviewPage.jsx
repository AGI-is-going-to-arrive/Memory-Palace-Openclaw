import React, { useCallback, useEffect, useMemo, useState, useRef } from 'react';
import { useTranslation } from 'react-i18next';
import {
  approveSnapshot,
  clearSession,
  extractApiError,
  getDiff,
  getSnapshotStorageSummary,
  getSessions,
  getSnapshots,
  rollbackResource,
} from '../../lib/api';
import SnapshotList from '../../components/SnapshotList';
import { SimpleDiff } from '../../components/DiffViewer';
import { ConfirmDialog, AlertDialog } from '../../components/ModalDialog'; // Uses the Memory Palace styled diff
import {
  Activity, 
  Check, 
  ChevronRight, 
  Clock, 
  Database, 
  FileText,
  Layout, 
  Link2,
  RefreshCw, 
  RotateCcw, 
  Settings2,
  ShieldCheck, 
  Trash2
} from 'lucide-react';
import clsx from 'clsx';
import { formatTime } from '../../lib/format';

const normalizeSessionList = (value) => {
  if (!Array.isArray(value)) return [];
  return value.map((session, index) => {
    const normalizedSession = session && typeof session === 'object' ? session : {};
    const rawSessionId = normalizedSession.session_id;
    const sessionId =
      (typeof rawSessionId === 'string' || typeof rawSessionId === 'number')
        ? String(rawSessionId).trim()
        : '';
    return {
      ...normalizedSession,
      session_id: sessionId || `session-${index + 1}`,
    };
  });
};

const formatSnapshotTime = (value, lng, fallback) => {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return fallback;
  return formatTime(parsed, lng, {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  }) || fallback;
};

const formatStorageBytes = (value) => {
  const numeric = Number(value || 0);
  if (!Number.isFinite(numeric) || numeric <= 0) return '0 B';
  if (numeric < 1024) return `${Math.trunc(numeric)} B`;
  if (numeric < 1024 * 1024) return `${(numeric / 1024).toFixed(1)} KB`;
  if (numeric < 1024 * 1024 * 1024) return `${(numeric / (1024 * 1024)).toFixed(1)} MB`;
  return `${(numeric / (1024 * 1024 * 1024)).toFixed(1)} GB`;
};

const parseTimestamp = (value) => {
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
};

const formatAgeDays = (value, t) => {
  const numeric = Number(value);
  if (!Number.isFinite(numeric) || numeric < 0) {
    return t('common.states.unknown');
  }
  if (numeric === 0) {
    return t('review.cleanup.ageToday');
  }
  return t('review.cleanup.ageDays', { count: Math.trunc(numeric) });
};

function ReviewPage() {
  const { t, i18n } = useTranslation();
  const [sessions, setSessions] = useState([]);
  const [storageSummary, setStorageSummary] = useState(null);
  const [currentSessionId, setCurrentSessionId] = useState(null);
  const [snapshots, setSnapshots] = useState([]);
  const [selectedSnapshot, setSelectedSnapshot] = useState(null);
  const [diffData, setDiffData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [diffErrorState, setDiffErrorState] = useState(null);
  const [mutationInFlight, setMutationInFlight] = useState(false);
  const [cleanupSort, setCleanupSort] = useState('reclaim');
  const [cleanupAgeFilter, setCleanupAgeFilter] = useState('all');
  const [cleanupWarningsOnly, setCleanupWarningsOnly] = useState(false);
  const [cleanupTopOnly, setCleanupTopOnly] = useState(false);
  const [snapshotSort, setSnapshotSort] = useState('recent');
  
  const sessionsRequestRef = useRef(0);
  const currentSessionIdRef = useRef(null);
  const diffRequestRef = useRef(0);
  const snapshotsRequestRef = useRef(0);
  const mutationInFlightRef = useRef(false);
  const clearReviewState = React.useCallback((clearSnapshotList = true) => {
    snapshotsRequestRef.current += 1;
    diffRequestRef.current += 1;
    setLoading(false);
    if (clearSnapshotList) {
      setSnapshots([]);
    }
    setSelectedSnapshot(null);
    setDiffData(null);
    setDiffErrorState(null);
  }, []);
  const diffError = useMemo(() => {
    if (!diffErrorState) return null;
    return extractApiError(diffErrorState.error, t(diffErrorState.fallbackKey));
  }, [diffErrorState, t]);
  const enrichedSessions = useMemo(() => {
    const storageSessions = Array.isArray(storageSummary?.sessions) ? storageSummary.sessions : [];
    const runtimeById = new Map(
      sessions.map((session) => [String(session.session_id || ''), session])
    );
    const storageById = new Map(
      storageSessions
        .filter((session) => session && typeof session === 'object')
        .map((session) => [String(session.session_id || ''), session])
    );
    const sessionIds = Array.from(
      new Set([
        ...sessions.map((session) => String(session.session_id || '')),
        ...storageSessions.map((session) => String(session?.session_id || '')),
      ])
    ).filter(Boolean);
    return sessionIds.map((sessionId) => {
      const session = runtimeById.get(sessionId) || { session_id: sessionId };
      const storageMeta = storageById.get(sessionId) || {};
      return {
        ...storageMeta,
        ...session,
        total_bytes: Number(storageMeta.total_bytes || 0),
        estimated_reclaim_bytes: Number(
          storageMeta.estimated_reclaim_bytes ?? storageMeta.total_bytes ?? 0
        ),
        age_days: storageMeta.age_days ?? null,
        warning_codes: Array.isArray(storageMeta.warning_codes) ? storageMeta.warning_codes : [],
        over_warning_threshold: Boolean(storageMeta.over_warning_threshold),
      };
    });
  }, [sessions, storageSummary?.sessions]);
  const cleanupCandidates = useMemo(() => {
    let next = [...enrichedSessions];
    if (cleanupAgeFilter !== 'all') {
      const threshold = Number(cleanupAgeFilter);
      next = next.filter((session) => Number(session.age_days ?? -1) >= threshold);
    }
    if (cleanupWarningsOnly) {
      next = next.filter((session) => session.over_warning_threshold);
    }
    next.sort((left, right) => {
      if (cleanupSort === 'age') {
        return Number(right.age_days || 0) - Number(left.age_days || 0);
      }
      if (cleanupSort === 'session_newest') {
        return (
          (parseTimestamp(right.newest_snapshot_time)?.getTime() || 0)
          - (parseTimestamp(left.newest_snapshot_time)?.getTime() || 0)
        );
      }
      return Number(right.estimated_reclaim_bytes || 0) - Number(left.estimated_reclaim_bytes || 0);
    });
    if (cleanupTopOnly && next.length > 0) {
      next = [next[0]];
    }
    return next;
  }, [cleanupAgeFilter, cleanupSort, cleanupTopOnly, cleanupWarningsOnly, enrichedSessions]);
  const currentSessionMeta = useMemo(
    () => enrichedSessions.find((session) => session.session_id === currentSessionId) || null,
    [currentSessionId, enrichedSessions]
  );
  const sortedSnapshots = useMemo(() => {
    const next = [...snapshots];
    next.sort((left, right) => {
      if (snapshotSort === 'size_desc') {
        return Number(right.file_bytes || 0) - Number(left.file_bytes || 0);
      }
      if (snapshotSort === 'size_asc') {
        return Number(left.file_bytes || 0) - Number(right.file_bytes || 0);
      }
      if (snapshotSort === 'oldest') {
        return (
          (parseTimestamp(left.snapshot_time)?.getTime() || 0)
          - (parseTimestamp(right.snapshot_time)?.getTime() || 0)
        );
      }
      return (
        (parseTimestamp(right.snapshot_time)?.getTime() || 0)
        - (parseTimestamp(left.snapshot_time)?.getTime() || 0)
      );
    });
    return next;
  }, [snapshotSort, snapshots]);

  const beginMutation = () => {
    if (mutationInFlightRef.current) return false;
    mutationInFlightRef.current = true;
    setMutationInFlight(true);
    return true;
  };

  const endMutation = () => {
    mutationInFlightRef.current = false;
    setMutationInFlight(false);
  };

  useEffect(() => {
    currentSessionIdRef.current = currentSessionId;
  }, [currentSessionId]);

  // --- Data Loading Logic (Keep existing logic, refine UI) ---
  useEffect(() => {
    const controller = new AbortController();
    loadSessions(controller.signal);
    return () => controller.abort();
  }, []);

  const loadSessions = async (signal) => {
    const requestId = ++sessionsRequestRef.current;
    try {
      const [rawList, rawStorageSummary] = await Promise.all([
        getSessions(),
        getSnapshotStorageSummary().catch(() => null),
      ]);
      const list = normalizeSessionList(rawList);
      if (requestId !== sessionsRequestRef.current) return;
      if (signal?.aborted) return;
      setDiffErrorState(null);
      setSessions(list);
      setStorageSummary(rawStorageSummary && typeof rawStorageSummary === 'object' ? rawStorageSummary : null);
      // Logic to auto-select or maintain selection
      const activeSessionId = currentSessionIdRef.current;
      const hasActiveSession = Boolean(
        activeSessionId && list.find((session) => session.session_id === activeSessionId)
      );
      if (hasActiveSession) return;
      if (list.length === 0) {
        clearReviewState();
        setCurrentSessionId(null);
        return;
      }
      clearReviewState();
      setCurrentSessionId(list[0].session_id);
    } catch (err) {
      if (signal?.aborted) return;
      if (requestId !== sessionsRequestRef.current) return;
      setDiffErrorState({ error: err, fallbackKey: 'review.errors.loadSessions' });
    }
  };

  useEffect(() => {
    if (!currentSessionId) {
      clearReviewState();
      return;
    }
    clearReviewState();
    const controller = new AbortController();
    loadSnapshots(currentSessionId, controller.signal);
    return () => controller.abort();
  }, [clearReviewState, currentSessionId]);

  const loadSnapshots = async (sessionId, signal) => {
    const requestId = ++snapshotsRequestRef.current;
    setLoading(true);
    setDiffErrorState(null);
    try {
      const list = await getSnapshots(sessionId);
      if (requestId !== snapshotsRequestRef.current) return;
      if (signal?.aborted) return;
      setSnapshots(list);
      if (list.length > 0) setSelectedSnapshot(list[0]);
      else clearReviewState();
    } catch (err) {
      if (signal?.aborted) return;
      if (requestId !== snapshotsRequestRef.current) return;
      if (err.response?.status === 404) {
        clearReviewState();
        return;
      }
      clearReviewState();
      setDiffErrorState({ error: err, fallbackKey: 'review.errors.loadSnapshots' });
    } finally {
      if (requestId !== snapshotsRequestRef.current) return;
      setLoading(false);
    }
  };

  useEffect(() => {
    if (currentSessionId && selectedSnapshot) {
      const controller = new AbortController();
      loadDiff(currentSessionId, selectedSnapshot.resource_id, controller.signal);
      return () => controller.abort();
    }
  }, [currentSessionId, selectedSnapshot]);

  const loadDiff = async (sessionId, resourceId, signal) => {
    const requestId = ++diffRequestRef.current;
    setDiffErrorState(null);
    setDiffData(null);
    try {
      const data = await getDiff(sessionId, resourceId);
      if (signal?.aborted) return;
      if (requestId === diffRequestRef.current) setDiffData(data);
    } catch (err) {
      if (signal?.aborted) return;
      if (requestId === diffRequestRef.current) {
        setDiffErrorState({ error: err, fallbackKey: 'review.errors.retrieveFragment' });
        setDiffData(null);
      }
    }
  };

  // --- Modal state ---
  const [alertMsg, setAlertMsg] = useState(null);
  const [rollbackConfirmOpen, setRollbackConfirmOpen] = useState(false);
  const [clearSessionConfirmOpen, setClearSessionConfirmOpen] = useState(false);
  const [clearSessionPromptText, setClearSessionPromptText] = useState('');

  // --- Handlers ---
  const handleRollback = useCallback(() => {
    if (!currentSessionId || !selectedSnapshot) return;
    setRollbackConfirmOpen(true);
  }, [currentSessionId, selectedSnapshot]);

  const confirmRollback = useCallback(async () => {
    setRollbackConfirmOpen(false);
    if (!currentSessionId || !selectedSnapshot) return;
    if (!beginMutation()) return;
    try {
      const rollbackResult = await rollbackResource(currentSessionId, selectedSnapshot.resource_id);
      if (!rollbackResult?.success) {
        throw new Error(rollbackResult?.message || t('review.errors.rollback'));
      }
      let cleanupError = null;
      try {
        await approveSnapshot(currentSessionId, selectedSnapshot.resource_id);
      } catch (err) {
        cleanupError = err;
      }
      await loadSnapshots(currentSessionId);
      await loadSessions();
      if (cleanupError) {
        setAlertMsg(t('review.alerts.rollbackCleanupFailed', {
          detail: extractApiError(cleanupError, cleanupError?.message || t('review.errors.approve')),
        }));
      }
    } catch (err) {
      setAlertMsg(t('review.alerts.rejectionFailed', {
        detail: extractApiError(err, err?.message || t('review.errors.rollback')),
      }));
    } finally {
      endMutation();
    }
  }, [currentSessionId, selectedSnapshot, beginMutation, endMutation, loadSnapshots, loadSessions, t]);

  const handleApprove = async () => {
    if (!currentSessionId || !selectedSnapshot) return;
    if (!beginMutation()) return;
    try {
      await approveSnapshot(currentSessionId, selectedSnapshot.resource_id);
      await loadSnapshots(currentSessionId);
      await loadSessions();
    } catch (err) {
      setAlertMsg(t('review.alerts.integrationFailed', {
        detail: extractApiError(err, err?.message || t('review.errors.approve')),
      }));
    } finally {
      endMutation();
    }
  };

  const handleClearSession = useCallback(() => {
    if (!currentSessionId) return;
    const prompt = currentSessionMeta
      ? t('review.prompts.cleanupPreview', {
        sessionId: currentSessionId,
        count: currentSessionMeta.resource_count || 0,
        size: formatStorageBytes(currentSessionMeta.estimated_reclaim_bytes),
      })
      : t('review.prompts.integrateAll');
    setClearSessionPromptText(prompt);
    setClearSessionConfirmOpen(true);
  }, [currentSessionId, currentSessionMeta, t]);

  const confirmClearSession = useCallback(async () => {
    setClearSessionConfirmOpen(false);
    if (!currentSessionId) return;
    if (!beginMutation()) return;
    try {
      await clearSession(currentSessionId);
      await loadSessions();
    } catch (err) {
      setAlertMsg(t('review.alerts.massIntegrationFailed', {
        detail: extractApiError(err, err?.message || t('review.errors.clearSession')),
      }));
    } finally {
      endMutation();
    }
  }, [currentSessionId, beginMutation, endMutation, loadSessions, t]);

  // --- Render Helpers ---
  
  // Surviving Paths Renderer (for DELETE operations)
  const renderSurvivingPaths = () => {
    if (!selectedSnapshot || selectedSnapshot.operation_type !== 'delete') return null;
    if (!diffData?.current_data) return null;
    
    const survivingPathsRaw = diffData.current_data.surviving_paths;
    if (survivingPathsRaw === undefined) return null;  // Data not loaded yet
    const survivingPaths = Array.isArray(survivingPathsRaw) ? survivingPathsRaw : [];
    
    const isFullDeletion = survivingPaths.length === 0;

    return (
      <div className={clsx(
        "mb-8 p-4 rounded-lg border backdrop-blur-sm",
        isFullDeletion 
          ? "bg-rose-950/20 border-rose-800/40" 
          : "bg-stone-900/40 border-stone-800/60"
      )}>
        <h3 className="text-xs font-bold uppercase mb-3 flex items-center gap-2 tracking-widest">
          {isFullDeletion ? (
            <>
              <Trash2 size={12} className="text-rose-500" />
              <span className="text-rose-400">{t('review.memoryFullyOrphaned')}</span>
            </>
          ) : (
            <>
              <Link2 size={12} className="text-stone-500" />
              <span className="text-stone-500">{t('review.survivingPaths')}</span>
            </>
          )}
        </h3>
        
        {isFullDeletion ? (
          <p className="text-xs text-rose-300/70">
            {t('review.noOtherPaths')}
          </p>
        ) : (
          <div className="space-y-1.5">
            <p className="text-xs text-stone-500 mb-2">
              {t('review.stillReachable', { count: survivingPaths.length })}
            </p>
            {survivingPaths.map((path, idx) => (
              <div key={idx} className="flex items-center gap-2 text-xs font-mono text-emerald-400/80 bg-emerald-950/20 rounded px-2.5 py-1.5 border border-emerald-900/30">
                <Link2 size={10} className="text-emerald-600 flex-shrink-0" />
                <span className="truncate">{path}</span>
              </div>
            ))}
          </div>
        )}
      </div>
    );
  };

  // Custom Metadata Renderer
  const renderMetadataChanges = () => {
    if (!diffData?.snapshot_data || !diffData?.current_data) return null;
    const metaKeys = ['priority', 'disclosure'];
    const changes = metaKeys.filter(key => {
      const oldVal = diffData.snapshot_data[key];
      const newVal = diffData.current_data[key];
      return JSON.stringify(oldVal) !== JSON.stringify(newVal);
    });

    if (changes.length === 0) return null;

    return (
      <div className="mb-8 p-4 bg-stone-900/40 border border-stone-800/60 rounded-lg backdrop-blur-sm">
        <h3 className="text-xs font-bold text-stone-500 uppercase mb-4 flex items-center gap-2 tracking-widest">
          <Activity size={12} /> {t('review.metadataShifts')}
        </h3>
        <div className="space-y-3">
          {changes.map(key => {
            const oldVal = diffData.snapshot_data[key];
            const newVal = diffData.current_data[key];
            return (
              <div key={key} className="grid grid-cols-[100px_1fr_20px_1fr] gap-4 text-sm items-start">
                <span className="text-stone-400 font-medium capitalize text-xs pt-0.5">
                  {key === 'priority' ? t('common.labels.priority') : t('common.labels.disclosure')}
                </span>
                <div className="text-rose-400/70 line-through text-xs font-mono text-right break-words">
                  {oldVal != null ? String(oldVal) : t('common.states.empty')}
                </div>
                <div className="text-center text-stone-700 pt-0.5">→</div>
                <div className="text-emerald-400 text-xs font-mono font-bold break-words">
                  {newVal != null ? String(newVal) : t('common.states.empty')}
                </div>
              </div>
            );
          })}
        </div>
      </div>
    );
  };

  return (
    <div className="palace-harmonized relative flex h-full overflow-hidden bg-[color:var(--palace-bg)] text-[color:var(--palace-ink)] font-sans selection:bg-[rgba(179,133,79,0.28)] selection:text-[color:var(--palace-ink)]">
      <ConfirmDialog
        open={rollbackConfirmOpen}
        message={selectedSnapshot ? t('review.prompts.rejectChanges', { resourceId: selectedSnapshot.resource_id }) : ''}
        onConfirm={confirmRollback}
        onCancel={() => setRollbackConfirmOpen(false)}
      />
      <ConfirmDialog
        open={clearSessionConfirmOpen}
        message={clearSessionPromptText}
        onConfirm={confirmClearSession}
        onCancel={() => setClearSessionConfirmOpen(false)}
      />
      <AlertDialog
        open={!!alertMsg}
        message={alertMsg || ''}
        onClose={() => setAlertMsg(null)}
      />

      {/* Sidebar: The Void */}
      <div className="w-72 flex-shrink-0 flex flex-col border-r border-[color:var(--palace-line)] bg-[rgba(255,250,244,0.9)] backdrop-blur-sm">
        {/* Header */}
        <div className="border-b border-[color:var(--palace-line)]/90 bg-[linear-gradient(180deg,rgba(255,252,247,0.88),rgba(244,235,223,0.68))] p-5">
          <div className="flex items-center gap-3 text-stone-100 mb-6">
            <div className="w-8 h-8 rounded bg-gradient-to-br from-amber-500 to-amber-600 flex items-center justify-center shadow-lg shadow-amber-900/20">
              <ShieldCheck className="w-4 h-4 text-white" />
            </div>
            <span className="font-display text-sm tracking-wide text-amber-50">{t('review.ledgerTitle')}</span>
          </div>
          
          <div className="relative group">
            <label
              htmlFor="review-session-select"
              className="text-[10px] text-stone-600 uppercase font-bold mb-1.5 block tracking-widest pl-1"
            >
              {t('review.targetSession')}
            </label>
            <div className="relative">
              <select 
                id="review-session-select"
                name="review_session_id"
                className="w-full cursor-pointer appearance-none rounded-md border border-[color:var(--palace-line)] bg-white/90 px-3 py-2 text-xs text-[color:var(--palace-ink)] outline-none transition-all hover:border-[color:var(--palace-accent)] focus:border-[color:var(--palace-accent)] focus:ring-1 focus:ring-[color:var(--palace-accent)]/40"
                value={currentSessionId || ''}
                onChange={(e) => {
                  setSelectedSnapshot(null);
                  setCurrentSessionId(e.target.value);
                }}
              >
                {sessions.length === 0 && <option>{t('review.noActiveSessions')}</option>}
                {sessions.map(s => (
                  <option key={s.session_id} value={s.session_id}>
                    {s.session_id}
                  </option>
                ))}
              </select>
              <div className="absolute right-3 top-1/2 -translate-y-1/2 pointer-events-none text-stone-600">
                <ChevronRight size={12} className="rotate-90" />
              </div>
            </div>

            {storageSummary ? (
              <div className="mt-4 rounded-md border border-[color:var(--palace-line)] bg-white/80 p-3 text-[11px] text-[color:var(--palace-muted)]">
                <div className="mb-2 font-semibold uppercase tracking-[0.12em] text-[color:var(--palace-ink)]">
                  {t('review.storage.title')}
                </div>
                <div>{t('review.storage.sessions', { count: storageSummary.session_count || 0 })}</div>
                <div>{t('review.storage.snapshots', { count: storageSummary.total_resources || 0 })}</div>
                <div>{t('review.storage.size', { size: formatStorageBytes(storageSummary.total_bytes) })}</div>
                {Array.isArray(storageSummary.warnings) && storageSummary.warnings.length > 0 ? (
                  <div className="mt-2 text-[color:var(--palace-accent-2)]">
                    {String(storageSummary.warnings[0]?.message || '')}
                  </div>
                ) : (
                  <div className="mt-2">{t('review.storage.noWarnings')}</div>
                )}

                <div className="mt-4 border-t border-[color:var(--palace-line)] pt-3">
                  <div className="mb-2 font-semibold uppercase tracking-[0.12em] text-[color:var(--palace-ink)]">
                    {t('review.cleanup.title')}
                  </div>

                  <div className="grid gap-2">
                    <label className="block">
                      <span className="mb-1 block text-[10px] font-semibold uppercase tracking-[0.14em] text-[color:var(--palace-muted)]">
                        {t('review.cleanup.sort')}
                      </span>
                      <select
                        className="w-full rounded-md border border-[color:var(--palace-line)] bg-white/90 px-2 py-1.5 text-[11px] text-[color:var(--palace-ink)]"
                        value={cleanupSort}
                        onChange={(event) => setCleanupSort(event.target.value)}
                      >
                        <option value="reclaim">{t('review.cleanup.sortOptions.reclaim')}</option>
                        <option value="age">{t('review.cleanup.sortOptions.age')}</option>
                        <option value="session_newest">{t('review.cleanup.sortOptions.newest')}</option>
                      </select>
                    </label>

                    <label className="block">
                      <span className="mb-1 block text-[10px] font-semibold uppercase tracking-[0.14em] text-[color:var(--palace-muted)]">
                        {t('review.cleanup.sessionAge')}
                      </span>
                      <select
                        className="w-full rounded-md border border-[color:var(--palace-line)] bg-white/90 px-2 py-1.5 text-[11px] text-[color:var(--palace-ink)]"
                        value={cleanupAgeFilter}
                        onChange={(event) => setCleanupAgeFilter(event.target.value)}
                      >
                        <option value="all">{t('review.cleanup.ageFilters.all')}</option>
                        <option value="1">{t('review.cleanup.ageFilters.one')}</option>
                        <option value="7">{t('review.cleanup.ageFilters.seven')}</option>
                        <option value="30">{t('review.cleanup.ageFilters.thirty')}</option>
                      </select>
                    </label>

                    <label className="flex items-center gap-2">
                      <input
                        type="checkbox"
                        checked={cleanupWarningsOnly}
                        onChange={(event) => setCleanupWarningsOnly(event.target.checked)}
                      />
                      <span>{t('review.cleanup.warningsOnly')}</span>
                    </label>

                    <label className="flex items-center gap-2">
                      <input
                        type="checkbox"
                        checked={cleanupTopOnly}
                        onChange={(event) => setCleanupTopOnly(event.target.checked)}
                      />
                      <span>{t('review.cleanup.topOnly')}</span>
                    </label>
                  </div>

                  <div className="mt-3 space-y-2">
                    {cleanupCandidates.length === 0 ? (
                      <div className="rounded-md border border-dashed border-[color:var(--palace-line)] px-3 py-2 text-[11px]">
                        {t('review.cleanup.empty')}
                      </div>
                    ) : cleanupCandidates.map((session) => {
                      const isActive = session.session_id === currentSessionId;
                      return (
                        <button
                          key={session.session_id}
                          type="button"
                          onClick={() => {
                            setSelectedSnapshot(null);
                            setCurrentSessionId(session.session_id);
                          }}
                          className={clsx(
                            'w-full rounded-md border px-3 py-2 text-left transition',
                            isActive
                              ? 'border-[color:var(--palace-accent)] bg-[rgba(251,245,236,0.82)] text-[color:var(--palace-ink)]'
                              : 'border-[color:var(--palace-line)] bg-white/70 hover:border-[color:var(--palace-accent)]'
                          )}
                        >
                          <div className="flex min-w-0 items-center justify-between gap-2">
                            <span className="min-w-0 truncate font-medium">{session.session_id}</span>
                            <span className="text-[10px] uppercase tracking-[0.12em]">
                              {formatStorageBytes(session.estimated_reclaim_bytes)}
                            </span>
                          </div>
                          <div className="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-[10px] text-[color:var(--palace-muted)]">
                            <span>{t('review.cleanup.candidateSnapshots', { count: session.resource_count || 0 })}</span>
                            <span>{formatAgeDays(session.age_days, t)}</span>
                            {session.over_warning_threshold ? (
                              <span>{t('review.cleanup.overThreshold')}</span>
                            ) : null}
                          </div>
                        </button>
                      );
                    })}
                  </div>

                  {currentSessionMeta ? (
                    <div className="mt-3 rounded-md border border-[rgba(184,150,46,0.24)] bg-[rgba(249,241,228,0.9)] p-3">
                      <div className="text-[10px] font-semibold uppercase tracking-[0.12em] text-[color:var(--palace-ink)]">
                        {t('review.cleanup.previewTitle')}
                      </div>
                      <div className="mt-2 text-[11px] text-[color:var(--palace-muted)]">
                        {t('review.cleanup.previewSummary', {
                          sessionId: currentSessionMeta.session_id,
                          count: currentSessionMeta.resource_count || 0,
                          size: formatStorageBytes(currentSessionMeta.estimated_reclaim_bytes),
                        })}
                      </div>
                      <button
                        type="button"
                        onClick={handleClearSession}
                        disabled={mutationInFlight}
                        className="mt-3 w-full rounded-md border border-[rgba(184,150,46,0.24)] bg-white/90 px-3 py-2 text-[11px] font-semibold text-[color:var(--palace-ink)] transition hover:border-[color:var(--palace-accent)] hover:bg-[rgba(237,226,211,0.72)] disabled:cursor-not-allowed disabled:opacity-60"
                      >
                        {t('review.cleanup.deletePreviewed')}
                      </button>
                    </div>
                  ) : null}
                </div>
              </div>
            ) : null}
          </div>
        </div>

        {/* Snapshot List */}
        <div className="flex-1 overflow-y-auto py-2">
            {snapshots.length > 0 ? (
              <div className="px-4 pb-2">
                <label className="block">
                  <span className="mb-1 block text-[10px] font-semibold uppercase tracking-[0.14em] text-[color:var(--palace-muted)]">
                    {t('review.cleanup.snapshotSort')}
                  </span>
                  <select
                    className="w-full rounded-md border border-[color:var(--palace-line)] bg-white/90 px-2 py-1.5 text-[11px] text-[color:var(--palace-ink)]"
                    value={snapshotSort}
                    onChange={(event) => setSnapshotSort(event.target.value)}
                  >
                    <option value="recent">{t('review.cleanup.snapshotSortOptions.recent')}</option>
                    <option value="oldest">{t('review.cleanup.snapshotSortOptions.oldest')}</option>
                    <option value="size_desc">{t('review.cleanup.snapshotSortOptions.sizeDesc')}</option>
                    <option value="size_asc">{t('review.cleanup.snapshotSortOptions.sizeAsc')}</option>
                  </select>
                </label>
              </div>
            ) : null}
            {loading ? (
                <div className="p-8 flex justify-center">
                    <div className="w-6 h-6 border-2 border-amber-500/30 border-t-amber-500 rounded-full animate-spin"></div>
                </div>
            ) : (
                <SnapshotList 
                    snapshots={sortedSnapshots} 
                    selectedId={selectedSnapshot?.resource_id} 
                    onSelect={setSelectedSnapshot} 
                />
            )}
        </div>
      </div>

      {/* Main Stage */}
      <div className="relative flex min-w-0 flex-1 flex-col bg-[rgba(255,250,244,0.7)]">
        {/* Background Ambient Gradient */}
        <div className="pointer-events-none absolute left-0 right-0 top-0 h-96 bg-[radial-gradient(circle_at_top_left,rgba(198,165,126,0.2),rgba(246,242,234,0.08)_52%,transparent_72%)]" />

        {selectedSnapshot ? (
          <>
            {/* Context Header */}
            <div className="relative z-10 flex h-20 items-center justify-between border-b border-[color:var(--palace-line)] bg-white/62 px-8 backdrop-blur-sm">
              <div className="flex items-center gap-4 min-w-0">
                 <div className={clsx(
                    "w-10 h-10 rounded-full flex items-center justify-center border",
                    {
                      'create':         "bg-amber-950/10 border-amber-500/20 text-amber-400 shadow-[0_0_14px_rgba(179,133,79,0.16)]",
                      'create_alias':   "bg-amber-950/10 border-amber-500/20 text-amber-400 shadow-[0_0_14px_rgba(179,133,79,0.16)]",
                      'delete':         "bg-amber-950/10 border-amber-500/20 text-amber-400 shadow-[0_0_14px_rgba(163,124,82,0.14)]",
                      'modify_meta':    "bg-amber-950/10 border-amber-500/20 text-amber-400 shadow-[0_0_14px_rgba(163,124,82,0.14)]",
                      'modify_content': "bg-amber-950/10 border-amber-500/20 text-amber-400 shadow-[0_0_14px_rgba(163,124,82,0.14)]",
                      'modify':         "bg-amber-950/10 border-amber-500/20 text-amber-400 shadow-[0_0_14px_rgba(163,124,82,0.14)]",
                    }[selectedSnapshot.operation_type] || "bg-amber-950/10 border-amber-500/20 text-amber-400 shadow-[0_0_14px_rgba(163,124,82,0.14)]"
                 )}>
                    {{
                      'create':         <Database size={18} />,
                      'create_alias':   <Link2 size={18} />,
                      'delete':         <Trash2 size={18} />,
                      'modify_meta':    <Settings2 size={18} />,
                      'modify_content': <FileText size={18} />,
                      'modify':         <RefreshCw size={18} />,
                    }[selectedSnapshot.operation_type] || <RefreshCw size={18} />}
                 </div>
                 <div className="min-w-0 flex-1 flex flex-col">
                    <h2 className="min-w-0 font-display text-lg tracking-tight text-amber-50 truncate">
                        {selectedSnapshot.uri || selectedSnapshot.resource_id}
                    </h2>
                    <div className="flex items-center gap-2 text-xs text-stone-500">
                        <span className="bg-stone-800/50 px-1.5 py-0.5 rounded text-stone-400">
                          {t(`resourceTypes.${selectedSnapshot.resource_type}`, {
                            defaultValue: selectedSnapshot.resource_type,
                          })}
                        </span>
                        <span>•</span>
                        <span className="flex items-center gap-1 font-mono opacity-70">
                            <Clock size={10} />
                            {formatSnapshotTime(
                              selectedSnapshot.snapshot_time,
                              i18n.resolvedLanguage,
                              t('common.states.unknown')
                            )}
                        </span>
                    </div>
                 </div>
              </div>
              
              <div className="flex items-center gap-3">
                <button 
                    onClick={handleRollback}
                    disabled={mutationInFlight}
                    className="flex items-center gap-2 px-5 py-2 bg-stone-900 hover:bg-rose-950/30 border border-stone-700 hover:border-rose-800 text-stone-400 hover:text-rose-400 rounded-md transition-all duration-200 text-xs font-medium uppercase tracking-wider"
                >
                    <RotateCcw size={14} /> {t('review.reject')}
                </button>
                <button 
                    onClick={handleApprove}
                    disabled={mutationInFlight}
                    className="flex items-center gap-2 rounded-md border border-amber-600/40 bg-amber-950/35 px-6 py-2 text-xs font-bold uppercase tracking-wider text-amber-100 transition-all duration-200 hover:bg-amber-900/45 hover:border-amber-500/60 shadow-[0_0_15px_rgba(245,158,11,0.18)] hover:shadow-[0_0_20px_rgba(245,158,11,0.28)]"
                >
                    <Check size={14} /> {t('review.integrate')}
                </button>
              </div>
            </div>

            {/* Reading/Diff Area */}
            <div className="flex-1 overflow-y-auto px-8 py-8 custom-scrollbar">
               <div className="max-w-4xl mx-auto">
                   
                   {diffError ? (
                       <div className="mt-20 flex flex-col items-center justify-center text-rose-500 gap-6 animate-in fade-in zoom-in duration-300">
                           <div className="w-20 h-20 bg-rose-950/20 rounded-full flex items-center justify-center border border-rose-900/50 shadow-xl">
                                <Activity size={32} />
                           </div>
                           <div className="text-center">
                                <p className="text-lg font-medium text-rose-200">{t('review.currentDiffFailure')}</p>
                                <p className="text-rose-400/60 mt-2 max-w-md text-sm">{diffError}</p>
                           </div>
                           <button 
                               onClick={() => loadDiff(currentSessionId, selectedSnapshot.resource_id)} 
                               className="px-6 py-2 bg-stone-800/50 hover:bg-stone-800 rounded-full text-stone-300 text-xs transition-colors border border-stone-700"
                           >
                               {t('review.retryConnection')}
                           </button>
                       </div>
                   ) : diffData ? (
                       <div className="animate-in fade-in slide-in-from-bottom-4 duration-500">
                           {/* Diff Summary Badge */}
                           <div className="mb-6 flex justify-end">
                               <div className={clsx(
                                   "inline-flex items-center gap-2 px-3 py-1 rounded-full text-[10px] font-bold uppercase tracking-widest border",
                                   diffData.has_changes 
                                    ? "bg-amber-500/5 border-amber-500/20 text-amber-500" 
                                    : "bg-stone-800/50 border-stone-700 text-stone-500"
                               )}>
                                   {diffData.has_changes ? t('review.modificationDetected') : t('review.noContentDeviation')}
                               </div>
                           </div>

                           {renderMetadataChanges()}
                           {renderSurvivingPaths()}
                           
                           {/* The Core Content */}
                           <div className="bg-stone-900/50 rounded-xl border border-stone-800/50 p-1 min-h-[200px] shadow-2xl relative overflow-hidden">
                                <div className="absolute top-0 left-0 w-full h-1 bg-gradient-to-r from-transparent via-amber-500/20 to-transparent opacity-50"></div>
                                <div className="p-6 md:p-10">
                                    <SimpleDiff 
                                        oldText={diffData.snapshot_data?.content ?? ''} 
                                        newText={diffData.current_data?.content ?? ''} 
                                    />
                                </div>
                           </div>
                       </div>
                   ) : (
                       <div className="flex flex-col items-center justify-center h-64 text-stone-700">
                           <div className="w-2 h-2 bg-amber-500 rounded-full animate-ping mb-4"></div>
                           <span className="text-xs tracking-widest uppercase opacity-50">{t('review.synchronizing')}</span>
                       </div>
                   )}
               </div>
            </div>
          </>
        ) : diffError ? (
           <div className="flex-1 flex flex-col items-center justify-center text-rose-500 gap-4">
             <Activity size={48} className="opacity-20" />
             <p className="text-sm font-medium opacity-50">{t('common.states.connectionLost')}</p>
             <p className="max-w-md px-6 text-center text-xs text-rose-400/80">{diffError}</p>
           </div>
        ) : (
          <div className="flex-1 flex flex-col items-center justify-center text-stone-700 gap-6 select-none">
            <div className="relative">
                <div className="absolute inset-0 bg-amber-500/20 blur-3xl rounded-full opacity-20 animate-pulse"></div>
                <Layout size={64} className="opacity-20 relative z-10" />
            </div>
            <div className="text-center">
                <p className="text-lg font-light text-stone-500">{t('common.states.awaitingInput')}</p>
                <p className="text-xs text-stone-600 mt-2 tracking-wide uppercase">{t('review.selectFragment')}</p>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

export default ReviewPage

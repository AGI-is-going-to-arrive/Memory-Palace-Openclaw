import React, { useMemo } from 'react';
import clsx from 'clsx';
import { useTranslation } from 'react-i18next';
import GlassCard from '../../../components/GlassCard';
import {
  AlertTriangle,
  CheckCircle2,
  RefreshCw,
  ShieldAlert,
  Wrench,
  XCircle,
} from 'lucide-react';
import Badge from './Badge';
import {
  getHealthI18nValue,
  getDegradeReasonHint,
} from '../observabilityI18n';

const formatMetricLabel = (value) =>
  String(value || '')
    .replace(/_/g, ' ')
    .trim();

function AggregatedHealthPanel({ summary, onReindex, reindexing }) {
  const { i18n } = useTranslation();
  const lng = i18n.resolvedLanguage || i18n.language || 'en';

  const healthData = useMemo(() => {
    if (!summary) return null;

    const status = summary.status || 'ok';
    const indexHealth = summary.health?.index || {};
    const transport = summary.transport || {};
    const searchStats = summary.search_stats || {};
    const topDegradeReasons = Array.isArray(searchStats.top_degrade_reasons)
      ? searchStats.top_degrade_reasons
      : [];

    const degradeSources = [];

    if (indexHealth.degraded) {
      degradeSources.push({
        source: 'index',
        reason: indexHealth.reason || 'index_degraded',
      });
    }
    if (transport.degraded) {
      degradeSources.push({
        source: 'transport',
        reason: transport.reason || 'transport_degraded',
      });
    }
    if (indexHealth.semantic_vector_block_reason) {
      degradeSources.push({
        source: 'semantic_vector',
        reason: indexHealth.semantic_vector_block_reason,
      });
    }

    const detectedDims = Array.isArray(indexHealth.semantic_vector_detected_dims)
      ? indexHealth.semantic_vector_detected_dims
      : [];
    const hasMixedDims = detectedDims.length > 1;

    if (hasMixedDims && !degradeSources.some((s) => s.source === 'semantic_vector')) {
      degradeSources.push({
        source: 'semantic_vector',
        reason: 'mixed_embedding_dimensions',
      });
    }

    const isDegraded = status === 'degraded' || degradeSources.length > 0;
    const isSevere = hasMixedDims || degradeSources.some(
      (s) => s.reason?.includes('mismatch') || s.reason?.includes('mixed')
    );

    return {
      isDegraded,
      isSevere,
      degradeSources,
      topDegradeReasons,
      hasMixedDims,
      detectedDims,
      blockReason: indexHealth.semantic_vector_block_reason || '',
    };
  }, [summary]);

  if (!healthData) return null;

  const { isDegraded, isSevere, degradeSources, topDegradeReasons, hasMixedDims, detectedDims, blockReason } = healthData;

  const borderClass = isSevere
    ? 'border-red-400/60'
    : isDegraded
      ? 'border-amber-400/60'
      : 'border-emerald-400/60';

  const StatusIcon = isSevere ? XCircle : isDegraded ? AlertTriangle : CheckCircle2;
  const statusColor = isSevere
    ? 'text-red-500'
    : isDegraded
      ? 'text-amber-500'
      : 'text-emerald-500';

  const ht = (key, interpolations = {}) =>
    getHealthI18nValue(key, lng, interpolations);

  const statusLabel = isSevere
    ? ht('obs.health.severe')
    : isDegraded
      ? ht('obs.health.degraded')
      : ht('obs.health.healthy');

  return (
    <GlassCard
      className={clsx('mb-5 border-2 p-5', borderClass)}
      data-testid="aggregated-health-panel"
    >
      <div className="flex items-start justify-between gap-4">
        <div className="flex items-center gap-3">
          <StatusIcon size={22} className={statusColor} aria-hidden="true" />
          <div>
            <h2 className="text-sm font-semibold text-[color:var(--palace-ink)]">
              {ht('obs.health.title')}
            </h2>
            <p className={clsx('mt-0.5 text-xs font-medium', statusColor)}>
              {statusLabel}
            </p>
          </div>
        </div>

        {hasMixedDims && (
          <button
            type="button"
            onClick={onReindex}
            disabled={reindexing}
            className="inline-flex cursor-pointer items-center gap-2 rounded-lg border border-red-400/50 bg-red-50/80 px-3 py-2 text-xs font-medium text-red-700 transition-colors hover:bg-red-100/90 disabled:cursor-not-allowed disabled:opacity-50 focus:outline-none focus:ring-2 focus:ring-red-400/35"
          >
            {reindexing ? (
              <RefreshCw size={14} className="animate-spin" />
            ) : (
              <Wrench size={14} />
            )}
            {reindexing ? ht('obs.health.reindexing') : ht('obs.health.action_reindex')}
          </button>
        )}
      </div>

      {isDegraded && degradeSources.length > 0 && (
        <div className="mt-4">
          <h3 className="mb-2 text-[11px] uppercase tracking-[0.14em] text-[color:var(--palace-muted)]">
            {ht('obs.health.reason')}
          </h3>
          <ul className="space-y-2">
            {degradeSources.map((item, idx) => {
              const hint = getDegradeReasonHint(item.reason, lng);
              return (
                <li
                  key={`${item.source}-${idx}`}
                  className="rounded-lg border border-[color:var(--palace-line)] bg-[rgba(255,250,244,0.72)] px-3 py-2"
                >
                  <div className="flex items-start gap-2">
                    <ShieldAlert size={14} className="mt-0.5 shrink-0 text-amber-500" aria-hidden="true" />
                    <div>
                      <span className="text-xs font-medium text-[color:var(--palace-ink)]">
                        {formatMetricLabel(item.reason)}
                      </span>
                      <span className="ml-2 text-[10px] text-[color:var(--palace-muted)]">
                        ({item.source})
                      </span>
                      {hint && (
                        <p className="mt-1 text-[11px] text-[color:var(--palace-muted)]">
                          {ht('obs.health.fix_hint_prefix')} {hint}
                        </p>
                      )}
                    </div>
                  </div>
                </li>
              );
            })}
          </ul>
        </div>
      )}

      {blockReason && (
        <p className="mt-3 text-xs text-red-600" data-testid="health-block-reason">
          {ht('obs.health.block_reason', { reason: blockReason })}
        </p>
      )}

      {hasMixedDims && (
        <div className="mt-2">
          <p className="text-xs font-medium text-red-600" data-testid="health-mixed-dims-warning">
            {ht('obs.health.reindex_needed')}
          </p>
          <p className="mt-1 text-[11px] text-[color:var(--palace-muted)]" data-testid="health-detected-dims">
            {ht('obs.health.detected_dims', { dims: detectedDims.join(', ') })}
          </p>
        </div>
      )}

      {topDegradeReasons.length > 0 && (
        <div className="mt-4">
          <h3 className="mb-2 text-[11px] uppercase tracking-[0.14em] text-[color:var(--palace-muted)]">
            {ht('obs.health.top_degrade_reasons')}
          </h3>
          <div className="flex flex-wrap gap-2">
            {topDegradeReasons.map((entry) => (
              <Badge key={entry.reason} tone={isDegraded ? 'warn' : 'neutral'}>
                {formatMetricLabel(entry.reason)}: {entry.count}
              </Badge>
            ))}
          </div>
        </div>
      )}
    </GlassCard>
  );
}

export default React.memo(AggregatedHealthPanel);

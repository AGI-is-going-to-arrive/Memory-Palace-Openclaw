import React from 'react';
import clsx from 'clsx';
import { useTranslation } from 'react-i18next';
import {
  Activity,
  Database,
  Gauge,
  Search,
  TimerReset,
  Zap,
} from 'lucide-react';

function StatCard({ icon: Icon, label, value, hint, tone = 'neutral' }) {
  return (
    <div
      className={clsx(
        'rounded-2xl border p-4 backdrop-blur-sm transition duration-200 shadow-[var(--palace-shadow-sm)]',
        tone === 'good' && 'border-[rgba(179,133,79,0.45)] bg-[rgba(251,245,236,0.9)]',
        tone === 'warn' && 'border-[rgba(200,171,134,0.65)] bg-[rgba(244,236,224,0.92)]',
        tone === 'danger' && 'border-[rgba(143,106,69,0.5)] bg-[rgba(236,224,207,0.88)]',
        tone === 'neutral' && 'border-[color:var(--palace-line)] bg-[rgba(255,250,244,0.9)]',
      )}
    >
      <div className="mb-3 flex items-center justify-between">
        <span className="text-[11px] uppercase tracking-[0.16em] text-[color:var(--palace-muted)]">
          {label}
        </span>
        <Icon size={14} className="text-[color:var(--palace-accent-2)]" />
      </div>
      <div className="text-2xl font-semibold text-[color:var(--palace-ink)]">{value}</div>
      <div className="mt-1 text-xs text-[color:var(--palace-muted)]">{hint}</div>
    </div>
  );
}

export default function StatCards({ summary, formatNumber, formatMs }) {
  const { t, i18n } = useTranslation();
  const searchStats = summary?.search_stats || {};
  const latency = searchStats.latency_ms || {};
  const indexLatency = summary?.index_latency || {};
  const cleanupQueryStats = summary?.cleanup_query_stats || {};
  const cleanupLatency = cleanupQueryStats.latency_ms || {};

  return (
    <section className="mb-5 grid gap-3 sm:grid-cols-2 xl:grid-cols-6">
      <StatCard
        icon={Search}
        label={t('observability.stats.queries')}
        value={formatNumber(searchStats.total_queries, i18n.resolvedLanguage)}
        hint={t('observability.stats.degraded', {
          count: formatNumber(searchStats.degraded_queries, i18n.resolvedLanguage),
        })}
        tone="neutral"
      />
      <StatCard
        icon={TimerReset}
        label={t('observability.stats.latency')}
        value={formatMs(latency.avg)}
        hint={`p95 ${formatMs(latency.p95)}`}
        tone="neutral"
      />
      <StatCard
        icon={Zap}
        label={t('observability.stats.cacheHitRatio')}
        value={`${((searchStats.cache_hit_ratio || 0) * 100).toFixed(1)}%`}
        hint={t('observability.stats.hitQueries', {
          count: formatNumber(searchStats.cache_hit_queries, i18n.resolvedLanguage),
        })}
        tone={searchStats.cache_hit_ratio > 0.4 ? 'good' : 'neutral'}
      />
      <StatCard
        icon={Gauge}
        label={t('observability.stats.indexLatency')}
        value={formatMs(indexLatency.avg_ms)}
        hint={`samples ${formatNumber(indexLatency.samples)}`}
        tone={indexLatency.samples > 0 ? 'neutral' : 'warn'}
      />
      <StatCard
        icon={Database}
        label={t('observability.stats.cleanupP95')}
        value={formatMs(cleanupLatency.p95)}
        hint={`slow ${formatNumber(cleanupQueryStats.slow_queries)} (>=${formatMs(cleanupQueryStats.slow_threshold_ms)})`}
        tone={cleanupQueryStats.slow_queries > 0 ? 'warn' : 'neutral'}
      />
      <StatCard
        icon={Activity}
        label={t('observability.stats.cleanupIndexHit')}
        value={`${((cleanupQueryStats.index_hit_ratio || 0) * 100).toFixed(1)}%`}
        hint={`full scan ${formatNumber(cleanupQueryStats.full_scan_queries)}`}
        tone={
          cleanupQueryStats.index_hit_ratio >= 0.9
            ? 'good'
            : cleanupQueryStats.index_hit_ratio >= 0.5
              ? 'neutral'
              : 'warn'
        }
      />
    </section>
  );
}

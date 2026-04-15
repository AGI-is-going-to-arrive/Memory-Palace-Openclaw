import React from 'react';
import { useTranslation } from 'react-i18next';
import { Gauge } from 'lucide-react';
import Badge, { MemoTraceBadgeList as TraceBadgeList } from './Badge';
import {
  localizeObservabilityStatus,
  localizeObservabilityText,
} from '../observabilityI18n';

const PANEL_CLASS =
  'rounded-2xl border border-[color:var(--palace-line)] bg-[rgba(255,250,244,0.9)] p-4 shadow-[var(--palace-shadow-sm)] backdrop-blur-sm';

const formatMetricLabel = (value) =>
  String(value || '')
    .replace(/_/g, ' ')
    .trim();

/**
 * TransportDiagnosticsPanel renders the transport diagnostics section.
 *
 * All data is passed as props from the parent ObservabilityPage which owns
 * the state and memoized values.
 */
function TransportDiagnosticsPanel({
  transport,
  transportDiagnostics,
  transportLastReport,
  transportLastReportChecks,
  transportExceptionBreakdown,
  transportExceptionCategoryEntries,
  transportExceptionToolEntries,
  transportExceptionItems: _transportExceptionItems,
  visibleTransportExceptionItems,
  hiddenTransportExceptionCount,
  transportSignatureEntries,
  visibleTransportSignatureItems,
  hiddenTransportSignatureCount,
  transportIncidentEntries,
  visibleTransportIncidentItems,
  hiddenTransportIncidentCount,
  showTransportInstances,
  visibleTransportInstances,
  hiddenTransportInstanceCount,
  visibleTransportChecks,
  hiddenTransportCheckCount,
  recentTransportEvents,
  formatDateTime,
  formatTraceSummaryValue,
  getTransportCauseDetails,
}) {
  const { t, i18n } = useTranslation();

  return (
    <div className={PANEL_CLASS}>
      <h3 className="mb-3 flex items-center gap-2 text-sm font-semibold text-[color:var(--palace-ink)]">
        <Gauge size={15} className="text-[color:var(--palace-accent)]" />
        {t('observability.transport.title')}
      </h3>
      {!transport?.available ? (
        <p className="text-xs text-[color:var(--palace-muted)]">
          {t('observability.transport.unavailable', { reason: transport?.reason || 'transport_trace_unavailable' })}
        </p>
      ) : (
        <div className="space-y-3 text-xs text-[color:var(--palace-muted)]">
          <div className="flex flex-wrap gap-2">
            <Badge tone={transport?.degraded ? 'warn' : 'good'}>
              {t('observability.transport.status', { value: transport?.status || 'unknown' })}
            </Badge>
            <Badge tone="neutral">
              {t('observability.transport.active', { value: transport?.active_transport || '-' })}
            </Badge>
            <Badge tone="neutral">
              {t('observability.transport.configured', { value: transport?.configured_transport || '-' })}
            </Badge>
          </div>
          <div className="space-y-1">
            <p>{t('observability.transport.snapshotCount', { value: transport.snapshot_count ?? 0 })}</p>
            <p>{t('observability.transport.connectAttempts', { value: transportDiagnostics.connect_attempts ?? 0 })}</p>
            <p>
              {t('observability.transport.connectLatency', {
                value: formatTraceSummaryValue(
                  transportDiagnostics.connect_latency_ms,
                  'connect_latency_ms'
                ),
              })}
            </p>
            <p>{t('observability.transport.connectRetries', { value: transportDiagnostics.connect_retry_count ?? 0 })}</p>
            <p>{t('observability.transport.callRetries', { value: transportDiagnostics.call_retry_count ?? 0 })}</p>
            <p>{t('observability.transport.fallbackCount', { value: transportDiagnostics.fallback_count ?? 0 })}</p>
            <p>{t('observability.transport.reuseCount', { value: transportDiagnostics.reuse_count ?? 0 })}</p>
            <p>{t('observability.transport.lastConnectedAt', { value: formatDateTime(transportDiagnostics.last_connected_at, i18n.resolvedLanguage) })}</p>
            <p>{t('observability.transport.lastHealthCheckAt', { value: formatDateTime(transportDiagnostics.last_health_check_at, i18n.resolvedLanguage) })}</p>
            <p>{t('observability.transport.lastError', { value: transportDiagnostics.last_error || '-' })}</p>
            <p>{t('observability.transport.lastHealthError', { value: transportDiagnostics.last_health_check_error || '-' })}</p>
          </div>
          {Array.isArray(transport?.fallback_order) && transport.fallback_order.length > 0 && (
            <div className="rounded-lg border border-[color:var(--palace-line)] bg-[rgba(255,250,244,0.72)] p-3">
              <div className="mb-2 text-[11px] uppercase tracking-[0.14em] text-[color:var(--palace-muted)]">
                {t('observability.transport.fallbackOrder')}
              </div>
              <div className="flex flex-wrap gap-2">
                {transport.fallback_order.map((entry) => (
                  <Badge key={entry} tone="neutral">
                    {entry}
                  </Badge>
                ))}
              </div>
            </div>
          )}
          <div className="rounded-lg border border-[color:var(--palace-line)] bg-[rgba(255,250,244,0.72)] p-3">
            <div className="mb-2 text-[11px] uppercase tracking-[0.14em] text-[color:var(--palace-muted)]">
              {t('observability.transport.exceptionBreakdown')}
            </div>
            {Number(transportExceptionBreakdown?.total || 0) <= 0 ? (
              <p>{t('observability.transport.noExceptions')}</p>
            ) : (
              <div className="space-y-3">
                <div className="flex flex-wrap gap-2">
                  {transportExceptionBreakdown?.status_counts?.fail > 0 && (
                    <Badge tone="danger">
                      {t('observability.transport.failCount', {
                        count: transportExceptionBreakdown.status_counts.fail,
                      })}
                    </Badge>
                  )}
                  {transportExceptionBreakdown?.status_counts?.warn > 0 && (
                    <Badge tone="warn">
                      {t('observability.transport.warnCount', {
                        count: transportExceptionBreakdown.status_counts.warn,
                      })}
                    </Badge>
                  )}
                  {transportExceptionBreakdown?.last_exception_at && (
                    <Badge tone="neutral">
                      {t('observability.transport.lastExceptionAt', {
                        value: formatDateTime(
                          transportExceptionBreakdown.last_exception_at,
                          i18n.resolvedLanguage
                        ),
                      })}
                    </Badge>
                  )}
                </div>
                <TraceBadgeList
                  title={t('observability.transport.exceptionCategories')}
                  entries={transportExceptionCategoryEntries}
                />
                <TraceBadgeList
                  title={t('observability.transport.exceptionTools')}
                  entries={transportExceptionToolEntries}
                />
                {Number(
                  transportExceptionBreakdown?.incident_breakdown?.incident_count || 0
                ) > 0 && (
                  <div className="space-y-2">
                    <div className="flex flex-wrap gap-2">
                      <Badge tone="neutral">
                        {t('observability.transport.incidentCount', {
                          count:
                            transportExceptionBreakdown.incident_breakdown.incident_count,
                        })}
                      </Badge>
                    </div>
                    <TraceBadgeList
                      title={t('observability.transport.canonicalCauses')}
                      entries={transportIncidentEntries}
                    />
                    {visibleTransportIncidentItems.map((item, index) => {
                      const incidentParts = [
                        item.category,
                        item.highest_status,
                        item.tool || item.check_id || item.transport,
                      ]
                        .filter(Boolean)
                        .map((part) => formatMetricLabel(part));
                      const causeDetails = getTransportCauseDetails(
                        item.canonical_cause,
                        t,
                        item.cause_family
                      );
                      return (
                        <div
                          key={`${item.canonical_cause}-${index}` }
                          className="rounded border border-[color:var(--palace-line)] bg-white/70 px-2 py-2"
                        >
                          <div className="flex flex-wrap gap-2">
                            <Badge
                              tone={item.highest_status === 'fail' ? 'danger' : 'warn'}
                            >
                              {item.highest_status}
                            </Badge>
                            <Badge tone="neutral">
                              {t('observability.transport.signatureCount', {
                                count: item.signal_count,
                              })}
                            </Badge>
                            {causeDetails.family && (
                              <Badge tone="neutral">
                                {t(
                                  `observability.transport.causeFamilies.${causeDetails.family}`,
                                  {
                                    defaultValue: formatMetricLabel(causeDetails.family),
                                  }
                                )}
                              </Badge>
                            )}
                          </div>
                          <div className="mt-1 space-y-1">
                            <p>{incidentParts.join(' · ') || causeDetails.label}</p>
                            <p>{causeDetails.label}</p>
                            {causeDetails.action && (
                              <p>
                                {t('observability.transport.recommendedAction', {
                                  value: causeDetails.action,
                                })}
                              </p>
                            )}
                            <p>{localizeObservabilityText(item.sample_message, t)}</p>
                            <p>
                              {t('observability.transport.rawCauseCode', {
                                value: item.canonical_cause,
                              })}
                            </p>
                            {item.last_seen_at && (
                              <p>
                                {t('observability.transport.lastSeenAt', {
                                  value: formatDateTime(
                                    item.last_seen_at,
                                    i18n.resolvedLanguage
                                  ),
                                })}
                              </p>
                            )}
                            {item.sources?.length > 0 && (
                              <p>
                                {t('observability.transport.signatureSources', {
                                  value: item.sources.join(', '),
                                })}
                              </p>
                            )}
                          </div>
                        </div>
                      );
                    })}
                    {hiddenTransportIncidentCount > 0 && (
                      <p className="text-[10px] text-[color:var(--palace-muted)]/80">
                        {t('observability.transport.moreItems', {
                          count: hiddenTransportIncidentCount,
                        })}
                      </p>
                    )}
                  </div>
                )}
                {Number(
                  transportExceptionBreakdown?.signature_breakdown?.total || 0
                ) > 0 && (
                  <div className="space-y-2">
                    <TraceBadgeList
                      title={t('observability.transport.exceptionSignatures')}
                      entries={transportSignatureEntries}
                    />
                    {visibleTransportSignatureItems.map((item, index) => {
                    const signatureParts = [
                      item.category,
                      localizeObservabilityStatus(item.status, t),
                      item.tool || item.check_id || item.transport,
                    ]
                        .filter(Boolean)
                        .map((part) => formatMetricLabel(part));
                      return (
                        <div
                          key={`${item.signature || 'transport-signature'}-${index}`}
                          className="rounded border border-[color:var(--palace-line)] bg-white/70 px-2 py-2"
                        >
                          <div className="flex flex-wrap gap-2">
                            <Badge tone={item.status === 'fail' ? 'danger' : 'warn'}>
                              {localizeObservabilityStatus(item.status, t)}
                            </Badge>
                            <Badge tone="neutral">
                              {t('observability.transport.signatureCount', {
                                count: item.signal_count,
                              })}
                            </Badge>
                          </div>
                          <div className="mt-1 space-y-1">
                            <p>{signatureParts.join(' · ') || item.signature}</p>
                            <p>{localizeObservabilityText(item.message, t)}</p>
                            {item.sources?.length > 0 && (
                              <p>
                                {t('observability.transport.signatureSources', {
                                  value: item.sources.join(', '),
                                })}
                              </p>
                            )}
                          </div>
                        </div>
                      );
                    })}
                    {hiddenTransportSignatureCount > 0 && (
                      <p className="text-[10px] text-[color:var(--palace-muted)]/80">
                        {t('observability.transport.moreItems', {
                          count: hiddenTransportSignatureCount,
                        })}
                      </p>
                    )}
                  </div>
                )}
                <div className="space-y-2">
                  {visibleTransportExceptionItems.map((item, index) => {
                    const parts = [
                      item.category || item.source,
                      localizeObservabilityStatus(item.status, t),
                      item.tool || item.check_id || item.transport,
                    ]
                      .filter(Boolean)
                      .map((part) => formatMetricLabel(part));
                    return (
                      <div
                        key={`${item.source || 'transport-exception'}-${index}`}
                        className="rounded border border-[color:var(--palace-line)] bg-white/70 px-2 py-2"
                      >
                        <div className="flex flex-wrap gap-2">
                          <Badge tone={item.status === 'fail' ? 'danger' : 'warn'}>
                            {localizeObservabilityStatus(item.status, t)}
                          </Badge>
                          {item.count > 1 && <Badge tone="neutral">x{item.count}</Badge>}
                        </div>
                        <div className="mt-1 space-y-1">
                          <p>{parts.join(' · ') || formatMetricLabel(item.source || 'transport')}</p>
                          <p>{localizeObservabilityText(item.message, t)}</p>
                        </div>
                      </div>
                    );
                  })}
                  {hiddenTransportExceptionCount > 0 && (
                    <p className="text-[10px] text-[color:var(--palace-muted)]/80">
                      {t('observability.transport.moreItems', { count: hiddenTransportExceptionCount })}
                    </p>
                  )}
                </div>
              </div>
            )}
          </div>
          {transportLastReport && (
            <div className="rounded-lg border border-[color:var(--palace-line)] bg-[rgba(255,250,244,0.72)] p-3">
              <div className="mb-2 flex flex-wrap items-center gap-2">
                <Badge tone={transportLastReport.status === 'pass' ? 'good' : transportLastReport.status === 'fail' ? 'danger' : 'warn'}>
                  {t('observability.transport.lastReport', { command: transportLastReport.command || 'diagnostic' })}
                </Badge>
                <span>{localizeObservabilityText(transportLastReport.summary, t) || '-'}</span>
              </div>
              {transportLastReportChecks.length > 0 && (
                <div className="space-y-1">
                  {visibleTransportChecks.map((check) => (
                    <p key={`${check.id}:${check.status}`}>
                      {check.id}: {localizeObservabilityStatus(check.status, t)} · {localizeObservabilityText(check.message, t)}
                    </p>
                  ))}
                  {hiddenTransportCheckCount > 0 && (
                    <p className="text-[10px] text-[color:var(--palace-muted)]/80">
                      {t('observability.transport.moreItems', { count: hiddenTransportCheckCount })}
                    </p>
                  )}
                </div>
              )}
            </div>
          )}
          {showTransportInstances && (
            <div className="rounded-lg border border-[color:var(--palace-line)] bg-[rgba(255,250,244,0.72)] p-3">
              <div className="mb-2 text-[11px] uppercase tracking-[0.14em] text-[color:var(--palace-muted)]">
                {t('observability.transport.instances')}
              </div>
              <div className="space-y-2">
                {visibleTransportInstances.map((instance, index) => (
                  <div key={`${instance.instance_id || 'instance'}-${index}`} className="rounded border border-[color:var(--palace-line)] bg-white/70 px-2 py-2">
                    <div className="flex flex-wrap gap-2">
                      <Badge tone={instance.status === 'pass' ? 'good' : instance.status === 'fail' ? 'danger' : 'warn'}>
                        {localizeObservabilityStatus(instance.status || 'unknown', t)}
                      </Badge>
                      <Badge tone="neutral">{instance.instance_id || 'instance'}</Badge>
                      {instance.active_transport && <Badge tone="neutral">{instance.active_transport}</Badge>}
                    </div>
                    <div className="mt-1 space-y-1">
                      <p>{formatDateTime(instance.updated_at, i18n.resolvedLanguage)}</p>
                      {instance.source_path && (
                        <p className="text-[10px] text-[color:var(--palace-muted)]/80">
                          {t('observability.transport.debugPath', { value: instance.source_path })}
                        </p>
                      )}
                    </div>
                  </div>
                ))}
                {hiddenTransportInstanceCount > 0 && (
                  <p className="text-[10px] text-[color:var(--palace-muted)]/80">
                    {t('observability.transport.moreItems', { count: hiddenTransportInstanceCount })}
                  </p>
                )}
              </div>
            </div>
          )}
          {recentTransportEvents.length > 0 && (
            <div className="rounded-lg border border-[color:var(--palace-line)] bg-[rgba(255,250,244,0.72)] p-3">
              <div className="mb-2 text-[11px] uppercase tracking-[0.14em] text-[color:var(--palace-muted)]">
                {t('observability.transport.recentEvents')}
              </div>
              <div className="space-y-2">
                {recentTransportEvents.map((event, index) => (
                  <div key={`${event.at || 'event'}-${index}`} className="rounded border border-[color:var(--palace-line)] bg-white/70 px-2 py-2">
                    <div className="flex flex-wrap gap-2">
                      <Badge tone={event.status === 'pass' ? 'good' : event.status === 'fail' ? 'danger' : event.status === 'warn' ? 'warn' : 'neutral'}>
                        {localizeObservabilityStatus(event.status || 'unknown', t)}
                      </Badge>
                      <Badge tone="neutral">{event.category || 'transport'}</Badge>
                      {event.transport && <Badge tone="neutral">{event.transport}</Badge>}
                      {event.tool && <Badge tone="neutral">{event.tool}</Badge>}
                    </div>
                    <div className="mt-1 space-y-1">
                      <p>{formatDateTime(event.at, i18n.resolvedLanguage)}</p>
                      {event.message && <p>{localizeObservabilityText(event.message, t)}</p>}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export default React.memo(TransportDiagnosticsPanel);

export const TRANSPORT_CAUSE_DETAILS = {
  healthcheck_auth_failure: {
    family: 'auth',
    labelKey: 'observability.transport.causeLabels.healthcheck_auth_failure',
    labelDefault: 'Health check authentication failure',
    actionKey: 'observability.transport.causeActions.healthcheck_auth_failure',
    actionDefault:
      'Verify MCP_API_KEY alignment between plugin config, backend env, and transport headers.',
  },
  transport_connect_fallback: {
    family: 'latency',
    labelKey: 'observability.transport.causeLabels.transport_connect_fallback',
    labelDefault: 'Connected only after transport fallback',
    actionKey: 'observability.transport.causeActions.transport_connect_fallback',
    actionDefault:
      'Review the preferred transport health; the system is currently relying on fallback.',
  },
  transport_timeout: {
    family: 'latency',
    labelKey: 'observability.transport.causeLabels.transport_timeout',
    labelDefault: 'Transport timeout',
    actionKey: 'observability.transport.causeActions.transport_timeout',
    actionDefault:
      'Check upstream latency and timeout settings before raising retry ceilings.',
  },
  transport_connection_refused: {
    family: 'network',
    labelKey: 'observability.transport.causeLabels.transport_connection_refused',
    labelDefault: 'Connection refused',
    actionKey: 'observability.transport.causeActions.transport_connection_refused',
    actionDefault:
      'Confirm the backend or provider endpoint is listening on the target host and port.',
  },
  transport_network_unreachable: {
    family: 'network',
    labelKey: 'observability.transport.causeLabels.transport_network_unreachable',
    labelDefault: 'Network unreachable',
    actionKey: 'observability.transport.causeActions.transport_network_unreachable',
    actionDefault:
      'Check route tables, VPN or container networking, and whether the target host is reachable.',
  },
  transport_connection_reset: {
    family: 'network',
    labelKey: 'observability.transport.causeLabels.transport_connection_reset',
    labelDefault: 'Connection reset during transport',
    actionKey: 'observability.transport.causeActions.transport_connection_reset',
    actionDefault:
      'Inspect upstream connection stability, proxy idle timeouts, and abrupt socket resets.',
  },
  transport_dns_failure: {
    family: 'network',
    labelKey: 'observability.transport.causeLabels.transport_dns_failure',
    labelDefault: 'DNS / host resolution failure',
    actionKey: 'observability.transport.causeActions.transport_dns_failure',
    actionDefault:
      'Verify DNS and host mapping for the configured endpoint, especially in Docker.',
  },
  transport_tls_failure: {
    family: 'tls',
    labelKey: 'observability.transport.causeLabels.transport_tls_failure',
    labelDefault: 'TLS / certificate failure',
    actionKey: 'observability.transport.causeActions.transport_tls_failure',
    actionDefault:
      'Check certificate trust, expiry, and whether the endpoint should use HTTP instead of HTTPS.',
  },
  transport_rate_limited: {
    family: 'upstream',
    labelKey: 'observability.transport.causeLabels.transport_rate_limited',
    labelDefault: 'Rate limited by upstream',
    actionKey: 'observability.transport.causeActions.transport_rate_limited',
    actionDefault: 'Reduce request burst or switch to a higher-quota key before retrying.',
  },
  transport_payload_too_large: {
    family: 'upstream',
    labelKey: 'observability.transport.causeLabels.transport_payload_too_large',
    labelDefault: 'Payload rejected as too large',
    actionKey: 'observability.transport.causeActions.transport_payload_too_large',
    actionDefault:
      'Trim payload size, lower attachment verbosity, or increase the upstream request size limit.',
  },
  transport_upstream_unavailable: {
    family: 'upstream',
    labelKey: 'observability.transport.causeLabels.transport_upstream_unavailable',
    labelDefault: 'Upstream unavailable',
    actionKey: 'observability.transport.causeActions.transport_upstream_unavailable',
    actionDefault: 'Inspect upstream provider health and retry after the service recovers.',
  },
  transport_protocol_error: {
    family: 'upstream',
    labelKey: 'observability.transport.causeLabels.transport_protocol_error',
    labelDefault: 'Unexpected upstream protocol response',
    actionKey: 'observability.transport.causeActions.transport_protocol_error',
    actionDefault:
      'Confirm the upstream returns the expected JSON protocol and content-type for this transport.',
  },
  sqlite_database_locked: {
    family: 'storage',
    labelKey: 'observability.transport.causeLabels.sqlite_database_locked',
    labelDefault: 'SQLite database lock contention',
    actionKey: 'observability.transport.causeActions.sqlite_database_locked',
    actionDefault:
      'Reduce concurrent SQLite activity or inspect lock holders before retrying.',
  },
  transport_snapshot_load_failed: {
    family: 'observability',
    labelKey: 'observability.transport.causeLabels.transport_snapshot_load_failed',
    labelDefault: 'Transport snapshot load failed',
    actionKey: 'observability.transport.causeActions.transport_snapshot_load_failed',
    actionDefault:
      'Remove corrupted snapshot files and rerun doctor to regenerate diagnostics.',
  },
};

const formatMetricLabel = (value) =>
  String(value || '')
    .replace(/_/g, ' ')
    .trim();

export const getTransportCauseDetails = (cause, t, familyOverride = '') => {
  const code = String(cause || '').trim();
  const details = TRANSPORT_CAUSE_DETAILS[code];
  if (!details) {
    return {
      family: String(familyOverride || 'other').trim() || 'other',
      label: code ? formatMetricLabel(code) : t('observability.transport.unknownCause'),
      action: '',
    };
  }
  return {
    family: String(familyOverride || details.family || 'other').trim() || 'other',
    label: t(details.labelKey, {
      defaultValue: details.labelDefault || formatMetricLabel(code),
    }),
    action: details.actionKey
      ? t(details.actionKey, {
          defaultValue: details.actionDefault || '',
        })
      : '',
  };
};

const normalizeCountMap = (raw) => {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
    return {};
  }
  return Object.fromEntries(
    Object.entries(raw)
      .map(([key, value]) => [key, Number(value)])
      .filter(([, value]) => Number.isFinite(value) && value > 0)
  );
};

const normalizeTransportExceptionItem = (raw) => {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
    return null;
  }
  const item = {
    source: String(raw.source || '').trim(),
    status: String(raw.status || '').trim(),
    category: String(raw.category || '').trim(),
    tool: String(raw.tool || '').trim(),
    transport: String(raw.transport || '').trim(),
    check_id: String(raw.check_id || '').trim(),
    message: String(raw.message || '').trim(),
    count: Number(raw.count || 0),
  };
  if (!item.status || !item.message) {
    return null;
  }
  return item;
};

const normalizeTransportSignatureItem = (raw) => {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
    return null;
  }
  const item = {
    signature: String(raw.signature || '').trim(),
    status: String(raw.status || '').trim(),
    category: String(raw.category || '').trim(),
    tool: String(raw.tool || '').trim(),
    transport: String(raw.transport || '').trim(),
    check_id: String(raw.check_id || '').trim(),
    message: String(raw.message || '').trim(),
    signal_count: Number(raw.signal_count || 0),
    sources: Array.isArray(raw.sources)
      ? raw.sources.map((source) => String(source || '').trim()).filter(Boolean)
      : [],
  };
  if (!item.status || !item.message) {
    return null;
  }
  return item;
};

const normalizeTransportIncidentItem = (raw) => {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
    return null;
  }
  const item = {
    canonical_cause: String(raw.canonical_cause || '').trim(),
    highest_status: String(raw.highest_status || '').trim(),
    category: String(raw.category || '').trim(),
    tool: String(raw.tool || '').trim(),
    transport: String(raw.transport || '').trim(),
    check_id: String(raw.check_id || '').trim(),
    sample_message: String(raw.sample_message || '').trim(),
    cause_family: String(raw.cause_family || '').trim(),
    signal_count: Number(raw.signal_count || 0),
    sources: Array.isArray(raw.sources)
      ? raw.sources.map((source) => String(source || '').trim()).filter(Boolean)
      : [],
    last_seen_at:
      typeof raw.last_seen_at === 'string' && raw.last_seen_at.trim()
        ? raw.last_seen_at
        : null,
  };
  if (!item.canonical_cause || !item.highest_status || !item.sample_message) {
    return null;
  }
  return item;
};

export const normalizeTransportExceptionBreakdown = (raw) => {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
    return null;
  }
  return {
    total: Number(raw.total || 0),
    status_counts: normalizeCountMap(raw.status_counts),
    source_counts: normalizeCountMap(raw.source_counts),
    category_counts: normalizeCountMap(raw.category_counts),
    tool_counts: normalizeCountMap(raw.tool_counts),
    check_id_counts: normalizeCountMap(raw.check_id_counts),
    last_exception_at:
      typeof raw.last_exception_at === 'string' && raw.last_exception_at.trim()
        ? raw.last_exception_at
        : null,
    signature_breakdown:
      raw.signature_breakdown &&
      typeof raw.signature_breakdown === 'object' &&
      !Array.isArray(raw.signature_breakdown)
        ? {
            total: Number(raw.signature_breakdown.total || 0),
            signature_counts: normalizeCountMap(raw.signature_breakdown.signature_counts),
            items: Array.isArray(raw.signature_breakdown.items)
              ? raw.signature_breakdown.items
                  .map((item) => normalizeTransportSignatureItem(item))
                  .filter(Boolean)
              : [],
          }
        : {
            total: 0,
            signature_counts: {},
            items: [],
          },
    incident_breakdown:
      raw.incident_breakdown &&
      typeof raw.incident_breakdown === 'object' &&
      !Array.isArray(raw.incident_breakdown)
        ? {
            incident_count: Number(raw.incident_breakdown.incident_count || 0),
            canonical_cause_counts: normalizeCountMap(raw.incident_breakdown.canonical_cause_counts),
            items: Array.isArray(raw.incident_breakdown.items)
              ? raw.incident_breakdown.items
                  .map((item) => normalizeTransportIncidentItem(item))
                  .filter(Boolean)
              : [],
          }
        : {
            incident_count: 0,
            canonical_cause_counts: {},
            items: [],
          },
    items: Array.isArray(raw.items)
      ? raw.items
          .map((item) => normalizeTransportExceptionItem(item))
          .filter(Boolean)
      : [],
  };
};

export const buildTransportExceptionBreakdownFallback = ({
  diagnostics,
  lastReportChecks,
  activeTransport,
  updatedAt,
}) => {
  const statusCounts = {};
  const sourceCounts = {};
  const categoryCounts = {};
  const toolCounts = {};
  const checkIdCounts = {};
  const dedupedItems = new Map();
  const signatureItems = new Map();
  const incidentItems = new Map();
  let lastExceptionAt = null;

  const canonicalizeCause = ({ category, tool, checkId, transport, message, fallbackSignature }) => {
    const normalizedMessage = String(message || '').trim().toLowerCase();
    const normalizedCategory = String(category || '').trim().toLowerCase();
    const normalizedContext = [
      normalizedCategory,
      String(tool || '').trim().toLowerCase(),
      String(checkId || '').trim().toLowerCase(),
      String(transport || '').trim().toLowerCase(),
      normalizedMessage,
    ]
      .filter(Boolean)
      .join(' | ');
    if (normalizedMessage.includes('database is locked')) {
      return 'sqlite_database_locked';
    }
    if (normalizedCategory === 'snapshot_load') {
      return 'transport_snapshot_load_failed';
    }
    if (
      ['healthcheck', 'report_check', 'transport'].includes(normalizedCategory) &&
      [
        'token=[redacted]',
        'x-mcp-api-key: [redacted]',
        'authorization: bearer [redacted]',
        'unauthorized',
        'forbidden',
        'invalid api key',
        'api key missing',
        'api key invalid',
        'invalid token',
        'missing token',
        '401',
        '403',
      ].some((marker) => normalizedMessage.includes(marker))
    ) {
      return 'healthcheck_auth_failure';
    }
    if (normalizedCategory === 'connect' && normalizedMessage.includes('connected after fallback')) {
      return 'transport_connect_fallback';
    }
    if (
      ['timed out', 'timeout', 'deadline exceeded', 'read timeout', 'connect timeout'].some(
        (marker) => normalizedContext.includes(marker)
      )
    ) {
      return 'transport_timeout';
    }
    if (
      ['connection refused', 'econnrefused', 'actively refused'].some((marker) =>
        normalizedContext.includes(marker)
      )
    ) {
      return 'transport_connection_refused';
    }
    if (
      ['network is unreachable', 'no route to host', 'host is unreachable', 'enetunreach', 'ehostunreach'].some(
        (marker) => normalizedContext.includes(marker)
      )
    ) {
      return 'transport_network_unreachable';
    }
    if (
      ['connection reset', 'connection reset by peer', 'econnreset', 'socket hang up', 'broken pipe', 'connection aborted'].some(
        (marker) => normalizedContext.includes(marker)
      )
    ) {
      return 'transport_connection_reset';
    }
    if (
      [
        'temporary failure in name resolution',
        'name or service not known',
        'nodename nor servname provided',
        'getaddrinfo',
        'enotfound',
        'eai_again',
        'dns',
      ].some((marker) => normalizedContext.includes(marker))
    ) {
      return 'transport_dns_failure';
    }
    if (
      [
        'certificate verify failed',
        'certificate has expired',
        'self signed certificate',
        'ssl:',
        'tls',
        'handshake',
      ].some((marker) => normalizedContext.includes(marker))
    ) {
      return 'transport_tls_failure';
    }
    if (
      ['429', 'rate limit', 'rate-limit', 'too many requests'].some((marker) =>
        normalizedContext.includes(marker)
      )
    ) {
      return 'transport_rate_limited';
    }
    if (
      ['413', 'payload too large', 'request entity too large', 'content too large'].some((marker) =>
        normalizedContext.includes(marker))
    ) {
      return 'transport_payload_too_large';
    }
    if (
      [
        '500',
        '502',
        '503',
        '504',
        'bad gateway',
        'service unavailable',
        'gateway timeout',
        'internal server error',
        'upstream connect error',
        'upstream request failed',
      ].some((marker) => normalizedContext.includes(marker))
    ) {
      return 'transport_upstream_unavailable';
    }
    if (
      [
        'protocol error',
        'bad status line',
        'invalid content-type',
        'unexpected content-type',
        'invalid json',
        'unexpected token <',
        'malformed response',
      ].some((marker) => normalizedContext.includes(marker))
    ) {
      return 'transport_protocol_error';
    }
    return fallbackSignature;
  };

  const resolveReportCheckSignal = (check) => {
    const normalizedCheckId = String(check?.id || '').trim().toLowerCase();
    const normalizedMessage = String(check?.message || '').trim();
    if (
      normalizedCheckId !== 'transport-health' ||
      normalizedMessage.toLowerCase() !== 'transport health check failed.'
    ) {
      return {
        category: 'report_check',
        tool: '',
        transport: '',
        message: normalizedMessage,
      };
    }

    const correlatedHealthcheckMessage = String(diagnostics?.last_health_check_error || '').trim();
    if (correlatedHealthcheckMessage) {
      return {
        category: 'healthcheck',
        tool: String(diagnostics?.healthcheck_tool || '').trim(),
        transport: String(activeTransport || '').trim(),
        message: correlatedHealthcheckMessage,
      };
    }

    const correlatedLastError = String(diagnostics?.last_error || '').trim();
    if (correlatedLastError) {
      return {
        category: 'transport',
        tool: '',
        transport: String(activeTransport || '').trim(),
        message: correlatedLastError,
      };
    }

    return {
      category: 'report_check',
      tool: '',
      transport: '',
      message: normalizedMessage,
    };
  };

  const bump = (target, key) => {
    if (!key) return;
    target[key] = Number(target[key] || 0) + 1;
  };

  const record = ({
    source,
    status,
    category,
    tool,
    transport,
    checkId,
    message,
    at,
  }) => {
    const normalizedStatus = String(status || '').trim().toLowerCase();
    const normalizedMessage = String(message || '').trim();
    if (!['warn', 'fail'].includes(normalizedStatus) || !normalizedMessage) {
      return;
    }
    const normalizedSource = String(source || '').trim().toLowerCase();
    const normalizedCategory = String(category || '').trim().toLowerCase();
    const normalizedTool = String(tool || '').trim().toLowerCase();
    const normalizedTransport = String(transport || '').trim().toLowerCase();
    const normalizedCheckId = String(checkId || '').trim().toLowerCase();
    const signatureSubject = normalizedTool || normalizedCheckId || normalizedTransport;
    const signature = [normalizedCategory, signatureSubject, normalizedMessage]
      .filter(Boolean)
      .join(' | ');
    const canonicalCause = canonicalizeCause({
      category: normalizedCategory,
      tool: normalizedTool,
      checkId: normalizedCheckId,
      transport: normalizedTransport,
      message: normalizedMessage,
      fallbackSignature: signature,
    });

    bump(statusCounts, normalizedStatus);
    bump(sourceCounts, normalizedSource);
    bump(categoryCounts, normalizedCategory);
    bump(toolCounts, normalizedTool);
    bump(checkIdCounts, normalizedCheckId);
    if (typeof at === 'string' && at && (!lastExceptionAt || at > lastExceptionAt)) {
      lastExceptionAt = at;
    }

    const signatureEntry = signatureItems.get(signature);
    if (signatureEntry) {
      const statusRank = { fail: 3, warn: 2, pass: 1 };
      if ((statusRank[normalizedStatus] || 0) > (statusRank[signatureEntry.status] || 0)) {
        signatureEntry.status = normalizedStatus;
      }
      signatureEntry.signal_count += 1;
      if (!signatureEntry.sources.includes(normalizedSource)) {
        signatureEntry.sources.push(normalizedSource);
      }
      const effectiveSubject =
        signatureEntry.tool || signatureEntry.check_id || signatureEntry.transport;
      signatureEntry.signature = [
        signatureEntry.status,
        signatureEntry.category,
        effectiveSubject,
        signatureEntry.message,
      ]
        .filter(Boolean)
        .join(' | ');
    } else {
      signatureItems.set(signature, {
        signature: [normalizedStatus, normalizedCategory, signatureSubject, normalizedMessage]
          .filter(Boolean)
          .join(' | '),
        status: normalizedStatus,
        category: normalizedCategory,
        tool: normalizedTool,
        transport: normalizedTransport,
        check_id: normalizedCheckId,
        message: normalizedMessage,
        signal_count: 1,
        sources: [normalizedSource],
      });
    }

    const incidentEntry = incidentItems.get(canonicalCause);
    if (incidentEntry) {
      const statusRank = { fail: 3, warn: 2, pass: 1 };
      if ((statusRank[normalizedStatus] || 0) > (statusRank[incidentEntry.highest_status] || 0)) {
        incidentEntry.highest_status = normalizedStatus;
      }
      incidentEntry.signal_count += 1;
      if (!incidentEntry.sources.includes(normalizedSource)) {
        incidentEntry.sources.push(normalizedSource);
      }
      if (typeof at === 'string' && at && (!incidentEntry.last_seen_at || at > incidentEntry.last_seen_at)) {
        incidentEntry.last_seen_at = at;
      }
      if (!incidentEntry.tool && normalizedTool) incidentEntry.tool = normalizedTool;
      if (!incidentEntry.transport && normalizedTransport) incidentEntry.transport = normalizedTransport;
      if (!incidentEntry.check_id && normalizedCheckId) incidentEntry.check_id = normalizedCheckId;
    } else {
      incidentItems.set(canonicalCause, {
        canonical_cause: canonicalCause,
        cause_family: (TRANSPORT_CAUSE_DETAILS[canonicalCause] || {}).family || '',
        highest_status: normalizedStatus,
        category: normalizedCategory,
        tool: normalizedTool,
        transport: normalizedTransport,
        check_id: normalizedCheckId,
        sample_message: normalizedMessage,
        signal_count: 1,
        sources: [normalizedSource],
        last_seen_at: typeof at === 'string' ? at : null,
      });
    }

    const dedupeKey = [
      normalizedSource,
      normalizedStatus,
      normalizedCategory,
      normalizedTool,
      normalizedTransport,
      normalizedMessage,
    ].join('::');
    const existing = dedupedItems.get(dedupeKey);
    if (existing) {
      existing.count += 1;
      return;
    }
    dedupedItems.set(dedupeKey, {
      source: normalizedSource,
      status: normalizedStatus,
      category: normalizedCategory,
      tool: normalizedTool,
      transport: normalizedTransport,
      check_id: normalizedCheckId,
      message: normalizedMessage,
      count: 1,
    });
  };

  const recentEvents = Array.isArray(diagnostics?.recent_events)
    ? diagnostics.recent_events
    : [];
  recentEvents.forEach((event) => {
    record({
      source: 'recent_events',
      status: event?.status,
      category: event?.category,
      tool: event?.tool,
      transport: event?.transport,
      message: event?.message,
      at: event?.at,
    });
  });

  const checks = Array.isArray(lastReportChecks) ? lastReportChecks : [];
  checks.forEach((check) => {
    const signal = resolveReportCheckSignal(check);
    record({
      source: 'last_report_checks',
      status: check?.status,
      category: signal.category,
      tool: signal.tool,
      transport: signal.transport,
      checkId: check?.id,
      message: signal.message,
      at: updatedAt,
    });
  });

  if (diagnostics?.last_error) {
    record({
      source: 'last_error',
      status: 'fail',
      category: 'transport',
      transport: activeTransport,
      message: diagnostics.last_error,
      at: updatedAt,
    });
  }
  if (diagnostics?.last_health_check_error) {
    record({
      source: 'last_health_check_error',
      status: 'warn',
      category: 'healthcheck',
      tool: diagnostics.healthcheck_tool,
      transport: activeTransport,
      message: diagnostics.last_health_check_error,
      at: updatedAt,
    });
  }

  return {
    total: Object.values(statusCounts).reduce((sum, value) => sum + Number(value || 0), 0),
    status_counts: statusCounts,
    source_counts: sourceCounts,
    category_counts: categoryCounts,
    tool_counts: toolCounts,
    check_id_counts: checkIdCounts,
    last_exception_at: lastExceptionAt,
    signature_breakdown: {
      total: signatureItems.size,
      signature_counts: Object.fromEntries(
        Array.from(signatureItems.values()).map((item) => [item.signature, item.signal_count])
      ),
      items: Array.from(signatureItems.values()).sort((left, right) => {
        if (right.signal_count !== left.signal_count) return right.signal_count - left.signal_count;
        return String(left.signature || '').localeCompare(String(right.signature || ''));
      }),
    },
    incident_breakdown: {
      incident_count: incidentItems.size,
      canonical_cause_counts: Object.fromEntries(
        Array.from(incidentItems.values()).map((item) => [
          item.canonical_cause,
          item.signal_count,
        ])
      ),
      items: Array.from(incidentItems.values()).sort((left, right) => {
        if (right.signal_count !== left.signal_count) return right.signal_count - left.signal_count;
        return String(left.canonical_cause || '').localeCompare(String(right.canonical_cause || ''));
      }),
    },
    items: Array.from(dedupedItems.values()).sort((left, right) => {
      if (right.count !== left.count) return right.count - left.count;
      return String(left.message || '').localeCompare(String(right.message || ''));
    }),
  };
};

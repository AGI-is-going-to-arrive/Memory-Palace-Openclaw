import { describe, expect, it } from 'vitest';
import {
  buildTransportExceptionBreakdownFallback,
  getTransportCauseDetails,
  normalizeTransportExceptionBreakdown,
} from './transportDiagnostics';

describe('transportDiagnostics helpers', () => {
  it('normalizes backend payloads and drops empty entries', () => {
    const payload = normalizeTransportExceptionBreakdown({
      total: '2',
      status_counts: { fail: 2, warn: 0 },
      source_counts: { recent_events: 1, last_error: 1 },
      category_counts: { transport: 2 },
      tool_counts: { index_status: 0 },
      check_id_counts: { 'transport-health': 1 },
      last_exception_at: '2026-01-01T00:00:00Z',
      signature_breakdown: {
        total: '1',
        signature_counts: { 'transport timeout': 2 },
        items: [
          {
            signature: 'transport timeout',
            status: 'fail',
            category: 'transport',
            message: 'connect timeout',
            signal_count: '2',
            sources: ['recent_events'],
          },
          {
            signature: '',
            status: '',
            message: '',
          },
        ],
      },
      incident_breakdown: {
        incident_count: '1',
        canonical_cause_counts: { transport_timeout: 2 },
        items: [
          {
            canonical_cause: 'transport_timeout',
            highest_status: 'fail',
            category: 'transport',
            sample_message: 'connect timeout',
            cause_family: 'latency',
            signal_count: '2',
            sources: ['recent_events'],
            last_seen_at: '2026-01-01T00:00:00Z',
          },
        ],
      },
      items: [
        {
          source: 'recent_events',
          status: 'fail',
          category: 'transport',
          message: 'connect timeout',
          count: '2',
        },
        {
          source: '',
          status: '',
          message: '',
        },
      ],
    });

    expect(payload.total).toBe(2);
    expect(payload.status_counts).toEqual({ fail: 2 });
    expect(payload.source_counts).toEqual({ recent_events: 1, last_error: 1 });
    expect(payload.category_counts).toEqual({ transport: 2 });
    expect(payload.tool_counts).toEqual({});
    expect(payload.check_id_counts).toEqual({ 'transport-health': 1 });
    expect(payload.last_exception_at).toBe('2026-01-01T00:00:00Z');
    expect(payload.signature_breakdown).toMatchObject({
      total: 1,
      signature_counts: { 'transport timeout': 2 },
    });
    expect(payload.signature_breakdown.items).toEqual([
      expect.objectContaining({
        signature: 'transport timeout',
        status: 'fail',
        message: 'connect timeout',
        signal_count: 2,
      }),
    ]);
    expect(payload.incident_breakdown).toMatchObject({
      incident_count: 1,
      canonical_cause_counts: { transport_timeout: 2 },
    });
    expect(payload.incident_breakdown.items).toEqual([
      expect.objectContaining({
        canonical_cause: 'transport_timeout',
        cause_family: 'latency',
        signal_count: 2,
      }),
    ]);
    expect(payload.items).toEqual([
      expect.objectContaining({
        source: 'recent_events',
        status: 'fail',
        message: 'connect timeout',
        count: 2,
      }),
    ]);
  });

  it('derives canonical causes from fallback transport signals', () => {
    const fallback = buildTransportExceptionBreakdownFallback({
      diagnostics: {
        last_health_check_error: '401 Unauthorized token=[REDACTED]',
        healthcheck_tool: 'index_status',
        last_error: 'socket hang up while streaming tool results',
        recent_events: [
          {
            at: '2026-01-01T00:00:00Z',
            category: 'connect',
            status: 'fail',
            transport: 'sse',
            message: 'connect timeout after 1000ms',
          },
          {
            at: '2026-01-01T00:01:00Z',
            category: 'transport',
            status: 'fail',
            transport: 'sse',
            message: 'HTTP 413 Payload Too Large',
          },
        ],
      },
      lastReportChecks: [
        {
          id: 'transport-health',
          status: 'fail',
          message: 'Transport health check failed.',
        },
      ],
      activeTransport: 'sse',
      updatedAt: '2026-01-01T00:02:00Z',
    });

    expect(fallback.status_counts).toEqual({ fail: 4, warn: 1 });
    expect(fallback.incident_breakdown.canonical_cause_counts).toMatchObject({
      transport_timeout: 1,
      transport_payload_too_large: 1,
      transport_connection_reset: 1,
      healthcheck_auth_failure: 2,
    });
  });

  it('prefers backend cause_family metadata when present', () => {
    const details = getTransportCauseDetails(
      'backend_custom_cause',
      (key, values = {}) => values.defaultValue || key,
      'healthcheck'
    );

    expect(details.family).toBe('healthcheck');
    expect(details.label).toBe('backend custom cause');
    expect(details.action).toBe('');
  });
});

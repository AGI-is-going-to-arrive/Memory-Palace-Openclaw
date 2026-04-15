import React from 'react';
import clsx from 'clsx';
import {
  AlertTriangle,
  RefreshCw,
  RotateCcw,
} from 'lucide-react';

import GlassCard from '../../../components/GlassCard';
import { useSetup, getBooleanTone } from '../useSetupContext';
import { localizeSetupText } from '../setupI18n';
import { StatusPill, SummaryItem, ProviderStatusPill } from './shared';

function CheckStatusPill({ status, label }) {
  return (
    <span
      className={clsx(
        'inline-flex items-center rounded-full px-2.5 py-1 text-[11px] font-semibold uppercase tracking-[0.14em]',
        status === 'pass' && 'bg-[rgba(212,175,55,0.14)] text-[color:var(--palace-ink)]',
        status === 'warn' && 'bg-[rgba(184,150,46,0.14)] text-[color:var(--palace-ink)]',
        status === 'fail' && 'bg-[rgba(143,106,69,0.14)] text-[color:var(--palace-ink)]',
        status === 'unknown' && 'bg-white/70 text-[color:var(--palace-muted)]'
      )}
    >
      {label}
    </span>
  );
}

export default function PreflightChecks() {
  const {
    t,
    form,
    setup,
    statusLoading,
    localizedStatusSummary,
    localizedSetupWarnings,
    preflightChecks,
    preflightSummary,
    providerProbe,
    providerProbeStatus,
    providerProbeSummaryMessage,
    providerProbeItems,
    providerNextSteps,
    probeState,
    advancedFieldsVisible,
    restartState,
    restartSupported,
    effectiveRestartRequired,
    submitState,
    localizedRestartResultMessage,
    handleProviderProbe,
    handleRestart,
  } = useSetup();

  return (
    <GlassCard className="order-2 p-5 sm:p-6 xl:order-2 xl:sticky xl:top-4 xl:self-start">
      <div className="flex items-start justify-between gap-4">
        <div>
          <div className="text-lg font-semibold text-[color:var(--palace-ink)]">
            {t('setup.summary.title')}
          </div>
          <div className="mt-1 text-sm text-[color:var(--palace-muted)]">
            {localizedStatusSummary || t('setup.messages.statusUnavailable')}
          </div>
        </div>
        {statusLoading ? (
          <div className="text-sm text-[color:var(--palace-muted)]">
            {t('setup.loadingStatus')}
          </div>
        ) : null}
      </div>

      <div className="mt-5 grid gap-3 sm:grid-cols-2">
        <SummaryItem
          label={t('setup.fields.mode')}
          value={setup.mode || t('common.states.notAvailable')}
          tone="neutral"
        />
        <SummaryItem
          label={t('setup.fields.transport')}
          value={setup.transport || t('common.states.notAvailable')}
          tone="neutral"
        />
        <SummaryItem
          label={t('setup.fields.profile')}
          value={setup.effectiveProfile || setup.requestedProfile || t('common.states.notAvailable')}
          tone="neutral"
        />
        <SummaryItem
          label={t('setup.fields.restartRequired')}
          value={setup.restartRequired ? t('setup.values.yes') : t('setup.values.no')}
          tone={setup.restartRequired ? 'warn' : 'good'}
        />
        <SummaryItem
          label={t('setup.fields.mcpApiKeyConfigured')}
          value={setup.mcpApiKeyConfigured ? t('setup.values.configured') : t('setup.values.missing')}
          tone={getBooleanTone(setup.mcpApiKeyConfigured)}
        />
        <SummaryItem
          label={t('setup.fields.embeddingConfigured')}
          value={setup.embeddingConfigured ? t('setup.values.configured') : t('setup.values.missing')}
          tone={getBooleanTone(setup.embeddingConfigured)}
        />
        <SummaryItem
          label={t('setup.fields.rerankerConfigured')}
          value={setup.rerankerConfigured ? t('setup.values.configured') : t('setup.values.missing')}
          tone={getBooleanTone(setup.rerankerConfigured)}
        />
        <SummaryItem
          label={t('setup.fields.llmConfigured')}
          value={setup.llmConfigured ? t('setup.values.configured') : t('setup.values.missing')}
          tone={getBooleanTone(setup.llmConfigured)}
        />
      </div>

      <div className="mt-5 flex flex-wrap gap-2">
        <StatusPill active={Boolean(setup.frontendAvailable)}>
          {t('setup.fields.frontendAvailable')}: {setup.frontendAvailable ? t('setup.values.yes') : t('setup.values.no')}
        </StatusPill>
        <StatusPill active={Boolean(setup.envFile)}>
          {t('setup.fields.envFile')}: {setup.envFile || t('common.states.notAvailable')}
        </StatusPill>
        <StatusPill active={Boolean(setup.configPath)}>
          {t('setup.fields.configPath')}: {setup.configPath || t('common.states.notAvailable')}
        </StatusPill>
      </div>

      <div className="mt-5 rounded-2xl border border-[color:var(--palace-line)] bg-white/70 p-4">
        <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
          <div>
            <div className="text-lg font-semibold text-[color:var(--palace-ink)]">
              {t('setup.providerReadiness.title')}
            </div>
            <div className="mt-1 text-sm text-[color:var(--palace-muted)]">
              {t('setup.providerReadiness.subtitle')}
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <ProviderStatusPill
              status={providerProbeStatus}
              label={t(`setup.providerReadiness.status.${providerProbeStatus}`, {
                defaultValue: providerProbeStatus,
              })}
            />
            <button
              type="button"
              onClick={() => void handleProviderProbe()}
              disabled={probeState.loading || !advancedFieldsVisible}
              className="palace-btn-ghost rounded-full border border-[color:var(--palace-line)] bg-white/70 px-4 disabled:cursor-not-allowed disabled:opacity-60"
            >
              <RefreshCw size={15} className={clsx(probeState.loading && 'animate-spin')} />
              {probeState.loading
                ? t('setup.actions.reprobingProviders')
                : t('setup.actions.reprobeProviders')}
            </button>
          </div>
        </div>

        <div className="mt-4 grid gap-3 sm:grid-cols-2">
          <SummaryItem
            label={t('setup.providerReadiness.metrics.requestedProfile')}
            value={String(providerProbe?.requestedProfile || form.profile || t('common.states.notAvailable')).toUpperCase()}
          />
          <SummaryItem
            label={t('setup.providerReadiness.metrics.effectiveProfile')}
            value={String(providerProbe?.effectiveProfile || setup.effectiveProfile || form.profile || t('common.states.notAvailable')).toUpperCase()}
            tone={providerProbe?.fallbackApplied ? 'warn' : 'neutral'}
          />
          <SummaryItem
            label={t('setup.providerReadiness.metrics.missingFields')}
            value={String(Array.isArray(providerProbe?.missingFields) ? providerProbe.missingFields.length : 0)}
            tone={Array.isArray(providerProbe?.missingFields) && providerProbe.missingFields.length > 0 ? 'warn' : 'good'}
          />
          <SummaryItem
            label={t('setup.providerReadiness.metrics.checkedAt')}
            value={
              providerProbe?.checkedAt
                ? localizeSetupText(providerProbe.checkedAt, t) || providerProbe.checkedAt
                : t('common.states.notAvailable')
            }
          />
        </div>

        <div className="mt-4 text-sm text-[color:var(--palace-muted)]">
          {providerProbeSummaryMessage || t('setup.providerReadiness.unavailable')}
        </div>

        {!advancedFieldsVisible ? (
          <div className="mt-4 text-sm text-[color:var(--palace-muted)]">
            {t('setup.providerReadiness.notRequiredHint')}
          </div>
        ) : null}

        {probeState.error ? (
          <div className="mt-4 rounded-xl border border-[rgba(143,106,69,0.28)] bg-[rgba(248,238,226,0.9)] px-4 py-3 text-sm text-[color:var(--palace-ink)]">
            {probeState.error}
          </div>
        ) : null}

        {Array.isArray(providerProbe?.missingFields) && providerProbe.missingFields.length > 0 ? (
          <div className="mt-4">
            <div className="text-xs font-semibold uppercase tracking-[0.14em] text-[color:var(--palace-muted)]">
              {t('setup.providerReadiness.missingFieldsTitle')}
            </div>
            <div className="mt-2 flex flex-wrap gap-2">
              {providerProbe.missingFields.map((field) => (
                <span
                  key={field}
                  className="rounded-full border border-[rgba(184,150,46,0.24)] bg-[rgba(249,241,228,0.9)] px-3 py-1 text-xs font-medium text-[color:var(--palace-ink)]"
                >
                  {field}
                </span>
              ))}
            </div>
          </div>
        ) : null}

        {providerNextSteps.length > 0 ? (
          <div className="mt-4 rounded-xl border border-[rgba(212,175,55,0.18)] bg-[rgba(251,245,236,0.82)] p-4">
            <div className="text-xs font-semibold uppercase tracking-[0.14em] text-[color:var(--palace-muted)]">
              {t('setup.providerReadiness.nextStepsTitle')}
            </div>
            <ul className="m-0 mt-2 list-disc space-y-2 pl-5 text-sm text-[color:var(--palace-muted)]">
              {providerNextSteps.map((step) => (
                <li key={step}>{step}</li>
              ))}
            </ul>
          </div>
        ) : null}

        <div className="mt-4 grid gap-3 lg:grid-cols-2">
          {providerProbeItems.map((item) => (
            <div
              key={item.component}
              className={clsx(
                'rounded-xl border px-4 py-3',
                item.status === 'pass'
                  ? 'border-[rgba(212,175,55,0.22)] bg-[rgba(251,245,236,0.82)]'
                  : 'border-[rgba(184,150,46,0.24)] bg-[rgba(249,241,228,0.9)]'
              )}
            >
              <div className="flex items-center justify-between gap-2">
                <div className="text-sm font-semibold text-[color:var(--palace-ink)]">
                  {item.label}
                </div>
                <ProviderStatusPill
                  status={item.status}
                  label={t(`setup.providerReadiness.status.${item.status}`, {
                    defaultValue: item.status,
                  })}
                />
              </div>
              <div className="mt-2 text-sm text-[color:var(--palace-muted)]">{item.detail}</div>
              <div className="mt-3 space-y-1 text-xs text-[color:var(--palace-muted)]">
                {item.baseUrl ? <div>{t('setup.providerReadiness.baseUrl')}: {item.baseUrl}</div> : null}
                {item.model ? <div>{t('setup.providerReadiness.model')}: {item.model}</div> : null}
                {item.detectedDim ? <div>{t('setup.providerReadiness.detectedDim', { value: item.detectedDim })}</div> : null}
              </div>
              {item.missingFields.length > 0 ? (
                <div className="mt-3 flex flex-wrap gap-2">
                  {item.missingFields.map((field) => (
                    <span
                      key={field}
                      className="rounded-full border border-[rgba(184,150,46,0.24)] bg-white/70 px-2.5 py-1 text-[11px] font-medium text-[color:var(--palace-ink)]"
                    >
                      {field}
                    </span>
                  ))}
                </div>
              ) : null}
            </div>
          ))}
        </div>
      </div>

      <div className="mt-5 rounded-2xl border border-[color:var(--palace-line)] bg-white/70 p-4">
        <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
          <div>
            <div className="text-sm font-semibold text-[color:var(--palace-ink)]">
              {t('setup.preflight.title')}
            </div>
            <div className="mt-1 text-sm text-[color:var(--palace-muted)]">
              {t('setup.preflight.subtitle')}
            </div>
          </div>
          <StatusPill active={preflightSummary.attention === 0 && preflightSummary.total > 0}>
            {preflightSummary.attention > 0
              ? t('setup.preflight.summary.attention', { count: preflightSummary.attention })
              : t('setup.preflight.summary.ready')}
          </StatusPill>
        </div>

        {preflightSummary.total > 0 ? (
          <>
            <div className="mt-4 grid gap-3 sm:grid-cols-3">
              <SummaryItem
                label={t('setup.preflight.metrics.total')}
                value={String(preflightSummary.total)}
                tone="neutral"
              />
              <SummaryItem
                label={t('setup.preflight.metrics.passing')}
                value={String(preflightSummary.passing)}
                tone={preflightSummary.passing === preflightSummary.total ? 'good' : 'neutral'}
              />
              <SummaryItem
                label={t('setup.preflight.metrics.attention')}
                value={String(preflightSummary.attention)}
                tone={preflightSummary.attention > 0 ? 'warn' : 'good'}
              />
            </div>

            <div className="mt-4 space-y-3">
              {preflightChecks.map((check) => (
                <div
                  key={check.id}
                  className={clsx(
                    'rounded-xl border px-4 py-3',
                    check.status === 'pass'
                      ? 'border-[rgba(212,175,55,0.22)] bg-[rgba(251,245,236,0.82)]'
                      : 'border-[rgba(184,150,46,0.24)] bg-[rgba(249,241,228,0.9)]'
                  )}
                >
                  <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
                    <div className="text-sm font-semibold text-[color:var(--palace-ink)]">
                      {check.label}
                    </div>
                    <CheckStatusPill
                      status={check.status}
                      label={t(`setup.preflight.status.${check.status}`, {
                        defaultValue: check.status,
                      })}
                    />
                  </div>
                  <div className="mt-2 text-sm text-[color:var(--palace-muted)]">{check.message}</div>
                  {check.detailsText ? (
                    <div className="mt-2 text-xs text-[color:var(--palace-muted)]">
                      <span className="font-semibold text-[color:var(--palace-ink)]">
                        {t('setup.preflight.labels.details')}:
                      </span>{' '}
                      {check.detailsText}
                    </div>
                  ) : null}
                  {check.action ? (
                    <div className="mt-2 text-xs text-[color:var(--palace-muted)]">
                      <span className="font-semibold text-[color:var(--palace-ink)]">
                        {t('setup.preflight.labels.action')}:
                      </span>{' '}
                      {check.action}
                    </div>
                  ) : null}
                </div>
              ))}
            </div>
          </>
        ) : (
          <div className="mt-4 text-sm text-[color:var(--palace-muted)]">
            {t('setup.preflight.unavailable')}
          </div>
        )}
      </div>

      {localizedSetupWarnings.length > 0 ? (
        <div className="mt-5 rounded-2xl border border-[rgba(184,150,46,0.24)] bg-[rgba(249,241,228,0.9)] p-4">
          <div className="mb-2 flex items-center gap-2 text-sm font-semibold text-[color:var(--palace-ink)]">
            <AlertTriangle size={16} className="text-[color:var(--palace-accent-2)]" />
            {t('setup.warnings.title')}
          </div>
          <ul className="m-0 list-disc space-y-2 pl-5 text-sm text-[color:var(--palace-muted)]">
            {localizedSetupWarnings.map((warning) => (
              <li key={warning}>{warning}</li>
            ))}
          </ul>
        </div>
      ) : null}

      {effectiveRestartRequired && !submitState.result ? (
        <div className="mt-5 flex flex-col gap-3 rounded-2xl border border-[rgba(184,150,46,0.24)] bg-[rgba(249,241,228,0.9)] p-4">
          <div className="text-sm text-[color:var(--palace-muted)]">
            {restartSupported
              ? t('setup.messages.restartHint')
              : t('setup.messages.restartUnsupported')}
          </div>
          <div className="flex flex-wrap items-center gap-3">
            <button
              type="button"
              onClick={() => void handleRestart()}
              disabled={restartState.loading || !restartSupported}
              className="palace-btn-primary justify-center rounded-full px-5 disabled:cursor-not-allowed disabled:opacity-60"
            >
              <RotateCcw size={16} className={clsx(restartState.loading && 'animate-spin')} />
              {restartState.loading
                ? t('setup.actions.restartingBackend')
                : t('setup.actions.restartBackend')}
            </button>
            {localizedRestartResultMessage ? (
              <span className="text-sm text-[color:var(--palace-muted)]">
                {localizedRestartResultMessage}
              </span>
            ) : null}
          </div>
        </div>
      ) : null}
    </GlassCard>
  );
}

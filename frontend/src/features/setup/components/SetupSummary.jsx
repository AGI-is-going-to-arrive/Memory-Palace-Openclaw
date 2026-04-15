import React from 'react';
import clsx from 'clsx';
import {
  AlertTriangle,
  CheckCircle2,
  Database,
  RotateCcw,
  Settings2,
} from 'lucide-react';

import GlassCard from '../../../components/GlassCard';
import { useSetup } from '../useSetupContext';
import { reindexGateString, reindexGateReasonLabel } from '../setupI18n';
import { SummaryItem, ProviderStatusPill } from './shared';

const CARD_CLASS =
  'rounded-2xl border border-[color:var(--palace-line)] bg-[rgba(255,250,244,0.9)] p-5 shadow-[var(--palace-shadow-sm)] backdrop-blur-sm';

export default function SetupSummary() {
  const {
    t,
    submitState,
    restartState,
    reindexState,
    reindexGate,
    reindexGateRequired,
    localizedSubmitResult,
    localizedRestartResultMessage,
    localizedValidation,
    effectiveRestartRequired,
    restartSupported,
    i18nLang,
    handleSubmit,
    handleSubmitAndValidate,
    handleRestart,
    handleReindex,
  } = useSetup();

  return (
    <>
      {/* Apply buttons card */}
      <GlassCard className="p-5 sm:p-6">
        <div className="flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between">
          <div>
            <div className="text-sm font-semibold text-[color:var(--palace-ink)]">
              {t('setup.actions.apply')}
            </div>
            <div className="mt-1 text-sm text-[color:var(--palace-muted)]">
              {t('setup.messages.applyHint')}
            </div>
          </div>
          <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
            <button
              type="button"
              onClick={() => void handleSubmitAndValidate()}
              disabled={submitState.loading}
              className="palace-btn-ghost justify-center rounded-full px-5 disabled:cursor-not-allowed disabled:opacity-60"
            >
              <CheckCircle2 size={16} className={clsx(submitState.loading && submitState.action === 'validate' && 'animate-spin')} />
              {submitState.loading && submitState.action === 'validate'
                ? t('setup.actions.applyingAndValidating')
                : t('setup.actions.applyAndValidate')}
            </button>
            <button
              type="submit"
              disabled={submitState.loading}
              className="palace-btn-primary justify-center rounded-full px-5"
            >
              <Settings2 size={16} className={clsx(submitState.loading && 'animate-spin')} />
              {submitState.loading && submitState.action !== 'validate'
                ? t('setup.actions.applying')
                : t('common.actions.apply')}
            </button>
          </div>
        </div>
      </GlassCard>
    </>
  );
}

export function SetupResultCards() {
  const {
    t,
    submitState,
    restartState,
    reindexState,
    reindexGate,
    reindexGateRequired,
    localizedSubmitResult,
    localizedRestartResultMessage,
    localizedValidation,
    effectiveRestartRequired,
    restartSupported,
    i18nLang,
    handleRestart,
    handleReindex,
  } = useSetup();

  return (
    <>
      {submitState.error ? (
        <GlassCard className="border-[rgba(143,106,69,0.28)] bg-[rgba(248,238,226,0.9)] p-4">
          <div className="flex items-start gap-3 text-sm text-[color:var(--palace-ink)]">
            <AlertTriangle className="mt-0.5 shrink-0 text-[color:var(--palace-accent-2)]" size={18} />
            <div>
              <div className="font-semibold">{t('setup.messages.applyFailed')}</div>
              <div className="mt-1 text-[color:var(--palace-muted)]">{submitState.error}</div>
            </div>
          </div>
        </GlassCard>
      ) : null}

      {restartState.error ? (
        <GlassCard className="border-[rgba(143,106,69,0.28)] bg-[rgba(248,238,226,0.9)] p-4">
          <div className="flex items-start gap-3 text-sm text-[color:var(--palace-ink)]">
            <AlertTriangle className="mt-0.5 shrink-0 text-[color:var(--palace-accent-2)]" size={18} />
            <div>
              <div className="font-semibold">{t('setup.messages.restartFailed')}</div>
              <div className="mt-1 text-[color:var(--palace-muted)]">{restartState.error}</div>
            </div>
          </div>
        </GlassCard>
      ) : null}

      {localizedSubmitResult ? (
        <GlassCard className="p-6">
          <div className="flex items-start gap-3">
            <CheckCircle2 size={20} className="mt-0.5 text-[color:var(--palace-accent-2)]" />
            <div className="min-w-0 flex-1">
              <div className="text-lg font-semibold text-[color:var(--palace-ink)]">
                {t('setup.result.title')}
              </div>
              <div className="mt-1 text-sm text-[color:var(--palace-muted)]">
                {localizedSubmitResult.summary}
              </div>

              <div className="mt-5 grid gap-3 md:grid-cols-3">
                <SummaryItem
                  label={t('setup.result.effectiveProfile')}
                  value={localizedSubmitResult.effectiveProfile || t('common.states.notAvailable')}
                />
                <SummaryItem
                  label={t('setup.result.fallbackApplied')}
                  value={localizedSubmitResult.fallbackApplied ? t('setup.values.yes') : t('setup.values.no')}
                  tone={localizedSubmitResult.fallbackApplied ? 'warn' : 'good'}
                />
                <SummaryItem
                  label={t('setup.fields.restartRequired')}
                  value={localizedSubmitResult.restartRequired ? t('setup.values.yes') : t('setup.values.no')}
                  tone={localizedSubmitResult.restartRequired ? 'warn' : 'good'}
                />
              </div>

              <div className="mt-5 grid gap-4 lg:grid-cols-3">
                <div className={CARD_CLASS}>
                  <div className="text-sm font-semibold text-[color:var(--palace-ink)]">
                    {t('setup.result.actions')}
                  </div>
                  <ul className="m-0 mt-3 list-disc space-y-2 pl-5 text-sm text-[color:var(--palace-muted)]">
                    {(localizedSubmitResult.actions || []).map((item) => (
                      <li key={item}>{item}</li>
                    ))}
                  </ul>
                </div>
                <div className={CARD_CLASS}>
                  <div className="text-sm font-semibold text-[color:var(--palace-ink)]">
                    {t('setup.result.nextSteps')}
                  </div>
                  <ul className="m-0 mt-3 list-disc space-y-2 pl-5 text-sm text-[color:var(--palace-muted)]">
                    {(localizedSubmitResult.nextSteps || []).map((item) => (
                      <li key={item}>{item}</li>
                    ))}
                  </ul>
                </div>
                <div className={CARD_CLASS}>
                  <div className="text-sm font-semibold text-[color:var(--palace-ink)]">
                    {t('setup.warnings.title')}
                  </div>
                  <ul className="m-0 mt-3 list-disc space-y-2 pl-5 text-sm text-[color:var(--palace-muted)]">
                    {(localizedSubmitResult.warnings || []).map((item) => (
                      <li key={item}>{item}</li>
                    ))}
                  </ul>
                </div>
              </div>

              {localizedValidation ? (
                <div className="mt-5 rounded-2xl border border-[rgba(212,175,55,0.18)] bg-[rgba(251,245,236,0.82)] p-4">
                  <div className="text-sm font-semibold text-[color:var(--palace-ink)]">
                    {t('setup.validation.title')}
                  </div>
                  <div className="mt-2 space-y-2 text-sm text-[color:var(--palace-muted)]">
                    {localizedValidation.steps.map((step) => (
                      <div key={step.name} className="flex items-start justify-between gap-3 rounded-xl border border-[color:var(--palace-line)] bg-white/70 px-3 py-2">
                        <div>
                          <div className="font-medium text-[color:var(--palace-ink)]">
                            {t(`setup.validation.steps.${step.name}`, { defaultValue: step.name })}
                          </div>
                          {step.summary ? (
                            <div className="mt-1 text-xs text-[color:var(--palace-muted)]">{step.summary}</div>
                          ) : null}
                        </div>
                        <ProviderStatusPill
                          status={step.ok ? 'pass' : 'fail'}
                          label={step.ok ? t('setup.validation.pass') : t('setup.validation.fail')}
                        />
                      </div>
                    ))}
                  </div>
                  {localizedValidation.failed_step ? (
                    <div className="mt-3 text-sm text-[color:var(--palace-accent-2)]">
                      {t('setup.validation.failedStep', { step: localizedValidation.failed_step })}
                    </div>
                  ) : null}
                </div>
              ) : null}

              {reindexGateRequired ? (
                <div className="mt-5 flex flex-col gap-3 rounded-2xl border border-[rgba(217,163,46,0.32)] bg-[rgba(255,248,230,0.92)] p-4">
                  <div className="flex items-start gap-2">
                    <AlertTriangle size={18} className="mt-0.5 shrink-0 text-amber-600" />
                    <div>
                      <div className="text-sm font-semibold text-[color:var(--palace-ink)]">
                        {reindexGateString('title', i18nLang)}
                      </div>
                      <div className="mt-1 text-sm text-[color:var(--palace-muted)]">
                        {reindexGateString('description', i18nLang)}
                      </div>
                    </div>
                  </div>
                  {Array.isArray(reindexGate.reasonKeys) && reindexGate.reasonKeys.length > 0 ? (
                    <div className="ml-7 text-sm text-[color:var(--palace-muted)]">
                      <span className="font-medium">{reindexGateString('reasons', i18nLang)}</span>
                      <ul className="m-0 mt-1 list-disc space-y-1 pl-5">
                        {reindexGate.reasonKeys.map((rk) => (
                          <li key={rk}>{reindexGateReasonLabel(rk, i18nLang)}</li>
                        ))}
                      </ul>
                    </div>
                  ) : null}
                  <div className="ml-7 flex flex-wrap items-center gap-3">
                    <button
                      type="button"
                      onClick={() => void handleReindex()}
                      disabled={reindexState.loading}
                      className="palace-btn-primary justify-center rounded-full px-5 disabled:cursor-not-allowed disabled:opacity-60"
                    >
                      <Database size={16} className={clsx(reindexState.loading && 'animate-spin')} />
                      {reindexState.loading
                        ? reindexGateString('inProgress', i18nLang)
                        : reindexGateString('action', i18nLang)}
                    </button>
                    {reindexState.error ? (
                      <span className="text-sm text-red-600">{reindexState.error}</span>
                    ) : null}
                  </div>
                </div>
              ) : null}

              {reindexState.done && reindexGate?.required ? (
                <div className="mt-5 flex items-center gap-2 rounded-2xl border border-[rgba(52,168,83,0.24)] bg-[rgba(237,250,240,0.9)] p-4 text-sm text-green-700">
                  <CheckCircle2 size={16} />
                  {reindexGateString('complete', i18nLang)}
                </div>
              ) : null}

              {effectiveRestartRequired ? (
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
            </div>
          </div>
        </GlassCard>
      ) : null}
    </>
  );
}

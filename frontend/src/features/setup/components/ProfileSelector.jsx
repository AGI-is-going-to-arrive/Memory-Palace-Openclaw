import React from 'react';
import clsx from 'clsx';
import {
  RefreshCw,
  Settings2,
  Sparkles,
} from 'lucide-react';

import GlassCard from '../../../components/GlassCard';
import { useSetup } from '../useSetupContext';
import { StatusPill, SummaryItem, ProviderStatusPill, Field, SecretField } from './shared';

const CARD_CLASS =
  'rounded-2xl border border-[color:var(--palace-line)] bg-[rgba(255,250,244,0.9)] p-5 shadow-[var(--palace-shadow-sm)] backdrop-blur-sm';
const STRATEGY_CARD_CLASS =
  'rounded-[1.4rem] border p-4 shadow-[0_10px_32px_rgba(179,133,79,0.08)] backdrop-blur-sm';
const INPUT_CLASS =
  'w-full rounded-xl border border-[color:var(--palace-line)] bg-white/90 px-3 py-2 text-sm text-[color:var(--palace-ink)] placeholder:text-[color:var(--palace-muted)] focus:outline-none focus:ring-2 focus:ring-[color:var(--palace-accent)]/35 focus:border-[color:var(--palace-accent)]';
const LABEL_CLASS =
  'mb-2 block text-xs font-medium uppercase tracking-[0.14em] text-[color:var(--palace-muted)]';
const SEGMENT_BASE_CLASS =
  'inline-flex items-center justify-center rounded-full border px-4 py-2 text-sm font-medium transition';

function StrategyCard({ label, title, description, tone = 'default' }) {
  return (
    <div
      className={clsx(
        STRATEGY_CARD_CLASS,
        tone === 'recommended'
          ? 'border-[rgba(184,150,46,0.34)] bg-[linear-gradient(135deg,rgba(251,245,236,0.96),rgba(245,233,211,0.92))]'
          : 'border-[color:var(--palace-line)] bg-[rgba(255,255,255,0.78)]'
      )}
    >
      <div className="text-[11px] font-semibold uppercase tracking-[0.16em] text-[color:var(--palace-muted)]">
        {label}
      </div>
      <div className="mt-2 text-base font-semibold text-[color:var(--palace-ink)]">{title}</div>
      <div className="mt-2 text-sm leading-6 text-[color:var(--palace-muted)]">{description}</div>
    </div>
  );
}

function PresetBadge({ tone = 'default', children }) {
  return (
    <span
      className={clsx(
        'rounded-full border px-2.5 py-0.5 text-[10px] font-semibold uppercase tracking-[0.12em]',
        tone === 'recommended'
          && 'border-[rgba(184,150,46,0.3)] bg-[rgba(212,175,55,0.16)] text-[color:var(--palace-ink)]',
        tone === 'default'
          && 'border-[rgba(212,175,55,0.22)] bg-white/80 text-[color:var(--palace-accent-2)]',
        tone === 'optional'
          && 'border-[color:var(--palace-line)] bg-white/75 text-[color:var(--palace-muted)]'
      )}
    >
      {children}
    </span>
  );
}

function SegmentGroup({ label, options, value, onChange, getLabel }) {
  return (
    <div>
      <div className={LABEL_CLASS}>{label}</div>
      <div className="flex flex-wrap gap-2">
        {options.map((option) => {
          const selected = option === value;
          return (
            <button
              key={option}
              type="button"
              aria-pressed={selected}
              onClick={() => onChange(option)}
              className={clsx(
                SEGMENT_BASE_CLASS,
                selected
                  ? 'border-[rgba(212,175,55,0.45)] bg-[rgba(212,175,55,0.14)] text-[color:var(--palace-ink)] shadow-[0_6px_18px_rgba(212,175,55,0.12)]'
                  : 'border-[color:var(--palace-line)] bg-white/70 text-[color:var(--palace-muted)] hover:border-[rgba(212,175,55,0.32)] hover:text-[color:var(--palace-ink)]'
              )}
            >
              {getLabel(option)}
            </button>
          );
        })}
      </div>
    </div>
  );
}

export default function ProfileSelector() {
  const {
    t,
    form,
    setup,
    statusLoading,
    providerProbeStatus,
    preflightSummary,
    preferredAdvancedProfileActive,
    activePresetId,
    wizardSteps,
    guidedValidationCommand,
    profileOptions,
    modeOptions,
    transportOptions,
    handleOptionChange,
    handlePresetApply,
    handleRefresh,
    handleValueChange,
    handleBooleanChange,
    SETUP_PRESETS,
  } = useSetup();

  return (
    <>
      {/* Hero header card */}
      <GlassCard className="p-5 sm:p-6">
        <div className="grid gap-6 xl:grid-cols-[minmax(0,1.18fr)_minmax(320px,0.82fr)] xl:items-start">
          <div>
            <div className="grid gap-5 xl:grid-cols-[minmax(0,1fr)_minmax(280px,320px)] xl:items-start">
              <div className="max-w-3xl">
                <div className="mb-3 inline-flex items-center gap-2 rounded-full border border-[rgba(212,175,55,0.26)] bg-[rgba(212,175,55,0.12)] px-3 py-1 text-xs font-semibold uppercase tracking-[0.16em] text-[color:var(--palace-muted)]">
                  <Settings2 size={14} />
                  {t('setup.kicker')}
                </div>
                <h1 className="m-0 text-2xl font-semibold text-[color:var(--palace-ink)] sm:text-3xl">
                  {t('setup.title')}
                </h1>
                <p className="mt-3 max-w-2xl text-sm leading-6 text-[color:var(--palace-muted)]">
                  {t('setup.subtitle')}
                </p>

                <div className="mt-4 flex flex-wrap gap-2">
                  <StatusPill active={Boolean(setup.requiresOnboarding)}>
                    {setup.requiresOnboarding
                      ? t('setup.summary.requiresOnboarding')
                      : t('setup.summary.ready')}
                  </StatusPill>
                  <ProviderStatusPill
                    status={providerProbeStatus}
                    label={t(`setup.providerReadiness.status.${providerProbeStatus}`, {
                      defaultValue: providerProbeStatus,
                    })}
                  />
                </div>
              </div>

              <div className="space-y-3">
                <div className="grid gap-3 sm:grid-cols-3 xl:grid-cols-1">
                  <SummaryItem
                    label={t('setup.fields.mode')}
                    value={t(`setup.modeLabels.${form.mode}`, {
                      defaultValue: form.mode || t('common.states.notAvailable'),
                    })}
                  />
                  <SummaryItem
                    label={t('setup.fields.profile')}
                    value={String(setup.effectiveProfile || form.profile || t('common.states.notAvailable')).toUpperCase()}
                    tone={setup.requiresOnboarding ? 'warn' : 'good'}
                  />
                  <SummaryItem
                    label={t('setup.preflight.metrics.attention')}
                    value={String(preflightSummary.attention)}
                    tone={preflightSummary.attention > 0 ? 'warn' : 'good'}
                  />
                </div>

                <div className="flex flex-wrap items-center gap-2 xl:justify-end">
                  <button
                    type="button"
                    onClick={() => void handleRefresh()}
                    disabled={statusLoading}
                    className="palace-btn-ghost rounded-full border border-[color:var(--palace-line)] bg-white/70 px-4"
                  >
                    <RefreshCw size={15} className={clsx(statusLoading && 'animate-spin')} />
                    {t('setup.actions.refreshStatus')}
                  </button>
                </div>
              </div>
            </div>

            <div className="mt-5 rounded-2xl border border-[rgba(212,175,55,0.18)] bg-[linear-gradient(180deg,rgba(255,252,247,0.94),rgba(250,243,232,0.88))] p-4 lg:hidden">
              <div className="text-[11px] font-semibold uppercase tracking-[0.16em] text-[color:var(--palace-muted)]">
                {t('setup.guided.strategyTitle')}
              </div>
              <p className="m-0 mt-2 text-sm leading-6 text-[color:var(--palace-muted)]">
                {t('setup.guided.strategyBody')}
              </p>
              <div className="mt-3 flex flex-wrap gap-2">
                <PresetBadge tone="default">{t('setup.guided.badges.default')}</PresetBadge>
                <PresetBadge tone="recommended">{t('setup.guided.badges.recommended')}</PresetBadge>
              </div>
            </div>

            <div className="mt-6 hidden gap-4 lg:grid lg:grid-cols-[minmax(0,0.9fr)_minmax(0,1.1fr)]">
              <StrategyCard
                label={t('setup.guided.tracks.default.label')}
                title={t('setup.guided.tracks.default.title')}
                description={t('setup.guided.tracks.default.description')}
              />
              <StrategyCard
                tone="recommended"
                label={t('setup.guided.tracks.recommended.label')}
                title={t('setup.guided.tracks.recommended.title')}
                description={t('setup.guided.tracks.recommended.description')}
              />
            </div>
          </div>

          <div className="rounded-[1.6rem] border border-[rgba(212,175,55,0.18)] bg-[linear-gradient(180deg,rgba(255,252,247,0.94),rgba(250,243,232,0.88))] p-5 shadow-[0_16px_42px_rgba(179,133,79,0.08)]">
            <div className="text-[11px] font-semibold uppercase tracking-[0.16em] text-[color:var(--palace-muted)]">
              {t('setup.guided.strategyTitle')}
            </div>
            <div className="mt-3 space-y-3 text-sm leading-6 text-[color:var(--palace-muted)]">
              <p className="m-0">{t('setup.guided.strategyBody')}</p>
              <div className="flex flex-wrap gap-2">
                <PresetBadge tone="default">{t('setup.guided.badges.default')}</PresetBadge>
                <PresetBadge tone="recommended">{t('setup.guided.badges.recommended')}</PresetBadge>
              </div>
              <div className="rounded-2xl border border-[rgba(212,175,55,0.18)] bg-white/70 p-4">
                <div className="text-sm font-semibold text-[color:var(--palace-ink)]">
                  {preferredAdvancedProfileActive
                    ? t('setup.guided.activeState.recommended')
                    : t('setup.guided.activeState.default')}
                </div>
                <div className="mt-1 text-sm text-[color:var(--palace-muted)]">
                  {preferredAdvancedProfileActive
                    ? t('setup.guided.activeState.recommendedBody')
                    : t('setup.guided.activeState.defaultBody')}
                </div>
              </div>
            </div>
          </div>
        </div>
      </GlassCard>

      {/* Guided presets card */}
      <GlassCard className="p-5 sm:p-6">
        <div className="grid gap-6 lg:grid-cols-[minmax(0,1.2fr)_minmax(280px,0.8fr)]">
          <div>
            <div className="text-lg font-semibold text-[color:var(--palace-ink)]">
              {t('setup.guided.title')}
            </div>
            <div className="mt-1 text-sm text-[color:var(--palace-muted)]">
              {t('setup.guided.subtitle')}
            </div>

            <div className="mt-4 grid gap-3 md:grid-cols-2 2xl:grid-cols-3">
              {SETUP_PRESETS.map((preset) => {
                const active = activePresetId === preset.id;
                return (
                  <button
                    key={preset.id}
                    type="button"
                    onClick={() => handlePresetApply(preset)}
                    className={clsx(
                      'rounded-2xl border p-4 text-left transition',
                      active
                        ? 'border-[rgba(212,175,55,0.42)] bg-[rgba(251,245,236,0.92)] shadow-[0_10px_24px_rgba(212,175,55,0.08)]'
                        : 'border-[color:var(--palace-line)] bg-white/75 hover:border-[rgba(212,175,55,0.28)]'
                    )}
                  >
                    <div className="flex items-center justify-between gap-2">
                      <div className="text-sm font-semibold text-[color:var(--palace-ink)]">
                        {t(`setup.guided.presets.${preset.id}.title`)}
                      </div>
                      {preset.badgeKey ? (
                        <PresetBadge tone={preset.badgeTone}>
                          {t(`setup.guided.badges.${preset.badgeKey}`)}
                        </PresetBadge>
                      ) : null}
                    </div>
                    <div className="mt-2 text-sm text-[color:var(--palace-muted)]">
                      {t(`setup.guided.presets.${preset.id}.description`)}
                    </div>
                    <div className="mt-3 flex flex-wrap gap-2 text-[11px] text-[color:var(--palace-muted)]">
                      <span>{t(`setup.modeLabels.${preset.mode}`)}</span>
                      <span>•</span>
                      <span>{t(`setup.profileLabels.${preset.profile}`)}</span>
                      <span>•</span>
                      <span>{t(`setup.transportLabels.${preset.transport}`)}</span>
                    </div>
                    <div className="mt-4 text-xs font-semibold uppercase tracking-[0.14em] text-[color:var(--palace-accent-2)]">
                      {active ? t('setup.guided.currentPath') : t('setup.guided.usePath')}
                    </div>
                  </button>
                );
              })}
            </div>
          </div>

          <div className="rounded-2xl border border-[color:var(--palace-line)] bg-white/75 p-4">
            <div className="text-sm font-semibold text-[color:var(--palace-ink)]">
              {t('setup.guided.stepsTitle')}
            </div>
            <div className="mt-3 space-y-3">
              {wizardSteps.map((step, index) => (
                <div
                  key={step.id}
                  className="flex items-start gap-3"
                  data-testid={`guided-step-${step.id}`}
                  data-state={step.done ? 'done' : 'pending'}
                >
                  <div
                    className={clsx(
                      'flex h-7 w-7 items-center justify-center rounded-full text-xs font-semibold',
                      step.done
                        ? 'bg-[rgba(212,175,55,0.16)] text-[color:var(--palace-ink)]'
                        : 'bg-[rgba(237,226,211,0.72)] text-[color:var(--palace-muted)]'
                    )}
                  >
                    {index + 1}
                  </div>
                  <div>
                    <div className="text-sm font-semibold text-[color:var(--palace-ink)]">
                      {step.title}
                    </div>
                    <div className="mt-1 text-sm text-[color:var(--palace-muted)]">
                      {step.description}
                    </div>
                  </div>
                </div>
              ))}
            </div>

            {guidedValidationCommand ? (
              <div className="mt-4 rounded-xl border border-[rgba(212,175,55,0.18)] bg-[rgba(251,245,236,0.82)] p-3">
                <div className="text-xs font-semibold uppercase tracking-[0.14em] text-[color:var(--palace-muted)]">
                  {t('setup.guided.cliShortcutTitle')}
                </div>
                <code className="mt-2 block whitespace-pre-wrap break-all text-xs text-[color:var(--palace-ink)]">
                  {guidedValidationCommand}
                </code>
              </div>
            ) : null}
          </div>
        </div>
      </GlassCard>

      {/* Configuration form card -- profile/mode/transport selectors + basic fields */}
      <GlassCard className="p-5 sm:p-6">
        <div className="mb-5 flex flex-wrap items-start justify-between gap-3">
          <div>
            <div className="text-lg font-semibold text-[color:var(--palace-ink)]">
              {t('setup.form.title')}
            </div>
            <div className="mt-1 text-sm text-[color:var(--palace-muted)]">
              {t('setup.form.subtitle')}
            </div>
          </div>
          <div className="flex flex-wrap gap-2">
            <PresetBadge tone="default">{t('setup.guided.tracks.default.title')}</PresetBadge>
            <PresetBadge tone="recommended">{t('setup.guided.tracks.recommended.title')}</PresetBadge>
          </div>
        </div>

        <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(280px,0.92fr)]">
          <div className="space-y-6">
            <SegmentGroup
              label={t('setup.fields.profile')}
              options={profileOptions}
              value={form.profile}
              onChange={(nextValue) => handleOptionChange('profile', nextValue)}
              getLabel={(option) => t(`setup.profileLabels.${option}`, { defaultValue: option })}
            />

            <SegmentGroup
              label={t('setup.fields.mode')}
              options={modeOptions}
              value={form.mode}
              onChange={(nextValue) => handleOptionChange('mode', nextValue)}
              getLabel={(option) => t(`setup.modeLabels.${option}`, { defaultValue: option.toUpperCase() })}
            />

            <SegmentGroup
              label={t('setup.fields.transport')}
              options={transportOptions}
              value={form.transport}
              onChange={(nextValue) => handleOptionChange('transport', nextValue)}
              getLabel={(option) => t(`setup.transportLabels.${option}`, { defaultValue: option })}
            />
          </div>

          <div className={CARD_CLASS}>
            <div className="flex items-center gap-3">
              <Sparkles size={18} className="text-[color:var(--palace-accent-2)]" />
              <div>
                <div className="text-sm font-semibold text-[color:var(--palace-ink)]">
                  {t('setup.messages.secretHandlingTitle')}
                </div>
                <div className="mt-1 text-sm text-[color:var(--palace-muted)]">
                  {t('setup.messages.secretHandlingBody')}
                </div>
              </div>
            </div>

            <div className="mt-4 space-y-3 text-sm text-[color:var(--palace-muted)]">
              <label className="flex items-start gap-3">
                <input
                  type="checkbox"
                  name="reconfigure"
                  checked={form.reconfigure}
                  onChange={handleBooleanChange}
                  className="mt-1 h-4 w-4 rounded border-[color:var(--palace-line)] text-[color:var(--palace-accent)] focus:ring-[color:var(--palace-accent)]"
                />
                <span>{t('setup.fields.reconfigure')}</span>
              </label>
              <label className="flex items-start gap-3">
                <input
                  type="checkbox"
                  name="allowInsecureLocal"
                  checked={form.allowInsecureLocal}
                  onChange={handleBooleanChange}
                  className="mt-1 h-4 w-4 rounded border-[color:var(--palace-line)] text-[color:var(--palace-accent)] focus:ring-[color:var(--palace-accent)]"
                />
                <span>{t('setup.fields.allowInsecureLocal')}</span>
              </label>
            </div>
          </div>
        </div>

        <div className="mt-6 grid gap-4 md:grid-cols-2">
          <Field id="setup-database-path" label={t('setup.fields.databasePath')}>
            <input
              id="setup-database-path"
              name="databasePath"
              className={INPUT_CLASS}
              value={form.databasePath}
              onChange={handleValueChange}
            />
          </Field>

          <Field
            id="setup-sse-url"
            label={t('setup.fields.sseUrl')}
            hint={form.transport === 'stdio' ? t('setup.hints.sseUrlOptional') : undefined}
          >
            <input
              id="setup-sse-url"
              name="sseUrl"
              className={INPUT_CLASS}
              value={form.sseUrl}
              onChange={handleValueChange}
            />
          </Field>

          <SecretField
            id="setup-mcp-api-key"
            name="mcpApiKey"
            label={t('setup.fields.mcpApiKey')}
            value={form.mcpApiKey}
            onChange={handleValueChange}
          />
        </div>
      </GlassCard>
    </>
  );
}

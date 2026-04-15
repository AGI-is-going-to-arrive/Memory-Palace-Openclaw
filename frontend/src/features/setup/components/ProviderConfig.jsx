import React from 'react';
import clsx from 'clsx';
import {
  CheckCircle2,
  Database,
  KeyRound,
  ServerCog,
  Sparkles,
  Workflow,
} from 'lucide-react';

import GlassCard from '../../../components/GlassCard';
import { useSetup } from '../useSetupContext';

const CARD_CLASS =
  'rounded-2xl border border-[color:var(--palace-line)] bg-[rgba(255,250,244,0.9)] p-5 shadow-[var(--palace-shadow-sm)] backdrop-blur-sm';
const INPUT_CLASS =
  'w-full rounded-xl border border-[color:var(--palace-line)] bg-white/90 px-3 py-2 text-sm text-[color:var(--palace-ink)] placeholder:text-[color:var(--palace-muted)] focus:outline-none focus:ring-2 focus:ring-[color:var(--palace-accent)]/35 focus:border-[color:var(--palace-accent)]';
const LABEL_CLASS =
  'mb-2 block text-xs font-medium uppercase tracking-[0.14em] text-[color:var(--palace-muted)]';

function Field({ id, label, children, hint }) {
  return (
    <label htmlFor={id} className="block">
      <span className={LABEL_CLASS}>{label}</span>
      {children}
      {hint ? (
        <span className="mt-2 block text-xs text-[color:var(--palace-muted)]">{hint}</span>
      ) : null}
    </label>
  );
}

function SecretField({ id, name, label, value, onChange, hint }) {
  return (
    <Field id={id} label={label} hint={hint}>
      <input
        id={id}
        name={name}
        type="password"
        autoComplete="off"
        className={INPUT_CLASS}
        value={value}
        onChange={onChange}
      />
    </Field>
  );
}

export default function ProviderConfig() {
  const {
    t,
    form,
    advancedFieldsVisible,
    embeddingDimValidationMessage,
    handleValueChange,
  } = useSetup();

  if (!advancedFieldsVisible) return null;

  return (
    <GlassCard className="p-5 sm:p-6">
      <div className="mb-5 flex items-center gap-3">
        <ServerCog size={18} className="text-[color:var(--palace-accent-2)]" />
        <div>
          <div className="text-lg font-semibold text-[color:var(--palace-ink)]">
            {t('setup.advanced.title')}
          </div>
          <div className="mt-1 text-sm text-[color:var(--palace-muted)]">
            {t('setup.advanced.subtitle')}
          </div>
        </div>
      </div>

      <div className="grid gap-4 xl:grid-cols-2">
        <div className={CARD_CLASS}>
          <div className="mb-4 flex items-center gap-2 text-sm font-semibold text-[color:var(--palace-ink)]">
            <Database size={16} />
            {t('setup.advanced.embedding')}
          </div>
          <div className="mb-4 text-sm text-[color:var(--palace-muted)]">
            {t('setup.advanced.embeddingHint')}
          </div>
          <div className="grid gap-4 md:grid-cols-2">
            <Field id="embedding-api-base" label={t('setup.fields.apiBase')}>
              <input
                id="embedding-api-base"
                name="embeddingApiBase"
                className={INPUT_CLASS}
                value={form.embeddingApiBase}
                onChange={handleValueChange}
              />
            </Field>
            <SecretField
              id="embedding-api-key"
              name="embeddingApiKey"
              label={t('setup.fields.apiKey')}
              value={form.embeddingApiKey}
              onChange={handleValueChange}
            />
            <Field id="embedding-model" label={t('setup.fields.model')}>
              <input
                id="embedding-model"
                name="embeddingModel"
                className={INPUT_CLASS}
                value={form.embeddingModel}
                onChange={handleValueChange}
              />
            </Field>
            <Field id="embedding-dim" label={t('setup.fields.embeddingDim')}>
              <input
                id="embedding-dim"
                name="embeddingDim"
                inputMode="numeric"
                pattern="[0-9]*"
                aria-invalid={Boolean(embeddingDimValidationMessage)}
                className={INPUT_CLASS}
                value={form.embeddingDim}
                onChange={handleValueChange}
              />
              {embeddingDimValidationMessage ? (
                <span className="mt-2 block text-xs text-[color:var(--palace-accent-2)]">
                  {embeddingDimValidationMessage}
                </span>
              ) : null}
            </Field>
          </div>
        </div>

        <div className={CARD_CLASS}>
          <div className="mb-4 flex items-center gap-2 text-sm font-semibold text-[color:var(--palace-ink)]">
            <Workflow size={16} />
            {t('setup.advanced.reranker')}
          </div>
          <div className="mb-4 text-sm text-[color:var(--palace-muted)]">
            {t('setup.advanced.rerankerHint')}
          </div>
          <div className="grid gap-4 md:grid-cols-2">
            <Field id="reranker-api-base" label={t('setup.fields.apiBase')}>
              <input
                id="reranker-api-base"
                name="rerankerApiBase"
                className={INPUT_CLASS}
                value={form.rerankerApiBase}
                onChange={handleValueChange}
              />
            </Field>
            <SecretField
              id="reranker-api-key"
              name="rerankerApiKey"
              label={t('setup.fields.apiKey')}
              value={form.rerankerApiKey}
              onChange={handleValueChange}
            />
            <Field id="reranker-model" label={t('setup.fields.model')}>
              <input
                id="reranker-model"
                name="rerankerModel"
                className={INPUT_CLASS}
                value={form.rerankerModel}
                onChange={handleValueChange}
              />
            </Field>
          </div>
        </div>

        <div className={CARD_CLASS}>
          <div className="mb-4 flex items-center gap-2 text-sm font-semibold text-[color:var(--palace-ink)]">
            <Sparkles size={16} />
            {t('setup.advanced.llm')}
          </div>
          <div className="mb-4 text-sm text-[color:var(--palace-muted)]">
            {t('setup.advanced.llmHint')}
          </div>
          <div className="grid gap-4 md:grid-cols-2">
            <Field id="llm-api-base" label={t('setup.fields.apiBase')}>
              <input
                id="llm-api-base"
                name="llmApiBase"
                className={INPUT_CLASS}
                value={form.llmApiBase}
                onChange={handleValueChange}
              />
            </Field>
            <SecretField
              id="llm-api-key"
              name="llmApiKey"
              label={t('setup.fields.apiKey')}
              value={form.llmApiKey}
              onChange={handleValueChange}
            />
            <Field id="llm-model" label={t('setup.fields.model')}>
              <input
                id="llm-model"
                name="llmModel"
                className={INPUT_CLASS}
                value={form.llmModel}
                onChange={handleValueChange}
              />
            </Field>
          </div>
        </div>

        <div className={CARD_CLASS}>
          <div className="mb-4 flex items-center gap-2 text-sm font-semibold text-[color:var(--palace-ink)]">
            <KeyRound size={16} />
            {t('setup.advanced.writeGuard')}
          </div>
          <div className="grid gap-4 md:grid-cols-2">
            <Field id="write-guard-llm-api-base" label={t('setup.fields.apiBase')}>
              <input
                id="write-guard-llm-api-base"
                name="writeGuardLlmApiBase"
                className={INPUT_CLASS}
                value={form.writeGuardLlmApiBase}
                onChange={handleValueChange}
              />
            </Field>
            <SecretField
              id="write-guard-llm-api-key"
              name="writeGuardLlmApiKey"
              label={t('setup.fields.apiKey')}
              value={form.writeGuardLlmApiKey}
              onChange={handleValueChange}
            />
            <Field id="write-guard-llm-model" label={t('setup.fields.model')}>
              <input
                id="write-guard-llm-model"
                name="writeGuardLlmModel"
                className={INPUT_CLASS}
                value={form.writeGuardLlmModel}
                onChange={handleValueChange}
              />
            </Field>
          </div>
        </div>

        <div className={clsx(CARD_CLASS, 'xl:col-span-2')}>
          <div className="mb-4 flex items-center gap-2 text-sm font-semibold text-[color:var(--palace-ink)]">
            <CheckCircle2 size={16} />
            {t('setup.advanced.compactGist')}
          </div>
          <div className="grid gap-4 md:grid-cols-3">
            <Field id="compact-gist-llm-api-base" label={t('setup.fields.apiBase')}>
              <input
                id="compact-gist-llm-api-base"
                name="compactGistLlmApiBase"
                className={INPUT_CLASS}
                value={form.compactGistLlmApiBase}
                onChange={handleValueChange}
              />
            </Field>
            <SecretField
              id="compact-gist-llm-api-key"
              name="compactGistLlmApiKey"
              label={t('setup.fields.apiKey')}
              value={form.compactGistLlmApiKey}
              onChange={handleValueChange}
            />
            <Field id="compact-gist-llm-model" label={t('setup.fields.model')}>
              <input
                id="compact-gist-llm-model"
                name="compactGistLlmModel"
                className={INPUT_CLASS}
                value={form.compactGistLlmModel}
                onChange={handleValueChange}
              />
            </Field>
          </div>
        </div>
      </div>
    </GlassCard>
  );
}

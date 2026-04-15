import React from 'react';
import { Activity, RefreshCw, Search } from 'lucide-react';

const MODE_OPTIONS = ['hybrid', 'semantic', 'keyword'];
const INPUT_CLASS =
  'w-full rounded-lg border border-[color:var(--palace-line)] bg-white/90 px-3 py-2 text-sm text-[color:var(--palace-ink)] placeholder:text-[color:var(--palace-muted)] focus:outline-none focus:ring-2 focus:ring-[color:var(--palace-accent)]/35 focus:border-[color:var(--palace-accent)]';
const LABEL_CLASS =
  'mb-2 block text-xs font-medium uppercase tracking-[0.14em] text-[color:var(--palace-muted)]';
const PANEL_CLASS =
  'rounded-2xl border border-[color:var(--palace-line)] bg-[rgba(255,250,244,0.9)] p-4 shadow-[var(--palace-shadow-sm)] backdrop-blur-sm';

export default function SearchConsoleCard({ form, onFieldChange, runSearch, searching, searchError, t }) {
  return (
    <form onSubmit={runSearch} noValidate className={PANEL_CLASS}>
      <h2 className="mb-4 flex items-center gap-2 text-sm font-semibold text-[color:var(--palace-ink)]">
        <Activity size={15} className="text-[color:var(--palace-accent)]" />
        {t('observability.searchConsole')}
      </h2>

      <label htmlFor="obs-query-input" className={LABEL_CLASS}>
        {t('observability.query')}
      </label>
      <input
        id="obs-query-input"
        name="query"
        value={form.query}
        onChange={(e) => onFieldChange('query', e.target.value)}
        className={`mb-3 ${INPUT_CLASS}`}
        placeholder={t('observability.placeholders.query')}
      />

      <div className="mb-3 grid grid-cols-2 gap-2">
        <div>
          <label htmlFor="obs-mode-select" className={LABEL_CLASS}>
            {t('observability.mode')}
          </label>
          <select
            id="obs-mode-select"
            name="mode"
            value={form.mode}
            onChange={(e) => onFieldChange('mode', e.target.value)}
            className={INPUT_CLASS}
          >
            {MODE_OPTIONS.map((option) => (
              <option key={option} value={option}>
                {t(`observability.modes.${option}`, { defaultValue: option })}
              </option>
            ))}
          </select>
        </div>
        <div>
          <label htmlFor="obs-session-id-input" className={LABEL_CLASS}>
            {t('observability.sessionId')}
          </label>
          <input
            id="obs-session-id-input"
            name="session_id"
            value={form.sessionId}
            onChange={(e) => onFieldChange('sessionId', e.target.value)}
            className={INPUT_CLASS}
            placeholder={t('observability.placeholders.sessionId')}
          />
        </div>
      </div>

      <div className="mb-3 grid grid-cols-2 gap-2">
        <div>
          <label htmlFor="obs-max-results-input" className={LABEL_CLASS}>
            {t('observability.maxResults')}
          </label>
          <input
            id="obs-max-results-input"
            name="max_results"
            type="number"
            min="1"
            max="50"
            value={form.maxResults}
            onChange={(e) => onFieldChange('maxResults', e.target.value)}
            className={INPUT_CLASS}
          />
        </div>
        <div>
          <label htmlFor="obs-candidate-multiplier-input" className={LABEL_CLASS}>
            {t('observability.candidateMultiplier')}
          </label>
          <input
            id="obs-candidate-multiplier-input"
            name="candidate_multiplier"
            type="number"
            min="1"
            max="20"
            value={form.candidateMultiplier}
            onChange={(e) => onFieldChange('candidateMultiplier', e.target.value)}
            className={INPUT_CLASS}
          />
        </div>
      </div>

      <div className="mb-3 grid grid-cols-2 gap-2">
        <input
          id="obs-domain-filter-input"
          name="domain_filter"
          aria-label={t('observability.domainFilter')}
          value={form.domain}
          onChange={(e) => onFieldChange('domain', e.target.value)}
          className={INPUT_CLASS}
          placeholder={t('observability.placeholders.domainFilter')}
        />
        <input
          id="obs-path-prefix-input"
          name="path_prefix"
          aria-label={t('observability.pathPrefixFilter')}
          value={form.pathPrefix}
          onChange={(e) => onFieldChange('pathPrefix', e.target.value)}
          className={INPUT_CLASS}
          placeholder={t('observability.placeholders.pathPrefix')}
        />
      </div>

      <div className="mb-3">
        <input
          id="obs-scope-hint-input"
          name="scope_hint"
          aria-label={t('observability.scopeHint')}
          value={form.scopeHint}
          onChange={(e) => onFieldChange('scopeHint', e.target.value)}
          className={INPUT_CLASS}
          placeholder={t('observability.placeholders.scopeHint')}
        />
      </div>

      <div className="mb-4 flex items-center justify-between gap-2">
        <input
          id="obs-max-priority-input"
          name="max_priority"
          type="number"
          min="0"
          step="1"
          aria-label={t('observability.maxPriorityFilter')}
          value={form.maxPriority}
          onChange={(e) => onFieldChange('maxPriority', e.target.value)}
          className={INPUT_CLASS}
          placeholder={t('observability.placeholders.maxPriority')}
        />
        <label
          htmlFor="obs-include-session-checkbox"
          className="inline-flex cursor-pointer items-center gap-2 text-xs text-[color:var(--palace-muted)]"
        >
          <input
            id="obs-include-session-checkbox"
            name="include_session"
            type="checkbox"
            checked={form.includeSession}
            onChange={(e) => onFieldChange('includeSession', e.target.checked)}
            className="h-4 w-4 rounded border-[color:var(--palace-line)] bg-white text-[color:var(--palace-accent)] focus:ring-[color:var(--palace-accent)]/40"
          />
          {t('observability.includeSessionFirst')}
        </label>
      </div>

      <button
        type="submit"
        disabled={searching}
        className="inline-flex w-full cursor-pointer items-center justify-center gap-2 rounded-lg border border-[color:var(--palace-accent)] bg-[linear-gradient(135deg,rgba(198,165,126,0.34),rgba(255,250,244,0.92))] px-3 py-2 text-sm font-medium text-[color:var(--palace-ink)] transition-colors hover:border-[color:var(--palace-accent-2)] hover:bg-[linear-gradient(135deg,rgba(191,154,110,0.42),rgba(255,250,244,0.95))] disabled:cursor-not-allowed disabled:opacity-60 focus:outline-none focus:ring-2 focus:ring-[color:var(--palace-accent)]/35"
      >
        {searching ? (
          <RefreshCw size={14} className="animate-spin" />
        ) : (
          <Search size={14} />
        )}
        {t('observability.runDiagnosticSearch')}
      </button>

      {searchError && (
        <div className="mt-3 rounded-md border border-[rgba(143,106,69,0.45)] bg-[rgba(232,218,198,0.88)] px-3 py-2 text-xs text-[color:var(--palace-accent-2)]">
          {searchError}
        </div>
      )}
    </form>
  );
}

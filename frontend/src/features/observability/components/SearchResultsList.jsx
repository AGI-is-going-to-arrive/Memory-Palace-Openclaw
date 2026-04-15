import React from 'react';
import { RefreshCw } from 'lucide-react';
import Badge from './Badge';
import HighlightedSnippet from '../../../components/HighlightedSnippet';

function ResultCard({ item, t }) {
  const finalScore = item?.scores?.final;
  const scoreText = finalScore === undefined ? '-' : Number(finalScore).toFixed(4);
  const uri = item?.uri || '-';
  const snippet = item?.snippet || t('observability.result.emptySnippet');
  const metadata = item?.metadata || {};
  const source = metadata.source || metadata.match_type || 'global';

  return (
    <article className="rounded-2xl border border-[color:var(--palace-line)] bg-[rgba(255,250,244,0.9)] p-4 shadow-[var(--palace-shadow-sm)] transition duration-200 hover:border-[color:var(--palace-accent-2)] hover:shadow-[var(--palace-shadow-md)]">
      <header className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <code className="break-all text-xs text-[color:var(--palace-accent-2)]">{uri}</code>
        <div className="flex items-center gap-2">
          <Badge tone="neutral">{t('observability.result.score', { value: scoreText })}</Badge>
          <Badge tone={source === 'session_queue' ? 'good' : 'neutral'}>{source}</Badge>
        </div>
      </header>
      <p className="mb-3 whitespace-pre-wrap text-sm leading-relaxed text-[color:var(--palace-ink)]">
        <HighlightedSnippet text={snippet} />
      </p>
      <footer className="flex flex-wrap gap-2 text-[11px] text-[color:var(--palace-muted)]">
        <span>{t('observability.result.memory', { value: item?.memory_id ?? '-' })}</span>
        <span>{t('observability.result.priority', { value: metadata.priority ?? '-' })}</span>
        <span>{metadata.updated_at || t('observability.result.updatedAtUnknown')}</span>
      </footer>
    </article>
  );
}

export default function SearchResultsList({ searchResult, searching, t }) {
  return (
    <div className="space-y-3">
      {searching && (
        <div className="flex items-center gap-2 rounded-lg border border-[color:var(--palace-line)] bg-[rgba(255,250,244,0.86)] px-3 py-2 text-sm text-[color:var(--palace-muted)]">
          <RefreshCw size={14} className="animate-spin" />
          {t('observability.runningQuery')}
        </div>
      )}
      {!searching && searchResult?.results?.length === 0 && (
        <div className="rounded-lg border border-[color:var(--palace-line)] bg-[rgba(255,250,244,0.86)] px-3 py-3 text-sm text-[color:var(--palace-muted)]">
          {t('observability.noMatchedSnippets')}
        </div>
      )}
      {(searchResult?.results || []).map((item, idx) => (
        <ResultCard key={`${item.uri || 'result'}-${idx}`} item={item} t={t} />
      ))}
    </div>
  );
}

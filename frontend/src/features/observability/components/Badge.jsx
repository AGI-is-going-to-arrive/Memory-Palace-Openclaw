import React from 'react';
import clsx from 'clsx';

function Badge({ children, tone = 'neutral' }) {
  return (
    <span
      className={clsx(
        'inline-flex items-center rounded border px-2 py-0.5 text-[11px] font-medium',
        tone === 'good' && 'border-[rgba(179,133,79,0.5)] bg-[rgba(246,237,224,0.85)] text-[color:var(--palace-accent-2)]',
        tone === 'warn' && 'border-[rgba(200,171,134,0.65)] bg-[rgba(240,230,215,0.9)] text-[color:var(--palace-accent-2)]',
        tone === 'danger' && 'border-[rgba(143,106,69,0.45)] bg-[rgba(232,218,198,0.9)] text-[color:var(--palace-accent-2)]',
        tone === 'neutral' && 'border-[color:var(--palace-line)] bg-[rgba(255,250,244,0.84)] text-[color:var(--palace-muted)]'
      )}
    >
      {children}
    </span>
  );
}

function TraceBadgeList({ title, entries }) {
  if (!Array.isArray(entries) || entries.length === 0) {
    return null;
  }

  return (
    <div>
      <div className="mb-2 text-[11px] uppercase tracking-[0.14em] text-[color:var(--palace-muted)]">
        {title}
      </div>
      <div className="flex flex-wrap gap-2">
        {entries.map(([key, value]) => (
          <Badge key={key} tone="neutral">
            {key}: {String(value)}
          </Badge>
        ))}
      </div>
    </div>
  );
}

export default React.memo(Badge);
export const MemoTraceBadgeList = React.memo(TraceBadgeList);

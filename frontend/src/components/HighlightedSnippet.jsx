import React from 'react';

/**
 * Renders text with <<matched>> markers as <mark> elements.
 * The backend search wraps query-term matches in << >> delimiters.
 */
export default function HighlightedSnippet({ text, className }) {
  if (!text || !text.includes('<<')) {
    return <span className={className}>{text}</span>;
  }

  const parts = [];
  let remaining = text;
  let key = 0;

  while (remaining) {
    const start = remaining.indexOf('<<');
    if (start === -1) {
      parts.push(remaining);
      break;
    }
    const end = remaining.indexOf('>>', start + 2);
    if (end === -1) {
      parts.push(remaining);
      break;
    }
    if (start > 0) {
      parts.push(remaining.slice(0, start));
    }
    parts.push(
      <mark
        key={key++}
        className="rounded-sm bg-amber-200/60 px-0.5 text-inherit dark:bg-amber-500/30"
      >
        {remaining.slice(start + 2, end)}
      </mark>
    );
    remaining = remaining.slice(end + 2);
  }

  return <span className={className}>{parts}</span>;
}

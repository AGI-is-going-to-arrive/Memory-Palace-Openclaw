import React from 'react';
import clsx from 'clsx';

const INPUT_CLASS =
  'w-full rounded-xl border border-[color:var(--palace-line)] bg-white/90 px-3 py-2 text-sm text-[color:var(--palace-ink)] placeholder:text-[color:var(--palace-muted)] focus:outline-none focus:ring-2 focus:ring-[color:var(--palace-accent)]/35 focus:border-[color:var(--palace-accent)]';
const LABEL_CLASS =
  'mb-2 block text-xs font-medium uppercase tracking-[0.14em] text-[color:var(--palace-muted)]';

export function StatusPill({ active, children }) {
  return (
    <span
      className={clsx(
        'inline-flex items-center gap-1 rounded-full px-3 py-1 text-xs font-semibold',
        active
          ? 'bg-[rgba(212,175,55,0.16)] text-[color:var(--palace-ink)] ring-1 ring-[rgba(212,175,55,0.28)]'
          : 'bg-white/70 text-[color:var(--palace-muted)] ring-1 ring-[color:var(--palace-line)]'
      )}
    >
      {children}
    </span>
  );
}

export function SummaryItem({ label, value, tone = 'neutral' }) {
  return (
    <div
      className={clsx(
        'rounded-xl border px-4 py-3',
        tone === 'good' && 'border-[rgba(212,175,55,0.38)] bg-[rgba(251,245,236,0.92)]',
        tone === 'warn' && 'border-[rgba(184,150,46,0.35)] bg-[rgba(244,236,224,0.95)]',
        tone === 'neutral' && 'border-[color:var(--palace-line)] bg-white/70'
      )}
    >
      <div className="text-[11px] uppercase tracking-[0.16em] text-[color:var(--palace-muted)]">
        {label}
      </div>
      <div className="mt-2 text-sm font-semibold text-[color:var(--palace-ink)]">{value}</div>
    </div>
  );
}

export function ProviderStatusPill({ status, label }) {
  return (
    <span
      className={clsx(
        'inline-flex items-center rounded-full px-2.5 py-1 text-[11px] font-semibold uppercase tracking-[0.14em]',
        status === 'pass' && 'bg-[rgba(212,175,55,0.14)] text-[color:var(--palace-ink)]',
        status === 'missing' && 'bg-[rgba(184,150,46,0.14)] text-[color:var(--palace-ink)]',
        status === 'fail' && 'bg-[rgba(143,106,69,0.14)] text-[color:var(--palace-ink)]',
        status === 'fallback' && 'bg-[rgba(184,150,46,0.14)] text-[color:var(--palace-ink)]',
        status === 'not_checked' && 'bg-white/70 text-[color:var(--palace-muted)]',
        status === 'not_required' && 'bg-white/70 text-[color:var(--palace-muted)]',
        status === 'unknown' && 'bg-white/70 text-[color:var(--palace-muted)]'
      )}
    >
      {label}
    </span>
  );
}

export function Field({ id, label, children, hint }) {
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

export function SecretField({ id, name, label, value, onChange, hint }) {
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

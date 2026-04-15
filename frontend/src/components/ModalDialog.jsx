import React, { useCallback, useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';

function Overlay({ children, onClose }) {
  const overlayRef = useRef(null);
  const handleOverlayClick = useCallback(
    (e) => {
      if (e.target === overlayRef.current) onClose?.();
    },
    [onClose]
  );

  useEffect(() => {
    const handleEsc = (e) => {
      if (e.key === 'Escape') onClose?.();
    };
    window.addEventListener('keydown', handleEsc);
    return () => window.removeEventListener('keydown', handleEsc);
  }, [onClose]);

  return (
    <div
      ref={overlayRef}
      onClick={handleOverlayClick}
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm"
      role="dialog"
      aria-modal="true"
    >
      <div className="mx-4 w-full max-w-md rounded-2xl border border-[color:var(--palace-line)] bg-[rgba(255,250,244,0.97)] p-6 shadow-lg">
        {children}
      </div>
    </div>
  );
}

export function ConfirmDialog({ open, title, message, onConfirm, onCancel }) {
  const { t } = useTranslation();
  if (!open) return null;

  return (
    <Overlay onClose={onCancel}>
      {title && (
        <h3 className="mb-2 text-sm font-semibold text-[color:var(--palace-ink)]">{title}</h3>
      )}
      <p className="mb-5 text-xs text-[color:var(--palace-muted)]">{message}</p>
      <div className="flex justify-end gap-3">
        <button
          type="button"
          onClick={onCancel}
          className="cursor-pointer rounded-lg border border-[color:var(--palace-line)] bg-white/90 px-4 py-2 text-xs text-[color:var(--palace-muted)] transition-colors hover:border-[color:var(--palace-accent)] hover:text-[color:var(--palace-ink)]"
        >
          {t('modal.cancel', 'Cancel')}
        </button>
        <button
          type="button"
          onClick={onConfirm}
          autoFocus
          className="cursor-pointer rounded-lg border border-[color:var(--palace-accent)] bg-[rgba(246,237,224,0.9)] px-4 py-2 text-xs font-medium text-[color:var(--palace-accent-2)] transition-colors hover:bg-[rgba(240,230,215,1)]"
        >
          {t('modal.confirm', 'Confirm')}
        </button>
      </div>
    </Overlay>
  );
}

export function PromptDialog({ open, title, message, defaultValue, inputType, onSubmit, onCancel }) {
  const { t } = useTranslation();
  const [value, setValue] = useState(defaultValue || '');
  const inputRef = useRef(null);

  useEffect(() => {
    if (open) {
      setValue(defaultValue || '');
      const timer = setTimeout(() => inputRef.current?.focus(), 50);
      return () => clearTimeout(timer);
    }
  }, [open, defaultValue]);

  if (!open) return null;

  const handleSubmit = (e) => {
    e.preventDefault();
    onSubmit?.(value);
  };

  return (
    <Overlay onClose={onCancel}>
      {title && (
        <h3 className="mb-2 text-sm font-semibold text-[color:var(--palace-ink)]">{title}</h3>
      )}
      {message && (
        <p className="mb-3 text-xs text-[color:var(--palace-muted)]">{message}</p>
      )}
      <form onSubmit={handleSubmit}>
        <input
          ref={inputRef}
          type={inputType || 'text'}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          className="mb-4 w-full rounded-lg border border-[color:var(--palace-line)] bg-white/90 px-3 py-2 text-xs text-[color:var(--palace-ink)] outline-none focus:border-[color:var(--palace-accent)]"
        />
        <div className="flex justify-end gap-3">
          <button
            type="button"
            onClick={onCancel}
            className="cursor-pointer rounded-lg border border-[color:var(--palace-line)] bg-white/90 px-4 py-2 text-xs text-[color:var(--palace-muted)] transition-colors hover:border-[color:var(--palace-accent)] hover:text-[color:var(--palace-ink)]"
          >
            {t('modal.cancel', 'Cancel')}
          </button>
          <button
            type="submit"
            className="cursor-pointer rounded-lg border border-[color:var(--palace-accent)] bg-[rgba(246,237,224,0.9)] px-4 py-2 text-xs font-medium text-[color:var(--palace-accent-2)] transition-colors hover:bg-[rgba(240,230,215,1)]"
          >
            {t('modal.ok', 'OK')}
          </button>
        </div>
      </form>
    </Overlay>
  );
}

export function AlertDialog({ open, title, message, onClose }) {
  const { t } = useTranslation();
  if (!open) return null;

  return (
    <Overlay onClose={onClose}>
      {title && (
        <h3 className="mb-2 text-sm font-semibold text-[color:var(--palace-ink)]">{title}</h3>
      )}
      <p className="mb-5 text-xs text-[color:var(--palace-muted)]">{message}</p>
      <div className="flex justify-end">
        <button
          type="button"
          onClick={onClose}
          autoFocus
          className="cursor-pointer rounded-lg border border-[color:var(--palace-accent)] bg-[rgba(246,237,224,0.9)] px-4 py-2 text-xs font-medium text-[color:var(--palace-accent-2)] transition-colors hover:bg-[rgba(240,230,215,1)]"
        >
          {t('modal.ok', 'OK')}
        </button>
      </div>
    </Overlay>
  );
}

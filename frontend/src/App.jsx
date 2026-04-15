import React from 'react';
import { BrowserRouter, Routes, Route, NavLink, Navigate, useNavigate } from 'react-router-dom';
import { ShieldCheck, Database, LibraryBig, Feather, Eye, Languages, Settings2 } from 'lucide-react';
import clsx from 'clsx';
import { motion } from 'framer-motion';
import { useTranslation } from 'react-i18next';

import ReviewPage from './features/review/ReviewPage';
import MemoryBrowser from './features/memory/MemoryBrowser';
import MaintenancePage from './features/maintenance/MaintenancePage';
import ObservabilityPage from './features/observability/ObservabilityPage';
import SetupPage from './features/setup/SetupPage';
import FluidBackground from './components/FluidBackground';
import { PromptDialog, AlertDialog } from './components/ModalDialog';
import {
  clearStoredMaintenanceAuth,
  getBootstrapStatus,
  getMaintenanceAuthState,
  MAINTENANCE_AUTH_CHANGE_EVENT,
  saveStoredMaintenanceAuth,
} from './lib/api';
import { CHINESE_LOCALE, DEFAULT_LOCALE } from './i18n';

function hasSameAuthState(currentValue, nextValue) {
  if (currentValue === nextValue) return true;
  if (!currentValue || !nextValue) return false;
  return currentValue.source === nextValue.source
    && currentValue.mode === nextValue.mode
    && currentValue.key === nextValue.key;
}

function NavItem({ to, icon: Icon, label, disabled = false, onClick }) {
  const baseClass = "relative flex h-10 shrink-0 items-center gap-2 rounded-full px-4 text-sm font-medium transition-all duration-300 whitespace-nowrap";

  if (disabled) {
    return (
      <button
        type="button"
        onClick={onClick}
        className={clsx(
          baseClass,
          "text-[color:var(--palace-muted)]/80 hover:text-[color:var(--palace-ink)]"
        )}
      >
        <span className="relative z-10 flex items-center gap-2">
          <Icon size={16} className="text-current" />
          {label}
        </span>
      </button>
    );
  }

  return (
    <NavLink
      to={to}
      className={({ isActive }) => clsx(
        baseClass,
        isActive
          ? "text-[color:var(--palace-ink)]"
          : "text-[color:var(--palace-muted)] hover:text-[color:var(--palace-ink)]"
      )}
    >
      {({ isActive }) => (
        <>
          {isActive && (
            <motion.div
              layoutId="nav-pill"
              className="absolute inset-0 rounded-full bg-white shadow-[0_2px_12px_rgba(212,175,55,0.15)] ring-1 ring-[color:var(--palace-accent)]/20"
              transition={{ type: "spring", bounce: 0.2, duration: 0.6 }}
            />
          )}
          <span className="relative z-10 flex items-center gap-2">
            <Icon size={16} className={clsx(isActive ? "text-[color:var(--palace-accent)]" : "text-current")} />
            {label}
          </span>
        </>
      )}
    </NavLink>
  );
}

function AuthControls({ authState, onSetApiKey, onClearApiKey }) {
  const { t } = useTranslation();

  if (authState?.source === 'runtime') {
    return (
      <div className="hidden md:flex items-center rounded-full border border-emerald-500/20 bg-emerald-500/10 px-3 py-2 text-xs font-medium text-emerald-700 shadow-sm">
        {t('app.auth.runtimeBadge')}
      </div>
    );
  }

  return (
    <div className="flex flex-wrap items-center justify-end gap-2 sm:flex-nowrap">
      <button
        type="button"
        onClick={onSetApiKey}
        data-testid="auth-set-api-key"
        className="rounded-full border border-white/40 bg-white/40 px-3 py-2 text-xs font-medium text-[color:var(--palace-ink)] backdrop-blur-md transition whitespace-nowrap hover:bg-white/60"
      >
        {authState ? t('app.auth.updateApiKey') : t('app.auth.setApiKey')}
      </button>
      {authState ? (
        <button
          type="button"
          onClick={onClearApiKey}
          data-testid="auth-clear-api-key"
          className="rounded-full border border-white/30 bg-white/20 px-3 py-2 text-xs font-medium text-[color:var(--palace-muted)] backdrop-blur-md transition whitespace-nowrap hover:bg-white/40 hover:text-[color:var(--palace-ink)]"
        >
          {t('app.auth.clearKey')}
        </button>
      ) : null}
    </div>
  );
}

function LanguageToggle() {
  const { t, i18n } = useTranslation();
  const currentLocale = i18n.resolvedLanguage || DEFAULT_LOCALE;
  const nextLocale = currentLocale === DEFAULT_LOCALE ? CHINESE_LOCALE : DEFAULT_LOCALE;
  const nextLabel = nextLocale === CHINESE_LOCALE
    ? t('common.language.chinese')
    : t('common.language.english');
  const ariaLabel = nextLocale === CHINESE_LOCALE
    ? t('common.language.switchToChinese')
    : t('common.language.switchToEnglish');

  const handleToggle = React.useCallback(() => {
    void i18n.changeLanguage(nextLocale);
  }, [i18n, nextLocale]);

  return (
    <button
      type="button"
      onClick={handleToggle}
      data-testid="language-toggle"
      aria-label={ariaLabel}
      title={ariaLabel}
      className="inline-flex items-center gap-2 rounded-full border border-white/40 bg-white/40 px-3 py-2 text-xs font-medium text-[color:var(--palace-ink)] backdrop-blur-md transition whitespace-nowrap hover:bg-white/60"
    >
      <Languages size={14} />
      <span className="hidden sm:inline">{nextLabel}</span>
    </button>
  );
}

export function buildRoutesKey(authState, authRevision) {
  return authState
    ? `${authState.source}:${authState.mode}:${authRevision}`
    : `no-auth:${authRevision}`;
}

function RootRedirect({ bootstrapStatus, bootstrapLoading }) {
  const { t } = useTranslation();

  if (bootstrapLoading) {
    return (
      <div className="flex h-full min-h-[320px] items-center justify-center">
        <div className="glass-card rounded-2xl px-5 py-4 text-sm text-[color:var(--palace-muted)]">
          {t('setup.loadingStatus')}
        </div>
      </div>
    );
  }

  if (bootstrapStatus?.setup?.requiresOnboarding) {
    return <Navigate to="/setup" replace />;
  }

  if (!bootstrapStatus) {
    return <Navigate to="/setup" replace />;
  }

  return <Navigate to="/memory" replace />;
}

function OnboardingGate({ bootstrapStatus, bootstrapLoading, bootstrapError, children }) {
  const { t } = useTranslation();

  if (bootstrapLoading) {
    return (
      <div className="flex h-full min-h-[320px] items-center justify-center">
        <div className="glass-card rounded-2xl px-5 py-4 text-sm text-[color:var(--palace-muted)]">
          {t('setup.loadingStatus')}
        </div>
      </div>
    );
  }

  if (bootstrapStatus?.setup?.requiresOnboarding) {
    return <Navigate to="/setup" replace />;
  }

  if (bootstrapError || !bootstrapStatus) {
    return <Navigate to="/setup" replace />;
  }

  return children;
}

function Layout({
  authState,
  authRevision,
  bootstrapStatus,
  bootstrapLoading,
  bootstrapError,
  onRefreshBootstrapStatus,
  onSetApiKey,
  onClearApiKey,
}) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const routesKey = buildRoutesKey(authState, authRevision);
  const protectedNavLocked = (
    bootstrapLoading
    || Boolean(bootstrapError)
    || !bootstrapStatus
    || Boolean(bootstrapStatus?.setup?.requiresOnboarding)
  );
  const handleProtectedNavClick = React.useCallback(() => {
    navigate('/setup');
  }, [navigate]);

  return (
    <div className="relative flex h-screen flex-col overflow-hidden text-[color:var(--palace-ink)]">
      <FluidBackground />

      {/* Floating Header */}
      <div className="relative z-20 shrink-0 px-4 pb-2 pt-4 sm:px-6 sm:pt-6">
        <div className="mx-auto flex max-w-7xl flex-col gap-3 xl:flex-row xl:items-center xl:justify-between">
          <motion.div
            initial={{ opacity: 0, x: -20 }}
            animate={{ opacity: 1, x: 0 }}
            className="flex min-w-0 shrink-0 items-center gap-3 rounded-2xl border border-white/40 bg-white/40 px-3 py-2 backdrop-blur-md shadow-sm sm:px-4"
          >
            <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-[linear-gradient(135deg,var(--palace-accent),var(--palace-accent-2))] text-white shadow-md">
              <LibraryBig size={18} />
            </div>
            <span className="font-display text-base font-semibold tracking-wide text-[color:var(--palace-ink)] sm:text-lg">
              {t('common.appName')}
            </span>
          </motion.div>

          <motion.nav
            aria-label="Main navigation"
            initial={{ opacity: 0, y: -20 }}
            animate={{ opacity: 1, y: 0 }}
            className="order-3 flex w-full min-w-0 items-center gap-1 overflow-x-auto rounded-full border border-white/30 bg-white/20 p-1.5 backdrop-blur-xl shadow-[0_8px_32px_rgba(179,133,79,0.05)] scrollbar-hide xl:order-none xl:w-auto xl:flex-1 xl:justify-center"
          >
            <NavItem to="/setup" icon={Settings2} label={t('app.nav.setup')} />
            <NavItem
              to="/memory"
              icon={Database}
              label={t('app.nav.memory')}
              disabled={protectedNavLocked}
              onClick={handleProtectedNavClick}
            />
            <NavItem
              to="/review"
              icon={ShieldCheck}
              label={t('app.nav.review')}
              disabled={protectedNavLocked}
              onClick={handleProtectedNavClick}
            />
            <NavItem
              to="/maintenance"
              icon={Feather}
              label={t('app.nav.maintenance')}
              disabled={protectedNavLocked}
              onClick={handleProtectedNavClick}
            />
            <NavItem
              to="/observability"
              icon={Eye}
              label={t('app.nav.observability')}
              disabled={protectedNavLocked}
              onClick={handleProtectedNavClick}
            />
          </motion.nav>

          <div className="order-2 flex flex-wrap items-center justify-end gap-2 xl:order-none xl:shrink-0">
            <LanguageToggle />
            <AuthControls
              authState={authState}
              onSetApiKey={onSetApiKey}
              onClearApiKey={onClearApiKey}
            />
          </div>
        </div>
      </div>

      {/* Main Area */}
      <div className="relative z-10 flex-1 min-h-0 overflow-hidden px-4 pb-4 pt-2 sm:px-6 sm:pb-6">
        <div className="h-full w-full max-w-7xl mx-auto">
            <Routes key={routesKey}>
              <Route
                path="/"
                element={
                  <RootRedirect
                    bootstrapStatus={bootstrapStatus}
                    bootstrapLoading={bootstrapLoading}
                  />
                }
              />
              <Route
                path="/setup"
                element={(
                  <SetupPage
                    bootstrapStatus={bootstrapStatus}
                    statusLoading={bootstrapLoading}
                    statusError={bootstrapError}
                    onRefreshStatus={onRefreshBootstrapStatus}
                  />
                )}
              />
              <Route
                path="/review"
                element={(
                  <OnboardingGate
                    bootstrapStatus={bootstrapStatus}
                    bootstrapLoading={bootstrapLoading}
                    bootstrapError={bootstrapError}
                  >
                    <ReviewPage />
                  </OnboardingGate>
                )}
              />
              <Route
                path="/memory"
                element={(
                  <OnboardingGate
                    bootstrapStatus={bootstrapStatus}
                    bootstrapLoading={bootstrapLoading}
                    bootstrapError={bootstrapError}
                  >
                    <MemoryBrowser />
                  </OnboardingGate>
                )}
              />
              <Route
                path="/maintenance"
                element={(
                  <OnboardingGate
                    bootstrapStatus={bootstrapStatus}
                    bootstrapLoading={bootstrapLoading}
                    bootstrapError={bootstrapError}
                  >
                    <MaintenancePage />
                  </OnboardingGate>
                )}
              />
              <Route
                path="/observability"
                element={(
                  <OnboardingGate
                    bootstrapStatus={bootstrapStatus}
                    bootstrapLoading={bootstrapLoading}
                    bootstrapError={bootstrapError}
                  >
                    <ObservabilityPage />
                  </OnboardingGate>
                )}
              />
              <Route
                path="*"
                element={(
                  <RootRedirect
                    bootstrapStatus={bootstrapStatus}
                    bootstrapLoading={bootstrapLoading}
                  />
                )}
              />
            </Routes>
        </div>
      </div>
    </div>
  );
}

function App() {
  const { t, i18n } = useTranslation();
  const [authState, setAuthState] = React.useState(() => getMaintenanceAuthState());
  const [authRevision, setAuthRevision] = React.useState(0);
  const authStateRef = React.useRef(authState);
  const bootstrapRequestRef = React.useRef(0);
  const [bootstrapStatus, setBootstrapStatus] = React.useState(null);
  const [bootstrapLoading, setBootstrapLoading] = React.useState(true);
  const [bootstrapError, setBootstrapError] = React.useState(null);

  React.useEffect(() => {
    authStateRef.current = authState;
  }, [authState]);

  React.useEffect(() => {
    document.title = t('app.documentTitle');
  }, [i18n.resolvedLanguage, t]);

  const syncAuthState = React.useCallback((nextAuthState = getMaintenanceAuthState()) => {
    if (hasSameAuthState(authStateRef.current, nextAuthState)) return;
    authStateRef.current = nextAuthState;
    setAuthState(nextAuthState);
    setAuthRevision((value) => value + 1);
  }, []);

  const refreshBootstrapStatus = React.useCallback(async () => {
    const requestId = bootstrapRequestRef.current + 1;
    bootstrapRequestRef.current = requestId;
    setBootstrapLoading(true);
    setBootstrapError(null);
    try {
      const nextStatus = await getBootstrapStatus();
      if (requestId !== bootstrapRequestRef.current) return null;
      setBootstrapStatus(nextStatus);
      return nextStatus;
    } catch (error) {
      if (requestId !== bootstrapRequestRef.current) return null;
      setBootstrapStatus(null);
      setBootstrapError(error);
      return null;
    } finally {
      if (requestId !== bootstrapRequestRef.current) return;
      setBootstrapLoading(false);
    }
  }, []);

  React.useEffect(() => {
    void refreshBootstrapStatus();
  }, [refreshBootstrapStatus]);

  React.useEffect(() => {
    const handleMaintenanceAuthChange = (event) => {
      syncAuthState(event?.detail ?? getMaintenanceAuthState());
    };
    window.addEventListener(MAINTENANCE_AUTH_CHANGE_EVENT, handleMaintenanceAuthChange);
    return () => {
      window.removeEventListener(MAINTENANCE_AUTH_CHANGE_EVENT, handleMaintenanceAuthChange);
    };
  }, [syncAuthState]);

  const [apiKeyPromptOpen, setApiKeyPromptOpen] = React.useState(false);
  const [emptyKeyAlertOpen, setEmptyKeyAlertOpen] = React.useState(false);

  const handleSetApiKey = React.useCallback(() => {
    setApiKeyPromptOpen(true);
  }, []);

  const handleApiKeySubmit = React.useCallback((nextValue) => {
    setApiKeyPromptOpen(false);
    if (typeof nextValue !== 'string') return;
    const saved = saveStoredMaintenanceAuth(nextValue, authState?.mode ?? 'header');
    if (!saved) {
      setEmptyKeyAlertOpen(true);
      return;
    }
    syncAuthState(saved);
  }, [authState, syncAuthState]);

  const handleClearApiKey = React.useCallback(() => {
    clearStoredMaintenanceAuth();
    syncAuthState(getMaintenanceAuthState());
  }, [syncAuthState]);

  return (
    <BrowserRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
      <PromptDialog
        open={apiKeyPromptOpen}
        title={t('app.auth.prompt')}
        defaultValue={authState?.source === 'stored' ? authState.key : ''}
        inputType="password"
        onSubmit={handleApiKeySubmit}
        onCancel={() => setApiKeyPromptOpen(false)}
      />
      <AlertDialog
        open={emptyKeyAlertOpen}
        message={t('app.auth.emptyKey')}
        onClose={() => setEmptyKeyAlertOpen(false)}
      />
      <Layout
        authState={authState}
        authRevision={authRevision}
        bootstrapStatus={bootstrapStatus}
        bootstrapLoading={bootstrapLoading}
        bootstrapError={bootstrapError}
        onRefreshBootstrapStatus={refreshBootstrapStatus}
        onSetApiKey={handleSetApiKey}
        onClearApiKey={handleClearApiKey}
      />
    </BrowserRouter>
  );
}

export default App;

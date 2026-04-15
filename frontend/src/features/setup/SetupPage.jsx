import React from 'react';
import { AlertTriangle } from 'lucide-react';
import { extractApiError } from '../../lib/api';
import { useTranslation } from 'react-i18next';

import GlassCard from '../../components/GlassCard';
import { SetupProvider, useSetup } from './useSetupContext';
import ProfileSelector from './components/ProfileSelector';
import ProviderConfig from './components/ProviderConfig';
import PreflightChecks from './components/PreflightChecks';
import SetupSummary, { SetupResultCards } from './components/SetupSummary';

function SetupPageInner() {
  const {
    statusErrorMessage,
    handleSubmit,
    t,
  } = useSetup();

  return (
    <div className="h-full overflow-y-auto pr-1">
      <div className="space-y-6 pb-2">
        {/* Profile / preset / mode selection + hero header */}
        <ProfileSelector />

        {/* Status error banner */}
        {statusErrorMessage ? (
          <GlassCard className="border-[rgba(143,106,69,0.28)] bg-[rgba(248,238,226,0.9)] p-4">
            <div className="flex items-start gap-3 text-sm text-[color:var(--palace-ink)]">
              <AlertTriangle className="mt-0.5 shrink-0 text-[color:var(--palace-accent-2)]" size={18} />
              <div>
                <div className="font-semibold">{t('setup.messages.statusLoadFailed')}</div>
                <div className="mt-1 text-[color:var(--palace-muted)]">{statusErrorMessage}</div>
              </div>
            </div>
          </GlassCard>
        ) : null}

        {/* Two-column layout: form (left) + preflight checks (right) */}
        <div className="grid gap-6 xl:grid-cols-[minmax(0,1.08fr)_minmax(320px,0.92fr)] xl:items-start">
          {/* Preflight checks sidebar */}
          <PreflightChecks />

          {/* Configuration form */}
          <form onSubmit={handleSubmit} className="order-1 space-y-6 xl:order-1 xl:self-start">
            {/* Provider config (advanced profiles C/D only) */}
            <ProviderConfig />

            {/* Apply / validate buttons */}
            <SetupSummary />
          </form>
        </div>

        {/* Result cards (errors, success result, reindex gate, restart) */}
        <SetupResultCards />
      </div>
    </div>
  );
}

export default function SetupPage({
  bootstrapStatus,
  statusLoading,
  statusError,
  onRefreshStatus,
}) {
  return (
    <SetupProvider
      bootstrapStatus={bootstrapStatus}
      statusLoading={statusLoading}
      statusError={statusError}
      onRefreshStatus={onRefreshStatus}
    >
      <SetupPageInner />
    </SetupProvider>
  );
}

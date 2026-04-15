import React from 'react';
import { useTranslation } from 'react-i18next';

import {
  applyBootstrapConfiguration,
  extractApiError,
  getMaintenanceAuthState,
  probeBootstrapProviders,
  requestBootstrapRestart,
  saveStoredMaintenanceAuth,
  triggerIndexRebuild,
} from '../../lib/api';
import {
  localizeSetupText,
  localizeSetupTextList,
  reindexGateString,
  reindexGateReasonLabel,
} from './setupI18n';

const DEFAULT_PROFILE_OPTIONS = ['a', 'b', 'c', 'd'];
const DEFAULT_MODE_OPTIONS = ['basic', 'full', 'dev'];
const DEFAULT_TRANSPORT_OPTIONS = ['stdio', 'sse'];
const ADVANCED_PROFILES = new Set(['c', 'd']);

const PROVIDER_COMPONENT_ORDER = ['embedding', 'reranker', 'llm'];

const ADVANCED_FORM_FIELDS = [
  'embeddingApiBase',
  'embeddingApiKey',
  'embeddingModel',
  'embeddingDim',
  'rerankerApiBase',
  'rerankerApiKey',
  'rerankerModel',
  'llmApiBase',
  'llmApiKey',
  'llmModel',
  'writeGuardLlmApiBase',
  'writeGuardLlmApiKey',
  'writeGuardLlmModel',
  'compactGistLlmApiBase',
  'compactGistLlmApiKey',
  'compactGistLlmModel',
];

const SETUP_PRESETS = [
  {
    id: 'basic_local',
    mode: 'basic',
    profile: 'b',
    transport: 'stdio',
    badgeTone: 'default',
    badgeKey: 'default',
  },
  {
    id: 'local_dashboard',
    mode: 'full',
    profile: 'b',
    transport: 'stdio',
    badgeTone: 'optional',
    badgeKey: 'optional',
  },
  {
    id: 'advanced_cd',
    mode: 'full',
    profile: 'c',
    transport: 'stdio',
    badgeTone: 'recommended',
    badgeKey: 'recommended',
  },
];

const getOptionList = (value, fallback) =>
  Array.isArray(value) && value.length > 0 ? value : fallback;

const pickOption = (currentValue, options) => {
  const normalizedCurrent = typeof currentValue === 'string' ? currentValue.trim() : '';
  if (normalizedCurrent && options.includes(normalizedCurrent)) {
    return normalizedCurrent;
  }
  return options[0] || '';
};

const buildInitialForm = (bootstrapStatus) => {
  const setup = bootstrapStatus?.setup || {};
  const profileOptions = getOptionList(
    bootstrapStatus?.profileOptions,
    DEFAULT_PROFILE_OPTIONS
  );
  const modeOptions = getOptionList(bootstrapStatus?.modeOptions, DEFAULT_MODE_OPTIONS);
  const transportOptions = getOptionList(
    bootstrapStatus?.transportOptions,
    DEFAULT_TRANSPORT_OPTIONS
  );

  return {
    mode: pickOption(setup.mode || 'basic', modeOptions),
    profile: pickOption(
      setup.requestedProfile || setup.effectiveProfile || 'b',
      profileOptions
    ),
    transport: pickOption(setup.transport || 'stdio', transportOptions),
    reconfigure: Boolean(setup.requiresOnboarding),
    databasePath: '',
    sseUrl: '',
    mcpApiKey: '',
    allowInsecureLocal: false,
    embeddingApiBase: '',
    embeddingApiKey: '',
    embeddingModel: '',
    embeddingDim: '',
    rerankerApiBase: '',
    rerankerApiKey: '',
    rerankerModel: '',
    llmApiBase: '',
    llmApiKey: '',
    llmModel: '',
    writeGuardLlmApiBase: '',
    writeGuardLlmApiKey: '',
    writeGuardLlmModel: '',
    compactGistLlmApiBase: '',
    compactGistLlmApiKey: '',
    compactGistLlmModel: '',
  };
};

const syncFormWithBootstrapStatus = (
  currentForm,
  bootstrapStatus,
  { preservePathSelection = false } = {}
) => {
  const nextStatusForm = buildInitialForm(bootstrapStatus);
  if (!currentForm) {
    return nextStatusForm;
  }
  return {
    ...currentForm,
    mode: preservePathSelection ? currentForm.mode : nextStatusForm.mode,
    profile: preservePathSelection ? currentForm.profile : nextStatusForm.profile,
    transport: preservePathSelection ? currentForm.transport : nextStatusForm.transport,
    reconfigure: preservePathSelection ? currentForm.reconfigure : nextStatusForm.reconfigure,
  };
};

const clearSecretFields = (currentForm) => ({
  ...currentForm,
  mcpApiKey: '',
  embeddingApiKey: '',
  rerankerApiKey: '',
  llmApiKey: '',
  writeGuardLlmApiKey: '',
  compactGistLlmApiKey: '',
});

export const normalizeEmbeddingDim = (value) => {
  const normalizedValue = typeof value === 'string' ? value.trim() : '';
  if (!normalizedValue) {
    return { value: '' };
  }
  if (!/^\d+$/.test(normalizedValue)) {
    return { value: null, valid: false };
  }
  const parsedValue = Number(normalizedValue);
  if (!Number.isSafeInteger(parsedValue) || parsedValue <= 0) {
    return { value: null, valid: false };
  }
  return { value: parsedValue, valid: true };
};

const buildBootstrapPayload = (form, advancedFieldsVisible, normalizedEmbeddingDim) => {
  const payload = {
    ...form,
    embeddingDim: normalizedEmbeddingDim.value,
  };
  if (advancedFieldsVisible) {
    return payload;
  }
  for (const field of ADVANCED_FORM_FIELDS) {
    delete payload[field];
  }
  return payload;
};

export const getBooleanTone = (value) => (value ? 'good' : 'neutral');

export const normalizeCheckStatus = (value) => {
  const normalized = typeof value === 'string' ? value.trim().toLowerCase() : '';
  if (normalized === 'pass' || normalized === 'warn' || normalized === 'fail') {
    return normalized;
  }
  return 'unknown';
};

export const normalizeProviderStatus = (value) => {
  const normalized = typeof value === 'string' ? value.trim().toLowerCase() : '';
  if (
    normalized === 'pass'
    || normalized === 'fail'
    || normalized === 'missing'
    || normalized === 'fallback'
    || normalized === 'not_checked'
    || normalized === 'not_required'
  ) {
    return normalized;
  }
  return 'unknown';
};

const resolveMissingFieldSection = (field) => {
  const normalized = typeof field === 'string' ? field.trim().toUpperCase() : '';
  if (normalized.startsWith('RETRIEVAL_EMBEDDING_')) return 'embedding';
  if (normalized.startsWith('RETRIEVAL_RERANKER_')) return 'reranker';
  if (normalized.startsWith('WRITE_GUARD_LLM_')) return 'llm';
  return null;
};

const buildProviderProbePayload = (form, normalizedEmbeddingDim) => ({
  mode: form.mode,
  profile: form.profile,
  transport: form.transport,
  sseUrl: form.sseUrl,
  mcpApiKey: form.mcpApiKey,
  allowInsecureLocal: form.allowInsecureLocal,
  embeddingApiBase: form.embeddingApiBase,
  embeddingApiKey: form.embeddingApiKey,
  embeddingModel: form.embeddingModel,
  embeddingDim: normalizedEmbeddingDim.value,
  rerankerApiBase: form.rerankerApiBase,
  rerankerApiKey: form.rerankerApiKey,
  rerankerModel: form.rerankerModel,
  llmApiBase: form.llmApiBase,
  llmApiKey: form.llmApiKey,
  llmModel: form.llmModel,
  writeGuardLlmApiBase: form.writeGuardLlmApiBase,
  writeGuardLlmApiKey: form.writeGuardLlmApiKey,
  writeGuardLlmModel: form.writeGuardLlmModel,
  compactGistLlmApiBase: form.compactGistLlmApiBase,
  compactGistLlmApiKey: form.compactGistLlmApiKey,
  compactGistLlmModel: form.compactGistLlmModel,
});

const resolveRepoPythonCommand = () => {
  if (typeof navigator !== 'undefined' && /win/i.test(String(navigator.platform || ''))) {
    return 'py -3';
  }
  return 'python3';
};

const buildGuidedValidationCommand = ({ configPath, envFile, form }) => {
  if (!configPath || !envFile || !form?.mode || !form?.profile || !form?.transport) {
    return null;
  }
  const segments = String(envFile).split(/[/\\]+/);
  const setupRoot = segments.slice(0, -1).join('/') || String(envFile);
  return [
    `${resolveRepoPythonCommand()} scripts/openclaw_memory_palace.py setup`,
    `--config "${configPath}"`,
    `--setup-root "${setupRoot}"`,
    `--mode ${form.mode}`,
    `--profile ${form.profile}`,
    `--transport ${form.transport}`,
    '--validate',
    '--json',
  ].join(' ');
};

const extractShellEnvName = (value, fallback = 'MCP_API_KEY') => {
  if (typeof value !== 'string') return fallback;
  const match = value.trim().match(/^([A-Z0-9_]+)\b/);
  return match?.[1] || fallback;
};

export const buildPreflightMessage = (check, t) => {
  const id = check?.id;
  const status = normalizeCheckStatus(check?.status);
  const details = check?.details && typeof check.details === 'object' ? check.details : null;
  switch (id) {
    case 'config-path':
      return t('setup.preflight.messages.configPath');
    case 'plugin-load-path':
      return t('setup.preflight.messages.pluginLoadPath');
    case 'bundled-skill':
      return t(
        status === 'pass'
          ? 'setup.preflight.messages.bundledSkillReady'
          : 'setup.preflight.messages.bundledSkillMissing'
      );
    case 'openclaw-bin':
      return t(
        status === 'pass'
          ? 'setup.preflight.messages.openclawBinReady'
          : 'setup.preflight.messages.openclawBinMissing'
      );
    case 'openclaw-version':
      if (details?.required || details?.detected) {
        return t('setup.preflight.messages.openclawVersionDetected', {
          detected: details?.detected || t('common.states.notAvailable'),
          required: details?.required || t('common.states.notAvailable'),
        });
      }
      return t('setup.preflight.messages.openclawVersionUnknown', {
        required: details?.required || t('common.states.notAvailable'),
      });
    case 'stdio-wrapper':
      return t(
        status === 'pass'
          ? 'setup.preflight.messages.stdioWrapperReady'
          : 'setup.preflight.messages.stdioWrapperMissing'
      );
    case 'runtime-env-file':
      return t(
        status === 'pass'
          ? 'setup.preflight.messages.runtimeEnvReady'
          : 'setup.preflight.messages.runtimeEnvMissing'
      );
    case 'backend-venv':
      return t(
        status === 'pass'
          ? 'setup.preflight.messages.backendVenvReady'
          : 'setup.preflight.messages.backendVenvMissing'
      );
    case 'database-url':
      return t(
        status === 'pass'
          ? 'setup.preflight.messages.databaseUrlReady'
          : 'setup.preflight.messages.databaseUrlMissing'
      );
    case 'sse-url':
      return t(
        status === 'pass'
          ? 'setup.preflight.messages.sseUrlReady'
          : 'setup.preflight.messages.sseUrlMissing'
      );
    case 'sse-api-key-env': {
      const envName = extractShellEnvName(check?.message);
      return t(
        status === 'pass'
          ? 'setup.preflight.messages.sseApiKeyEnvReady'
          : 'setup.preflight.messages.sseApiKeyEnvMissing',
        { envName }
      );
    }
    default:
      if (typeof check?.message === 'string' && check.message.trim()) {
        return check.message.trim();
      }
      return t(
        status === 'pass'
          ? 'setup.preflight.messages.genericPass'
          : 'setup.preflight.messages.genericWarn'
      );
  }
};

export const buildPreflightAction = (check, t) => {
  const id = check?.id;
  const status = normalizeCheckStatus(check?.status);
  const details = check?.details && typeof check.details === 'object' ? check.details : null;
  if (status === 'pass') return null;
  switch (id) {
    case 'bundled-skill':
      return t('setup.preflight.actions.bundledSkill');
    case 'openclaw-bin':
      return t('setup.preflight.actions.openclawBin');
    case 'openclaw-version':
      return t('setup.preflight.actions.openclawVersion', {
        required: details?.required || '2026.3.2',
      });
    case 'stdio-wrapper':
      return t('setup.preflight.actions.stdioWrapper');
    case 'backend-venv':
      return t('setup.preflight.actions.backendVenv');
    case 'sse-url':
      return t('setup.preflight.actions.sseUrl');
    case 'sse-api-key-env':
      return t('setup.preflight.actions.sseApiKeyEnv', {
        envName: extractShellEnvName(check?.message),
      });
    default:
      return typeof check?.action === 'string' && check.action.trim() ? check.action.trim() : null;
  }
};

export const buildPreflightDetails = (check, t) => {
  const details = check?.details;
  if (typeof details === 'string' && details.trim()) {
    return details.trim();
  }
  if (!details || typeof details !== 'object') {
    return null;
  }
  if (typeof details.path === 'string' && details.path.trim()) {
    return t('setup.preflight.details.path', { path: details.path.trim() });
  }
  if (typeof details.detected === 'string' || typeof details.required === 'string') {
    return t('setup.preflight.details.version', {
      detected:
        typeof details.detected === 'string' && details.detected.trim()
          ? details.detected.trim()
          : t('common.states.notAvailable'),
      required:
        typeof details.required === 'string' && details.required.trim()
          ? details.required.trim()
          : t('common.states.notAvailable'),
    });
  }
  return null;
};

export const SetupContext = React.createContext(null);

export function SetupProvider({ bootstrapStatus, statusLoading, statusError, onRefreshStatus, children }) {
  const { t } = useTranslation();
  const [form, setForm] = React.useState(() => buildInitialForm(bootstrapStatus));
  const clearSecretsOnNextBootstrapSyncRef = React.useRef(false);
  const pathSelectionDirtyRef = React.useRef(false);
  const [submitState, setSubmitState] = React.useState({
    loading: false,
    error: null,
    result: null,
    action: 'apply',
  });
  const [restartState, setRestartState] = React.useState({
    loading: false,
    error: null,
    result: null,
  });
  const [reindexState, setReindexState] = React.useState({
    loading: false,
    error: null,
    done: false,
  });
  const [probeState, setProbeState] = React.useState({
    loading: false,
    error: null,
    result: null,
  });

  const embeddingDimValidationMessage = React.useMemo(() => {
    const validation = normalizeEmbeddingDim(form.embeddingDim);
    if (form.embeddingDim === '' || validation.valid !== false) {
      return null;
    }
    return t('setup.messages.invalidEmbeddingDim');
  }, [form.embeddingDim, t]);

  React.useEffect(() => {
    if (!bootstrapStatus) return;
    setForm((current) => {
      const syncedForm = syncFormWithBootstrapStatus(current, bootstrapStatus, {
        preservePathSelection: pathSelectionDirtyRef.current,
      });
      if (!clearSecretsOnNextBootstrapSyncRef.current) {
        return syncedForm;
      }
      clearSecretsOnNextBootstrapSyncRef.current = false;
      return clearSecretFields(syncedForm);
    });
    setProbeState((current) => (current.loading ? current : { loading: false, error: null, result: null }));
  }, [bootstrapStatus]);

  const setup = bootstrapStatus?.setup || {};
  const localizedStatusSummary = React.useMemo(
    () => localizeSetupText(bootstrapStatus?.summary, t),
    [bootstrapStatus?.summary, t]
  );
  const localizedSetupWarnings = React.useMemo(
    () => localizeSetupTextList(setup.warnings, t),
    [setup.warnings, t]
  );
  const preflightChecks = React.useMemo(() => {
    const rawChecks = Array.isArray(bootstrapStatus?.checks) ? bootstrapStatus.checks : [];
    return rawChecks
      .filter((item) => item && typeof item === 'object')
      .map((item) => {
        const status = normalizeCheckStatus(item.status);
        return {
          ...item,
          status,
          label: t(`setup.preflight.checkLabels.${item.id}`, { defaultValue: item.id }),
          message: buildPreflightMessage(item, t),
          action: buildPreflightAction(item, t),
          detailsText: buildPreflightDetails(item, t),
        };
      });
  }, [bootstrapStatus?.checks, t]);
  const preflightSummary = React.useMemo(() => {
    const total = preflightChecks.length;
    const passing = preflightChecks.filter((item) => item.status === 'pass').length;
    const attention = preflightChecks.filter((item) => item.status === 'warn' || item.status === 'fail').length;
    return { total, passing, attention };
  }, [preflightChecks]);
  const localizedSubmitResult = React.useMemo(() => {
    if (!submitState.result) return null;
    const warnings = localizeSetupTextList(submitState.result.warnings, t);
    const actions = localizeSetupTextList(submitState.result.actions, t);
    const nextSteps = localizeSetupTextList(submitState.result.nextSteps, t);
    if (submitState.result.dashboardAuthStatus === 'manual_required') {
      const warning = t('setup.messages.manualDashboardAuthWarning');
      const nextStep = t('setup.messages.manualDashboardAuthNextStep');
      if (!warnings.includes(warning)) warnings.push(warning);
      if (!nextSteps.includes(nextStep)) nextSteps.push(nextStep);
    }
    return {
      ...submitState.result,
      summary: localizeSetupText(submitState.result.summary, t),
      warnings,
      actions,
      nextSteps,
    };
  }, [submitState.result, t]);
  const localizedRestartResultMessage = React.useMemo(
    () => localizeSetupText(restartState.result?.message, t),
    [restartState.result?.message, t]
  );
  const localizedValidation = React.useMemo(() => {
    const validation = submitState.result?.validation;
    if (!validation || typeof validation !== 'object') return null;
    const steps = Array.isArray(validation.steps) ? validation.steps : [];
    return {
      ...validation,
      steps: steps.map((step) => ({
        ...step,
        summary: localizeSetupText(step.summary, t),
      })),
    };
  }, [submitState.result?.validation, t]);
  const profileOptions = React.useMemo(
    () => getOptionList(bootstrapStatus?.profileOptions, DEFAULT_PROFILE_OPTIONS),
    [bootstrapStatus?.profileOptions]
  );
  const modeOptions = React.useMemo(
    () => getOptionList(bootstrapStatus?.modeOptions, DEFAULT_MODE_OPTIONS),
    [bootstrapStatus?.modeOptions]
  );
  const transportOptions = React.useMemo(
    () => getOptionList(bootstrapStatus?.transportOptions, DEFAULT_TRANSPORT_OPTIONS),
    [bootstrapStatus?.transportOptions]
  );
  const advancedFieldsVisible = ADVANCED_PROFILES.has(String(form.profile || '').toLowerCase());
  const providerProbe = React.useMemo(() => {
    const pendingProbe = probeState.result?.providerProbe;
    if (pendingProbe) {
      return pendingProbe;
    }
    const appliedProbe = submitState.result?.setup?.providerProbe;
    if (appliedProbe) {
      return appliedProbe;
    }
    const persistedProbe = setup.lastProviderProbe;
    if (
      persistedProbe
      && typeof persistedProbe === 'object'
      && String(persistedProbe.requestedProfile || '').trim().toLowerCase()
        === String(form.profile || '').trim().toLowerCase()
    ) {
      return persistedProbe;
    }
    return setup.providerProbe && typeof setup.providerProbe === 'object'
      ? setup.providerProbe
      : null;
  }, [form.profile, probeState.result, setup.lastProviderProbe, setup.providerProbe, submitState.result?.setup?.providerProbe]);
  const providerProbeStatus = React.useMemo(() => {
    if (!providerProbe || typeof providerProbe !== 'object') return 'unknown';
    const providers = providerProbe.providers && typeof providerProbe.providers === 'object'
      ? providerProbe.providers
      : {};
    const statuses = PROVIDER_COMPONENT_ORDER.map((component) =>
      normalizeProviderStatus(providers?.[component]?.status)
    );
    if (providerProbe.requiresProviders === false) return 'not_required';
    if (providerProbe.fallbackApplied) return 'fallback';
    if ((providerProbe.missingFields || []).length > 0 || statuses.includes('missing')) return 'missing';
    if (statuses.includes('fail')) return 'fail';
    if (statuses.length > 0 && statuses.every((status) => status === 'pass')) return 'pass';
    if (statuses.includes('not_checked')) return 'not_checked';
    return 'unknown';
  }, [providerProbe]);
  const providerProbeSummaryMessage = React.useMemo(
    () => localizeSetupText(providerProbe?.summaryMessage, t),
    [providerProbe?.summaryMessage, t]
  );
  const providerProbeItems = React.useMemo(() => {
    const providers = providerProbe?.providers && typeof providerProbe.providers === 'object'
      ? providerProbe.providers
      : {};
    return PROVIDER_COMPONENT_ORDER
      .map((component) => {
        const item = providers?.[component];
        if (!item || typeof item !== 'object') return null;
        return {
          component,
          status: normalizeProviderStatus(item.status),
          label: t(`setup.preflight.checkLabels.provider-${component}`, {
            defaultValue: component,
          }),
          detail: localizeSetupText(item.detail, t),
          baseUrl: typeof item.baseUrl === 'string' ? item.baseUrl : null,
          model: typeof item.model === 'string' ? item.model : null,
          missingFields: Array.isArray(item.missingFields) ? item.missingFields : [],
          detectedDim: typeof item.detectedDim === 'string' ? item.detectedDim : null,
        };
      })
      .filter(Boolean);
  }, [providerProbe?.providers, t]);
  const providerMissingSections = React.useMemo(() => {
    const fields = Array.isArray(providerProbe?.missingFields) ? providerProbe.missingFields : [];
    return Array.from(
      new Set(
        fields
          .map(resolveMissingFieldSection)
          .filter(Boolean)
      )
    );
  }, [providerProbe?.missingFields]);
  const providerNextSteps = React.useMemo(() => {
    const steps = [];
    for (const section of providerMissingSections) {
      steps.push(
        t('setup.providerReadiness.nextSteps.completeSection', {
          section: t(`setup.advanced.${section}`),
        })
      );
    }
    for (const item of providerProbeItems) {
      if (item.status === 'fail') {
        steps.push(
          t('setup.providerReadiness.nextSteps.retryProvider', {
            provider: item.label,
          })
        );
      }
    }
    if (providerProbe?.fallbackApplied) {
      steps.push(
        t('setup.providerReadiness.nextSteps.reapplyRequestedProfile', {
          profile: String(providerProbe.requestedProfile || form.profile || '').toUpperCase(),
        })
      );
    } else if (providerProbeStatus === 'pass') {
      steps.push(
        t('setup.providerReadiness.nextSteps.readyToApply', {
          profile: String(providerProbe.requestedProfile || form.profile || '').toUpperCase(),
        })
      );
    }
    return Array.from(new Set(steps.filter(Boolean)));
  }, [form.profile, providerMissingSections, providerProbe?.fallbackApplied, providerProbe?.requestedProfile, providerProbeItems, providerProbeStatus, t]);
  const activePresetId = React.useMemo(() => {
    const normalizedMode = String(form.mode || '').toLowerCase();
    const normalizedProfile = String(form.profile || '').toLowerCase();
    const normalizedTransport = String(form.transport || '').toLowerCase();
    const matched = SETUP_PRESETS.find((preset) =>
      preset.mode === normalizedMode
      && preset.profile === normalizedProfile
      && preset.transport === normalizedTransport
    );
    return matched?.id || null;
  }, [form.mode, form.profile, form.transport]);
  const wizardSteps = React.useMemo(() => {
    const basicDetailsReady =
      form.transport !== 'sse' || Boolean(String(form.sseUrl || '').trim());
    const providerReady = !advancedFieldsVisible
      || providerProbeStatus === 'pass'
      || providerProbeStatus === 'not_required';
    const appliedCurrentProfile = Boolean(
      submitState.result
      || (
        !setup.requiresOnboarding
        && String(setup.effectiveProfile || '').trim().toLowerCase()
          === String(form.profile || '').trim().toLowerCase()
        && String(setup.mode || '').trim().toLowerCase()
          === String(form.mode || '').trim().toLowerCase()
        && String(setup.transport || '').trim().toLowerCase()
          === String(form.transport || '').trim().toLowerCase()
      )
    );
    return [
      {
        id: 'path',
        title: t('setup.guided.steps.path.title'),
        description: t('setup.guided.steps.path.description'),
        done: Boolean(activePresetId),
      },
      {
        id: 'details',
        title: t('setup.guided.steps.details.title'),
        description: t('setup.guided.steps.details.description'),
        done: basicDetailsReady,
      },
      {
        id: 'providers',
        title: t('setup.guided.steps.providers.title'),
        description: advancedFieldsVisible
          ? t('setup.guided.steps.providers.descriptionAdvanced')
          : t('setup.guided.steps.providers.descriptionBasic'),
        done: providerReady,
      },
      {
        id: 'apply',
        title: t('setup.guided.steps.apply.title'),
        description: t('setup.guided.steps.apply.description'),
        done: appliedCurrentProfile,
      },
    ];
  }, [activePresetId, advancedFieldsVisible, form.mode, form.profile, form.sseUrl, form.transport, providerProbe, setup.effectiveProfile, setup.mode, setup.requiresOnboarding, setup.transport, submitState.result, t]);
  const guidedValidationCommand = React.useMemo(
    () => buildGuidedValidationCommand({
      configPath: setup.configPath || submitState.result?.setup?.configPath,
      envFile: setup.envFile || submitState.result?.setup?.envFile,
      form,
    }),
    [form, setup.configPath, setup.envFile, submitState.result?.setup?.configPath, submitState.result?.setup?.envFile]
  );
  const preferredAdvancedProfileActive = React.useMemo(
    () => ['c', 'd'].includes(String(form.profile || '').trim().toLowerCase()),
    [form.profile]
  );

  const handleValueChange = React.useCallback((event) => {
    const { name, value } = event.target;
    setForm((current) => ({ ...current, [name]: value }));
  }, []);

  const handleBooleanChange = React.useCallback((event) => {
    const { name, checked } = event.target;
    if (name === 'reconfigure') {
      pathSelectionDirtyRef.current = true;
    }
    setForm((current) => ({ ...current, [name]: checked }));
  }, []);

  const handleOptionChange = React.useCallback((name, nextValue) => {
    if (name === 'mode' || name === 'profile' || name === 'transport') {
      pathSelectionDirtyRef.current = true;
    }
    setForm((current) => ({ ...current, [name]: nextValue }));
  }, []);

  const handlePresetApply = React.useCallback((preset) => {
    pathSelectionDirtyRef.current = true;
    setForm((current) => ({
      ...current,
      mode: preset.mode,
      profile: preset.profile,
      transport: preset.transport,
      reconfigure: true,
    }));
    setProbeState({ loading: false, error: null, result: null });
  }, []);

  const handleRefresh = React.useCallback(async () => {
    if (!onRefreshStatus) return;
    pathSelectionDirtyRef.current = false;
    await onRefreshStatus();
  }, [onRefreshStatus]);

  const handleProviderProbe = React.useCallback(async () => {
    const normalizedEmbDim = normalizeEmbeddingDim(form.embeddingDim);
    if (form.embeddingDim !== '' && normalizedEmbDim.valid === false) {
      setProbeState({
        loading: false,
        error: t('setup.messages.invalidEmbeddingDim'),
        result: null,
      });
      return;
    }

    setProbeState({ loading: true, error: null, result: null });
    try {
      const result = await probeBootstrapProviders(
        buildProviderProbePayload(form, normalizedEmbDim)
      );
      setProbeState({ loading: false, error: null, result });
      await onRefreshStatus?.();
    } catch (error) {
      setProbeState({
        loading: false,
        error: extractApiError(error, t('setup.messages.providerProbeFailed')),
        result: null,
      });
    }
  }, [form, onRefreshStatus, t]);

  const runSubmit = React.useCallback(
    async (validateAfterApply = false) => {
      const normalizedEmbDim = normalizeEmbeddingDim(form.embeddingDim);
      if (normalizedEmbDim.valid === false) {
        setSubmitState({
          loading: false,
          error: t('setup.messages.invalidEmbeddingDim'),
          result: null,
          action: validateAfterApply ? 'validate' : 'apply',
        });
        return;
      }
      setSubmitState({
        loading: true,
        error: null,
        result: null,
        action: validateAfterApply ? 'validate' : 'apply',
      });
      setRestartState({ loading: false, error: null, result: null });
      setReindexState({ loading: false, error: null, done: false });
      try {
        const result = await applyBootstrapConfiguration({
          validate: validateAfterApply,
          ...buildBootstrapPayload(form, advancedFieldsVisible, normalizedEmbDim),
        });
        // The backend only echoes a masked maintenance key. Reuse the key we
        // just submitted, and otherwise surface that the browser still needs a
        // real key for protected routes.
        const sentKey = typeof form.mcpApiKey === 'string' ? form.mcpApiKey.trim() : '';
        const hasKnownDashboardAuth = Boolean(sentKey) || Boolean(getMaintenanceAuthState());
        if (sentKey) {
          saveStoredMaintenanceAuth(sentKey, result?.maintenanceApiKeyMode || 'header');
        }
        setSubmitState({
          loading: false,
          error: null,
          result: {
            ...result,
            dashboardAuthStatus:
              result?.maintenanceApiKeySet && !hasKnownDashboardAuth
                ? 'manual_required'
                : 'synced_or_not_required',
          },
          action: validateAfterApply ? 'validate' : 'apply',
        });
        clearSecretsOnNextBootstrapSyncRef.current = true;
        pathSelectionDirtyRef.current = false;
        if (onRefreshStatus) {
          await onRefreshStatus();
        }
        setForm((current) => clearSecretFields(current));
      } catch (error) {
        setSubmitState({
          loading: false,
          error: extractApiError(error, t('setup.messages.applyFailed')),
          result: null,
          action: validateAfterApply ? 'validate' : 'apply',
        });
      }
    },
    [advancedFieldsVisible, form, onRefreshStatus, t]
  );
  const handleSubmit = React.useCallback(
    async (event) => {
      event.preventDefault();
      await runSubmit(false);
    },
    [runSubmit]
  );
  const handleSubmitAndValidate = React.useCallback(async () => {
    await runSubmit(true);
  }, [runSubmit]);

  const handleRestart = React.useCallback(async () => {
    setRestartState({ loading: true, error: null, result: null });
    try {
      const result = await requestBootstrapRestart();
      setRestartState({ loading: false, error: null, result });
      window.setTimeout(() => {
        void onRefreshStatus?.();
      }, 2000);
    } catch (error) {
      setRestartState({
        loading: false,
        error: extractApiError(error, t('setup.messages.restartFailed')),
        result: null,
      });
    }
  }, [onRefreshStatus, t]);

  const handleReindex = React.useCallback(async () => {
    setReindexState({ loading: true, error: null, done: false });
    try {
      await triggerIndexRebuild({ reason: 'setup_reindex_gate', wait: true, timeout_seconds: 120 });
      setReindexState({ loading: false, error: null, done: true });
    } catch (error) {
      setReindexState({
        loading: false,
        error: extractApiError(error, 'Reindex failed'),
        done: false,
      });
    }
  }, []);

  const statusErrorMessage = statusError
    ? extractApiError(statusError, t('setup.messages.statusLoadFailed'))
    : null;
  const effectiveRestartRequired =
    Boolean(submitState.result?.restartRequired) || Boolean(setup.restartRequired);
  const restartSupported =
    submitState.result?.restartSupported
    ?? setup.restartSupported
    ?? false;
  // Fix D: Read reindexGate from both submit result AND bootstrap status
  // so the gate persists across page refreshes.
  const reindexGate = submitState.result?.reindexGate || setup.reindexGate || null;
  const reindexGateRequired = Boolean(reindexGate?.required) && !reindexState.done;
  const effectiveSubmitResult = localizedSubmitResult || (reindexGateRequired
    ? {
        summary: setup.summary || statusErrorMessage || '',
        effectiveProfile: setup.effectiveProfile || t('common.states.notAvailable'),
        fallbackApplied: false,
        restartRequired: effectiveRestartRequired,
        actions: [],
        nextSteps: [],
        warnings: [],
      }
    : null);
  const i18nLang = (t('locale_code', { defaultValue: '' }) || '').startsWith('zh') ? 'zh' : 'en';

  const contextValue = React.useMemo(() => ({
    // props passed through
    bootstrapStatus,
    statusLoading,
    statusError,
    onRefreshStatus,
    // form state
    form,
    setForm,
    // async states
    submitState,
    restartState,
    reindexState,
    probeState,
    // derived / memoized
    setup,
    embeddingDimValidationMessage,
    localizedStatusSummary,
    localizedSetupWarnings,
    preflightChecks,
    preflightSummary,
    localizedSubmitResult: effectiveSubmitResult,
    localizedRestartResultMessage,
    localizedValidation,
    profileOptions,
    modeOptions,
    transportOptions,
    advancedFieldsVisible,
    providerProbe,
    providerProbeStatus,
    providerProbeSummaryMessage,
    providerProbeItems,
    providerMissingSections,
    providerNextSteps,
    activePresetId,
    wizardSteps,
    guidedValidationCommand,
    preferredAdvancedProfileActive,
    statusErrorMessage,
    effectiveRestartRequired,
    restartSupported,
    reindexGate,
    reindexGateRequired,
    i18nLang,
    // handlers
    handleValueChange,
    handleBooleanChange,
    handleOptionChange,
    handlePresetApply,
    handleRefresh,
    handleProviderProbe,
    handleSubmit,
    handleSubmitAndValidate,
    handleRestart,
    handleReindex,
    // constants re-exported
    SETUP_PRESETS,
    t,
  }), [
    bootstrapStatus, statusLoading, statusError, onRefreshStatus,
    form, submitState, restartState, reindexState, probeState,
    setup, embeddingDimValidationMessage,
    localizedStatusSummary, localizedSetupWarnings,
    preflightChecks, preflightSummary,
    localizedSubmitResult, localizedRestartResultMessage, localizedValidation,
    profileOptions, modeOptions, transportOptions, advancedFieldsVisible,
    providerProbe, providerProbeStatus, providerProbeSummaryMessage,
    providerProbeItems, providerMissingSections, providerNextSteps,
    activePresetId, wizardSteps, guidedValidationCommand,
    preferredAdvancedProfileActive,
    statusErrorMessage, effectiveRestartRequired, restartSupported,
    reindexGate, reindexGateRequired, i18nLang,
    handleValueChange, handleBooleanChange, handleOptionChange,
    handlePresetApply, handleRefresh, handleProviderProbe,
    handleSubmit, handleSubmitAndValidate, handleRestart, handleReindex,
    t,
  ]);

  return React.createElement(SetupContext.Provider, { value: contextValue }, children);
}

export function useSetup() {
  const ctx = React.useContext(SetupContext);
  if (!ctx) {
    throw new Error('useSetup must be used within a <SetupProvider>');
  }
  return ctx;
}

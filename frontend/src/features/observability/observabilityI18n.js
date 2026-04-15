const EXACT_TEXT_KEYS = new Map([
  ['No usable transport is configured.', 'observability.runtimeText.noUsableTransport'],
  [
    'Stable user entry is `openclaw memory-palace ...`; host-owned `openclaw memory ...` remains a separate command surface.',
    'observability.runtimeText.stableEntry',
  ],
  ['Plugin-bundled OpenClaw skill is present.', 'observability.runtimeText.bundledSkillPresent'],
  [
    'Plugin-bundled OpenClaw skill directory is missing.',
    'observability.runtimeText.bundledSkillMissing',
  ],
  [
    'Visual auto-harvest hooks are enabled (message:preprocessed / before_prompt_build / agent_end).',
    'observability.runtimeText.visualAutoHarvestEnabled',
  ],
  ['Visual auto-harvest hooks are disabled by config.', 'observability.runtimeText.visualAutoHarvestDisabled'],
  [
    'Automatic durable recall before agent start is enabled.',
    'observability.runtimeText.autoRecallEnabled',
  ],
  [
    'Automatic durable recall before agent start is disabled by config.',
    'observability.runtimeText.autoRecallDisabled',
  ],
  [
    'Automatic durable capture after successful turns is enabled.',
    'observability.runtimeText.autoCaptureEnabled',
  ],
  [
    'Automatic durable capture after successful turns is disabled by config.',
    'observability.runtimeText.autoCaptureDisabled',
  ],
  ['Default stdio launcher is present.', 'observability.runtimeText.defaultStdioLauncherPresent'],
  ['Default stdio launcher is missing.', 'observability.runtimeText.defaultStdioLauncherMissing'],
  [
    'Configured runtime python for the stdio wrapper is present.',
    'observability.runtimeText.runtimePythonPresent',
  ],
  [
    'Configured runtime python for the stdio wrapper is missing.',
    'observability.runtimeText.runtimePythonMissing',
  ],
  ['Transport health check passed over stdio.', 'observability.runtimeText.transportHealthPassedStdio'],
  ['Transport health check failed.', 'observability.runtimeText.transportHealthFailed'],
  ['index_status responded successfully.', 'observability.runtimeText.indexStatusSuccess'],
  ['search_memory probe failed.', 'observability.runtimeText.searchProbeFailed'],
  [
    'search_memory probe succeeded but returned no hits.',
    'observability.runtimeText.searchProbeNoHits',
  ],
  ['No readable path was available for the smoke read probe.', 'observability.runtimeText.readProbeNoPath'],
  ['connecting', 'observability.runtimeText.eventConnecting'],
  ['connected', 'observability.runtimeText.eventConnected'],
  ['reused existing client', 'observability.runtimeText.eventReusedClient'],
  ['health check passed', 'observability.runtimeText.eventHealthCheckPassed'],
  ['tool call passed', 'observability.runtimeText.eventToolCallPassed'],
]);

const PATTERN_LOCALIZERS = [
  {
    pattern: /^Configured transport order: (.+)\.$/i,
    localize: (m, t) => t('observability.runtimeText.transportOrder', { order: m[1] }),
  },
  {
    pattern:
      /^Configured retry policy: (\d+) reconnect (retry|retries) \/ base backoff (\d+)ms \/ request retries (\d+)\.$/i,
    localize: (m, t) =>
      t('observability.runtimeText.retryPolicy', {
        reconnectRetries: m[1],
        backoffMs: m[3],
        requestRetries: m[4],
      }),
  },
  {
    pattern: /^Stdio command is configured: (.+)\.$/i,
    localize: (m, t) => t('observability.runtimeText.stdioCommandConfigured', { command: m[1] }),
  },
  {
    pattern: /^SSE endpoint is configured: (.+)\.$/i,
    localize: (m, t) => t('observability.runtimeText.sseEndpointConfigured', { url: m[1] }),
  },
  {
    pattern: /^SSE endpoint is not configured\.$/i,
    localize: (_m, t) => t('observability.runtimeText.sseEndpointMissing'),
  },
  {
    pattern: /^search_memory probe returned (\d+) hit\(s\)\.$/i,
    localize: (m, t) => t('observability.runtimeText.searchProbeHits', { count: m[1] }),
  },
  {
    pattern: /^search_memory probe returned (\d+) hit\(s\) with degraded retrieval\.$/i,
    localize: (m, t) =>
      t('observability.runtimeText.searchProbeHitsDegraded', { count: m[1] }),
  },
  {
    pattern: /^read_memory probe succeeded for (.+)\.$/i,
    localize: (m, t) => t('observability.runtimeText.readProbeSucceeded', { uri: m[1] }),
  },
  {
    pattern: /^read_memory probe reached (.+) but returned empty text\.$/i,
    localize: (m, t) => t('observability.runtimeText.readProbeEmpty', { uri: m[1] }),
  },
  {
    pattern: /^verify passed with (\d+) check\(s\)\.$/i,
    localize: (m, t) => t('observability.runtimeText.verifySummaryPass', { count: m[1] }),
  },
  {
    pattern: /^verify completed with warnings\.$/i,
    localize: (_m, t) => t('observability.runtimeText.verifySummaryWarn'),
  },
  {
    pattern: /^doctor completed with warnings\.$/i,
    localize: (_m, t) => t('observability.runtimeText.doctorSummaryWarn'),
  },
  {
    pattern: /^doctor passed with (\d+) check\(s\)\.$/i,
    localize: (m, t) => t('observability.runtimeText.doctorSummaryPass', { count: m[1] }),
  },
  {
    pattern: /^smoke completed with warnings\.$/i,
    localize: (_m, t) => t('observability.runtimeText.smokeSummaryWarn'),
  },
  {
    pattern: /^smoke passed with (\d+) check\(s\)\.$/i,
    localize: (m, t) => t('observability.runtimeText.smokeSummaryPass', { count: m[1] }),
  },
];

const STATUS_KEYS = new Map([
  ['pass', 'observability.runtimeStatus.pass'],
  ['warn', 'observability.runtimeStatus.warn'],
  ['fail', 'observability.runtimeStatus.fail'],
  ['ok', 'observability.runtimeStatus.ok'],
  ['running', 'observability.runtimeStatus.running'],
  ['queued', 'observability.runtimeStatus.queued'],
  ['cancelling', 'observability.runtimeStatus.cancelling'],
  ['cancelled', 'observability.runtimeStatus.cancelled'],
  ['succeeded', 'observability.runtimeStatus.succeeded'],
  ['failed', 'observability.runtimeStatus.failed'],
  ['dropped', 'observability.runtimeStatus.dropped'],
  ['stopped', 'observability.runtimeStatus.stopped'],
  ['unknown', 'observability.runtimeStatus.unknown'],
  ['connecting', 'observability.runtimeStatus.connecting'],
  ['connected', 'observability.runtimeStatus.connected'],
]);

export const localizeObservabilityText = (value, t) => {
  if (typeof value !== 'string') return value;
  const normalized = value.trim();
  if (!normalized) return value;

  const exactKey = EXACT_TEXT_KEYS.get(normalized);
  if (exactKey) return t(exactKey);

  for (const { pattern, localize } of PATTERN_LOCALIZERS) {
    const matches = pattern.exec(normalized);
    if (matches) return localize(matches, t);
  }

  return value;
};

/**
 * Default i18n translations for the aggregated health panel (obs.health.*).
 * These are exported so they can be merged into the project locale bundles.
 * Until then, the component uses t(key, { defaultValue }) to work standalone.
 */
export const HEALTH_PANEL_I18N = {
  en: {
    'obs.health.title': 'System Health Overview',
    'obs.health.healthy': 'All systems operational',
    'obs.health.degraded': 'Partial degradation detected',
    'obs.health.severe': 'Severe degradation — action required',
    'obs.health.reason': 'Degradation Reasons',
    'obs.health.noReasons': 'No specific degradation reasons reported.',
    'obs.health.reindex_needed': 'Mixed embedding dimensions detected — reindex required',
    'obs.health.action_reindex': 'Reindex All Memories',
    'obs.health.reindexing': 'Reindexing...',
    'obs.health.fix_hint_prefix': 'Suggested fix:',
    'obs.health.detected_dims': 'Detected dimensions: {{dims}}',
    'obs.health.block_reason': 'Vector block reason: {{reason}}',
    'obs.health.top_degrade_reasons': 'Top Degrade Reasons (recent queries)',
  },
  'zh-CN': {
    'obs.health.title': '系统健康总览',
    'obs.health.healthy': '所有系统运行正常',
    'obs.health.degraded': '检测到部分降级',
    'obs.health.severe': '严重降级 — 需要采取措施',
    'obs.health.reason': '降级原因',
    'obs.health.noReasons': '无特定降级原因。',
    'obs.health.reindex_needed': '检测到混合嵌入维度 — 需要重建索引',
    'obs.health.action_reindex': '一键 Reindex',
    'obs.health.reindexing': '正在重建索引...',
    'obs.health.fix_hint_prefix': '建议修复：',
    'obs.health.detected_dims': '检测到的维度：{{dims}}',
    'obs.health.block_reason': '向量阻断原因：{{reason}}',
    'obs.health.top_degrade_reasons': '高频降级原因（近期查询）',
  },
};

/**
 * Mapping from common degrade_reason codes to human-readable fix hints.
 */
export const DEGRADE_REASON_FIX_HINTS = {
  en: {
    embedding_dim_mismatch_requires_reindex:
      'Run a full reindex to realign vector dimensions.',
    mixed_embedding_dimensions:
      'Run a full reindex to unify embedding dimensions across all memories.',
    empty_query: 'Provide a non-empty search query.',
    query_preprocess_failed:
      'Check backend logs for query preprocessing errors.',
    query_preprocess_unavailable:
      'Query preprocessing is not available in this profile.',
    intent_classification_failed:
      'Intent classification encountered an error; check backend LLM connectivity.',
    intent_classification_unavailable:
      'Intent classification is not supported in this profile.',
    intent_llm_unavailable:
      'LLM-based intent classification is not available.',
    session_cache_lookup_failed:
      'Session cache lookup failed; check backend session state.',
    mode_not_supported_by_search_api:
      'The selected search mode is not supported by the current search API version.',
    intent_profile_not_supported:
      'Intent profile is not supported by the current search API version.',
    candidate_multiplier_not_supported_by_search_api:
      'Candidate multiplier is not supported by the current search API version.',
  },
  'zh-CN': {
    embedding_dim_mismatch_requires_reindex:
      '执行完整 Reindex 以对齐向量维度。',
    mixed_embedding_dimensions:
      '执行完整 Reindex 以统一所有记忆的嵌入维度。',
    empty_query: '请提供非空的搜索查询。',
    query_preprocess_failed:
      '检查后端日志中的查询预处理错误。',
    query_preprocess_unavailable:
      '当前 Profile 不支持查询预处理。',
    intent_classification_failed:
      '意图分类遇到错误，请检查后端 LLM 连接。',
    intent_classification_unavailable:
      '当前 Profile 不支持意图分类。',
    intent_llm_unavailable:
      '基于 LLM 的意图分类不可用。',
    session_cache_lookup_failed:
      '会话缓存查找失败，请检查后端会话状态。',
    mode_not_supported_by_search_api:
      '当前搜索 API 版本不支持所选搜索模式。',
    intent_profile_not_supported:
      '当前搜索 API 版本不支持意图配置。',
    candidate_multiplier_not_supported_by_search_api:
      '当前搜索 API 版本不支持候选乘数。',
  },
};

export const getHealthI18nValue = (key, lng, interpolations = {}) => {
  const lang = lng && HEALTH_PANEL_I18N[lng] ? lng : 'en';
  let value = HEALTH_PANEL_I18N[lang]?.[key] || HEALTH_PANEL_I18N.en?.[key] || key;
  for (const [k, v] of Object.entries(interpolations)) {
    value = value.replace(new RegExp(`\\{\\{${k}\\}\\}`, 'g'), String(v));
  }
  return value;
};

export const getDegradeReasonHint = (reason, lng) => {
  const lang = lng && DEGRADE_REASON_FIX_HINTS[lng] ? lng : 'en';
  return DEGRADE_REASON_FIX_HINTS[lang]?.[reason] || DEGRADE_REASON_FIX_HINTS.en?.[reason] || '';
};

export const localizeObservabilityStatus = (value, t) => {
  if (typeof value !== 'string') return value;
  const normalized = value.trim().toLowerCase();
  if (!normalized) return value;
  const key = STATUS_KEYS.get(normalized);
  return key ? t(key) : value;
};

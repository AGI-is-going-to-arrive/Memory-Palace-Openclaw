const EXACT_TEXT_KEYS = new Map([
  ['Bootstrap configuration is ready.', 'setup.runtimeText.bootstrapReady'],
  ['Bootstrap configuration is not initialized yet.', 'setup.runtimeText.bootstrapNotInitialized'],
  [
    '当前进程尚未加载 bootstrap env 文件中的最新运行参数。',
    'setup.runtimeText.processNotLoadedLatestEnv',
  ],
  [
    'The current process has not loaded the latest runtime parameters from the bootstrap env file.',
    'setup.runtimeText.processNotLoadedLatestEnv',
  ],
  [
    '当前运行中的后端环境与刚写入的 bootstrap 配置不一致；需要重启相关进程后才会完全按新配置运行。',
    'setup.runtimeText.restartRequiredForLatestConfig',
  ],
  [
    'MCP_API_KEY 未提供，已为当前本机安装自动生成本地 key。',
    'setup.runtimeText.generatedLocalApiKey',
  ],
  [
    '当前 SSE 地址不是本机 loopback；已按显式确认生成远程场景 MCP_API_KEY。',
    'setup.runtimeText.generatedRemoteApiKey',
  ],
  ['当前 transport 为 SSE，但尚未配置 SSE URL。', 'setup.runtimeText.sseUrlMissing'],
  [
    '当前安装模式请求 dashboard，但发布包中未找到 frontend 目录。',
    'setup.runtimeText.dashboardFrontendMissing',
  ],
  [
    '当前安装模式请求 dashboard，但 PATH 中未找到 npm。',
    'setup.runtimeText.dashboardNpmMissing',
  ],
  [
    'dashboard 依赖安装失败；基础安装已完成，但 full/dev 页面暂不可用。',
    'setup.runtimeText.dashboardDepsInstallFailed',
  ],
  [
    'dashboard 依赖已安装，但启动超时；基础安装已完成，可稍后用 next step 手动检查前端日志。',
    'setup.runtimeText.dashboardStartTimeout',
  ],
  [
    '当前安装模式请求 dashboard/full stack，但发布包中未找到 backend 目录。',
    'setup.runtimeText.fullStackBackendMissing',
  ],
  [
    '当前安装模式请求 dashboard/full stack，但 runtime Python 还未就绪。',
    'setup.runtimeText.runtimePythonMissing',
  ],
  [
    '当前安装模式请求 dashboard/full stack，但 bootstrap env 文件还不存在；请先完成 setup。',
    'setup.runtimeText.bootstrapEnvMissing',
  ],
  [
    'backend HTTP API 启动超时；full/dev 只完成了基础安装，dashboard stack 尚未完全可用。',
    'setup.runtimeText.backendStartTimeout',
  ],
  ['backend HTTP API 未就绪，已跳过 dashboard 启动。', 'setup.runtimeText.dashboardSkippedBackendNotReady'],
  [
    'dashboard 进程已发送停止信号，但端口尚未释放；请稍后再查状态。',
    'setup.runtimeText.dashboardStopPending',
  ],
  [
    'dashboard 当前可访问，但不是由本工具管理的进程；未执行停止。',
    'setup.runtimeText.dashboardExternallyManaged',
  ],
  [
    'backend API 进程已发送停止信号，但端口尚未释放；请稍后再查状态。',
    'setup.runtimeText.backendStopPending',
  ],
  [
    'backend HTTP API 当前可访问，但不是由本工具管理的进程；未执行停止。',
    'setup.runtimeText.backendExternallyManaged',
  ],
  [
    '未检测到 openclaw，可继续做本地配置和 runtime 清理，但不会卸载插件文件。',
    'setup.runtimeText.openclawUnavailableForCleanup',
  ],
  ['Local backend restart scheduled.', 'setup.runtimeText.localBackendRestartScheduled'],
  ['ensured plugins.allow contains memory-palace', 'setup.runtimeText.actions.ensurePluginsAllow'],
  [
    'ensured plugins.load.paths contains plugin install root',
    'setup.runtimeText.actions.ensurePluginsLoadPath',
  ],
  [
    'ensured plugins.entries.memory-palace exists',
    'setup.runtimeText.actions.ensurePluginsEntry',
  ],
  ['set plugins.slots.memory to memory-palace', 'setup.runtimeText.actions.setMemorySlot'],
  [
    'installed backend requirements into runtime venv',
    'setup.runtimeText.actions.installedBackendRequirements',
  ],
  ['reused existing dashboard dependencies', 'setup.runtimeText.actions.reusedDashboardDependencies'],
]);

const PATTERN_LOCALIZERS = [
  {
    pattern: /^缺失的 C\/D 字段:\s*(.+)$/u,
    localize: (matches, t) =>
      t('setup.runtimeText.missingProfileFields', { fields: matches[1] }),
  },
  {
    pattern: /^Profile ([A-D]) 所需模型配置不完整，当前已自动回退到 Profile B。$/u,
    localize: (matches, t) =>
      t('setup.runtimeText.profileFallback', { profile: matches[1] }),
  },
  {
    pattern: /^上次请求的是 Profile ([A-D])，但实际回退到了 Profile B。$/u,
    localize: (matches, t) =>
      t('setup.runtimeText.previousProfileFallback', { profile: matches[1] }),
  },
  {
    pattern:
      /^dashboard 端口 (\d+) 已被其他服务占用，未自动启动新的 dashboard 进程。\s*可改用 `--dashboard-port (\d+)`。$/u,
    localize: (matches, t) =>
      t('setup.runtimeText.dashboardPortInUseWithSuggestion', {
        port: matches[1],
        suggestedPort: matches[2],
      }),
  },
  {
    pattern: /^dashboard 端口 (\d+) 已被其他服务占用，未自动启动新的 dashboard 进程。$/u,
    localize: (matches, t) =>
      t('setup.runtimeText.dashboardPortInUse', { port: matches[1] }),
  },
  {
    pattern:
      /^backend HTTP API 端口 (\d+) 已被其他服务占用，未自动启动新的 backend API 进程。\s*可改用 `--backend-api-port (\d+)`。$/u,
    localize: (matches, t) =>
      t('setup.runtimeText.backendPortInUseWithSuggestion', {
        port: matches[1],
        suggestedPort: matches[2],
      }),
  },
  {
    pattern: /^backend HTTP API 端口 (\d+) 已被其他服务占用，未自动启动新的 backend API 进程。$/u,
    localize: (matches, t) =>
      t('setup.runtimeText.backendPortInUse', { port: matches[1] }),
  },
  {
    pattern: /^Setup completed for mode=(\w+), requested profile=([A-Z]), effective profile=([A-Z])\.$/i,
    localize: (matches, t) =>
      t('setup.runtimeText.setupCompleted', {
        mode: matches[1],
        requestedProfile: matches[2],
        effectiveProfile: matches[3],
      }),
  },
  {
    pattern: /^created runtime venv at (.+)$/i,
    localize: (matches, t) =>
      t('setup.runtimeText.actions.createdRuntimeVenv', { path: matches[1] }),
  },
  {
    pattern: /^started backend HTTP API at (.+)$/i,
    localize: (matches, t) =>
      t('setup.runtimeText.actions.startedBackendApi', { url: matches[1] }),
  },
  {
    pattern: /^backend HTTP API already reachable at (.+)$/i,
    localize: (matches, t) =>
      t('setup.runtimeText.actions.backendAlreadyReachable', { url: matches[1] }),
  },
  {
    pattern: /^dashboard already reachable at (.+)$/i,
    localize: (matches, t) =>
      t('setup.runtimeText.actions.dashboardAlreadyReachable', { url: matches[1] }),
  },
  {
    pattern: /^started dashboard dev server at (.+)$/i,
    localize: (matches, t) =>
      t('setup.runtimeText.actions.startedDashboard', { url: matches[1] }),
  },
  {
    pattern: /^reused hinted plugin install root (.+)$/i,
    localize: (matches, t) =>
      t('setup.runtimeText.actions.reusedHintedPluginRoot', { path: matches[1] }),
  },
  {
    pattern: /^installed plugin from current package path (.+)$/i,
    localize: (matches, t) =>
      t('setup.runtimeText.actions.installedPluginFromCurrentPackage', {
        path: matches[1],
      }),
  },
  {
    pattern: /^Open dashboard:\s+(.+)$/i,
    localize: (matches, t) => t('setup.runtimeText.openDashboard', { url: matches[1] }),
  },
  {
    pattern: /^Export your SSE API key env before running the commands above\.$/i,
    localize: (_matches, t) => t('setup.runtimeText.exportSseApiKeyHint'),
  },
  {
    pattern:
      /^To switch back to the requested C\/D profile, re-run setup with the missing model fields populated\.$/i,
    localize: (_matches, t) => t('setup.runtimeText.switchBackToRequestedProfile'),
  },
];

const REINDEX_GATE_REASON_LABELS = {
  embedding_dim_changed: {
    en: 'Embedding dimension changed',
    zh: 'Embedding 维度已变更',
  },
  embedding_backend_changed: {
    en: 'Embedding backend changed',
    zh: 'Embedding 后端已变更',
  },
  reranker_toggled: {
    en: 'Reranker status changed',
    zh: 'Reranker 状态已变更',
  },
  embedding_model_changed: {
    en: 'Embedding model changed',
    zh: 'Embedding 模型已变更',
  },
  embedding_provider_changed: {
    en: 'Embedding provider changed',
    zh: 'Embedding 提供方已变更',
  },
  reranker_provider_changed: {
    en: 'Reranker provider changed',
    zh: 'Reranker 提供方已变更',
  },
  reranker_model_changed: {
    en: 'Reranker model changed',
    zh: 'Reranker 模型已变更',
  },
  search_mode_changed: {
    en: 'Search mode changed',
    zh: '搜索模式已变更',
  },
  vector_engine_changed: {
    en: 'Vector engine changed',
    zh: '向量引擎已变更',
  },
};

const REINDEX_GATE_STRINGS = {
  en: {
    title: 'Reindex Required',
    description:
      'Retrieval configuration changed. Existing vector data must be rebuilt to match the new settings.',
    action: 'Reindex Now',
    inProgress: 'Reindexing\u2026',
    complete: 'Reindex complete',
    reasons: 'Changed settings:',
  },
  zh: {
    title: '需要重新索引',
    description: '检索配置已变更，需要重建现有向量数据以匹配新设置。',
    action: '一键重新索引',
    inProgress: '正在重新索引\u2026',
    complete: '重新索引完成',
    reasons: '变更项：',
  },
};

/**
 * Resolve a reindex-gate UI string.
 * @param {string} key – one of: title, description, action, inProgress, complete, reasons
 * @param {string} [lang] – "en" | "zh"; defaults to "en"
 */
export const reindexGateString = (key, lang) => {
  const bucket = REINDEX_GATE_STRINGS[lang] || REINDEX_GATE_STRINGS.en;
  return bucket[key] || REINDEX_GATE_STRINGS.en[key] || key;
};

/**
 * Resolve the human-readable label for a single reindex reason key.
 * @param {string} reasonKey
 * @param {string} [lang]
 */
export const reindexGateReasonLabel = (reasonKey, lang) => {
  const entry = REINDEX_GATE_REASON_LABELS[reasonKey];
  if (!entry) return reasonKey;
  return entry[lang] || entry.en;
};

export const localizeSetupText = (value, t) => {
  if (typeof value !== 'string') return value;
  const normalized = value.trim();
  if (!normalized) return value;

  const exactKey = EXACT_TEXT_KEYS.get(normalized);
  if (exactKey) {
    return t(exactKey);
  }

  for (const { pattern, localize } of PATTERN_LOCALIZERS) {
    const matches = pattern.exec(normalized);
    if (!matches) continue;
    return localize(matches, t);
  }

  return value;
};

export const localizeSetupTextList = (items, t) =>
  Array.isArray(items) ? items.map((item) => localizeSetupText(item, t)) : [];

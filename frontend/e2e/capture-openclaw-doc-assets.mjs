import { existsSync } from 'node:fs';
import { mkdir, readFile, rename, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawn } from 'node:child_process';
import { chromium } from 'playwright';

const frontendRoot = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(frontendRoot, '..', '..');
const baseUrl = process.env.OPENCLAW_DOC_CAPTURE_BASE_URL || 'http://127.0.0.1:15184';
const apiKey = process.env.OPENCLAW_DOC_CAPTURE_API_KEY || '';
const defaultOutputDir = path.join(repoRoot, '.tmp', 'openclaw-doc-assets');
const outputDir = process.env.OPENCLAW_DOC_CAPTURE_OUTPUT_DIR
  || defaultOutputDir;
const visualAssetsDir = process.env.OPENCLAW_DOC_CAPTURE_VISUAL_ASSETS_DIR
  || outputDir;
const manifestOutputDir = path.relative(repoRoot, outputDir) || '.';
const viewport = { width: 1600, height: 1000 };
const recordSize = { width: 1440, height: 900 };
const videoFileName = 'openclaw-real-walkthrough.webm';
const requestedLocale = String(process.env.OPENCLAW_DOC_CAPTURE_LOCALE || '').trim();
const captureLocale = requestedLocale === 'zh' ? 'zh-CN' : (requestedLocale || 'en');
const localeSuffix =
  String(process.env.OPENCLAW_DOC_CAPTURE_LOCALE_SUFFIX || '').trim()
  || (captureLocale === 'zh-CN' ? 'zh' : 'en');
const shouldIncludeExtendedCaptures =
  String(process.env.OPENCLAW_DOC_CAPTURE_INCLUDE_EXTENDED || '').trim().toLowerCase() === 'true';
const shouldRecordVideo =
  String(process.env.OPENCLAW_DOC_CAPTURE_RECORD_VIDEO || '').trim().toLowerCase() === 'true';
const shouldWriteFixtures =
  String(process.env.OPENCLAW_DOC_CAPTURE_ALLOW_FIXTURE_WRITE || '').trim().toLowerCase() === 'true';
const captureSetupRoot = process.env.OPENCLAW_DOC_CAPTURE_SETUP_ROOT || '';
const runtimeEnvFile = process.env.OPENCLAW_DOC_CAPTURE_RUNTIME_ENV_FILE
  || (captureSetupRoot ? path.join(captureSetupRoot, 'runtime.env') : '');
const runtimePython = process.env.OPENCLAW_DOC_CAPTURE_RUNTIME_PYTHON
  || (captureSetupRoot ? path.join(captureSetupRoot, 'runtime', 'bin', 'python') : '');

const localizeFileName = (fileName) => {
  if (!localeSuffix) return fileName;
  return fileName.replace(/(\.[^.]+)$/u, `.${localeSuffix}$1`);
};

const captures = [
  {
    file: localizeFileName('dashboard-setup-page.png'),
    route: '/setup',
    waitForTexts: ['Bootstrap Setup', '引导设置'],
    fullPage: false,
  },
  {
    file: localizeFileName('dashboard-memory-page.png'),
    route: '/memory?domain=core',
    waitForTexts: ['Root Memory Hall', '根记忆大厅'],
    fullPage: false,
  },
  {
    file: localizeFileName('dashboard-review-page.png'),
    route: '/review',
    waitForTexts: ['Review Ledger', '审查账本'],
    fullPage: false,
    pauseMs: 1400,
  },
  {
    file: localizeFileName('dashboard-maintenance-page.png'),
    route: '/maintenance',
    waitForTexts: ['Maintenance Console', '维护控制台'],
    fullPage: false,
    pauseMs: 1400,
  },
  {
    file: localizeFileName('dashboard-observability-page.png'),
    route: '/observability',
    waitForTexts: ['Retrieval Observability Console', '检索观测控制台'],
    fullPage: false,
    pauseMs: 1400,
  },
  {
    file: localizeFileName('dashboard-visual-memory-root.png'),
    route: '/memory?domain=core',
    waitForTexts: ['Root Memory Hall', '根记忆大厅'],
    fullPage: false,
    outputDir: visualAssetsDir,
  },
  {
    file: localizeFileName('dashboard-visual-memory.png'),
    route: '/memory?domain=core&path=visual/2026/03/27/sha256-700e35275a23',
    waitForTexts: ['Visual Memory', '视觉记忆'],
    fullPage: false,
    outputDir: visualAssetsDir,
  },
  ...(shouldIncludeExtendedCaptures
    ? [
        {
          file: localizeFileName('dashboard-response-style.png'),
          route: '/memory?domain=core&path=response-style',
          waitForTexts: ['response-style', 'response-style'],
          fullPage: false,
        },
        {
          file: localizeFileName('dashboard-observability-transport.png'),
          route: '/observability',
          waitForTexts: ['Transport Diagnostics', '传输诊断'],
          scrollToTexts: ['Transport Diagnostics', '传输诊断'],
          fullPage: false,
          pauseMs: 1400,
        },
      ]
    : []),
];

const runtimeAuthPayload = apiKey
  ? JSON.stringify({
      maintenanceApiKey: apiKey,
      maintenanceApiKeyMode: 'header',
    })
  : '';

const resolveRouteUrl = (route, { preserveHash = true } = {}) => {
  const parsedBase = new URL(baseUrl);
  const basePath = parsedBase.pathname.endsWith('/')
    ? parsedBase.pathname
    : `${parsedBase.pathname}/`;
  const normalizedRoute = route.startsWith('/') ? `.${route}` : route;
  const resolved = new URL(normalizedRoute, `${parsedBase.origin}${basePath}`);
  if (preserveHash && parsedBase.hash) {
    resolved.hash = parsedBase.hash;
  }
  return resolved.toString();
};

const run = (command, args, options = {}) =>
  new Promise((resolve, reject) => {
    const child = spawn(command, args, {
      cwd: options.cwd ?? repoRoot,
      env: {
        ...process.env,
        ...options.env,
      },
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    let stdout = '';
    let stderr = '';
    child.stdout.on('data', (chunk) => {
      stdout += chunk.toString();
    });
    child.stderr.on('data', (chunk) => {
      stderr += chunk.toString();
    });
    child.on('close', (code) => {
      if ((code ?? 0) === 0) {
        resolve({stdout, stderr});
        return;
      }
      reject(new Error(`${command} ${args.join(' ')} failed with code ${code}\n${stderr}`));
    });
  });

const attemptDirectRuntimeSeed = async (seed) => {
  if (!runtimeEnvFile || !runtimePython || !existsSync(runtimePython)) {
    return { attempted: false, error: null };
  }
  const runtimeEnv = {};
  try {
    const raw = await readFile(runtimeEnvFile, 'utf8');
    for (const line of raw.split(/\r?\n/)) {
      const trimmed = line.trim();
      if (!trimmed || trimmed.startsWith('#')) continue;
      const index = trimmed.indexOf('=');
      if (index <= 0) continue;
      const key = trimmed.slice(0, index).trim();
      const value = trimmed.slice(index + 1);
      if (key) {
        runtimeEnv[key] = value;
      }
    }
  } catch (error) {
    return { attempted: true, error: `Could not read runtime env file ${runtimeEnvFile}: ${String(error?.message || error)}` };
  }

  const payload = JSON.stringify({
    path: seed.path,
    content: seed.content,
    disclosure: 'Dashboard capture fixture',
    domain: 'core',
  });
  const inlineScript = [
    'import asyncio, json, sys',
    'from db.sqlite_client import get_sqlite_client, close_sqlite_client',
    'payload = json.loads(sys.argv[1])',
    'async def main():',
    '    client = get_sqlite_client()',
    '    existing = await client.get_memory_by_path(payload["path"], payload["domain"], reinforce_access=False)',
    '    if existing:',
    '        await close_sqlite_client()',
    '        return',
    '    parent_path = payload["path"].rsplit("/", 1)[0] if "/" in payload["path"] else ""',
    '    title = payload["path"].rsplit("/", 1)[1] if "/" in payload["path"] else payload["path"]',
    '    await client.create_memory(',
    '        parent_path=parent_path,',
    '        content=payload["content"],',
    '        priority=1,',
    '        title=title,',
    '        disclosure=payload["disclosure"],',
    '        domain=payload["domain"],',
    '    )',
    '    await close_sqlite_client()',
    'asyncio.run(main())',
  ].join('\n');

  try {
    await run(runtimePython, ['-c', inlineScript, payload], {
      cwd: repoRoot,
      env: {
        ...process.env,
        ...runtimeEnv,
        OPENCLAW_MEMORY_PALACE_ENV_FILE: runtimeEnvFile,
        PYTHONPATH: path.join(repoRoot, 'backend'),
      },
      timeoutMs: 120_000,
    });
    return { attempted: true, error: null };
  } catch (error) {
    return { attempted: true, error: String(error?.message || error) };
  }
};

const authHeaders = apiKey ? {'X-MCP-API-Key': apiKey} : {};

const fetchJson = async (url, options = {}) => {
  const response = await fetch(url, options);
  const text = await response.text();
  let payload = null;
  try {
    payload = text ? JSON.parse(text) : null;
  } catch {
    payload = text;
  }
  return {response, payload};
};

const isPathMissingResponse = (payload) => {
  const detail = typeof payload?.detail === 'string'
    ? payload.detail
    : typeof payload === 'string'
      ? payload
      : '';
  return detail.startsWith('Path not found:');
};

const buildBrowseCandidates = () => {
  return [
    resolveRouteUrl('/api/browse/node', { preserveHash: false }),
    resolveRouteUrl('/browse/node', { preserveHash: false }),
  ].filter((value, index, array) => array.indexOf(value) === index);
};

const resolveBrowseUrl = async () => {
  const failures = [];
  for (const candidate of buildBrowseCandidates()) {
    const probeUrl = new URL(candidate);
    probeUrl.searchParams.set('domain', 'core');
    const probe = await fetchJson(probeUrl, {
      headers: authHeaders,
    });
    if (probe.response.ok) {
      return candidate;
    }
    failures.push({
      candidate,
      status: probe.response.status,
      payload: probe.payload,
    });
  }
  throw new Error(`Could not resolve dashboard browse endpoint: ${JSON.stringify(failures)}`);
};

const seedDashboardCaptureData = async () => {
  if (!apiKey || !shouldWriteFixtures) {
    return;
  }
  const backendBrowseUrl = await resolveBrowseUrl();
  const seeds = [
    {
      path: 'response-style',
      content: 'Dashboard capture fixture for core://response-style.\n\nPrefer structured answers, factual boundaries, and explicit caveats.',
    },
    {
      path: 'visual',
      content: 'Dashboard capture fixture for core://visual.\n\nVisual memory namespace for the dashboard capture flow.',
    },
    {
      path: 'visual/2026',
      content: 'Dashboard capture fixture for core://visual/2026.\n\nVisual memory year bucket for the dashboard capture flow.',
    },
    {
      path: 'visual/2026/03',
      content: 'Dashboard capture fixture for core://visual/2026/03.\n\nVisual memory month bucket for the dashboard capture flow.',
    },
    {
      path: 'visual/2026/03/27',
      content: 'Dashboard capture fixture for core://visual/2026/03/27.\n\nVisual memory day bucket for the dashboard capture flow.',
    },
    {
      path: 'visual/2026/03/27/sha256-700e35275a23',
      content: 'Dashboard capture fixture for core://visual/2026/03/27/sha256-700e35275a23.\n\nVisual Memory\n\nSummary: whiteboard launch plan\nOCR: launch checklist\nEntities: Alice, whiteboard',
    },
  ];

  for (const seed of seeds) {
    const getUrl = new URL(backendBrowseUrl);
    getUrl.searchParams.set('domain', 'core');
    getUrl.searchParams.set('path', seed.path);
    const existing = await fetchJson(getUrl, {
      headers: authHeaders,
    });
    if (existing.response.ok) {
      continue;
    }
    if (!isPathMissingResponse(existing.payload)) {
      throw new Error(
        `Dashboard browse endpoint did not behave like a node-missing read for ${seed.path}: ${JSON.stringify({
          status: existing.response.status,
          payload: existing.payload,
          url: getUrl.toString(),
        })}`,
      );
    }
    const parentPath = seed.path.includes('/') ? seed.path.slice(0, seed.path.lastIndexOf('/')) : '';
    const title = seed.path.includes('/') ? seed.path.slice(seed.path.lastIndexOf('/') + 1) : seed.path;
    const created = await fetchJson(backendBrowseUrl, {
      method: 'POST',
      headers: {
        ...authHeaders,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        parent_path: parentPath,
        title,
        content: seed.content,
        priority: 1,
        disclosure: 'Dashboard capture fixture',
        domain: 'core',
      }),
    });
    if (!created.response.ok) {
      throw new Error(`Failed to seed ${seed.path}: ${JSON.stringify(created.payload)}`);
    }
    let verified = await fetchJson(getUrl, {
      headers: authHeaders,
    });
    const needsRuntimeFallback =
      !verified.response.ok
      || created.payload?.success === false
      || created.payload?.created === false;
    let runtimeFallback = { attempted: false, error: null };
    if (needsRuntimeFallback) {
      runtimeFallback = await attemptDirectRuntimeSeed(seed);
      if (runtimeFallback.attempted) {
        verified = await fetchJson(getUrl, {
          headers: authHeaders,
        });
      }
    }
    if (!verified.response.ok) {
      throw new Error(
        `Seed write did not materialize ${seed.path}: ${JSON.stringify({
          create: created.payload,
          verifyStatus: verified.response.status,
          verifyPayload: verified.payload,
          runtimeFallback,
        })}`,
      );
    }
  }
};

const absoluteUrl = (route) => resolveRouteUrl(route);

const waitForOneOfTexts = async (page, texts, timeoutMs) => {
  const expectedTexts = (texts || []).filter(Boolean);
  if (!expectedTexts.length) return;

  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    for (const text of expectedTexts) {
      const locator = page.getByText(text, {exact: false}).first();
      if (await locator.isVisible().catch(() => false)) {
        return text;
      }
    }
    await page.waitForTimeout(300);
  }

  throw new Error(`Timed out waiting for any text: ${expectedTexts.join(' | ')}`);
};

const waitForStablePage = async (page, capture) => {
  await page.waitForLoadState('networkidle').catch(() => {});
  if (capture.waitForTexts?.length) {
    try {
      await waitForOneOfTexts(page, capture.waitForTexts, capture.waitTimeoutMs ?? 20_000);
    } catch (error) {
      console.warn(`[doc-capture] fallback without matched headline for ${capture.route}: ${error instanceof Error ? error.message : String(error)}`);
    }
  }
  await page.waitForTimeout(capture.pauseMs ?? 900);
};

await mkdir(outputDir, { recursive: true });
await mkdir(visualAssetsDir, { recursive: true });
await seedDashboardCaptureData();

const browser = await chromium.launch({ headless: true });
const context = await browser.newContext({
  viewport,
  ...(shouldRecordVideo
    ? {
        recordVideo: {
          dir: outputDir,
          size: recordSize,
        },
      }
    : {}),
});

await context.addInitScript(({ serializedAuth, locale }) => {
  if (typeof window === 'undefined') return;
  try {
    if (locale) {
      window.localStorage.setItem('memory-palace.locale', locale);
    }
    if (serializedAuth) {
      const parsed = JSON.parse(serializedAuth);
      window.__MEMORY_PALACE_RUNTIME__ = {
        ...(window.__MEMORY_PALACE_RUNTIME__ || {}),
        ...parsed,
      };
      window.__MCP_RUNTIME_CONFIG__ = {
        ...(window.__MCP_RUNTIME_CONFIG__ || {}),
        ...parsed,
      };
      window.sessionStorage.setItem('memory-palace.dashboardAuth', serializedAuth);
    }
  } catch {
    // Ignore storage bootstrap failures and let the page prompt if needed.
  }
}, { serializedAuth: runtimeAuthPayload, locale: captureLocale });

const page = await context.newPage();
const video = page.video();

page.on('dialog', async (dialog) => {
  if (dialog.type() === 'prompt' && apiKey) {
    await dialog.accept(apiKey);
    return;
  }
  await dialog.dismiss();
});

const manifest = {
  generatedAt: new Date().toISOString(),
  baseUrl: baseUrl.includes('127.0.0.1') ? '<local-dashboard>' : baseUrl,
  locale: captureLocale,
  outputDir: manifestOutputDir,
  visualAssetsDir: path.relative(repoRoot, visualAssetsDir) || '.',
  fixturesWritten: shouldWriteFixtures && Boolean(apiKey),
  captures: [],
  video: null,
};

try {
  await page.goto(baseUrl, { waitUntil: 'domcontentloaded' });
  await page.waitForLoadState('networkidle').catch(() => {});
  await page.waitForTimeout(900);
  for (const capture of captures) {
    await page.goto(absoluteUrl(capture.route), { waitUntil: 'domcontentloaded' });
    await waitForStablePage(page, capture);

    if (capture.scrollToTexts?.length) {
      for (const scrollTargetText of capture.scrollToTexts) {
        const target = page.getByText(scrollTargetText, { exact: false }).first();
        const visible = await target.isVisible().catch(() => false);
        if (!visible) {
          continue;
        }
        await target.scrollIntoViewIfNeeded();
        await page.waitForTimeout(500);
        break;
      }
    }

    const targetDir = capture.outputDir || outputDir;
    const targetPath = path.join(targetDir, capture.file);
    await page.screenshot({
      path: targetPath,
      fullPage: capture.fullPage ?? false,
    });

    manifest.captures.push({
      file: path.relative(repoRoot, targetPath),
      route: capture.route,
      waitForTexts: capture.waitForTexts ?? [],
      scrollToTexts: capture.scrollToTexts ?? [],
    });
  }
} finally {
  await context.close();
  await browser.close();
}

if (shouldRecordVideo && video) {
  const recordedPath = await video.path();
  const finalVideoPath = path.join(outputDir, videoFileName);
  await rename(recordedPath, finalVideoPath);
  manifest.video = {
    file: videoFileName,
  };
}

await writeFile(
  path.join(outputDir, 'manifest.json'),
  `${JSON.stringify(manifest, null, 2)}\n`,
  'utf8',
);

console.log(JSON.stringify(manifest, null, 2));

import { mkdir, rename, unlink, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';

const frontendRoot = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(frontendRoot, '..', '..');
const controlUrl = process.env.OPENCLAW_CONTROL_UI_URL;
if (!controlUrl) {
  throw new Error('OPENCLAW_CONTROL_UI_URL is required');
}

const outputDir = process.env.OPENCLAW_DOC_CAPTURE_OUTPUT_DIR
  || path.join(repoRoot, 'docs', 'openclaw-doc', 'assets', 'real-openclaw-run');
const chatScreenshotFile = process.env.OPENCLAW_CONTROL_UI_SCREENSHOT || '07-openclaw-control-ui-memory-chat.png';
const recallScreenshotFile = process.env.OPENCLAW_CONTROL_UI_RECALL_SCREENSHOT || '';
const visualScreenshotFile = process.env.OPENCLAW_CONTROL_UI_VISUAL_SCREENSHOT || '';
const agentsScreenshotFile = process.env.OPENCLAW_CONTROL_UI_AGENTS_SCREENSHOT;
const skillsScreenshotFile = process.env.OPENCLAW_CONTROL_UI_SKILLS_SCREENSHOT || 'openclaw-control-ui-skills-memory-palace.png';
const videoFile = process.env.OPENCLAW_CONTROL_UI_VIDEO || 'openclaw-control-ui-capability-tour.webm';
const viewport = { width: 1600, height: 1000 };
const recordSize = { width: 1440, height: 900 };

const sanitizeControlUrl = (rawUrl) => {
  const parsed = new URL(rawUrl);
  parsed.searchParams.delete('token');
  parsed.hash = '';
  return parsed;
};

const waitForCaptureText = async (page, capture, text) => {
  const target = page.getByText(text, { exact: false }).first();
  try {
    await target.waitFor({ state: 'visible', timeout: 7_000 });
    await target.scrollIntoViewIfNeeded();
  } catch (error) {
    throw new Error(
      `Capture target text "${text}" was not visible on route ${capture.route} for ${capture.file}: ${String(error?.message || error)}`,
    );
  }
};

const redactSensitiveText = async (page) => {
  await page.evaluate(() => {
    const replacements = [
      [/\/Users\/[^/\s]+/g, '/Users/<redacted>'],
      [/\/home\/[^/\s]+/g, '/home/<redacted>'],
      [/\/private\/var\/folders\/[^\s)]+/g, '/private/var/folders/<redacted>'],
      [/\/var\/folders\/[^\s)]+/g, '/var/folders/<redacted>'],
      [/[A-Za-z]:\\\\Users\\\\[^\\\\\s]+/g, 'C:\\Users\\<redacted>'],
    ];
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    let current = walker.nextNode();
    while (current) {
      let text = current.textContent || '';
      for (const [pattern, replacement] of replacements) {
        text = text.replace(pattern, replacement);
      }
      current.textContent = text;
      current = walker.nextNode();
    }
  });
};

await mkdir(outputDir, { recursive: true });

const browser = await chromium.launch({ headless: true });
const context = await browser.newContext({
  viewport,
  recordVideo: {
    dir: outputDir,
    size: recordSize,
  },
});
const warmupPage = await context.newPage();
const warmupVideo = warmupPage.video();
let warmupVideoPath = null;
let narrativeStorage = { local: {}, session: {} };
let narrativeRoute = '/chat?session=main';

await warmupPage.goto(controlUrl, {
  waitUntil: 'networkidle',
  timeout: 60_000,
});
await warmupPage.waitForTimeout(1800);
try {
  await warmupPage.waitForURL(/\/chat|\/agents|\/skills/, { timeout: 15_000 });
} catch {
  // Keep the current route if the WebUI does not auto-redirect in time.
}

narrativeStorage = await warmupPage.evaluate(() => ({
  local: Object.fromEntries(Object.keys(window.localStorage).map((key) => [key, window.localStorage.getItem(key)])),
  session: Object.fromEntries(Object.keys(window.sessionStorage).map((key) => [key, window.sessionStorage.getItem(key)])),
}));
const warmedUrl = sanitizeControlUrl(warmupPage.url());
narrativeRoute = `${warmedUrl.pathname}${warmedUrl.search}` || '/chat?session=main';

await warmupPage.close();
if (warmupVideo) {
  warmupVideoPath = await warmupVideo.path();
}

const page = await context.newPage();
const video = page.video();
await page.addInitScript((storageSnapshot) => {
  if (!storageSnapshot || typeof window === 'undefined') return;
  try {
    for (const [key, value] of Object.entries(storageSnapshot.local || {})) {
      window.localStorage.setItem(key, value);
    }
    for (const [key, value] of Object.entries(storageSnapshot.session || {})) {
      window.sessionStorage.setItem(key, value);
    }
  } catch {
    // Ignore browser storage bootstrap failures.
  }
}, narrativeStorage);

const manifest = {
  generatedAt: new Date().toISOString(),
  controlBaseUrl: '<local-capture-host-redacted>',
  screenshots: [],
  rawVideo: {
    file: videoFile,
    note: 'Local raw capture only. Public docs should reference the burned-subtitle MP4 deliverable.',
  },
  title: null,
};

const captureSpecs = [
  {
    file: skillsScreenshotFile,
    route: '/skills',
    searchInputPlaceholder: 'Search skills',
    searchValue: 'memory',
    dwellMs: 3200,
  },
  {
    file: chatScreenshotFile,
    route: '/chat?session=main',
    focusInput: true,
    dwellMs: 2600,
  },
  ...(recallScreenshotFile
    ? [{
        file: recallScreenshotFile,
        route: '/chat?session=main',
        scrollToText: 'memory-palace-profile',
        dwellMs: 3400,
      }]
    : []),
  ...(visualScreenshotFile
    ? [{
        file: visualScreenshotFile,
        route: '/chat?session=main',
        scrollToText: 'media_ref',
        dwellMs: 3400,
      }]
    : []),
  ...(agentsScreenshotFile
    ? [{
        file: agentsScreenshotFile,
        route: '/agents',
        dwellMs: 2800,
      }]
    : []),
];

try {
  await page.goto(new URL(narrativeRoute, controlUrl).toString(), {
    waitUntil: 'networkidle',
    timeout: 60_000,
  });
  await page.waitForTimeout(1000);
  for (const capture of captureSpecs) {
    const absoluteUrl = new URL(capture.route, controlUrl).toString();
    await page.goto(absoluteUrl, {
      waitUntil: 'networkidle',
      timeout: 60_000,
    });
    await page.waitForTimeout(1200);

    if (capture.focusInput) {
      await page.locator('textarea, input, [contenteditable="true"]').last().click({ timeout: 5000 }).catch(() => {});
      await page.waitForTimeout(300);
    }

    if (capture.scrollToText) {
      await waitForCaptureText(page, capture, capture.scrollToText);
      await page.waitForTimeout(500);
    }

    if (capture.searchInputPlaceholder && capture.searchValue) {
      const targetInput = page
        .locator(`input[placeholder*="${capture.searchInputPlaceholder}"], input`)
        .first();
      await targetInput.waitFor({ state: 'visible', timeout: 5_000 });
      await targetInput.fill(capture.searchValue);
      await page.waitForTimeout(600);
    }

    await redactSensitiveText(page);

    await page.screenshot({
      path: path.join(outputDir, capture.file),
      fullPage: false,
    });

    await page.waitForTimeout(capture.dwellMs ?? 2400);

    const sanitizedUrl = sanitizeControlUrl(page.url());
    manifest.screenshots.push({
      file: capture.file,
      route: `${sanitizedUrl.pathname}${sanitizedUrl.search}`,
    });
  }

  manifest.title = await page.title();

  await writeFile(
    path.join(outputDir, 'openclaw-control-ui-manifest.json'),
    `${JSON.stringify(manifest, null, 2)}\n`,
    'utf8',
  );

  console.log(JSON.stringify(manifest, null, 2));
} finally {
  await context.close();
  await browser.close();
}

if (warmupVideoPath) {
  await unlink(warmupVideoPath).catch(() => {});
}

if (video) {
  const recordedPath = await video.path();
  const finalVideoPath = path.join(outputDir, videoFile);
  await rename(recordedPath, finalVideoPath);
}

#!/usr/bin/env node
import {mkdir, writeFile} from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {
  docEnRef,
  docZhRef,
  getDashboardUrl,
  loadPlaywright,
  prepareScenario,
  startGateway,
  stopGateway,
  writeJson,
} from './openclaw_onboarding_doc_test_lib.mjs';

const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(scriptDir, '..');
const assetsDir = path.join(repoRoot, 'docs', 'openclaw-doc', 'assets', 'real-openclaw-run');
const reportPath = path.join(repoRoot, '.tmp', 'onboarding-doc-assets', 'report.json');
const useCurrentInstalledHost =
  String(process.env.OPENCLAW_ONBOARDING_USE_CURRENT_HOST || '').trim().toLowerCase() === 'true';

const viewport = {width: 1440, height: 900};

const specs = [
  {
    id: 'uninstalled-zh',
    scenarioName: 'capture-uninstalled',
    installPlugin: false,
    port: 18931,
    prompt: `请阅读 ${docZhRef} ，并按文档规则回答：如果当前宿主还没安装 memory-palace plugin，你会先检查什么，然后给我最短安装链路。不要假设 memory_onboarding_status 已经存在。`,
    expectedText: ['最短安装链路', 'setup --mode basic --profile b', '先检查'],
    output: 'openclaw-onboarding-doc-uninstalled.zh.png',
  },
  {
    id: 'installed-zh',
    scenarioName: 'capture-installed-zh',
    installPlugin: true,
    port: 18932,
    prompt: `请阅读 ${docZhRef} 。假设 plugin 已安装，请只用最短回答给出后续链路：先检查什么，再说明 onboarding -> provider probe -> apply。不要展开成长说明，也不要让我打开 dashboard。`,
    expectedText: ['provider probe', 'apply', 'onboarding --json'],
    output: 'openclaw-onboarding-doc-installed.zh.png',
  },
  {
    id: 'uninstalled-en',
    scenarioName: 'capture-uninstalled-en',
    installPlugin: false,
    port: 18933,
    prompt: `Read ${docEnRef} and answer by the document only: if the host OpenClaw has not installed the memory-palace plugin yet, what do you check first and what is the shortest install chain? Do not assume memory_onboarding_status already exists.`,
    expectedText: ['First check whether', 'setup --mode basic --profile b', 'shortest install chain'],
    output: 'openclaw-onboarding-doc-uninstalled.en.png',
  },
  {
    id: 'installed-en',
    scenarioName: 'capture-installed-en',
    installPlugin: true,
    port: 18934,
    prompt: `Read ${docEnRef}. Assume the plugin is already installed. Reply briefly with only the next chain: what do you check first, then onboarding -> provider probe -> apply. Do not give a long explanation and do not push me to the dashboard.`,
    expectedText: ['provider probe', 'apply', 'onboarding --json'],
    output: 'openclaw-onboarding-doc-installed.en.png',
  },
];

async function redactSensitiveText(page) {
  await page.evaluate(() => {
    const replacements = [
      [/\/Users\/[^/\s]+/g, "/Users/<redacted>"],
      [/\/home\/[^/\s]+/g, "/home/<redacted>"],
      [/\/private\/var\/folders\/[^\s)]+/g, "/private/var/folders/<redacted>"],
      [/\/var\/folders\/[^\s)]+/g, "/var/folders/<redacted>"],
      [/[A-Za-z]:\\\\Users\\\\[^\\\\\s]+/g, "C:\\Users\\<redacted>"],
    ];
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    let current = walker.nextNode();
    while (current) {
      let text = current.textContent || "";
      for (const [pattern, replacement] of replacements) {
        text = text.replace(pattern, replacement);
      }
      current.textContent = text;
      current = walker.nextNode();
    }
  });
}

async function capturePrompt(page, prompt, expectedText, outputPath) {
  const expectedTexts = Array.isArray(expectedText) ? expectedText : [expectedText];
  const input = page.getByRole('textbox').last();
  await input.fill(prompt);
  await input.press('Enter');

  const deadline = Date.now() + 120_000;
  let matchedText = null;
  while (Date.now() < deadline) {
    const pageText = await page.locator('main').innerText();
    matchedText = expectedTexts.find((candidate) => pageText.includes(candidate)) || null;
    if (matchedText) {
      break;
    }
    await page.waitForTimeout(1000);
  }

  if (!matchedText) {
    console.warn(`[capture] expected text not found, capturing current chat view: ${expectedTexts.join(' | ')}`);
  } else {
    const target = page.getByText(matchedText, {exact: false}).first();
    await target.scrollIntoViewIfNeeded().catch(() => {});
  }
  await redactSensitiveText(page);
  await page.screenshot({
    path: outputPath,
    fullPage: false,
  });
}

async function main() {
  await mkdir(assetsDir, {recursive: true});
  const {chromium} = await loadPlaywright();
  const report = {
    generatedAt: new Date().toISOString(),
    assetsDir,
    captures: [],
  };

  for (const spec of specs) {
    console.log(`[capture] prepare ${spec.id}`);
    const scenario = useCurrentInstalledHost && spec.installPlugin
      ? {
          name: `${spec.scenarioName}-current-host`,
          root: 'current-openclaw-host',
          env: process.env,
          port: spec.port,
        }
      : await prepareScenario({
          name: spec.scenarioName,
          port: spec.port,
          installPlugin: spec.installPlugin,
        });
    console.log(`[capture] start gateway ${spec.id}`);
    const gateway = useCurrentInstalledHost && spec.installPlugin ? null : await startGateway(scenario);
    const browser = await chromium.launch({headless: true});
    const context = await browser.newContext({viewport});
    const page = await context.newPage();

    try {
      console.log(`[capture] open dashboard ${spec.id}`);
      const url = await getDashboardUrl(scenario);
      await page.goto(url, {waitUntil: 'networkidle', timeout: 60_000});
      await page.waitForTimeout(1000);

      const connectButton = page.getByRole('button', {name: /连接|connect/i}).first();
      if (await connectButton.count()) {
        await connectButton.click().catch(() => {});
        await page.waitForTimeout(800);
      }

      await page.waitForURL(/\/chat/, {timeout: 60_000});
      const outputPath = path.join(assetsDir, spec.output);
      console.log(`[capture] render prompt ${spec.id}`);
      await capturePrompt(page, spec.prompt, spec.expectedText, outputPath);
      console.log(`[capture] saved ${spec.output}`);
      report.captures.push({
        id: spec.id,
        output: spec.output,
        route: page.url(),
        scenarioRoot: scenario.root,
      });
    } catch (error) {
      const logs = gateway?.getLogs?.() || {stdout: '', stderr: ''};
      console.error(`[capture] failed ${spec.id}`);
      console.error(logs.stdout);
      console.error(logs.stderr);
      throw error;
    } finally {
      await context.close().catch(() => {});
      await browser.close().catch(() => {});
      await stopGateway(gateway);
    }
  }

  await writeJson(reportPath, report);
  await writeFile(
    path.join(assetsDir, 'openclaw-onboarding-doc-assets-manifest.json'),
    `${JSON.stringify(report, null, 2)}\n`,
    'utf8',
  );
  console.log(JSON.stringify(report, null, 2));
}

main().catch((error) => {
  console.error(error instanceof Error ? error.stack || error.message : String(error));
  process.exitCode = 1;
});

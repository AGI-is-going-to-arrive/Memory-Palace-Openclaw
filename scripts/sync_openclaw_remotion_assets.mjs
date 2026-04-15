#!/usr/bin/env node
import {copyFile, mkdir, rm, stat} from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(scriptDir, '..');
const docsAssetsDir = path.join(repoRoot, 'docs', 'openclaw-doc', 'assets', 'real-openclaw-run');
const rawVideoDir = path.join(repoRoot, 'video', 'assets', 'raw');
const publicRoot = path.join(repoRoot, 'frontend', 'public', 'remotion', 'openclaw');

const clean = process.argv.includes('--clean');

const docsAssetFiles = [
  '07-openclaw-control-ui-memory-chat.png',
  '24-acl-agents-page.en.png',
  '24-acl-agents-page.png',
  '24-acl-alpha-memory-confirmed.en.png',
  '24-acl-alpha-memory-confirmed.png',
  '24-acl-beta-chat-isolated.en.png',
  '24-acl-beta-chat-isolated.png',
  'dashboard-visual-memory-root.png',
  'dashboard-visual-memory.png',
  'openclaw-control-ui-chat-recall-confirmed.png',
  'openclaw-control-ui-skills-memory-palace.png',
  'openclaw-control-ui-skills-memory-palace-detail.png',
  'openclaw-control-ui-skills-memory-palace-detail.zh.png',
];

const optionalRawVideoFiles = [
  'openclaw-onboarding-doc-flow.en.installed.webm',
  'openclaw-onboarding-doc-flow.en.uninstalled.webm',
  'openclaw-onboarding-doc-flow.zh.installed.webm',
  'openclaw-onboarding-doc-flow.zh.uninstalled.webm',
];

const ensureFile = async (sourcePath) => {
  try {
    const meta = await stat(sourcePath);
    if (!meta.isFile()) {
      throw new Error(`${sourcePath} is not a regular file`);
    }
  } catch (error) {
    throw new Error(`Required asset missing: ${sourcePath}\n${error}`);
  }
};

const syncFile = async (sourcePath, destinationPath) => {
  await ensureFile(sourcePath);
  await mkdir(path.dirname(destinationPath), {recursive: true});
  await copyFile(sourcePath, destinationPath);
};

const main = async () => {
  if (clean) {
    await rm(publicRoot, {recursive: true, force: true});
  }

  const docsTargetDir = publicRoot;
  const rawTargetDir = path.join(publicRoot, 'raw');
  const copiedRawVideos = [];
  const skippedOptional = [];

  for (const fileName of docsAssetFiles) {
    await syncFile(
      path.join(docsAssetsDir, fileName),
      path.join(docsTargetDir, fileName),
    );
  }

  for (const fileName of optionalRawVideoFiles) {
    const sourcePath = path.join(rawVideoDir, fileName);
    const destinationPath = path.join(rawTargetDir, fileName);
    try {
      await syncFile(sourcePath, destinationPath);
      copiedRawVideos.push(fileName);
    } catch {
      skippedOptional.push(path.relative(repoRoot, sourcePath).replaceAll(path.sep, '/'));
    }
  }

  const payload = {
    generatedAt: new Date().toISOString(),
    destination: path.relative(repoRoot, publicRoot).replaceAll(path.sep, '/'),
    copied: {
      docsAssets: docsAssetFiles,
      rawVideos: copiedRawVideos,
    },
    skippedOptional,
  };
  process.stdout.write(`${JSON.stringify(payload, null, 2)}\n`);
};

await main();

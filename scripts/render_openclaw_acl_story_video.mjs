#!/usr/bin/env node
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {spawn} from 'node:child_process';

const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(scriptDir, '..');
const remotionRenderScript = path.join(scriptDir, 'render_openclaw_remotion_videos.mjs');

const requestedLanguage = (process.argv[2] || 'all').trim().toLowerCase();
const target =
  requestedLanguage === 'zh'
    ? 'acl-zh'
    : requestedLanguage === 'en'
      ? 'acl-en'
      : 'acl';

const child = spawn('node', [remotionRenderScript, target], {
  cwd: repoRoot,
  stdio: 'inherit',
  env: process.env,
});

child.on('exit', (code) => {
  process.exit(code ?? 0);
});

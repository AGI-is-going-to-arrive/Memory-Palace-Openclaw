#!/usr/bin/env node
import {copyFile, mkdir, rm, writeFile} from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {spawn} from 'node:child_process';

const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(scriptDir, '..');
const frontendRoot = path.join(repoRoot, 'frontend');
const publicRoot = path.join(frontendRoot, 'public', 'remotion', 'openclaw');
const assetsDir = path.join(repoRoot, 'docs', 'openclaw-doc', 'assets', 'real-openclaw-run');
const tempDir = path.join(repoRoot, '.tmp', 'remotion-openclaw-render');

const fps = 30;
const requestedTarget = (process.argv[2] || 'all').trim().toLowerCase();
const shouldCaptureOnboarding = !process.argv.includes('--skip-onboarding-capture');
const shouldCaptureControlUi = Boolean(process.env.OPENCLAW_CONTROL_UI_URL);
const shouldCaptureDashboard = Boolean(process.env.OPENCLAW_DOC_CAPTURE_BASE_URL);
const onboardingChatBaseUrl =
  String(process.env.OPENCLAW_ONBOARDING_CHAT_BASE_URL || '').trim() ||
  'http://127.0.0.1:8317/v1';
const onboardingTempRoot =
  String(process.env.OPENCLAW_ONBOARDING_TEMP_ROOT || '').trim() ||
  '/tmp/openclaw-onboarding-doc-chat-flow';

const remotionEntry = 'src/remotion/index.jsx';
const compositionId = 'OpenClawDocStory';

const capabilityScenes = {
  zh: [
    {
      id: 'skills-list',
      asset: 'remotion/openclaw/openclaw-control-ui-skills-memory-palace.png',
      durationFrames: 150,
      shortLabel: '1',
      eyebrow: 'OpenClaw WebUI',
      heading: '先确认 Skills 里已经出现 Memory Palace 相关条目',
      body: '这里证明 companion skill 已进入宿主 WebUI，而不是要求某个固定显示名。',
      caption: '第 1 步：先在 Skills 页找到 Memory Palace 相关条目。关键是确认这条能力已经进入宿主 WebUI，而不是死盯一个固定显示名。',
    },
    {
      id: 'skills-detail',
      asset: 'remotion/openclaw/openclaw-control-ui-skills-memory-palace-detail.zh.png',
      durationFrames: 180,
      shortLabel: '2',
      eyebrow: 'Capability Surface',
      heading: '详情层展示 durable recall、显式记忆核验和维护能力',
      body: 'skill 负责告诉模型何时该显式介入 durable recall、memory verification 和 visual memory storage。',
      caption: '第 2 步：点开详情层。这里读的是 skill 的职责边界，不是说 skill 自己就是默认执行层。',
    },
    {
      id: 'default-recall',
      asset: 'remotion/openclaw/openclaw-control-ui-chat-recall-confirmed.png',
      durationFrames: 180,
      shortLabel: '3',
      eyebrow: 'Native Chat Recall',
      heading: '默认 recall 现在直接按当前宿主 strict acceptance 实拍',
      body: 'tool output、确认文案和 recall marker 都在同一条原生聊天线程里可见。',
      caption: '第 3 步：回到 Chat。现在公开主链直接引用当前宿主 strict acceptance 的实拍图，而不是继续沿用那张已经被空聊天壳覆盖的旧 recall 图。',
    },
    {
      id: 'visual-memory',
      asset: 'remotion/openclaw/dashboard-visual-memory.png',
      durationFrames: 180,
      shortLabel: '4',
      eyebrow: 'Visual Memory Proof',
      heading: '静态 visual memory 证据改成真实 Dashboard 节点页',
      body: '当前公开静态图直接展示 core://visual 节点里的 Visual Memory、Summary、OCR 和 Entities。',
      caption: '第 4 步：这轮不再用一张失真的聊天图硬说成 visual memory。新的静态证据来自真实 Dashboard 节点页，画面里能直接看到 Visual Memory、Summary、OCR 和 Entities。',
    },
    {
      id: 'chat-answer',
      asset: 'remotion/openclaw/07-openclaw-control-ui-memory-chat.png',
      durationFrames: 170,
      shortLabel: '5',
      eyebrow: 'User Experience',
      heading: '用户最终看到的是 recall block、tool card 和 answer block',
      body: '不是另开一套 Memory Palace 专属前端。',
      caption: '第 5 步：用户最终感知到的是 recall、tool output 和 answer block 在原生聊天里长出来，不是另开一套前端。',
    },
  ],
  en: [
    {
      id: 'skills-list',
      asset: 'remotion/openclaw/openclaw-control-ui-skills-memory-palace.png',
      durationFrames: 150,
      shortLabel: '1',
      eyebrow: 'OpenClaw WebUI',
      heading: 'Start in Skills and confirm a Memory Palace-related entry exists',
      body: 'The point is to prove the companion skill is present in the host WebUI, not to hard-code one visible label.',
      caption: 'Step 1: Start in Skills and confirm a Memory Palace-related entry exists. The point is presence in the host WebUI, not one fixed visible label.',
    },
    {
      id: 'skills-detail',
      asset: 'remotion/openclaw/openclaw-control-ui-skills-memory-palace-detail.png',
      durationFrames: 180,
      shortLabel: '2',
      eyebrow: 'Capability Surface',
      heading: 'The detail sheet shows durable recall, explicit verification, and maintenance',
      body: 'The skill tells the model when to intervene explicitly for durable recall and visual memory flows.',
      caption: 'Step 2: Open the detail sheet. Read it as the skill boundary, not as the default execution layer itself.',
    },
    {
      id: 'default-recall',
      asset: 'remotion/openclaw/openclaw-control-ui-chat-recall-confirmed.png',
      durationFrames: 180,
      shortLabel: '3',
      eyebrow: 'Native Chat Recall',
      heading: 'The default recall proof now comes from a current-host strict-acceptance still',
      body: 'Tool output, confirmation text, and the recall marker are visible inside one native chat thread.',
      caption: 'Step 3: Return to Chat. The public story now points at a current-host strict-acceptance still instead of reusing the older recall image that had already been overwritten by an empty shell.',
    },
    {
      id: 'visual-memory',
      asset: 'remotion/openclaw/dashboard-visual-memory.png',
      durationFrames: 180,
      shortLabel: '4',
      eyebrow: 'Visual Memory Proof',
      heading: 'The static visual-memory proof now comes from the real Dashboard node page',
      body: 'The still now shows the core://visual node with Visual Memory, Summary, OCR, and Entities on screen.',
      caption: 'Step 4: This refresh no longer overclaims visual memory from one stale chat still. The static proof now comes from the real Dashboard node page, where Visual Memory, Summary, OCR, and Entities are visible together.',
    },
    {
      id: 'chat-answer',
      asset: 'remotion/openclaw/07-openclaw-control-ui-memory-chat.png',
      durationFrames: 170,
      shortLabel: '5',
      eyebrow: 'User Experience',
      heading: 'Users finally see recall blocks, tool cards, and answer blocks',
      body: 'Not a separate Memory Palace frontend.',
      caption: 'Step 5: The final user experience is recall, tool output, and answer blocks inside native chat, not a separate frontend.',
    },
  ],
};

const aclScenes = {
  zh: [
    {
      id: 'agents',
      asset: 'remotion/openclaw/24-acl-agents-page.png',
      durationFrames: 160,
      shortLabel: '1',
      eyebrow: 'ACL',
      heading: '先到 Agents 页确认 main / alpha / beta scope',
      body: 'ACL 指的就是按这些 scope 限制 durable memory 的读写与可见性。',
      caption: '第 1 步：先到 Agents 页。ACL 指的就是按 main / alpha / beta 这些 scope 限制 durable memory 的读写与可见性。',
    },
    {
      id: 'alpha',
      asset: 'remotion/openclaw/24-acl-alpha-memory-confirmed.png',
      durationFrames: 170,
      shortLabel: '2',
      eyebrow: 'Alpha Write',
      heading: '先在 alpha 对话里明确写入一条 workflow',
      body: '这里先证明记忆真的已经写进去。',
      caption: '第 2 步：进入 alpha 对话，明确让 Memory Palace 记住 workflow，并看到确认回复。',
    },
    {
      id: 'beta-scope',
      asset: 'remotion/openclaw/24-acl-beta-chat-isolated.png',
      durationFrames: 170,
      shortLabel: '3',
      eyebrow: 'Beta Scope',
      heading: '再切到 beta，对话表面不变，recall scope 已改变',
      body: 'beta 只能读取 beta 自己的 durable memory。',
      caption: '第 3 步：切到 beta。界面还是同一套 OpenClaw WebUI，但 beta 只允许读 beta 自己的 durable memory。',
    },
    {
      id: 'beta-answer',
      asset: 'remotion/openclaw/24-acl-beta-chat-isolated.png',
      durationFrames: 180,
      shortLabel: '4',
      eyebrow: 'Isolation Proof',
      heading: 'beta 问 alpha 的默认 workflow，回答直接是 UNKNOWN',
      body: '这不是没记住，而是 ACL 不允许 beta 读取 alpha 的 durable memory。',
      caption: '第 4 步：beta 现在去问 alpha 的默认 workflow，回答直接是 UNKNOWN。这证明 ACL 隔离正在生效。',
    },
  ],
  en: [
    {
      id: 'agents',
      asset: 'remotion/openclaw/24-acl-agents-page.en.png',
      durationFrames: 160,
      shortLabel: '1',
      eyebrow: 'ACL',
      heading: 'Start in Agents and confirm the main / alpha / beta scopes',
      body: 'ACL means durable-memory read/write visibility is constrained by these scopes.',
      caption: 'Step 1: Start in Agents and confirm main / alpha / beta scopes. ACL means durable-memory visibility is constrained by those scopes.',
    },
    {
      id: 'alpha',
      asset: 'remotion/openclaw/24-acl-alpha-memory-confirmed.en.png',
      durationFrames: 170,
      shortLabel: '2',
      eyebrow: 'Alpha Write',
      heading: 'Write an explicit workflow memory in the alpha chat first',
      body: 'This proves the memory was actually stored.',
      caption: 'Step 2: Enter alpha chat and store a workflow memory. The confirmation reply proves it was really written.',
    },
    {
      id: 'beta-scope',
      asset: 'remotion/openclaw/24-acl-beta-chat-isolated.en.png',
      durationFrames: 170,
      shortLabel: '3',
      eyebrow: 'Beta Scope',
      heading: 'Then switch to beta: same UI surface, different recall scope',
      body: 'beta is limited to beta-scoped durable memory only.',
      caption: 'Step 3: Switch to beta. The UI is still the same OpenClaw WebUI, but beta is limited to beta-scoped durable memory only.',
    },
    {
      id: 'beta-answer',
      asset: 'remotion/openclaw/24-acl-beta-chat-isolated.en.png',
      durationFrames: 180,
      shortLabel: '4',
      eyebrow: 'Isolation Proof',
      heading: 'beta asks for alpha’s workflow and the answer is UNKNOWN',
      body: 'That does not mean the system forgot; ACL is blocking cross-scope recall.',
      caption: 'Step 4: beta asks for alpha’s workflow and gets UNKNOWN. That does not mean the system forgot. ACL is blocking cross-scope recall.',
    },
  ],
};

const onboardingScenes = {
  zh: [
    {
      id: 'uninstalled',
      asset: 'remotion/openclaw/openclaw-onboarding-doc-uninstalled.zh.png',
      durationFrames: 190,
      shortLabel: '1',
      eyebrow: 'Chat-first Install',
      heading: '未安装时，OpenClaw 先检查 plugin 是否存在',
      body: '它不会假装 `memory_onboarding_*` 已经存在，而是先给出最短安装链路。',
      caption: '第 1 步：把同一份 onboarding 文档交给 OpenClaw。未安装宿主时，它先检查 plugin 是否已安装，并给出最短安装链路。',
    },
    {
      id: 'installed',
      asset: 'remotion/openclaw/openclaw-onboarding-doc-installed.zh.png',
      durationFrames: 220,
      shortLabel: '2',
      eyebrow: 'Chat-first Onboarding',
      heading: '已安装时，OpenClaw 继续留在聊天里走 onboarding -> probe -> apply',
      body: '不是把用户推去 dashboard，而是在同一条对话里继续配置。',
      caption: '第 2 步：装好 plugin 后，再把同一份文档交给 OpenClaw。它留在聊天线程里，按 onboarding、provider probe 和 apply 往下走。',
    },
  ],
  en: [
    {
      id: 'uninstalled',
      asset: 'remotion/openclaw/openclaw-onboarding-doc-uninstalled.en.png',
      durationFrames: 190,
      shortLabel: '1',
      eyebrow: 'Chat-first Install',
      heading: 'If not installed, OpenClaw checks plugin state first',
      body: 'It does not pretend `memory_onboarding_*` already exists; it gives the shortest install path first.',
      caption: 'Step 1: Hand the same onboarding document to OpenClaw. In the uninstalled case, it first checks plugin state and gives the shortest install path.',
    },
    {
      id: 'installed',
      asset: 'remotion/openclaw/openclaw-onboarding-doc-installed.en.png',
      durationFrames: 220,
      shortLabel: '2',
      eyebrow: 'Chat-first Onboarding',
      heading: 'If installed, OpenClaw stays in chat and goes onboarding -> probe -> apply',
      body: 'It does not push the user to the dashboard; it continues configuration in the same thread.',
      caption: 'Step 2: After installation, hand the same document to OpenClaw again. It stays in chat and continues with onboarding, provider probe, and apply.',
    },
  ],
};

function run(command, args, options = {}) {
  return new Promise((resolve, reject) => {
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
}

async function ensureDir(dirPath) {
  await mkdir(dirPath, {recursive: true});
}

function buildCaptionJson(scenes) {
  let cursorMs = 0;
  return scenes.map((scene) => {
    const durationMs = Number.isFinite(scene.seconds)
      ? Math.round(scene.seconds * 1000)
      : Math.round((scene.durationFrames / fps) * 1000);
    const caption = {
      text: scene.caption,
      startMs: cursorMs,
      endMs: cursorMs + durationMs,
      timestampMs: cursorMs,
      confidence: 1,
    };
    cursorMs += durationMs;
    return caption;
  });
}

function captionJsonToSrt(captions) {
  const formatSrtTimestamp = (ms) => {
    const totalMs = Math.max(0, Math.round(ms));
    const hours = String(Math.floor(totalMs / 3_600_000)).padStart(2, '0');
    const minutes = String(Math.floor((totalMs % 3_600_000) / 60_000)).padStart(2, '0');
    const seconds = String(Math.floor((totalMs % 60_000) / 1000)).padStart(2, '0');
    const millis = String(totalMs % 1000).padStart(3, '0');
    return `${hours}:${minutes}:${seconds},${millis}`;
  };

  return captions.map((caption, index) => {
    return [
      String(index + 1),
      `${formatSrtTimestamp(caption.startMs)} --> ${formatSrtTimestamp(caption.endMs)}`,
      caption.text,
      '',
    ].join('\n');
  }).join('\n');
}

async function copyIfExists(sourcePath, destinationPath) {
  await ensureDir(path.dirname(destinationPath));
  await copyFile(sourcePath, destinationPath);
}

async function syncAssets(stories) {
  await ensureDir(publicRoot);
  const requiredAssets = new Set();
  for (const story of stories) {
    for (const scene of story.props.scenes) {
      if (typeof scene.assetPath === 'string' && scene.assetPath.startsWith('remotion/openclaw/')) {
        requiredAssets.add(scene.assetPath.replace('remotion/openclaw/', ''));
      }
    }
  }

  for (const asset of requiredAssets) {
    await copyIfExists(
      path.join(assetsDir, asset),
      path.join(publicRoot, asset),
    );
  }
}

async function runOptionalCaptureScripts(target) {
  const needsCapabilityOrAcl = target === 'all' || target === 'capability' || target === 'acl';
  const needsOnboarding = target === 'all' || target === 'onboarding';

  if (shouldCaptureDashboard && needsCapabilityOrAcl) {
    console.log('[render] refreshing dashboard captures');
    await run('node', ['frontend/e2e/capture-openclaw-doc-assets.mjs'], {
      cwd: repoRoot,
    });
  }
  if (shouldCaptureControlUi && needsCapabilityOrAcl) {
    console.log('[render] refreshing control-ui captures');
    await run('node', ['frontend/e2e/capture-openclaw-control-ui.mjs'], {
      cwd: repoRoot,
    });
  }
  if (shouldCaptureOnboarding && needsOnboarding) {
    console.log('[render] refreshing onboarding captures');
    await run('node', ['scripts/capture_openclaw_onboarding_doc_assets.mjs'], {
      cwd: repoRoot,
      env: {
        OPENCLAW_ONBOARDING_CHAT_BASE_URL: onboardingChatBaseUrl,
        OPENCLAW_ONBOARDING_TEMP_ROOT: onboardingTempRoot,
      },
    });
  }
}

function buildStoryProps({title, subtitle, language, scenes}) {
  return {
    title,
    subtitle,
    eyebrow: 'OpenClaw WebUI',
    language,
    scenes: scenes.map((scene) => ({
      id: scene.id,
      mediaType: scene.mediaType || 'image',
      assetPath: scene.asset,
      seconds: Number.isFinite(scene.seconds) ? scene.seconds : Number(scene.durationFrames || 0) / fps,
      badge: scene.eyebrow || scene.shortLabel,
      headline: scene.heading,
      body: scene.body,
      zoomStart: scene.zoomStart,
      zoomEnd: scene.zoomEnd,
      showCard: scene.showCard,
    })),
    captions: buildCaptionJson(scenes),
  };
}

function storiesForTarget(target) {
  const stories = [];
  if (target === 'all' || target === 'capability' || target === 'capability-zh' || target === 'capability-en') {
    if (target !== 'capability-en') {
      stories.push({
        id: 'capability-zh',
        output: path.join(assetsDir, 'openclaw-control-ui-capability-tour.zh.mp4'),
        props: buildStoryProps({
          title: 'Memory Palace in OpenClaw WebUI',
          subtitle: 'Plugin default path + companion skill escalation',
          language: 'zh',
          scenes: capabilityScenes.zh,
        }),
      });
    }
    if (target !== 'capability-zh') {
      stories.push({
        id: 'capability-en',
        output: path.join(assetsDir, 'openclaw-control-ui-capability-tour.en.mp4'),
        props: buildStoryProps({
          title: 'Memory Palace in OpenClaw WebUI',
          subtitle: 'Plugin default path + companion skill escalation',
          language: 'en',
          scenes: capabilityScenes.en,
        }),
      });
    }
  }
  if (target === 'all' || target === 'acl' || target === 'acl-zh' || target === 'acl-en') {
    if (target !== 'acl-en') {
      stories.push({
        id: 'acl-zh',
        output: path.join(assetsDir, 'openclaw-control-ui-acl-scenario.zh.mp4'),
        props: buildStoryProps({
          title: 'ACL Isolation in OpenClaw WebUI',
          subtitle: 'Durable memory stays scoped to each agent',
          language: 'zh',
          scenes: aclScenes.zh,
        }),
      });
    }
    if (target !== 'acl-zh') {
      stories.push({
        id: 'acl-en',
        output: path.join(assetsDir, 'openclaw-control-ui-acl-scenario.en.mp4'),
        props: buildStoryProps({
          title: 'ACL Isolation in OpenClaw WebUI',
          subtitle: 'Durable memory stays scoped to each agent',
          language: 'en',
          scenes: aclScenes.en,
        }),
      });
    }
  }
  if (target === 'all' || target === 'onboarding' || target === 'onboarding-zh' || target === 'onboarding-en') {
    if (target !== 'onboarding-en') {
      stories.push({
        id: 'onboarding-zh',
        output: path.join(assetsDir, 'openclaw-onboarding-doc-flow.zh.burned-captions.mp4'),
        props: buildStoryProps({
          title: 'Conversational Onboarding',
          subtitle: 'Same document, two states: uninstalled and installed',
          language: 'zh',
          scenes: onboardingScenes.zh,
        }),
      });
    }
    if (target !== 'onboarding-zh') {
      stories.push({
        id: 'onboarding-en',
        output: path.join(assetsDir, 'openclaw-onboarding-doc-flow.en.burned-captions.mp4'),
        props: buildStoryProps({
          title: 'Conversational Onboarding',
          subtitle: 'Same document, two states: uninstalled and installed',
          language: 'en',
          scenes: onboardingScenes.en,
        }),
      });
    }
  }
  return stories;
}

async function renderStory(story) {
  const propsPath = path.join(tempDir, `${story.id}.props.json`);
  await writeFile(propsPath, `${JSON.stringify(story.props)}\n`, 'utf8');
  const baseName = story.output
    .replace(/\.burned-captions\.mp4$/, '')
    .replace(/\.mp4$/, '');
  const captionJsonPath = `${baseName}.captions.json`;
  const transcriptPath = `${baseName}.transcript.json`;
  const srtPath = `${baseName}.srt`;
  await writeFile(captionJsonPath, `${JSON.stringify(story.props.captions, null, 2)}\n`, 'utf8');
  await writeFile(
    transcriptPath,
    `${JSON.stringify({
      generatedAt: new Date().toISOString(),
      text: story.props.captions.map((caption) => caption.text).join(' '),
      captions: story.props.captions,
    }, null, 2)}\n`,
    'utf8',
  );
  await writeFile(srtPath, `${captionJsonToSrt(story.props.captions)}\n`, 'utf8');
  console.log(`[render] remotion ${story.id}`);
  await run('pnpm', [
    '--dir',
    'frontend',
    'exec',
    'remotion',
    'render',
    remotionEntry,
    compositionId,
    story.output,
    '--props',
    propsPath,
  ], {
    cwd: repoRoot,
  });
}

async function main() {
  await rm(tempDir, {recursive: true, force: true});
  await ensureDir(tempDir);
  await runOptionalCaptureScripts(requestedTarget);

  const stories = storiesForTarget(requestedTarget);
  if (!stories.length) {
    throw new Error(`Unknown render target: ${requestedTarget}`);
  }
  await syncAssets(stories);

  for (const story of stories) {
    await renderStory(story);
  }

  const manifest = {
    generatedAt: new Date().toISOString(),
    target: requestedTarget,
    stories: stories.map((story) => ({
      ...(function buildSidecars() {
        const baseName = story.output
          .replace(/\.burned-captions\.mp4$/, '')
          .replace(/\.mp4$/, '');
        return {
          captions: path.relative(repoRoot, `${baseName}.captions.json`),
          srt: path.relative(repoRoot, `${baseName}.srt`),
          transcript: path.relative(repoRoot, `${baseName}.transcript.json`),
        };
      }()),
      id: story.id,
      output: path.relative(repoRoot, story.output),
      scenes: story.props.scenes.map((scene) => ({
        id: scene.id,
        asset: scene.assetPath,
      })),
    })),
  };
  await writeFile(
    path.join(assetsDir, 'openclaw-remotion-render-manifest.json'),
    `${JSON.stringify(manifest, null, 2)}\n`,
    'utf8',
  );
  console.log(JSON.stringify(manifest, null, 2));
}

main().catch((error) => {
  console.error(error instanceof Error ? error.stack || error.message : String(error));
  process.exitCode = 1;
});

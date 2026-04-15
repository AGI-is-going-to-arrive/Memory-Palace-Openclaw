#!/usr/bin/env node
import { mkdir, rm, writeFile } from "node:fs/promises";
import path from "node:path";
import { spawn } from "node:child_process";
import {
  docEnRef,
  docZhRef,
  getDashboardUrl,
  loadPlaywright,
  prepareScenario,
  repoRoot,
  startGateway,
  stopGateway,
} from "./openclaw_onboarding_doc_test_lib.mjs";

const requestedLanguage = (process.argv[2] || "all").trim().toLowerCase();
const rawDir = path.join(repoRoot, "video", "assets", "raw");
const outputDir =
  process.env.OPENCLAW_ONBOARDING_DOC_OUTPUT_DIR
  || path.join(repoRoot, "docs", "openclaw-doc", "assets", "real-openclaw-run");
const tempDir = path.join(repoRoot, ".tmp", "onboarding-doc-video-build");
const remotionRenderScript = path.join(repoRoot, "scripts", "render_openclaw_remotion_videos.mjs");
const useCurrentInstalledHost =
  String(process.env.OPENCLAW_ONBOARDING_USE_CURRENT_HOST || "").trim().toLowerCase() === "true";

const viewport = { width: 1440, height: 900 };
const recordSize = { width: 1440, height: 900 };

const prompts = {
  zh: {
    uninstalled: `请阅读 ${docZhRef} ，并按文档规则回答：如果当前宿主还没安装 memory-palace plugin，你会先检查什么，然后给我最短安装链路。不要假设 memory_onboarding_status 已经存在。`,
    installed: `请阅读 ${docZhRef} 。然后按这页的规则回答：第一步你会先检查什么？如果 plugin 未安装你会先让我做什么？如果 plugin 已安装你会走哪条链路？不要让我打开 dashboard。`,
    uninstalledWait: ["最短安装链路", "setup --mode basic --profile b", "先检查"],
    installedWait: ["如果 plugin 已安装", "provider probe", "apply"],
    captions: [
      "先在普通聊天窗口里把同一份 onboarding 文档交给 OpenClaw。未安装宿主时，它不会假装 onboarding tools 已存在，而是先检查 plugin 是否已安装，并给出最短安装链路。",
      "装好 plugin 之后，再把同一份文档交给 OpenClaw。它会继续留在聊天线程里，按安装状态分流到 onboarding、provider probe 和 apply，而不是把用户推去 dashboard。",
    ],
    subtitleStyle:
      "FontName=PingFang SC,FontSize=10,PrimaryColour=&H00FFFFFF,OutlineColour=&H00181818,BackColour=&H4A000000,BorderStyle=3,Outline=0.8,Shadow=0,MarginV=20,Alignment=2",
  },
  en: {
    uninstalled: `Read ${docEnRef} and answer by the document only: if the host OpenClaw has not installed the memory-palace plugin yet, what do you check first and what is the shortest install chain? Do not assume memory_onboarding_status already exists.`,
    installed: `Read ${docEnRef} and answer by the updated rules only: what must you check first, what do you do if the plugin is not installed yet, and what chain do you follow if it is already installed? Do not push me to the dashboard.`,
    uninstalledWait: [
      "setup --mode basic --profile b",
      "shortest install chain",
      "First check whether",
    ],
    installedWait: [
      "plugin is already installed",
      "provider probe",
      "apply",
      "onboarding --json",
    ],
    captions: [
      "Start by handing the same onboarding document to OpenClaw in a normal chat. In the uninstalled host case, it does not pretend the onboarding tools already exist. It first checks whether the plugin is installed, then gives the shortest install chain.",
      "After the plugin is installed, hand the same document to OpenClaw again. It stays in chat and routes the user into onboarding, provider probing, and apply, instead of pushing the user to the dashboard.",
    ],
    subtitleStyle:
      "FontName=Helvetica,FontSize=10,PrimaryColour=&H00FFFFFF,OutlineColour=&H00181818,BackColour=&H4A000000,BorderStyle=3,Outline=0.8,Shadow=0,MarginV=20,Alignment=2",
  },
};

function run(command, args, { cwd = repoRoot } = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, {
      cwd,
      env: process.env,
      stdio: ["ignore", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk) => {
      stdout += chunk.toString();
    });
    child.stderr.on("data", (chunk) => {
      stderr += chunk.toString();
    });
    child.on("close", (code) => {
      if ((code ?? 0) === 0) {
        resolve({ stdout, stderr });
        return;
      }
      reject(new Error(`${command} ${args.join(" ")} failed with code ${code}\n${stderr}`));
    });
  });
}

async function getDuration(filePath) {
  const result = await run("ffprobe", [
    "-v",
    "error",
    "-show_entries",
    "format=duration",
    "-of",
    "default=nk=1:nw=1",
    filePath,
  ]);
  return Number.parseFloat(result.stdout.trim());
}

function formatSrtTimestamp(seconds) {
  const totalMs = Math.round(seconds * 1000);
  const hours = String(Math.floor(totalMs / 3_600_000)).padStart(2, "0");
  const minutes = String(Math.floor((totalMs % 3_600_000) / 60_000)).padStart(2, "0");
  const secs = String(Math.floor((totalMs % 60_000) / 1000)).padStart(2, "0");
  const ms = String(totalMs % 1000).padStart(3, "0");
  return `${hours}:${minutes}:${secs},${ms}`;
}

async function transcodeWebmToMp4(inputPath, outputPath) {
  await run("ffmpeg", [
    "-y",
    "-i",
    inputPath,
    "-vf",
    "fps=30,scale=1440:900:force_original_aspect_ratio=decrease,pad=1440:900:(ow-iw)/2:(oh-ih)/2:color=#111111",
    "-c:v",
    "libx264",
    "-preset",
    "medium",
    "-crf",
    "22",
    "-pix_fmt",
    "yuv420p",
    "-movflags",
    "+faststart",
    "-an",
    outputPath,
  ]);
}

async function concatClips(clipPaths, outputPath) {
  const listPath = path.join(tempDir, `${path.basename(outputPath)}.txt`);
  await writeFile(
    listPath,
    `${clipPaths.map((clip) => `file '${clip.replaceAll("'", "'\\''")}'`).join("\n")}\n`,
    "utf8",
  );
  await run("ffmpeg", [
    "-y",
    "-f",
    "concat",
    "-safe",
    "0",
    "-i",
    listPath,
    "-c:v",
    "libx264",
    "-preset",
    "medium",
    "-crf",
    "22",
    "-pix_fmt",
    "yuv420p",
    outputPath,
  ]);
}

async function burnSubtitles(inputPath, srtPath, outputPath, subtitleStyle) {
  await run("ffmpeg", [
    "-y",
    "-i",
    inputPath,
    "-vf",
    `subtitles='${srtPath.replaceAll("\\", "/").replaceAll(":", "\\:")}':force_style='${subtitleStyle}'`,
    "-c:v",
    "libx264",
    "-preset",
    "medium",
    "-crf",
    "22",
    "-pix_fmt",
    "yuv420p",
    "-movflags",
    "+faststart",
    "-an",
    outputPath,
  ]);
}

async function buildCaptionArtifacts(language, durations) {
  const spec = prompts[language];
  const captions = [];
  let cursorMs = 0;
  for (let index = 0; index < durations.length; index += 1) {
    const durationMs = Math.round(durations[index] * 1000);
    captions.push({
      text: spec.captions[index],
      startMs: cursorMs,
      endMs: cursorMs + durationMs,
      timestampMs: cursorMs,
      confidence: 1.0,
    });
    cursorMs += durationMs;
  }
  const transcript = {
    text: spec.captions.join(" "),
    segments: [
      {
        text: spec.captions.join(" "),
        start: 0,
        end: cursorMs / 1000,
        duration: cursorMs / 1000,
      },
    ],
  };
  const srtLines = [];
  let current = 0;
  spec.captions.forEach((caption, index) => {
    const start = current;
    const end = current + durations[index];
    srtLines.push(
      String(index + 1),
      `${formatSrtTimestamp(start)} --> ${formatSrtTimestamp(end)}`,
      caption,
      "",
    );
    current = end;
  });
  return {
    captions,
    transcript,
    srt: `${srtLines.join("\n")}\n`,
  };
}

async function recordScenarioClip({ scenario, prompt, expectedText, outputPath }) {
  const gateway = useCurrentInstalledHost && scenario.installPlugin ? null : await startGateway(scenario);
  const { chromium } = await loadPlaywright();
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({
    viewport,
    recordVideo: {
      dir: rawDir,
      size: recordSize,
    },
  });
  const page = await context.newPage();
  try {
    const url = await getDashboardUrl(scenario);
    await page.goto(url, { waitUntil: "networkidle", timeout: 60_000 });
    await page.waitForTimeout(1_200);
    const connectButton = page.getByRole("button", { name: /连接|connect/i }).first();
    if (await connectButton.count()) {
      await connectButton.click().catch(() => {});
      await page.waitForTimeout(1_000);
    }
    await page.waitForURL(/\/chat/, { timeout: 60_000 });
    const input = page.getByRole("textbox").last();
    await input.fill(prompt);
    await input.press("Enter");
    const expectedTexts = Array.isArray(expectedText) ? expectedText : [expectedText];
    const deadline = Date.now() + 120_000;
    let matchedText = null;
    while (Date.now() < deadline) {
      const pageText = await page.locator("main").innerText();
      matchedText = expectedTexts.find((candidate) => pageText.includes(candidate)) || null;
      if (matchedText) {
        break;
      }
      await page.waitForTimeout(1_000);
    }
    if (!matchedText) {
      throw new Error(`Timed out waiting for any expected text: ${expectedTexts.join(" | ")}`);
    }
    await page.getByText(matchedText, { exact: false }).first().scrollIntoViewIfNeeded();
    await page.waitForTimeout(3_000);
    const videoPath = await page.video().path();
    await context.close();
    await browser.close();
    await stopGateway(gateway);
    await mkdir(path.dirname(outputPath), { recursive: true });
    await rm(outputPath, { force: true });
    await run("mv", [videoPath, outputPath]);
    return outputPath;
  } catch (error) {
    await context.close().catch(() => {});
    await browser.close().catch(() => {});
    await stopGateway(gateway);
    throw error;
  }
}

async function renderLanguage(language) {
  const spec = prompts[language];
  const uninstalled = await prepareScenario({
    name: `video-uninstalled-${language}`,
    port: language === "zh" ? 18911 : 18921,
    installPlugin: false,
  });
  const installed = useCurrentInstalledHost
    ? {
        name: `video-installed-${language}-current-host`,
        root: "current-openclaw-host",
        env: process.env,
        port: language === "zh" ? 18912 : 18922,
        installPlugin: true,
      }
    : await prepareScenario({
        name: `video-installed-${language}`,
        port: language === "zh" ? 18912 : 18922,
        installPlugin: true,
      });

  const rawUninstalled = path.join(
    rawDir,
    `openclaw-onboarding-doc-flow.${language}.uninstalled.webm`,
  );
  const rawInstalled = path.join(
    rawDir,
    `openclaw-onboarding-doc-flow.${language}.installed.webm`,
  );

  await recordScenarioClip({
    scenario: uninstalled,
    prompt: spec.uninstalled,
    expectedText: spec.uninstalledWait,
    outputPath: rawUninstalled,
  });
  await recordScenarioClip({
    scenario: installed,
    prompt: spec.installed,
    expectedText: spec.installedWait,
    outputPath: rawInstalled,
  });

  const durations = [
    await getDuration(rawUninstalled),
    await getDuration(rawInstalled),
  ];

  const srtPath = path.join(outputDir, `openclaw-onboarding-doc-flow.${language}.srt`);
  const captionsPath = path.join(outputDir, `openclaw-onboarding-doc-flow.${language}.captions.json`);
  const transcriptPath = path.join(outputDir, `openclaw-onboarding-doc-flow.${language}.transcript.json`);

  const artifacts = await buildCaptionArtifacts(language, durations);
  await writeFile(srtPath, artifacts.srt, "utf8");
  await writeFile(captionsPath, `${JSON.stringify(artifacts.captions, null, 2)}\n`, "utf8");
  await writeFile(transcriptPath, `${JSON.stringify(artifacts.transcript, null, 2)}\n`, "utf8");
  await run("node", [remotionRenderScript, `onboarding-${language}`, "--no-sync"]);

  return {
    language,
    rawUninstalled,
    rawInstalled,
    renderedOutput: path.join(
      outputDir,
      `openclaw-onboarding-doc-flow.${language}.burned-captions.mp4`,
    ),
    srtPath,
    captionsPath,
    transcriptPath,
    durations,
  };
}

async function main() {
  await mkdir(rawDir, { recursive: true });
  await mkdir(outputDir, { recursive: true });
  await mkdir(tempDir, { recursive: true });

  const languages =
    requestedLanguage === "all" ? ["zh", "en"] : [requestedLanguage];
  if (!languages.every((language) => language in prompts)) {
    throw new Error(`Unsupported language target: ${requestedLanguage}`);
  }

  const report = [];
  for (const language of languages) {
    report.push(await renderLanguage(language));
  }
  console.log(JSON.stringify(report, null, 2));
}

main().catch((error) => {
  console.error(error.stack || String(error));
  process.exitCode = 1;
});

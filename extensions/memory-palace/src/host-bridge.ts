import { createHash } from "node:crypto";
import fs, { existsSync } from "node:fs";
import {
  lstat as lstatAsync,
  readdir as readdirAsync,
  readFile as readFileAsync,
  realpath as realpathAsync,
  stat as statAsync,
} from "node:fs/promises";
import path from "node:path";
import type {
  HostWorkspaceHit,
  HostWorkspaceSourceKind,
  PluginConfig,
} from "./types.js";

export const HOST_BRIDGE_STOP_WORDS = new Set([
  "a",
  "an",
  "and",
  "are",
  "do",
  "for",
  "from",
  "how",
  "i",
  "is",
  "it",
  "me",
  "my",
  "of",
  "or",
  "please",
  "remember",
  "should",
  "tell",
  "that",
  "the",
  "this",
  "to",
  "what",
  "you",
]);

const HOST_BRIDGE_ALLOWED_MEMORY_EXTENSIONS = new Set([".md", ".markdown"]);
const HOST_BRIDGE_FILENAME_PRIORITY = new Map<string, number>([
  ["USER.md", 3],
  ["user.md", 3],
  ["MEMORY.md", 2],
  ["memory.md", 2],
]);
const HOST_BRIDGE_IGNORE_LINE_PATTERNS = [
  /^#\s+/u,
  /^_{2,}.+_{2,}$/u,
  /^-{3,}$/u,
  /^\*\*.+:\*\*\s*$/u,
  /^\(optional\)$/iu,
] as const;
const HOST_BRIDGE_MAX_DAILY_MEMORY_FILES = 196;
const HOST_BRIDGE_MAX_DAILY_MEMORY_DEPTH = 12;
const HOST_BRIDGE_MAX_DAILY_MEMORY_ENTRIES = 1000;
const HOST_BRIDGE_PATH_COLLATOR = new Intl.Collator("en", {
  numeric: true,
  sensitivity: "base",
});

type HostBridgeHelperDeps = {
  normalizeText: (text: string) => string;
  tokenizeForHostBridge: (text: string) => string[];
  countTokenOverlap: (left: string[], right: string[]) => number;
  inferCaptureCategory: (text: string) => string;
  hasCaptureSignal: (text: string) => boolean;
  looksLikePromptInjection: (text: string) => boolean;
  isSensitiveHostBridgeText: (text: string) => boolean;
  truncate: (text: string, limit: number) => string;
  escapeMemoryForPrompt: (text: string) => string;
  hostBridgeTag: string;
  hostBridgeDisclaimer: string;
};

export function normalizeHostBridgeComparablePath(inputPath: string): string {
  if (inputPath.startsWith("\\\\?\\UNC\\")) {
    return `\\\\${inputPath.slice("\\\\?\\UNC\\".length)}`;
  }
  if (inputPath.startsWith("\\\\?\\")) {
    return inputPath.slice("\\\\?\\".length);
  }
  return inputPath;
}

function resolveExistingPath(inputPath: string): string {
  const normalizedInput = normalizeHostBridgeComparablePath(inputPath);
  try {
    return normalizeHostBridgeComparablePath(fs.realpathSync(normalizedInput));
  } catch {
    return normalizedInput;
  }
}

async function resolveExistingPathAsync(inputPath: string): Promise<string> {
  const normalizedInput = normalizeHostBridgeComparablePath(inputPath);
  try {
    return normalizeHostBridgeComparablePath(await realpathAsync(normalizedInput));
  } catch {
    return normalizedInput;
  }
}

function resolveWorkspaceRelativePath(workspaceDir: string, absolutePath: string): string {
  return path
    .relative(resolveExistingPath(workspaceDir), resolveExistingPath(absolutePath))
    .replace(/\\/g, "/");
}

function isPathWithinWorkspace(workspaceDir: string, absolutePath: string): boolean {
  const relative = path.relative(resolveExistingPath(workspaceDir), resolveExistingPath(absolutePath));
  return relative === "" || (!relative.startsWith("..") && !path.isAbsolute(relative));
}

async function isPathWithinWorkspaceAsync(
  workspaceDir: string,
  absolutePath: string,
): Promise<boolean> {
  const relative = path.relative(
    await resolveExistingPathAsync(workspaceDir),
    await resolveExistingPathAsync(absolutePath),
  );
  return relative === "" || (!relative.startsWith("..") && !path.isAbsolute(relative));
}

export function createHostBridgeHelpers(deps: HostBridgeHelperDeps) {
  function resolveHostBridgeTarget(
    workspaceDir: string,
    absolutePath: string,
    maxFileBytes?: number,
  ): { canonicalPath: string; stat: fs.Stats; preReadContent?: Buffer } | undefined {
    try {
      const initialStat = fs.lstatSync(absolutePath);
      const canonicalPath = initialStat.isSymbolicLink()
        ? fs.realpathSync(absolutePath)
        : absolutePath;
      if (!isPathWithinWorkspace(workspaceDir, canonicalPath)) {
        return undefined;
      }
      // Open with O_NOFOLLOW for ALL paths (not just initial symlinks) so that
      // a regular file replaced by a symlink between lstat and open is rejected.
      // fstat + readFile through the fd eliminate the remaining TOCTOU window.
      let fd: number;
      try {
        fd = fs.openSync(canonicalPath, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW);
      } catch {
        // O_NOFOLLOW is not universally supported (e.g. some Windows builds).
        // Fall back to plain O_RDONLY; the window is narrower but not zero.
        fd = fs.openSync(canonicalPath, fs.constants.O_RDONLY);
      }
      let stat: fs.Stats;
      let preReadContent: Buffer | undefined;
      try {
        stat = fs.fstatSync(fd);
        // Cross-check: lstat the path again and compare inodes with the
        // opened fd.  If they differ, the path was swapped between our
        // initial resolution and the open() call (race condition).  This
        // catches the O_NOFOLLOW-fallback case where open() silently
        // followed a newly-created symlink.
        const postOpenStat = fs.lstatSync(canonicalPath);
        if (
          postOpenStat.ino !== stat.ino ||
          postOpenStat.dev !== stat.dev ||
          postOpenStat.isSymbolicLink()
        ) {
          return undefined;
        }
        const limit = maxFileBytes ?? Infinity;
        if (stat.isFile() && stat.size > 0 && stat.size <= limit) {
          preReadContent = fs.readFileSync(fd);
        }
      } finally {
        fs.closeSync(fd);
      }
      return { canonicalPath, stat, preReadContent };
    } catch {
      return undefined;
    }
  }

  async function resolveHostBridgeTargetAsync(
    workspaceDir: string,
    absolutePath: string,
    maxFileBytes?: number,
  ): Promise<{ canonicalPath: string; stat: fs.Stats; preReadContent?: Buffer } | undefined> {
    try {
      const initialStat = await lstatAsync(absolutePath);
      const canonicalPath = initialStat.isSymbolicLink()
        ? await realpathAsync(absolutePath)
        : absolutePath;
      if (!(await isPathWithinWorkspaceAsync(workspaceDir, canonicalPath))) {
        return undefined;
      }
      const { open } = await import("node:fs/promises");
      let handle: Awaited<ReturnType<typeof open>>;
      try {
        handle = await open(canonicalPath, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW);
      } catch {
        handle = await open(canonicalPath, fs.constants.O_RDONLY);
      }
      let stat: fs.Stats;
      let preReadContent: Buffer | undefined;
      try {
        stat = await handle.stat();
        const postOpenStat = await lstatAsync(canonicalPath);
        if (
          postOpenStat.ino !== stat.ino ||
          postOpenStat.dev !== stat.dev ||
          postOpenStat.isSymbolicLink()
        ) {
          return undefined;
        }
        const limit = maxFileBytes ?? Infinity;
        if (stat.isFile() && stat.size > 0 && stat.size <= limit) {
          preReadContent = await handle.readFile();
        }
      } finally {
        await handle.close();
      }
      return { canonicalPath, stat, preReadContent };
    } catch {
      return undefined;
    }
  }

  function normalizeHostWorkspaceLine(rawLine: string): string {
    return deps.normalizeText(
      rawLine
        .replace(/^\s*[-*+]\s*(?:\[[ xX]\]\s*)?/u, "")
        .replace(/^\s*\d+\.\s+/u, "")
        .replace(/^>\s*/u, "")
        .replace(/\*\*([^*]+)\*\*/gu, "$1")
        .replace(/__([^_]+)__/gu, "$1")
        .replace(/`([^`]+)`/gu, "$1"),
    );
  }

  function readHostWorkspaceFileText(
    absolutePath: string,
    maxFileBytes: number,
    preReadContent?: Buffer,
  ): string | undefined {
    try {
      const raw = preReadContent ?? fs.readFileSync(absolutePath);
      if (raw.length === 0 || raw.length > maxFileBytes) {
        return undefined;
      }
      const clipped = raw.subarray(0, raw.length);
      let decoded: string;
      if (clipped.length >= 2 && clipped[0] === 0xff && clipped[1] === 0xfe) {
        decoded = clipped.subarray(2).toString("utf16le");
      } else if (clipped.length >= 2 && clipped[0] === 0xfe && clipped[1] === 0xff) {
        const body = clipped.subarray(2);
        const evenLength = body.length - (body.length % 2);
        const swapped = Buffer.allocUnsafe(evenLength);
        for (let index = 0; index < evenLength; index += 2) {
          swapped[index] = body[index + 1]!;
          swapped[index + 1] = body[index]!;
        }
        decoded = swapped.toString("utf16le");
      } else {
        decoded = clipped.toString("utf8");
      }
      const replacementCount = decoded.match(/\uFFFD/g)?.length ?? 0;
      const replacementRatioTooHigh = replacementCount * 8 > decoded.length;
      const minimumCorruptedReplacements = decoded.length <= 128 ? 2 : 17;
      if (replacementRatioTooHigh && replacementCount >= minimumCorruptedReplacements) {
        return undefined;
      }
      return decoded.replace(/\r\n?/g, "\n");
    } catch {
      return undefined;
    }
  }

  async function readHostWorkspaceFileTextAsync(
    absolutePath: string,
    maxFileBytes: number,
    preReadContent?: Buffer,
  ): Promise<string | undefined> {
    try {
      const raw = preReadContent ?? await readFileAsync(absolutePath);
      if (raw.length === 0 || raw.length > maxFileBytes) {
        return undefined;
      }
      const clipped = raw.subarray(0, raw.length);
      let decoded: string;
      if (clipped.length >= 2 && clipped[0] === 0xff && clipped[1] === 0xfe) {
        decoded = clipped.subarray(2).toString("utf16le");
      } else if (clipped.length >= 2 && clipped[0] === 0xfe && clipped[1] === 0xff) {
        const body = clipped.subarray(2);
        const evenLength = body.length - (body.length % 2);
        const swapped = Buffer.allocUnsafe(evenLength);
        for (let index = 0; index < evenLength; index += 2) {
          swapped[index] = body[index + 1]!;
          swapped[index + 1] = body[index]!;
        }
        decoded = swapped.toString("utf16le");
      } else {
        decoded = clipped.toString("utf8");
      }
      const replacementCount = decoded.match(/\uFFFD/g)?.length ?? 0;
      const replacementRatioTooHigh = replacementCount * 8 > decoded.length;
      const minimumCorruptedReplacements = decoded.length <= 128 ? 2 : 17;
      if (replacementRatioTooHigh && replacementCount >= minimumCorruptedReplacements) {
        return undefined;
      }
      return decoded.replace(/\r\n?/g, "\n");
    } catch {
      return undefined;
    }
  }

  function resolveHostBridgeCategory(
    sourceKind: HostWorkspaceSourceKind,
    normalizedLine: string,
  ): { category: string; text: string } | undefined {
    const userFieldMatch =
      sourceKind === "user-md"
        ? /^\s*(name|what to call them|pronouns|timezone|notes)\s*:\s*(.+)$/iu.exec(normalizedLine)
        : null;
    if (userFieldMatch?.[2]?.trim()) {
      return {
        category: "profile",
        text: `${userFieldMatch[1]}: ${userFieldMatch[2].trim()}`,
      };
    }
    if (
      sourceKind === "memory-md" &&
      /^\s*(default workflow|workflow|default process|review order|delivery order)\s*:\s*(.+)$/iu.test(normalizedLine)
    ) {
      return {
        category: "workflow",
        text: normalizedLine,
      };
    }
    if (!deps.hasCaptureSignal(normalizedLine)) {
      return undefined;
    }
    return {
      category: deps.inferCaptureCategory(normalizedLine),
      text: normalizedLine,
    };
  }

  function listHostWorkspaceSources(
    workspaceDir: string,
    config: PluginConfig["hostBridge"],
  ): Array<{
    absolutePath: string;
    workspaceRelativePath: string;
    sourceKind: HostWorkspaceSourceKind;
    preReadContent?: Buffer;
  }> {
    const sources: Array<{
      absolutePath: string;
      workspaceRelativePath: string;
      sourceKind: HostWorkspaceSourceKind;
      preReadContent?: Buffer;
    }> = [];
    const seenSourcePaths = new Set<string>();
    const pushFile = (absolutePath: string, sourceKind: HostWorkspaceSourceKind) => {
      if (!existsSync(absolutePath) || !isPathWithinWorkspace(workspaceDir, absolutePath)) {
        return;
      }
      const resolvedTarget = resolveHostBridgeTarget(workspaceDir, absolutePath, config.maxFileBytes);
      if (
        !resolvedTarget ||
        !resolvedTarget.stat.isFile() ||
        resolvedTarget.stat.size <= 0 ||
        resolvedTarget.stat.size > config.maxFileBytes ||
        seenSourcePaths.has(resolvedTarget.canonicalPath)
      ) {
        return;
      }
      seenSourcePaths.add(resolvedTarget.canonicalPath);
      sources.push({
        absolutePath: resolvedTarget.canonicalPath,
        workspaceRelativePath: resolveWorkspaceRelativePath(workspaceDir, resolvedTarget.canonicalPath),
        sourceKind,
        preReadContent: resolvedTarget.preReadContent,
      });
    };
    if (config.importUserMd) {
      const canonicalUser = path.join(workspaceDir, "USER.md");
      if (existsSync(canonicalUser)) {
        pushFile(canonicalUser, "user-md");
      } else {
        pushFile(path.join(workspaceDir, "user.md"), "user-md");
      }
    }
    if (config.importMemoryMd) {
      const canonicalMemory = path.join(workspaceDir, "MEMORY.md");
      if (existsSync(canonicalMemory)) {
        pushFile(canonicalMemory, "memory-md");
      } else {
        pushFile(path.join(workspaceDir, "memory.md"), "memory-md");
      }
    }
    if (config.importDailyMemory) {
      const memoryDir = path.join(workspaceDir, "memory");
      if (existsSync(memoryDir)) {
        let scannedDailyMemoryEntries = 0;
        const queue = [{ dir: memoryDir, depth: 0 }];
        const visitedDirectories = new Set<string>();
        for (
          let queueIndex = 0;
          queueIndex < queue.length &&
          sources.length < HOST_BRIDGE_MAX_DAILY_MEMORY_FILES &&
          scannedDailyMemoryEntries < HOST_BRIDGE_MAX_DAILY_MEMORY_ENTRIES;
          queueIndex += 1
        ) {
          const current = queue[queueIndex]!;
          if (current.depth > HOST_BRIDGE_MAX_DAILY_MEMORY_DEPTH) {
            continue;
          }
          const resolvedCurrent = resolveHostBridgeTarget(workspaceDir, current.dir);
          if (!resolvedCurrent?.stat.isDirectory()) {
            continue;
          }
          const currentDir = resolvedCurrent.canonicalPath;
          if (visitedDirectories.has(currentDir)) {
            continue;
          }
          visitedDirectories.add(currentDir);
          let entries: fs.Dirent[] = [];
          try {
            entries = fs.readdirSync(currentDir, { withFileTypes: true });
          } catch {
            continue;
          }
          for (const entry of entries.sort((left, right) => HOST_BRIDGE_PATH_COLLATOR.compare(right.name, left.name))) {
            if (
              sources.length >= HOST_BRIDGE_MAX_DAILY_MEMORY_FILES ||
              scannedDailyMemoryEntries >= HOST_BRIDGE_MAX_DAILY_MEMORY_ENTRIES
            ) {
              break;
            }
            const absolutePath = path.join(currentDir, entry.name);
            if (!isPathWithinWorkspace(workspaceDir, absolutePath)) {
              continue;
            }
            scannedDailyMemoryEntries += 1;
            const resolvedEntry = resolveHostBridgeTarget(workspaceDir, absolutePath, config.maxFileBytes);
            if (!resolvedEntry) {
              continue;
            }
            if (resolvedEntry.stat.isDirectory()) {
              queue.push({ dir: resolvedEntry.canonicalPath, depth: current.depth + 1 });
              continue;
            }
            if (
              resolvedEntry.stat.isFile() &&
              HOST_BRIDGE_ALLOWED_MEMORY_EXTENSIONS.has(
                path.extname(resolvedEntry.canonicalPath).toLowerCase(),
              )
            ) {
              pushFile(resolvedEntry.canonicalPath, "daily-memory");
            }
          }
        }
      }
    }
    return sources;
  }

  async function listHostWorkspaceSourcesAsync(
    workspaceDir: string,
    config: PluginConfig["hostBridge"],
  ): Promise<Array<{
    absolutePath: string;
    workspaceRelativePath: string;
    sourceKind: HostWorkspaceSourceKind;
    preReadContent?: Buffer;
  }>> {
    const sources: Array<{
      absolutePath: string;
      workspaceRelativePath: string;
      sourceKind: HostWorkspaceSourceKind;
      preReadContent?: Buffer;
    }> = [];
    const seenSourcePaths = new Set<string>();
    const pushFile = async (
      absolutePath: string,
      sourceKind: HostWorkspaceSourceKind,
    ): Promise<void> => {
      const resolvedTarget = await resolveHostBridgeTargetAsync(workspaceDir, absolutePath, config.maxFileBytes);
      if (
        !resolvedTarget ||
        !resolvedTarget.stat.isFile() ||
        resolvedTarget.stat.size <= 0 ||
        resolvedTarget.stat.size > config.maxFileBytes ||
        seenSourcePaths.has(resolvedTarget.canonicalPath)
      ) {
        return;
      }
      seenSourcePaths.add(resolvedTarget.canonicalPath);
      sources.push({
        absolutePath: resolvedTarget.canonicalPath,
        workspaceRelativePath: path
          .relative(
            await resolveExistingPathAsync(workspaceDir),
            await resolveExistingPathAsync(resolvedTarget.canonicalPath),
          )
          .replace(/\\/g, "/"),
        sourceKind,
        preReadContent: resolvedTarget.preReadContent,
      });
    };
    if (config.importUserMd) {
      const canonicalUser = path.join(workspaceDir, "USER.md");
      if (existsSync(canonicalUser)) {
        await pushFile(canonicalUser, "user-md");
      } else {
        await pushFile(path.join(workspaceDir, "user.md"), "user-md");
      }
    }
    if (config.importMemoryMd) {
      const canonicalMemory = path.join(workspaceDir, "MEMORY.md");
      if (existsSync(canonicalMemory)) {
        await pushFile(canonicalMemory, "memory-md");
      } else {
        await pushFile(path.join(workspaceDir, "memory.md"), "memory-md");
      }
    }
    if (config.importDailyMemory) {
      const memoryDir = path.join(workspaceDir, "memory");
      const resolvedMemoryDir = await resolveHostBridgeTargetAsync(workspaceDir, memoryDir);
      if (resolvedMemoryDir?.stat.isDirectory()) {
        let scannedDailyMemoryEntries = 0;
        const queue = [{ dir: memoryDir, depth: 0 }];
        const visitedDirectories = new Set<string>();
        for (
          let queueIndex = 0;
          queueIndex < queue.length &&
          sources.length < HOST_BRIDGE_MAX_DAILY_MEMORY_FILES &&
          scannedDailyMemoryEntries < HOST_BRIDGE_MAX_DAILY_MEMORY_ENTRIES;
          queueIndex += 1
        ) {
          const current = queue[queueIndex]!;
          if (current.depth > HOST_BRIDGE_MAX_DAILY_MEMORY_DEPTH) {
            continue;
          }
          const resolvedCurrent = await resolveHostBridgeTargetAsync(workspaceDir, current.dir);
          if (!resolvedCurrent?.stat.isDirectory()) {
            continue;
          }
          const currentDir = resolvedCurrent.canonicalPath;
          if (visitedDirectories.has(currentDir)) {
            continue;
          }
          visitedDirectories.add(currentDir);
          let entries: fs.Dirent[] = [];
          try {
            entries = await readdirAsync(currentDir, { withFileTypes: true });
          } catch {
            continue;
          }
          for (const entry of entries.sort((left, right) => HOST_BRIDGE_PATH_COLLATOR.compare(right.name, left.name))) {
            if (
              sources.length >= HOST_BRIDGE_MAX_DAILY_MEMORY_FILES ||
              scannedDailyMemoryEntries >= HOST_BRIDGE_MAX_DAILY_MEMORY_ENTRIES
            ) {
              break;
            }
            const absolutePath = path.join(currentDir, entry.name);
            if (!(await isPathWithinWorkspaceAsync(workspaceDir, absolutePath))) {
              continue;
            }
            scannedDailyMemoryEntries += 1;
            const resolvedEntry = await resolveHostBridgeTargetAsync(workspaceDir, absolutePath, config.maxFileBytes);
            if (!resolvedEntry) {
              continue;
            }
            if (resolvedEntry.stat.isDirectory()) {
              queue.push({ dir: resolvedEntry.canonicalPath, depth: current.depth + 1 });
              continue;
            }
            if (
              resolvedEntry.stat.isFile() &&
              HOST_BRIDGE_ALLOWED_MEMORY_EXTENSIONS.has(
                path.extname(resolvedEntry.canonicalPath).toLowerCase(),
              )
            ) {
              await pushFile(resolvedEntry.canonicalPath, "daily-memory");
            }
          }
        }
      }
    }
    return sources;
  }

  function scanHostWorkspaceForQuery(
    query: string,
    workspaceDir: string,
    config: PluginConfig["hostBridge"],
  ): HostWorkspaceHit[] {
    const normalizedQuery = deps.normalizeText(query).toLowerCase();
    const promptTokens = deps.tokenizeForHostBridge(query);
    if (!normalizedQuery && promptTokens.length === 0) {
      return [];
    }
    const hits = new Map<string, HostWorkspaceHit>();
    for (const source of listHostWorkspaceSources(workspaceDir, config)) {
      const content = readHostWorkspaceFileText(source.absolutePath, config.maxFileBytes, source.preReadContent);
      if (!content) {
        continue;
      }
      const lines = content.split("\n");
      for (let index = 0; index < lines.length; index += 1) {
        const rawLine = lines[index] ?? "";
        if (HOST_BRIDGE_IGNORE_LINE_PATTERNS.some((pattern) => pattern.test(rawLine))) {
          continue;
        }
        const normalizedLine = normalizeHostWorkspaceLine(rawLine);
        if (!normalizedLine) {
          continue;
        }
        if (
          deps.looksLikePromptInjection(normalizedLine) ||
          deps.isSensitiveHostBridgeText(normalizedLine)
        ) {
          continue;
        }
        const resolved = resolveHostBridgeCategory(source.sourceKind, normalizedLine);
        if (!resolved) {
          continue;
        }
        const overlap = deps.countTokenOverlap(
          promptTokens,
          deps.tokenizeForHostBridge(resolved.text),
        );
        const exactMatchScore =
          normalizedQuery &&
          deps.normalizeText(resolved.text).toLowerCase().includes(normalizedQuery)
            ? Math.max(6, normalizedQuery.length / 3)
            : 0;
        if (overlap <= 0 && exactMatchScore <= 0) {
          continue;
        }
        const filenamePriority =
          HOST_BRIDGE_FILENAME_PRIORITY.get(path.basename(source.workspaceRelativePath)) ?? 1;
        const categoryPriority =
          resolved.category === "workflow" ? 2 : resolved.category === "preference" ? 1 : 0;
        const citation = `${source.workspaceRelativePath}#L${index + 1}`;
        const contentHash = createHash("sha256")
          .update(deps.normalizeText(resolved.text))
          .digest("hex");
        const nextHit: HostWorkspaceHit = {
          workspaceDir,
          workspaceRelativePath: source.workspaceRelativePath,
          sourceKind: source.sourceKind,
          absolutePath: source.absolutePath,
          lineStart: index + 1,
          lineEnd: index + 1,
          text: resolved.text,
          snippet: deps.truncate(resolved.text, config.maxSnippetChars),
          score: exactMatchScore + overlap * 8 + filenamePriority * 3 + categoryPriority,
          category: resolved.category,
          contentHash,
          citation,
        };
        const previous = hits.get(contentHash);
        if (!previous || previous.score < nextHit.score) {
          hits.set(contentHash, nextHit);
        }
      }
    }
    return Array.from(hits.values())
      .sort(
        (left, right) =>
          right.score - left.score ||
          HOST_BRIDGE_PATH_COLLATOR.compare(left.citation, right.citation),
      )
      .slice(0, config.maxHits);
  }

  async function scanHostWorkspaceForQueryAsync(
    query: string,
    workspaceDir: string,
    config: PluginConfig["hostBridge"],
  ): Promise<HostWorkspaceHit[]> {
    const normalizedQuery = deps.normalizeText(query).toLowerCase();
    const promptTokens = deps.tokenizeForHostBridge(query);
    if (!normalizedQuery && promptTokens.length === 0) {
      return [];
    }
    const hits = new Map<string, HostWorkspaceHit>();
    for (const source of await listHostWorkspaceSourcesAsync(workspaceDir, config)) {
      const content = await readHostWorkspaceFileTextAsync(
        source.absolutePath,
        config.maxFileBytes,
        source.preReadContent,
      );
      if (!content) {
        continue;
      }
      const lines = content.split("\n");
      for (let index = 0; index < lines.length; index += 1) {
        const rawLine = lines[index] ?? "";
        if (HOST_BRIDGE_IGNORE_LINE_PATTERNS.some((pattern) => pattern.test(rawLine))) {
          continue;
        }
        const normalizedLine = normalizeHostWorkspaceLine(rawLine);
        if (!normalizedLine) {
          continue;
        }
        if (
          deps.looksLikePromptInjection(normalizedLine) ||
          deps.isSensitiveHostBridgeText(normalizedLine)
        ) {
          continue;
        }
        const resolved = resolveHostBridgeCategory(source.sourceKind, normalizedLine);
        if (!resolved) {
          continue;
        }
        const overlap = deps.countTokenOverlap(
          promptTokens,
          deps.tokenizeForHostBridge(resolved.text),
        );
        const exactMatchScore =
          normalizedQuery &&
          deps.normalizeText(resolved.text).toLowerCase().includes(normalizedQuery)
            ? Math.max(6, normalizedQuery.length / 3)
            : 0;
        if (overlap <= 0 && exactMatchScore <= 0) {
          continue;
        }
        const filenamePriority =
          HOST_BRIDGE_FILENAME_PRIORITY.get(path.basename(source.workspaceRelativePath)) ?? 1;
        const categoryPriority =
          resolved.category === "workflow" ? 2 : resolved.category === "preference" ? 1 : 0;
        const citation = `${source.workspaceRelativePath}#L${index + 1}`;
        const contentHash = createHash("sha256")
          .update(deps.normalizeText(resolved.text))
          .digest("hex");
        const nextHit: HostWorkspaceHit = {
          workspaceDir,
          workspaceRelativePath: source.workspaceRelativePath,
          sourceKind: source.sourceKind,
          absolutePath: source.absolutePath,
          lineStart: index + 1,
          lineEnd: index + 1,
          text: resolved.text,
          snippet: deps.truncate(resolved.text, config.maxSnippetChars),
          score: exactMatchScore + overlap * 8 + filenamePriority * 3 + categoryPriority,
          category: resolved.category,
          contentHash,
          citation,
        };
        const previous = hits.get(contentHash);
        if (!previous || previous.score < nextHit.score) {
          hits.set(contentHash, nextHit);
        }
      }
    }
    return Array.from(hits.values())
      .sort(
        (left, right) =>
          right.score - left.score ||
          HOST_BRIDGE_PATH_COLLATOR.compare(left.citation, right.citation),
      )
      .slice(0, config.maxHits);
  }

  function formatHostBridgePromptContext(hits: HostWorkspaceHit[]): string {
    return [
      `<${deps.hostBridgeTag}>`,
      deps.hostBridgeDisclaimer,
      ...hits.map(
        (entry, index) =>
          `${index + 1}. [host-workspace] ${deps.escapeMemoryForPrompt(entry.citation)} :: ${deps.escapeMemoryForPrompt(entry.snippet)}`,
      ),
      `</${deps.hostBridgeTag}>`,
    ].join("\n");
  }

  return {
    readHostWorkspaceFileText,
    readHostWorkspaceFileTextAsync,
    scanHostWorkspaceForQuery,
    scanHostWorkspaceForQueryAsync,
    formatHostBridgePromptContext,
  };
}

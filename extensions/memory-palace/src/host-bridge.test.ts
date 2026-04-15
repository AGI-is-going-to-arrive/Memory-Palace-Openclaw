import { mkdtempSync, mkdirSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { describe, expect, it } from "bun:test";
import { __testing } from "../index.ts";
import {
  createHostBridgeHelpers,
  normalizeHostBridgeComparablePath,
} from "./host-bridge.ts";
import type { PluginConfig } from "./types.js";

const permissiveHelpers = createHostBridgeHelpers({
  normalizeText: (text) => text.replace(/\s+/g, " ").trim(),
  tokenizeForHostBridge: (text) =>
    text
      .toLowerCase()
      .split(/[^a-z0-9]+/u)
      .filter(Boolean),
  countTokenOverlap: (left, right) => {
    const rightSet = new Set(right);
    return left.reduce((count, token) => count + (rightSet.has(token) ? 1 : 0), 0);
  },
  inferCaptureCategory: () => "preference",
  hasCaptureSignal: () => true,
  looksLikePromptInjection: () => false,
  isSensitiveHostBridgeText: () => false,
  truncate: (text, limit) => text.slice(0, limit),
  escapeMemoryForPrompt: (text) => text,
  hostBridgeTag: "host-bridge",
  hostBridgeDisclaimer: "Host bridge context",
});

const realisticHelpers = createHostBridgeHelpers({
  normalizeText: (text) => text.replace(/\s+/g, " ").trim(),
  tokenizeForHostBridge: (text) =>
    text
      .toLowerCase()
      .split(/[^a-z0-9]+/u)
      .filter(Boolean),
  countTokenOverlap: (left, right) => {
    const rightSet = new Set(right);
    return left.reduce((count, token) => count + (rightSet.has(token) ? 1 : 0), 0);
  },
  inferCaptureCategory: (text) => __testing.inferCaptureCategory(text),
  hasCaptureSignal: (text) =>
    __testing.shouldAutoCapture(text, __testing.parsePluginConfig({}).autoCapture),
  looksLikePromptInjection: () => false,
  isSensitiveHostBridgeText: () => false,
  truncate: (text, limit) => text.slice(0, limit),
  escapeMemoryForPrompt: (text) => text,
  hostBridgeTag: "host-bridge",
  hostBridgeDisclaimer: "Host bridge context",
});

const hostBridgeConfig: PluginConfig["hostBridge"] = {
  enabled: true,
  importUserMd: false,
  importMemoryMd: false,
  importDailyMemory: true,
  writeBackSummary: false,
  maxHits: 5,
  maxImportPerRun: 5,
  maxFileBytes: 8_000,
  maxSnippetChars: 400,
  traceEnabled: false,
};

function createTempDir(prefix: string): string {
  return mkdtempSync(path.join(os.tmpdir(), prefix));
}

function cleanupDir(targetPath: string): void {
  rmSync(targetPath, { recursive: true, force: true });
}

function writeDailyMemoryFile(workspaceDir: string, fileName: string, content: string): void {
  const memoryDir = path.join(workspaceDir, "memory");
  mkdirSync(memoryDir, { recursive: true });
  writeFileSync(path.join(memoryDir, fileName), content, "utf8");
}

function createDirectoryLink(targetPath: string, linkPath: string): boolean {
  try {
    symlinkSync(
      targetPath,
      linkPath,
      process.platform === "win32" ? "junction" : "dir",
    );
    return true;
  } catch {
    return false;
  }
}

const supportsDirectoryLinks = (() => {
  const root = createTempDir("mp-host-bridge-link-check-");
  try {
    const target = path.join(root, "target");
    const link = path.join(root, "link");
    mkdirSync(target);
    return createDirectoryLink(target, link);
  } finally {
    cleanupDir(root);
  }
})();

const maybeIt = supportsDirectoryLinks ? it : it.skip;

describe("host-bridge workspace scanning", () => {
  it("normalizes Windows extended-length and UNC-prefixed paths before comparison", () => {
    expect(
      normalizeHostBridgeComparablePath(
        "\\\\?\\UNC\\server\\share\\memory\\2026-03-20.md",
      ),
    ).toBe("\\\\server\\share\\memory\\2026-03-20.md");
    expect(
      normalizeHostBridgeComparablePath(
        "\\\\?\\C:\\Users\\demo\\memory\\2026-03-20.md",
      ),
    ).toBe("C:\\Users\\demo\\memory\\2026-03-20.md");
  });

  maybeIt("follows workspace-local daily memory directory links", () => {
    const workspaceDir = createTempDir("mp-host-bridge-workspace-");
    try {
      const sharedMemoryDir = path.join(workspaceDir, "shared-memory");
      mkdirSync(sharedMemoryDir, { recursive: true });
      writeFileSync(
        path.join(sharedMemoryDir, "2026-03-20.md"),
        "Shell preference: keep status and smoke checks local.\n",
        "utf8",
      );
      expect(createDirectoryLink(sharedMemoryDir, path.join(workspaceDir, "memory"))).toBe(true);

      const hits = permissiveHelpers.scanHostWorkspaceForQuery(
        "shell preference local",
        workspaceDir,
        hostBridgeConfig,
      );

      expect(hits).toHaveLength(1);
      expect(hits[0]?.text).toContain("Shell preference");
      expect(hits[0]?.citation).toContain("shared-memory/2026-03-20.md#L1");
    } finally {
      cleanupDir(workspaceDir);
    }
  });

  it("matches async host bridge scans to the synchronous result set", async () => {
    const workspaceDir = createTempDir("mp-host-bridge-async-scan-");
    try {
      writeDailyMemoryFile(
        workspaceDir,
        "2026-03-20.md",
        "Default workflow: tests before docs.\n",
      );

      const syncHits = realisticHelpers.scanHostWorkspaceForQuery(
        "tests before docs",
        workspaceDir,
        hostBridgeConfig,
      );
      const asyncHits = await realisticHelpers.scanHostWorkspaceForQueryAsync(
        "tests before docs",
        workspaceDir,
        hostBridgeConfig,
      );

      expect(asyncHits).toEqual(syncHits);
    } finally {
      cleanupDir(workspaceDir);
    }
  });

  maybeIt("ignores daily memory directory links that resolve outside the workspace", () => {
    const workspaceDir = createTempDir("mp-host-bridge-workspace-");
    const externalRoot = createTempDir("mp-host-bridge-external-");
    try {
      const externalMemoryDir = path.join(externalRoot, "memory-outside");
      mkdirSync(externalMemoryDir, { recursive: true });
      writeFileSync(
        path.join(externalMemoryDir, "2026-03-20.md"),
        "Shell preference: do not leak external workspace state.\n",
        "utf8",
      );
      expect(createDirectoryLink(externalMemoryDir, path.join(workspaceDir, "memory"))).toBe(true);

      expect(
        permissiveHelpers.scanHostWorkspaceForQuery(
          "leak external workspace state",
          workspaceDir,
          hostBridgeConfig,
        ),
      ).toEqual([]);
    } finally {
      cleanupDir(workspaceDir);
      cleanupDir(externalRoot);
    }
  });

  it("does not import compliment-style like phrases as preference captures", () => {
    const workspaceDir = createTempDir("mp-host-bridge-compliment-");
    try {
      writeDailyMemoryFile(
        workspaceDir,
        "2026-03-20.md",
        "I like your analysis and the structure of this answer.\n",
      );

      expect(
        realisticHelpers.scanHostWorkspaceForQuery(
          "analysis",
          workspaceDir,
          hostBridgeConfig,
        ),
      ).toEqual([]);
    } finally {
      cleanupDir(workspaceDir);
    }
  });

  it("keeps genuine english preference lines importable through host bridge", () => {
    const workspaceDir = createTempDir("mp-host-bridge-preference-");
    try {
      writeDailyMemoryFile(
        workspaceDir,
        "2026-03-20.md",
        "I like using vim for quick edits.\n",
      );

      const hits = realisticHelpers.scanHostWorkspaceForQuery(
        "vim",
        workspaceDir,
        hostBridgeConfig,
      );

      expect(hits).toHaveLength(1);
      expect(hits[0]?.category).toBe("preference");
      expect(hits[0]?.text).toContain("I like using vim");
    } finally {
      cleanupDir(workspaceDir);
    }
  });

  it("rejects short corrupted files with dense replacement characters", () => {
    const workspaceDir = createTempDir("mp-host-bridge-corrupt-short-");
    try {
      const memoryDir = path.join(workspaceDir, "memory");
      mkdirSync(memoryDir, { recursive: true });
      const corruptedPath = path.join(memoryDir, "2026-03-20.md");
      writeFileSync(
        corruptedPath,
        Buffer.from([0xc3, 0x28, 0xc3, 0x28, 0xc3, 0x28, 0xc3, 0x28]),
      );

      expect(__testing.readHostWorkspaceFileText(corruptedPath, hostBridgeConfig.maxFileBytes)).toBeUndefined();
    } finally {
      cleanupDir(workspaceDir);
    }
  });

  it("does not import english negated preference lines through host bridge", () => {
    const workspaceDir = createTempDir("mp-host-bridge-negated-english-");
    try {
      writeDailyMemoryFile(
        workspaceDir,
        "2026-03-20.md",
        "I don't like dark mode for terminals.\n",
      );

      expect(
        realisticHelpers.scanHostWorkspaceForQuery(
          "dark mode",
          workspaceDir,
          hostBridgeConfig,
        ),
      ).toEqual([]);
    } finally {
      cleanupDir(workspaceDir);
    }
  });

  it("does not import english negated need lines through host bridge", () => {
    const workspaceDir = createTempDir("mp-host-bridge-negated-english-");
    try {
      writeDailyMemoryFile(
        workspaceDir,
        "2026-03-20.md",
        "I do not need dark mode in this setup.\n",
      );

      expect(
        realisticHelpers.scanHostWorkspaceForQuery(
          "dark mode",
          workspaceDir,
          hostBridgeConfig,
        ),
      ).toEqual([]);
    } finally {
      cleanupDir(workspaceDir);
    }
  });

  it("keeps mixed english negation lines importable when a later positive preference remains", () => {
    const workspaceDir = createTempDir("mp-host-bridge-english-mixed-");
    try {
      writeDailyMemoryFile(
        workspaceDir,
        "2026-03-20.md",
        "I don't like Java, but I like TypeScript.\n",
      );
      writeDailyMemoryFile(
        workspaceDir,
        "2026-03-21.md",
        "I do not need dark mode, but I need larger fonts.\n",
      );

      const languageHits = realisticHelpers.scanHostWorkspaceForQuery(
        "typescript",
        workspaceDir,
        hostBridgeConfig,
      );
      expect(languageHits).toHaveLength(1);
      expect(languageHits[0]?.category).toBe("preference");
      expect(languageHits[0]?.text).toContain("I don't like Java, but I like TypeScript");

      const fontHits = realisticHelpers.scanHostWorkspaceForQuery(
        "larger fonts",
        workspaceDir,
        hostBridgeConfig,
      );
      expect(fontHits).toHaveLength(1);
      expect(fontHits[0]?.category).toBe("preference");
      expect(fontHits[0]?.text).toContain("I do not need dark mode, but I need larger fonts");
    } finally {
      cleanupDir(workspaceDir);
    }
  });

  it("keeps mixed CJK negation lines importable when a later positive preference remains", () => {
    const workspaceDir = createTempDir("mp-host-bridge-cjk-mixed-");
    try {
      writeDailyMemoryFile(
        workspaceDir,
        "2026-03-20.md",
        "我不喜欢 Java，但喜欢 TypeScript。\n",
      );

      const hits = realisticHelpers.scanHostWorkspaceForQuery(
        "typescript",
        workspaceDir,
        hostBridgeConfig,
      );

      expect(hits).toHaveLength(1);
      expect(hits[0]?.category).toBe("preference");
      expect(hits[0]?.text).toContain("喜欢 TypeScript");
    } finally {
      cleanupDir(workspaceDir);
    }
  });

  it("rejects short files with a high replacement-character ratio", () => {
    const workspaceDir = createTempDir("mp-host-bridge-corrupt-short-");
    try {
      const memoryDir = path.join(workspaceDir, "memory");
      mkdirSync(memoryDir, { recursive: true });
      const filePath = path.join(memoryDir, "2026-03-20.md");
      writeFileSync(filePath, Buffer.from([0xff, 0xff, 0x61, 0x62]));

      expect(
        permissiveHelpers.readHostWorkspaceFileText(filePath, hostBridgeConfig.maxFileBytes),
      ).toBeUndefined();
      expect(
        permissiveHelpers.scanHostWorkspaceForQuery(
          "ab",
          workspaceDir,
          hostBridgeConfig,
        ),
      ).toEqual([]);
    } finally {
      cleanupDir(workspaceDir);
    }
  });

});

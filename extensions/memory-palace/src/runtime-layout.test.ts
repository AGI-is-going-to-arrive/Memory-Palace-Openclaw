import path from "node:path";
import { describe, expect, it } from "bun:test";
import { createResolveDefaultStdioLaunch } from "./runtime-layout.ts";

describe("runtime-layout stdio launch resolution", () => {
  it("uses zsh to launch the bash wrapper when both shells are available", () => {
    const previousShell = process.env.SHELL;
    process.env.SHELL = "/bin/zsh";
    try {
      const resolveDefaultStdioLaunch = createResolveDefaultStdioLaunch({
        currentHostPlatform: "posix",
        pluginProjectRoot: "/repo",
        packagedBackendRoot: "/repo/release/backend",
        isPackagedPluginLayout: false,
        defaultStdioWrapper: "/repo/scripts/run_memory_palace_mcp_stdio.sh",
        pathExists: (inputPath) => inputPath === "/bin/zsh" || inputPath === "/bin/bash",
      });

      expect(resolveDefaultStdioLaunch(undefined, "posix")).toEqual({
        command: "/bin/zsh",
        args: ["-lc", `'/bin/bash' '/repo/scripts/run_memory_palace_mcp_stdio.sh'`],
        cwd: "/repo",
      });
    } finally {
      if (previousShell === undefined) {
        delete process.env.SHELL;
      } else {
        process.env.SHELL = previousShell;
      }
    }
  });

  it("falls back to the runtime python wrapper when no shell exists", () => {
    const previousShell = process.env.SHELL;
    process.env.SHELL = "/bin/fish";
    try {
      const resolveDefaultStdioLaunch = createResolveDefaultStdioLaunch({
        currentHostPlatform: "posix",
        pluginProjectRoot: "/repo",
        packagedBackendRoot: "/repo/release/backend",
        isPackagedPluginLayout: false,
        defaultStdioWrapper: "/repo/scripts/run_memory_palace_mcp_stdio.sh",
        pathExists: () => false,
      });

      expect(
        resolveDefaultStdioLaunch(
          { OPENCLAW_MEMORY_PALACE_RUNTIME_PYTHON: "/custom/runtime/bin/python" },
          "posix",
        ),
      ).toEqual({
        command: "/custom/runtime/bin/python",
        args: [path.resolve("/repo", "backend", "mcp_wrapper.py")],
        cwd: path.resolve("/repo", "backend"),
      });
    } finally {
      if (previousShell === undefined) {
        delete process.env.SHELL;
      } else {
        process.env.SHELL = previousShell;
      }
    }
  });

  it("uses OPENCLAW_MEMORY_PALACE_RUNTIME_PYTHON from process env when runtime env is absent", () => {
    const previousShell = process.env.SHELL;
    const previousRuntimePython = process.env.OPENCLAW_MEMORY_PALACE_RUNTIME_PYTHON;
    process.env.SHELL = "/bin/fish";
    process.env.OPENCLAW_MEMORY_PALACE_RUNTIME_PYTHON = "/process/runtime/bin/python";
    try {
      const resolveDefaultStdioLaunch = createResolveDefaultStdioLaunch({
        currentHostPlatform: "posix",
        pluginProjectRoot: "/repo",
        packagedBackendRoot: "/repo/release/backend",
        isPackagedPluginLayout: false,
        defaultStdioWrapper: "/repo/scripts/run_memory_palace_mcp_stdio.sh",
        pathExists: () => false,
      });

      expect(resolveDefaultStdioLaunch(undefined, "posix")).toEqual({
        command: "/process/runtime/bin/python",
        args: [path.resolve("/repo", "backend", "mcp_wrapper.py")],
        cwd: path.resolve("/repo", "backend"),
      });
    } finally {
      if (previousShell === undefined) {
        delete process.env.SHELL;
      } else {
        process.env.SHELL = previousShell;
      }
      if (previousRuntimePython === undefined) {
        delete process.env.OPENCLAW_MEMORY_PALACE_RUNTIME_PYTHON;
      } else {
        process.env.OPENCLAW_MEMORY_PALACE_RUNTIME_PYTHON = previousRuntimePython;
      }
    }
  });

  it("uses /bin/sh when bash is unavailable but a POSIX shell exists", () => {
    const previousShell = process.env.SHELL;
    process.env.SHELL = "/bin/fish";
    try {
      const resolveDefaultStdioLaunch = createResolveDefaultStdioLaunch({
        currentHostPlatform: "posix",
        pluginProjectRoot: "/repo",
        packagedBackendRoot: "/repo/release/backend",
        isPackagedPluginLayout: false,
        defaultStdioWrapper: "/repo/scripts/run_memory_palace_mcp_stdio.sh",
        pathExists: (inputPath) => inputPath === "/bin/sh",
      });

      expect(resolveDefaultStdioLaunch(undefined, "posix")).toEqual({
        command: "/bin/sh",
        args: ["/repo/scripts/run_memory_palace_mcp_stdio.sh"],
        cwd: "/repo",
      });
    } finally {
      if (previousShell === undefined) {
        delete process.env.SHELL;
      } else {
        process.env.SHELL = previousShell;
      }
    }
  });
});

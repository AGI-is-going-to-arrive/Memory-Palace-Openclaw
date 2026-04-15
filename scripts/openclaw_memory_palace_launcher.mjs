#!/usr/bin/env node

import { spawnSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const pythonEntry = path.resolve(scriptDir, "openclaw_memory_palace.py");
const argv = process.argv.slice(2);
const configuredRuntimePython = String(
  process.env.OPENCLAW_MEMORY_PALACE_RUNTIME_PYTHON || "",
).trim();

const MIN_SUPPORTED_MINOR = 10;
const MAX_SUPPORTED_MINOR = 14;

function renderCandidate(command, prefixArgs) {
  return [command, ...prefixArgs].join(" ");
}

function probePython(command, prefixArgs) {
  const result = spawnSync(command, [...prefixArgs, "--version"], {
    encoding: "utf8",
    env: process.env,
  });
  if (result.error && result.error.code === "ENOENT") {
    return { missing: true, usable: false, reason: "missing" };
  }
  if (result.error) {
    return { missing: false, usable: false, reason: result.error.message };
  }
  if ((result.status ?? 0) !== 0) {
    const stderr = (result.stderr ?? "").trim();
    const stdout = (result.stdout ?? "").trim();
    return {
      missing: false,
      usable: false,
      reason: stderr || stdout || `exit ${result.status ?? 1}`,
    };
  }
  const versionText = `${result.stdout ?? ""}\n${result.stderr ?? ""}`.trim();
  const match = versionText.match(/Python\s+(\d+)\.(\d+)\.(\d+)/i);
  if (!match) {
    return { missing: false, usable: false, reason: `unparseable version: ${versionText}` };
  }
  const major = Number(match[1]);
  const minor = Number(match[2]);
  const version = `${major}.${minor}.${match[3]}`;
  const usable = major === 3 && minor >= MIN_SUPPORTED_MINOR && minor <= MAX_SUPPORTED_MINOR;
  return {
    missing: false,
    usable,
    version,
    reason: usable
      ? "ok"
      : `unsupported Python ${version}; requires 3.${MIN_SUPPORTED_MINOR}-3.${MAX_SUPPORTED_MINOR}`,
  };
}

const discoveredCandidates =
  process.platform === "win32"
    ? [
        { command: "py", prefixArgs: ["-3.14"] },
        { command: "py", prefixArgs: ["-3.13"] },
        { command: "py", prefixArgs: ["-3.12"] },
        { command: "py", prefixArgs: ["-3.11"] },
        { command: "py", prefixArgs: ["-3.10"] },
        { command: "python3.14", prefixArgs: [] },
        { command: "python3.13", prefixArgs: [] },
        { command: "python3.12", prefixArgs: [] },
        { command: "python3.11", prefixArgs: [] },
        { command: "python3.10", prefixArgs: [] },
        { command: "python", prefixArgs: [] },
        { command: "python3", prefixArgs: [] },
      ]
    : [
        { command: "python3.14", prefixArgs: [] },
        { command: "python3.13", prefixArgs: [] },
        { command: "python3.12", prefixArgs: [] },
        { command: "python3.11", prefixArgs: [] },
        { command: "python3.10", prefixArgs: [] },
        { command: "python3", prefixArgs: [] },
        { command: "python", prefixArgs: [] },
      ];

const pythonCandidates = [];
const seenCandidates = new Set();
if (configuredRuntimePython) {
  pythonCandidates.push({ command: configuredRuntimePython, prefixArgs: [] });
  seenCandidates.add(renderCandidate(configuredRuntimePython, []));
}
for (const candidate of discoveredCandidates) {
  const rendered = renderCandidate(candidate.command, candidate.prefixArgs);
  if (seenCandidates.has(rendered)) {
    continue;
  }
  seenCandidates.add(rendered);
  pythonCandidates.push(candidate);
}

const rejectedCandidates = [];

for (const candidate of pythonCandidates) {
  const { command, prefixArgs } = candidate;
  const probe = probePython(command, prefixArgs);
  if (probe.missing) {
    continue;
  }
  if (!probe.usable) {
    rejectedCandidates.push(`${renderCandidate(command, prefixArgs)} -> ${probe.reason}`);
    continue;
  }

  const result = spawnSync(command, [...prefixArgs, pythonEntry, ...argv], {
    stdio: "inherit",
    env: process.env,
  });
  if (result.error && result.error.code === "ENOENT") {
    continue;
  }
  if (result.error) {
    console.error(`Failed to launch ${renderCandidate(command, prefixArgs)}: ${result.error.message}`);
    process.exit(1);
  }
  process.exit(result.status ?? 0);
}

const attempts = pythonCandidates.map((candidate) => renderCandidate(candidate.command, candidate.prefixArgs));
console.error(
  `Could not find a usable Python launcher. Tried: ${attempts.join(", ")}. ` +
    `Memory Palace currently requires Python 3.${MIN_SUPPORTED_MINOR}-3.${MAX_SUPPORTED_MINOR}.`,
);
if (configuredRuntimePython) {
  console.error(
    `Configured OPENCLAW_MEMORY_PALACE_RUNTIME_PYTHON=${configuredRuntimePython}`,
  );
}
if (rejectedCandidates.length > 0) {
  console.error(`Rejected candidates: ${rejectedCandidates.join("; ")}`);
}
process.exit(1);

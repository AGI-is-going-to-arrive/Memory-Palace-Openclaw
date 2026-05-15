#!/usr/bin/env node
import path from "node:path";
import process from "node:process";

const readStdin = async () =>
  new Promise((resolve) => {
    let input = "";
    process.stdin.setEncoding("utf8");
    process.stdin.on("data", (chunk) => {
      input += chunk;
    });
    process.stdin.on("end", () => resolve(input));
  });

const isRecord = (value) =>
  typeof value === "object" && value !== null && !Array.isArray(value);

const extractFilePath = (payload) => {
  const toolInput = isRecord(payload.tool_input) ? payload.tool_input : {};
  const candidate = toolInput.file_path ?? toolInput.path;
  return typeof candidate === "string" ? candidate.trim() : "";
};

const isSensitivePath = (relativePath) => {
  const normalized = relativePath.replaceAll("\\", "/").toLowerCase();
  return (
    normalized === ".env" ||
    normalized.startsWith(".env.") ||
    normalized.includes("/.env") ||
    normalized.startsWith(".git/") ||
    normalized.includes("/.git/") ||
    normalized.includes("secret") ||
    normalized.includes("token") ||
    normalized.includes("credential")
  );
};

const main = async () => {
  let payload;
  try {
    payload = JSON.parse(await readStdin());
  } catch {
    return;
  }
  if (!isRecord(payload)) {
    return;
  }

  const toolName = typeof payload.tool_name === "string" ? payload.tool_name : "";
  if (toolName !== "Edit" && toolName !== "Write") {
    return;
  }

  const filePath = extractFilePath(payload);
  if (!filePath) {
    return;
  }

  const cwd =
    typeof payload.cwd === "string" && payload.cwd
      ? payload.cwd
      : process.env.CLAUDE_PROJECT_DIR || process.cwd();
  const absolutePath = path.resolve(cwd, filePath);
  const relativePath = path.relative(cwd, absolutePath);
  if (!relativePath || relativePath.startsWith("..") || path.isAbsolute(relativePath)) {
    return;
  }
  if (isSensitivePath(relativePath)) {
    return;
  }

  process.stdout.write(
    `${JSON.stringify({
      systemMessage: `Memory Palace observed a project file edit: ${relativePath}. Capture only durable project knowledge; do not store secrets or transient diffs.`,
    })}\n`,
  );
};

main().catch(() => {
  process.exit(0);
});

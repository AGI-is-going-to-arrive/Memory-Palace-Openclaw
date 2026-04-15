import { mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const packageRoot = path.resolve(scriptDir, "..");
const manifestPath = path.join(packageRoot, "openclaw.plugin.json");
const distDir = path.join(packageRoot, "dist");
const distManifestPath = path.join(distDir, "openclaw.plugin.json");

const manifest = JSON.parse(await readFile(manifestPath, "utf8"));
if (Array.isArray(manifest.skills)) {
  manifest.skills = manifest.skills.map((entry) => {
    if (entry === "./skills") {
      return "../skills";
    }
    return entry;
  });
}

await mkdir(distDir, { recursive: true });
await writeFile(distManifestPath, `${JSON.stringify(manifest, null, 2)}\n`, "utf8");

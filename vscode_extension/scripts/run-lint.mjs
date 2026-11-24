// Runs Biome's combined lint + format check (`biome check`, report-only) over the
// extension's TypeScript/JS sources; the config is biome.json.
//
// Runs the local `@biomejs/biome` bin script with `node` rather than `npx biome`, which is a
// `.cmd` wrapper on Windows that would need a shell. The bin script picks the
// platform's native binary itself.

import { execFileSync } from "node:child_process";
import * as path from "node:path";
import { fileURLToPath } from "node:url";

const projectRoot = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const biomeBin = path.join(projectRoot, "node_modules", "@biomejs", "biome", "bin", "biome");

execFileSync(process.execPath, [biomeBin, "check", "."], {
  stdio: "inherit",
  cwd: projectRoot,
});

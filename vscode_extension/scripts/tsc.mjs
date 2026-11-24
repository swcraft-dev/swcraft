// Cross-platform `tsc` invocation, shared by run-tests.mjs and run-integration-tests.mjs.
//
// Runs the local `typescript` bin script with `node` rather than `npx tsc`: `npx` is a
// `.cmd` wrapper on Windows, so spawning it portably would need a shell.

import { execFileSync } from "node:child_process";
import * as path from "node:path";
import { fileURLToPath } from "node:url";

export const projectRoot = path.dirname(path.dirname(fileURLToPath(import.meta.url)));

export function compile() {
  const tscBin = path.join(projectRoot, "node_modules", "typescript", "bin", "tsc");
  execFileSync(process.execPath, [tscBin], { stdio: "inherit", cwd: projectRoot });
}

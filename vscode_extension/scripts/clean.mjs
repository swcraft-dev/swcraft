// Removes the extension's generated output directories (`out/` and `dist/`).
//
// `scripts/build_artifacts.py` packages through `npm ci` -> `npm run clean` -> `npm run
// package`, so the shipped `.vsix` (whose `main` is `./dist/extension.js`) is built from the
// checked-out sources, never from a leftover `dist/`. A Node script rather than `rm -rf`:
// npm runs script strings through a platform shell, and Windows PowerShell 5.x has neither
// `rm -rf` nor `&&`.
import { rmSync } from "node:fs";
import * as path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

export const projectRoot = path.dirname(path.dirname(fileURLToPath(import.meta.url)));

/** Directories removed by `npm run clean`, relative to the project root. */
export const CLEAN_TARGETS = ["out", "dist"];

export function clean(root = projectRoot) {
  for (const target of CLEAN_TARGETS) {
    rmSync(path.join(root, target), { recursive: true, force: true });
  }
}

// Only clean when executed directly, never on import: a test importing CLEAN_TARGETS must
// not wipe the real out/ and dist/. `pathToFileURL` because a raw `file://${argv[1]}` does
// not match `import.meta.url` on Windows.

if (import.meta.url === pathToFileURL(process.argv[1]).href) {
  clean();
}

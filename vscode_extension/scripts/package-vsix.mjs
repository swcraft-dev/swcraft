// Packages the extension into an installable `.vsix` using @vscode/vsce.
//
// Uses the @vscode/vsce JS API rather than a spawned `vsce` CLI, so nothing goes through a
// platform shell (Windows portability).
import { spawnSync } from "node:child_process";
import { copyFileSync, existsSync, readdirSync, statSync } from "node:fs";
import * as path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { createVSIX } from "@vscode/vsce";

export const projectRoot = path.dirname(path.dirname(fileURLToPath(import.meta.url)));

/**
 * Inputs the bundle is built from: everything `esbuild.mjs` reads, plus `package.json`,
 * which decides what vsce puts into the `.vsix`. Directories are walked recursively; a
 * missing entry is skipped, so the check also works on a root holding only a bundle.
 */
export const FRESHNESS_INPUT_DIRS = ["src", path.join("fixtures", "agents")];
export const FRESHNESS_INPUT_FILES = ["esbuild.mjs", "package.json"];

function collectInputFiles(root) {
  const files = [];

  const walk = (dir) => {
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) {
        walk(full);
      } else if (entry.isFile()) {
        files.push(full);
      }
    }
  };

  for (const relative of FRESHNESS_INPUT_DIRS) {
    const dir = path.join(root, relative);
    if (existsSync(dir)) {
      walk(dir);
    }
  }
  for (const relative of FRESHNESS_INPUT_FILES) {
    const file = path.join(root, relative);
    if (existsSync(file)) {
      files.push(file);
    }
  }
  return files;
}

/**
 * Returns the project-relative path of the newest build input that is newer
 * than `dist/extension.js`, or `null` when the bundle is up to date.
 *
 * "The bundle exists" alone would let a stale bundle be packaged. Ties (equal mtimes)
 * count as fresh: a bundle written in the same clock tick as its input was built from it.
 */
export function findStaleInput(root) {
  const bundleMtime = statSync(path.join(root, "dist", "extension.js")).mtimeMs;

  let stalest = null;
  let newestInputMtime = bundleMtime;
  for (const file of collectInputFiles(root)) {
    const mtime = statSync(file).mtimeMs;
    if (mtime > newestInputMtime) {
      stalest = path.relative(root, file);
      newestInputMtime = mtime;
    }
  }
  return stalest;
}

/**
 * Pure precondition check: `dist/extension.js` (produced by the `build` task)
 * must exist **and** be newer than every input it is built from, otherwise
 * vsce would silently package a missing or stale extension bundle. Throws with
 * an actionable message -- naming the offending input in the stale case -- when
 * the precondition is not met; returns (no value) when it is.
 */
export function checkPackagePreconditions(root) {
  const bundlePath = path.join(root, "dist", "extension.js");
  if (!existsSync(bundlePath)) {
    throw new Error(`dist/extension.js not found at ${bundlePath} -- run "npm run build" first.`);
  }

  const stale = findStaleInput(root);
  if (stale !== null) {
    throw new Error(
      `dist/extension.js is stale: "${stale}" is newer than the bundle -- run ` +
        '"npm run build" before packaging.',
    );
  }
}

/**
 * Builds the bundle at `root` by running `esbuild.mjs` in a child Node process.
 *
 * Spawned rather than imported because `esbuild.mjs` builds at import time against its
 * *own* directory, so importing it could not honour `root`.
 */
export function buildBundle(root = projectRoot) {
  const result = spawnSync(process.execPath, [path.join(root, "esbuild.mjs")], {
    cwd: root,
    stdio: "inherit",
  });
  if (result.error) {
    throw result.error;
  }
  if (result.status !== 0) {
    throw new Error(
      `esbuild.mjs failed with exit code ${result.status} -- cannot package a .vsix.`,
    );
  }
}

/**
 * The repository root's licence and notice, which every `.vsix` carries:
 * vsce takes `LICENSE` as the extension's licence (packaged as `extension/LICENSE.txt`) and ships
 * `NOTICE` as a plain file, because `.vscodeignore` excludes neither.
 */
export const LICENCE_FILES = ["LICENSE", "NOTICE"];

/**
 * Copies {@link LICENCE_FILES} from `repoRoot` into the extension project at `root`, before vsce
 * runs. Staged here rather than by `scripts/build_artifacts.py` because every route to a `.vsix` --
 * `dev.py artifacts`, `dev.py package`, a direct `npm run package` -- comes through this script. The
 * copies are gitignored build products; the root files are the one source. Throws, naming the file,
 * when the repository root lacks one, and copies nothing then: without `LICENSE` vsce would only
 * warn, and a `.vsix` with no licence would ship.
 */
export function stageLicenceFiles(root = projectRoot, repoRoot = path.dirname(root)) {
  for (const name of LICENCE_FILES) {
    const source = path.join(repoRoot, name);
    if (!existsSync(source)) {
      throw new Error(
        `${name} not found at the repository root (${source}) -- the .vsix would ship without it.`,
      );
    }
  }
  for (const name of LICENCE_FILES) {
    copyFileSync(path.join(repoRoot, name), path.join(root, name));
  }
}

/**
 * Packages the extension at `root` into a `.vsix` file: build, stage the licence files
 * ({@link stageLicenceFiles}), check the preconditions, then hand off to the vsce JS API
 * (that last step is not unit-tested).
 */
export async function packageExtension(root = projectRoot) {
  buildBundle(root);
  stageLicenceFiles(root);
  checkPackagePreconditions(root);
  await createVSIX({ cwd: root });
}

// Only run when executed directly, not when imported by tests. `pathToFileURL()` because a
// raw `file://${...}` template yields `file://C:\foo\bar.mjs` on Windows, which never equals
// `import.meta.url`, so the script would exit 0 without packaging.

if (import.meta.url === pathToFileURL(process.argv[1]).href) {
  await packageExtension();
}

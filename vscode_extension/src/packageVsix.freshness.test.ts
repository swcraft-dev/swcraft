// Freshness guard for `checkPackagePreconditions()`.
//
// `npm run package` is driven natively with no build graph, so an existence-only check would ship
// a bundle built from an older source state. The bundle must be newer than every file under
// `src/` and `fixtures/agents/`, plus `esbuild.mjs` and `package.json`, and the error names the
// offending input.
//
// The tests build synthetic roots in tmpdir and set mtimes explicitly (`fs.utimesSync`): write
// ordering is not reliable at the filesystem timestamp granularity of a fast test run.

import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { test } from "node:test";
import { pathToFileURL } from "node:url";

const PROJECT_ROOT = path.join(__dirname, "..");

// See packageVsix.test.ts for why the ESM script is loaded through `new Function`.
const dynamicImport = new Function("specifier", "return import(specifier)") as (
  specifier: string,
) => Promise<{
  checkPackagePreconditions: (root: string) => void;
  findStaleInput: (root: string) => string | null;
}>;

async function loadPackageVsixScript() {
  const url = pathToFileURL(path.join(PROJECT_ROOT, "scripts", "package-vsix.mjs")).href;
  return dynamicImport(url);
}

const BUNDLE_SECONDS = 1_700_000_000;

function writeAt(file: string, contents: string, seconds: number): void {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, contents);
  fs.utimesSync(file, seconds, seconds);
}

/**
 * Builds a synthetic extension root: a bundle plus one file per declared input
 * source, all older than the bundle.
 */
function mkFreshWorkspace(): string {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "swcraft-vscode-freshness-test-"));
  writeAt(path.join(dir, "dist", "extension.js"), "// bundle\n", BUNDLE_SECONDS);
  writeAt(path.join(dir, "src", "extension.ts"), "// source\n", BUNDLE_SECONDS - 60);
  writeAt(path.join(dir, "src", "nested", "deep.ts"), "// nested source\n", BUNDLE_SECONDS - 60);
  writeAt(path.join(dir, "fixtures", "agents", "a.md"), "# asset\n", BUNDLE_SECONDS - 60);
  writeAt(path.join(dir, "esbuild.mjs"), "// build\n", BUNDLE_SECONDS - 60);
  writeAt(path.join(dir, "package.json"), "{}\n", BUNDLE_SECONDS - 60);
  return dir;
}

function cleanup(dir: string): void {
  fs.rmSync(dir, { recursive: true, force: true });
}

test("checkPackagePreconditions: passes when the bundle is newer than every input", async () => {
  const dir = mkFreshWorkspace();
  try {
    const { checkPackagePreconditions, findStaleInput } = await loadPackageVsixScript();
    assert.equal(findStaleInput(dir), null);
    assert.doesNotThrow(() => checkPackagePreconditions(dir));
  } finally {
    cleanup(dir);
  }
});

test("checkPackagePreconditions: passes when an input has exactly the bundle's mtime", async () => {
  const dir = mkFreshWorkspace();
  try {
    const source = path.join(dir, "src", "extension.ts");
    fs.utimesSync(source, BUNDLE_SECONDS, BUNDLE_SECONDS);
    const { checkPackagePreconditions } = await loadPackageVsixScript();
    assert.doesNotThrow(
      () => checkPackagePreconditions(dir),
      "a tie must count as fresh -- esbuild can write the bundle within the same " +
        "filesystem timestamp tick as the source it read",
    );
  } finally {
    cleanup(dir);
  }
});

// Every declared input location, so a future edit that drops one from the walk fails here.
for (const stale of [
  path.join("src", "extension.ts"),
  path.join("src", "nested", "deep.ts"),
  path.join("fixtures", "agents", "a.md"),
  "esbuild.mjs",
  "package.json",
]) {
  test(`checkPackagePreconditions: fails naming the stale input when ${stale} is newer than the bundle`, async () => {
    const dir = mkFreshWorkspace();
    try {
      const target = path.join(dir, stale);
      fs.utimesSync(target, BUNDLE_SECONDS + 60, BUNDLE_SECONDS + 60);

      const { checkPackagePreconditions, findStaleInput } = await loadPackageVsixScript();
      assert.equal(findStaleInput(dir), stale);
      assert.throws(
        () => checkPackagePreconditions(dir),
        (error: Error) => {
          assert.match(error.message, /stale/);
          assert.ok(
            error.message.includes(stale),
            `expected the error to name the stale input "${stale}", got: ${error.message}`,
          );
          assert.match(error.message, /npm run build/);
          return true;
        },
      );
    } finally {
      cleanup(dir);
    }
  });
}

test("checkPackagePreconditions: still fails when the bundle is missing entirely", async () => {
  const dir = mkFreshWorkspace();
  try {
    fs.rmSync(path.join(dir, "dist", "extension.js"));
    const { checkPackagePreconditions } = await loadPackageVsixScript();
    assert.throws(() => checkPackagePreconditions(dir), /dist\/extension\.js not found/);
  } finally {
    cleanup(dir);
  }
});

test("findStaleInput: ignores generated output (out/, dist/) and node_modules", async () => {
  const dir = mkFreshWorkspace();
  try {
    writeAt(path.join(dir, "out", "extension.js"), "// compiled\n", BUNDLE_SECONDS + 60);
    writeAt(path.join(dir, "dist", "onboarding-assets", "a.md"), "# copied\n", BUNDLE_SECONDS + 60);
    writeAt(path.join(dir, "node_modules", "x", "index.js"), "// dep\n", BUNDLE_SECONDS + 60);

    const { findStaleInput } = await loadPackageVsixScript();
    assert.equal(
      findStaleInput(dir),
      null,
      "generated output is written *after* the bundle by design (esbuild copies " +
        "dist/onboarding-assets/ last), so it must not be treated as a stale input",
    );
  } finally {
    cleanup(dir);
  }
});

// Behavioural test for `scripts/clean.mjs` (the `npm run clean` script).
//
// `clean()` removes directories recursively at a target derived from the script's own location,
// so one `dirname` too few or too many would silently retarget it at a developer's checkout;
// `npmScripts.test.ts` only checks the file exists and is invoked. This runs the real function
// against a synthetic root in tmpdir: the generated directories go, everything else stays.

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
  projectRoot: string;
  CLEAN_TARGETS: string[];
  clean: (root?: string) => void;
}>;

async function loadCleanScript() {
  const url = pathToFileURL(path.join(PROJECT_ROOT, "scripts", "clean.mjs")).href;
  return dynamicImport(url);
}

function makeSyntheticRoot(): string {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "swcraft-vscode-clean-test-"));
  fs.mkdirSync(path.join(root, "out", "nested"), { recursive: true });
  fs.mkdirSync(path.join(root, "dist", "onboarding-assets"), { recursive: true });
  fs.mkdirSync(path.join(root, "src"), { recursive: true });
  fs.writeFileSync(path.join(root, "out", "nested", "extension.test.js"), "//", "utf8");
  fs.writeFileSync(path.join(root, "dist", "extension.js"), "//", "utf8");
  fs.writeFileSync(path.join(root, "src", "extension.ts"), "//", "utf8");
  fs.writeFileSync(path.join(root, "package.json"), "{}", "utf8");
  return root;
}

test("clean() removes the generated directories and nothing else", async () => {
  const { clean } = await loadCleanScript();
  const root = makeSyntheticRoot();
  try {
    clean(root);

    assert.equal(fs.existsSync(path.join(root, "out")), false);
    assert.equal(fs.existsSync(path.join(root, "dist")), false);
    assert.equal(fs.existsSync(path.join(root, "src", "extension.ts")), true);
    assert.equal(fs.existsSync(path.join(root, "package.json")), true);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("clean() on a root with nothing to remove is a no-op, not an error", async () => {
  const { clean } = await loadCleanScript();
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "swcraft-vscode-clean-empty-"));
  try {
    clean(root);
    assert.equal(fs.existsSync(root), true);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("CLEAN_TARGETS holds exactly the two generated directories", async () => {
  const { CLEAN_TARGETS } = await loadCleanScript();
  assert.deepEqual([...CLEAN_TARGETS].sort(), ["dist", "out"]);
});

test("projectRoot resolves to the extension project, not to scripts/", async () => {
  const { projectRoot } = await loadCleanScript();
  assert.equal(path.resolve(projectRoot), path.resolve(PROJECT_ROOT));
  assert.equal(fs.existsSync(path.join(projectRoot, "package.json")), true);
});

import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { test } from "node:test";

// tsc emits src/**/*.ts -> out/ (rootDir: src), so this compiled test lives at
// out/runLint.test.js; PROJECT_ROOT (vscode_extension/) is one level up.
const PROJECT_ROOT = path.join(__dirname, "..");
const RUN_LINT_SCRIPT = path.join(PROJECT_ROOT, "scripts", "run-lint.mjs");

// scripts/run-lint.mjs (unlike scripts/run_python_lint.py) always checks the whole project
// from PROJECT_ROOT: biome.json's files.includes is project-relative, so it cannot be pointed
// at an isolated directory the way the Python runner test does.
//
// The negative case builds a throwaway project (the real `biome.json`, a copy of
// `run-lint.mjs`, a `node_modules` link so the same Biome binary resolves, and the malformed
// file) rather than dropping a scratch file into the shared checkout: that would leave the
// checkout lint-broken while `node --test` runs the other test files against it in parallel,
// an intermittent red.
//
// What that does not reach: the copy is byte-identical but a copy, so the negative case does
// not witness the script at its shipped path (the positive case below does, over the real
// tree); and the throwaway project has no `src/`, so it exercises `files.includes`'
// `scripts/**` entry alone.
function runLint(script: string, cwd: string): { status: number | null; output: string } {
  try {
    const output = execFileSync(process.execPath, [script], { cwd, encoding: "utf8" });
    return { status: 0, output };
  } catch (err) {
    const execErr = err as { status: number | null; stdout?: string; stderr?: string };
    return { status: execErr.status, output: `${execErr.stdout ?? ""}${execErr.stderr ?? ""}` };
  }
}

test("run-lint.mjs: exits 0 on the repo's current (clean) source tree", () => {
  const result = runLint(RUN_LINT_SCRIPT, PROJECT_ROOT);
  assert.equal(
    result.status,
    0,
    `expected scripts/run-lint.mjs to exit 0 on a clean tree; got status=${result.status}, output=${result.output}`,
  );
});

test("run-lint.mjs: exits non-zero when a scanned file has a lint/format violation", () => {
  const project = fs.mkdtempSync(path.join(os.tmpdir(), "swcraft-vscode-run-lint-"));
  try {
    fs.mkdirSync(path.join(project, "scripts"));
    fs.copyFileSync(path.join(PROJECT_ROOT, "biome.json"), path.join(project, "biome.json"));
    fs.copyFileSync(RUN_LINT_SCRIPT, path.join(project, "scripts", "run-lint.mjs"));
    // A junction on Windows and a directory symlink elsewhere: `node_modules` is
    // hundreds of megabytes and a junction needs no elevated privileges, which a
    // plain Windows symlink does. Node resolves through it to the real
    // `@biomejs/cli-<platform>` package beside it.
    fs.symlinkSync(
      path.join(PROJECT_ROOT, "node_modules"),
      path.join(project, "node_modules"),
      process.platform === "win32" ? "junction" : "dir",
    );
    // Deliberately malformed: unused import (lint/correctness/noUnusedVariables)
    // plus non-double-quote strings (formatter violation) -- either alone is
    // enough to make `biome check` report a finding. `scripts/**` is one of
    // biome.json's includes, so this file is scanned in the copy exactly as it
    // was in the checkout.
    fs.writeFileSync(
      path.join(project, "scripts", "zzz-run-lint-test-scratch.mjs"),
      "import { readFileSync } from 'node:fs';\nconst x = 1;\n",
    );

    const result = runLint(path.join(project, "scripts", "run-lint.mjs"), project);
    assert.notEqual(
      result.status,
      0,
      "expected scripts/run-lint.mjs to exit non-zero when a scanned file has a lint/format violation",
    );
    // Not merely non-zero: a run that failed because it could not find Biome at
    // all would also be non-zero, and would make this case pass while proving
    // nothing about the check.
    assert.match(
      result.output,
      /zzz-run-lint-test-scratch\.mjs/,
      `the failure must name the offending file; got: ${result.output}`,
    );
  } finally {
    fs.rmSync(project, { recursive: true, force: true });
  }
});

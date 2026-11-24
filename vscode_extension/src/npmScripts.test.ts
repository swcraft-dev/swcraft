// Contract guard for the extension's npm scripts, the extension's real gate (a bare
// `node --test` would run whatever stale JS is in out/):
//
//  1. the six scripts the native flow needs exist, and
//  2. each is a single `node <file>` invocation whose target file exists -- no shell operators,
//     because npm runs script strings through a platform shell and Windows PowerShell 5.x
//     supports neither `&&` nor `rm -rf` (why every `scripts/*.mjs` runner exists).

import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as path from "node:path";
import { test } from "node:test";

// tsc emits src/**/*.ts -> out/ (rootDir: src), so this compiled test lives at
// out/npmScripts.test.js; the project root is one level up.
const PROJECT_ROOT = path.join(__dirname, "..");
const PACKAGE_JSON_PATH = path.join(PROJECT_ROOT, "package.json");

const REQUIRED_SCRIPTS = ["clean", "build", "lint", "test", "package", "test-integration"] as const;

// Anything npm would hand to a shell for interpretation rather than passing
// through as a plain argument.
const SHELL_OPERATORS = ["&&", "||", "|", ";", ">", "<", "&", "$(", "`"];

function scripts(): Record<string, string> {
  const pkg = JSON.parse(fs.readFileSync(PACKAGE_JSON_PATH, "utf8")) as {
    scripts: Record<string, string>;
  };
  return pkg.scripts;
}

test("package.json declares every script the native (moon-free) flow needs", () => {
  const declared = scripts();
  for (const name of REQUIRED_SCRIPTS) {
    assert.ok(
      typeof declared[name] === "string" && declared[name].length > 0,
      `package.json must declare an npm "${name}" script -- scripts/build_artifacts.py and ` +
        "scripts/dev.py drive the extension through npm, not moon",
    );
  }
});

test('npm "test" delegates to run-tests.mjs so it cleans and compiles first', () => {
  const testScript = scripts().test;
  assert.match(
    testScript,
    /run-tests\.mjs$/,
    'npm "test" must run scripts/run-tests.mjs (clean -> tsc -> node --test), not a bare ' +
      '`node --test "out/*.test.js"` over a possibly stale out/ directory',
  );
});

test("every npm script is a single `node <file>` invocation with no shell operators", () => {
  for (const [name, command] of Object.entries(scripts())) {
    for (const operator of SHELL_OPERATORS) {
      assert.ok(
        !command.includes(operator),
        `npm "${name}" script contains the shell operator "${operator}" (${command}) -- npm runs ` +
          "script strings through the platform shell, and Windows PowerShell 5.x does not " +
          "support it; put the logic in a scripts/*.mjs runner instead",
      );
    }

    const parts = command.split(/\s+/).filter((part) => part.length > 0);
    assert.equal(parts[0], "node", `npm "${name}" script must be invoked via node: ${command}`);
    assert.equal(
      parts.length,
      2,
      `npm "${name}" script must be exactly \`node <file>\`: ${command}`,
    );
  }
});

test("every npm script's target file exists", () => {
  for (const [name, command] of Object.entries(scripts())) {
    const target = command.split(/\s+/)[1];
    const targetPath = path.join(PROJECT_ROOT, target);
    assert.ok(
      fs.existsSync(targetPath),
      `npm "${name}" script points at ${target}, which does not exist at ${targetPath}`,
    );
  }
});

import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as path from "node:path";
import { test } from "node:test";

// tsc emits src/**/*.ts -> out/ (rootDir: src), so this compiled test lives at
// out/launchConfig.test.js; .vscode/ is a sibling of out/, hence a single "..".
const LAUNCH_JSON_PATH = path.join(__dirname, "..", ".vscode", "launch.json");

interface LaunchConfiguration {
  name: string;
  type: string;
  request: string;
  args?: string[];
  [key: string]: unknown;
}

interface LaunchJson {
  version: string;
  configurations: LaunchConfiguration[];
}

function readLaunchJson(): LaunchJson {
  const raw = fs.readFileSync(LAUNCH_JSON_PATH, "utf8");
  return JSON.parse(raw) as LaunchJson;
}

// A machine-local path ("C:\...", "/Users/...", "/home/...") must never appear in the committed
// launch.json; anything but a ${variable} substitution or a relative segment is suspect, and the
// Windows-absolute shape is the one that has regressed before.
const HARDCODED_PATH_PATTERN = /^[A-Za-z]:[\\/]|^\/(?:Users|home)\//;

test("launch.json exists and is valid JSON", () => {
  assert.ok(fs.existsSync(LAUNCH_JSON_PATH), `expected ${LAUNCH_JSON_PATH} to exist`);
  assert.doesNotThrow(() => readLaunchJson());
});

test("launch.json has at least one extensionHost launch configuration", () => {
  const launchJson = readLaunchJson();
  assert.ok(Array.isArray(launchJson.configurations));
  assert.ok(launchJson.configurations.length > 0);
  const config = launchJson.configurations[0];
  assert.equal(config.type, "extensionHost");
  assert.equal(config.request, "launch");
});

test("launch.json configurations[0].args are ${workspaceFolder}-relative, no hardcoded absolute paths", () => {
  const launchJson = readLaunchJson();
  const config = launchJson.configurations[0];
  assert.ok(Array.isArray(config.args) && config.args.length > 0, "expected non-empty args");
  for (const arg of config.args as string[]) {
    assert.ok(
      !HARDCODED_PATH_PATTERN.test(arg),
      `expected no hardcoded absolute path in args, got: ${arg}`,
    );
  }
});

test("launch.json configurations[0].args includes --extensionDevelopmentPath=${workspaceFolder}", () => {
  const launchJson = readLaunchJson();
  const config = launchJson.configurations[0];
  assert.ok(
    (config.args as string[]).includes("--extensionDevelopmentPath=${workspaceFolder}"),
    "expected --extensionDevelopmentPath=${workspaceFolder} in args",
  );
});

test("launch.json configurations[0].args includes a ${workspaceFolder}-relative sample workspace path", () => {
  const launchJson = readLaunchJson();
  const config = launchJson.configurations[0];
  const sampleWorkspaceArg = (config.args as string[]).find(
    (arg) => arg !== "--extensionDevelopmentPath=${workspaceFolder}",
  );
  assert.ok(sampleWorkspaceArg, "expected a second arg with the sample workspace path");
  assert.ok(
    sampleWorkspaceArg?.startsWith("${workspaceFolder}"),
    `expected sample workspace arg to start with \${workspaceFolder}, got: ${sampleWorkspaceArg}`,
  );
});

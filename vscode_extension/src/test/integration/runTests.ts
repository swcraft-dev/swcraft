// @vscode/test-electron launcher.
//
// Downloads (and caches) a real VS Code build, then launches an Extension Development
// Host with this extension and the fixture workspace, running the compiled `smoke.test.ts`.
// A plain Node script (`node out/test/integration/runTests.js`), not part of the
// `node --test` unit suite: it downloads VS Code and needs a display.
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { runTests } from "@vscode/test-electron";

async function main(): Promise<void> {
  // Compiled layout: out/test/integration/runTests.js
  //   extensionDevelopmentPath -> out/../../.. -> the vscode_extension package root
  //   extensionTestsPath       -> the compiled smoke test in this same directory
  //   fixture workspace        -> the *source* fixtures dir (plain data, never compiled)
  const extensionDevelopmentPath = path.resolve(__dirname, "../../..");
  const extensionTestsPath = path.resolve(__dirname, "./smoke.test");
  const fixtureWorkspacePath = path.resolve(__dirname, "../../../src/test/fixtures");

  // VS Code's IPC socket lives under --user-data-dir, and macOS caps unix socket paths at
  // 103 chars: a deeply nested checkout fails with `EINVAL ...sock`. A short tmpdir-based
  // --user-data-dir/--extensions-dir avoids that.

  const shortTmpRoot = fs.mkdtempSync(path.join(os.tmpdir(), "swcraft-vscode-test-"));
  const userDataDir = path.join(shortTmpRoot, "user-data");
  const extensionsDir = path.join(shortTmpRoot, "extensions");

  try {
    await runTests({
      extensionDevelopmentPath,
      extensionTestsPath,
      launchArgs: [
        fixtureWorkspacePath,
        "--disable-extensions",
        `--user-data-dir=${userDataDir}`,
        `--extensions-dir=${extensionsDir}`,
      ],
    });
  } catch (err) {
    console.error("SWCraft integration smoke test failed:", err);
    process.exitCode = 1;
  }
}

void main();

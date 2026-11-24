// Portable equivalent of `rm -rf out && npx tsc && node --test "out/*.test.js"`, using
// only Node APIs: Windows PowerShell 5.x does not support `&&`.

import { execFileSync } from "node:child_process";
import { rmSync } from "node:fs";
import * as path from "node:path";

import { compile, projectRoot } from "./tsc.mjs";

// Clean `out/` first: tsc does not delete the output of a deleted source test, and the
// stale compiled test would keep running as a phantom pass.
rmSync(path.join(projectRoot, "out"), { recursive: true, force: true });

compile();

// The non-recursive glob excludes the integration suite under src/test/integration/, which
// imports "vscode" (unresolvable under plain `node --test`). Node's own `--test` glob
// (native since Node 20.6) receives the literal pattern; no shell expands it.

execFileSync(process.execPath, ["--test", "out/*.test.js"], {
  stdio: "inherit",
  cwd: projectRoot,
});

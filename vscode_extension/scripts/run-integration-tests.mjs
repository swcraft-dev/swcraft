// Portable equivalent of `npx tsc && node out/test/integration/runTests.js` (Windows
// PowerShell 5.x does not support `&&`).

import { execFileSync } from "node:child_process";
import * as path from "node:path";

import { compile, projectRoot } from "./tsc.mjs";

compile();

execFileSync(
  process.execPath,
  [path.join(projectRoot, "out", "test", "integration", "runTests.js")],
  { stdio: "inherit", cwd: projectRoot },
);

// Bundles the extension entry point for packaging. The `test` task uses tsc + node --test
// instead.

import { copyFileSync, mkdirSync, readdirSync } from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";
import * as esbuild from "esbuild";

const projectRoot = path.dirname(fileURLToPath(import.meta.url));

await esbuild.build({
  entryPoints: ["src/extension.ts"],
  bundle: true,
  outfile: "dist/extension.js",
  external: ["vscode"],
  format: "cjs",
  platform: "node",
  target: "node18",
  sourcemap: true,
});

// Copy the onboarding files (agent, instructions, hooks, the client files and the client
// table `clients.json` that *Onboard Project* reads at run time) into dist/onboarding-assets/
// so they ship in the .vsix: `.vscodeignore` excludes `fixtures/**`, re-including a subpath
// by negation is fragile, and `dist/**` is not excluded. `packageVsix.test.ts` pins the
// directory's exact file set.

const onboardingAssetsDir = path.join(projectRoot, "dist", "onboarding-assets");
mkdirSync(onboardingAssetsDir, { recursive: true });
const agentsFixturesDir = path.join(projectRoot, "fixtures", "agents");
for (const name of readdirSync(agentsFixturesDir)) {
  copyFileSync(path.join(agentsFixturesDir, name), path.join(onboardingAssetsDir, name));
}

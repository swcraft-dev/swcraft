// Sync guard: commands.ts's TREE_VIEW_ID constant, passed to
// `vscode.window.registerTreeDataProvider(TREE_VIEW_ID, ...)`, must name the view id
// package.json's `contributes.views` declares for the SWCraft Activity Bar entry. A typo in
// either silently breaks the TreeView (a provider for a view VS Code never renders, or a
// declared view with no provider) with no other automated signal.
//
// commands.ts imports "vscode", so it cannot be imported under plain `node --test`; as
// commandCore.test.ts's "no transitive vscode import" check does, this reads commands.ts as
// raw text and regex-extracts the constant.

import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as path from "node:path";
import { test } from "node:test";

// tsc emits src/**/*.ts -> out/ (rootDir: src), so this compiled test lives
// at out/treeViewId.test.js; src/ and package.json are one level up from out/.
const SRC_DIR = path.join(__dirname, "..", "src");
const PACKAGE_JSON_PATH = path.join(__dirname, "..", "package.json");

function extractTreeViewId(): string {
  const content = fs.readFileSync(path.join(SRC_DIR, "commands.ts"), "utf8");
  const match = content.match(/const\s+TREE_VIEW_ID\s*=\s*"([^"]+)"/);
  assert.ok(match, 'commands.ts must define `const TREE_VIEW_ID = "..."`');
  return match[1];
}

function packageJsonViewId(): string {
  const pkg = JSON.parse(fs.readFileSync(PACKAGE_JSON_PATH, "utf8")) as {
    contributes: { views: Record<string, Array<{ id: string }>> };
  };
  const views = pkg.contributes.views.swcraft;
  assert.ok(
    views && views.length > 0,
    "package.json's contributes.views.swcraft must declare at least one view",
  );
  return views[0].id;
}

test("commands.ts's TREE_VIEW_ID matches package.json's contributes.views.swcraft[0].id", () => {
  assert.equal(
    extractTreeViewId(),
    packageJsonViewId(),
    "TREE_VIEW_ID in commands.ts must match the view id declared in package.json, " +
      "or registerTreeDataProvider() registers a provider for a view VS Code never renders",
  );
});

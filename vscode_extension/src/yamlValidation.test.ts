// The `contributes.yamlValidation` block of package.json.
//
// The block is declarative metadata Red Hat YAML reads from the manifest, with no code path
// in this extension behind it, so the manifest content is what is tested: every entry points
// at a schema file that ships under `schemas/`, every glob is non-empty, and the entry count
// equals the schema file count (an orphan schema or a dangling entry both fail). Whether the
// block equals the generator's output is `packages/tools/tests/test_editor_schema_manifest.py`'s;
// whether the files are packaged into the .vsix is `packageVsix.test.ts`'s; whether a real VS
// Code host registers the block is the integration smoke's.
import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as path from "node:path";
import { test } from "node:test";

import { PROJECT_ROOT, readManifest, schemaFiles } from "./testSupport";

interface YamlValidationEntry {
  fileMatch: string[];
  url: string;
}

function readYamlValidation(): YamlValidationEntry[] {
  const block = readManifest().contributes?.yamlValidation;
  assert.ok(Array.isArray(block), "package.json must carry contributes.yamlValidation as an array");
  return block as YamlValidationEntry[];
}

test("every yamlValidation url resolves to an existing file under schemas/", () => {
  for (const entry of readYamlValidation()) {
    assert.ok(
      entry.url.startsWith("./schemas/"),
      `url "${entry.url}" must be extension-relative under ./schemas/ (the client resolves it with joinPath(extensionUri, url))`,
    );
    const resolved = path.join(PROJECT_ROOT, entry.url);
    assert.ok(
      fs.existsSync(resolved),
      `url "${entry.url}" does not resolve to a file: ${resolved}`,
    );
    assert.doesNotThrow(() => JSON.parse(fs.readFileSync(resolved, "utf8")));
  }
});

test("every yamlValidation fileMatch is a non-empty array of non-empty globs", () => {
  for (const entry of readYamlValidation()) {
    assert.ok(Array.isArray(entry.fileMatch), `fileMatch of "${entry.url}" must be an array`);
    assert.ok(entry.fileMatch.length > 0, `fileMatch of "${entry.url}" is empty`);
    for (const glob of entry.fileMatch) {
      assert.ok(
        typeof glob === "string" && glob.length > 0,
        `fileMatch of "${entry.url}" carries an empty glob`,
      );
    }
  }
});

test("the yamlValidation entry count equals the schema file count, one to one", () => {
  const entries = readYamlValidation();
  const files = schemaFiles();
  assert.equal(
    entries.length,
    files.length,
    `yamlValidation has ${entries.length} entries but schemas/ holds ${files.length} files`,
  );
  const referenced = new Set(entries.map((entry) => path.basename(entry.url)));
  assert.deepEqual([...referenced].sort(), [...files].sort());
});

test("the extension pack names Red Hat YAML so the association is not inert", () => {
  // Membership only: this file is about the association, and the association is
  // inert unless *something* reads it. The exact member list, its order and the
  // absence of `extensionDependencies` are `extensionPack.test.ts`'s, so adding
  // or removing another member (Red Hat XML beside it) is not an edit here.
  const manifest = readManifest();
  assert.ok(
    (manifest.extensionPack ?? []).includes("redhat.vscode-yaml"),
    "contributes.yamlValidation is read by redhat.vscode-yaml; without it in the pack the block is inert",
  );
  assert.equal(manifest.extensionDependencies, undefined, "extensionPack, never a hard dependency");
});

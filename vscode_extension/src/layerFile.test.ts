// The active-editor layer-file predicate.
//
// The walkthrough's *Edit a layer file* step completes on
// `onContext:swcraft.activeEditorIsLayerFile`, which commands.ts sets from
// `isLayerFilePath()` on every `onDidChangeActiveTextEditor`. The predicate must say yes for a
// layer file and **no** for everything else: a key that cannot go back to false latches the
// checkmark on after one hit.
//
// No `vscode` import: this runs under plain `node --test`.
import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as path from "node:path";
import { test } from "node:test";

import {
  isLayerFilePath,
  isUnderModelRoot,
  LAYER_FILE_CONTEXT_KEY,
  type LayerDirectoryRule,
  type LayerStorageFacts,
  layerDirectoryRules,
  layerOf,
} from "./layerFile";
import { loadReferences } from "./navigationCore";
import { readManifest, SCHEMAS_DIR } from "./testSupport";

const MODEL_ROOT = path.resolve("/projects/vc/model");

/**
 * The rules the extension runs with, parsed out of the real `contributes.yamlValidation`
 * block, which `editor_schema.py` generates from the model registry: a layer added to the
 * model contract reaches this predicate without a second list to edit.
 */
function shippedBlock(): Array<{ fileMatch?: string[] }> {
  const block = readManifest().contributes?.yamlValidation as
    | Array<{ fileMatch?: string[] }>
    | undefined;
  assert.ok(Array.isArray(block), "package.json must carry contributes.yamlValidation as an array");
  return block;
}

function shippedRules(): LayerDirectoryRule[] {
  return layerDirectoryRules(shippedBlock().flatMap((entry) => entry.fileMatch ?? []));
}

test("the layer directories are recovered from the shipped yamlValidation globs", () => {
  const rules = shippedRules();
  const directories = rules.map((rule) => rule.directory);
  // Every layer the block declares, and nothing else. The entries of
  // `swcraft.yaml` and of a needs mapping file are not globs of the layer
  // shape, so they contribute no rule -- an editor on the manifest or on a
  // mapping must not count as editing a layer file.
  assert.ok(rules.length > 0, "no layer directory was recovered from contributes.yamlValidation");
  // The count, not just two names: `editor_schema.py` emits one entry per declared layer plus
  // the manifest's and the needs mapping's, so a future layer whose glob LAYER_GLOB does not
  // match would otherwise drop out of `rules` silently while every named assertion passes.
  const nonLayer = ["swcraft.yaml", "**/*needs-mapping.yaml"];
  assert.equal(
    rules.length,
    shippedBlock().length - nonLayer.length,
    `every yamlValidation entry but the manifest's and the mapping's must yield a layer rule; recovered ${directories.join(", ")}`,
  );
  for (const glob of nonLayer) {
    assert.equal(
      shippedBlock().filter((entry) => (entry.fileMatch ?? []).includes(glob)).length,
      1,
      `the entries that yield no layer rule must be the manifest's and the mapping's own; ${glob} is not one entry`,
    );
    assert.deepEqual(layerDirectoryRules([glob]), [], `${glob} must yield no layer rule`);
  }
  assert.ok(directories.includes("swc"), `swc missing from: ${directories.join(", ")}`);
  assert.ok(
    directories.includes("interfaces"),
    `interfaces missing from: ${directories.join(", ")}`,
  );
  assert.equal(new Set(directories).size, directories.length, "duplicate layer directory");
  assert.ok(!directories.includes("swcraft.yaml"));
  for (const rule of rules) {
    assert.equal(rule.suffix, ".yaml", `layer "${rule.directory}" declares suffix ${rule.suffix}`);
  }
  // `swc` is the one nesting layer (`**/swc/**/*.yaml`); every other layer is flat.
  assert.deepEqual(
    rules.filter((rule) => rule.nested).map((rule) => rule.directory),
    ["swc"],
  );
});

test("a layer file under the model root is a layer file", () => {
  assert.equal(
    isLayerFilePath(path.join(MODEL_ROOT, "interfaces", "ISpeed.yaml"), MODEL_ROOT, shippedRules()),
    true,
  );
});

test("a file in a nested swc/{group}/ directory is a layer file", () => {
  // `swc` is the one layer whose glob nests (`**/swc/**/*.yaml`), because
  // elements are grouped one level down.
  assert.equal(
    isLayerFilePath(
      path.join(MODEL_ROOT, "swc", "chassis", "SpeedSensor.yaml"),
      MODEL_ROOT,
      shippedRules(),
    ),
    true,
  );
  // The same layer directly, with no group: `**` matches zero segments too.
  assert.equal(
    isLayerFilePath(path.join(MODEL_ROOT, "swc", "SpeedSensor.yaml"), MODEL_ROOT, shippedRules()),
    true,
  );
});

test("a YAML file outside the model root is not a layer file", () => {
  const rules = shippedRules();
  // Same layer directory name, wrong root -- the sibling `dist/` tree, and the
  // project directory above the model root.
  assert.equal(
    isLayerFilePath(path.resolve("/projects/vc/dist/interfaces/ISpeed.yaml"), MODEL_ROOT, rules),
    false,
  );
  assert.equal(
    isLayerFilePath(path.resolve("/projects/vc/swcraft.yaml"), MODEL_ROOT, rules),
    false,
  );
  assert.equal(
    isLayerFilePath(path.resolve("/elsewhere/model/tasks/T10ms.yaml"), MODEL_ROOT, rules),
    false,
  );
});

test("a non-YAML file inside the model root is not a layer file", () => {
  const rules = shippedRules();
  assert.equal(
    isLayerFilePath(path.join(MODEL_ROOT, "swc", "README.md"), MODEL_ROOT, rules),
    false,
  );
  assert.equal(
    isLayerFilePath(path.join(MODEL_ROOT, "interfaces", "ISpeed.json"), MODEL_ROOT, rules),
    false,
  );
  // A file whose whole name *is* the suffix is not a YAML element file either.
  assert.equal(isLayerFilePath(path.join(MODEL_ROOT, "tasks", ".yaml"), MODEL_ROOT, rules), false);
});

test("no active editor, and no detected project, are both not a layer file", () => {
  // The two `undefined` cases the adapter feeds in: `window.activeTextEditor`
  // is undefined when every editor is closed, and `model_root` is undefined
  // when the workspace has no `swcraft.yaml`. Neither may throw, and neither
  // may leave the context key at a stale `true`.
  assert.equal(isLayerFilePath(undefined, MODEL_ROOT, shippedRules()), false);
  assert.equal(
    isLayerFilePath(path.join(MODEL_ROOT, "tasks", "T10ms.yaml"), undefined, shippedRules()),
    false,
  );
  assert.equal(isLayerFilePath(undefined, undefined, shippedRules()), false);
});

test("a directory that is not a declared layer is not a layer file", () => {
  const rules = shippedRules();
  assert.equal(
    isLayerFilePath(path.join(MODEL_ROOT, "notALayer", "Thing.yaml"), MODEL_ROOT, rules),
    false,
  );
  // Directly in the model root: in no layer at all.
  assert.equal(isLayerFilePath(path.join(MODEL_ROOT, "Thing.yaml"), MODEL_ROOT, rules), false);
  // The model root itself.
  assert.equal(isLayerFilePath(MODEL_ROOT, MODEL_ROOT, rules), false);
});

test("a flat layer does not match a nested path", () => {
  // `**/tasks/*.yaml` is one level; only `swc` declares `**/tasks/**/*.yaml`'s
  // shape. A subdirectory under a flat layer is not a file the schemas reach,
  // so it must not tick the step either.
  assert.equal(
    isLayerFilePath(
      path.join(MODEL_ROOT, "tasks", "group", "T10ms.yaml"),
      MODEL_ROOT,
      shippedRules(),
    ),
    false,
  );
});

test("layerDirectoryRules drops globs that are not of the layer shape", () => {
  assert.deepEqual(layerDirectoryRules(["swcraft.yaml"]), []);
  assert.deepEqual(
    layerDirectoryRules(["**/*.yaml", "*.yaml", "**/a/*/*.yaml", "**/a//b/*.yaml"]),
    [],
  );
  assert.deepEqual(layerDirectoryRules(["**/tasks/*.yaml", "**/tasks/*.yaml"]), [
    { directory: "tasks", nested: false, suffix: ".yaml" },
  ]);
});

// A layer's directory may have several segments (`data_types/base`): its glob is
// `**/data_types/base/*.yaml`, and a path is matched against the rule whose directory is the
// LONGEST that prefixes it, so a flat layer and a layer stored inside its directory each keep
// their own files. A synthetic rule set: the shipped contract refuses one layer directory
// inside another (its data description layers are siblings under `data_types/`, the test after
// this group), so the predicate's generality is what is held.
const NESTED_RULES = layerDirectoryRules([
  "**/swc/**/*.yaml",
  "**/data_types/*.yaml",
  "**/data_types/base/*.yaml",
  "**/data_types/application/*.yaml",
]);

test("layerDirectoryRules recovers a directory of several segments", () => {
  assert.deepEqual(NESTED_RULES, [
    { directory: "swc", nested: true, suffix: ".yaml" },
    { directory: "data_types", nested: false, suffix: ".yaml" },
    { directory: "data_types/base", nested: false, suffix: ".yaml" },
    { directory: "data_types/application", nested: false, suffix: ".yaml" },
  ]);
  assert.deepEqual(layerDirectoryRules(["**/a/b/**/*.yml"]), [
    { directory: "a/b", nested: true, suffix: ".yml" },
  ]);
});

test("a file of a layer stored in a nested directory is a layer file", () => {
  for (const relative of [
    ["data_types", "base", "Float32.yaml"],
    ["data_types", "application", "Speed.yaml"],
    ["data_types", "Speed.yaml"],
  ]) {
    assert.equal(
      isLayerFilePath(path.join(MODEL_ROOT, ...relative), MODEL_ROOT, NESTED_RULES),
      true,
      relative.join("/"),
    );
  }
});

test("the longest directory decides, and its depth rule applies below it", () => {
  for (const relative of [
    // One level too deep for the flat nested layer: not handed to `data_types`.
    ["data_types", "base", "sub", "Float32.yaml"],
    // A subdirectory of the flat layer that is no layer's directory.
    ["data_types", "other", "Speed.yaml"],
    // The nested directory itself, with nothing below it.
    ["data_types", "base"],
    // Matching is by whole segments, not by string prefix.
    ["data_types", "basement", "Float32.yaml"],
  ]) {
    assert.equal(
      isLayerFilePath(path.join(MODEL_ROOT, ...relative), MODEL_ROOT, NESTED_RULES),
      false,
      relative.join("/"),
    );
  }
});

test("the shipped data description layers are each a layer file, and the retired places are not", () => {
  const rules = shippedRules();
  for (const relative of [
    ["data_types", "application", "ProbeApp.yaml"],
    ["data_types", "implementation", "ProbeImpl.yaml"],
    ["data_types", "base", "probe_u8.yaml"],
    ["data_types", "mapping_sets", "ProbeMaps.yaml"],
  ]) {
    assert.equal(
      isLayerFilePath(path.join(MODEL_ROOT, ...relative), MODEL_ROOT, rules),
      true,
      relative.join("/"),
    );
  }
  // The two retired flat directories: a file directly in either is refused by every read, so
  // the editor does not count it as a layer file.
  for (const relative of [
    ["data_types", "ProbeImpl.yaml"],
    ["mapping_sets", "ProbeMaps.yaml"],
  ]) {
    assert.equal(
      isLayerFilePath(path.join(MODEL_ROOT, ...relative), MODEL_ROOT, rules),
      false,
      relative.join("/"),
    );
  }
});

test("the context key is the one the walkthrough step names", () => {
  // The single source: commands.ts passes it to `setContext` and `walkthrough.test.ts` checks
  // package.json's `onContext:` event against it. Pinned so a rename is a deliberate edit.
  assert.equal(LAYER_FILE_CONTEXT_KEY, "swcraft.activeEditorIsLayerFile");
});

// --- layerOf: the layer NAME of a file, from references.json -----

function shippedLayers(): ReadonlyMap<string, LayerStorageFacts> {
  return loadReferences(fs.readFileSync(path.join(SCHEMAS_DIR, "references.json"), "utf8")).layers;
}

test("layerOf names the layer of a flat file, a nested swc/<group>/ file and a multi-segment directory", () => {
  const layers = shippedLayers();
  assert.equal(
    layerOf(path.join(MODEL_ROOT, "interfaces", "ISpeed.yaml"), MODEL_ROOT, layers),
    "interfaces",
  );
  assert.equal(
    layerOf(path.join(MODEL_ROOT, "swc", "chassis", "SpeedSensor.yaml"), MODEL_ROOT, layers),
    "swc",
  );
  assert.equal(
    layerOf(path.join(MODEL_ROOT, "data_types", "base", "uint8.yaml"), MODEL_ROOT, layers),
    "base_types",
  );
  assert.equal(
    layerOf(path.join(MODEL_ROOT, "data_types", "mapping_sets", "M.yaml"), MODEL_ROOT, layers),
    "mapping_sets",
  );
});

test("layerOf: the longest directory decides, never a flat layer one level too deep", () => {
  // A synthetic `data_types` layer beside `data_types/base`: the shipped
  // contract refuses that shape, the predicate's generality is what is held.
  const layers = new Map<string, LayerStorageFacts>([
    ["data_types", { directory: "data_types", nesting: "flat", fileSuffix: ".yaml" }],
    ["base_types", { directory: "data_types/base", nesting: "flat", fileSuffix: ".yaml" }],
  ]);
  assert.equal(
    layerOf(path.join(MODEL_ROOT, "data_types", "base", "X.yaml"), MODEL_ROOT, layers),
    "base_types",
  );
  assert.equal(
    layerOf(path.join(MODEL_ROOT, "data_types", "X.yaml"), MODEL_ROOT, layers),
    "data_types",
  );
});

test("layerOf: any nesting other than 'flat' nests, whatever the placement field is called", () => {
  const layers = new Map<string, LayerStorageFacts>([
    ["things", { directory: "things", nesting: "family", fileSuffix: ".yaml" }],
  ]);
  assert.equal(
    layerOf(path.join(MODEL_ROOT, "things", "a", "T.yaml"), MODEL_ROOT, layers),
    "things",
  );
});

test("layerOf answers nothing for the manifest, a file outside the root, a non-layer directory", () => {
  const layers = shippedLayers();
  assert.equal(layerOf(path.resolve("/projects/vc/swcraft.yaml"), MODEL_ROOT, layers), undefined);
  assert.equal(
    layerOf(path.resolve("/elsewhere/model/tasks/T10ms.yaml"), MODEL_ROOT, layers),
    undefined,
  );
  assert.equal(
    layerOf(path.join(MODEL_ROOT, "notALayer", "T.yaml"), MODEL_ROOT, layers),
    undefined,
  );
  assert.equal(
    layerOf(path.join(MODEL_ROOT, "tasks", "g", "T.yaml"), MODEL_ROOT, layers),
    undefined,
  );
  assert.equal(layerOf(undefined, MODEL_ROOT, layers), undefined);
  assert.equal(layerOf(path.join(MODEL_ROOT, "tasks", "T.yaml"), undefined, layers), undefined);
});

test("isUnderModelRoot: a path strictly under the model root, nothing else", () => {
  assert.equal(isUnderModelRoot(path.join(MODEL_ROOT, "swc", "app", "A.yaml"), MODEL_ROOT), true);
  assert.equal(isUnderModelRoot(path.join(MODEL_ROOT, "..", "swcraft.yaml"), MODEL_ROOT), false);
  assert.equal(isUnderModelRoot(`${MODEL_ROOT}-other${path.sep}A.yaml`, MODEL_ROOT), false);
  assert.equal(isUnderModelRoot(MODEL_ROOT, MODEL_ROOT), false);
  assert.equal(isUnderModelRoot(path.join(MODEL_ROOT, "..x.yaml"), MODEL_ROOT), true);
});

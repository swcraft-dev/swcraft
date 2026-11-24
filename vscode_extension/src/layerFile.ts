// Pure logic: "is the file in the active editor a model layer file?". No `vscode` import: it runs
// under plain `node --test`; the adapter half (the `onDidChangeActiveTextEditor` listener and the
// `setContext` call) is in commands.ts.
//
// It backs the walkthrough's *Edit a layer file* step, whose `onContext:<key>` completion event
// means "the reader edited a layer file", not "the reader opened the step".
//
// These are **path tests**, not a model read: they compare a file path against the manifest's
// `model_root` and the layer directories, and never open the file, parse YAML or ask what it
// declares. Validating model content stays the CLI's.
//
// The layer directories are not a TypeScript literal: they are parsed back out of the extension's
// own `contributes.yamlValidation` globs, which `editor_schema.py`'s `yaml_validation_entries()`
// generates from the model registry. A new layer reaches this predicate through the same
// `python scripts/dev.py schemas` run that reaches the schemas (`test_editor_schema_drift.py` is
// the gate), with no second list and no process spawned per editor switch.

import * as path from "node:path";

/**
 * The context key the *Edit a layer file* walkthrough step completes on; declared here so
 * `walkthrough.test.ts` can check every `onContext:` event in `package.json` names a key the
 * extension sets.
 */
export const LAYER_FILE_CONTEXT_KEY = "swcraft.activeEditorIsLayerFile";

/**
 * One layer directory, recovered from a `contributes.yamlValidation` glob.
 *
 * `directory` is model-root-relative and may have several `/`-joined segments (`data_types/base`).
 * `nested` mirrors the Python `StorageRecord.is_recursive`: `swc` groups its elements one level
 * deeper (`swc/{group}/Name.yaml`), every other layer is flat. A two-segment directory is not a
 * nesting layer by itself.
 */
export interface LayerDirectoryRule {
  directory: string;
  nested: boolean;
  suffix: string;
}

// The two layer glob shapes `editor_schema.py` emits, `<dir>` one or more `/`-joined segments:
//   `**/<dir>/*<suffix>`      — a flat layer
//   `**/<dir>/**/*<suffix>`   — a nesting layer (`swc`)
// A segment holds no `*`, so the nesting layer's `**/` is never read as one.
const LAYER_GLOB = /^\*\*\/((?:[^/*]+\/)*[^/*]+)\/(\*\*\/)?\*(\.[^/*]+)$/;

/**
 * Recover the layer directories from *fileMatches*, every `fileMatch` glob of
 * `contributes.yamlValidation` flattened.
 *
 * Globs without the layer shape are dropped, not rejected: the manifest's (`swcraft.yaml`) and the
 * needs mapping file's (`*needs-mapping.yaml`, no directory segment before its `*`) are no layer.
 */
export function layerDirectoryRules(fileMatches: readonly string[]): LayerDirectoryRule[] {
  const rules: LayerDirectoryRule[] = [];
  const seen = new Set<string>();
  for (const glob of fileMatches) {
    const match = LAYER_GLOB.exec(glob);
    if (match === null || seen.has(match[1])) {
      continue;
    }
    seen.add(match[1]);
    rules.push({ directory: match[1], nested: match[2] !== undefined, suffix: match[3] });
  }
  return rules;
}

function segments(relative: string): string[] {
  return relative.split(/[\\/]/).filter((segment) => segment !== "");
}

/**
 * The rule whose directory is the longest that prefixes *parts* with at least one segment left
 * below it, with that directory's depth, or `undefined`.
 *
 * The longest, because a rule's directory may lie inside another's (`data_types` beside
 * `data_types/base` in a test): the first segment alone would read `data_types/base/X.yaml` as a
 * flat layer's file one level too deep. The Python side matches the same way
 * (`model_registry.longest_directory_match`).
 */
function longestDirectoryRule(
  parts: readonly string[],
  rules: readonly LayerDirectoryRule[],
): { rule: LayerDirectoryRule; depth: number } | undefined {
  let best: LayerDirectoryRule | undefined;
  let bestDepth = 0;
  for (const rule of rules) {
    const prefix = segments(rule.directory);
    if (
      prefix.length <= bestDepth ||
      prefix.length >= parts.length ||
      prefix.some((segment, index) => segment !== parts[index])
    ) {
      continue;
    }
    best = rule;
    bestDepth = prefix.length;
  }
  return best === undefined ? undefined : { rule: best, depth: bestDepth };
}

/**
 * Is *filePath* a model layer file of the project whose `model_root` resolves to *modelRoot*?
 *
 * `false`, never a throw, for every "no": no active editor, no detected project, a path outside
 * the model root or directly at it, an undeclared directory, the wrong suffix, a nested path under
 * a flat layer. The caller pushes the result into a context key, and a key that cannot go back to
 * `false` latches the walkthrough's checkmark on.
 */
export function isLayerFilePath(
  filePath: string | undefined,
  modelRoot: string | undefined,
  rules: readonly LayerDirectoryRule[],
): boolean {
  return layerFileRule(filePath, modelRoot, rules) !== undefined;
}

/**
 * Whether *file* lies strictly below *modelRoot*, compared as resolved paths: a child named like
 * `..x` is inside, `..` and its descendants are not. Also whether a saved file can change a lens
 * count (the CLI reads the whole model).
 */
export function isUnderModelRoot(file: string, modelRoot: string): boolean {
  const relative = path.relative(path.resolve(modelRoot), path.resolve(file));
  return (
    relative !== "" &&
    relative !== ".." &&
    !relative.startsWith(`..${path.sep}`) &&
    !path.isAbsolute(relative)
  );
}

/** The rule *filePath* is a layer file of, or `undefined`: the one matcher both public tests share. */
function layerFileRule(
  filePath: string | undefined,
  modelRoot: string | undefined,
  rules: readonly LayerDirectoryRule[],
): LayerDirectoryRule | undefined {
  if (filePath === undefined || modelRoot === undefined) {
    return undefined;
  }
  if (!isUnderModelRoot(filePath, modelRoot)) {
    return undefined;
  }
  const parts = segments(path.relative(path.resolve(modelRoot), path.resolve(filePath)));
  if (parts.length < 2) {
    // A file sitting directly in `model_root` is in no layer.
    return undefined;
  }
  const match = longestDirectoryRule(parts, rules);
  if (match === undefined) {
    return undefined;
  }
  const { rule, depth } = match;
  const below = parts.length - depth;
  if (!rule.nested && below !== 1) {
    // A flat layer's glob is `**/<dir>/*.yaml`: one level, no subdirectories.
    return undefined;
  }
  const basename = parts[parts.length - 1];
  return basename.length > rule.suffix.length && basename.endsWith(rule.suffix) ? rule : undefined;
}

/**
 * A layer's storage facts as `references.json` publishes them. `nesting` is `"flat"` or a nesting
 * layer's placement field (`"group"` for `swc`); anything but `"flat"` nests.
 */
export interface LayerStorageFacts {
  directory: string;
  nesting: string;
  fileSuffix: string;
}

/**
 * The name of the layer *filePath* is a file of, under the project whose `model_root` resolves to
 * *modelRoot*, or `undefined` (the manifest, a file outside the root or in no declared directory).
 * *layers* are `references.json`'s, keyed by layer name, decided by the same matcher as
 * {@link isLayerFilePath}.
 */
export function layerOf(
  filePath: string | undefined,
  modelRoot: string | undefined,
  layers: ReadonlyMap<string, LayerStorageFacts>,
): string | undefined {
  const names = new Map<LayerDirectoryRule, string>();
  for (const [name, facts] of layers) {
    names.set(
      { directory: facts.directory, nested: facts.nesting !== "flat", suffix: facts.fileSuffix },
      name,
    );
  }
  const rule = layerFileRule(filePath, modelRoot, [...names.keys()]);
  return rule === undefined ? undefined : names.get(rule);
}

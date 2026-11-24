import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { type TestContext, test } from "node:test";

import * as project from "./project";
import {
  DEFAULT_ARXML_OUTPUT,
  DEFAULT_MODEL_ROOT,
  DEFAULT_NEEDS_OUTPUT,
  DEFAULT_VIEWS_ROOT,
  findManifest,
  joinLikePathlib,
  MANIFEST_FILENAME,
  MANIFEST_PATH_FIELDS,
  manifestPathRefusal,
  parseDistRoot,
  parseModelRoot,
  parseOutputsArxml,
  parseOutputsNeeds,
  parseViewsRoot,
  pythonResolve,
  readManifestPaths,
  resolveModelRoot,
  resolveViewsRoot,
  viewsRootRefusal,
} from "./project";

function mkWorkspace(): string {
  return fs.mkdtempSync(path.join(os.tmpdir(), "swcraft-vscode-project-test-"));
}

function cleanup(dir: string): void {
  fs.rmSync(dir, { recursive: true, force: true });
}

test("findManifest: manifest present at workspace root (success)", () => {
  const dir = mkWorkspace();
  try {
    const manifestPath = path.join(dir, MANIFEST_FILENAME);
    fs.writeFileSync(manifestPath, "name: test\n");
    assert.equal(findManifest(dir), manifestPath);
  } finally {
    cleanup(dir);
  }
});

test("findManifest: no manifest anywhere returns undefined", () => {
  const dir = mkWorkspace();
  try {
    assert.equal(findManifest(dir), undefined);
  } finally {
    cleanup(dir);
  }
});

test("findManifest: nested-only manifest is ignored in spike mode", () => {
  const dir = mkWorkspace();
  try {
    const nested = path.join(dir, "sub-project");
    fs.mkdirSync(nested);
    fs.writeFileSync(path.join(nested, MANIFEST_FILENAME), "name: test\n");
    assert.equal(findManifest(dir), undefined);
  } finally {
    cleanup(dir);
  }
});

test("findManifest: a directory named swcraft.yaml is not a manifest", () => {
  const dir = mkWorkspace();
  try {
    fs.mkdirSync(path.join(dir, MANIFEST_FILENAME));
    assert.equal(findManifest(dir), undefined);
  } finally {
    cleanup(dir);
  }
});

// --- No `c4` symbol ----------------------------------------------------------
//
// The manifest has no `outputs.c4`: the diagrams are written into `views_root`, the one address
// the extension reads.

test("project.ts exports no c4 symbol: views_root is the one diagram address", () => {
  const exported = Object.keys(project);
  assert.deepEqual(
    exported.filter((name) => /c4/i.test(name)),
    [],
    `project.ts still exports ${exported.join(", ")}`,
  );
  // Every caller reads the loader's whole-manifest verdict (`readManifestPaths()`), never one field
  // judged alone.
  for (const retired of [
    "parseOutputsC4",
    "resolveViewsRootPath",
    "resolveModelRootPath",
    "resolveArxmlOutput",
  ]) {
    assert.equal(retired in project, false, `${retired} is still exported`);
  }
});

// --- The `#` rule of the shared line reader ----------------------------------
//
// PyYAML's plain-scalar comment rule: `Scanner.scan_plain` ends the scalar only at a `#` that
// begins a token. Both manifest fields the extension reads go through the same extractor.

test("parseViewsRoot: a # not preceded by whitespace is content, exactly as PyYAML reads it", () => {
  // A "first # wins" rule would answer `views`, a different directory from the one the CLI writes.
  assert.equal(parseViewsRoot("views_root: views#1\n"), "views#1");
  assert.equal(parseViewsRoot("views_root: a#b#c\n"), "a#b#c");
});

test("parseViewsRoot: a # after whitespace starts a comment; a value that is only a comment is the default", () => {
  assert.equal(parseViewsRoot("views_root: views #1\n"), "views");
  assert.equal(parseViewsRoot("views_root: # nothing here\n"), DEFAULT_VIEWS_ROOT);
});

test("parseModelRoot: the same # rule, since both fields share the extractor", () => {
  assert.equal(parseModelRoot("model_root: model#2\n"), "model#2");
  assert.equal(parseModelRoot("model_root: model #2\n"), "model");
});

// --- model_root --------------------------------------------------------------
//
// The one manifest field layerFile.ts's path test needs: declared, absent, and
// an unreadable manifest.

test("parseModelRoot: a declared model_root wins, comments and quotes stripped", () => {
  assert.equal(parseModelRoot("name: vc\nmodel_root: architecture\n"), "architecture");
  assert.equal(parseModelRoot("model_root: architecture  # the layers\n"), "architecture");
  assert.equal(parseModelRoot('model_root: "arch # itecture"\n'), "arch # itecture");
});

test("parseModelRoot: absent or empty falls back to the Pydantic default", () => {
  assert.equal(parseModelRoot("name: vc\n"), DEFAULT_MODEL_ROOT);
  assert.equal(parseModelRoot("model_root:\n"), DEFAULT_MODEL_ROOT);
  // Indented -- part of some other block, not the top-level field.
  assert.equal(parseModelRoot("outputs:\n  model_root: nope\n"), DEFAULT_MODEL_ROOT);
});

test("resolveModelRoot: resolved against the manifest's directory, default on an unreadable file", () => {
  const dir = mkWorkspace();
  try {
    const manifestPath = path.join(dir, MANIFEST_FILENAME);
    // Never written -- fs.readFileSync() throws.
    assert.equal(resolveModelRoot(manifestPath), path.resolve(dir, DEFAULT_MODEL_ROOT));
    fs.writeFileSync(manifestPath, "name: test\nmodel_root: architecture\n");
    assert.equal(resolveModelRoot(manifestPath), path.resolve(dir, "architecture"));
  } finally {
    cleanup(dir);
  }
});

test("resolveModelRoot: undefined wherever the loader refuses the manifest's paths", () => {
  // Nothing may be watched, walked or path-tested under a model root no command
  // reads: `model_root: ..` would have the activation scan walk the parent.
  const dir = mkWorkspace();
  try {
    const manifestPath = path.join(dir, MANIFEST_FILENAME);
    for (const manifest of [
      "name: p\nmodel_root: ..\n",
      "name: p\nmodel_root: /\n",
      "name: p\nmodel_root: .\n",
      "name: p\ndist_root: /var/tmp/dist\n",
      "name: p\ndist_root: model/dist\n",
    ]) {
      fs.writeFileSync(manifestPath, manifest);
      assert.equal(resolveModelRoot(manifestPath), undefined, manifest);
    }
  } finally {
    cleanup(dir);
  }
});

// --- views_root (SWCraft: Diagrams) ------------------------------------------
//
// The directory `generate diagrams` writes and `Diagrams` opens `index.html`
// from, and the directory the panel looks in for the views on disk.

test("parseViewsRoot: a declared views_root wins, comments and quotes stripped", () => {
  assert.equal(parseViewsRoot("name: vc\nviews_root: diagrams\n"), "diagrams");
  assert.equal(parseViewsRoot("views_root: views  # generated diagrams\n"), "views");
  assert.equal(parseViewsRoot('views_root: "my # views"\n'), "my # views");
});

test("parseViewsRoot: absent, empty or indented falls back to the Pydantic default", () => {
  assert.equal(parseViewsRoot("name: vc\nmodel_root: model\n"), DEFAULT_VIEWS_ROOT);
  assert.equal(parseViewsRoot("views_root:\n"), DEFAULT_VIEWS_ROOT);
  assert.equal(parseViewsRoot("outputs:\n  views_root: nope\n"), DEFAULT_VIEWS_ROOT);
  assert.equal(DEFAULT_VIEWS_ROOT, "views");
});

test("the manifest keys are read with spaces before the colon, exactly as PyYAML reads them", () => {
  // `views_root : diagrams` loads as the plain key. The CLI reads with libyaml
  // (`yaml.CSafeLoader`, PyYAML 6.0.3), where a TAB before the colon is separation too
  // (`views_root\t: diagrams` -> 'diagrams').
  assert.equal(parseViewsRoot("views_root : diagrams\n"), "diagrams");
  assert.equal(parseViewsRoot("views_root   : diagrams  # c\n"), "diagrams");
  assert.equal(parseModelRoot("model_root  : m # c\n"), "m");
  assert.equal(parseViewsRoot("views_root\t: diagrams\n"), "diagrams");
  assert.equal(parseViewsRoot("views_root \t : diagrams\n"), "diagrams");
  assert.equal(parseViewsRoot("views_root\t:\tdiagrams\n"), "diagrams");
  assert.equal(parseViewsRoot("views_root\t:\n"), DEFAULT_VIEWS_ROOT);
  assert.equal(parseDistRoot("dist_root\t: out\n"), "out");
  // A longer key that merely starts with the name is still another key.
  assert.equal(parseViewsRoot("views_root_old : diagrams\n"), DEFAULT_VIEWS_ROOT);
});

test("a manifest key needs a space or the line's end after its colon, exactly as PyYAML reads it", () => {
  // A colon not followed by separation or the line's end is plain-scalar content, so these lines
  // are no key and the CLI refuses the manifest at load; `Diagrams` must leave that report to the
  // CLI rather than answer with its own refusal. With `yaml.CSafeLoader` (PyYAML 6.0.3):
  //   "views_root :/tmp", "views_root:value", "views_root\t:#x" -> ScannerError
  //   "views_root:\tvalue", "views_root: \tvalue", "model_root:\tm" -> the value
  //   "views_root: \t", "views_root:\t# c"                         -> None (the default)
  assert.equal(parseViewsRoot("views_root :/tmp\n"), DEFAULT_VIEWS_ROOT);
  assert.equal(parseViewsRoot("views_root:value\n"), DEFAULT_VIEWS_ROOT);
  assert.equal(parseViewsRoot("views_root :#x\n"), DEFAULT_VIEWS_ROOT);
  assert.equal(parseViewsRoot("views_root:#x\n"), DEFAULT_VIEWS_ROOT);
  assert.equal(parseViewsRoot("views_root\t:#x\n"), DEFAULT_VIEWS_ROOT);
  assert.equal(parseViewsRoot("views_root:\tvalue\n"), "value");
  assert.equal(parseViewsRoot("views_root: \tvalue\n"), "value");
  assert.equal(parseViewsRoot("views_root :\tx\n"), "x");
  assert.equal(parseViewsRoot("views_root: x\t# c\n"), "x");
  assert.equal(parseViewsRoot("views_root: \t\n"), DEFAULT_VIEWS_ROOT);
  assert.equal(parseViewsRoot("views_root:\t# c\n"), DEFAULT_VIEWS_ROOT);
  assert.equal(parseModelRoot("model_root:\tm\n"), "m");
  assert.equal(parseModelRoot("model_root :foo\n"), DEFAULT_MODEL_ROOT);
  assert.equal(parseModelRoot("model_root:foo\n"), DEFAULT_MODEL_ROOT);
  // Every spelling libyaml reads as the key with spaces is still read (measured the same way):
  //   "views_root : value", "views_root:  value", "views_root: a:b" -> the value;
  //   "views_root :", "views_root : ", "views_root : # comment" -> None (the default).
  assert.equal(parseViewsRoot("views_root : value\n"), "value");
  assert.equal(parseViewsRoot("views_root:  value\n"), "value");
  assert.equal(parseViewsRoot("views_root: a:b\n"), "a:b");
  assert.equal(parseViewsRoot("views_root :\n"), DEFAULT_VIEWS_ROOT);
  assert.equal(parseViewsRoot("views_root : \n"), DEFAULT_VIEWS_ROOT);
  assert.equal(parseViewsRoot("views_root : # comment\n"), DEFAULT_VIEWS_ROOT);
});

test("parseViewsRoot and parseModelRoot read their own keys from one manifest", () => {
  const text = ["name: vc", "model_root: architecture", "views_root: diagrams", ""].join("\n");
  assert.equal(parseModelRoot(text), "architecture");
  assert.equal(parseViewsRoot(text), "diagrams");
});

// --- resolveViewsRoot: the three refusals ------------------------------------
//
// `swcraft clean-outputs` (`_clean_root_problems` in packages/tools/src/swcraft_tools/cli/build_commands.py)
// refuses a `views_root` that is absolute, the project directory itself or outside it, and the
// loader refuses all three manifests before any generator runs. The extension refuses the same
// three with the CLI's sentences, so it never opens a directory the CLI would not have written.

function writeManifest(dir: string, viewsRoot: string): string {
  const manifestPath = path.join(dir, MANIFEST_FILENAME);
  fs.writeFileSync(manifestPath, ["name: test", `views_root: ${viewsRoot}`, ""].join("\n"));
  return manifestPath;
}

test("resolveViewsRoot: views_root resolved against the manifest's directory", () => {
  const dir = mkWorkspace();
  try {
    assert.deepEqual(resolveViewsRoot(writeManifest(dir, "build/diagrams")), {
      ok: true,
      path: path.resolve(dir, "build/diagrams"),
    });
  } finally {
    cleanup(dir);
  }
});

test("resolveViewsRoot: the default when views_root is absent, and on an unreadable manifest", () => {
  const dir = mkWorkspace();
  try {
    const manifestPath = path.join(dir, MANIFEST_FILENAME);
    // Never written -- fs.readFileSync() throws. The CLI would fail on it too,
    // but not for a path reason, so this is not a refusal.
    assert.deepEqual(resolveViewsRoot(manifestPath), {
      ok: true,
      path: path.resolve(dir, DEFAULT_VIEWS_ROOT),
    });
    fs.writeFileSync(manifestPath, "name: test\n");
    assert.deepEqual(resolveViewsRoot(manifestPath), {
      ok: true,
      path: path.resolve(dir, DEFAULT_VIEWS_ROOT),
    });
  } finally {
    cleanup(dir);
  }
});

test("resolveViewsRoot: an absolute views_root is refused with the CLI's sentence", () => {
  const dir = mkWorkspace();
  try {
    const resolution = resolveViewsRoot(writeManifest(dir, "/var/tmp/views"));
    assert.equal(resolution.ok, false);
    assert.equal(
      resolution.ok ? "" : resolution.reason,
      "swcraft.yaml declares views_root: /var/tmp/views, an absolute path — " +
        "it must be written relative to the project directory.",
    );
  } finally {
    cleanup(dir);
  }
});

test("resolveViewsRoot: the project directory itself is refused with the CLI's sentence", () => {
  const dir = mkWorkspace();
  try {
    for (const written of [".", "views/..", "./"]) {
      const resolution = resolveViewsRoot(writeManifest(dir, written));
      assert.equal(resolution.ok, false, written);
      assert.equal(
        resolution.ok ? "" : resolution.reason,
        `swcraft.yaml declares views_root: ${written}, which is the project directory ` +
          "itself — cleaning it would remove the model.",
      );
    }
  } finally {
    cleanup(dir);
  }
});

test("resolveViewsRoot: a views_root outside the project directory is refused with the CLI's sentence", () => {
  const dir = mkWorkspace();
  try {
    const resolution = resolveViewsRoot(writeManifest(dir, "../views"));
    // The sentence spells the paths as the CLI does, symlinks followed (the
    // temporary directory is itself below one on macOS); `path` is lexical.
    const projectRoot = fs.realpathSync(dir);
    const target = path.resolve(projectRoot, "../views");
    assert.deepEqual(resolution, {
      ok: false,
      path: path.resolve(dir, "../views"),
      reason:
        `swcraft.yaml declares views_root: ../views, which resolves to '${target}', ` +
        `outside the project directory '${projectRoot}'.`,
    });
  } finally {
    cleanup(dir);
  }
});

test("resolveViewsRoot: a views_root overlapping model_root is refused with the loader's sentence", () => {
  // The loader's rule 2 (`_overlaps`), in either direction and case-folded; the sentence is
  // `ManifestPathOverlapsModelError.render(full_path=True)`, as `swcraft generate diagrams` prints
  // it after `Error: `.
  const dir = mkWorkspace();
  try {
    const manifest = path.join(dir, MANIFEST_FILENAME);
    const refused: [string, string][] = [
      ["model", "model/views"],
      ["model", "Model/views"],
      ["views/model", "views"],
      ["model", "model"],
    ];
    for (const [modelRoot, viewsRoot] of refused) {
      fs.writeFileSync(manifest, `name: p\nmodel_root: ${modelRoot}\nviews_root: ${viewsRoot}\n`);
      const real = fs.realpathSync(dir);
      const target = path.resolve(real, viewsRoot);
      assert.deepEqual(
        resolveViewsRoot(manifest),
        {
          ok: false,
          path: path.resolve(dir, viewsRoot),
          reason:
            `Manifest field 'views_root' resolves to '${target}', which overlaps 'model_root' ` +
            `('${path.resolve(real, modelRoot)}'). Generated paths must be outside the model: ` +
            "generating or cleaning them would otherwise write into, or delete, the model that " +
            "is the source of truth.",
        },
        `${modelRoot} / ${viewsRoot}`,
      );
    }
    // A sibling sharing the prefix does not overlap.
    fs.writeFileSync(manifest, "name: p\nmodel_root: model\nviews_root: model-views\n");
    assert.deepEqual(resolveViewsRoot(manifest), {
      ok: true,
      path: path.resolve(dir, "model-views"),
    });
    // The three clean-outputs refusals keep their own sentences first.
    fs.writeFileSync(manifest, "name: p\nmodel_root: model\nviews_root: .\n");
    const itself = resolveViewsRoot(manifest);
    assert.match(itself.ok ? "" : itself.reason, /which is the project directory itself/);
  } finally {
    cleanup(dir);
  }
});

test("viewsRootRefusal: a sibling sharing the prefix is outside; a nested directory is inside", () => {
  const root = path.resolve("/proj");
  assert.match(
    viewsRootRefusal(root, "../proj2/views", path.resolve("/proj2/views")) ?? "",
    /outside the project directory/,
  );
  assert.equal(viewsRootRefusal(root, "a/b/c", path.resolve("/proj/a/b/c")), undefined);
  // Exact case, as `Path.is_relative_to` compares: a case-only difference is outside.
  assert.match(
    viewsRootRefusal(root, "../PROJ/views", path.resolve("/PROJ/views")) ?? "",
    /outside the project directory/,
  );
});

test("viewsRootRefusal: absolute is judged on both platforms' rules, as is_absolute_anywhere judges it", () => {
  // As CPython 3.11's `project.is_absolute_anywhere` answers: a POSIX root, a drive with a root and
  // a UNC share are absolute; a drive-relative `C:v`, a bare-rooted `\\v` and a lone `\\\\host` are
  // not, where `path.win32.isAbsolute` answers `true` for the last two. The absolute sentence wins
  // over the containment one, as in the CLI, so both hosts answer alike.
  const root = path.resolve("/proj");
  const inside = path.resolve("/proj/x");
  for (const written of ["/tmp/v", "//host/share/v", "C:\\v", "C:/v", "c:\\x", "\\\\host\\share"]) {
    assert.match(viewsRootRefusal(root, written, inside) ?? "", /an absolute path/, written);
  }
  for (const written of ["C:v", "\\v", "\\\\host", "views", "../v"]) {
    assert.doesNotMatch(viewsRootRefusal(root, written, inside) ?? "", /an absolute path/, written);
  }
});

test("the manifests in this repository resolve views_root without a refusal", () => {
  const repoRoot = path.resolve(__dirname, "..", "..");
  const manifests = [
    path.join(repoRoot, "examples", "basic", MANIFEST_FILENAME),
    path.join(
      repoRoot,
      "packages",
      "tools",
      "src",
      "swcraft_tools",
      "templates",
      "vehicle-architecture",
      MANIFEST_FILENAME,
    ),
  ].filter((candidate) => fs.existsSync(candidate));
  assert.ok(manifests.length >= 1, "no manifest found to check");
  for (const manifest of manifests) {
    assert.deepEqual(resolveViewsRoot(manifest), {
      ok: true,
      path: path.resolve(path.dirname(manifest), "views"),
    });
  }
});

// --- outputs.arxml (the ARXML the Build item marks) ----------------------------
//
// The file `generate arxml` writes, read from the manifest's nested `outputs.arxml` so that the
// item can tell *absent* from *out of date*. Each expectation is what `yaml.safe_load` answers for
// the same text.

test("parseOutputsArxml: the arxml key of the top-level outputs block", () => {
  assert.equal(parseOutputsArxml("outputs:\n  arxml: a.arxml # c\n  needs: r\n"), "a.arxml");
  assert.equal(parseOutputsArxml('outputs:\n  needs: r\n\n  # x\n  arxml: "b # c"\n'), "b # c");
  assert.equal(parseOutputsArxml("outputs:\n    arxml: c\n"), "c");
  assert.equal(parseOutputsArxml("outputs :\n  arxml : d\n"), "d");
  // A tab as separation, as the CLI's reader (`yaml.CSafeLoader`) takes it: each of these reads
  // `{'outputs': {'arxml': 'e'}}`.
  assert.equal(parseOutputsArxml("outputs\t:\n  arxml\t: e\n"), "e");
  assert.equal(parseOutputsArxml("outputs:\t# c\n  arxml:\te\n"), "e");
});

test("parseOutputsArxml: absent, empty, or outside the block falls back to the Pydantic default", () => {
  assert.equal(DEFAULT_ARXML_OUTPUT, "dist/model.arxml");
  assert.equal(parseOutputsArxml("name: x\n"), DEFAULT_ARXML_OUTPUT);
  assert.equal(parseOutputsArxml("name: x\narxml: top\n"), DEFAULT_ARXML_OUTPUT);
  assert.equal(parseOutputsArxml("outputs:\n  needs: r\narxml: top\n"), DEFAULT_ARXML_OUTPUT);
  assert.equal(parseOutputsArxml("outputs:\n  arxml:\n"), DEFAULT_ARXML_OUTPUT);
  assert.equal(
    parseOutputsArxml("other:\n  arxml: nested-elsewhere\n"),
    DEFAULT_ARXML_OUTPUT,
    "an arxml key under another block is not the output",
  );
});

test("parseDistRoot and parseOutputsNeeds: the two fields only the verdict reads, with their defaults", () => {
  assert.equal(parseDistRoot("name: x\n"), "dist");
  assert.equal(parseDistRoot("dist_root: build # c\n"), "build");
  assert.equal(DEFAULT_NEEDS_OUTPUT, "dist/needs.json", "Python's OutputPaths.needs default");
  assert.equal(parseOutputsNeeds("name: x\n"), DEFAULT_NEEDS_OUTPUT);
  assert.equal(parseOutputsNeeds("outputs:\n  arxml: a\n  needs: docs/n.json\n"), "docs/n.json");
  assert.equal(parseOutputsArxml("outputs:\n  needs: docs/n.json\n"), DEFAULT_ARXML_OUTPUT);
});

test("parseOutputsNeeds: the retired rst key is not read, so the default stands", () => {
  // `outputs.rst` is an unknown manifest key (`check-model` reports it); the
  // panel neither reads it nor judges it as an output path.
  assert.equal(parseOutputsNeeds("outputs:\n  rst: dist/needs\n"), DEFAULT_NEEDS_OUTPUT);
  assert.ok(!(MANIFEST_PATH_FIELDS as readonly string[]).includes("outputs.rst"));
  assert.ok((MANIFEST_PATH_FIELDS as readonly string[]).includes("outputs.needs"));
});

// --- readManifestPaths: the loader's three rules, one test per rule ------------
//
// `_validate_project_paths` (packages/tools/src/swcraft_tools/project.py). Each expectation is
// what `require_project` answers on CPython 3.11: the field its refusal names and the rule it
// breaks; every accepted manifest loads.

function verdict(dir: string, manifest: string): unknown {
  const manifestPath = path.join(dir, MANIFEST_FILENAME);
  fs.writeFileSync(manifestPath, manifest, "utf8");
  return readManifestPaths(manifestPath).refusal;
}

test("readManifestPaths: every field resolved against the manifest's directory, the defaults accepted", () => {
  const dir = mkWorkspace();
  try {
    const manifestPath = path.join(dir, MANIFEST_FILENAME);
    const unread = readManifestPaths(manifestPath);
    assert.equal(unread.refusal, undefined, "an unreadable manifest answers the defaults");
    assert.deepEqual(unread.resolved, {
      model_root: path.resolve(dir, "model"),
      dist_root: path.resolve(dir, "dist"),
      views_root: path.resolve(dir, "views"),
      "outputs.arxml": path.resolve(dir, "dist", "model.arxml"),
      "outputs.needs": path.resolve(dir, "dist", "needs.json"),
    });
    const absoluteInside = path.join(path.resolve(dir), "out", "m.arxml");
    const accepted = [
      "name: p\nmodel_root: src/model\ndist_root: build\nviews_root: build/views\n" +
        "outputs:\n  arxml: src/x.arxml\n  needs: build/needs.json\n",
      // Rule 3 is the two roots' alone: an absolute output inside the project loads.
      `name: p\noutputs:\n  arxml: ${absoluteInside}\n`,
      "name: p\nmodel_root: model\nviews_root: model-views\n",
    ];
    for (const manifest of accepted) {
      assert.equal(verdict(dir, manifest), undefined, manifest);
    }
  } finally {
    cleanup(dir);
  }
});

test("readManifestPaths: an outputs.needs inside the project is accepted, as the loader accepts it", () => {
  const dir = mkWorkspace();
  try {
    const absoluteInside = path.join(path.resolve(dir), "publish", "needs.json");
    for (const needs of [
      "dist/needs.json",
      "publish/architecture.json",
      absoluteInside,
      "needs.json",
    ]) {
      const manifest = `name: p\noutputs:\n  needs: ${needs}\n`;
      assert.equal(verdict(dir, manifest), undefined, manifest);
    }
    fs.writeFileSync(
      path.join(dir, MANIFEST_FILENAME),
      "name: p\noutputs:\n  needs: publish/n.json\n",
      "utf8",
    );
    assert.equal(
      readManifestPaths(path.join(dir, MANIFEST_FILENAME)).resolved["outputs.needs"],
      path.resolve(dir, "publish", "n.json"),
    );
  } finally {
    cleanup(dir);
  }
});

test("readManifestPaths rule 3: dist_root or views_root written absolute, judged first", () => {
  const dir = mkWorkspace();
  try {
    const refused: [string, unknown][] = [
      ["dist_root: /var/tmp/dist\n", { field: "dist_root", rule: "absolute" }],
      ["views_root: C:\\views\n", { field: "views_root", rule: "absolute" }],
      ["views_root: //host/share/v\n", { field: "views_root", rule: "absolute" }],
      // Before rule 1 reads model_root's escape, as the loader orders it.
      ["model_root: ..\ndist_root: /var/tmp/dist\n", { field: "dist_root", rule: "absolute" }],
      // dist_root before views_root.
      ["dist_root: /a\nviews_root: /b\n", { field: "dist_root", rule: "absolute" }],
    ];
    for (const [manifest, expected] of refused) {
      assert.deepEqual(verdict(dir, `name: p\n${manifest}`), expected, manifest);
    }
  } finally {
    cleanup(dir);
  }
});

test("readManifestPaths rule 1: any of the five fields resolving outside the project directory", () => {
  const dir = mkWorkspace();
  try {
    const refused: [string, unknown][] = [
      ["model_root: ..\n", { field: "model_root", rule: "outside" }],
      ["model_root: /\n", { field: "model_root", rule: "outside" }],
      ["dist_root: ../dist\n", { field: "dist_root", rule: "outside" }],
      ["views_root: ../views\n", { field: "views_root", rule: "outside" }],
      ["outputs:\n  arxml: ../m.arxml\n", { field: "outputs.arxml", rule: "outside" }],
      [
        `outputs:\n  arxml: ${path.join(os.tmpdir(), "elsewhere.arxml")}\n`,
        { field: "outputs.arxml", rule: "outside" },
      ],
      ["outputs:\n  needs: ../needs.json\n", { field: "outputs.needs", rule: "outside" }],
      // Before rule 2: model_root's escape is reported, not an overlap it causes.
      ["model_root: ..\ndist_root: dist\n", { field: "model_root", rule: "outside" }],
    ];
    for (const [manifest, expected] of refused) {
      assert.deepEqual(verdict(dir, `name: p\n${manifest}`), expected, manifest);
    }
  } finally {
    cleanup(dir);
  }
});

test("readManifestPaths rule 2: a generated path overlapping model_root, either way and case-folded", () => {
  const dir = mkWorkspace();
  try {
    const refused: [string, unknown][] = [
      ["dist_root: model/dist\n", { field: "dist_root", rule: "overlap" }],
      ["views_root: Model/views\n", { field: "views_root", rule: "overlap" }],
      ["outputs:\n  arxml: model/swc/A.yaml\n", { field: "outputs.arxml", rule: "overlap" }],
      ["outputs:\n  needs: model\n", { field: "outputs.needs", rule: "overlap" }],
      ["outputs:\n  needs: model/needs.json\n", { field: "outputs.needs", rule: "overlap" }],
      // The model inside an output root: the field that contains it is named.
      ["model_root: dist/model\n", { field: "dist_root", rule: "overlap" }],
      ["model_root: .\n", { field: "dist_root", rule: "overlap" }],
    ];
    for (const [manifest, expected] of refused) {
      assert.deepEqual(verdict(dir, `name: p\n${manifest}`), expected, manifest);
    }
  } finally {
    cleanup(dir);
  }
});

// Rule 4, measured against `require_project` (`test_project.py`'s
// `test_an_outputs_needs_naming_a_file_the_project_keeps_is_refused`, the same cases).
test("readManifestPaths rule 4: an outputs.needs naming a file the project keeps, case-folded", () => {
  const dir = mkWorkspace();
  try {
    const collides = { field: "outputs.needs", rule: "collides" };
    const refused: [string, unknown][] = [
      ["outputs:\n  needs: dist/model.arxml\n", collides],
      ["outputs:\n  arxml: out/m.arxml\n  needs: OUT/m.ARXML\n", collides],
      ["outputs:\n  needs: dist/model_old.arxml\n", collides],
      // A name ending in `.` has no suffix to Python 3.11's pathlib: the previous file is `model._old`.
      ["outputs:\n  arxml: dist/model.\n  needs: dist/model._old\n", collides],
      ["outputs:\n  needs: dist/audit.jsonl\n", collides],
      ["outputs:\n  needs: swcraft.yaml\n", collides],
      ["outputs:\n  needs: dist/build-report.md\n", collides],
      ["outputs:\n  needs: Dist/Build-Report.MD\n", collides],
      ["outputs:\n  needs: views/index.html\n", collides],
      ["outputs:\n  needs: views/report.txt\n", collides],
      ["outputs:\n  needs: views/ibd-Chassis.svg\n", collides],
      ["outputs:\n  needs: Views/needs.HTML\n", collides],
      ["outputs:\n  needs: dist/metrics/metrics.md\n", collides],
      ["outputs:\n  needs: dist/metrics\n", collides],
      ["outputs:\n  needs: dist/resource-report/resource-report.md\n", collides],
      ["outputs:\n  needs: dist/change-report/change-report.md\n", collides],
      ["outputs:\n  needs: dist/change-report/change-report-pr.md\n", collides],
      ["outputs:\n  needs: Dist/Change-Report/change-report.JSON\n", collides],
      ["outputs:\n  needs: dist/change-report/overview-x-before.svg\n", collides],
      ["outputs:\n  needs: dist/test-tables\n", collides],
      ["outputs:\n  needs: Dist/TEST-TABLES\n", collides],
      ["outputs:\n  needs: dist/test-tables/needs.json\n", collides],
      ["outputs:\n  needs: Dist/Test-Tables/manifest.JSON\n", collides],
      ["outputs:\n  needs: dist/components\n", collides],
      ["outputs:\n  needs: dist/experiments\n", collides],
      ["outputs:\n  needs: dist/model.arxml.json\n", undefined],
      ["outputs:\n  needs: dist/audit.json\n", undefined],
      ["outputs:\n  needs: dist/sub/audit.jsonl\n", undefined],
      ["outputs:\n  needs: swcraft.json\n", undefined],
      ["outputs:\n  needs: dist/build-report.json\n", undefined],
      ["outputs:\n  needs: views/needs.json\n", undefined],
      ["outputs:\n  needs: views/sub/index.html\n", undefined],
      ["outputs:\n  needs: dist/index.html\n", undefined],
      ["outputs:\n  needs: dist/metrics.md\n", undefined],
      ["outputs:\n  needs: dist/metrics-needs.json\n", undefined],
      ["outputs:\n  needs: dist/sub/metrics/metrics.md\n", undefined],
      ["outputs:\n  needs: dist/change-report.json\n", undefined],
      ["outputs:\n  needs: dist/components/needs.json\n", undefined],
      ["outputs:\n  needs: dist/experiments/needs.json\n", undefined],
      ["outputs:\n  needs: metrics/metrics.md\n", undefined],
      ["outputs:\n  needs: dist/test-tables2\n", undefined],
      ["outputs:\n  needs: dist/test-tables2/needs.json\n", undefined],
      ["outputs:\n  needs: dist/test-tables.json\n", undefined],
      ["outputs:\n  arxml: dist/model.\n  needs: dist/model_old.\n", undefined],
    ];
    for (const [manifest, expected] of refused) {
      assert.deepEqual(verdict(dir, `name: p\n${manifest}`), expected, manifest);
    }
  } finally {
    cleanup(dir);
  }
});

test("manifestPathRefusal: containment is exact-case where the overlap is folded", () => {
  const root = path.resolve("/proj");
  const at = (relative: string): string => path.resolve(root, relative);
  const written = {
    model_root: "model",
    dist_root: "dist",
    views_root: "views",
    "outputs.arxml": "dist/model.arxml",
    "outputs.needs": "dist/needs.json",
  };
  const resolved = {
    model_root: at("model"),
    dist_root: at("dist"),
    views_root: path.resolve("/PROJ/views"),
    "outputs.arxml": at("dist/model.arxml"),
    "outputs.needs": at("dist/needs.json"),
  };
  assert.deepEqual(manifestPathRefusal(root, written, resolved), {
    field: "views_root",
    rule: "outside",
  });
});

// --- symlinks: the verdict follows them, as `Path.resolve()` does -------------
//
// `require_project` judges the five fields after `Path.resolve()`, which follows symlinks
// (`packages/tools/tests/test_project.py`'s
// `test_a_generated_root_symlinked_into_the_model_is_rejected`); every refusal below is its answer
// on the same layout. A host that cannot make a symlink (Windows without the privilege) skips, as
// `symlink_support.py` does, by attempting one.

function symlinkOrSkip(t: TestContext, target: string, link: string): boolean {
  try {
    fs.symlinkSync(target, link, "dir");
    return true;
  } catch (error) {
    t.skip(`symlinks are not available here: ${String(error)}`);
    return false;
  }
}

test("readManifestPaths: a generated root symlinked into the model or out of the project is refused", (t) => {
  const dir = mkWorkspace();
  const outside = mkWorkspace();
  try {
    fs.mkdirSync(path.join(dir, "model", "generated"), { recursive: true });
    fs.mkdirSync(path.join(outside, "deep", "dir"), { recursive: true });
    if (!symlinkOrSkip(t, path.join(dir, "model", "generated"), path.join(dir, "dist"))) {
      return;
    }
    // The lexical spelling of every field is inside the project and apart from
    // the model; only the link makes `dist` the model's.
    assert.deepEqual(verdict(dir, "name: p\n"), { field: "dist_root", rule: "overlap" });
    assert.equal(resolveModelRoot(path.join(dir, MANIFEST_FILENAME)), undefined);
    fs.unlinkSync(path.join(dir, "dist"));

    symlinkOrSkip(t, outside, path.join(dir, "views"));
    assert.deepEqual(verdict(dir, "name: p\n"), { field: "views_root", rule: "outside" });
    const views = resolveViewsRoot(path.join(dir, MANIFEST_FILENAME));
    assert.deepEqual(views, {
      ok: false,
      path: path.resolve(dir, "views"),
      reason:
        `swcraft.yaml declares views_root: views, which resolves to '${fs.realpathSync(outside)}', ` +
        `outside the project directory '${fs.realpathSync(dir)}'.`,
    });
    fs.unlinkSync(path.join(dir, "views"));

    // On POSIX a link is replaced by its target BEFORE a following `..` is applied, so
    // `lnk/../views` is `<outside>/deep/views`, where `path.resolve()` said `<project>/views`. On
    // Windows `ntpath.realpath` runs `normpath` first (CPython 3.11), so the `..` swallows `lnk`
    // before any link is read and the manifest names `<project>/views`, inside the project:
    // accepted, as `require_project` accepts it there.
    symlinkOrSkip(t, path.join(outside, "deep", "dir"), path.join(dir, "lnk"));
    assert.deepEqual(
      verdict(dir, "name: p\nviews_root: lnk/../views\n"),
      process.platform === "win32" ? undefined : { field: "views_root", rule: "outside" },
    );

    // An output path through a link into the model.
    symlinkOrSkip(t, path.join(dir, "model"), path.join(dir, "out"));
    assert.deepEqual(verdict(dir, "name: p\noutputs:\n  arxml: out/m.arxml\n"), {
      field: "outputs.arxml",
      rule: "overlap",
    });
  } finally {
    cleanup(dir);
    cleanup(outside);
  }
});

test("readManifestPaths: a symlink loop is refused, as Path.resolve() raises on one", (t) => {
  if (process.platform === "win32") {
    t.skip("a loop on Windows is a stated limit of pythonResolve()");
    return;
  }
  const dir = mkWorkspace();
  try {
    if (!symlinkOrSkip(t, "b", path.join(dir, "a"))) {
      return;
    }
    fs.symlinkSync("a", path.join(dir, "b"));
    assert.deepEqual(verdict(dir, "name: p\nviews_root: a/x\n"), {
      field: "views_root",
      rule: "loop",
    });
    assert.throws(() => pythonResolve(path.join(dir, "a", "x")), /Symlink loop from/);
  } finally {
    cleanup(dir);
  }
});

test("readManifestPaths: a project below a symlinked directory is accepted, acted on lexically", (t) => {
  const real = mkWorkspace();
  const holder = mkWorkspace();
  try {
    const linked = path.join(holder, "project");
    if (!symlinkOrSkip(t, real, linked)) {
      return;
    }
    const manifestPath = path.join(linked, MANIFEST_FILENAME);
    fs.writeFileSync(manifestPath, "name: p\n", "utf8");
    const paths = readManifestPaths(manifestPath);
    assert.equal(paths.refusal, undefined);
    // Where the extension acts: spelled as the workspace spells its files.
    assert.equal(paths.resolved.model_root, path.join(linked, "model"));
    assert.equal(resolveModelRoot(manifestPath), path.join(linked, "model"));
    // What the loader judges: the link followed.
    assert.equal(paths.realProjectRoot, fs.realpathSync(real));
    assert.equal(paths.real.model_root, path.join(fs.realpathSync(real), "model"));
  } finally {
    cleanup(holder);
    cleanup(real);
  }
});

test("readManifestPaths: the acted-on path is derived from the judged one, where the lexical join differs", (t) => {
  // `lnk/../views` with `lnk -> <project>/deep/dir`: on POSIX `require_project` accepts it and
  // resolves it to `<project>/deep/views`, where `path.resolve()` says `<project>/views`, a
  // directory the CLI never writes. The acted-on path is the judged one re-rooted at the
  // workspace's own spelling of the project. On Windows `ntpath.realpath` collapses the `..` before
  // it reads the link, so the judged path is `<project>/views` and the workspace spelling is kept.
  const below = process.platform === "win32" ? ["views"] : ["deep", "views"];
  const real = mkWorkspace();
  const holder = mkWorkspace();
  try {
    fs.mkdirSync(path.join(real, "deep", "dir"), { recursive: true });
    if (!symlinkOrSkip(t, path.join(real, "deep", "dir"), path.join(real, "lnk"))) {
      return;
    }
    const linked = path.join(holder, "project");
    fs.symlinkSync(real, linked);
    const manifestPath = path.join(linked, MANIFEST_FILENAME);
    fs.writeFileSync(manifestPath, "name: p\nviews_root: lnk/../views\n", "utf8");
    const paths = readManifestPaths(manifestPath);
    assert.equal(paths.refusal, undefined);
    assert.equal(paths.real.views_root, path.join(fs.realpathSync(real), ...below));
    assert.equal(paths.resolved.views_root, path.join(linked, ...below));
    assert.deepEqual(resolveViewsRoot(manifestPath), {
      ok: true,
      path: path.join(linked, ...below),
    });
  } finally {
    cleanup(holder);
    cleanup(real);
  }
});

test("readManifestPaths: for every accepted manifest, the acted-on path resolves to the judged one", (t) => {
  // The property that closes the family: `resolved` is derived from `real`, so for every field
  // of every accepted manifest Python's resolution of the path the extension acts on IS the path
  // the loader judged, and it is spelled below the workspace's own project directory. Measured on
  // this host's filesystem; the Windows half (a junction to another drive, `C:views`) is
  // `pythonResolve()`'s and unmeasured against CPython.
  const real = mkWorkspace();
  const holder = mkWorkspace();
  try {
    fs.mkdirSync(path.join(real, "deep", "dir"), { recursive: true });
    fs.mkdirSync(path.join(real, "build"), { recursive: true });
    if (!symlinkOrSkip(t, path.join(real, "deep", "dir"), path.join(real, "lnk"))) {
      return;
    }
    fs.symlinkSync(path.join(real, "build"), path.join(real, "dist"));
    const linked = path.join(holder, "project");
    fs.symlinkSync(real, linked);
    const manifests = [
      "name: p\n",
      "name: p\nmodel_root: src/model\ndist_root: build\nviews_root: build/views\n" +
        "outputs:\n  arxml: src/x.arxml\n  needs: build/needs.json\n",
      "name: p\nviews_root: lnk/../views\n",
      "name: p\nmodel_root: lnk/../m\ndist_root: lnk\noutputs:\n  arxml: lnk/../../o/a.arxml\n",
      "name: p\nviews_root: .\\x\nmodel_root: ./model/../model\n",
      "name: p\nviews_root: dist/views\n",
    ];
    let accepted = 0;
    for (const root of [real, linked]) {
      for (const manifest of manifests) {
        const manifestPath = path.join(root, MANIFEST_FILENAME);
        fs.writeFileSync(manifestPath, manifest, "utf8");
        const paths = readManifestPaths(manifestPath);
        if (paths.refusal !== undefined) {
          continue;
        }
        accepted += 1;
        for (const field of MANIFEST_PATH_FIELDS) {
          const where = `${root} ${field} ${JSON.stringify(manifest)}`;
          assert.equal(pythonResolve(paths.resolved[field]), paths.real[field], where);
          assert.ok(
            paths.resolved[field] === paths.projectRoot ||
              paths.resolved[field].startsWith(paths.projectRoot + path.sep),
            where,
          );
        }
      }
    }
    assert.ok(accepted >= 10, `only ${accepted} manifests accepted`);
  } finally {
    cleanup(holder);
    cleanup(real);
  }
});

test("pythonResolve: a missing component is kept as written and the walk goes on", () => {
  const dir = mkWorkspace();
  try {
    const real = fs.realpathSync(dir);
    assert.equal(pythonResolve(path.join(dir, "absent", "..", "x")), path.join(real, "x"));
    assert.equal(pythonResolve(dir), real);
  } finally {
    cleanup(dir);
  }
});

test("joinLikePathlib: a Windows drive-relative or rooted value joins as PureWindowsPath joins it", () => {
  // As PureWindowsPath in CPython 3.11 joins: `C:/proj` / `C:model` is `C:\proj\model`, `C:/proj` /
  // `\model` is `C:\model`, and `D:/proj` / `C:model` is `C:model`, read against the C: drive's own
  // current directory, never a folder inside the project. Appending would give
  // `D:\proj\C:model`, which rule 1 accepts as inside the project.
  assert.equal(joinLikePathlib("C:\\proj", "C:model", path.win32), "C:\\proj\\model");
  assert.equal(joinLikePathlib("C:\\proj", "\\model", path.win32), "C:\\model");
  assert.equal(joinLikePathlib("C:\\proj", "model\\..\\x", path.win32), "C:\\proj\\x");
  const otherDrive = joinLikePathlib("D:\\proj", "C:model", path.win32);
  assert.match(otherDrive, /^C:\\/);
  assert.equal(otherDrive.toLowerCase().startsWith("d:\\proj"), false, otherDrive);
  // POSIX keeps `..` for the link walk, and an absolute value replaces the directory.
  assert.equal(joinLikePathlib("/proj", "a/../b", path.posix), "/proj/a/../b");
  assert.equal(joinLikePathlib("/proj", "/elsewhere", path.posix), "/elsewhere");
});

// Held equal to `writer.arxml_xml.previous_arxml_path` by `test_project.py`'s
// `test_the_extension_splits_the_previous_arxml_name_as_python_does`, which reads this table.
const PREVIOUS_ARXML_NAMES: readonly (readonly [string, string])[] = [
  ["model.arxml", "model_old.arxml"],
  ["model.", "model._old"],
  ["model..", "model.._old"],
  ["a..b", "a._old.b"],
  [".hidden", ".hidden_old"],
  [".hidden.arxml", ".hidden_old.arxml"],
  ["..x", "._old.x"],
  ["...", "..._old"],
  ["x.tar.gz", "x.tar_old.gz"],
  ["model", "model_old"],
];

test("previousArxmlPath splits a name as Python 3.11's pathlib does", () => {
  for (const [name, previous] of PREVIOUS_ARXML_NAMES) {
    assert.equal(
      project.previousArxmlPath(path.join("dist", name)),
      path.join("dist", previous),
      name,
    );
  }
});

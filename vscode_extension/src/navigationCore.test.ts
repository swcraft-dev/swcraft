// The editor's navigation core: the key path under the cursor, the one scalar reader, the
// reference lookup and the definition resolver — every decision Go to Definition makes, tested
// on text and a fake file system. No `vscode` import: this runs under plain `node --test`.
//
// The Impact view's documents under `src/fixtures/impact/` are recorded from the CLI on
// `examples/basic`; re-record them, after reading the difference, with
// `.venv/bin/python packages/tools/tests/test_impact_panel_fixtures.py` (its tests hold every
// file equal to a fresh run).
import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { test } from "node:test";

import type { ChildCapture } from "./commandCore";
import { parseElementsBlock } from "./diagramsTab";
import {
  DIAGRAM_VIEW_PREFIXES,
  decodeModelText,
  definitionTarget,
  dependentLocations,
  HOVER_EXCERPT_LINES,
  hoverCard,
  hoverMarkdown,
  IMPACT_SCHEMA_VERSION,
  ImpactDocumentError,
  type ImpactRow,
  identityAt,
  itemExtent,
  itemScalarAt,
  keyPathAt,
  type LayerFacts,
  lensPlan,
  lineShapes,
  loadReferences,
  locateSubject,
  MAX_GROUP_DEPTH,
  memberLinesOf,
  memberReferenceAt,
  parseImpactDocument,
  type Reader,
  type ReferenceEdge,
  type References,
  ReferencesArtifactError,
  referenceAt,
  referenceLocations,
  referencesAnswer,
  relationAt,
  requestReader,
  requirementMemberLines,
  resolveDefinition,
  resolveElement,
  resolveMemberReference,
  resolveRelation,
  scalarValueAt,
  topLevelScalar,
  unsavedIdentityAnswer,
} from "./navigationCore";
import { readManifestPaths } from "./project";
import {
  BASIC_MODEL,
  diskReader,
  functionBody,
  impactFixture,
  PROJECT_ROOT,
  readSource,
  SCHEMAS_DIR,
  withoutCommentLines,
} from "./testSupport";

const MODEL_ROOT = path.resolve("/projects/vc/model");

function shippedText(): string {
  return fs.readFileSync(path.join(SCHEMAS_DIR, "references.json"), "utf8");
}

function shipped(): References {
  return loadReferences(shippedText());
}

/** The text of *lines*, and the 0-based index of the line holding *marker*. */
function doc(lines: string[]): string {
  return `${lines.join("\n")}\n`;
}

function lineOf(text: string, needle: string, occurrence = 0): number {
  const lines = text.split("\n");
  let seen = 0;
  for (let i = 0; i < lines.length; i += 1) {
    if (lines[i].includes(needle)) {
      if (seen === occurrence) {
        return i;
      }
      seen += 1;
    }
  }
  throw new Error(`no line holds ${needle} (#${occurrence})`);
}

function edge(refs: References, field: string): ReferenceEdge {
  const found = refs.references.find((candidate) => candidate.field === field);
  assert.ok(found, `no edge ${field}`);
  return found;
}

// --- loadReferences --------------------------------------------------------

test("the shipped references.json loads: every layer and every edge", () => {
  const refs = shipped();
  const raw = JSON.parse(shippedText()) as { layers: object; references: unknown[] };
  assert.equal(refs.layers.size, Object.keys(raw.layers).length);
  assert.equal(refs.references.length, raw.references.length);
  assert.ok(refs.references.length > 0);
  assert.equal(refs.layers.get("swc")?.nesting, "group");
  assert.equal(refs.layers.get("interfaces")?.discriminator, "kind");
});

test("a malformed artifact is a refusal naming the key", () => {
  const base = JSON.parse(shippedText()) as {
    layers: Record<string, Record<string, unknown>>;
    references: Array<Record<string, unknown>>;
  };
  const cases: Array<[string, (document: typeof base) => unknown, RegExp]> = [
    ["no layers", (d) => ({ references: d.references }), /layers must be a non-empty object/],
    [
      "a layer without a token",
      (d) => {
        delete d.layers.swc.token;
        return d;
      },
      /layers\.swc\.token must be a non-empty string/,
    ],
    [
      "a layer without a guide page",
      (d) => {
        delete d.layers.interfaces.guidePage;
        return d;
      },
      /layers\.interfaces\.guidePage must be a non-empty string/,
    ],
    [
      "a guide page that is not a string",
      (d) => {
        d.layers.tasks.guidePage = ["reference/model/tasks"];
        return d;
      },
      /layers\.tasks\.guidePage must be a non-empty string/,
    ],
    [
      "a kind without a guide anchor",
      (d) => {
        delete (d.layers.interfaces.topLevelKinds as Array<Record<string, unknown>>)[1].guideAnchor;
        return d;
      },
      /layers\.interfaces\.topLevelKinds\[1\]\.guideAnchor must be a non-empty string/,
    ],
    [
      "a guide anchor that is not a string",
      (d) => {
        (d.layers.swc.topLevelKinds as Array<Record<string, unknown>>)[0].guideAnchor = 3;
        return d;
      },
      /layers\.swc\.topLevelKinds\[0\]\.guideAnchor must be a non-empty string/,
    ],
    [
      "a discriminator value on a single-kind layer",
      (d) => {
        (d.layers.swc.topLevelKinds as Array<Record<string, unknown>>)[0].discriminatorValue = "x";
        return d;
      },
      /layers\.swc\.topLevelKinds\[0\]\.discriminatorValue/,
    ],
    [
      "no discriminator value on a multi-kind layer",
      (d) => {
        (
          d.layers.interfaces.topLevelKinds as Array<Record<string, unknown>>
        )[0].discriminatorValue = null;
        return d;
      },
      /layers\.interfaces\.topLevelKinds\[0\]\.discriminatorValue must be null exactly when/,
    ],
    [
      "references not an array",
      (d) => ({ layers: d.layers, references: {} }),
      /references must be an array/,
    ],
    [
      "an edge to an undeclared layer",
      (d) => {
        d.references[0].targetLayer = "nowhere";
        return d;
      },
      /references\[0\]\.targetLayer names 'nowhere', which is no layer/,
    ],
    [
      "an edge with no path",
      (d) => {
        d.references[0].paths = [];
        return d;
      },
      /references\[0\]\.paths must be a non-empty array/,
    ],
    [
      "a path step that is not a string",
      (d) => {
        d.references[0].paths = [{ topKind: "swc", path: ["ports", 1] }];
        return d;
      },
      /references\[0\]\.paths\[0\]\.path must hold non-empty strings only/,
    ],
  ];
  for (const [label, damage, message] of cases) {
    const copy = structuredClone(base);
    assert.throws(
      () => loadReferences(JSON.stringify(damage(copy))),
      (error: unknown) => error instanceof ReferencesArtifactError && message.test(error.message),
      label,
    );
  }
  assert.throws(() => loadReferences("{not json"), /references\.json: not JSON/);
  assert.throws(() => loadReferences("[]"), /the document must be an object/);
});

test("a row's sourceKind and sourceField are not read, so a damaged one leaves navigation on", () => {
  const base = JSON.parse(shippedText()) as { references: Array<Record<string, unknown>> };
  delete base.references[0].sourceKind;
  base.references[0].sourceField = 7;
  const refs = loadReferences(JSON.stringify(base));
  assert.equal(refs.references.length, base.references.length);
  assert.throws(() => loadReferences("[]"), /the document must be an object/);
});

// --- keyPathAt and scalarValueAt ------------------------------------------

const INDENTED_PORTS = doc([
  "name: C",
  "ports:",
  "  - name: rIn",
  "    direction: require",
  "    interface: ISig",
  "  - interface: IOther",
  "    name: rOut",
]);

const INDENTLESS_PORTS = doc([
  "name: C",
  "ports:",
  "- name: rIn",
  "  direction: require",
  "  interface: ISig",
  "- interface: IOther",
  "  name: rOut",
  "runnables: []",
]);

test("the key path of a key in a sequence item, in both sequence styles", () => {
  for (const text of [INDENTED_PORTS, INDENTLESS_PORTS]) {
    const line = lineOf(text, "interface: ISig");
    assert.deepEqual(keyPathAt(text, line), ["ports", "*", "interface"]);
    assert.equal(scalarValueAt(text, line), "ISig");
    // The key on the item's first line.
    const first = lineOf(text, "- interface: IOther");
    assert.deepEqual(keyPathAt(text, first), ["ports", "*", "interface"]);
    assert.equal(scalarValueAt(text, first), "IOther");
    assert.deepEqual(keyPathAt(text, lineOf(text, "- name: rIn")), ["ports", "*", "name"]);
    // A top-level key.
    assert.deepEqual(keyPathAt(text, 0), ["name"]);
  }
  // The sibling after an indentless sequence is a top-level key again.
  assert.deepEqual(keyPathAt(INDENTLESS_PORTS, lineOf(INDENTLESS_PORTS, "runnables")), [
    "runnables",
  ]);
});

test("a nested mapping: a connector end's swc", () => {
  const text = doc([
    "name: Conn",
    "from:",
    "  swc: A",
    "  port: p",
    "to:",
    "  swc: B",
    "  port: q",
  ]);
  assert.deepEqual(keyPathAt(text, lineOf(text, "swc: A")), ["from", "swc"]);
  assert.deepEqual(keyPathAt(text, lineOf(text, "swc: B")), ["to", "swc"]);
  assert.equal(scalarValueAt(text, lineOf(text, "swc: B")), "B");
});

test("two levels of sequences, in both styles", () => {
  const indented = doc([
    "name: I",
    "kind: client_server",
    "operations:",
    "  - name: op",
    "    arguments:",
    "      - name: a",
    "        application_type: T",
  ]);
  const indentless = doc([
    "name: I",
    "kind: client_server",
    "operations:",
    "- name: op",
    "  arguments:",
    "  - name: a",
    "    application_type: T",
  ]);
  for (const text of [indented, indentless]) {
    assert.deepEqual(keyPathAt(text, lineOf(text, "application_type")), [
      "operations",
      "*",
      "arguments",
      "*",
      "application_type",
    ]);
  }
});

test("blank, comment, flow and block-scalar lines answer nothing", () => {
  const text = doc([
    "name: C",
    "",
    "# a comment",
    "ports: []",
    "interface: [a, b]",
    "description: |",
    "  interface: ISig",
    "type: >",
    "  folded",
  ]);
  assert.equal(keyPathAt(text, 1), undefined);
  assert.equal(keyPathAt(text, 2), undefined);
  assert.equal(scalarValueAt(text, lineOf(text, "ports: []")), undefined);
  assert.equal(scalarValueAt(text, lineOf(text, "interface: [a, b]")), undefined);
  // A line inside a block scalar is text, not a key.
  assert.equal(keyPathAt(text, lineOf(text, "  interface: ISig")), undefined);
  assert.equal(scalarValueAt(text, lineOf(text, "description: |")), undefined);
  assert.equal(scalarValueAt(text, lineOf(text, "type: >")), undefined);
  // Out of range.
  assert.equal(keyPathAt(text, -1), undefined);
  assert.equal(keyPathAt(text, 99), undefined);
  assert.equal(scalarValueAt(text, 99), undefined);
});

test("value spelling: plain, quoted, a trailing comment, '#' inside quotes", () => {
  const cases: Array<[string, string | undefined]> = [
    ["interface: ISig", "ISig"],
    ["interface: 'ISig'", "ISig"],
    ['interface: "ISig"', "ISig"],
    ["interface: ISig # the port's interface", "ISig"],
    ['interface: "a#b"', "a#b"],
    ["interface: 'a#b' # c", "a#b"],
    ["interface: a#b", "a#b"],
    ['interface: "ISig" trailing', undefined],
    ['interface: "a\\"b"', undefined],
    ["interface: 'a''b'", undefined],
    ['interface: "open', undefined],
    ["interface:", undefined],
    ["interface: # only a comment", undefined],
    ["interface: &a ISig", undefined],
    ["interface: *a", undefined],
    ["interface: !!str ISig", undefined],
    ["interface: {a: b}", undefined],
    ["interface: - x", undefined],
    ["interface: a: b", undefined],
  ];
  for (const [line, expected] of cases) {
    assert.equal(scalarValueAt(doc(["name: C", line]), 1), expected, line);
  }
});

test("a plain scalar continued on a deeper line is not the line's text", () => {
  const text = doc(["ports:", "  - name: rIn", "    interface: ISig", "      Extra"]);
  assert.equal(scalarValueAt(text, 2), undefined);
  // A comment or a blank line between does not hide the continuation.
  const spaced = doc(["name: ISig", "", "  Extra"]);
  assert.equal(scalarValueAt(spaced, 0), undefined);
  // The next key of the same mapping is no continuation.
  assert.equal(scalarValueAt(INDENTED_PORTS, lineOf(INDENTED_PORTS, "- name: rIn")), "rIn");
});

test("the reference key repeated in its own mapping, before or after the line, answers nothing", () => {
  for (const text of [
    doc(["ports:", "  - name: rIn", "    interface: IOther", "    interface: ISig"]),
    doc(["ports:", "- name: rIn", "  interface: IOther", "  interface: ISig"]),
  ]) {
    assert.equal(keyPathAt(text, lineOf(text, "IOther")), undefined, "a repeat after");
    assert.equal(keyPathAt(text, lineOf(text, "ISig")), undefined, "a repeat before");
  }
  // The item's first key repeated further down the same item.
  const first = doc(["ports:", "  - interface: IOther", "    interface: ISig"]);
  assert.equal(keyPathAt(first, 1), undefined);
});

test("a repeated top-level ancestor, before or after the cursor's block, in both styles", () => {
  for (const text of [
    doc([
      "name: C",
      "ports:",
      "  - name: a",
      "    interface: X",
      "ports:",
      "  - name: b",
      "    interface: Y",
    ]),
    doc([
      "name: C",
      "ports:",
      "- name: a",
      "  interface: X",
      "ports:",
      "- name: b",
      "  interface: Y",
    ]),
  ]) {
    assert.equal(keyPathAt(text, lineOf(text, "interface: X")), undefined, "a second block after");
    assert.equal(keyPathAt(text, lineOf(text, "interface: Y")), undefined, "a first block before");
  }
});

test("a repeated nested parent answers nothing", () => {
  const connector = doc(["name: Conn", "from:", "  swc: A", "from:", "  swc: B"]);
  assert.equal(keyPathAt(connector, lineOf(connector, "swc: A")), undefined);
  assert.equal(keyPathAt(connector, lineOf(connector, "swc: B")), undefined);
  // A parent repeated inside one sequence item, in both styles.
  for (const text of [
    doc([
      "operations:",
      "  - name: op",
      "    arguments:",
      "      - application_type: T",
      "    arguments:",
      "      - application_type: U",
    ]),
    doc([
      "operations:",
      "- name: op",
      "  arguments:",
      "  - application_type: T",
      "  arguments:",
      "  - application_type: U",
    ]),
  ]) {
    assert.equal(keyPathAt(text, lineOf(text, "application_type: T")), undefined);
    assert.equal(keyPathAt(text, lineOf(text, "application_type: U")), undefined);
  }
});

test("the same key in two separate items: two mappings, both answer", () => {
  for (const text of [
    doc(["ports:", "  - name: a", "    interface: X", "  - name: b", "    interface: Y"]),
    doc(["ports:", "- name: a", "  interface: X", "- name: b", "  interface: Y"]),
  ]) {
    for (const [needle, value] of [
      ["interface: X", "X"],
      ["interface: Y", "Y"],
    ]) {
      assert.deepEqual(keyPathAt(text, lineOf(text, needle)), ["ports", "*", "interface"]);
      assert.equal(scalarValueAt(text, lineOf(text, needle)), value);
    }
  }
});

test("shapes the scanner does not read answer nothing", () => {
  // An indented top-level key, a top-level sequence, a document marker above
  // the path, a scalar-valued parent, a tab in the indentation.
  assert.equal(keyPathAt(doc([" name: C"]), 0), undefined);
  assert.equal(keyPathAt(doc(["- name: C"]), 0), undefined);
  assert.equal(keyPathAt(doc(["---", "name: C"]), 1), undefined);
  assert.equal(keyPathAt(doc(["ports: x", "  interface: ISig"]), 1), undefined);
  assert.equal(keyPathAt(doc(["ports:", "\tinterface: ISig"]), 1), undefined);
  assert.equal(scalarValueAt(doc(["name: ISig", "\tExtra"]), 0), undefined);
  // A bare `-` item and a nested `- -` sequence.
  assert.equal(keyPathAt(doc(["ports:", "  -", "    interface: ISig"]), 2), undefined);
  assert.equal(keyPathAt(doc(["ports:", "  - - interface: ISig"]), 1), undefined);
});

// --- topLevelScalar and referenceAt ----------------------------------------

test("a column-0 key: absent, one, two (refused), continued (refused), indented (not it)", () => {
  assert.deepEqual(topLevelScalar(doc(["kind: a"]), "kind"), {
    status: "one",
    line: 0,
    value: "a",
  });
  assert.deepEqual(topLevelScalar(doc(["name: x"]), "kind"), { status: "absent" });
  assert.deepEqual(topLevelScalar(doc(["kind: a", "kind: a"]), "kind"), { status: "refused" });
  assert.deepEqual(topLevelScalar(doc(["kind: a", "  Extra"]), "kind"), { status: "refused" });
  assert.deepEqual(topLevelScalar(doc(["ports:", "  - kind: a"]), "kind"), { status: "absent" });
});

test("the reference under the cursor: the edge its key path names", () => {
  const refs = shipped();
  const line = lineOf(INDENTLESS_PORTS, "interface: ISig");
  const path0 = keyPathAt(INDENTLESS_PORTS, line);
  assert.ok(path0);
  assert.equal(
    referenceAt(refs, INDENTLESS_PORTS, "swc", path0)?.field,
    "field:swc.port.interface",
  );
  // A key that is no reference.
  assert.equal(referenceAt(refs, INDENTLESS_PORTS, "swc", ["ports", "*", "direction"]), undefined);
  // An undeclared layer.
  assert.equal(referenceAt(refs, INDENTLESS_PORTS, "nowhere", path0), undefined);
});

test("the top-level discriminator decides between two edges of one field name", () => {
  const refs = shipped();
  const value = doc(["name: T", "kind: value_type", "base: uint8"]);
  const reference = doc(["name: T", "kind: reference_type", "base: Other"]);
  assert.equal(
    referenceAt(refs, value, "implementation_types", ["base"])?.targetLayer,
    "base_types",
  );
  assert.equal(
    referenceAt(refs, reference, "implementation_types", ["base"])?.targetLayer,
    "implementation_types",
  );
});

test("a multi-kind file answers only with exactly one readable discriminator line", () => {
  const refs = shipped();
  for (const [label, text] of [
    ["no kind line", doc(["name: T", "base: uint8"])],
    [
      "two equal kind lines",
      doc(["name: T", "kind: value_type", "kind: value_type", "base: uint8"]),
    ],
    ["two kind lines", doc(["name: T", "kind: value_type", "kind: reference_type", "base: uint8"])],
    ["a continued kind", doc(["name: T", "kind: value_type", "  Extra", "base: uint8"])],
    ["an undeclared kind", doc(["name: T", "kind: no_such_kind", "base: uint8"])],
  ]) {
    assert.equal(referenceAt(refs, text, "implementation_types", ["base"]), undefined, label);
  }
});

// --- resolveDefinition -----------------------------------------------------

/**
 * A fake file system over *files* (absolute path → text). Case-insensitive by
 * default, as macOS's and Windows's are; `caseSensitive` answers the exact
 * spelling only, as Linux does.
 */
function fakeReader(files: Record<string, string>, caseSensitive = false): Reader {
  const fold = (value: string) => (caseSensitive ? value : value.toLowerCase());
  const find = (filePath: string) =>
    Object.keys(files).find((candidate) => fold(candidate) === fold(filePath));
  return {
    readText: (filePath) => {
      const found = find(filePath);
      return found === undefined ? undefined : files[found];
    },
    listDir: (directory) => {
      const prefix = `${directory}${path.sep}`;
      const entries = new Set<string>();
      for (const candidate of Object.keys(files)) {
        if (fold(candidate).startsWith(fold(prefix))) {
          entries.add(candidate.slice(prefix.length).split(path.sep)[0]);
        }
      }
      return entries.size === 0 ? undefined : [...entries];
    },
  };
}

function at(...parts: string[]): string {
  return path.join(MODEL_ROOT, ...parts);
}

test("a port's interface resolves to interfaces/<name>.yaml at its identity line", () => {
  const refs = shipped();
  const reader = fakeReader({
    [at("interfaces", "ISig.yaml")]: doc(["# header", "name: ISig", "kind: sender_receiver"]),
  });
  assert.deepEqual(
    resolveDefinition(refs, edge(refs, "field:swc.port.interface"), "ISig", MODEL_ROOT, reader),
    { file: at("interfaces", "ISig.yaml"), line: 1 },
  );
});

test("a task's partition resolves into hardware/", () => {
  const refs = shipped();
  const reader = fakeReader({ [at("hardware", "P1.yaml")]: doc(["name: P1", "kind: partition"]) });
  assert.deepEqual(
    resolveDefinition(refs, edge(refs, "field:tasks.task.partition"), "P1", MODEL_ROOT, reader),
    { file: at("hardware", "P1.yaml"), line: 0 },
  );
});

test("a composition member's type resolves into the swc group that holds it", () => {
  const refs = shipped();
  const reader = fakeReader({
    [at("swc", "body", "Door.yaml")]: doc(["name: Door"]),
    [at("swc", "chassis", "Speed.yaml")]: doc(["name: Speed"]),
  });
  assert.deepEqual(
    resolveDefinition(
      refs,
      edge(refs, "field:compositions.composition_member.type"),
      "Speed",
      MODEL_ROOT,
      reader,
    ),
    { file: at("swc", "chassis", "Speed.yaml"), line: 0 },
  );
});

test("a plain candidate whose identity differs: the listing, and the ~N that holds the value", () => {
  const refs = shipped();
  const interfaceEdge = edge(refs, "field:swc.port.interface");
  // The case-insensitive file system answers isig.yaml for ISig.yaml.
  const insensitive = fakeReader({
    [at("interfaces", "isig.yaml")]: doc(["name: isig"]),
    [at("interfaces", "ISig~1.yaml")]: doc(["name: ISig"]),
  });
  assert.deepEqual(resolveDefinition(refs, interfaceEdge, "ISig", MODEL_ROOT, insensitive), {
    file: at("interfaces", "ISig~1.yaml"),
    line: 0,
  });
  // The ~N candidates are tried by N.
  const ordered = fakeReader({
    [at("interfaces", "ISig.yaml")]: doc(["name: Other"]),
    [at("interfaces", "ISig~10.yaml")]: doc(["name: ISig"]),
    [at("interfaces", "ISig~2.yaml")]: doc(["name: ISig"]),
    [at("interfaces", "ISig~1.yaml")]: doc(["name: Third"]),
  });
  assert.deepEqual(resolveDefinition(refs, interfaceEdge, "ISig", MODEL_ROOT, ordered), {
    file: at("interfaces", "ISig~2.yaml"),
    line: 0,
  });
});

test("one request's reader answers every name of a listing as a fresh reader does, listing it once", () => {
  const refs = shipped();
  const interfaceEdge = edge(refs, "field:swc.port.interface");
  const files = {
    [at("interfaces", "ISig.yaml")]: doc(["name: Other"]),
    [at("interfaces", "ISig~10.yaml")]: doc(["name: ISig"]),
    [at("interfaces", "ISig~2.yaml")]: doc(["name: ISig"]),
    [at("interfaces", "ISig~1.yaml")]: doc(["name: Third"]),
    [at("interfaces", "Other.yaml")]: doc(["name: Other"]),
    [at("interfaces", "isig~5.yaml")]: doc(["name: ISig~5"]),
  };
  const values = ["ISig", "Other", "ISig~5", "isig", "Third", "Missing"];
  const expected = values.map((value) =>
    resolveDefinition(refs, interfaceEdge, value, MODEL_ROOT, fakeReader(files)),
  );
  const base = fakeReader(files);
  let listings = 0;
  const counted: Reader = {
    readText: (file) => base.readText(file),
    listDir: (directory) => {
      listings += 1;
      return base.listDir(directory);
    },
  };
  const reader = requestReader(counted);
  assert.deepEqual(
    values.map((value) => resolveDefinition(refs, interfaceEdge, value, MODEL_ROOT, reader)),
    expected,
  );
  assert.equal(listings, 1);
  assert.deepEqual(expected.slice(0, 3), [
    { file: at("interfaces", "ISig~2.yaml"), line: 0 },
    { file: at("interfaces", "Other.yaml"), line: 0 },
    { file: at("interfaces", "isig~5.yaml"), line: 0 },
  ]);
});

test("a case-sensitive file system: the differently cased occupant, and the ~N names", () => {
  const refs = shipped();
  const interfaceEdge = edge(refs, "field:swc.port.interface");
  // `isig.yaml` declaring `ISig` is the element's own file (occupied by self).
  const self = fakeReader({ [at("interfaces", "isig.yaml")]: doc(["name: ISig"]) }, true);
  assert.deepEqual(resolveDefinition(refs, interfaceEdge, "ISig", MODEL_ROOT, self), {
    file: at("interfaces", "isig.yaml"),
    line: 0,
  });
  const suffixed = fakeReader(
    {
      [at("interfaces", "isig.yaml")]: doc(["name: IOther"]),
      [at("interfaces", "ISig~1.yaml")]: doc(["name: ISig"]),
    },
    true,
  );
  assert.deepEqual(resolveDefinition(refs, interfaceEdge, "ISig", MODEL_ROOT, suffixed), {
    file: at("interfaces", "ISig~1.yaml"),
    line: 0,
  });
  const casedSuffix = fakeReader(
    {
      [at("interfaces", "isig.yaml")]: doc(["name: IOther"]),
      [at("interfaces", "isig~1.yaml")]: doc(["name: ISig"]),
    },
    true,
  );
  assert.deepEqual(resolveDefinition(refs, interfaceEdge, "ISig", MODEL_ROOT, casedSuffix), {
    file: at("interfaces", "isig~1.yaml"),
    line: 0,
  });
});

test("only a single column-0 identity line confirms a candidate", () => {
  const refs = shipped();
  const interfaceEdge = edge(refs, "field:swc.port.interface");
  // A member's indented name equal to the value is not the identity.
  const member = fakeReader({
    [at("interfaces", "ISig.yaml")]: doc(["name: Other", "data_elements:", "  - name: ISig"]),
  });
  assert.equal(resolveDefinition(refs, interfaceEdge, "ISig", MODEL_ROOT, member), undefined);
  // Two identity lines, in either order: refused, and not passed over for a
  // candidate that would confirm.
  for (const lines of [
    ["name: ISig", "name: IOther"],
    ["name: IOther", "name: ISig"],
  ]) {
    const twice = fakeReader({
      [at("interfaces", "ISig.yaml")]: doc(lines),
      [at("interfaces", "ISig~1.yaml")]: doc(["name: ISig"]),
    });
    assert.equal(
      resolveDefinition(refs, interfaceEdge, "ISig", MODEL_ROOT, twice),
      undefined,
      lines.join(" / "),
    );
  }
  // A continued identity holds another value to the loader.
  const continued = fakeReader({ [at("interfaces", "ISig.yaml")]: doc(["name: ISig", "  Extra"]) });
  assert.equal(resolveDefinition(refs, interfaceEdge, "ISig", MODEL_ROOT, continued), undefined);
});

test("an absent target, and a value that is no single path segment, resolve nothing", () => {
  const refs = shipped();
  const interfaceEdge = edge(refs, "field:swc.port.interface");
  const reader = fakeReader({ [at("interfaces", "ISig.yaml")]: doc(["name: ISig"]) });
  assert.equal(resolveDefinition(refs, interfaceEdge, "INone", MODEL_ROOT, reader), undefined);
  for (const value of ["a/b", "a\\b", "..", ".", "", "   ", "A.", "a\u0000b", "a\nb"]) {
    assert.equal(
      resolveDefinition(refs, interfaceEdge, value, MODEL_ROOT, reader),
      undefined,
      value,
    );
  }
});

test("the model root is the manifest's, read through readManifestPaths", () => {
  const refs = shipped();
  const project = fs.mkdtempSync(path.join(os.tmpdir(), "swcraft-nav-"));
  try {
    fs.writeFileSync(path.join(project, "swcraft.yaml"), "model_root: arch\n", {
      encoding: "utf8",
    });
    fs.mkdirSync(path.join(project, "arch", "interfaces"), { recursive: true });
    fs.mkdirSync(path.join(project, "model", "interfaces"), { recursive: true });
    fs.writeFileSync(path.join(project, "arch", "interfaces", "ISig.yaml"), "name: ISig\n", {
      encoding: "utf8",
    });
    fs.writeFileSync(path.join(project, "model", "interfaces", "ISig.yaml"), "name: ISig\n", {
      encoding: "utf8",
    });
    const modelRoot = readManifestPaths(path.join(project, "swcraft.yaml")).resolved.model_root;
    const reader: Reader = {
      readText: (filePath) => fs.readFileSync(filePath, "utf8"),
      listDir: (directory) => fs.readdirSync(directory),
    };
    const found = resolveDefinition(
      refs,
      edge(refs, "field:swc.port.interface"),
      "ISig",
      modelRoot,
      reader,
    );
    assert.ok(found);
    assert.equal(
      path
        .relative(fs.realpathSync(project), fs.realpathSync(found.file))
        .split(path.sep)
        .join("/"),
      "arch/interfaces/ISig.yaml",
    );
  } finally {
    fs.rmSync(project, { recursive: true, force: true });
  }
});

// --- the hover card ----------------------------------------------------------

function layer(refs: References, name: string): LayerFacts {
  const found = refs.layers.get(name);
  assert.ok(found, `no layer ${name}`);
  return found;
}

test("the card: a discriminated layer's kind from its kind: line, the identity, no description", () => {
  const interfaces = layer(shipped(), "interfaces");
  assert.deepEqual(hoverCard(doc(["name: ISig", "kind: sender_receiver"]), interfaces), {
    kind: "sender_receiver",
    identity: "ISig",
    description: undefined,
    excerpt: "name: ISig\nkind: sender_receiver",
  });
});

test("the card: a single-kind layer's one kind, and a single-line description", () => {
  const tasks = layer(shipped(), "tasks");
  const card = hoverCard(doc(["name: T10ms", "description: Runs every 10 ms # cyclic"]), tasks);
  assert.equal(card?.kind, "task");
  assert.equal(card?.identity, "T10ms");
  assert.equal(card?.description, "Runs every 10 ms");
});

test("the card: a description the writer wrapped, a block, a quoted one", () => {
  const tasks = layer(shipped(), "tasks");
  const cases: Array<[string, string, string | undefined]> = [
    [
      "wrapped",
      doc(["name: T", "description: The first half", "  and the second half", "period: 10"]),
      "The first half and the second half",
    ],
    [
      "a | block",
      doc(["name: T", "description: |-", "  Line one", "", "  Line two", "period: 10"]),
      "Line one Line two",
    ],
    ["a > block", doc(["name: T", "description: >", "  Folded text"]), "Folded text"],
    ["single-quoted", doc(["name: T", "description: 'a # not a comment'"]), "a # not a comment"],
    [
      "double-quoted over two lines",
      doc(["name: T", 'description: "opens here', '  and closes here"']),
      "opens here and closes here",
    ],
    [
      "a member's description is not the target's",
      doc(["name: T", "runnables:", "  - name: R", "    description: a member's"]),
      undefined,
    ],
    ["two description lines", doc(["name: T", "description: one", "description: two"]), undefined],
    ["absent", doc(["name: T"]), undefined],
  ];
  for (const [label, text, expected] of cases) {
    assert.equal(hoverCard(text, tasks)?.description, expected, label);
  }
});

test("the card's excerpt is the file's first twelve lines", () => {
  const tasks = layer(shipped(), "tasks");
  const lines = ["name: T", ...Array.from({ length: 20 }, (_, index) => `# line ${index}`)];
  const card = hoverCard(doc(lines), tasks);
  assert.equal(HOVER_EXCERPT_LINES, 12);
  assert.deepEqual(card?.excerpt.split("\n"), lines.slice(0, 12));
});

test("no card: no identity, two kind: lines, a continued identity, a continued kind:", () => {
  const interfaces = layer(shipped(), "interfaces");
  const refused: Array<[string, string]> = [
    ["not found (no identity line)", doc(["kind: sender_receiver"])],
    ["two kind: lines, equal", doc(["name: I", "kind: sender_receiver", "kind: sender_receiver"])],
    [
      "two kind: lines, different",
      doc(["name: I", "kind: sender_receiver", "kind: client_server"]),
    ],
    ["no kind: line", doc(["name: I"])],
    ["an undeclared kind", doc(["name: I", "kind: nonsense"])],
    ["a continued identity", doc(["name: ISig", "  Extra", "kind: sender_receiver"])],
    ["a continued kind:", doc(["name: ISig", "kind: sender_receiver", "  Extra"])],
    ["two identity lines", doc(["name: ISig", "name: IOther", "kind: sender_receiver"])],
  ];
  for (const [label, text] of refused) {
    assert.equal(hoverCard(text, interfaces), undefined, label);
  }
});

test("the card as Markdown: model text escaped, the excerpt fenced past its own backticks", () => {
  const markdown = hoverMarkdown({
    kind: "sender_receiver",
    identity: "I_*Sig*",
    description: "see [here](x)",
    excerpt: "name: I_*Sig*\n# ```",
  });
  assert.equal(
    markdown,
    [
      "**sender\\_receiver** I\\_\\*Sig\\*",
      "see \\[here\\]\\(x\\)",
      "````yaml\nname: I_*Sig*\n# ```\n````",
    ].join("\n\n"),
  );
  const bare = hoverMarkdown({ kind: "task", identity: "T", description: undefined, excerpt: "x" });
  assert.equal(bare, "**task** T\n\n```yaml\nx\n```");
});

// --- Find All References -----------------------------------------------------

/** The `impact` document recorded verbatim from the live CLI on `examples/basic`. */
function recordedImpact(): string {
  return fs.readFileSync(
    path.join(PROJECT_ROOT, "src", "fixtures", "navigation", "impact-IExample.json"),
    "utf8",
  );
}

/** `examples/basic`'s two dependents of `IExample`, one of them behind a `~N` name. */
function dependentsReader(): Reader {
  return fakeReader(
    {
      [at("swc", "example", "ExampleConsumerSWC.yaml")]: doc([
        "name: ExampleConsumerSWC",
        "ports:",
        "  - name: rIn",
        "    interface: IExample",
      ]),
      // The plain name is held by another element in another case, so the
      // element lives at `~1` (`model_state.free_storage_path`).
      [at("swc", "example", "exampleswc.yaml")]: doc(["name: exampleswc"]),
      [at("swc", "example", "ExampleSWC~1.yaml")]: doc([
        "# header",
        "name: ExampleSWC",
        "ports:",
        "  - name: pOut",
        "    interface: IExample",
      ]),
    },
    true,
  );
}

function impactDoc(matched: object, schemaVersion: unknown = 3): string {
  return JSON.stringify({ schemaVersion, name: "X", direction: "dependents", depth: 1, matched });
}

test("the recorded impact document parses: schemaVersion 3, matched by layer token", () => {
  const parsed = parseImpactDocument(recordedImpact());
  assert.equal(IMPACT_SCHEMA_VERSION, 3);
  assert.deepEqual([...parsed.matched.keys()], ["interface"]);
  assert.deepEqual(parsed.matched.get("interface"), [
    { layer: "swc", name: "ExampleConsumerSWC", detail: "via port rIn" },
    { layer: "swc", name: "ExampleSWC", detail: "via port pOut" },
  ]);
});

test("the recorded document's dependents resolve to their files, a ~N target among them", () => {
  const refs = shipped();
  const locations = referenceLocations(
    refs,
    parseImpactDocument(recordedImpact()),
    "interfaces",
    MODEL_ROOT,
    dependentsReader(),
  );
  assert.deepEqual(locations, {
    locations: [
      { file: at("swc", "example", "ExampleConsumerSWC.yaml"), line: 0 },
      { file: at("swc", "example", "ExampleSWC~1.yaml"), line: 1 },
    ],
    unlocated: [],
  });
});

test("only the cursor layer's match is read; an unknown token or an unconfirmed file is unlocated", () => {
  const refs = shipped();
  const parsed = parseImpactDocument(
    impactDoc({
      // The same name matched an interface and an SWC; the cursor is on the SWC.
      interface: [{ layer: "swc", name: "ExampleConsumerSWC", detail: "", details: [] }],
      swc: [
        { layer: "swc", name: "ExampleSWC", detail: "", details: [] },
        { layer: "no_such_token", name: "ExampleSWC", detail: "", details: [] },
        { layer: "swc", name: "Missing", detail: "", details: [] },
      ],
    }),
  );
  assert.deepEqual(referenceLocations(refs, parsed, "swc", MODEL_ROOT, dependentsReader()), {
    locations: [{ file: at("swc", "example", "ExampleSWC~1.yaml"), line: 1 }],
    unlocated: [
      { layer: "no_such_token", name: "ExampleSWC" },
      { layer: "swc", name: "Missing" },
    ],
  });
});

test("an unknown name — matched: {} — has no locations", () => {
  const refs = shipped();
  const parsed = parseImpactDocument(impactDoc({}));
  assert.equal(parsed.matched.size, 0);
  assert.deepEqual(referenceLocations(refs, parsed, "interfaces", MODEL_ROOT, dependentsReader()), {
    locations: [],
    unlocated: [],
  });
});

test("a foreign schemaVersion, or a malformed document, is a refusal naming what is wrong", () => {
  assert.throws(
    () => parseImpactDocument(impactDoc({}, 2)),
    (error: unknown) => {
      assert.ok(error instanceof ImpactDocumentError);
      assert.match(error.message, /schemaVersion 2 is not the 3/);
      return true;
    },
  );
  assert.throws(() => parseImpactDocument("not json"), ImpactDocumentError);
  assert.throws(() => parseImpactDocument(impactDoc([])), /matched must be an object/);
  assert.throws(
    () => parseImpactDocument(impactDoc({ swc: { layer: "swc", name: "X" } })),
    /matched\.swc must be an array/,
  );
  assert.throws(
    () => parseImpactDocument(impactDoc({ swc: [{ layer: "swc" }] })),
    /matched\.swc\[0\] must carry a string layer and name/,
  );
});

function capture(overrides: Partial<ChildCapture>): ChildCapture {
  return { exitCode: 0, spawnFailed: false, stdout: "", stderr: "", ...overrides };
}

const OPTIONS = { cancelled: false, executable: "/opt/swcraft/bin/swcraft" };

test("exit 0: the locations, the declaration first when asked; no message on a quiet stderr", () => {
  const refs = shipped();
  const declaration = { file: at("interfaces", "IExample.yaml"), line: 0 };
  const answer = referencesAnswer(
    refs,
    capture({ stdout: recordedImpact() }),
    "interfaces",
    MODEL_ROOT,
    dependentsReader(),
    { ...OPTIONS, declaration },
  );
  assert.deepEqual(answer, {
    locations: [
      declaration,
      { file: at("swc", "example", "ExampleConsumerSWC.yaml"), line: 0 },
      { file: at("swc", "example", "ExampleSWC~1.yaml"), line: 1 },
    ],
  });
});

test("exit 0 with a stderr (a pin mismatch's Warning:): the locations and the text as information", () => {
  const answer = referencesAnswer(
    shipped(),
    capture({ stdout: recordedImpact(), stderr: "Warning: this project pins 0.14.0\n" }),
    "interfaces",
    MODEL_ROOT,
    dependentsReader(),
    OPTIONS,
  );
  assert.equal(answer.locations.length, 2);
  assert.deepEqual(answer.message, {
    severity: "information",
    text: "SWCraft: Find All References: Warning: this project pins 0.14.0",
  });
});

test("exit 1 (the project could not be read): no locations, the stderr as a warning", () => {
  const answer = referencesAnswer(
    shipped(),
    capture({
      exitCode: 1,
      stdout: recordedImpact(),
      stderr: "Error: swc/X.yaml does not parse\n",
    }),
    "interfaces",
    MODEL_ROOT,
    dependentsReader(),
    OPTIONS,
  );
  assert.deepEqual(answer.locations, []);
  assert.equal(answer.message?.severity, "warning");
  assert.match(answer.message?.text ?? "", /exit 1.*swc\/X\.yaml does not parse/);
});

test("the timeout (exitCode null, not spawnFailed): no locations, the did-not-answer line", () => {
  const stopped =
    "SWCraft: /opt/swcraft/bin/swcraft did not answer within 60000 ms and was stopped.";
  const answer = referencesAnswer(
    shipped(),
    capture({ exitCode: null, stderr: `partial\n${stopped}\n` }),
    "interfaces",
    MODEL_ROOT,
    dependentsReader(),
    OPTIONS,
  );
  assert.deepEqual(answer.locations, []);
  assert.deepEqual(answer.message, {
    severity: "warning",
    text: `SWCraft: Find All References: ${stopped}`,
  });
});

test("a cancelled request: no locations and no message, whatever the run captured", () => {
  for (const captured of [capture({ exitCode: null }), capture({ stdout: recordedImpact() })]) {
    assert.deepEqual(
      referencesAnswer(shipped(), captured, "interfaces", MODEL_ROOT, dependentsReader(), {
        ...OPTIONS,
        cancelled: true,
      }),
      { locations: [] },
    );
  }
});

test("a CLI that could not be started: no locations and the install hint", () => {
  const answer = referencesAnswer(
    shipped(),
    capture({ exitCode: null, spawnFailed: true, stderr: "spawn ENOENT\n" }),
    "interfaces",
    MODEL_ROOT,
    dependentsReader(),
    OPTIONS,
  );
  assert.deepEqual(answer.locations, []);
  assert.equal(answer.message?.severity, "warning");
  assert.match(answer.message?.text ?? "", /could not be started \(\/opt\/swcraft\/bin\/swcraft\)/);
  assert.match(answer.message?.text ?? "", /SWCraft: Install Toolchain/);
});

test("a foreign schemaVersion on exit 0: no locations, the refusal naming it", () => {
  const answer = referencesAnswer(
    shipped(),
    capture({ stdout: impactDoc({}, 4) }),
    "interfaces",
    MODEL_ROOT,
    dependentsReader(),
    OPTIONS,
  );
  assert.deepEqual(answer.locations, []);
  assert.match(answer.message?.text ?? "", /schemaVersion 4/);
});

test("the name asked about: the cursor on the file's only column-0 identity line", () => {
  const refs = shipped();
  const text = doc(["name: IExample", "kind: sender_receiver", "elements:", "  - name: value"]);
  assert.equal(identityAt(refs, text, "interfaces", 0), "IExample");
  // Not the identity line: a member's indented name, another key.
  assert.equal(identityAt(refs, text, "interfaces", 3), undefined);
  assert.equal(identityAt(refs, text, "interfaces", 1), undefined);
  // Two identity lines (the loader keeps the last): neither asks the CLI.
  const twice = doc(["name: IExample", "name: IOther", "kind: sender_receiver"]);
  assert.equal(identityAt(refs, twice, "interfaces", 0), undefined);
  assert.equal(identityAt(refs, twice, "interfaces", 1), undefined);
  // A continued identity loads as `IExample Extra`: no name, no CLI call.
  const continued = doc(["name: IExample", "  Extra", "kind: sender_receiver"]);
  assert.equal(identityAt(refs, continued, "interfaces", 0), undefined);
  // A layer the artifact does not declare.
  assert.equal(identityAt(refs, text, "no_such_layer", 0), undefined);
});

test("the reference provider is bounded, cancellable, and runs no CLI without an identity", () => {
  // Pinned as source text, as `toolchainInstall.test.ts` pins the version
  // probe's bound: `commands.ts` imports `vscode` and cannot be loaded here.
  const source = withoutCommentLines(readSource("src/commands.ts"));
  const body = functionBody(source, "async function provideReferencesAt(");
  assert.match(body, /onCancellationRequested\(\(\) => controller\.abort\(\)\)/);
  assert.match(
    body,
    /spawnCliCapture\(\s*executable,\s*argv,\s*workspaceRoot,\s*LIST_PROBE_TIMEOUT_MS,\s*controller\.signal,?\s*\)/,
  );
  assert.match(body, /cancelled: token\.isCancellationRequested/);
  assert.match(body, /buildImpactJsonArgv\(resolution\.manifestRelPath, name\)/);
  assert.match(body, /cancellation\.dispose\(\)/);
  // A member line keeps the link's columns; any other location is its line's start.
  assert.match(
    body,
    /found\.start === undefined \|\| found\.end === undefined\s*\?\s*new vscode\.Position\(found\.line, 0\)\s*:\s*new vscode\.Range\(found\.line, found\.start, found\.line, found\.end\)/,
  );
  // An unsaved identity edit is answered before the CLI is asked.
  const unsaved = body.indexOf("unsavedIdentityAnswer(");
  assert.ok(unsaved !== -1 && unsaved < body.indexOf("spawnCliCapture("));
  const identity = body.indexOf("identityAt(");
  const spawnAt = body.indexOf("spawnCliCapture(");
  assert.ok(identity !== -1 && spawnAt !== -1 && identity < spawnAt);
  assert.match(body.slice(identity, spawnAt), /if \(name === undefined\) \{\s*return undefined;/);
  assert.match(source, /registerReferenceProvider\(selector,/);
});

test("navigation is registered last in activate(), inside its own guard", () => {
  // A missing or damaged references.json throws out of registerNavigation(); the
  // guard keeps that from taking the commands, the tree and the context key with it.
  const body = functionBody(
    withoutCommentLines(readSource("src/extension.ts")),
    "export function activate(",
  );
  const layerContext = body.indexOf("registerLayerFileContext(context)");
  const navigation = body.indexOf("registerNavigation(context)");
  assert.ok(layerContext !== -1 && navigation > layerContext, "after every other registration");
  assert.match(
    body.slice(layerContext),
    /try \{\s*registerNavigation\(context\);\s*\} catch \(error\) \{[\s\S]*showWarningMessage/,
  );
});

// --- the resolver's places and spellings ---------------------------------------

test("a component stored flat in swc/ itself, and one nested deeper than one group, resolve", () => {
  const refs = shipped();
  const memberType = edge(refs, "field:compositions.composition_member.type");
  const reader = fakeReader({
    [at("swc", "Flat.yaml")]: doc(["name: Flat"]),
    [at("swc", "body", "front", "Deep.yaml")]: doc(["name: Deep"]),
    [at("swc", "body", "Door.yaml")]: doc(["name: Door"]),
  });
  assert.deepEqual(resolveDefinition(refs, memberType, "Flat", MODEL_ROOT, reader), {
    file: at("swc", "Flat.yaml"),
    line: 0,
  });
  assert.deepEqual(resolveDefinition(refs, memberType, "Deep", MODEL_ROOT, reader), {
    file: at("swc", "body", "front", "Deep.yaml"),
    line: 0,
  });
});

test("a component deeper than MAX_GROUP_DEPTH is not reached, and is reported unlocated", () => {
  const refs = shipped();
  const groups = Array.from({ length: MAX_GROUP_DEPTH + 1 }, (_, index) => `g${index}`);
  const atLimit = Array.from({ length: MAX_GROUP_DEPTH }, (_, index) => `g${index}`);
  const reader = fakeReader({
    [at("swc", ...atLimit, "Reached.yaml")]: doc(["name: Reached"]),
    [at("swc", ...groups, "TooDeep.yaml")]: doc(["name: TooDeep"]),
  });
  const swc = layer(refs, "swc");
  assert.deepEqual(resolveElement(swc, "Reached", MODEL_ROOT, reader), {
    file: at("swc", ...atLimit, "Reached.yaml"),
    line: 0,
  });
  assert.equal(resolveElement(swc, "TooDeep", MODEL_ROOT, reader), undefined);
  const answer = referencesAnswer(
    refs,
    capture({
      stdout: impactDoc({
        interface: [
          { layer: "swc", name: "Reached" },
          { layer: "swc", name: "TooDeep" },
        ],
      }),
    }),
    "interfaces",
    MODEL_ROOT,
    reader,
    OPTIONS,
  );
  assert.deepEqual(answer.locations, [{ file: at("swc", ...atLimit, "Reached.yaml"), line: 0 }]);
  assert.deepEqual(answer.message, {
    severity: "information",
    text:
      "SWCraft: Find All References: 1 dependent the CLI named could not be located in the " +
      "model files and is not listed: TooDeep (swc).",
  });
});

test("a nesting layer: groups in code-unit order, ~N inside a group, files are no groups", () => {
  const refs = shipped();
  const swc = layer(refs, "swc");
  // The same name in two groups (an invalid model): the first group by code-unit order.
  const twice = fakeReader({
    [at("swc", "b", "Twin.yaml")]: doc(["name: Twin"]),
    [at("swc", "a", "Twin.yaml")]: doc(["name: Twin"]),
  });
  assert.deepEqual(resolveElement(swc, "Twin", MODEL_ROOT, twice), {
    file: at("swc", "a", "Twin.yaml"),
    line: 0,
  });
  // A ~N file inside a group, behind a plain occupant in another case.
  const suffixed = fakeReader(
    {
      [at("swc", "g", "door.yaml")]: doc(["name: door"]),
      [at("swc", "g", "Door~1.yaml")]: doc(["name: Door"]),
      [at("swc", "Readme.yaml")]: doc(["name: Readme"]),
    },
    true,
  );
  assert.deepEqual(resolveElement(swc, "Door", MODEL_ROOT, suffixed), {
    file: at("swc", "g", "Door~1.yaml"),
    line: 0,
  });
});

test("a refused candidate in the first group ends the resolution: no later group is tried", () => {
  const swc = layer(shipped(), "swc");
  const reader = fakeReader({
    [at("swc", "a", "Door.yaml")]: doc(["name: Door", "name: Other"]),
    [at("swc", "b", "Door.yaml")]: doc(["name: Door"]),
  });
  assert.equal(resolveElement(swc, "Door", MODEL_ROOT, reader), undefined);
});

// --- the walk is lazy, the order unchanged ---------------------------------------

test("the group walk is lazy: a directory is listed only when no earlier place answered", () => {
  const swc = layer(shipped(), "swc");
  const files = {
    [at("swc", "Flat.yaml")]: doc(["name: Flat"]),
    [at("swc", "a", "Deep.yaml")]: doc(["name: Deep"]),
    [at("swc", "b", "Other.yaml")]: doc(["name: Other"]),
  };
  const counting = (): { reader: Reader; listed: string[] } => {
    const inner = fakeReader(files);
    const listed: string[] = [];
    return {
      listed,
      reader: {
        readText: inner.readText,
        listDir: (directory) => {
          listed.push(directory);
          return inner.listDir(directory);
        },
      },
    };
  };
  // A hit in the layer directory lists no group.
  const flat = counting();
  assert.deepEqual(resolveElement(swc, "Flat", MODEL_ROOT, flat.reader), {
    file: at("swc", "Flat.yaml"),
    line: 0,
  });
  assert.deepEqual(flat.listed, [at("swc")]);
  // A hit in `swc/a/` lists nothing after it (`swc/b/` is never asked).
  const deep = counting();
  assert.deepEqual(resolveElement(swc, "Deep", MODEL_ROOT, deep.reader), {
    file: at("swc", "a", "Deep.yaml"),
    line: 0,
  });
  assert.deepEqual(deep.listed, [at("swc"), at("swc", "Flat.yaml"), at("swc", "a")]);
  // A miss walks every place, in the documented order: the layer directory,
  // then each group before its siblings' (code-unit order), depth first.
  const miss = counting();
  assert.equal(resolveElement(swc, "Absent", MODEL_ROOT, miss.reader), undefined);
  assert.deepEqual(miss.listed, [
    at("swc"),
    at("swc", "Flat.yaml"),
    at("swc", "a"),
    at("swc", "a", "Deep.yaml"),
    at("swc", "b"),
    at("swc", "b", "Other.yaml"),
  ]);
});

// --- a model file's bytes are decoded strictly ---------------------------------

test("decodeModelText refuses bytes that are not UTF-8 and keeps a byte-order mark", () => {
  assert.equal(decodeModelText(Buffer.from("name: ISig\n", "utf8")), "name: ISig\n");
  assert.equal(decodeModelText(Buffer.from([0x6e, 0x61, 0x6d, 0x65, 0x3a, 0x20, 0xff])), undefined);
  assert.equal(decodeModelText(Buffer.from("﻿name: ISig\n", "utf8")), "﻿name: ISig\n");
});

test("a target file that is not UTF-8 is unreadable: no definition, as for a missing file", () => {
  const refs = shipped();
  const project = fs.mkdtempSync(path.join(os.tmpdir(), "swcraft-nav-"));
  try {
    const interfaces = path.join(project, "interfaces");
    fs.mkdirSync(interfaces, { recursive: true });
    // `name: ISig` followed by a lone 0xFF byte: a loose decode would read the
    // identity line and answer; the loader refuses the file.
    fs.writeFileSync(
      path.join(interfaces, "ISig.yaml"),
      Buffer.concat([Buffer.from("name: ISig\n# ", "utf8"), Buffer.from([0xff, 0x0a])]),
    );
    const reader: Reader = {
      readText: (filePath) => decodeModelText(fs.readFileSync(filePath)),
      listDir: (directory) => fs.readdirSync(directory),
    };
    assert.equal(
      resolveDefinition(refs, edge(refs, "field:swc.port.interface"), "ISig", project, reader),
      undefined,
    );
  } finally {
    fs.rmSync(project, { recursive: true, force: true });
  }
});

test("a case-insensitive file system: the location carries the name on disk", () => {
  const refs = shipped();
  const reader = fakeReader({ [at("interfaces", "isig.yaml")]: doc(["name: ISig"]) });
  assert.deepEqual(
    resolveDefinition(refs, edge(refs, "field:swc.port.interface"), "ISig", MODEL_ROOT, reader),
    { file: at("interfaces", "isig.yaml"), line: 0 },
  );
});

test("several plain names in other cases: the exact spelling first, then code-unit order", () => {
  const interfaces = layer(shipped(), "interfaces");
  const files = {
    [at("interfaces", "isig.yaml")]: doc(["name: ISig"]),
    [at("interfaces", "ISIG.yaml")]: doc(["name: ISig"]),
  };
  // Neither is the exact spelling: `ISIG.yaml` sorts before `isig.yaml`.
  assert.deepEqual(resolveElement(interfaces, "ISig", MODEL_ROOT, fakeReader(files, true)), {
    file: at("interfaces", "ISIG.yaml"),
    line: 0,
  });
  // The exact spelling wins over both.
  const exact = { ...files, [at("interfaces", "ISig.yaml")]: doc(["name: ISig"]) };
  assert.deepEqual(resolveElement(interfaces, "ISig", MODEL_ROOT, fakeReader(exact, true)), {
    file: at("interfaces", "ISig.yaml"),
    line: 0,
  });
});

// --- Find All References' remaining answers ----------------------------------

test("a stopped run or a failed exit with an empty stderr still says what happened", () => {
  const stopped = referencesAnswer(
    shipped(),
    capture({ exitCode: null }),
    "interfaces",
    MODEL_ROOT,
    dependentsReader(),
    OPTIONS,
  );
  assert.deepEqual(stopped.message, {
    severity: "warning",
    text: "SWCraft: Find All References: the CLI was stopped before it answered.",
  });
  const failed = referencesAnswer(
    shipped(),
    capture({ exitCode: 1, stderr: "  \n" }),
    "interfaces",
    MODEL_ROOT,
    dependentsReader(),
    OPTIONS,
  );
  assert.deepEqual(failed.message, {
    severity: "warning",
    text: "SWCraft: Find All References: the CLI could not read the project (exit 1).",
  });
});

test("the declaration is listed once, even when the CLI names the element among its dependents", () => {
  const refs = shipped();
  const self = { file: at("swc", "example", "ExampleSWC~1.yaml"), line: 1 };
  const answer = referencesAnswer(
    refs,
    capture({ stdout: impactDoc({ swc: [{ layer: "swc", name: "ExampleSWC" }] }) }),
    "swc",
    MODEL_ROOT,
    dependentsReader(),
    { ...OPTIONS, declaration: self },
  );
  assert.deepEqual(answer, { locations: [self] });
});

test("unlocated dependents and a stderr share the one information message", () => {
  const answer = referencesAnswer(
    shipped(),
    capture({
      stdout: impactDoc({ interface: [{ layer: "swc", name: "Missing" }] }),
      stderr: "Warning: this project pins 0.14.0\n",
    }),
    "interfaces",
    MODEL_ROOT,
    dependentsReader(),
    OPTIONS,
  );
  assert.deepEqual(answer.locations, []);
  assert.equal(answer.message?.severity, "information");
  assert.match(answer.message?.text ?? "", /1 dependent .* not listed: Missing \(swc\)\. Warning:/);
});

test("an unsaved identity: asked only when the saved file declares the same name", () => {
  const refs = shipped();
  const saved = doc(["name: IExample", "kind: sender_receiver"]);
  assert.equal(unsavedIdentityAnswer(refs, saved, "interfaces", "IExample"), undefined);
  for (const [label, text] of [
    ["renamed, not saved", saved],
    ["unreadable", undefined],
    ["two identity lines on disk", doc(["name: IRenamed", "name: IRenamed"])],
  ] as Array<[string, string | undefined]>) {
    assert.deepEqual(
      unsavedIdentityAnswer(refs, text, "interfaces", "IRenamed"),
      {
        locations: [],
        message: {
          severity: "information",
          text:
            "SWCraft: Find All References: save the file first — the CLI reads the saved model, " +
            "and the saved file does not declare 'IRenamed'.",
        },
      },
      label,
    );
  }
});

test("an unsaved identity: the message's prefix is the caller's, Find All References' by default", () => {
  const refs = shipped();
  const saved = doc(["name: IExample", "kind: sender_receiver"]);
  assert.equal(
    unsavedIdentityAnswer(refs, saved, "interfaces", "IRenamed", "SWCraft: Impact")?.message?.text,
    "SWCraft: Impact: save the file first — the CLI reads the saved model, " +
      "and the saved file does not declare 'IRenamed'.",
  );
  assert.equal(
    unsavedIdentityAnswer(refs, saved, "interfaces", "IExample", "SWCraft: Impact"),
    undefined,
  );
});

// --- CRLF text (a Windows-saved file) ------------------------------------------

test("CRLF text reads as LF text: key paths, values, the column-0 scalar and the hover", () => {
  const refs = shipped();
  const crlf = (text: string) => text.split("\n").join("\r\n");
  for (const ports of [INDENTED_PORTS, INDENTLESS_PORTS]) {
    const line = lineOf(ports, "interface: ISig");
    assert.deepEqual(keyPathAt(crlf(ports), line), keyPathAt(ports, line));
    assert.ok(keyPathAt(crlf(ports), line));
    assert.equal(scalarValueAt(crlf(ports), line), "ISig");
  }
  assert.deepEqual(topLevelScalar(crlf(doc(["name: T", "kind: a"])), "kind"), {
    status: "one",
    line: 1,
    value: "a",
  });
  const target = doc(["name: T10ms", "description: Runs every", "  10 ms", "period: 10"]);
  assert.equal(hoverCard(crlf(target), layer(refs, "tasks"))?.description, "Runs every 10 ms");
});

// --- the three sections of references.json -----------------------------------

test("the shipped artifact's relations, collections and member references load", () => {
  const refs = shipped();
  const raw = JSON.parse(shippedText()) as {
    relations: unknown[];
    collections: unknown[];
    memberReferences: unknown[];
  };
  assert.equal(refs.relations.length, raw.relations.length);
  assert.equal(refs.collections.length, raw.collections.length);
  assert.equal(refs.memberReferences.length, raw.memberReferences.length);
  assert.ok(refs.relations.length > 0 && refs.collections.length > 0);
  assert.ok(refs.memberReferences.length > 0);
  const accessible = refs.relations.find((edge) => edge.field.endsWith("memory.accessible"));
  assert.equal(accessible?.targetKind, "core");
  assert.equal(accessible?.resolution, "list-values");
  const element = refs.memberReferences.find((edge) => edge.field.endsWith("data_write.element"));
  assert.equal(element?.via, "port");
  assert.deepEqual(
    element?.collections.map((target) => [target.kind, target.identityField]),
    [
      ["sender_receiver", "name"],
      ["external_interface", null],
    ],
  );
});

test("a malformed relation, collection or member-reference row is a refusal naming the key", () => {
  type Rows = Array<Record<string, unknown>>;
  const base = JSON.parse(shippedText()) as {
    relations: Rows;
    collections: Rows;
    memberReferences: Rows;
  };
  const cases: Array<[string, (document: typeof base) => unknown, RegExp]> = [
    [
      "no relations section",
      (d) => {
        delete (d as Partial<typeof base>).relations;
        return d;
      },
      /relations must be an array/,
    ],
    [
      "no memberReferences section",
      (d) => {
        delete (d as Partial<typeof base>).memberReferences;
        return d;
      },
      /memberReferences must be an array/,
    ],
    [
      "a resolution no reader knows",
      (d) => {
        d.relations[0].resolution = "values";
        return d;
      },
      /relations\[0\]\.resolution must be one of list-values, map-keys, versioned-list-values/,
    ],
    [
      "a target kind the target layer does not hold",
      (d) => {
        d.relations[0].targetKind = "nowhere";
        return d;
      },
      /relations\[0\]\.targetKind names 'nowhere', which is no top-level kind of hardware/,
    ],
    [
      "a relation into no layer",
      (d) => {
        d.relations[0].targetLayer = "nowhere";
        return d;
      },
      /relations\[0\]\.targetLayer names 'nowhere', which is no layer/,
    ],
    [
      "a collection's collectionKind that is neither list nor map",
      (d) => {
        d.collections[0].collectionKind = "set";
        return d;
      },
      /collections\[0\]\.collectionKind must be one of list, map/,
    ],
    [
      "member kinds that are not strings",
      (d) => {
        d.collections[0].memberKinds = [1];
        return d;
      },
      /collections\[0\]\.memberKinds must be an array of non-empty strings/,
    ],
    [
      "a collection with no path",
      (d) => {
        d.collections[0].paths = [];
        return d;
      },
      /collections\[0\]\.paths must be a non-empty array/,
    ],
    [
      "a member reference with no collection",
      (d) => {
        d.memberReferences[0].collections = [];
        return d;
      },
      /memberReferences\[0\]\.collections must be a non-empty array/,
    ],
    [
      "a member-reference collection on no layer",
      (d) => {
        (d.memberReferences[1].collections as Rows)[0].layer = "nowhere";
        return d;
      },
      /memberReferences\[1\]\.collections\[0\]\.layer names 'nowhere', which is no layer/,
    ],
    [
      "a via that is not a string or null",
      (d) => {
        d.memberReferences[1].via = 3;
        return d;
      },
      /memberReferences\[1\]\.via must be a non-empty string or null/,
    ],
  ];
  for (const [label, damage, message] of cases) {
    const copy = structuredClone(base);
    assert.throws(
      () => loadReferences(JSON.stringify(damage(copy))),
      (error: unknown) => error instanceof ReferencesArtifactError && message.test(error.message),
      label,
    );
  }
});

// --- the scalar item reader --------------------------------------------------

const INDENTED_MEMORY = doc([
  "name: RAM",
  "kind: memory",
  "accessible:",
  "  - Core0",
  "  - 'Core1'",
  '  - "Core#2" # a comment',
  "size: 1",
]);

const INDENTLESS_MEMORY = doc([
  "name: RAM",
  "kind: memory",
  "accessible:",
  "- Core0",
  "- 'Core1'",
  '- "Core#2" # a comment',
  "size: 1",
]);

test("a scalar sequence item: its owner's path plus '*', and its value, in both styles", () => {
  for (const text of [INDENTED_MEMORY, INDENTLESS_MEMORY]) {
    assert.deepEqual(itemScalarAt(text, lineOf(text, "Core0")), {
      path: ["accessible", "*"],
      value: "Core0",
    });
    assert.equal(itemScalarAt(text, lineOf(text, "Core1"))?.value, "Core1");
    assert.equal(itemScalarAt(text, lineOf(text, "Core#2"))?.value, "Core#2");
    // A key line, the owner itself and a line past the list are no item.
    assert.equal(itemScalarAt(text, lineOf(text, "accessible:")), undefined);
    assert.equal(itemScalarAt(text, lineOf(text, "size: 1")), undefined);
    assert.equal(itemScalarAt(text, 99), undefined);
  }
});

test("a scalar item the reader refuses answers nothing", () => {
  for (const dash of ["  - ", "- "]) {
    const item = (value: string, ...after: string[]) =>
      doc(["name: RAM", "accessible:", `${dash}${value}`, ...after]);
    const cases: Array<[string, string]> = [
      ["an escaped double quote", item('"Co\\"re"')],
      ["a doubled single quote", item("'Co''re'")],
      ["an unclosed quote", item('"Core')],
      ["an anchor", item("&a Core0")],
      ["an alias", item("*a")],
      ["a tag", item("!t Core0")],
      ["a flow list", item("[A, B]")],
      ["a nested sequence", item("- Core0")],
      ["an empty item", item("")],
      ["a continued plain item", item("Core0", `${" ".repeat(dash.length + 2)}more`)],
    ];
    for (const [label, text] of cases) {
      assert.equal(itemScalarAt(text, 2), undefined, `${label} (${JSON.stringify(dash)})`);
    }
  }
  // A flow list has no item line; the owner's line is a key with a value.
  const flow = doc(["id: R1", "links: [A, B]"]);
  assert.equal(itemScalarAt(flow, 1), undefined);
});

test("the owner key repeated in its mapping, before or after the list, answers nothing", () => {
  for (const dash of ["  - ", "- "]) {
    const after = doc(["name: RAM", "accessible:", `${dash}Core0`, "accessible:", `${dash}Core1`]);
    assert.equal(itemScalarAt(after, 2), undefined);
    assert.equal(itemScalarAt(after, 4), undefined);
    const nested = doc(["name: M", "ports:", "- name: p", "  measures:", "  - x", "ports: []"]);
    assert.equal(itemScalarAt(nested, 4), undefined, "a repeated ancestor");
  }
});

test("lineShapes and itemExtent: an item runs to the line before the next at its dash column", () => {
  for (const text of [INDENTED_PORTS, INDENTLESS_PORTS]) {
    const shapes = lineShapes(text);
    const first = lineOf(text, "- name: rIn");
    assert.deepEqual(itemExtent(shapes, first), {
      first,
      last: lineOf(text, "interface: ISig"),
    });
    const second = lineOf(text, "- interface: IOther");
    assert.deepEqual(itemExtent(shapes, second), { first: second, last: lineOf(text, "rOut") });
    assert.equal(itemExtent(shapes, 0), undefined, "no dash");
  }
  const trailing = doc(["ports:", "- name: a", "", "  # note", "- name: b"]);
  assert.deepEqual(itemExtent(lineShapes(trailing), 1), { first: 1, last: 1 });
  assert.equal(itemExtent(lineShapes(doc(["x:", "- - a"])), 1), undefined);
});

// --- relations -------------------------------------------------------------------

function relationTarget(
  refs: References,
  text: string,
  layerName: string,
  line: number,
  reader: Reader,
) {
  const hit = relationAt(refs, text, layerName, line);
  return hit === undefined
    ? undefined
    : resolveRelation(refs, hit.edge, hit.value, MODEL_ROOT, reader);
}

/**
 * The id column of `packages/tools/tests/test_trace.py`'s `SPLIT_TABLE`, repeated by hand:
 * the text before the first `@`, and nothing for a member holding `:` — element rows
 * included.
 */
const SPLIT_TABLE_IDS: ReadonlyArray<[string, string | undefined]> = [
  ["REQ-12@3", "REQ-12"],
  ["REQ-12", "REQ-12"],
  ["REQ-12@0", "REQ-12"],
  ["REQ-12@01", "REQ-12"],
  ["REQ-12@x", "REQ-12"],
  ["REQ-12@3@4", "REQ-12"],
  ["REQ-12@", "REQ-12"],
  ["REQ_EXAMPLE_001@12", "REQ_EXAMPLE_001"],
  ["hardware:Core0", undefined],
  ["swc:SpeedCtrlSWC", undefined],
  ["connectors:X", undefined],
];

function requirementReader() {
  return fakeReader({
    [at("requirements", "REQ-12.yaml")]: doc(["id: REQ-12", "version: 3"]),
    [at("requirements", "REQ_EXAMPLE_001.yaml")]: doc(["id: REQ_EXAMPLE_001", "version: 12"]),
  });
}

test("a component's satisfies item resolves to the requirement, version stripped, in both styles", () => {
  const refs = shipped();
  const reader = requirementReader();
  for (const dash of ["  - ", "- "]) {
    const text = doc([
      "name: Door",
      "satisfies:",
      `${dash}REQ-12@3`,
      `${dash}REQ-12`,
      `${dash}REQ-99@1`,
    ]);
    const hit = relationAt(refs, text, "swc", 2);
    assert.equal(hit?.edge.field, "field:swc.swc.satisfies");
    assert.equal(hit?.edge.resolution, "versioned-list-values");
    assert.equal(hit?.value, "REQ-12@3");
    const target = { file: at("requirements", "REQ-12.yaml"), line: 0 };
    assert.deepEqual(relationTarget(refs, text, "swc", 2, reader), target);
    assert.deepEqual(relationTarget(refs, text, "swc", 3, reader), target, "a bare id");
    assert.equal(relationTarget(refs, text, "swc", 4, reader), undefined, "no such requirement");
    // The same list in a layer that declares none is no relation.
    assert.equal(relationAt(refs, text, "connectors", 2), undefined);
  }
});

test("a port's and a runnable's satisfies item resolve at depth four", () => {
  const refs = shipped();
  const reader = requirementReader();
  const target = { file: at("requirements", "REQ-12.yaml"), line: 0 };
  for (const owner of ["ports", "runnables"]) {
    const text = doc([
      "name: Door",
      `${owner}:`,
      "  - name: pIn",
      "    satisfies:",
      "      - REQ-12@3",
    ]);
    const hit = relationAt(refs, text, "swc", 4);
    assert.equal(hit?.edge.field, `field:swc.${owner === "ports" ? "port" : "runnable"}.satisfies`);
    assert.deepEqual(relationTarget(refs, text, "swc", 4, reader), target, owner);
  }
});

test("the versioned strip agrees with test_trace.py's SPLIT_TABLE id column", () => {
  const refs = shipped();
  const reader = requirementReader();
  const satisfies = refs.relations.find((edge) => edge.field === "field:swc.swc.satisfies");
  const derived = refs.relations.find((edge) => edge.field.endsWith("requirement.derived_from"));
  assert.ok(satisfies && derived);
  for (const edge of [satisfies, derived]) {
    for (const [member, id] of SPLIT_TABLE_IDS) {
      const expected =
        id === undefined ? undefined : { file: at("requirements", `${id}.yaml`), line: 0 };
      assert.deepEqual(
        resolveRelation(refs, edge, member, MODEL_ROOT, reader),
        expected,
        `${edge.field}: ${member}`,
      );
    }
  }
  // A plain list-values relation strips nothing: `@` is part of its value.
  const accessible = refs.relations.find((edge) => edge.field.endsWith("memory.accessible"));
  assert.ok(accessible);
  const hardware = fakeReader({
    [at("hardware", "Core0.yaml")]: doc(["name: Core0", "kind: core"]),
  });
  assert.ok(resolveRelation(refs, accessible, "Core0", MODEL_ROOT, hardware));
  assert.equal(resolveRelation(refs, accessible, "Core0@1", MODEL_ROOT, hardware), undefined);
});

test("a memory's accessible core resolves; a processor of the same name does not", () => {
  const refs = shipped();
  const reader = fakeReader({
    [at("hardware", "Core0.yaml")]: doc(["name: Core0", "kind: core"]),
    [at("hardware", "CPU.yaml")]: doc(["name: CPU", "kind: processor"]),
  });
  for (const text of [INDENTED_MEMORY, INDENTLESS_MEMORY]) {
    assert.deepEqual(relationTarget(refs, text, "hardware", lineOf(text, "Core0"), reader), {
      file: at("hardware", "Core0.yaml"),
      line: 0,
    });
    const cpu = text.replace("Core0", "CPU");
    assert.equal(relationAt(refs, cpu, "hardware", lineOf(cpu, "CPU"))?.value, "CPU");
    assert.equal(relationTarget(refs, cpu, "hardware", lineOf(cpu, "CPU"), reader), undefined);
  }
  // A hardware file of another kind holds no accessible relation.
  const processor = INDENTLESS_MEMORY.replace("kind: memory", "kind: processor");
  assert.equal(relationAt(refs, processor, "hardware", lineOf(processor, "Core0")), undefined);
});

const COMPONENT_MEMORY = doc([
  "name: Brake",
  "memory:",
  "  RAM: 2048",
  "  'Flash': 10",
  "data_type_mappings:",
  "- BrakeMaps",
  "ports: []",
]);

test("a component's memory key and its mapping set resolve", () => {
  const refs = shipped();
  const reader = fakeReader({
    [at("hardware", "RAM.yaml")]: doc(["name: RAM", "kind: memory"]),
    [at("data_types", "mapping_sets", "BrakeMaps.yaml")]: doc(["name: BrakeMaps"]),
  });
  const ram = lineOf(COMPONENT_MEMORY, "RAM: 2048");
  assert.deepEqual(relationAt(refs, COMPONENT_MEMORY, "swc", ram)?.value, "RAM");
  assert.deepEqual(relationTarget(refs, COMPONENT_MEMORY, "swc", ram, reader), {
    file: at("hardware", "RAM.yaml"),
    line: 0,
  });
  // A quoted key is not one the key pattern reads.
  assert.equal(
    relationAt(refs, COMPONENT_MEMORY, "swc", lineOf(COMPONENT_MEMORY, "Flash")),
    undefined,
  );
  assert.equal(
    relationAt(refs, COMPONENT_MEMORY, "swc", lineOf(COMPONENT_MEMORY, "memory:")),
    undefined,
  );
  assert.deepEqual(
    relationTarget(refs, COMPONENT_MEMORY, "swc", lineOf(COMPONENT_MEMORY, "- BrakeMaps"), reader),
    { file: at("data_types", "mapping_sets", "BrakeMaps.yaml"), line: 0 },
  );
});

test("a relation whose target field is not the target's identity resolves nothing", () => {
  const refs = shipped();
  const reader = fakeReader({ [at("hardware", "Core0.yaml")]: doc(["name: Core0", "kind: core"]) });
  const accessible = refs.relations.find((edge) => edge.field.endsWith("memory.accessible"));
  assert.ok(accessible);
  assert.ok(resolveRelation(refs, accessible, "Core0", MODEL_ROOT, reader));
  assert.equal(
    resolveRelation(refs, { ...accessible, targetField: "kind" }, "Core0", MODEL_ROOT, reader),
    undefined,
  );
});

test("Go to Definition on a relation, and the hover's card of its target", () => {
  const refs = shipped();
  const reader = fakeReader({
    [at("hardware", "Core0.yaml")]: doc(["name: Core0", "kind: core", "description: First core"]),
  });
  const file = at("hardware", "RAM.yaml");
  const found = definitionTarget(
    refs,
    INDENTLESS_MEMORY,
    "hardware",
    lineOf(INDENTLESS_MEMORY, "Core0"),
    file,
    MODEL_ROOT,
    reader,
  );
  assert.deepEqual(found, {
    target: { file: at("hardware", "Core0.yaml"), line: 0 },
    targetLayer: "hardware",
    edgeKind: "relation",
  });
  const text = reader.readText(at("hardware", "Core0.yaml"));
  assert.ok(text);
  const card = hoverCard(text, layer(refs, "hardware"));
  assert.equal(card?.kind, "core");
  assert.equal(card?.description, "First core");
});

// --- member references -------------------------------------------------------

const COMPONENT_INDENTLESS = [
  "name: Brake",
  "ports:",
  "- name: pOut",
  "  direction: provide",
  "  interface: ISig",
  "- name: pStub",
  "  interface: IStub",
  "- name: pCs",
  "  interface: ICs",
  "- name: pLost",
  "  interface: INowhere",
  "- name: pBare",
  "runnables:",
  "- name: Run",
  "  writes:",
  "  - port: pOut",
  "    element: speed",
  "  - port: pStub",
  "    element: raw",
  "  reads:",
  "  - element: speed",
  "    port: pOut",
  "  calls:",
  "  - port: pCs",
  "    operation: Get",
  "  - port: pLost",
  "    operation: Get",
  "  - port: pBare",
  "    operation: Get",
];

// The same component hand-written: every sequence indented two columns past its owner.
const COMPONENT_INDENTED = [
  "name: Brake",
  "ports:",
  "  - name: pOut",
  "    direction: provide",
  "    interface: ISig",
  "  - name: pStub",
  "    interface: IStub",
  "  - name: pCs",
  "    interface: ICs",
  "  - name: pLost",
  "    interface: INowhere",
  "  - name: pBare",
  "runnables:",
  "  - name: Run",
  "    writes:",
  "      - port: pOut",
  "        element: speed",
  "      - port: pStub",
  "        element: raw",
  "    reads:",
  "      - element: speed",
  "        port: pOut",
  "    calls:",
  "      - port: pCs",
  "        operation: Get",
  "      - port: pLost",
  "        operation: Get",
  "      - port: pBare",
  "        operation: Get",
];

function component(indentless: boolean): string {
  return doc(indentless ? COMPONENT_INDENTLESS : COMPONENT_INDENTED);
}

const INTERFACE_FILES: Record<string, string> = {
  [at("interfaces", "ISig.yaml")]: doc([
    "name: ISig",
    "kind: sender_receiver",
    "data_elements:",
    "- name: speed",
    "  type: Speed_T",
    "- name: torque",
  ]),
  [at("interfaces", "IStub.yaml")]: doc([
    "name: IStub",
    "kind: external_interface",
    "data_elements:",
    "- raw",
    "- other",
    "operations:",
    "- raw",
  ]),
  [at("interfaces", "ICs.yaml")]: doc([
    "name: ICs",
    "kind: client_server",
    "operations:",
    "- name: Get",
    "  arguments:",
    "  - name: speed",
    "    direction: out",
  ]),
};

function memberTarget(text: string, line: number, reader: Reader = fakeReader(INTERFACE_FILES)) {
  const refs = shipped();
  const file = at("swc", "Brake.yaml");
  return definitionTarget(refs, text, "swc", line, file, MODEL_ROOT, reader);
}

test("a port: member reference resolves in the same buffer, to its ports item, in both styles", () => {
  for (const indentless of [true, false]) {
    const text = component(indentless);
    const line = lineOf(text, "- port: pOut");
    const hit = memberReferenceAt(shipped(), text, "swc", line);
    assert.equal(hit?.edge.field, "field:swc.data_write.port");
    assert.equal(hit?.value, "pOut");
    assert.deepEqual(memberTarget(text, line), {
      target: { file: at("swc", "Brake.yaml"), line: lineOf(text, "- name: pOut") },
      targetLayer: "swc",
      edgeKind: "memberReference",
    });
    // The read's port, on the item's second line.
    assert.equal(memberTarget(text, lineOf(text, "port: pOut", 1))?.target.line, 2);
  }
  // Two ports items of that name: none.
  const twice = doc([
    "name: Brake",
    "ports:",
    "- name: pOut",
    "- name: pOut",
    "runnables:",
    "- name: Run",
    "  writes:",
    "  - port: pOut",
  ]);
  assert.equal(memberTarget(twice, lineOf(twice, "- port: pOut")), undefined);
});

// A runnable's two exclusive area lists: each entry is `{area: X}`, a member reference ending
// at a key, into the component's own `exclusive_areas`; the edge comes from references.json.
const AREA_COMPONENT_INDENTLESS = [
  "name: SpeedEstimator",
  "runnables:",
  "- name: FilterSpeed",
  "  runs_inside:",
  "  - area: SpeedState",
  "- name: CalibrateOffsets",
  "  can_enter:",
  "  - area: OffsetTable",
  "  - area: Nowhere",
  "exclusive_areas:",
  "- name: SpeedState",
  "  description: Guards the filtered speed.",
  "- name: OffsetTable",
];

const AREA_COMPONENT_INDENTED = [
  "name: SpeedEstimator",
  "runnables:",
  "  - name: FilterSpeed",
  "    runs_inside:",
  "      - area: SpeedState",
  "  - name: CalibrateOffsets",
  "    can_enter:",
  "      - area: OffsetTable",
  "      - area: Nowhere",
  "exclusive_areas:",
  "  - name: SpeedState",
  "    description: Guards the filtered speed.",
  "  - name: OffsetTable",
];

test("an area: in runs_inside and in can_enter lands on its exclusive_areas item, in both styles", () => {
  for (const indentless of [true, false]) {
    const text = doc(indentless ? AREA_COMPONENT_INDENTLESS : AREA_COMPONENT_INDENTED);
    const refs = shipped();
    const file = at("swc", "SpeedEstimator.yaml");
    const reader = fakeReader({});
    const inside = lineOf(text, "- area: SpeedState");
    const hit = memberReferenceAt(refs, text, "swc", inside);
    assert.equal(hit?.edge.field, "field:swc.runs_inside_area.area");
    assert.equal(hit?.value, "SpeedState");
    assert.deepEqual(definitionTarget(refs, text, "swc", inside, file, MODEL_ROOT, reader), {
      target: { file, line: lineOf(text, "- name: SpeedState") },
      targetLayer: "swc",
      edgeKind: "memberReference",
    });
    const enter = lineOf(text, "- area: OffsetTable");
    assert.equal(
      memberReferenceAt(refs, text, "swc", enter)?.edge.field,
      "field:swc.can_enter_area.area",
    );
    assert.deepEqual(definitionTarget(refs, text, "swc", enter, file, MODEL_ROOT, reader)?.target, {
      file,
      line: lineOf(text, "- name: OffsetTable"),
    });
    // An area the component does not declare answers nothing.
    const nowhere = lineOf(text, "- area: Nowhere");
    assert.equal(definitionTarget(refs, text, "swc", nowhere, file, MODEL_ROOT, reader), undefined);
  }
});

// A data-received event names `port` and `element`, the pair a read names, as member
// references into the component's ports and through the port into its interface's data
// elements. The edges come from references.json, which matches by key path, not by the
// member's `type`, so an operation event's `port:` is navigable too (its field declares no
// memberReference).
const DATA_EVENT_COMPONENT_INDENTLESS = [
  "name: Brake",
  "ports:",
  "- name: rSpeed",
  "  direction: require",
  "  interface: ISig",
  "- name: rStub",
  "  direction: require",
  "  interface: IStub",
  "- name: pServe",
  "  interface: ICs",
  "runnables:",
  "- name: Filter",
  "  events:",
  "  - type: data",
  "    port: rSpeed",
  "    element: speed",
  "  - type: data",
  "    port: rStub",
  "    element: raw",
  "- name: Serve",
  "  events:",
  "  - type: operation",
  "    port: pServe",
  "    operation: Get",
];

const DATA_EVENT_COMPONENT_INDENTED = [
  "name: Brake",
  "ports:",
  "  - name: rSpeed",
  "    direction: require",
  "    interface: ISig",
  "  - name: rStub",
  "    direction: require",
  "    interface: IStub",
  "  - name: pServe",
  "    interface: ICs",
  "runnables:",
  "  - name: Filter",
  "    events:",
  "      - type: data",
  "        port: rSpeed",
  "        element: speed",
  "      - type: data",
  "        port: rStub",
  "        element: raw",
  "  - name: Serve",
  "    events:",
  "      - type: operation",
  "        port: pServe",
  "        operation: Get",
];

test("a data-received event's element: and port: land on the data element and the port, in both styles", () => {
  for (const indentless of [true, false]) {
    const text = doc(indentless ? DATA_EVENT_COMPONENT_INDENTLESS : DATA_EVENT_COMPONENT_INDENTED);
    const refs = shipped();
    const element = lineOf(text, "element: speed");
    const hit = memberReferenceAt(refs, text, "swc", element);
    assert.equal(hit?.edge.field, "field:swc.data_event.element");
    assert.equal(hit?.value, "speed");
    assert.deepEqual(memberTarget(text, element), {
      target: { file: at("interfaces", "ISig.yaml"), line: 3 },
      targetLayer: "interfaces",
      edgeKind: "memberReference",
    });
    // Through an external interface stub: the `- raw` line of its data_elements.
    assert.deepEqual(memberTarget(text, lineOf(text, "element: raw"))?.target, {
      file: at("interfaces", "IStub.yaml"),
      line: 3,
    });
    const port = lineOf(text, "port: rSpeed");
    assert.equal(
      memberReferenceAt(refs, text, "swc", port)?.edge.field,
      "field:swc.data_event.port",
    );
    assert.deepEqual(memberTarget(text, port)?.target, {
      file: at("swc", "Brake.yaml"),
      line: lineOf(text, "- name: rSpeed"),
    });
    // The stated side effect: an operation event's `port:` follows the same edge.
    assert.deepEqual(memberTarget(text, lineOf(text, "port: pServe"))?.target, {
      file: at("swc", "Brake.yaml"),
      line: lineOf(text, "- name: pServe"),
    });
    // An element the interface does not hold answers nothing.
    const missing = text.replace("element: speed", "element: brake");
    assert.equal(memberTarget(missing, lineOf(missing, "element: brake")), undefined);
  }
});

test("an element: resolves through the port's interface to its data element's name line", () => {
  for (const indentless of [true, false]) {
    const text = component(indentless);
    const reader = fakeReader(INTERFACE_FILES);
    assert.deepEqual(memberTarget(text, lineOf(text, "element: speed"), reader), {
      target: { file: at("interfaces", "ISig.yaml"), line: 3 },
      targetLayer: "interfaces",
      edgeKind: "memberReference",
    });
    // The read item names its element before its port.
    assert.equal(memberTarget(text, lineOf(text, "- element: speed"), reader)?.target.line, 3);
    // Through an external interface stub: the `- raw` line of its data_elements, not its operations.
    assert.deepEqual(memberTarget(text, lineOf(text, "element: raw"), reader)?.target, {
      file: at("interfaces", "IStub.yaml"),
      line: 3,
    });
  }
});

test("an operation: resolves to the client-server operation's name line, not an argument", () => {
  for (const indentless of [true, false]) {
    const text = component(indentless);
    assert.deepEqual(memberTarget(text, lineOf(text, "operation: Get"))?.target, {
      file: at("interfaces", "ICs.yaml"),
      line: 3,
    });
  }
});

test("a member reference that cannot be followed answers nothing, in both styles", () => {
  for (const indentless of [true, false]) {
    const text = component(indentless);
    const style = indentless ? "indentless" : "indented";
    // The write item `- port: pOut` / `element: speed`, whatever its indentation.
    const write = /- port: pOut\n( +)element: speed/;
    const rewrite = (replacement: string): string => {
      const changed = text.replace(write, replacement);
      assert.notEqual(changed, text, style);
      return changed;
    };
    // A port whose interface does not resolve, and a port with no interface line.
    assert.equal(memberTarget(text, lineOf(text, "operation: Get", 1)), undefined, style);
    assert.equal(memberTarget(text, lineOf(text, "operation: Get", 2)), undefined, style);
    // An element the interface does not hold.
    const missing = text.replace("element: speed", "element: brake");
    assert.equal(memberTarget(missing, lineOf(missing, "element: brake")), undefined, style);
    // The interface's kind holds no listed collection: an element through a client-server port.
    const wrongKind = rewrite("- port: pCs\n$1element: speed");
    assert.equal(memberTarget(wrongKind, lineOf(wrongKind, "element: speed")), undefined, style);
    // The same, the client-server interface carrying a stray `data_elements:` that
    // holds the name: the target's kind decides, not the key it happens to hold.
    const stray = fakeReader({
      ...INTERFACE_FILES,
      [at("interfaces", "ICs.yaml")]: doc([
        "name: ICs",
        "kind: client_server",
        "data_elements:",
        "- name: speed",
        "operations:",
        "- name: Get",
      ]),
    });
    assert.equal(
      memberTarget(wrongKind, lineOf(wrongKind, "element: speed"), stray),
      undefined,
      style,
    );
    // A sibling port: repeated in the item.
    const repeated = rewrite("- port: pOut\n$1element: speed\n$1port: pOut");
    assert.equal(memberTarget(repeated, lineOf(repeated, "element: speed")), undefined, style);
    // An item with no port: at all.
    const portless = rewrite("- element: speed");
    assert.equal(memberTarget(portless, lineOf(portless, "- element: speed")), undefined, style);
    // The interface's data element name repeated: two members, none.
    const doubled = fakeReader({
      ...INTERFACE_FILES,
      [at("interfaces", "ISig.yaml")]: doc([
        "name: ISig",
        "kind: sender_receiver",
        "data_elements:",
        "- name: speed",
        "- name: speed",
      ]),
    });
    assert.equal(memberTarget(text, lineOf(text, "element: speed"), doubled), undefined, style);
  }
});

test("resolveMemberReference is what definitionTarget answers through", () => {
  const refs = shipped();
  const text = component(true);
  const line = lineOf(text, "element: speed");
  const hit = memberReferenceAt(refs, text, "swc", line);
  assert.ok(hit);
  assert.deepEqual(
    resolveMemberReference(
      refs,
      hit,
      { text, line, file: at("swc", "Brake.yaml"), layer: "swc" },
      MODEL_ROOT,
      fakeReader(INTERFACE_FILES),
    ),
    { target: { file: at("interfaces", "ISig.yaml"), line: 3 }, targetLayer: "interfaces" },
  );
});

test("definitionTarget: a reference is still a reference, and a plain key is nothing", () => {
  const refs = shipped();
  const text = component(true);
  const reader = fakeReader(INTERFACE_FILES);
  assert.deepEqual(
    definitionTarget(
      refs,
      text,
      "swc",
      lineOf(text, "interface: ISig"),
      at("swc", "Brake.yaml"),
      MODEL_ROOT,
      reader,
    ),
    {
      target: { file: at("interfaces", "ISig.yaml"), line: 0 },
      targetLayer: "interfaces",
      edgeKind: "reference",
    },
  );
  assert.equal(
    definitionTarget(
      refs,
      text,
      "swc",
      lineOf(text, "direction"),
      at("swc", "Brake.yaml"),
      MODEL_ROOT,
      reader,
    ),
    undefined,
  );
});

test("the hover draws no card for a member reference", () => {
  // Pinned as source text: commands.ts imports `vscode` and cannot be loaded here.
  const source = withoutCommentLines(readSource("src/commands.ts"));
  const hover = source.slice(source.indexOf("const hoverProvider"));
  assert.match(
    hover.slice(0, hover.indexOf("registerDefinitionProvider")),
    /resolved\?\.edgeKind === "memberReference" \? undefined : resolved/,
  );
  assert.match(
    functionBody(source, "function definitionAt("),
    /definitionTarget\(\s*refs,\s*document\.getText\(\),\s*at\.layer,\s*position\.line,/,
  );
});

// --- CodeLens --------------------------------------------------------------------

test("lensPlan: references and impact on the one identity line, nowhere else", () => {
  const refs = shipped();
  const text = doc(["# a component", "name: BrakeController", "type: application", "ports: []"]);
  assert.deepEqual(lensPlan(refs, text, "swc", ["ibd-BrakeController.html"]), {
    line: 1,
    name: "BrakeController",
    lenses: [{ kind: "references" }, { kind: "impact", name: "BrakeController" }],
  });
  // Two identity lines: the loader keeps the last, a lens names neither.
  const twice = doc(["name: A", "name: B"]);
  assert.equal(lensPlan(refs, twice, "swc", []), undefined);
  // No identity, a refused identity, a layer the artifact does not declare.
  assert.equal(lensPlan(refs, doc(["type: application"]), "swc", []), undefined);
  assert.equal(lensPlan(refs, doc(["name: A", "  more"]), "swc", []), undefined);
  assert.equal(lensPlan(refs, doc(["name: A"]), "no_such_layer", []), undefined);
});

test("DIAGRAM_VIEW_PREFIXES transcribes the writer's view ids for compositions and sequences", () => {
  // Duplicated, a stated limit; the generator's own output keeps the two spellings one: in
  // every committed golden set, each element of a prefixed layer is listed in the view
  // `<prefix><stem>`, and that page is one the generator wrote. Not reached: which of a
  // composition's own pages (`ibd-` or `overview-`) the lens should open -- both are named for
  // it -- and a layer the goldens hold no element of.
  const goldens = path.join(
    PROJECT_ROOT,
    "..",
    "packages",
    "tools",
    "tests",
    "fixtures",
    "diagrams_golden",
  );
  const seen = new Set<string>();
  for (const set of fs.readdirSync(goldens, { withFileTypes: true })) {
    if (!set.isDirectory()) {
      continue;
    }
    const directory = path.join(goldens, set.name);
    const index = parseElementsBlock(fs.readFileSync(path.join(directory, "index.html"), "utf8"));
    assert.equal(index.kind, "ok", set.name);
    for (const [file, views] of index.kind === "ok" ? index.value : []) {
      const [layer, ...rest] = file.split("/");
      const prefix = DIAGRAM_VIEW_PREFIXES.get(layer);
      if (prefix === undefined) {
        continue;
      }
      const view = `${prefix}${path.posix.basename(rest.join("/"), ".yaml")}`;
      assert.ok(views.views.includes(view), `${set.name}: ${file} is not drawn in ${view}`);
      assert.ok(
        fs.existsSync(path.join(directory, `${view}.html`)),
        `${set.name}: no ${view}.html`,
      );
      seen.add(layer);
    }
  }
  assert.deepEqual([...seen].sort(), [...DIAGRAM_VIEW_PREFIXES.keys()].sort());
});

test("lensPlan: the diagram lens for a composition's ibd page and a sequence's page, listed exactly", () => {
  const refs = shipped();
  const composition = doc(["name: Chassis", "members: []"]);
  const sequence = doc(["name: BrakeFlow", "steps: []"]);
  const listing = [
    "index.html",
    "ibd-Chassis.html",
    "overview-Chassis.html",
    "sequence-BrakeFlow.html",
  ];
  assert.deepEqual(lensPlan(refs, composition, "compositions", listing)?.lenses.at(-1), {
    kind: "diagram",
    view: "ibd-Chassis",
  });
  assert.deepEqual(lensPlan(refs, sequence, "sequences", listing)?.lenses.at(-1), {
    kind: "diagram",
    view: "sequence-BrakeFlow",
  });
  const diagramOf = (text: string, layer: string, entries: string[] | undefined) =>
    lensPlan(refs, text, layer, entries)?.lenses.find((lens) => lens.kind === "diagram");
  // A case variant, a `~N` page or the SVG alone is not the element's page.
  assert.equal(diagramOf(composition, "compositions", ["ibd-chassis.html"]), undefined);
  assert.equal(diagramOf(composition, "compositions", ["ibd-Chassis~1.html"]), undefined);
  assert.equal(diagramOf(composition, "compositions", ["ibd-Chassis.svg"]), undefined);
  // views_root refused or unreadable.
  assert.equal(diagramOf(composition, "compositions", undefined), undefined);
  // Any other layer has no page, even when a file of that name is listed.
  assert.equal(diagramOf(doc(["name: Chassis"]), "swc", ["ibd-Chassis.html"]), undefined);
});

/** An index page holding only an elements block of *json*, read as the lens reads it. */
function indexBlock(json: string) {
  return parseElementsBlock(
    `<body><script type="application/json" id="swcraft-elements">${json}</script></body>`,
  );
}

test("lensPlan with the diagrams' index: the diagram lens on every element it lists", () => {
  const refs = shipped();
  const elements = indexBlock(
    JSON.stringify({
      "swc/chassis/Sensor.yaml": {
        views: ["ibd-Chassis", "nesting", "deployment"],
        members: { "ports/pSpeed": ["ibd-Chassis"] },
      },
      "connectors/C1.yaml": { views: ["ibd-Chassis"], members: {} },
      "compositions/Chassis.yaml": { views: ["ibd-Chassis", "ibd-Vehicle"], members: {} },
      "tasks/Unused.yaml": { views: [], members: {} },
    }),
  );
  assert.equal(elements.kind, "ok");
  const lastOf = (text: string, layer: string, file: string | undefined, listing: string[] = []) =>
    lensPlan(refs, text, layer, listing, { file, elements })?.lenses.at(-1);
  const show = (file: string) => ({ kind: "show-in-diagram", file });
  // An element on several views, and one on one view: Show in Diagram on the element.
  assert.deepEqual(
    lastOf(doc(["name: Sensor"]), "swc", "swc/chassis/Sensor.yaml"),
    show("swc/chassis/Sensor.yaml"),
  );
  assert.deepEqual(
    lastOf(doc(["name: C1"]), "connectors", "connectors/C1.yaml"),
    show("connectors/C1.yaml"),
  );
  // On no view: listed with none, or not listed at all — no lens.
  assert.equal(lastOf(doc(["name: Unused"]), "tasks", "tasks/Unused.yaml")?.kind, "impact");
  assert.equal(
    lastOf(doc(["name: ISpeed"]), "interfaces", "interfaces/ISpeed.yaml")?.kind,
    "impact",
  );
  // The file's name was not found in the index (no lexical or single real-path match).
  assert.equal(lastOf(doc(["name: Sensor"]), "swc", undefined)?.kind, "impact");
  // A composition keeps its own page and the direct open while its page is listed…
  assert.deepEqual(
    lastOf(doc(["name: Chassis"]), "compositions", "compositions/Chassis.yaml", [
      "ibd-Chassis.html",
    ]),
    { kind: "diagram", view: "ibd-Chassis" },
  );
  // …and is Show in Diagram when the index draws it and its page is not listed.
  assert.deepEqual(
    lastOf(doc(["name: Chassis"]), "compositions", "compositions/Chassis.yaml"),
    show("compositions/Chassis.yaml"),
  );
  // One lens only: the page's lens is not doubled by the index's.
  const both = lensPlan(refs, doc(["name: Chassis"]), "compositions", ["ibd-Chassis.html"], {
    file: "compositions/Chassis.yaml",
    elements,
  });
  assert.equal(both?.lenses.filter((lens) => lens.kind !== "references").length, 2);
});

test("lensPlan with the diagrams' index: a duplicate identity, no index and an unreadable one draw no lens", () => {
  const refs = shipped();
  const text = doc(["name: Sensor"]);
  // Two files declaring one identity: the generator's entries refuse both, so its
  // index lists neither file, and neither file's lens is drawn.
  const duplicate = indexBlock(
    JSON.stringify({ "connectors/C1.yaml": { views: ["ibd-Chassis"], members: {} } }),
  );
  for (const file of ["swc/chassis/Sensor.yaml", "swc/other/Sensor.yaml"]) {
    assert.equal(
      lensPlan(refs, text, "swc", [], { file, elements: duplicate })?.lenses.at(-1)?.kind,
      "impact",
      file,
    );
  }
  // No index: no `index.html`, or no index argument at all (the old call).
  const none = lensPlan(refs, text, "swc", [], {
    file: "swc/chassis/Sensor.yaml",
    elements: undefined,
  });
  assert.equal(none?.lenses.at(-1)?.kind, "impact");
  assert.equal(lensPlan(refs, text, "swc", [])?.lenses.at(-1)?.kind, "impact");
  // An unreadable index: no elements block (another release's index), malformed JSON, a wrong shape.
  for (const elements of [
    parseElementsBlock("<body><h1>Old index</h1></body>"),
    indexBlock("{"),
    indexBlock('{"swc/chassis/Sensor.yaml": {"views": "ibd-Chassis", "members": {}}}'),
  ]) {
    assert.notEqual(elements.kind, "ok");
    const plan = lensPlan(refs, text, "swc", [], { file: "swc/chassis/Sensor.yaml", elements });
    assert.equal(plan?.lenses.at(-1)?.kind, "impact", elements.kind);
  }
  // No identity line: no lens at all, the index notwithstanding.
  assert.equal(
    lensPlan(refs, doc(["type: application"]), "swc", [], {
      file: "swc/chassis/Sensor.yaml",
      elements: indexBlock(
        JSON.stringify({ "swc/chassis/Sensor.yaml": { views: ["ibd-Chassis"], members: {} } }),
      ),
    }),
    undefined,
  );
});

test("the show-in-diagram lens runs Show in Diagram with the element's file and name, titled diagram", () => {
  const source = withoutCommentLines(readSource("src/commands.ts"));
  const lens = functionBody(source, "function codeLensOf(");
  assert.match(
    lens,
    /case "show-in-diagram":\s*return new vscode\.CodeLens\(range, \{\s*title: "diagram",\s*command: "swcraft\.showInDiagram",\s*arguments: \[\{ file: item\.file, name: plan\.name \}\],/,
  );
  // The composition's and the sequence's lens keep the direct open of their own page.
  assert.match(
    lens,
    /case "diagram":\s*return new vscode\.CodeLens\(range, \{\s*title: "diagram",\s*command: "swcraft\.diagrams",\s*arguments: \[\{ view: item\.view \}\],/,
  );
  // The lenses read the index through the one cached reader and the one name match.
  const lenses = functionBody(source, "function codeLensesOf(");
  assert.match(lenses, /diagramIndexEntry\(views\.path\)/);
  assert.match(lenses, /indexedNameOf\(entry, modelRoot, document\.uri\.fsPath\)/);
  // The guide lens is in the row, built by guideLens.ts's own two functions.
  assert.match(lenses, /guideLens\(/);
  assert.match(lenses, /guideLensCommand\(/);
});

test("a workspace symbol's partial location is set after the SymbolInformation constructor", () => {
  // Pinned as source text (`commands.ts` imports `vscode`). The constructor keeps a location
  // only when it is a `vscode.Location` instance, so a plain `{uri}` handed to `super` leaves
  // it undefined and the host's converter throws on every Go to Symbol in Workspace answer;
  // only the host witness (smoke.test.ts), which no gate runs, exercises that converter.
  const source = withoutCommentLines(readSource("src/commands.ts"));
  const start = source.indexOf("class FileSymbol extends vscode.SymbolInformation {");
  assert.ok(start >= 0, "commands.ts declares no FileSymbol");
  const body = source.slice(start, source.indexOf("\n}\n", start));
  assert.match(body, /super\(\s*name,\s*kind,\s*container,\s*new vscode\.Location\(/);
  assert.match(body, /this\.location = \{ uri: vscode\.Uri\.file\(file\) \} as vscode\.Location;/);
});

test("the references lens carries no count: complete when provided, its click Find All References", () => {
  // Pinned as source text (`commands.ts` imports `vscode`). A per-document `impact` run held
  // the lens row for seconds on a large project, so the provider has no `resolveCodeLens` and
  // no refresh: every lens of the row is complete when provided and VS Code has nothing to
  // wait for.
  //
  // The one refresh: the diagrams' index changes outside the model watch, so a press and a tab
  // reload fire the provider's `onDidChangeCodeLenses`; nothing else fires it, and a save does
  // not.
  const source = withoutCommentLines(readSource("src/commands.ts"));
  const provider = functionBody(source, "function codeLensProvider(");
  assert.match(provider, /provideCodeLenses: \(document\) => codeLensesOf\(refs, document\)/);
  assert.equal(/resolveCodeLens/.test(source), false, "a lens is resolved");
  assert.deepEqual(source.match(/onDidChangeCodeLenses[^,\n]*/g), [
    "onDidChangeCodeLenses: changes",
  ]);
  assert.equal(/onDidSaveTextDocument/.test(provider), false, "the provider refreshes on a save");
  assert.match(
    source,
    /registerCodeLensProvider\(selector, codeLensProvider\(refs, lensChanges\.event\)\)/,
  );
  assert.deepEqual(source.match(/diagramLensChanges\?\.fire\(\)/g), ["diagramLensChanges?.fire()"]);
  assert.match(functionBody(source, "function finishDiagramsPress("), /refreshDiagramLenses\(\);/);
  assert.match(
    functionBody(source, "async function buildCommand("),
    /if \(buildRunGenerations\(result\)\.views\) \{\s*diagramsTab\.reloadShown\(\);\s*refreshDiagramLenses\(\);\s*\}/,
    "a Build that generated the views refreshes the lenses, a tab open or not",
  );
  assert.match(
    source,
    /case "reload":\s*this\.reloadShown\(\);\s*refreshDiagramLenses\(\);/,
    "a tab reload refreshes the lenses",
  );
  assert.equal(
    /reloadShown\(\): void \{\s*if \([^{]*\) \{\s*this\.render\(null\);\s*\}\s*\}/.test(source),
    true,
    "reloadShown() re-reads the page only: the refresh is its callers'",
  );
  assert.equal(
    source.match(/refreshDiagramLenses\(\);/g)?.length,
    3,
    "the press, a Build that generated the views and the tab's reload — nothing else",
  );
  // The references lens: titled `references`, dirty or saved, its click Find All
  // References at the identity line — the element's own line included, as Shift+F12.
  const lens = functionBody(source, "function codeLensOf(");
  assert.match(
    lens,
    /case "references":\s*return new vscode\.CodeLens\(range, referencesLensCommand\(document\.uri, line, "references"\)\);/,
  );
  assert.equal(/isDirty/.test(lens), false, "a dirty document is titled differently");
  const click = functionBody(source, "function referencesLensCommand(");
  assert.match(click, /command: "editor\.action\.findReferences"/);
  assert.match(click, /arguments: \[uri, new vscode\.Position\(line, 0\)\]/);
  // No CLI runs for a lens: the lenses' functions reach no spawn.
  for (const name of [
    "function codeLensOf(",
    "function codeLensesOf(",
    "function codeLensProvider(",
  ]) {
    assert.equal(
      /spawn|resolveSwcraftExecutable\(|buildImpactJsonArgv\(/.test(functionBody(source, name)),
      false,
      `${name} runs the CLI`,
    );
  }
});

// --- a finding's subject located in its element's text ---------------------------

// The writer's indentless block sequences (`yaml.safe_dump(default_flow_style=False)`):
// every sequence at its owner key's own column.
const SUBJECT_INDENTLESS = doc([
  "name: SpeedCtrlSWC",
  "ports:",
  "- name: pOut",
  "  interface: ISig",
  "- name: pIn",
  "  interface: ISig",
  "  satisfies:",
  "  - REQ-11",
  "  - REQ-12@3",
  "runnables:",
  "- name: Run",
  "  writes:",
  "  - port: pOut",
  "    element: speed",
  "  - port: pOut",
  "    element: raw",
  "  satisfies:",
  "  - REQ-12@3",
  "memory:",
  "  RAM0: 2048",
  "satisfies:",
  "- REQ-12@3",
  "- 'REQ-13'",
]);

// The same component hand-written: every sequence indented past its owner.
const SUBJECT_INDENTED = doc([
  "name: SpeedCtrlSWC",
  "ports:",
  "  - name: pOut",
  "    interface: ISig",
  "  - name: pIn",
  "    interface: ISig",
  "    satisfies:",
  "      - REQ-11",
  "      - REQ-12@3",
  "runnables:",
  "  - name: Run",
  "    writes:",
  "      - port: pOut",
  "        element: speed",
  "      - port: pOut",
  "        element: raw",
  "    satisfies:",
  "      - REQ-12@3",
  "memory:",
  "  RAM0: 2048",
  "satisfies:",
  "  - REQ-12@3",
  "  - 'REQ-13'",
]);

/** The range of *value* on the *occurrence*-th line of *text* holding *needle*. */
function rangeOf(text: string, needle: string, value: string, occurrence = 0) {
  const line = lineOf(text, needle, occurrence);
  const start = text.split("\n")[line].indexOf(value);
  assert.notEqual(start, -1);
  return { line, start, end: start + value.length };
}

test("locateSubject walks fields, identity members, whole-value members and keys, both sequence styles", () => {
  const refs = shipped();
  for (const text of [SUBJECT_INDENTLESS, SUBJECT_INDENTED]) {
    const locate = (steps: Parameters<typeof locateSubject>[4]) =>
      locateSubject(refs, "swc", "swc", text, steps);
    // A field holding a block: the key's columns.
    assert.deepEqual(locate([{ field: "memory" }]), rangeOf(text, "memory:", "memory"));
    // A member named by its identity field: the identity value's columns.
    assert.deepEqual(
      locate([{ field: "ports" }, { member: "pIn" }]),
      rangeOf(text, "- name: pIn", "pIn"),
    );
    // A last field inside that member, on a scalar: its own value, not another port's.
    assert.deepEqual(
      locate([{ field: "ports" }, { member: "pOut" }, { field: "interface" }]),
      rangeOf(text, "interface: ISig", "ISig", 0),
    );
    // The component's own link, a port's and a runnable's: the member value's columns.
    assert.deepEqual(
      locate([{ field: "satisfies" }, { member: "REQ-12@3" }]),
      rangeOf(text, "- REQ-12@3", "REQ-12@3", 2),
    );
    assert.deepEqual(
      locate([
        { field: "ports" },
        { member: "pIn" },
        { field: "satisfies" },
        { member: "REQ-12@3" },
      ]),
      rangeOf(text, "- REQ-12@3", "REQ-12@3", 0),
    );
    assert.deepEqual(
      locate([
        { field: "runnables" },
        { member: "Run" },
        { field: "satisfies" },
        { member: "REQ-12@3" },
      ]),
      rangeOf(text, "- REQ-12@3", "REQ-12@3", 1),
    );
    // A quoted whole value: its columns with the quotes.
    assert.deepEqual(
      locate([{ field: "satisfies" }, { member: "REQ-13" }]),
      rangeOf(text, "- 'REQ-13'", "'REQ-13'"),
    );
    // A map key.
    assert.deepEqual(
      locate([{ field: "memory" }, { key: "RAM0" }]),
      rangeOf(text, "RAM0:", "RAM0"),
    );
    // A composite identity: the one item whose identity fields read the dict's values.
    assert.deepEqual(
      locate([
        { field: "runnables" },
        { member: "Run" },
        { field: "writes" },
        { member: { port: "pOut", element: "raw" } },
      ]),
      rangeOf(text, "- port: pOut", "pOut", 1),
    );
  }
});

test("locateSubject answers undefined where the path does not resolve", () => {
  const refs = shipped();
  const locate = (text: string, steps: Parameters<typeof locateSubject>[4]) =>
    locateSubject(refs, "swc", "swc", text, steps);
  const text = SUBJECT_INDENTLESS;
  // An empty path is the element itself: the caller's identity line.
  assert.equal(locate(text, []), undefined);
  // A flow list has no item line.
  const flow = doc([
    "name: X",
    "satisfies: [REQ-12@3]",
    "ports:",
    "- name: p",
    "  satisfies: [REQ-12]",
  ]);
  assert.equal(locate(flow, [{ field: "satisfies" }, { member: "REQ-12@3" }]), undefined);
  assert.equal(
    locate(flow, [
      { field: "ports" },
      { member: "p" },
      { field: "satisfies" },
      { member: "REQ-12" },
    ]),
    undefined,
  );
  // A member, a field or a key the text does not hold.
  assert.equal(locate(text, [{ field: "ports" }, { member: "pGone" }]), undefined);
  assert.equal(locate(text, [{ field: "satisfies" }, { member: "REQ-99" }]), undefined);
  assert.equal(locate(text, [{ field: "description" }]), undefined);
  assert.equal(locate(text, [{ field: "memory" }, { key: "ROM0" }]), undefined);
  // A port's link sought on another port: the search stays inside the member reached.
  assert.equal(
    locate(text, [{ field: "ports" }, { member: "pOut" }, { field: "satisfies" }]),
    undefined,
  );
  // A member held twice, a composite matching two items, and one matching none.
  const twice = doc(["name: X", "satisfies:", "- REQ-12@3", "- REQ-12@3"]);
  assert.equal(locate(twice, [{ field: "satisfies" }, { member: "REQ-12@3" }]), undefined);
  const writes = [{ field: "runnables" }, { member: "Run" }, { field: "writes" }];
  assert.equal(locate(text, [...writes, { member: { port: "pOut" } }]), undefined);
  assert.equal(locate(text, [...writes, { member: { port: "pOut", element: "x" } }]), undefined);
  // A mapping where the collection names its members by a field.
  assert.equal(locate(text, [{ field: "ports" }, { member: { name: "pIn" } }]), undefined);
  // A collection the artifact does not declare there: a map's members are keys.
  assert.equal(locate(text, [{ field: "memory" }, { member: "RAM0" }]), undefined);
  // A step below a whole-value member.
  assert.equal(
    locate(text, [{ field: "satisfies" }, { member: "REQ-12@3" }, { field: "x" }]),
    undefined,
  );
  // A layer or a top-level kind the collection facts do not hold.
  assert.equal(
    locateSubject(refs, "swc", "core", text, [{ field: "ports" }, { member: "pIn" }]),
    undefined,
  );
});

// A finding on a field is about its value.
const SUBJECT_VALUES = doc([
  "name: BrakeSWC",
  "ports:",
  "- name: pBrake",
  "  interface: IPermittedBrake",
  "- name: pDouble",
  '  interface: "IPermittedBrake"  # quoted',
  "- name: pSingle",
  "  interface: 'IPermittedBrake'",
  "- name: pEmpty",
  "  interface:",
  "- name: pFlow",
  "  interface: [IA, IB]",
  "- name: pFolded",
  "  interface: |",
  "    IPermittedBrake",
  "- name: pTypo",
  "  intrface: ISig",
  "- name: pCommented",
  "  interface: ISig # the signal",
  "derived___from: REQ_1",
]);

test("locateSubject: a last field step on a scalar marks the value, quotes included", () => {
  const refs = shipped();
  const locate = (steps: Parameters<typeof locateSubject>[4]) =>
    locateSubject(refs, "swc", "swc", SUBJECT_VALUES, steps);
  const iface = (port: string) => [{ field: "ports" }, { member: port }, { field: "interface" }];
  // A reference finding: the name it refers to.
  assert.deepEqual(
    locate(iface("pBrake")),
    rangeOf(SUBJECT_VALUES, "  interface: IPermittedBrake", "IPermittedBrake"),
  );
  // A quoted value: its quotes too, and never the comment after it.
  assert.deepEqual(
    locate(iface("pDouble")),
    rangeOf(SUBJECT_VALUES, '  interface: "IPermittedBrake"', '"IPermittedBrake"'),
  );
  assert.deepEqual(
    locate(iface("pSingle")),
    rangeOf(SUBJECT_VALUES, "  interface: 'IPermittedBrake'", "'IPermittedBrake'"),
  );
  assert.deepEqual(
    locate(iface("pCommented")),
    rangeOf(SUBJECT_VALUES, "  interface: ISig # the signal", "ISig"),
  );
  // A top-level field on a scalar: its value.
  assert.deepEqual(
    locate([{ field: "name" }]),
    rangeOf(SUBJECT_VALUES, "name: BrakeSWC", "BrakeSWC"),
  );
});

test("locateSubject: a block, an empty or a refused value, a key step and an inner field keep the key", () => {
  const refs = shipped();
  const locate = (steps: Parameters<typeof locateSubject>[4]) =>
    locateSubject(refs, "swc", "swc", SUBJECT_VALUES, steps);
  const iface = (port: string) => [{ field: "ports" }, { member: port }, { field: "interface" }];
  // The *occurrence*-th `interface` key line: the empty one is a prefix of the others.
  const interfaceKey = (needle: string, occurrence = 0) =>
    rangeOf(SUBJECT_VALUES, needle, "interface", occurrence);
  // A block list below the key: the field as a whole, the key.
  assert.deepEqual(locate([{ field: "ports" }]), rangeOf(SUBJECT_VALUES, "ports:", "ports"));
  // An empty value, a flow list and a multi-line scalar: the key.
  assert.deepEqual(locate(iface("pEmpty")), interfaceKey("  interface:", 3));
  assert.deepEqual(locate(iface("pFlow")), interfaceKey("  interface: [IA, IB]"));
  assert.deepEqual(locate(iface("pFolded")), interfaceKey("  interface: |"));
  // A key step on a scalar — an undeclared key, at the top and inside a member: the stray key
  // itself, never its value.
  assert.deepEqual(
    locate([{ key: "derived___from" }]),
    rangeOf(SUBJECT_VALUES, "derived___from: REQ_1", "derived___from"),
  );
  assert.deepEqual(
    locate([{ field: "ports" }, { member: "pTypo" }, { key: "intrface" }]),
    rangeOf(SUBJECT_VALUES, "  intrface: ISig", "intrface"),
  );
  // A field step that is not last: the range is the last step's, as before —
  // the member's identity value, and an endpoint's stray key below its field.
  assert.deepEqual(
    locate([{ field: "ports" }, { member: "pBrake" }]),
    rangeOf(SUBJECT_VALUES, "- name: pBrake", "pBrake"),
  );
  const connector = doc(["name: A_to_B", "from:", "  component: A", "  port: pOut", "  prt: pOut"]);
  assert.deepEqual(
    locateSubject(refs, "connectors", "connector", connector, [{ field: "from" }, { key: "prt" }]),
    rangeOf(connector, "  prt: pOut", "prt"),
  );
});

test("memberLinesOf searches the whole text, or only the item it is given", () => {
  const text = SUBJECT_INDENTLESS;
  const all = memberLinesOf(text, ["runnables", "*", "writes"], "port", "pOut");
  assert.deepEqual(all, [lineOf(text, "- port: pOut", 0), lineOf(text, "- port: pOut", 1)]);
  const first = lineOf(text, "- port: pOut", 1);
  assert.deepEqual(
    memberLinesOf(text, ["runnables", "*", "writes"], "port", "pOut", { first, last: first + 1 }),
    [first],
  );
  assert.deepEqual(memberLinesOf(text, ["satisfies"], null, "REQ-12@3"), [
    lineOf(text, "- REQ-12@3", 2),
  ]);
});

// --- the element form and the member lines -------------------------------------

function derivedFrom(member: string, dash = "  - "): string {
  return doc(["id: REQ-20", "version: 1", "derived_from:", `${dash}${member}`]);
}

const ELEMENT_FILES: Record<string, string> = {
  [at("hardware", "Core0.yaml")]: doc(["name: Core0", "kind: core", "description: First core"]),
  [at("swc", "body", "Door.yaml")]: doc(["name: Door", "type: application"]),
  [at("connectors", "X.yaml")]: doc(["name: X"]),
  [at("requirements", "hardware.yaml")]: doc(["id: hardware", "version: 1"]),
};

test("a derived_from member in the element form resolves to the element; the hover is its layer's", () => {
  const refs = shipped();
  const reader = fakeReader(ELEMENT_FILES);
  const file = at("requirements", "REQ-20.yaml");
  for (const dash of ["  - ", "- "]) {
    const text = derivedFrom("hardware:Core0", dash);
    const found = definitionTarget(refs, text, "requirements", 3, file, MODEL_ROOT, reader);
    assert.deepEqual(found, {
      target: { file: at("hardware", "Core0.yaml"), line: 0 },
      targetLayer: "hardware",
      edgeKind: "relation",
    });
    const targetText = reader.readText(at("hardware", "Core0.yaml"));
    assert.ok(found && targetText);
    const card = hoverCard(targetText, layer(refs, found.targetLayer));
    assert.equal(card?.kind, "core", "the hover's facts are the hardware layer's");
    assert.equal(card?.description, "First core");
  }
  const swc = definitionTarget(
    refs,
    derivedFrom("swc:Door"),
    "requirements",
    3,
    file,
    MODEL_ROOT,
    reader,
  );
  assert.deepEqual(swc, {
    target: { file: at("swc", "body", "Door.yaml"), line: 0 },
    targetLayer: "swc",
    edgeKind: "relation",
  });
});

test("a bare token, an unknown token and connector:X resolve nothing in the element form", () => {
  const refs = shipped();
  const reader = fakeReader(ELEMENT_FILES);
  const file = at("requirements", "REQ-20.yaml");
  for (const member of ["hardware:", "nosuch:Core0", "connector:X", "hardware:Missing"]) {
    assert.equal(
      definitionTarget(refs, derivedFrom(member), "requirements", 3, file, MODEL_ROOT, reader),
      undefined,
      member,
    );
  }
  // A bare token holds no ':' and is read as a requirement id: it names the
  // requirement `hardware`, never the layer.
  assert.deepEqual(
    definitionTarget(refs, derivedFrom("hardware"), "requirements", 3, file, MODEL_ROOT, reader),
    {
      target: { file: at("requirements", "hardware.yaml"), line: 0 },
      targetLayer: "requirements",
      edgeKind: "relation",
    },
  );
  assert.equal(
    definitionTarget(
      refs,
      derivedFrom("hardware"),
      "requirements",
      3,
      file,
      MODEL_ROOT,
      fakeReader({ [at("hardware", "Core0.yaml")]: doc(["name: Core0", "kind: core"]) }),
    ),
    undefined,
    "no requirement named hardware",
  );
  // The element form is derived_from's alone: in a satisfies list it names nothing.
  const satisfies = doc(["name: Door", "satisfies:", "  - hardware:Core0"]);
  assert.equal(
    definitionTarget(refs, satisfies, "swc", 2, at("swc", "Door.yaml"), MODEL_ROOT, reader),
    undefined,
  );
});

/** The relation paths into `requirements` published for *layerName*'s kind *topKind*. */
function requirementPaths(refs: References, layerName: string, topKind: string): string[][] {
  return refs.relations
    .filter((edge) => edge.targetLayer === "requirements" && edge.sourceLayer === layerName)
    .flatMap((edge) => edge.paths.filter((entry) => entry.topKind === topKind))
    .map((entry) => [...entry.path]);
}

function linkedComponent(indentless: boolean, own: string, port: string, runnable: string): string {
  const d = indentless ? "- " : "  - ";
  const n = indentless ? "  " : "    ";
  return doc([
    "name: Door",
    "ports:",
    `${d}name: pOut`,
    `${n}satisfies:`,
    `${n}  - ${port}`,
    `${n}  - REQ-99@1`,
    "runnables:",
    `${d}name: Run`,
    `${n}satisfies:`,
    `${n}- ${runnable}`,
    "satisfies:",
    `${d}${own}`,
    `${d}REQ-77@2`,
  ]);
}

test("requirementMemberLines: the own, a port's and a runnable's link, stated, bare or malformed", () => {
  const refs = shipped();
  const paths = requirementPaths(refs, "swc", "swc");
  assert.equal(paths.length, 3);
  for (const indentless of [false, true]) {
    for (const [own, port, runnable] of [
      ["REQ-12@3", "REQ-12", "REQ-12@x"],
      ["REQ-12", "REQ-12@x", "'REQ-12@3'"],
    ]) {
      const text = linkedComponent(indentless, own, port, runnable);
      const lines = text.split("\n");
      const on = (line: number, value: string) => {
        const start = lines[line].indexOf(value);
        assert.notEqual(start, -1);
        return { line, start, end: start + value.length };
      };
      const expected = [on(4, port), on(9, runnable), on(11, own)];
      assert.deepEqual(requirementMemberLines(text, paths, "REQ-12"), expected, text);
      assert.equal(requirementMemberLines(text, paths, "REQ-77").length, 1);
      assert.deepEqual(requirementMemberLines(text, paths, "REQ-1"), [], "a prefix is no match");
    }
  }
});

test("requirementMemberLines: a flow list, a list at no published path, and no path, find nothing", () => {
  const refs = shipped();
  const paths = requirementPaths(refs, "swc", "swc");
  const flow = doc(["name: Door", "satisfies: [REQ-12@3]"]);
  assert.deepEqual(requirementMemberLines(flow, paths, "REQ-12"), []);
  const elsewhere = doc(["name: Door", "notes:", "  - REQ-12@3"]);
  assert.deepEqual(requirementMemberLines(elsewhere, paths, "REQ-12"), []);
  const own = doc(["name: Door", "satisfies:", "  - REQ-12@3"]);
  assert.deepEqual(requirementMemberLines(own, [], "REQ-12"), []);
  // A requirement's derived_from is its layer's own path.
  const derived = derivedFrom("REQ-12@3");
  assert.deepEqual(
    requirementMemberLines(
      derived,
      requirementPaths(refs, "requirements", "requirement"),
      "REQ-12",
    ),
    [{ line: 3, start: 4, end: 12 }],
  );
});

/**
 * The `impact` document recorded verbatim from the live CLI on an unmodified scratch copy of
 * `examples/basic`: `.venv/bin/python -m swcraft_tools.cli impact --project
 * <copy>/swcraft.yaml --format json --depth 1 -- REQ_EXAMPLE_001`.
 */
function recordedRequirementImpact(): string {
  return fs.readFileSync(
    path.join(PROJECT_ROOT, "src", "fixtures", "navigation", "impact-REQ_EXAMPLE_001.json"),
    "utf8",
  );
}

/** ExampleSWC links the requirement three times; ExampleConsumerSWC in a flow list only. */
function requirementDependents(): Reader {
  return fakeReader({
    [at("requirements", "REQ_EXAMPLE_001.yaml")]: doc(["id: REQ_EXAMPLE_001", "version: 1"]),
    [at("swc", "example", "ExampleSWC.yaml")]: doc([
      "name: ExampleSWC",
      "ports:",
      "  - name: pOut",
      "    satisfies:",
      "      - REQ_EXAMPLE_001",
      "runnables:",
      "  - name: ExampleRun",
      "    satisfies:",
      "      - REQ_EXAMPLE_001@x",
      "satisfies:",
      "- REQ_EXAMPLE_001@1",
    ]),
    [at("swc", "example", "ExampleConsumerSWC.yaml")]: doc([
      "name: ExampleConsumerSWC",
      "satisfies: [REQ_EXAMPLE_001@1]",
    ]),
  });
}

test("the recorded requirement impact parses with its name, the rows under the requirement token", () => {
  const parsed = parseImpactDocument(recordedRequirementImpact());
  assert.equal(parsed.name, "REQ_EXAMPLE_001");
  assert.deepEqual(parsed.matched.get("requirement"), [
    { layer: "swc", name: "ExampleConsumerSWC" },
    { layer: "swc", name: "ExampleSWC" },
  ]);
});

test("Find All References on a requirement: one location per member line, the declaration first", () => {
  const refs = shipped();
  const declaration = { file: at("requirements", "REQ_EXAMPLE_001.yaml"), line: 0 };
  const swc = at("swc", "example", "ExampleSWC.yaml");
  const answer = referencesAnswer(
    refs,
    capture({ stdout: recordedRequirementImpact() }),
    "requirements",
    MODEL_ROOT,
    requirementDependents(),
    { ...OPTIONS, declaration },
  );
  assert.deepEqual(answer, {
    locations: [
      declaration,
      // A flow list holds no member line: the identity line is the fallback.
      { file: at("swc", "example", "ExampleConsumerSWC.yaml"), line: 0 },
      { file: swc, line: 4, start: 8, end: 23 },
      { file: swc, line: 8, start: 8, end: 25 },
      { file: swc, line: 10, start: 2, end: 19 },
    ],
  });
  // A layer no versioned relation targets keeps the identity line, whatever the name.
  const located = referenceLocations(
    refs,
    {
      name: "REQ_EXAMPLE_001",
      matched: new Map([["interface", [{ layer: "swc", name: "ExampleSWC" }]]]),
    },
    "interfaces",
    MODEL_ROOT,
    requirementDependents(),
  );
  assert.deepEqual(located.locations, [{ file: swc, line: 0 }]);
  // A document without a name: the identity lines, as before.
  const nameless = referenceLocations(
    refs,
    { matched: parseImpactDocument(recordedRequirementImpact()).matched },
    "requirements",
    MODEL_ROOT,
    requirementDependents(),
  );
  assert.deepEqual(nameless.locations, [
    { file: at("swc", "example", "ExampleConsumerSWC.yaml"), line: 0 },
    { file: swc, line: 0 },
  ]);
});

test("Find All References on a requirement reaches a requirement's derived_from, and a block list beside a flow list", () => {
  const refs = shipped();
  const derived = at("requirements", "REQ_B.yaml");
  const mixed = at("swc", "example", "MixedSWC.yaml");
  const reader = fakeReader({
    [derived]: doc([
      "id: REQ_B",
      "version: 1",
      "derived_from:",
      "- hardware:Core0",
      "- REQ_EXAMPLE_001@1",
    ]),
    // Its own flow-list link has no item line; the port's block item is its one location.
    [mixed]: doc([
      "name: MixedSWC",
      "satisfies: [REQ_EXAMPLE_001@1]",
      "ports:",
      "- name: pOut",
      "  satisfies:",
      "  - REQ_EXAMPLE_001@1",
    ]),
  });
  const located = referenceLocations(
    refs,
    {
      name: "REQ_EXAMPLE_001",
      matched: new Map([
        [
          "requirement",
          [
            { layer: "requirement", name: "REQ_B" },
            { layer: "swc", name: "MixedSWC" },
          ],
        ],
      ]),
    },
    "requirements",
    MODEL_ROOT,
    reader,
  );
  assert.deepEqual(located, {
    locations: [
      { file: derived, line: 4, start: 2, end: 2 + "REQ_EXAMPLE_001@1".length },
      { file: mixed, line: 5, start: 4, end: 4 + "REQ_EXAMPLE_001@1".length },
    ],
    unlocated: [],
  });
});

// --- The Impact view's documents: the detail, and the shared per-row placement ---

/** The 0-based line of *file* (under `examples/basic`'s model root) that is exactly *text*. */
function basicLine(file: string, text: string): number {
  const lines = fs.readFileSync(path.join(BASIC_MODEL, file), "utf8").split(/\r?\n/);
  const index = lines.indexOf(text);
  assert.notEqual(index, -1, `${file} holds no line '${text}'`);
  return index;
}

/** *file*'s identity line (`name: <name>`) under `examples/basic`'s model root, as a location list. */
function basicIdentity(file: string, name: string) {
  return [{ file: path.join(BASIC_MODEL, file), line: basicLine(file, `name: ${name}`) }];
}

test("the whole walk's rows keep their detail; an empty one is none", () => {
  const interfaceRows = parseImpactDocument(impactFixture("IExample.json")).matched;
  assert.deepEqual(interfaceRows.get("interface"), [
    { layer: "swc", name: "ExampleConsumerSWC", detail: "via port rIn" },
    { layer: "swc", name: "ExampleSWC", detail: "via port pOut" },
    {
      layer: "connector",
      name: "Example_to_Consumer",
      detail: "via ports ExampleSWC.pOut, ExampleConsumerSWC.rIn",
    },
    { layer: "composition", name: "Root", detail: "members ExampleSWC, ExampleConsumerSWC" },
    { layer: "task", name: "Task_10ms", detail: "runnable ExampleConsumerRun" },
    { layer: "sequence", name: "task10ms_consumer_cycle", detail: "step: ExampleConsumerRun" },
  ]);
  // The requirement's direct dependents carry `"detail": ""`: no detail key.
  const requirement = parseImpactDocument(impactFixture("REQ_EXAMPLE_001.json")).matched;
  assert.deepEqual(requirement.get("requirement")?.slice(0, 2), [
    { layer: "swc", name: "ExampleConsumerSWC" },
    { layer: "swc", name: "ExampleSWC" },
  ]);
  assert.equal(parseImpactDocument(impactFixture("NoSuchElement.json")).matched.size, 0);
  const root = parseImpactDocument(impactFixture("Root.json")).matched;
  assert.deepEqual([...root], [["composition", []]]);
});

test("a row's detail: a string is kept, an absent or non-string one is none, never a refusal", () => {
  const parsed = parseImpactDocument(
    impactDoc({
      interface: [
        { layer: "swc", name: "Kept", detail: "via port p" },
        { layer: "swc", name: "Absent" },
        { layer: "swc", name: "Number", detail: 3 },
        { layer: "swc", name: "Null", detail: null },
        { layer: "swc", name: "List", detail: ["via port p"] },
        { layer: "swc", name: "Empty", detail: "" },
      ],
    }),
  );
  assert.deepEqual(parsed.matched.get("interface"), [
    { layer: "swc", name: "Kept", detail: "via port p" },
    { layer: "swc", name: "Absent" },
    { layer: "swc", name: "Number" },
    { layer: "swc", name: "Null" },
    { layer: "swc", name: "List" },
    { layer: "swc", name: "Empty" },
  ]);
});

test("dependentLocations: each kind of row the whole walk holds opens at its identity line", () => {
  const refs = shipped();
  const parsed = parseImpactDocument(impactFixture("IExample.json"));
  const placed = (parsed.matched.get("interface") ?? []).map((row) =>
    dependentLocations(refs, row, "interfaces", parsed.name, BASIC_MODEL, diskReader),
  );
  assert.deepEqual(placed, [
    basicIdentity(path.join("swc", "example", "ExampleConsumerSWC.yaml"), "ExampleConsumerSWC"),
    basicIdentity(path.join("swc", "example", "ExampleSWC.yaml"), "ExampleSWC"),
    basicIdentity(path.join("connectors", "Example_to_Consumer.yaml"), "Example_to_Consumer"),
    basicIdentity(path.join("compositions", "Root.yaml"), "Root"),
    basicIdentity(path.join("tasks", "Task_10ms.yaml"), "Task_10ms"),
    basicIdentity(
      path.join("sequences", "task10ms_consumer_cycle.yaml"),
      "task10ms_consumer_cycle",
    ),
  ]);
});

test("dependentLocations under a requirement: a direct row's member lines, a transitive row's identity", () => {
  const refs = shipped();
  const parsed = parseImpactDocument(impactFixture("REQ_EXAMPLE_001.json"));
  const rows = parsed.matched.get("requirement") ?? [];
  const row = (name: string) => {
    const found = rows.find((candidate) => candidate.name === name);
    assert.ok(found, name);
    return found;
  };
  const place = (name: string, reader: Reader, model: string) =>
    dependentLocations(refs, row(name), "requirements", parsed.name, model, reader);
  // Direct, on disk: the one block-list member line naming the requirement.
  const swc = path.join("swc", "example", "ExampleSWC.yaml");
  const member = "- REQ_EXAMPLE_001@1";
  assert.deepEqual(place("ExampleSWC", diskReader, BASIC_MODEL), [
    {
      file: path.join(BASIC_MODEL, swc),
      line: basicLine(swc, member),
      start: 2,
      end: member.length,
    },
  ]);
  // Direct, a file naming it three times: every member line, in file order.
  const fake = at("swc", "example", "ExampleSWC.yaml");
  assert.deepEqual(place("ExampleSWC", requirementDependents(), MODEL_ROOT), [
    { file: fake, line: 4, start: 8, end: 23 },
    { file: fake, line: 8, start: 8, end: 25 },
    { file: fake, line: 10, start: 2, end: 19 },
  ]);
  // Transitive: the connector, the composition, the task and the sequence name no requirement.
  for (const [file, name] of [
    [path.join("connectors", "Example_to_Consumer.yaml"), "Example_to_Consumer"],
    [path.join("compositions", "Root.yaml"), "Root"],
    [path.join("tasks", "Task_10ms.yaml"), "Task_10ms"],
    [path.join("sequences", "task10ms_consumer_cycle.yaml"), "task10ms_consumer_cycle"],
  ]) {
    assert.deepEqual(place(name, diskReader, BASIC_MODEL), basicIdentity(file, name));
  }
  // Without the document's name, a direct dependent keeps its identity line too.
  assert.deepEqual(
    dependentLocations(refs, row("ExampleSWC"), "requirements", undefined, BASIC_MODEL, diskReader),
    basicIdentity(swc, "ExampleSWC"),
  );
});

test("dependentLocations for a root (no root layer, the empty string): its identity line, a requirement's id:", () => {
  // The Impact view's root click passes "": no relation targets it, so the file is not read for members.
  const refs = shipped();
  const requirement = path.join("requirements", "REQ_EXAMPLE_001.yaml");
  assert.deepEqual(
    dependentLocations(
      refs,
      { layer: "requirement", name: "REQ_EXAMPLE_001" },
      "",
      "REQ_EXAMPLE_001",
      BASIC_MODEL,
      diskReader,
    ),
    [
      {
        file: path.join(BASIC_MODEL, requirement),
        line: basicLine(requirement, "id: REQ_EXAMPLE_001"),
      },
    ],
  );
  assert.deepEqual(
    dependentLocations(
      refs,
      { layer: "interface", name: "IExample" },
      "",
      "IExample",
      BASIC_MODEL,
      diskReader,
    ),
    basicIdentity(path.join("interfaces", "IExample.yaml"), "IExample"),
  );
});

test("dependentLocations: an unknown token, or a file not found, places nothing", () => {
  const refs = shipped();
  const parsed = parseImpactDocument(impactFixture("unknown-token.json"));
  const rows = parsed.matched.get("interface") ?? [];
  const future = rows.find((row) => row.layer === "future_layer");
  assert.ok(future);
  const place = (row: ImpactRow) =>
    dependentLocations(refs, row, "interfaces", parsed.name, BASIC_MODEL, diskReader);
  assert.deepEqual(place(future), []);
  assert.deepEqual(place({ layer: "swc", name: "Missing" }), []);
});

test("locateSubject: a composite identity with a null field matches on its other fields", () => {
  const refs = shipped();
  const text = doc([
    "name: X",
    "runnables:",
    "- name: Run",
    "  writes:",
    "  - port: pIn",
    "  - port: pOut",
    "    element: raw",
  ]);
  const writes = [{ field: "runnables" }, { member: "Run" }, { field: "writes" }];
  assert.deepEqual(
    locateSubject(refs, "swc", "swc", text, [
      ...writes,
      { member: { port: "pIn", element: null } },
    ]),
    rangeOf(text, "  - port: pIn", "pIn"),
  );
});

// A key no field declares (`model.unknown-key`) inside an event: the event is a
// whole value whose identity is a mapping, matched over its string entries.
const SUBJECT_EVENTS = doc([
  "name: X",
  "ports:",
  "- name: pOut",
  "  intrface: ISig",
  "- name: pSrv",
  "  direction: provide",
  "runnables:",
  "- name: Other",
  "  events:",
  "  - type: timing",
  "    period: 0.02",
  "    perod: 0.02",
  "- name: R",
  "  events:",
  "  - type: init",
  "  - type: timing",
  "    period: 0.01",
  "    perod: 0.01",
  "  - type: operation",
  "    port: pSrv",
  "    operation: Op",
  "    oepration: Op",
  "  - type: operation",
  "    port: pSrv",
  "    operation: Op2",
  "    oepration: Op2",
  "  satisfies:",
  "  - REQ-12@3",
]);

test("locateSubject places a key inside an event, the event matched over its string entries", () => {
  const refs = shipped();
  const locate = (steps: Parameters<typeof locateSubject>[4]) =>
    locateSubject(refs, "swc", "swc", SUBJECT_EVENTS, steps);
  const events = [{ field: "runnables" }, { member: "R" }, { field: "events" }];
  // A key below a field-identity member: the key's columns on its own line.
  assert.deepEqual(
    locate([{ field: "ports" }, { member: "pOut" }, { key: "intrface" }]),
    rangeOf(SUBJECT_EVENTS, "  intrface: ISig", "intrface"),
  );
  // The runnable's only timing event, `type` its one string entry (`period` is a
  // number, not compared): the stray key inside that event, not the other runnable's.
  assert.deepEqual(
    locate([...events, { member: { type: "timing", period: 0.01 } }, { key: "perod" }]),
    rangeOf(SUBJECT_EVENTS, "    perod: 0.01", "perod"),
  );
  // An operation event matched by `type`, `port` and `operation`: one of two.
  assert.deepEqual(
    locate([
      ...events,
      { member: { type: "operation", port: "pSrv", operation: "Op2" } },
      { key: "oepration" },
    ]),
    rangeOf(SUBJECT_EVENTS, "    oepration: Op2", "oepration"),
  );
  // The event itself: its first compared entry's value columns.
  assert.deepEqual(
    locate([...events, { member: { type: "operation", port: "pSrv", operation: "Op" } }]),
    rangeOf(SUBJECT_EVENTS, "  - type: operation", "operation", 0),
  );
});

test("locateSubject: an event it cannot single out, and a step below a scalar whole value, answer undefined", () => {
  const refs = shipped();
  const locate = (text: string, steps: Parameters<typeof locateSubject>[4]) =>
    locateSubject(refs, "swc", "swc", text, steps);
  const events = [{ field: "runnables" }, { member: "R" }, { field: "events" }];
  // Two timing events of one runnable: their string entries are equal.
  const twoTimings = doc([
    "name: X",
    "runnables:",
    "- name: R",
    "  events:",
    "  - type: timing",
    "    period: 0.01",
    "    perod: 0.01",
    "  - type: timing",
    "    period: 0.02",
  ]);
  assert.equal(
    locate(twoTimings, [...events, { member: { type: "timing", period: 0.01 } }, { key: "perod" }]),
    undefined,
  );
  // A flow-style event has no key line to match.
  const flow = doc([
    "name: X",
    "runnables:",
    "- name: R",
    "  events:",
    "  - {type: timing, period: 0.01, perod: 0.01}",
  ]);
  assert.equal(
    locate(flow, [...events, { member: { type: "timing", period: 0.01 } }, { key: "perod" }]),
    undefined,
  );
  // A key inside a `disabled_modes` member: that collection names no discriminator,
  // so its mapping members are not matched.
  const disabled = doc([
    "name: X",
    "runnables:",
    "- name: R",
    "  events:",
    "  - type: timing",
    "    period: 0.01",
    "    disabled_modes:",
    "    - port: rMode",
    "      mode_group_ref: /M/G",
    "      mode_ref: /M/G/OFF",
    "      mdoe: x",
  ]);
  const mode = { port: "rMode", mode_group_ref: "/M/G", mode_ref: "/M/G/OFF" };
  assert.equal(
    locate(disabled, [
      ...events,
      { member: { type: "timing", period: 0.01, disabled_modes: [mode] } },
      { field: "disabled_modes" },
      { member: mode },
      { key: "mdoe" },
    ]),
    undefined,
  );
  // The event above it is still found: the list entry is not compared.
  assert.deepEqual(
    locate(disabled, [
      ...events,
      { member: { type: "timing", period: 0.01, disabled_modes: [mode] } },
      { field: "disabled_modes" },
    ]),
    rangeOf(disabled, "    disabled_modes:", "disabled_modes"),
  );
  // A mapping without its discriminator, or with it not a string, is no event identity.
  assert.equal(locate(SUBJECT_EVENTS, [...events, { member: { port: "pSrv" } }]), undefined);
  assert.equal(locate(SUBJECT_EVENTS, [...events, { member: { type: 1 } }]), undefined);
  // A `satisfies` member keeps today's answer: nothing below a scalar whole value.
  assert.equal(
    locate(SUBJECT_EVENTS, [
      { field: "runnables" },
      { member: "R" },
      { field: "satisfies" },
      { member: "REQ-12@3" },
      { key: "x" },
    ]),
    undefined,
  );
});

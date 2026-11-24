// The outline and Go to Symbol in Workspace: every decision the two providers make, tested on
// text, the shipped `references.json` and a fake file system. No `vscode` import: this runs
// under plain `node --test`.
import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as path from "node:path";
import { test } from "node:test";

import { loadReferences, type Reader, type References } from "./navigationCore";
import {
  matchesQuery,
  type OutlineSymbol,
  outlineOf,
  resolveSymbolLine,
  rootKind,
  type Span,
  SYMBOL_KIND_TABLES,
  workspaceSymbols,
} from "./symbolCore";
import { readSource, SCHEMAS_DIR } from "./testSupport";

const MODEL_ROOT = path.resolve("/projects/vc/model");

function shipped(): References {
  return loadReferences(fs.readFileSync(path.join(SCHEMAS_DIR, "references.json"), "utf8"));
}

function doc(lines: string[]): string {
  return `${lines.join("\n")}\n`;
}

function names(symbols: readonly OutlineSymbol[]): string[] {
  return symbols.map((symbol) => symbol.name);
}

function child(symbol: OutlineSymbol, name: string): OutlineSymbol {
  const found = symbol.children.find((candidate) => candidate.name === name);
  assert.ok(found, `${symbol.name} has no child ${name} (${names(symbol.children).join(", ")})`);
  return found;
}

function contains(outer: Span, inner: Span): boolean {
  const before = (a: number, b: number, c: number, d: number) => a < c || (a === c && b <= d);
  return (
    before(outer.startLine, outer.startCharacter, inner.startLine, inner.startCharacter) &&
    before(inner.endLine, inner.endCharacter, outer.endLine, outer.endCharacter)
  );
}

/** Every range holds its selection and every child's range — VS Code refuses a symbol that does not. */
function assertNested(symbols: readonly OutlineSymbol[], parent?: OutlineSymbol): void {
  for (const symbol of symbols) {
    assert.ok(contains(symbol.range, symbol.selection), `${symbol.name}: selection outside range`);
    if (parent !== undefined) {
      assert.ok(contains(parent.range, symbol.range), `${symbol.name} outside ${parent.name}`);
    }
    assertNested(symbol.children, symbol);
  }
}

const COMPONENT = [
  "name: BrakeController",
  "type: application",
  "ports:",
  "- name: rBrake",
  "  direction: require",
  "  interface: IBrake",
  "  com_specs:",
  "  - data_element: temperature",
  "  measures:",
  "    temperature: 12",
  "- name: pOut",
  "  interface: IOut",
  "runnables:",
  "- name: Run",
  "  events:",
  "  - type: timing",
  "    period: 10",
  "    disabled_modes:",
  "    - OFF",
  "  - type: init",
  "  reads:",
  "  - port: rBrake",
  "    element: temperature",
  "  writes:",
  "  - port: pOut",
  "memory:",
  "  RAM: 2048",
  "data_type_mappings:",
  "- MapA",
];

/** The same component in the hand-written style: every sequence indented under its key. */
const COMPONENT_INDENTED = [
  "name: BrakeController",
  "type: application",
  "ports:",
  "  - name: rBrake",
  "    direction: require",
  "    interface: IBrake",
  "    com_specs:",
  "      - data_element: temperature",
  "    measures:",
  "      temperature: 12",
  "  - name: pOut",
  "    interface: IOut",
  "runnables:",
  "  - name: Run",
  "    events:",
  "      - type: timing",
  "        period: 10",
  "        disabled_modes:",
  "          - OFF",
  "      - type: init",
  "    reads:",
  "      - port: rBrake",
  "        element: temperature",
  "    writes:",
  "      - port: pOut",
  "memory:",
  "  RAM: 2048",
  "data_type_mappings:",
  "  - MapA",
];

for (const [style, lines] of [
  ["indentless", COMPONENT],
  ["indented", COMPONENT_INDENTED],
] as const) {
  test(`a component's outline, ${style}: root, groups, members and their nesting`, () => {
    const text = doc(lines);
    const outline = outlineOf(shipped(), text, "swc");
    assert.equal(outline.length, 1);
    const [root] = outline;
    assert.equal(root.name, "BrakeController");
    assert.equal(root.kind, "Class");
    assert.equal(root.detail, "swc");
    assert.deepEqual(names(root.children), ["ports", "runnables", "memory", "data_type_mappings"]);

    const ports = child(root, "ports");
    assert.equal(ports.kind, "Array");
    assert.equal(ports.detail, "2 members");
    assert.deepEqual(names(ports.children), ["rBrake", "pOut"]);
    const rBrake = child(ports, "rBrake");
    assert.equal(rBrake.kind, "Interface");
    assert.deepEqual(names(rBrake.children), ["com_specs", "measures"]);
    assert.deepEqual(names(child(rBrake, "com_specs").children), ["temperature"]);
    assert.equal(child(child(rBrake, "com_specs"), "temperature").kind, "Property");
    const measures = child(rBrake, "measures");
    assert.equal(measures.kind, "Object");
    assert.equal(child(measures, "temperature").kind, "Key");
    assert.equal(child(measures, "temperature").detail, "12");

    const run = child(child(root, "runnables"), "Run");
    assert.equal(run.kind, "Function");
    assert.deepEqual(names(run.children), ["events", "reads", "writes"]);
    const events = child(run, "events");
    assert.deepEqual(names(events.children), ["timing", "init"]);
    assert.equal(child(events, "timing").kind, "Event");
    const modes = child(child(events, "timing"), "disabled_modes");
    assert.deepEqual(names(modes.children), ["OFF"]);
    assert.equal(child(modes, "OFF").kind, "EnumMember");
    assert.deepEqual(names(child(run, "reads").children), ["rBrake.temperature"]);
    // A composite identity names the values present: `element` is absent here.
    assert.deepEqual(names(child(run, "writes").children), ["pOut"]);

    const memory = child(root, "memory");
    assert.deepEqual(names(memory.children), ["RAM"]);
    assert.equal(child(memory, "RAM").detail, "2048");
    const mappings = child(root, "data_type_mappings");
    assert.deepEqual(names(mappings.children), ["MapA"]);
    assert.equal(child(mappings, "MapA").kind, "Constant");

    assertNested(outline);
    // The root spans the whole document and selects its identity line.
    assert.equal(root.range.startLine, 0);
    assert.equal(root.range.endLine, text.split("\n").length - 1);
    assert.equal(root.selection.startLine, 0);
  });
}

test("the selection of a member is its identity line; of a group its key", () => {
  const text = doc(COMPONENT);
  const [root] = outlineOf(shipped(), text, "swc");
  const ports = child(root, "ports");
  assert.deepEqual(ports.selection, {
    startLine: 2,
    startCharacter: 0,
    endLine: 2,
    endCharacter: "ports".length,
  });
  assert.equal(ports.range.endLine, 11);
  const rBrake = child(ports, "rBrake");
  assert.deepEqual(rBrake.selection, {
    startLine: 3,
    startCharacter: 2,
    endLine: 3,
    endCharacter: "- name: rBrake".length,
  });
  assert.equal(rBrake.range.startLine, 3);
  assert.equal(rBrake.range.endLine, 9);
});

for (const [style, lines] of [
  [
    "indentless",
    [
      "name: ICalc",
      "kind: client_server",
      "operations:",
      "- name: Add",
      "  arguments:",
      "  - name: a",
      "  - name: b",
      "- name: Sub",
    ],
  ],
  [
    "indented",
    [
      "name: ICalc",
      "kind: client_server",
      "operations:",
      "  - name: Add",
      "    arguments:",
      "      - name: a",
      "      - name: b",
      "  - name: Sub",
    ],
  ],
] as const) {
  test(`an interface's operations nest their arguments, ${style}`, () => {
    const outline = outlineOf(shipped(), doc([...lines]), "interfaces");
    const [root] = outline;
    assert.equal(root.kind, "Interface");
    assert.equal(root.detail, "client_server");
    const operations = child(root, "operations");
    assert.deepEqual(names(operations.children), ["Add", "Sub"]);
    assert.equal(child(operations, "Add").kind, "Method");
    const args = child(child(operations, "Add"), "arguments");
    assert.deepEqual(names(args.children), ["a", "b"]);
    assert.equal(child(args, "a").kind, "Variable");
    assert.deepEqual(child(operations, "Sub").children, []);
    assertNested(outline);
  });
}

test("a requirement's derived_from members are scalars, quoted or plain, named by their value", () => {
  const [root] = outlineOf(
    shipped(),
    doc(["id: REQ-1", "text: x", "derived_from:", "- REQ-0@2", "- 'hardware:Core0' # why"]),
    "requirements",
  );
  assert.equal(root.name, "REQ-1");
  assert.equal(root.kind, "Key");
  assert.deepEqual(names(child(root, "derived_from").children), ["REQ-0@2", "hardware:Core0"]);
});

test("a task's runnables are named by their present identity values joined with '.'", () => {
  const [root] = outlineOf(
    shipped(),
    doc([
      "name: T10",
      "runnables:",
      "- swc: BrakeController",
      "  runnable: Run",
      "- instance: /Chassis/Front",
      "  runnable: Run",
    ]),
    "tasks",
  );
  assert.equal(root.kind, "Event");
  const runnables = child(root, "runnables");
  assert.deepEqual(names(runnables.children), ["BrakeController.Run", "/Chassis/Front.Run"]);
  assert.equal(runnables.children[0].kind, "Function");
});

test("a data-received event is an Event in the outline, named by its discriminator, beside the timing one", () => {
  const [root] = outlineOf(
    shipped(),
    doc([
      "name: SpeedEstimator",
      "runnables:",
      "- name: MonitorSpeed",
      "  events:",
      "  - type: timing",
      "    period: 0.1",
      "  - type: data",
      "    port: R_WheelSpeed",
      "    element: FrontRight",
    ]),
    "swc",
  );
  const events = child(child(child(root, "runnables"), "MonitorSpeed"), "events");
  assert.deepEqual(names(events.children), ["timing", "data"]);
  assert.equal(child(events, "data").kind, "Event");
  assert.equal(child(events, "timing").kind, "Event");
});

test("a call-returned event is an Event in the outline, named by its discriminator", () => {
  const [root] = outlineOf(
    shipped(),
    doc([
      "name: BrakeController",
      "runnables:",
      "- name: OnBlockWritten",
      "  events:",
      "  - type: call_returned",
      "    port: rNvStorage",
      "    operation: WriteBlock",
    ]),
    "swc",
  );
  const events = child(child(child(root, "runnables"), "OnBlockWritten"), "events");
  assert.deepEqual(names(events.children), ["call_returned"]);
  assert.equal(child(events, "call_returned").kind, "Event");
});

test("a member whose identity is missing or refused is shown as [i]", () => {
  const [root] = outlineOf(
    shipped(),
    doc([
      "name: C",
      "ports:",
      "- name: a",
      "- direction: provide",
      '- name: "esc\\"aped"',
      "- name: continued",
      "    on the next line",
      "- name: d",
    ]),
    "swc",
  );
  assert.deepEqual(names(child(root, "ports").children), ["a", "[1]", "[2]", "[3]", "d"]);
});

test("an [i] index counts a dropped `- - x` item at the dash column too", () => {
  const [root] = outlineOf(
    shipped(),
    doc(["name: C", "ports:", "- name: a", "- - x", "- direction: provide", "- name: d"]),
    "swc",
  );
  assert.deepEqual(names(child(root, "ports").children), ["a", "[2]", "d"]);
});

test("a duplicated collection key yields no group, and a flow collection none", () => {
  const duplicated = outlineOf(
    shipped(),
    doc(["name: C", "ports:", "- name: a", "ports:", "- name: b", "runnables:", "- name: R"]),
    "swc",
  );
  assert.deepEqual(names(duplicated[0].children), ["runnables"]);
  const flow = outlineOf(
    shipped(),
    doc(["name: C", "ports: []", "runnables:", "- name: R"]),
    "swc",
  );
  assert.deepEqual(names(flow[0].children), ["runnables"]);
  const empty = outlineOf(shipped(), doc(["name: C", "ports:", "runnables:", "- name: R"]), "swc");
  assert.deepEqual(names(empty[0].children), ["runnables"]);
});

test("a file with two identity lines has no root: the groups are listed at top level", () => {
  const outline = outlineOf(
    shipped(),
    doc(["name: C", "name: D", "ports:", "- name: a", "runnables:", "- name: R"]),
    "swc",
  );
  assert.deepEqual(names(outline), ["ports", "runnables"]);
  assertNested(outline);
});

test("the root's kind follows the top-level kind where the table has one", () => {
  const refs = shipped();
  assert.equal(
    outlineOf(refs, doc(["name: E", "kind: enum"]), "application_types")[0].kind,
    "Enum",
  );
  assert.equal(
    outlineOf(refs, doc(["name: R", "kind: record"]), "application_types")[0].kind,
    "Struct",
  );
  assert.equal(
    outlineOf(refs, doc(["name: V", "kind: application_type"]), "application_types")[0].kind,
    "TypeParameter",
  );
  assert.equal(
    outlineOf(refs, doc(["name: L", "kind: linear"]), "compu_methods")[0].kind,
    "Operator",
  );
  // No kind line: the layer's default, and no group (the collections are per kind).
  const noKind = outlineOf(refs, doc(["name: E", "literals:", "- name: A"]), "application_types");
  assert.equal(noKind[0].kind, "TypeParameter");
  assert.deepEqual(noKind[0].children, []);
});

test("every shipped layer has a root kind of its own, and an unknown layer answers nothing", () => {
  const refs = shipped();
  for (const layer of refs.layers.keys()) {
    if (layer === "hardware") {
      continue; // the table's own kind for it is the fallback's, `Object`
    }
    assert.notEqual(rootKind(layer, undefined), "Object", `${layer} falls back`);
  }
  assert.deepEqual(outlineOf(refs, doc(["name: X"]), "no_such_layer"), []);
});

test("every kind the tables name is a vscode.SymbolKind member, keyed by a kind the artifact declares", () => {
  // The adapter looks a name up in `vscode.SymbolKind` and falls back silently, so
  // a misspelt name would only degrade an icon: the enum is read from the API's
  // own declaration file, as text (this file cannot import `vscode`).
  const api = readSource("node_modules/@types/vscode/index.d.ts");
  const body = /export enum SymbolKind \{([\s\S]*?)\n\t\}/.exec(api)?.[1];
  assert.ok(body, "index.d.ts declares no SymbolKind enum");
  const members = new Set([...body.matchAll(/^\t\t([A-Za-z]+) = \d+,?$/gm)].map((m) => m[1]));
  assert.ok(members.has("EnumMember") && members.has("TypeParameter"), [...members].join(", "));
  const named = [
    ...SYMBOL_KIND_TABLES.rootByLayer.values(),
    ...SYMBOL_KIND_TABLES.rootByTopKind.values(),
    ...SYMBOL_KIND_TABLES.byMemberKind.values(),
    ...SYMBOL_KIND_TABLES.constants,
  ];
  for (const name of named) {
    assert.ok(members.has(name), `'${name}' is no vscode.SymbolKind member`);
  }
  // The keys: each a layer, a top-level kind, or a member kind of the shipped artifact.
  const refs = shipped();
  for (const layer of SYMBOL_KIND_TABLES.rootByLayer.keys()) {
    assert.ok(refs.layers.has(layer), `'${layer}' is no layer`);
  }
  const topKinds = new Set(
    [...refs.layers.values()].flatMap((facts) => facts.topLevelKinds.map((entry) => entry.kind)),
  );
  for (const kind of SYMBOL_KIND_TABLES.rootByTopKind.keys()) {
    assert.ok(topKinds.has(kind), `'${kind}' is no top-level kind`);
  }
  const memberKinds = new Set(refs.collections.flatMap((collection) => collection.memberKinds));
  for (const kind of SYMBOL_KIND_TABLES.byMemberKind.keys()) {
    assert.ok(memberKinds.has(kind), `'${kind}' is no collection's member kind`);
  }
});

test("the outline reads a CRLF document and a trailing blank and comment are not a member's", () => {
  const text = ["name: C", "ports:", "- name: a", "", "# note", "runnables:", "- name: R", ""].join(
    "\r\n",
  );
  const [root] = outlineOf(shipped(), text, "swc");
  const a = child(child(root, "ports"), "a");
  assert.equal(a.range.endLine, 2);
  assert.deepEqual(names(child(root, "runnables").children), ["R"]);
  assertNested([root]);
});

// --- Go to Symbol in Workspace -------------------------------------------------

/** A case-sensitive fake file system over *files*, counting the texts read. */
function fakeReader(files: Record<string, string>): Reader & { reads: string[] } {
  const reads: string[] = [];
  return {
    reads,
    readText: (filePath) => {
      reads.push(filePath);
      return files[filePath];
    },
    listDir: (directory) => {
      const prefix = `${directory}${path.sep}`;
      const entries = new Set<string>();
      for (const candidate of Object.keys(files)) {
        if (candidate.startsWith(prefix)) {
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

const WORKSPACE = {
  [at("swc", "Plain.yaml")]: doc(["name: Plain"]),
  [at("swc", "application", "BrakeController.yaml")]: doc(["name: BrakeController"]),
  [at("swc", "application", "deep", "Door.yaml")]: doc(["name: Door"]),
  [at("interfaces", "IBrake.yaml")]: doc(["name: IBrake"]),
  [at("interfaces", "ibrake~1.yaml")]: doc(["name: IBRAKE"]),
  [at("interfaces", "Odd~2.yaml")]: doc(["name: A", "name: B"]),
  [at("interfaces", "README.md")]: "notes\n",
  [at("interfaces", "IBrake.YAML")]: doc(["name: IBrake"]),
  [at("interfaces", "sub", "Hidden.yaml")]: doc(["name: Hidden"]),
  [at("data_types", "implementation", "uint8.yaml")]: doc(["name: uint8", "kind: value_type"]),
};

test("every layer's files by stem, the storage directory beside them, the layer's kind", () => {
  const reader = fakeReader(WORKSPACE);
  const found = workspaceSymbols(shipped(), MODEL_ROOT, reader, "");
  const rows = found.map((symbol) => [symbol.name, symbol.container, symbol.kind, symbol.layer]);
  assert.deepEqual(
    rows.sort((left, right) => (left.join("|") < right.join("|") ? -1 : 1)),
    [
      ["BrakeController", "swc/application", "Class", "swc"],
      ["Door", "swc/application/deep", "Class", "swc"],
      ["IBRAKE", "interfaces", "Interface", "interfaces"],
      ["IBrake", "interfaces", "Interface", "interfaces"],
      ["Odd~2", "interfaces", "Interface", "interfaces"],
      ["Plain", "swc", "Class", "swc"],
      ["uint8", "data_types/implementation", "TypeParameter", "implementation_types"],
    ],
  );
  // Only the two `~N` files were read: the scan is file names.
  assert.deepEqual(reader.reads.sort(), [
    at("interfaces", "Odd~2.yaml"),
    at("interfaces", "ibrake~1.yaml"),
  ]);
  const file = found.find((symbol) => symbol.name === "Door")?.file;
  assert.equal(file, at("swc", "application", "deep", "Door.yaml"));
});

test("the query is a case-insensitive subsequence of the name", () => {
  assert.equal(matchesQuery("bctl", "BrakeController"), true);
  assert.equal(matchesQuery("BRAKE", "BrakeController"), true);
  assert.equal(matchesQuery("", "x"), true);
  assert.equal(matchesQuery("lb", "BrakeController"), false);
  const found = workspaceSymbols(shipped(), MODEL_ROOT, fakeReader(WORKSPACE), "ibk");
  assert.deepEqual(found.map((symbol) => symbol.name).sort(), ["IBRAKE", "IBrake"]);
});

test("a layer directory that cannot be listed is skipped silently", () => {
  const found = workspaceSymbols(shipped(), MODEL_ROOT, fakeReader({}), "");
  assert.deepEqual(found, []);
});

test("resolveSymbolLine opens at the identity line, else at line 0", () => {
  const refs = shipped();
  const facts = refs.layers.get("interfaces");
  assert.ok(facts);
  const reader = fakeReader({
    [at("interfaces", "A.yaml")]: doc(["# header", "kind: sender_receiver", "name: A"]),
    [at("interfaces", "B.yaml")]: doc(["name: B", "name: C"]),
  });
  assert.equal(resolveSymbolLine(facts, at("interfaces", "A.yaml"), reader), 2);
  assert.equal(resolveSymbolLine(facts, at("interfaces", "B.yaml"), reader), 0);
  assert.equal(resolveSymbolLine(facts, at("interfaces", "Gone.yaml"), reader), 0);
});

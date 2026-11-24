// The Impact view's pure logic (impactCore.ts): the tree drawn from recorded
// walk documents, and the question the command asks, read on `examples/basic`'s
// model files as they are on disk.
//
// Every file under `src/fixtures/impact/` is the stdout of `swcraft impact
// --format json -- <name>` with no `--depth` (the view's whole walk) on
// `examples/basic`, or shaped from such a run (`two-roots.json`,
// `unknown-token.json`). `packages/tools/tests/test_impact_panel_fixtures.py` holds each
// one equal to a fresh run of the CLI, so the drift check is on the Python
// side, where the CLI is. Re-record them all, after reading the difference, with
//
//   .venv/bin/python packages/tools/tests/test_impact_panel_fixtures.py
//
// — never to make a red run green.
import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as path from "node:path";
import { test } from "node:test";

import {
  IMPACT_DEPENDENT_CONTEXT,
  IMPACT_EXPAND_AT_MOST,
  IMPACT_LAYER_CONTEXT,
  IMPACT_MESSAGE_CONTEXT,
  IMPACT_NON_SPAWNING_COMMANDS,
  IMPACT_ROOT_CONTEXT,
  IMPACT_VIEW_COMMANDS,
  type ImpactAnswer,
  type ImpactEditor,
  type ImpactNode,
  type ImpactQuestionContext,
  impactAfterFolderChange,
  impactAfterRun,
  impactFileNotFoundMessage,
  impactLayerLabel,
  impactNodeOf,
  impactNodes,
  impactQuestion,
  impactRunLine,
  NO_DEPENDENTS_ROW,
  NO_ELEMENT_FILE_MESSAGE,
  NO_IDENTITY_MESSAGE,
  noLayerDeclaresRow,
  notFoundMessage,
  readImpactDocument,
} from "./impactCore";
import {
  DIFFERENT_RELEASES,
  definitionTarget,
  type ImpactDocument,
  loadReferences,
  parseImpactDocument,
  type Reader,
  type References,
} from "./navigationCore";
import { FAILED_DESCRIPTION, FAILED_ROW, OUT_OF_DATE_DESCRIPTION } from "./resourceCore";
import { BASIC_MODEL, diskReader, impactFixture, PROJECT_ROOT, SCHEMAS_DIR } from "./testSupport";

function shipped(): References {
  return loadReferences(fs.readFileSync(path.join(SCHEMAS_DIR, "references.json"), "utf8"));
}

function fixture(name: string): ImpactDocument {
  return parseImpactDocument(impactFixture(name));
}

const ANSWERED = { outOfDate: false, failed: false };

/** A node as the eye reads it: label, description, collapsed state, children. */
type Shape = [string, string, ImpactNode["collapsible"], ...Shape[]];

function shape(node: ImpactNode): Shape {
  return [node.label, node.description, node.collapsible, ...node.children.map(shape)];
}

/** Every node of *nodes*, depth first. */
function allNodes(nodes: readonly ImpactNode[]): ImpactNode[] {
  return nodes.flatMap((node) => [node, ...allNodes(node.children)]);
}

// --- The tree ----------------------------------------------------------------------

test("the IExample tree exactly as the specification draws it", () => {
  const tree = impactNodes(fixture("IExample.json"), shipped(), ANSWERED);
  assert.equal(tree.description, "");
  assert.deepEqual(tree.nodes.map(shape), [
    [
      "IExample",
      "interfaces · 6 dependents",
      "expanded",
      [
        "swc",
        "2",
        "expanded",
        ["ExampleConsumerSWC", "via port rIn", "none"],
        ["ExampleSWC", "via port pOut", "none"],
      ],
      [
        "connectors",
        "1",
        "expanded",
        ["Example_to_Consumer", "via ports ExampleSWC.pOut, ExampleConsumerSWC.rIn", "none"],
      ],
      ["compositions", "1", "expanded", ["Root", "members ExampleSWC, ExampleConsumerSWC", "none"]],
      ["tasks", "1", "expanded", ["Task_10ms", "runnable ExampleConsumerRun", "none"]],
      [
        "sequences",
        "1",
        "expanded",
        ["task10ms_consumer_cycle", "step: ExampleConsumerRun", "none"],
      ],
    ],
  ]);
});

test("the IExample tree's ids carry the question, and its context values and elements", () => {
  const [root] = impactNodes(fixture("IExample.json"), shipped(), ANSWERED).nodes;
  assert.equal(root.id, "impact:IExample:interface");
  assert.equal(root.contextValue, IMPACT_ROOT_CONTEXT);
  assert.deepEqual(root.element, { token: "interface", name: "IExample" });
  assert.equal(root.tooltip, "interfaces 'IExample'");
  assert.equal(root.rootName, "IExample", "the asked name a click places by");
  const swc = root.children[0];
  assert.equal(swc.id, "impact:IExample:interface:layer:swc");
  assert.equal(swc.contextValue, IMPACT_LAYER_CONTEXT);
  assert.equal(swc.element, undefined, "a layer node has no click");
  assert.equal(swc.rootName, undefined);
  const consumer = swc.children[0];
  assert.equal(consumer.id, "impact:IExample:interface:row:swc:ExampleConsumerSWC");
  assert.equal(consumer.contextValue, IMPACT_DEPENDENT_CONTEXT);
  assert.deepEqual(consumer.element, { token: "swc", name: "ExampleConsumerSWC" });
  assert.equal(consumer.rootLayer, "interfaces");
  assert.equal(consumer.rootName, "IExample");
  assert.equal(consumer.tooltip, "swc 'ExampleConsumerSWC'\nvia port rIn");
  assert.deepEqual(consumer.children, [], "a row has no children: the walk is transitive");
  const ids = allNodes([root]).map((node) => node.id);
  assert.equal(new Set(ids).size, ids.length, "every id is unique");
  // A new question gets new ids, so VS Code carries no expansion over.
  const other = impactNodes(fixture("REQ_EXAMPLE_001.json"), shipped(), ANSWERED).nodes;
  assert.ok(allNodes(other).every((node) => !ids.includes(node.id)));
});

test("a row with no detail shows none, and its tooltip is the layer and the name", () => {
  const [root] = impactNodes(fixture("REQ_EXAMPLE_001.json"), shipped(), ANSWERED).nodes;
  assert.equal(root.description, "requirements · 6 dependents");
  const swcRow = root.children[0].children[1];
  assert.equal(swcRow.label, "ExampleSWC");
  assert.equal(swcRow.description, "");
  assert.equal(swcRow.tooltip, "swc 'ExampleSWC'");
  assert.equal(swcRow.rootLayer, "requirements");
  assert.equal(swcRow.rootName, "REQ_EXAMPLE_001");
});

test("the layer nodes follow references.json's order, not the walk's", () => {
  const doc = parseImpactDocument(
    JSON.stringify({
      schemaVersion: 3,
      name: "X",
      matched: {
        interface: [
          { layer: "sequence", name: "S", detail: "" },
          { layer: "swc", name: "A", detail: "" },
          { layer: "task", name: "T", detail: "" },
          { layer: "swc", name: "B", detail: "" },
        ],
      },
    }),
  );
  const [root] = impactNodes(doc, shipped(), ANSWERED).nodes;
  assert.deepEqual(
    root.children.map((node) => node.label),
    ["swc", "tasks", "sequences"],
  );
  assert.deepEqual(
    root.children[0].children.map((node) => node.label),
    ["A", "B"],
    "rows keep the walk's order within a layer",
  );
});

test("a name two layers declare: two roots in the document's order, each with its own layers", () => {
  const tree = impactNodes(fixture("two-roots.json"), shipped(), ANSWERED);
  assert.deepEqual(
    tree.nodes.map((node) => [node.label, node.description, node.id]),
    [
      ["IExample", "interfaces · 6 dependents", "impact:IExample:interface"],
      ["IExample", "swc · 1 dependent", "impact:IExample:swc"],
    ],
  );
  assert.deepEqual(shape(tree.nodes[1]), [
    "IExample",
    "swc · 1 dependent",
    "expanded",
    ["compositions", "1", "expanded", ["Root", "members ExampleSWC, ExampleConsumerSWC", "none"]],
  ]);
});

function swcRows(count: number): ImpactDocument {
  return parseImpactDocument(
    JSON.stringify({
      schemaVersion: 3,
      name: "IHub",
      matched: {
        interface: Array.from({ length: count }, (_, index) => ({
          layer: "swc",
          name: `C${index}`,
          detail: `via port p${index}`,
        })),
      },
    }),
  );
}

test("a layer of 20 rows is expanded and of 21 collapsed, its count kept", () => {
  assert.equal(IMPACT_EXPAND_AT_MOST, 20);
  const twenty = impactNodes(swcRows(20), shipped(), ANSWERED).nodes[0].children[0];
  assert.deepEqual([twenty.description, twenty.collapsible], ["20", "expanded"]);
  const many = impactNodes(swcRows(21), shipped(), ANSWERED).nodes[0];
  assert.equal(many.description, "interfaces · 21 dependents");
  assert.equal(many.collapsible, "expanded", "the root stays expanded");
  assert.deepEqual(
    [many.children[0].description, many.children[0].collapsible],
    ["21", "collapsed"],
  );
  assert.equal(many.children[0].children.length, 21);
});

test("an empty matched: one message row naming the name, so the tree is never empty", () => {
  const tree = impactNodes(fixture("NoSuchElement.json"), shipped(), ANSWERED);
  assert.deepEqual(tree.nodes.map(shape), [["No layer declares 'NoSuchElement'.", "", "none"]]);
  assert.equal(tree.nodes[0].contextValue, IMPACT_MESSAGE_CONTEXT);
  assert.equal(noLayerDeclaresRow("X"), "No layer declares 'X'.");
  // An empty answer is an answer: it goes out of date like any other.
  assert.equal(
    impactNodes(fixture("NoSuchElement.json"), shipped(), { outOfDate: true, failed: false })
      .description,
    OUT_OF_DATE_DESCRIPTION,
  );
});

test("no dependents: the root says so, and one message row under it", () => {
  const tree = impactNodes(fixture("Root.json"), shipped(), ANSWERED);
  assert.deepEqual(tree.nodes.map(shape), [
    ["Root", "compositions · no dependents", "expanded", [NO_DEPENDENTS_ROW, "", "none"]],
  ]);
  assert.equal(NO_DEPENDENTS_ROW, "Nothing depends on it.");
  assert.equal(tree.nodes[0].children[0].contextValue, IMPACT_MESSAGE_CONTEXT);
  assert.deepEqual(tree.nodes[0].element, { token: "composition", name: "Root" });
});

test("an unknown token: its rows under a node labelled with the token, last; never dropped", () => {
  const tree = impactNodes(fixture("unknown-token.json"), shipped(), ANSWERED);
  const [interfaceRoot, futureRoot] = tree.nodes;
  assert.deepEqual(
    interfaceRoot.children.map((node) => [node.label, node.description]),
    [
      ["swc", "2"],
      ["connectors", "1"],
      ["compositions", "1"],
      ["tasks", "1"],
      ["sequences", "1"],
      ["future_layer", "1"],
    ],
  );
  const future = interfaceRoot.children[5].children[0];
  assert.equal(future.label, "FutureElement");
  assert.deepEqual(future.element, { token: "future_layer", name: "FutureElement" });
  assert.equal(future.tooltip, "future_layer 'FutureElement'\nvia port pOut");
  // A matched token no layer carries: the token in the description, and no click.
  assert.equal(futureRoot.description, "future_layer · 1 dependent");
  assert.equal(futureRoot.element, undefined);
  assert.equal(futureRoot.children[0].label, "swc");
  assert.equal(futureRoot.children[0].children[0].rootLayer, undefined);
});

test("before any question: no nodes (the welcome) and no description", () => {
  assert.deepEqual(impactNodes(undefined, shipped(), ANSWERED), { description: "", nodes: [] });
});

test("a failed run: the last tree kept with the failure's description, or one message row", () => {
  const kept = impactNodes(fixture("IExample.json"), shipped(), { outOfDate: false, failed: true });
  assert.equal(kept.description, FAILED_DESCRIPTION);
  assert.equal(kept.nodes[0].label, "IExample");
  const none = impactNodes(undefined, shipped(), { outOfDate: false, failed: true });
  assert.equal(none.description, FAILED_DESCRIPTION);
  assert.deepEqual(none.nodes.map(shape), [[FAILED_ROW, "", "none"]]);
  assert.equal(none.nodes[0].contextValue, IMPACT_MESSAGE_CONTEXT);
});

test("the description's precedence: failed, then out of date, then empty", () => {
  const doc = fixture("IExample.json");
  const description = (outOfDate: boolean, failed: boolean) =>
    impactNodes(doc, shipped(), { outOfDate, failed }).description;
  assert.equal(description(true, true), FAILED_DESCRIPTION);
  assert.equal(description(true, false), OUT_OF_DATE_DESCRIPTION);
  assert.equal(OUT_OF_DATE_DESCRIPTION, "out of date — Refresh");
  assert.equal(description(false, false), "");
});

test("the asked name comes from the document, else from the question", () => {
  const doc: ImpactDocument = { matched: new Map([["interface", []]]) };
  const tree = impactNodes(doc, shipped(), { question: "IAsked", ...ANSWERED });
  assert.equal(tree.nodes[0].label, "IAsked");
  assert.equal(tree.nodes[0].id, "impact:IAsked:interface");
});

test("a name repeated in one layer group still gets an id of its own: the ~N suffix", () => {
  const doc: ImpactDocument = {
    name: "IExample",
    matched: new Map([
      [
        "interface",
        [
          { layer: "swc", name: "A" },
          { layer: "swc", name: "A" },
          { layer: "swc", name: "B" },
        ],
      ],
    ]),
  };
  const [root] = impactNodes(doc, shipped(), ANSWERED).nodes;
  assert.deepEqual(
    root.children[0].children.map((node) => node.id),
    [
      "impact:IExample:interface:row:swc:A",
      "impact:IExample:interface:row:swc:A~2",
      "impact:IExample:interface:row:swc:B",
    ],
  );
  const ids = allNodes([root]).map((node) => node.id);
  assert.equal(new Set(ids).size, ids.length, "every id is unique");
});

// --- The view's state across runs and folder changes ---------------------------------

const FOLDER_A = "/work/a";
const FOLDER_B = "/work/b";

/** A finished run in *root*: *read* is an exit 0's document or refusal, `undefined` another exit. */
function finished(
  name: string,
  read: ImpactDocument | { error: string } | undefined,
  startedAt = 100,
  root = FOLDER_A,
) {
  return { root, name, refs: shipped(), startedAt, read };
}

test("impactAfterRun: a read document replaces the answer, stamped with the run's start", () => {
  const doc = fixture("IExample.json");
  const state = impactAfterRun<ImpactAnswer>({ failed: true }, finished("IExample", doc, 7));
  assert.equal(state.document, doc);
  assert.equal(state.question, "IExample");
  assert.equal(state.readAt, 7);
  assert.equal(state.root, FOLDER_A);
  assert.equal(state.failed, false);
  assert.ok(state.refs !== undefined);
});

test("impactAfterRun: a non-zero exit with no tree yet holds the failed name, so Refresh retries it", () => {
  const state = impactAfterRun<ImpactAnswer>({ failed: false }, finished("IAsked", undefined));
  assert.equal(state.document, undefined);
  assert.equal(state.question, "IAsked");
  assert.equal(state.readAt, undefined, "a failure takes no stamp");
  assert.equal(state.root, FOLDER_A, "the failed question belongs to its folder");
  assert.equal(state.failed, true);
  const tree = impactNodes(state.document, shipped(), { ...state, outOfDate: false });
  assert.deepEqual(tree.nodes.map(shape), [[FAILED_ROW, "", "none"]]);
});

test("impactAfterRun: a refused document keeps the last good tree, its question and its stamp", () => {
  const doc = fixture("IExample.json");
  const shown = impactAfterRun<ImpactAnswer>({ failed: false }, finished("IExample", doc, 7));
  const after = impactAfterRun(shown, finished("REQ_EXAMPLE_001", { error: "not JSON" }, 9));
  assert.equal(after.document, doc);
  assert.equal(after.question, "IExample", "Refresh asks the shown question");
  assert.equal(after.readAt, 7);
  assert.equal(after.failed, true);
  const tree = impactNodes(after.document, shipped(), { ...after, outOfDate: false });
  assert.equal(tree.description, FAILED_DESCRIPTION);
  assert.equal(tree.nodes[0].label, "IExample");
  // A non-zero exit after a shown answer keeps it the same way.
  const exited = impactAfterRun(shown, finished("REQ_EXAMPLE_001", undefined, 9));
  assert.equal(exited.document, doc);
  assert.equal(exited.question, "IExample");
  assert.equal(exited.failed, true);
});

test("impactAfterRun keeps what only commands.ts writes: the busy flag and the change stamp", () => {
  const state = impactAfterRun(
    { failed: false, busy: true, newestChangeAt: 50 },
    finished("IExample", fixture("IExample.json")),
  );
  assert.equal(state.busy, true);
  assert.equal(state.newestChangeAt, 50);
});

test("impactAfterFolderChange: a failed first run of another folder is dropped, the welcome back", () => {
  const failed = impactAfterRun<ImpactAnswer>({ failed: false }, finished("IAsked", undefined));
  const moved = impactAfterFolderChange(failed, FOLDER_B);
  assert.equal(moved.question, undefined, "Refresh no longer asks project A's name in B");
  assert.equal(moved.failed, false);
  assert.equal(moved.root, undefined);
  assert.deepEqual(impactNodes(moved.document, shipped(), { ...moved, outOfDate: false }), {
    description: "",
    nodes: [],
  });
});

test("impactAfterFolderChange: an answer of another folder is dropped with its stamp", () => {
  const shown = impactAfterRun<ImpactAnswer>(
    { failed: false },
    finished("IExample", fixture("IExample.json"), 7),
  );
  const moved = impactAfterFolderChange(shown, undefined);
  assert.equal(moved.document, undefined);
  assert.equal(moved.question, undefined);
  assert.equal(moved.readAt, undefined);
  assert.equal(moved.root, undefined);
});

test("impactAfterFolderChange: the folder still open first, or nothing held, is the state itself", () => {
  const shown = impactAfterRun<ImpactAnswer>(
    { failed: false },
    finished("IExample", fixture("IExample.json")),
  );
  assert.equal(impactAfterFolderChange(shown, FOLDER_A), shown);
  const empty: ImpactAnswer = { failed: false };
  assert.equal(impactAfterFolderChange(empty, FOLDER_B), empty);
});

test("impactNodeOf: the view's own nodes only", () => {
  const [root] = impactNodes(fixture("IExample.json"), shipped(), ANSWERED).nodes;
  assert.equal(impactNodeOf(root), root);
  for (const other of [undefined, null, "IExample", { id: "resource:x", contextValue: "x" }, {}]) {
    assert.equal(impactNodeOf(other), undefined);
  }
});

// --- The question ------------------------------------------------------------------

/** The disk, with *buffers* standing in for files at those paths (saved texts of hand-shaped files). */
function readerWith(buffers: Record<string, string>): Reader {
  const saved = new Map(Object.entries(buffers));
  return {
    readText: (filePath) => saved.get(filePath) ?? diskReader.readText(filePath),
    listDir: diskReader.listDir,
  };
}

const EXAMPLE_SWC = path.join(BASIC_MODEL, "swc", "example", "ExampleSWC.yaml");
const CONNECTOR = path.join(BASIC_MODEL, "connectors", "Example_to_Consumer.yaml");

/** The active editor on *file* of `examples/basic` as saved, the cursor on the line that is *line*. */
function editorOn(file: string, line: string, isDirty = false, text?: string): ImpactEditor {
  const buffer = text ?? fs.readFileSync(file, "utf8");
  const index = buffer.split(/\r?\n/).indexOf(line);
  assert.notEqual(index, -1, `${file} holds no line '${line}'`);
  return { file, text: buffer, line: index, isDirty };
}

function context(
  editor: ImpactEditor | undefined,
  reader: Reader = diskReader,
): ImpactQuestionContext {
  return { refs: shipped(), modelRoot: BASIC_MODEL, reader, editor };
}

test("the question from a string: the lens's name; an empty one reads the editor", () => {
  assert.deepEqual(impactQuestion("IExample", context(undefined)), { name: "IExample" });
  assert.deepEqual(impactQuestion("", context(undefined)), { message: NO_ELEMENT_FILE_MESSAGE });
  assert.deepEqual(impactQuestion("", context(editorOn(EXAMPLE_SWC, "name: ExampleSWC"))), {
    name: "ExampleSWC",
  });
});

test("the lens's name beside its document: save first when the saved file declares another", () => {
  const lensUri = { scheme: "file", fsPath: EXAMPLE_SWC };
  // The saved file declares the lens's name: asked, whatever the active editor.
  assert.deepEqual(impactQuestion("ExampleSWC", context(undefined), lensUri), {
    name: "ExampleSWC",
  });
  // An unsaved rename drew the lens with the live buffer's name: save first, no run.
  assert.deepEqual(impactQuestion("Renamed", context(undefined), lensUri), {
    message:
      "SWCraft: Impact: save the file first — the CLI reads the saved model, " +
      "and the saved file does not declare 'Renamed'.",
  });
  // No Uri, a Uri that is no file, or a file in no layer: the name as passed.
  const manifest = path.join(BASIC_MODEL, "..", "swcraft.yaml");
  for (const other of [
    undefined,
    { scheme: "untitled", fsPath: "Untitled-1" },
    { scheme: "file", fsPath: manifest },
  ]) {
    assert.deepEqual(impactQuestion("Renamed", context(undefined), other), { name: "Renamed" });
  }
});

test("the question from a dependent row: the row's name (the re-root)", () => {
  const [root] = impactNodes(fixture("IExample.json"), shipped(), ANSWERED).nodes;
  const row = root.children[1].children[0];
  assert.deepEqual(impactQuestion(row, context(undefined)), { name: "Example_to_Consumer" });
  // A root or a layer node is no dependent: the command reads the editor instead.
  assert.deepEqual(impactQuestion(root, context(undefined)), { message: NO_ELEMENT_FILE_MESSAGE });
});

test("ExampleSWC.yaml: the interface: line asks IExample; name: and the description ask ExampleSWC", () => {
  assert.deepEqual(
    impactQuestion(undefined, context(editorOn(EXAMPLE_SWC, "    interface: IExample"))),
    { name: "IExample" },
  );
  assert.deepEqual(impactQuestion(undefined, context(editorOn(EXAMPLE_SWC, "name: ExampleSWC"))), {
    name: "ExampleSWC",
  });
  assert.deepEqual(
    impactQuestion(
      undefined,
      context(
        editorOn(
          EXAMPLE_SWC,
          "description: Example software component for testing and demonstration",
        ),
      ),
    ),
    { name: "ExampleSWC" },
  );
  assert.deepEqual(impactQuestion(undefined, context(editorOn(EXAMPLE_SWC, ""))), {
    name: "ExampleSWC",
  });
});

test("ExampleSWC.yaml: a satisfies: member asks the requirement (the trace link's id)", () => {
  assert.deepEqual(
    impactQuestion(undefined, context(editorOn(EXAMPLE_SWC, "- REQ_EXAMPLE_001@1"))),
    { name: "REQ_EXAMPLE_001" },
  );
});

test("ExampleSWC.yaml: a map-keys relation (RAM0: under memory:) asks the hardware element", () => {
  assert.deepEqual(impactQuestion(undefined, context(editorOn(EXAMPLE_SWC, "  RAM0: 2048"))), {
    name: "RAM0",
  });
});

test("Example_to_Consumer.yaml: from: swc: asks ExampleSWC; port: asks the connector itself", () => {
  assert.deepEqual(impactQuestion(undefined, context(editorOn(CONNECTOR, "  swc: ExampleSWC"))), {
    name: "ExampleSWC",
  });
  assert.deepEqual(impactQuestion(undefined, context(editorOn(CONNECTOR, "  port: pOut"))), {
    name: "Example_to_Consumer",
  });
});

/** A hand-shaped component beside `examples/basic`'s, reading IExample's data element. */
const HAND_SWC = path.join(BASIC_MODEL, "swc", "example", "HandSWC.yaml");
const HAND_TEXT = [
  "name: HandSWC",
  "type: application",
  "ports:",
  "  - name: rIn",
  "    direction: require",
  "    interface: IExample",
  "runnables:",
  "  - name: HandRun",
  "    reads:",
  "      - port: rIn",
  "        element: value",
  "",
].join("\n");

test("a runnable's reads entry: element: asks the interface holding the data element", () => {
  const reader = readerWith({ [HAND_SWC]: HAND_TEXT });
  assert.deepEqual(
    impactQuestion(
      undefined,
      context(editorOn(HAND_SWC, "        element: value", false, HAND_TEXT), reader),
    ),
    { name: "IExample" },
  );
});

test("a runnable's reads entry: port: names the file's own port, so the file's own element", () => {
  const reader = readerWith({ [HAND_SWC]: HAND_TEXT });
  assert.deepEqual(
    impactQuestion(
      undefined,
      context(editorOn(HAND_SWC, "      - port: rIn", false, HAND_TEXT), reader),
    ),
    { name: "HandSWC" },
  );
});

test("a reference that does not resolve: the message with the classifying value, and no name", () => {
  const text = HAND_TEXT.replace("interface: IExample", "interface: INoSuch");
  const reader = readerWith({ [HAND_SWC]: text });
  assert.deepEqual(
    impactQuestion(
      undefined,
      context(editorOn(HAND_SWC, "    interface: INoSuch", false, text), reader),
    ),
    { message: notFoundMessage("INoSuch") },
  );
  assert.equal(
    notFoundMessage("INoSuch"),
    "SWCraft: Impact: 'INoSuch' under the cursor is not found in the model, so there is nothing to ask.",
  );
  // A relation's value is its item as written, the version kept.
  const linked = `${HAND_TEXT}satisfies:\n- REQ_NOPE@2\n`;
  assert.deepEqual(
    impactQuestion(
      undefined,
      context(
        editorOn(HAND_SWC, "- REQ_NOPE@2", false, linked),
        readerWith({ [HAND_SWC]: linked }),
      ),
    ),
    { message: notFoundMessage("REQ_NOPE@2") },
  );
  // A member reference naming no member of the port's interface.
  const member = HAND_TEXT.replace("element: value", "element: nothing");
  assert.deepEqual(
    impactQuestion(
      undefined,
      context(
        editorOn(HAND_SWC, "        element: nothing", false, member),
        readerWith({ [HAND_SWC]: member }),
      ),
    ),
    { message: notFoundMessage("nothing") },
  );
});

test("a target found whose identity is then not read: the not-found message, and no name", () => {
  // definitionTarget confirms the target's identity itself, so the identity read
  // after it misses only when the file changed in between: here the reader gives
  // the saved text on the first read of IExample.yaml and another text after.
  const target = path.join(BASIC_MODEL, "interfaces", "IExample.yaml");
  const saved = fs.readFileSync(target, "utf8");
  for (const later of [`name: Other\n${saved}`, saved.replace("name: IExample\n", "")]) {
    let reads = 0;
    const reader: Reader = {
      readText: (filePath) => {
        if (filePath !== target) {
          return diskReader.readText(filePath);
        }
        reads += 1;
        return reads === 1 ? saved : later;
      },
      listDir: diskReader.listDir,
    };
    const editor = editorOn(EXAMPLE_SWC, "    interface: IExample");
    assert.deepEqual(impactQuestion(undefined, context(editor, reader)), {
      message: notFoundMessage("IExample"),
    });
    assert.equal(reads, 2, "found by definitionTarget's read, then missed by the identity's");
  }
  // The same file read the same way both times is asked.
  const editor = editorOn(EXAMPLE_SWC, "    interface: IExample");
  assert.ok(
    definitionTarget(
      shipped(),
      editor.text,
      "swc",
      editor.line,
      editor.file,
      BASIC_MODEL,
      diskReader,
    ),
  );
  assert.deepEqual(impactQuestion(undefined, context(editor)), { name: "IExample" });
});

test("an edge with no readable value counts as none: the file's own element", () => {
  const text = HAND_TEXT.replace("interface: IExample", "interface:");
  assert.deepEqual(
    impactQuestion(
      undefined,
      context(editorOn(HAND_SWC, "    interface:", false, text), readerWith({ [HAND_SWC]: text })),
    ),
    { name: "HandSWC" },
  );
});

test("a file with no single identity line: the message, and no name", () => {
  for (const text of [HAND_TEXT.replace("name: HandSWC\n", ""), `name: A\n${HAND_TEXT}`]) {
    assert.deepEqual(
      impactQuestion(
        undefined,
        context(
          editorOn(HAND_SWC, "type: application", false, text),
          readerWith({ [HAND_SWC]: text }),
        ),
      ),
      { message: NO_IDENTITY_MESSAGE },
    );
  }
  assert.equal(
    NO_IDENTITY_MESSAGE,
    "SWCraft: Impact: this file declares no single element name, so there is nothing to ask.",
  );
});

test("a dirty buffer: save first when the saved file declares another identity", () => {
  const renamed = fs.readFileSync(EXAMPLE_SWC, "utf8").replace("name: ExampleSWC", "name: Renamed");
  assert.deepEqual(
    impactQuestion(undefined, context(editorOn(EXAMPLE_SWC, "name: Renamed", true, renamed))),
    {
      message:
        "SWCraft: Impact: save the file first — the CLI reads the saved model, " +
        "and the saved file does not declare 'Renamed'.",
    },
  );
  // The same identity on disk: asked.
  const edited = fs.readFileSync(EXAMPLE_SWC, "utf8").replace("type: application", "type: other");
  assert.deepEqual(
    impactQuestion(undefined, context(editorOn(EXAMPLE_SWC, "name: ExampleSWC", true, edited))),
    { name: "ExampleSWC" },
  );
  // A reference under the cursor names another, saved file: asked as it is on disk.
  assert.deepEqual(
    impactQuestion(
      undefined,
      context(editorOn(EXAMPLE_SWC, "    interface: IExample", true, renamed)),
    ),
    { name: "IExample" },
  );
});

test("no element file: no editor, the manifest, or a file in no layer", () => {
  assert.deepEqual(impactQuestion(undefined, context(undefined)), {
    message: NO_ELEMENT_FILE_MESSAGE,
  });
  const manifest = path.join(BASIC_MODEL, "..", "swcraft.yaml");
  assert.deepEqual(
    impactQuestion(undefined, context(editorOn(manifest, "name: swcraft-basic-example"))),
    { message: NO_ELEMENT_FILE_MESSAGE },
  );
  assert.deepEqual(
    impactQuestion(undefined, {
      ...context(editorOn(EXAMPLE_SWC, "name: ExampleSWC")),
      modelRoot: undefined,
    }),
    { message: NO_ELEMENT_FILE_MESSAGE },
  );
});

test("the editor menu's Uri: the active document's reads the cursor; another file asks its saved identity", () => {
  const editor = editorOn(EXAMPLE_SWC, "    interface: IExample");
  assert.deepEqual(
    impactQuestion({ scheme: "file", fsPath: EXAMPLE_SWC }, context(editor)),
    { name: "IExample" },
    "the active document's Uri: the cursor's line",
  );
  assert.deepEqual(
    impactQuestion({ scheme: "file", fsPath: CONNECTOR }, context(editor)),
    { name: "Example_to_Consumer" },
    "another file's Uri: its own saved identity, never the other editor's cursor",
  );
  assert.deepEqual(
    impactQuestion({ scheme: "file", fsPath: CONNECTOR }, context(undefined)),
    { name: "Example_to_Consumer" },
    "no active text editor: the menu's file still answers",
  );
  // The saved text is read, never the other editor's buffer.
  const saved = HAND_TEXT.replace("name: HandSWC", "name: SavedName");
  assert.deepEqual(
    impactQuestion(
      { scheme: "file", fsPath: HAND_SWC },
      context(editor, readerWith({ [HAND_SWC]: saved })),
    ),
    { name: "SavedName" },
  );
  const manifest = path.join(BASIC_MODEL, "..", "swcraft.yaml");
  assert.deepEqual(impactQuestion({ scheme: "file", fsPath: manifest }, context(editor)), {
    message: NO_ELEMENT_FILE_MESSAGE,
  });
  // A menu file in a layer that cannot be read declares no identity.
  assert.deepEqual(
    impactQuestion(
      { scheme: "file", fsPath: path.join(BASIC_MODEL, "swc", "example", "Unreadable.yaml") },
      context(editor),
    ),
    { message: NO_IDENTITY_MESSAGE },
  );
  // With no model root, no menu file is in a layer.
  assert.deepEqual(
    impactQuestion(
      { scheme: "file", fsPath: CONNECTOR },
      { ...context(editor), modelRoot: undefined },
    ),
    { message: NO_ELEMENT_FILE_MESSAGE },
  );
  // A Uri that is no file reads the active editor, as no argument does.
  assert.deepEqual(impactQuestion({ scheme: "untitled", fsPath: "Untitled-1" }, context(editor)), {
    name: "IExample",
  });
});

// --- The view's commands and its run -------------------------------------------------

test("the view's commands: swcraft.impact, its Refresh and a row's click; only the click spawns nothing", () => {
  assert.deepEqual(
    [...IMPACT_VIEW_COMMANDS],
    ["swcraft.impact", "swcraft.impactRefresh", "swcraft.impactOpen"],
  );
  assert.deepEqual([...IMPACT_NON_SPAWNING_COMMANDS], ["swcraft.impactOpen"]);
});

test("impactRunLine: the log's closing line, never the answer", () => {
  assert.equal(
    impactRunLine(0, fixture("IExample.json")),
    "SWCraft: the answer is shown in the Impact view (exit code 0).",
  );
  assert.equal(
    impactRunLine(0, { error: "swcraft impact --format json: not JSON: x" }),
    "SWCraft: swcraft impact --format json: not JSON: x",
  );
  assert.equal(
    impactRunLine(1, undefined),
    "SWCraft: swcraft impact ended with exit code 1; the Impact view keeps its last answer.",
  );
  assert.equal(
    impactRunLine(null, undefined),
    "SWCraft: swcraft impact ended with no exit code; the Impact view keeps its last answer.",
  );
});

test("readImpactDocument: the walk document, or the refusal as one line", () => {
  const text = fs.readFileSync(
    path.join(PROJECT_ROOT, "src", "fixtures", "impact", "IExample.json"),
    "utf8",
  );
  assert.deepEqual(readImpactDocument(text), fixture("IExample.json"));
  const other = readImpactDocument(JSON.stringify({ schemaVersion: 99, matched: {} }));
  assert.ok("error" in other);
  assert.match(other.error, /schemaVersion 99/);
  assert.match(other.error, new RegExp(DIFFERENT_RELEASES));
  const garbage = readImpactDocument("not json");
  assert.ok("error" in garbage);
  assert.match(garbage.error, /^swcraft impact --format json: not JSON/);
});

test("impactFileNotFoundMessage and impactLayerLabel: the contract layer name, else the token", () => {
  const refs = shipped();
  assert.equal(impactLayerLabel(refs, "interface"), "interfaces");
  assert.equal(impactLayerLabel(refs, "future_layer"), "future_layer");
  assert.equal(
    impactFileNotFoundMessage("swc", "ExampleSWC"),
    "SWCraft: Impact: the file of swc 'ExampleSWC' was not found.",
  );
});

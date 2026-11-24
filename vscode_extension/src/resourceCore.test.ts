// The Resources view's pure logic (resourceCore.ts), tested on recorded documents.
//
// Every file under `src/fixtures/resources/` is the stdout of
// `swcraft resource-report --format json`, verbatim: `basic.json` and `fit-core.json` over
// `examples/basic` (the second asked `--fit-utilization 0.05`), and `over-budget.json`,
// `unknown.json` and `fit-memory.json` over synthetic models `conftest`'s builders write (the
// last asked `--fit-bytes 12288`). `packages/tools/tests/test_resource_panel_fixtures.py`
// holds each equal to a fresh run of the CLI, so the drift check is on the Python side.
// Re-record them all, after reading the difference, with
//
//   .venv/bin/python packages/tools/tests/test_resource_panel_fixtures.py
//
// — never to make a red run green. The two `fit-*` documents are the input of the test that
// the view does not read a `fit` key.
import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as path from "node:path";
import { test } from "node:test";

import {
  FAILED_DESCRIPTION,
  FAILED_ROW,
  ICON_INFO,
  ICON_NO,
  ICON_WARNING,
  ICON_YES,
  isResourceRefusal,
  NO_RESOURCES_ROW,
  OTHER_RELEASE,
  OUT_OF_DATE_DESCRIPTION,
  parseResourceDocument,
  RESOURCE_ID_PREFIX,
  RESOURCE_NON_SPAWNING_COMMANDS,
  RESOURCE_VIEW_COMMANDS,
  type ResourceDocument,
  type ResourceNode,
  resourceLine,
  resourceNodeOf,
  resourceNodes,
  resourceNotFoundMessage,
  resourceRunLine,
  STATUS_BREACH,
  STATUS_ICONS,
  STATUS_OK,
  STATUS_UNKNOWN,
} from "./resourceCore";
import { PROJECT_ROOT } from "./testSupport";

function fixtureText(name: string): string {
  return fs.readFileSync(path.join(PROJECT_ROOT, "src", "fixtures", "resources", name), "utf8");
}

function fixture(name: string): ResourceDocument {
  const read = parseResourceDocument(fixtureText(name));
  if (isResourceRefusal(read)) {
    assert.fail(`${name} refused: ${read.error}`);
  }
  return read;
}

function section(nodes: readonly ResourceNode[], label: string): ResourceNode {
  const found = nodes.find(
    (node) => node.contextValue === "resource.section" && node.label.startsWith(label),
  );
  assert.ok(found, `no section ${label}`);
  return found;
}

function row(
  nodes: readonly ResourceNode[],
  kind: "Cores" | "Memories",
  name: string,
): ResourceNode {
  const found = section(nodes, kind).children.find((node) => node.label === name);
  assert.ok(found, `no row ${name}`);
  return found;
}

/** A parsed fixture before the reader checks it, to edit into a malformed one. */
type RawDocument = ReturnType<typeof JSON.parse>;

// --- parseResourceDocument --------------------------------------------------------

test("every recorded document is read", () => {
  for (const name of [
    "basic.json",
    "fit-core.json",
    "over-budget.json",
    "unknown.json",
    "fit-memory.json",
  ]) {
    fixture(name);
  }
});

test("the basic document is read field by field", () => {
  const document = fixture("basic.json");
  assert.deepEqual(document.topology, []);
  assert.deepEqual(document.findings, { errors: 0, informational: 0 });
  assert.deepEqual(
    document.cores.map((core) => [core.name, core.utilization, core.limit, core.physical_headroom]),
    [["CORE0", "12.00%", "70.00%", "88.00%"]],
  );
  const [memory] = document.memories;
  assert.equal(memory.size, 262144);
  assert.equal(memory.used, "3584");
  assert.equal(memory.status, STATUS_OK);
  assert.deepEqual(memory.consumers[0], {
    name: "ExampleSWC",
    layer: "swc",
    amount: "2048 bytes",
    declared: true,
    detail: "",
  });
});

test("the subjects of a finding are read with their paths", () => {
  const [core] = fixture("over-budget.json").cores;
  assert.deepEqual(core.findings[0].subjects[0], {
    layer: "hardware",
    element: "CORE0",
    path: [{ field: "threshold" }],
  });
});

test("a malformed subject is refused by check-model's subject reader, in this reader's words", () => {
  const cases: [(raw: RawDocument) => void, string][] = [
    [
      (raw) => (raw.cores[0].findings[0].subjects[0].path = [{}]),
      "cores[0].findings[0].subjects[0].path[0] must be an object with exactly one key",
    ],
    [
      (raw) => (raw.cores[0].findings[0].subjects[0].path = [{ field: 1 }]),
      "cores[0].findings[0].subjects[0].path[0] must be a string field, a member or a string key",
    ],
    [
      (raw) => (raw.cores[0].findings[0].subjects[0].element = 7),
      "cores[0].findings[0].subjects[0].element must be a string",
    ],
    [
      (raw) => delete raw.cores[0].findings[0].subjects[0].layer,
      "cores[0].findings[0].subjects[0] must carry a string layer",
    ],
  ];
  for (const [edit, expected] of cases) {
    const raw = JSON.parse(fixtureText("over-budget.json"));
    edit(raw);
    const read = parseResourceDocument(JSON.stringify(raw));
    assert.ok(isResourceRefusal(read), expected);
    assert.equal(read.error, `the report cannot be read: ${expected}`);
  }
  // A member step is any value, as `SubjectStep.to_json` writes it; a key step a string.
  const raw = JSON.parse(fixtureText("over-budget.json"));
  raw.cores[0].findings[0].subjects[0].path = [{ field: "memory" }, { key: "RAM0" }, { member: 3 }];
  const read = parseResourceDocument(JSON.stringify(raw));
  assert.ok(!isResourceRefusal(read));
  assert.deepEqual(read.cores[0].findings[0].subjects[0].path, [
    { field: "memory" },
    { key: "RAM0" },
    { member: 3 },
  ]);
});

test("a version-2 document is refused as another release's", () => {
  const raw = JSON.parse(fixtureText("basic.json"));
  raw.schemaVersion = 2;
  assert.deepEqual(parseResourceDocument(JSON.stringify(raw)), { error: OTHER_RELEASE });
  assert.equal(
    OTHER_RELEASE,
    "This report was written by another SWCraft release; update the toolchain or the extension.",
  );
});

test("a malformed document is refused naming the place, never thrown", () => {
  const cases: [(raw: RawDocument) => void, string][] = [
    [(raw) => (raw.memories[0].status = "fine"), 'memories[0].status "fine"'],
    [(raw) => delete raw.cores[0].utilization, "cores[0].utilization must be a string"],
    [(raw) => (raw.memories[0].size = "big"), "memories[0].size must be a whole number"],
    [
      (raw) => (raw.memories[0].consumers[0].layer = "tasks"),
      'memories[0].consumers[0].layer must be "swc"',
    ],
    [(raw) => (raw.cores[0].kind = "memory"), 'cores[0].kind must be "core"'],
    [(raw) => (raw.topology = {}), "the report.topology must be an array"],
    [(raw) => delete raw.findings, "findings must be an object"],
    [
      (raw) => (raw.findings.errors = 1.5),
      "findings must count errors and informational as whole numbers",
    ],
    [
      (raw) => delete raw.findings.informational,
      "findings must count errors and informational as whole numbers",
    ],
  ];
  for (const [edit, expected] of cases) {
    const raw = JSON.parse(fixtureText("basic.json"));
    edit(raw);
    const read = parseResourceDocument(JSON.stringify(raw));
    assert.ok(isResourceRefusal(read), expected);
    assert.ok(read.error.startsWith("the report cannot be read: "), read.error);
    assert.ok(read.error.includes(expected), `${read.error} lacks ${expected}`);
  }
  // An integer JSON.parse cannot hold exactly is refused, never rounded into the tooltip.
  const unsafe = fixtureText("basic.json").replace(/"size": 262144/, '"size": 9007199254740993');
  assert.notEqual(unsafe, fixtureText("basic.json"));
  const read = parseResourceDocument(unsafe);
  assert.ok(isResourceRefusal(read));
  assert.ok(
    read.error.includes("memories[0].size is larger than this view can show exactly"),
    read.error,
  );
  for (const text of ["", "not json", "[]", "null"]) {
    assert.ok(isResourceRefusal(parseResourceDocument(text)), text);
  }
});

// --- resourceNodes: the rows ---------------------------------------------------------

test("the basic tree: two expanded sections, the rows' descriptions, tooltips and opens", () => {
  const tree = resourceNodes(fixture("basic.json"), false, false);
  assert.equal(tree.description, "");
  assert.deepEqual(
    tree.nodes.map((node) => [node.id, node.label, node.contextValue, node.collapsible]),
    [
      ["resource:section:cores", "Cores", "resource.section", "expanded"],
      ["resource:section:memories", "Memories", "resource.section", "expanded"],
    ],
  );
  const core = row(tree.nodes, "Cores", "CORE0");
  assert.equal(core.id, "resource:core:CORE0");
  assert.equal(core.contextValue, "resource.core");
  assert.equal(core.description, "12.00% of 70.00% · headroom 58.00%");
  assert.equal(
    core.tooltip,
    "strict · within budget · physical headroom 88.00% of the core · 1 consumer",
  );
  assert.equal(core.collapsible, "collapsed");
  assert.deepEqual(core.open, { layer: "hardware", element: "CORE0" });
  const [task] = core.children;
  assert.deepEqual(
    [task.id, task.label, task.description, task.icon, task.contextValue, task.open],
    [
      "resource:core:CORE0:consumer:0",
      "Task_10ms",
      "12.00% (wcet 1200us / period 0.01s)",
      undefined,
      "resource.consumer",
      { layer: "tasks", element: "Task_10ms" },
    ],
  );
  const memory = row(tree.nodes, "Memories", "RAM0");
  assert.equal(memory.contextValue, "resource.memory");
  assert.equal(memory.description, "3584 of 209715 bytes · headroom 206131");
  assert.equal(
    memory.tooltip,
    "strict · within budget · physical headroom 258560 bytes of a 262144 byte size · 2 consumers",
  );
  assert.deepEqual(
    memory.children.map((node) => [node.label, node.description, node.open?.layer]),
    [
      ["ExampleSWC", "2048 bytes", "swc"],
      ["ExampleConsumerSWC", "1536 bytes", "swc"],
    ],
  );
});

test("every status has its icon and colour", () => {
  assert.deepEqual(STATUS_ICONS[STATUS_OK], { id: "check", color: "charts.green" });
  assert.deepEqual(STATUS_ICONS[STATUS_BREACH], { id: "error", color: "errorForeground" });
  assert.deepEqual(STATUS_ICONS[STATUS_UNKNOWN], { id: "warning", color: "charts.yellow" });
  const over = resourceNodes(fixture("over-budget.json"), false, false).nodes;
  assert.deepEqual(row(over, "Cores", "CORE0").icon, ICON_NO);
  assert.deepEqual(row(over, "Memories", "RAM").icon, ICON_NO);
  const unknown = resourceNodes(fixture("unknown.json"), false, false).nodes;
  assert.deepEqual(row(unknown, "Cores", "CORE0").icon, ICON_WARNING);
  assert.deepEqual(row(unknown, "Memories", "RAM").icon, ICON_WARNING);
  const basic = resourceNodes(fixture("basic.json"), false, false).nodes;
  assert.deepEqual(row(basic, "Cores", "CORE0").icon, ICON_YES);
});

test("an error finding is red and an advisory resource's info finding blue, each opening its first subject", () => {
  const nodes = resourceNodes(fixture("over-budget.json"), false, false).nodes;
  const core = row(nodes, "Cores", "CORE0");
  const finding = core.children.find((node) => node.contextValue === "resource.finding");
  assert.ok(finding);
  assert.equal(
    finding.label,
    "CPU core 'CORE0' utilization 80.00% exceeds its threshold of 70.00%.",
  );
  assert.deepEqual(finding.icon, ICON_NO);
  assert.deepEqual(finding.open, {
    layer: "hardware",
    element: "CORE0",
    path: [{ field: "threshold" }],
  });
  const memory = row(nodes, "Memories", "RAM");
  assert.ok(memory.tooltip.startsWith("advisory · over budget · "));
  const info = memory.children.find((node) => node.contextValue === "resource.finding");
  assert.ok(info);
  assert.deepEqual(info.icon, ICON_INFO);
  assert.deepEqual(ICON_INFO, { id: "info", color: "charts.blue" });
});

test("topology is a section only with records, each a red row with no click", () => {
  const nodes = resourceNodes(fixture("over-budget.json"), false, false).nodes;
  assert.deepEqual(
    nodes.map((node) => node.label),
    ["Cores", "Memories", "Topology (1)"],
  );
  const [record] = section(nodes, "Topology").children;
  assert.equal(
    record.label,
    "hardware.undeclared-partition: Task 'Task_lost' runs in partition 'NOWHERE', which no hardware " +
      "element declares (declared partitions: PARTITION0).",
  );
  assert.deepEqual(
    [record.icon, record.contextValue, record.open],
    [ICON_NO, "resource.topology", undefined],
  );
  const basic = resourceNodes(fixture("basic.json"), false, false).nodes;
  assert.ok(!basic.some((node) => node.label.startsWith("Topology")));
});

test("the undeclared consumer is a yellow row, the declared zeros and the missing budgets fold", () => {
  const nodes = resourceNodes(fixture("unknown.json"), false, false).nodes;
  const memory = row(nodes, "Memories", "RAM");
  assert.equal(
    memory.tooltip,
    "strict · unknown (not every consumer declared a budget) · physical headroom 6144 bytes of a 8192 " +
      "byte size · 5 consumers, 1 undeclared",
  );
  assert.deepEqual(
    memory.children.map((node) => [node.label, node.description, node.icon, node.contextValue]),
    [
      ["Alpha", "2048 bytes", undefined, "resource.consumer"],
      ["Beta", "not declared", ICON_WARNING, "resource.consumer"],
      ["3 consumers declare 0 bytes", "", undefined, "resource.consumer"],
      ["1 consumer declares no budget", "", ICON_NO, "resource.finding"],
    ],
  );
  const zeros = memory.children[2];
  assert.equal(zeros.open, undefined);
  assert.deepEqual(memory.children[3].open?.element, "Beta");
  const core = row(nodes, "Cores", "CORE0");
  assert.deepEqual(core.children[1].description, "not declared (period 0.02s, no wcet declared)");
  assert.deepEqual(core.children[1].icon, ICON_WARNING);
});

test("several missing budgets fold into one row at the first one's place", () => {
  const document = fixture("unknown.json");
  const [memory] = document.memories;
  memory.findings.push(
    { ...memory.findings[0], message: "another" },
    {
      severity: "error",
      code: "threshold_breach",
      message: "over",
      subjects: [],
    },
  );
  const children = row(resourceNodes(document, false, false).nodes, "Memories", "RAM").children;
  const findings = children.filter((node) => node.contextValue === "resource.finding");
  assert.deepEqual(
    findings.map((node) => [node.label, node.open === undefined]),
    [
      ["2 consumers declare no budget", false],
      ["over", true],
    ],
  );
});

test("the consumers keep the document's order", () => {
  const document = fixture("basic.json");
  document.memories[0].consumers.reverse();
  const memory = row(resourceNodes(document, false, false).nodes, "Memories", "RAM0");
  assert.deepEqual(
    memory.children.map((node) => node.label),
    ["ExampleConsumerSWC", "ExampleSWC"],
  );
  assert.deepEqual(
    memory.children.map((node) => node.id),
    ["resource:memory:RAM0:consumer:0", "resource:memory:RAM0:consumer:1"],
  );
});

test("a core's declared zeros fold as 0%", () => {
  const document = fixture("basic.json");
  document.cores[0].consumers.push({
    name: "Task_idle",
    layer: "tasks",
    amount: "0.00%",
    declared: true,
    detail: "",
  });
  const core = row(resourceNodes(document, false, false).nodes, "Cores", "CORE0");
  assert.deepEqual(core.children.at(-1)?.label, "1 consumer declares 0%");
});

test("an empty section is not shown, and a model with nothing is one message row", () => {
  const document = fixture("basic.json");
  document.cores = [];
  assert.deepEqual(
    resourceNodes(document, false, false).nodes.map((node) => node.label),
    ["Memories"],
  );
  document.memories = [];
  const empty = resourceNodes(document, false, false).nodes;
  assert.deepEqual(
    empty.map((node) => [node.label, node.contextValue, node.icon]),
    [[NO_RESOURCES_ROW, "resource.message", undefined]],
  );
  assert.equal(NO_RESOURCES_ROW, "The model declares no core and no memory.");
  document.topology = [{ rule: "hardware.no-processor", message: "no processor" }];
  assert.deepEqual(
    resourceNodes(document, false, false).nodes.map((node) => node.label),
    ["Topology (1)"],
  );
});

test("two resources of one name get distinct ids", () => {
  const document = fixture("basic.json");
  document.memories.push({ ...document.memories[0] });
  const ids = section(resourceNodes(document, false, false).nodes, "Memories").children.map(
    (node) => node.id,
  );
  assert.deepEqual(ids, ["resource:memory:RAM0", "resource:memory:RAM0~2"]);
});

// --- resourceNodes: the description and a failed run ----------------------------------

test("the description's precedence: failed, then out of date, then empty", () => {
  const document = fixture("basic.json");
  assert.equal(resourceNodes(document, false, false).description, "");
  assert.equal(resourceNodes(document, true, false).description, OUT_OF_DATE_DESCRIPTION);
  assert.equal(resourceNodes(document, true, true).description, FAILED_DESCRIPTION);
  assert.equal(resourceNodes(document, false, true).description, FAILED_DESCRIPTION);
  assert.equal(OUT_OF_DATE_DESCRIPTION, "out of date — Refresh");
  assert.equal(FAILED_DESCRIPTION, "the last run failed — see the SWCraft output channel");
});

test("a failed run keeps the last tree; with none it is one message row; before any run nothing", () => {
  const document = fixture("basic.json");
  assert.deepEqual(
    resourceNodes(document, false, true).nodes,
    resourceNodes(document, false, false).nodes,
  );
  assert.deepEqual(
    resourceNodes(undefined, false, true).nodes.map((node) => [node.label, node.contextValue]),
    [[FAILED_ROW, "resource.message"]],
  );
  assert.deepEqual(resourceNodes(undefined, false, false), {
    description: "",
    nodes: [],
  });
});

// --- A document with a `fit` key ------------------------------------------------------

test("a document with a fit key parses and draws no answer row: the key is not read", () => {
  const allNodes = (nodes: readonly ResourceNode[]): ResourceNode[] =>
    nodes.flatMap((node) => [node, ...allNodes(node.children)]);
  for (const name of ["fit-core.json", "fit-memory.json"]) {
    const asked = JSON.parse(fixtureText(name));
    assert.notEqual(asked.fit, null, `${name} is a --fit-* run's document`);
    const withKey = fixture(name);
    assert.equal("fit" in withKey, false, name);
    for (const resource of [...withKey.cores, ...withKey.memories]) {
      assert.equal("fit" in resource, false, resource.name);
    }
    const nodes = resourceNodes(withKey, false, false).nodes;
    for (const node of allNodes(nodes)) {
      assert.equal(node.id.includes(":fit:"), false, node.id);
      assert.notEqual(node.id, "resource:question");
      assert.ok(!["resource.fit", "resource.verdict"].includes(node.contextValue), node.id);
      if (["resource.core", "resource.memory"].includes(node.contextValue)) {
        assert.notEqual(node.collapsible, "expanded", node.id);
      }
    }
    // The same document without the keys draws the same tree.
    delete asked.fit;
    for (const resource of [...asked.cores, ...asked.memories]) {
      delete resource.fit;
    }
    const without = parseResourceDocument(JSON.stringify(asked));
    assert.ok(!isResourceRefusal(without));
    assert.deepEqual(resourceNodes(without, false, false).nodes, nodes);
  }
  // A key the view does not read cannot refuse a document, whatever it holds.
  const raw = JSON.parse(fixtureText("basic.json"));
  raw.fit = { kind: "disk", asked: "1" };
  raw.memories[0].fit = 7;
  assert.ok(!isResourceRefusal(parseResourceDocument(JSON.stringify(raw))));
});

// --- The nodes ------------------------------------------------------------------------

test("every node the view draws is one resourceNodeOf() recognises, and nothing else is", () => {
  const trees = [
    resourceNodes(fixture("basic.json"), false, false),
    resourceNodes(fixture("fit-memory.json"), true, true),
    resourceNodes(fixture("unknown.json"), false, false),
    resourceNodes(fixture("over-budget.json"), false, false),
    resourceNodes(undefined, false, true),
  ];
  const all = (nodes: readonly ResourceNode[]): ResourceNode[] =>
    nodes.flatMap((node) => [node, ...all(node.children)]);
  const nodes = trees.flatMap((tree) => all(tree.nodes));
  assert.ok(nodes.length > 0);
  for (const node of nodes) {
    assert.ok(node.id.startsWith(RESOURCE_ID_PREFIX), node.id);
    assert.equal(resourceNodeOf(node), node, node.id);
  }
  for (const other of [
    undefined,
    null,
    "resource:x",
    { id: "trace:x", contextValue: "x" },
    { id: "resource:x" },
  ]) {
    assert.equal(resourceNodeOf(other), undefined, JSON.stringify(other));
  }
});

test("a resource row's context value names its kind", () => {
  const nodes = resourceNodes(fixture("basic.json"), false, false).nodes;
  assert.equal(row(nodes, "Cores", "CORE0").contextValue, "resource.core");
  assert.equal(row(nodes, "Memories", "RAM0").contextValue, "resource.memory");
});

// --- The view's commands and its runs ------------------------------------------------

test("the view's two commands, one of which spawns nothing", () => {
  assert.deepEqual(
    [...RESOURCE_VIEW_COMMANDS],
    ["swcraft.resourcesRefresh", "swcraft.resourcesOpen"],
  );
  assert.deepEqual([...RESOURCE_NON_SPAWNING_COMMANDS], ["swcraft.resourcesOpen"]);
});

test("the channel's closing line: the exit, or the refusal's reason, never the report", () => {
  const document = fixture("basic.json");
  assert.equal(
    resourceRunLine(0, document, { report: false }),
    "SWCraft: the report is shown in the Resources view (exit code 0).",
  );
  assert.equal(
    resourceRunLine(0, parseResourceDocument('{"schemaVersion": 2}'), { report: false }),
    `SWCraft: ${OTHER_RELEASE}`,
  );
  assert.equal(
    resourceRunLine(1, undefined, { report: false }),
    "SWCraft: swcraft resource-report ended with exit code 1; the Resources view keeps its last report.",
  );
  assert.equal(
    resourceRunLine(null, undefined, { report: false }),
    "SWCraft: swcraft resource-report ended with no exit code; the Resources view keeps its last report.",
  );
  assert.equal(resourceRunLine(0, document, { report: false }).includes("RAM"), false);
});

test("a press an older CLI refuses says it is another release; Refresh and other exits do not", () => {
  const refused =
    "SWCraft: swcraft refused the report's options: it is another SWCraft release; update the toolchain or the extension. The Resources view keeps its last report.";
  assert.equal(resourceRunLine(2, undefined, { report: true }), refused);
  assert.equal(
    resourceRunLine(2, undefined, { report: false }),
    "SWCraft: swcraft resource-report ended with exit code 2; the Resources view keeps its last report.",
  );
  assert.equal(
    resourceRunLine(1, undefined, { report: true }),
    "SWCraft: swcraft resource-report ended with exit code 1; the Resources view keeps its last report.",
  );
  assert.equal(
    resourceRunLine(0, fixture("basic.json"), { report: true }),
    "SWCraft: the report is shown in the Resources view (exit code 0).",
  );
});

test("a row whose file is not found names its element", () => {
  assert.equal(
    resourceNotFoundMessage({ layer: "swc", element: "ExampleSWC" }),
    "SWCraft: 'ExampleSWC' (swc) is not in the model as saved; refresh the Resources view.",
  );
});

// --- resourceLine: the line a click opens at -------------------------------------

/** One file of `examples/basic/model/` as saved: the shapes the rows open. */
function exampleText(...parts: string[]): string {
  return fs.readFileSync(path.join(PROJECT_ROOT, "..", "examples", "basic", "model", ...parts), {
    encoding: "utf8",
  });
}

test("each row kind carries the key paths of the line it answers; a finding carries none", () => {
  const nodes = resourceNodes(fixture("basic.json"), false, false).nodes;
  const core = row(nodes, "Cores", "CORE0");
  const memory = row(nodes, "Memories", "RAM0");
  assert.deepEqual(core.lineKeys, [["threshold"], ["size"]]);
  assert.deepEqual(memory.lineKeys, [["threshold"], ["size"]]);
  assert.deepEqual(core.children[0].lineKeys, [["wcet"]]);
  assert.deepEqual(
    memory.children.map((node) => node.lineKeys),
    [[["memory", "RAM0"]], [["memory", "RAM0"]]],
  );
  const over = resourceNodes(fixture("over-budget.json"), false, false).nodes;
  const finding = row(over, "Cores", "CORE0").children.find(
    (node) => node.contextValue === "resource.finding",
  );
  assert.ok(finding);
  assert.equal(finding.lineKeys, undefined, "a finding opens at its subject's place");
});

/** The 0-based line of *text* that starts with *prefix*, which the file must hold exactly once. */
function lineStarting(text: string, prefix: string): number {
  const lines = text.split(/\r?\n/);
  const found = lines.flatMap((line, index) => (line.startsWith(prefix) ? [index] : []));
  assert.equal(found.length, 1, `exactly one line starts with ${JSON.stringify(prefix)}`);
  return found[0];
}

test("a core and a memory open at their threshold line in the example's hardware files", () => {
  for (const file of ["CORE0.yaml", "RAM0.yaml"]) {
    const text = exampleText("hardware", file);
    assert.equal(resourceLine(text, [["threshold"], ["size"]]), lineStarting(text, "threshold:"));
  }
});

test("a memory without a threshold line falls back to its size line", () => {
  const text = "name: RAM0\nkind: memory\ntype: RAM\nsize: 262144\nenforcement: strict\n";
  assert.equal(resourceLine(text, [["threshold"], ["size"]]), 3);
});

test("a task opens at its own wcet, never a runnable's", () => {
  const task = exampleText("tasks", "Task_10ms.yaml");
  assert.equal(resourceLine(task, [["wcet"]]), lineStarting(task, "wcet:"));
  const text = [
    "name: T",
    "period: 0.01",
    "runnables:",
    "- swc: A",
    "  runnable: R",
    "  wcet: 10us",
    "wcet: 1200us",
    "",
  ].join("\n");
  assert.equal(resourceLine(text, [["wcet"]]), 6);
});

test("a component opens at the entry naming the memory under memory:", () => {
  const text = exampleText("swc", "example", "ExampleSWC.yaml");
  const memory = lineStarting(text, "memory:");
  const entry = text
    .split(/\r?\n/)
    .findIndex((line, index) => index > memory && /^\s+RAM0:/.test(line));
  assert.ok(entry > memory, "the example lists RAM0 under memory:");
  assert.equal(resourceLine(text, [["memory", "RAM0"]]), entry);
  const two = "name: A\nmemory:\n  ROM0: 512\n  RAM0: 64\n";
  assert.equal(resourceLine(two, [["memory", "RAM0"]]), 3);
});

test("an absent key falls back to line 0", () => {
  assert.equal(resourceLine("name: T\nperiod: 0.01\n", [["wcet"]]), 0);
  assert.equal(resourceLine("name: A\nmemory:\n  ROM0: 512\n", [["memory", "RAM0"]]), 0);
  // A flow mapping has no key line of its own.
  assert.equal(resourceLine("name: A\nmemory: {RAM0: 64}\n", [["memory", "RAM0"]]), 0);
  assert.equal(resourceLine("", [["threshold"], ["size"]]), 0);
});

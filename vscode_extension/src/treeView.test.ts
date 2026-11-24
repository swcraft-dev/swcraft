import assert from "node:assert/strict";
import { test } from "node:test";

import {
  ARTIFACT_DEFINITIONS,
  ArtifactRecord,
  type ArtifactState,
  buildProjectNode,
  buildTree,
  type CommandStatus,
  decideArtifactState,
  decideResourceReportState,
  describeBadge,
  describeStatus,
  isOutOfDate,
  lastGenerationKey,
  MARKED_DEFINITIONS,
  nextSetupSection,
  OUT_OF_DATE_ICON,
  PANEL_MENU_COMMANDS,
  RUNNING_TEXT,
  RunningCommands,
  SETUP_SECTION_KEY,
  type SetupFacts,
  SetupSectionRecord,
  type SetupSectionState,
  STATUS_WINDOW_MS,
  setupSectionCollapsed,
  TREE_COMMAND_DEFINITIONS,
  TREE_SECTIONS,
} from "./treeView";

/**
 * A fixed instant: the pure module must render from its arguments, never a
 * clock. Built from *local* calendar components rather than parsed from a `Z`
 * string on purpose — `describeStatus()` renders the viewer's local wall
 * clock, so a UTC literal would make the expected `12:39` true only on a
 * machine in UTC and this suite would pass or fail by `TZ`.
 */
const NOW = new Date(2026, 7, 23, 12, 39, 0);

/** The same instant as a status stamp, for the "just ran" cases. */
function justRan(outcome: CommandStatus["kind"], message = "boom"): CommandStatus {
  return outcome === "success"
    ? { kind: "success", at: NOW }
    : { kind: "failure", message, at: NOW };
}

/** Flatten the two-level tree back to the item order the TreeView renders top to bottom. */
function flatten(sections: ReturnType<typeof buildTree>) {
  return sections.flatMap((section) => section.items);
}

/**
 * Whether *id* is the id drawn for the section whose `TREE_SECTIONS` id is *base*: the base
 * itself, or — Setup's — the base with its default and generation appended
 * (`section:setup:expanded:0`).
 */
function isSectionId(id: string, base: string): boolean {
  return id === base || id.startsWith(`${base}:`);
}

/**
 * One section of a built tree, selected by its identity (`section:<name>`)
 * rather than by its position: a section added between two others must not
 * make a test read the wrong one.
 */
function sectionOf(
  sections: ReturnType<typeof buildTree>,
  name: "model" | "experiments" | "setup",
) {
  const found = sections.find((section) => isSectionId(section.id, `section:${name}`));
  assert.ok(found, `buildTree must emit the '${name}' section`);
  return found;
}

// --- buildProjectNode --------------------------------------------------------

test("buildProjectNode: no workspace open", () => {
  const node = buildProjectNode({ workspaceRoot: undefined, hasManifest: false });
  assert.equal(node.label, "No workspace open");
  assert.equal(node.description, "Open a folder to get started");
});

test("buildProjectNode: workspace open but no swcraft.yaml detected", () => {
  const node = buildProjectNode({ workspaceRoot: "/some/workspace", hasManifest: false });
  assert.equal(node.label, "No SWCraft project detected");
  assert.equal(node.description, "Run Onboard Project");
});

test("buildProjectNode: workspace open with swcraft.yaml detected", () => {
  const node = buildProjectNode({ workspaceRoot: "/some/workspace", hasManifest: true });
  assert.equal(node.label, "SWCraft project detected");
  assert.equal(node.description, "/some/workspace");
});

// --- buildTree: the three sections -------------------------------------------

test("buildTree: returns exactly the three sections, Model, Experiments, Setup", () => {
  const sections = buildTree({}, NOW);
  assert.deepEqual(
    sections.map((section) => section.label),
    ["Model", "Experiments", "Setup"],
  );
});

test("buildTree: the Experiments section sits between Model and Setup", () => {
  // An experiment asks a question of the model and writes nothing into it, so it follows the
  // model commands and comes before the one-off setup steps.
  const ids = buildTree({}, NOW).map((section) => section.id);
  const at = (base: string) => ids.findIndex((id) => isSectionId(id, base));
  assert.equal(at("section:experiments"), at("section:model") + 1);
  assert.equal(at("section:setup"), at("section:experiments") + 1);
});

test("buildTree: the Experiments section holds exactly Allocation (CARMA) and Trace Links", () => {
  const experiments = sectionOf(buildTree({}, NOW), "experiments");
  assert.deepEqual(
    experiments.items.map((item) => [item.label, item.commandId]),
    [
      ["Allocation (CARMA)", "swcraft.experimentAllocation"],
      ["Trace Links", "swcraft.experimentTrace"],
    ],
  );
});

test("buildTree: a successful experiment reads 'ran HH:MM'", () => {
  const item = flatten(buildTree({ "swcraft.experimentAllocation": justRan("success") }, NOW)).find(
    (candidate) => candidate.commandId === "swcraft.experimentAllocation",
  );
  assert.equal(item?.description, "ran 12:39");
});

test("buildTree: the Model section lists its ten commands in the decided order", () => {
  // Named by the want, not by the pipeline step: Build (which ends with `swcraft validate`)
  // keeps the top, Check Model stays directly under it, and Test Tables sits directly after
  // Diagrams (the tables are artifacts like the diagrams, and the press behaves like theirs).
  // Impact is asked from the element's file. The ordering comment above
  // TREE_COMMAND_DEFINITIONS says why.
  const model = sectionOf(buildTree({}, NOW), "model");
  assert.deepEqual(
    model.items.map((item) => item.commandId),
    [
      "swcraft.build",
      "swcraft.checkModel",
      "swcraft.diagrams",
      "swcraft.testTables",
      "swcraft.changeReport",
      "swcraft.resourceReport",
      "swcraft.metrics",
      "swcraft.exportComponent",
      "swcraft.importComponent",
      "swcraft.importRequirements",
    ],
  );
  assert.deepEqual(
    model.items.map((item) => item.label),
    [
      "Build",
      "Check Model",
      "Diagrams",
      "Test Tables",
      "Change Report",
      "Resource Report",
      "Architecture Metrics",
      "Export Component",
      "Import Component",
      "Import Requirements",
    ],
  );
});

test("buildTree: the three retired pipeline items are not in the panel", () => {
  // Generate C4 lives inside Diagrams and Build, Validate inside Build (the terminal keeps
  // `swcraft validate`), and Open Generated C4 has no `.c4` file to open. A label coming back
  // is the pipeline-step panel coming back.
  const labels = flatten(buildTree({}, NOW)).map((item) => item.label);
  for (const retired of ["Validate", "Generate C4", "Open Generated C4"]) {
    assert.equal(labels.includes(retired), false, `the panel lists '${retired}' again`);
  }
});

test("buildTree: the read-only commands are in the Model section, not Setup; Impact in neither", () => {
  // Change Report and resource-report sit beside the other model commands; Setup is for the
  // things you do once to a project. Impact is not a panel item: it is asked from the
  // element's file, and its answer is the Impact view.
  const sections = buildTree({}, NOW);
  const model = sectionOf(sections, "model");
  const setup = sectionOf(sections, "setup");
  const readOnly = ["swcraft.changeReport", "swcraft.resourceReport", "swcraft.metrics"];
  assert.equal(
    flatten(sections).some((item) => item.commandId === "swcraft.impact"),
    false,
    "Impact is no panel item: it is asked from the element's file",
  );
  const inSetup = new Set(setup.items.map((item) => item.commandId));
  for (const commandId of readOnly) {
    assert.ok(
      model.items.some((item) => item.commandId === commandId),
      `${commandId} must be a Model section item`,
    );
    assert.equal(inSetup.has(commandId), false, `${commandId} must not be a Setup item`);
  }
});

test("buildTree: the Setup section lists its commands in the decided order", () => {
  // The toolchain leads: it is what every health finding and the onboarding preflight refusal
  // send the reader to, and what has to exist before Onboard Project has an executable to run.
  // Setup holds setup only: these three, in this order, and nothing else.
  const setup = sectionOf(buildTree({}, NOW), "setup");
  assert.deepEqual(
    setup.items.map((item) => item.commandId),
    ["swcraft.installToolchain", "swcraft.updateToolchain", "swcraft.onboardProject"],
  );
  assert.deepEqual(
    setup.items.map((item) => item.label),
    ["Install Toolchain", "Update Toolchain", "Onboard Project"],
  );
});

test("buildTree: Open Guide and Check Editor Tooling are in no section — they are the panel's menu entries", () => {
  // Both live in the view title's `…` menu, where VS Code keeps help and diagnostics; a tree
  // item for either would put it in two places of the same panel.
  const moved = PANEL_MENU_COMMANDS.map((entry) => entry.commandId);
  assert.deepEqual(moved.slice().sort(), ["swcraft.checkEditorTooling", "swcraft.openGuide"]);
  for (const section of buildTree({}, NOW)) {
    for (const item of section.items) {
      assert.equal(moved.includes(item.commandId), false, `${item.commandId} in ${section.label}`);
      assert.notEqual(item.label, "Open Guide");
      assert.notEqual(item.label, "Check Editor Tooling");
    }
  }
});

test("buildTree: a section node carries no commandId — clicking it invokes nothing", () => {
  for (const section of buildTree({}, NOW)) {
    assert.equal(
      "commandId" in section,
      false,
      `section '${section.label}' must not carry a commandId`,
    );
    assert.ok(section.items.length > 0, `section '${section.label}' must not be empty`);
  }
});

test("buildTree: every id is unique across sections and items", () => {
  const sections = buildTree({}, NOW);
  const ids = [...sections.map((section) => section.id), ...flatten(sections).map((i) => i.id)];
  assert.equal(new Set(ids).size, ids.length, `ids must be unique across the tree: ${ids}`);
});

test("buildTree: renders every definition, and each lands in exactly one section", () => {
  const sections = buildTree({}, NOW);
  const items = flatten(sections);
  assert.equal(items.length, TREE_COMMAND_DEFINITIONS.length);
  assert.deepEqual(
    [...items.map((item) => item.id)].sort(),
    [...TREE_COMMAND_DEFINITIONS.map((def) => def.id)].sort(),
  );
});

test("buildTree: item order within a section follows TREE_COMMAND_DEFINITIONS", () => {
  for (const section of TREE_SECTIONS) {
    const built = buildTree({}, NOW).find((candidate) => isSectionId(candidate.id, section.id));
    assert.ok(built, `buildTree must emit a section for '${section.id}'`);
    assert.deepEqual(
      built.items.map((item) => item.id),
      TREE_COMMAND_DEFINITIONS.filter((def) => def.section === section.section).map(
        (def) => def.id,
      ),
    );
  }
});

// --- The Setup section's default ---------------------------------------------

test("setupSectionCollapsed: true only with all three facts, false for each of the seven others", () => {
  const values = [false, true];
  let collapsed = 0;
  for (const toolchainInstalled of values) {
    for (const hasManifest of values) {
      for (const hasAgentProfile of values) {
        const facts: SetupFacts = { toolchainInstalled, hasManifest, hasAgentProfile };
        const expected = toolchainInstalled && hasManifest && hasAgentProfile;
        assert.equal(setupSectionCollapsed(facts), expected, JSON.stringify(facts));
        if (setupSectionCollapsed(facts)) {
          collapsed += 1;
        }
      }
    }
  }
  assert.equal(collapsed, 1, "exactly one of the eight combinations collapses Setup");
});

test("nextSetupSection: generation 0 first, kept while the default holds, advanced on each change", () => {
  const first = nextSetupSection(undefined, false);
  assert.deepEqual(first, { collapsedByDefault: false, generation: 0 });
  assert.deepEqual(nextSetupSection(undefined, true), { collapsedByDefault: true, generation: 0 });
  // The same default returns the very state: the id holds, so VS Code keeps
  // the user's own toggling of it.
  assert.equal(nextSetupSection(first, false), first);
  const second = nextSetupSection(first, true);
  assert.deepEqual(second, { collapsedByDefault: true, generation: 1 });
  assert.equal(nextSetupSection(second, true), second);
  assert.deepEqual(nextSetupSection(second, false), { collapsedByDefault: false, generation: 2 });
});

test("buildTree: a round trip expanded → collapsed → expanded draws Setup under three distinct ids", () => {
  // The third state must not get the first id back, or a collapse the user made on it would
  // be restored although a setup fact is missing again.
  let state: SetupSectionState | undefined;
  const drawn: Array<[string, boolean]> = [];
  for (const collapsedByDefault of [false, true, false]) {
    state = nextSetupSection(state, collapsedByDefault);
    const setup = sectionOf(buildTree({}, NOW, {}, new Set<string>(), state), "setup");
    drawn.push([setup.id, setup.collapsedByDefault]);
  }
  assert.deepEqual(drawn, [
    ["section:setup:expanded:0", false],
    ["section:setup:collapsed:1", true],
    ["section:setup:expanded:2", false],
  ]);
  assert.equal(new Set(drawn.map(([id]) => id)).size, 3);
});

/** A `workspaceState` stand-in over a plain map, counting its writes. */
function setupMemento(initial?: unknown) {
  const values = new Map<string, unknown>();
  if (initial !== undefined) {
    values.set(SETUP_SECTION_KEY, initial);
  }
  let writes = 0;
  return {
    values,
    writes: () => writes,
    get<T>(key: string): T | undefined {
      return values.get(key) as T | undefined;
    },
    update(key: string, value: unknown): void {
      writes += 1;
      values.set(key, value);
    },
  };
}

test("SetupSectionRecord: a reload starts from the stored state, so the id holds while the default does", () => {
  const memento = setupMemento();
  const before = new SetupSectionRecord(memento);
  before.advance(() => false, new Set<string>());
  const set = before.advance(() => true, new Set<string>());
  assert.deepEqual(set, { collapsedByDefault: true, generation: 1 });
  assert.deepEqual(memento.values.get(SETUP_SECTION_KEY), set);
  // The window reloads: a new record over the same memento draws the same id,
  // which is what lets VS Code restore the user's own toggle of it.
  const after = new SetupSectionRecord(memento);
  const drawn = after.advance(() => true, new Set<string>());
  assert.deepEqual(drawn, set);
  // And a later change advances from the stored generation, never back to 0.
  assert.deepEqual(
    after.advance(() => false, new Set<string>()),
    {
      collapsedByDefault: false,
      generation: 2,
    },
  );
});

test("SetupSectionRecord: writes the memento only when the state changes", () => {
  const memento = setupMemento();
  const record = new SetupSectionRecord(memento);
  record.advance(() => false, new Set<string>());
  record.advance(() => false, new Set<string>());
  record.advance(() => false, new Set<string>());
  assert.equal(memento.writes(), 1);
});

test("SetupSectionRecord: a stored value of another shape is ignored, and the first state is generation 0", () => {
  for (const stored of [
    "collapsed",
    null,
    { collapsedByDefault: "yes", generation: 1 },
    { collapsedByDefault: true, generation: -1 },
    { collapsedByDefault: true, generation: 1.5 },
  ]) {
    const record = new SetupSectionRecord(setupMemento(stored));
    assert.deepEqual(
      record.advance(() => true, new Set<string>()),
      { collapsedByDefault: true, generation: 0 },
      JSON.stringify(stored),
    );
  }
});

test("SetupSectionRecord: while a toolchain install or update runs the facts are not read and the state holds", () => {
  for (const commandId of ["swcraft.installToolchain", "swcraft.updateToolchain"]) {
    const record = new SetupSectionRecord(setupMemento());
    const set = record.advance(() => true, new Set<string>());
    let reads = 0;
    const missing = () => {
      reads += 1;
      return false;
    };
    // The installer's swap: the executable is briefly gone.
    assert.equal(record.advance(missing, new Set([commandId])), set, commandId);
    assert.equal(reads, 0, `${commandId}: no fact read while it runs`);
    // Its closing refresh, with the id gone from the running set, reads again.
    assert.deepEqual(
      record.advance(() => true, new Set<string>()),
      set,
    );
  }
  // Another command running reads the facts as usual.
  const record = new SetupSectionRecord(setupMemento());
  record.advance(() => true, new Set<string>());
  assert.deepEqual(
    record.advance(() => false, new Set(["swcraft.build"])),
    {
      collapsedByDefault: false,
      generation: 1,
    },
  );
});

test("SetupSectionRecord: with no state yet, the facts are read even while the toolchain installs", () => {
  const record = new SetupSectionRecord(setupMemento());
  assert.deepEqual(
    record.advance(() => false, new Set(["swcraft.installToolchain"])),
    {
      collapsedByDefault: false,
      generation: 0,
    },
  );
});

test("buildTree: without a Setup state, Setup is drawn expanded as section:setup:expanded:0", () => {
  const setup = sectionOf(buildTree({}, NOW), "setup");
  assert.equal(setup.id, "section:setup:expanded:0");
  assert.equal(setup.collapsedByDefault, false);
});

test("buildTree: Model and Experiments are never collapsed by default and keep their ids", () => {
  for (const state of [
    undefined,
    { collapsedByDefault: false, generation: 3 },
    { collapsedByDefault: true, generation: 4 },
  ]) {
    const sections = buildTree({}, NOW, {}, new Set<string>(), state);
    const model = sectionOf(sections, "model");
    const experiments = sectionOf(sections, "experiments");
    assert.equal(model.id, "section:model");
    assert.equal(experiments.id, "section:experiments");
    assert.equal(model.collapsedByDefault, false);
    assert.equal(experiments.collapsedByDefault, false);
    const ids = [...sections.map((section) => section.id), ...flatten(sections).map((i) => i.id)];
    assert.equal(new Set(ids).size, ids.length, `ids must be unique across the tree: ${ids}`);
  }
  const collapsed = sectionOf(
    buildTree({}, NOW, {}, new Set<string>(), { collapsedByDefault: true, generation: 4 }),
    "setup",
  );
  assert.equal(collapsed.id, "section:setup:collapsed:4");
  assert.equal(collapsed.collapsedByDefault, true);
});

test("TREE_COMMAND_DEFINITIONS: every command declares a section and a past-tense verb", () => {
  for (const def of TREE_COMMAND_DEFINITIONS) {
    assert.ok(
      def.section === "model" || def.section === "experiments" || def.section === "setup",
      `${def.commandId} must declare a known section, got '${def.section}'`,
    );
    assert.match(def.verb, /^[a-z]+$/, `${def.commandId} must declare a lowercase verb`);
  }
});

// --- buildTree: the per-item status ------------------------------------------

test("buildTree: no statuses means no description and no tooltip anywhere", () => {
  for (const item of flatten(buildTree({}, NOW))) {
    assert.equal(
      item.description,
      undefined,
      `${item.commandId} should have no description when never run`,
    );
    assert.equal(
      item.tooltip,
      undefined,
      `${item.commandId} should have no tooltip when never run`,
    );
  }
});

test("buildTree: a successful status renders the command's verb and the time, no tooltip", () => {
  const statuses: Partial<Record<string, CommandStatus>> = {
    "swcraft.build": justRan("success"),
  };
  const buildItem = flatten(buildTree(statuses, NOW)).find(
    (item) => item.commandId === "swcraft.build",
  );
  assert.ok(buildItem);
  assert.equal(buildItem.description, "built 12:39");
  assert.equal(buildItem.tooltip, undefined);
});

test("buildTree: a failure status renders 'failed <time>' and the error as tooltip", () => {
  const statuses: Partial<Record<string, CommandStatus>> = {
    "swcraft.diagrams": { kind: "failure", message: "exit code 1", at: NOW },
  };
  const diagramsItem = flatten(buildTree(statuses, NOW)).find(
    (item) => item.commandId === "swcraft.diagrams",
  );
  assert.ok(diagramsItem);
  assert.equal(diagramsItem.description, "failed 12:39");
  assert.equal(diagramsItem.tooltip, "exit code 1");
});

test("buildTree: statuses are applied independently per command, across sections", () => {
  const statuses: Partial<Record<string, CommandStatus>> = {
    "swcraft.build": justRan("success"),
    "swcraft.onboardProject": { kind: "failure", message: "SWCraft CLI not found", at: NOW },
  };
  const byId = new Map(flatten(buildTree(statuses, NOW)).map((item) => [item.commandId, item]));

  assert.equal(byId.get("swcraft.build")?.description, "built 12:39");
  assert.equal(byId.get("swcraft.onboardProject")?.description, "failed 12:39");
  assert.equal(byId.get("swcraft.onboardProject")?.tooltip, "SWCraft CLI not found");
  // Untouched commands remain unset.
  assert.equal(byId.get("swcraft.diagrams")?.description, undefined);
  assert.equal(byId.get("swcraft.exportComponent")?.description, undefined);
  assert.equal(byId.get("swcraft.checkModel")?.description, undefined);
});

// --- edge cases: the tree is the same shape whatever the project state is ----
//
// The project node is built separately from the sections (the adapter renders
// it above them and asks for the sections as its children), so a workspace
// with no folder open — or one with no manifest — must still get the project
// node *and* every fully-populated section beneath it. A tree that hid its
// commands when no project was detected would hide `Onboard Project`, the one
// command that fixes that state.

test("buildTree: no workspace open still renders the project node above every section", () => {
  const node = buildProjectNode({ workspaceRoot: undefined, hasManifest: false });
  assert.equal(node.label, "No workspace open");
  const sections = buildTree({}, NOW);
  assert.deepEqual(
    sections.map((section) => section.label),
    ["Model", "Experiments", "Setup"],
  );
  assert.equal(flatten(sections).length, TREE_COMMAND_DEFINITIONS.length);
});

test("buildTree: a workspace with no manifest still offers Onboard Project under Setup", () => {
  const node = buildProjectNode({ workspaceRoot: "/some/workspace", hasManifest: false });
  assert.equal(node.label, "No SWCraft project detected");
  const sections = buildTree({}, NOW);
  const model = sectionOf(sections, "model");
  const setup = sectionOf(sections, "setup");
  assert.equal(model.items.length, 10);
  assert.ok(setup.items.some((item) => item.commandId === "swcraft.onboardProject"));
  assert.deepEqual(setup.items.map((item) => item.commandId).slice(0, 2), [
    "swcraft.installToolchain",
    "swcraft.updateToolchain",
  ]);
});

// --- describeStatus: the verb, the time, and the window ----------------------
//
// Driven directly rather than only through `buildTree()`: the window boundary
// and the verb-from-the-table rule are properties of this function, and a test
// that had to route every case through the whole tree would say less about
// which of the two decided the answer.

test("describeStatus: a never-run command renders neither description nor tooltip", () => {
  assert.deepEqual(describeStatus(undefined, "validated", NOW), {
    description: undefined,
    tooltip: undefined,
  });
});

test("describeStatus: success renders '<verb> HH:MM' from the verb it is given", () => {
  const rendered = describeStatus({ kind: "success", at: NOW }, "generated", NOW);
  assert.equal(rendered.description, "generated 12:39");
  assert.equal(rendered.tooltip, undefined);
});

test("describeStatus: failure renders 'failed HH:MM' and keeps the message as tooltip", () => {
  // The verb is deliberately ignored on the failure path: the item's own label
  // sits right beside the status, so "failed" plus the time is the whole fact.
  const rendered = describeStatus(
    { kind: "failure", message: "exit code 2", at: NOW },
    "exported",
    NOW,
  );
  assert.equal(rendered.description, "failed 12:39");
  assert.equal(rendered.tooltip, "exit code 2");
});

test("describeStatus: the time is the completion stamp, not the render instant", () => {
  // Ran at 12:39, rendered at 12:39:20 — still inside the window, and the
  // label must say when the run finished rather than when the tree redrew.
  const later = new Date(NOW.getTime() + 20_000);
  assert.equal(
    describeStatus({ kind: "success", at: NOW }, "validated", later).description,
    "validated 12:39",
  );
});

test("describeStatus: a status older than the window renders nothing at all", () => {
  const after = new Date(NOW.getTime() + STATUS_WINDOW_MS);
  assert.deepEqual(describeStatus({ kind: "success", at: NOW }, "validated", after), {
    description: undefined,
    tooltip: undefined,
  });
  // A failure loses its tooltip with it: the tooltip lives exactly as long as
  // the status it explains, which is until the window closes (the adapter's
  // 30-second timer clears the map at the same instant).
  assert.deepEqual(
    describeStatus({ kind: "failure", message: "exit code 1", at: NOW }, "validated", after),
    { description: undefined, tooltip: undefined },
  );
});

test("describeStatus: the last instant inside the window still renders", () => {
  const justInside = new Date(NOW.getTime() + STATUS_WINDOW_MS - 1);
  assert.equal(
    describeStatus({ kind: "success", at: NOW }, "validated", justInside).description,
    "validated 12:39",
  );
});

test("describeStatus: a stamp in the future is treated as current, not as expired", () => {
  // A clock stepped backwards between the run and the render would otherwise
  // make `now - at` negative; that must not read as "older than the window".
  const earlier = new Date(NOW.getTime() - 5_000);
  assert.equal(
    describeStatus({ kind: "success", at: NOW }, "validated", earlier).description,
    "validated 12:39",
  );
});

test("describeStatus: single-digit hours and minutes are zero-padded", () => {
  const early = new Date(2026, 7, 23, 9, 5, 0);
  assert.equal(
    describeStatus({ kind: "success", at: early }, "reported", early).description,
    "reported 09:05",
  );
});

// --- the verb comes from the definition table, not from the renderer ---------

test("buildTree: every command renders its own declared verb on success", () => {
  const statuses: Partial<Record<string, CommandStatus>> = Object.fromEntries(
    TREE_COMMAND_DEFINITIONS.map((def) => [def.commandId, justRan("success")]),
  );
  const byId = new Map(flatten(buildTree(statuses, NOW)).map((item) => [item.commandId, item]));

  // Spelled out for the commands whose verbs differ, so a table edit that
  // swapped two of them fails here rather than only in the loop below.
  assert.equal(byId.get("swcraft.build")?.description, "built 12:39");
  assert.equal(byId.get("swcraft.checkModel")?.description, "checked 12:39");
  assert.equal(byId.get("swcraft.exportComponent")?.description, "exported 12:39");
  assert.equal(byId.get("swcraft.importComponent")?.description, "imported 12:39");
  assert.equal(byId.get("swcraft.importRequirements")?.description, "imported 12:39");
  assert.equal(byId.get("swcraft.onboardProject")?.description, "onboarded 12:39");
  assert.equal(byId.get("swcraft.diagrams")?.description, "opened 12:39");

  for (const def of TREE_COMMAND_DEFINITIONS) {
    assert.equal(
      byId.get(def.commandId)?.description,
      `${def.verb} 12:39`,
      `${def.commandId} must render the verb declared beside its label`,
    );
  }
});

test("buildTree: every command renders the same 'failed <time>' text whatever its verb", () => {
  const statuses: Partial<Record<string, CommandStatus>> = Object.fromEntries(
    TREE_COMMAND_DEFINITIONS.map((def) => [def.commandId, justRan("failure", `${def.id} broke`)]),
  );
  for (const item of flatten(buildTree(statuses, NOW))) {
    assert.equal(item.description, "failed 12:39");
    assert.equal(item.tooltip, `${item.id} broke`);
  }
});

// --- the pure module never reads a clock -------------------------------------

test("buildTree: the same arguments render the same text across a real time gap", async (context) => {
  // The behavioural check that the module reads no clock. Its own limitation, stated: at
  // `HH:MM` resolution two calls milliseconds apart would agree even if a
  // clock *were* read, so this alone would not catch one — it is the window
  // comparison it really pins, which is computed from `now` and would drift
  // against a wall clock. Kept short deliberately: a gap long enough to be
  // conclusive on its own would be a 30-second unit test.
  const statuses: Partial<Record<string, CommandStatus>> = {
    "swcraft.build": justRan("success"),
  };
  const before = JSON.stringify(buildTree(statuses, NOW));
  await new Promise((resolve) => setTimeout(resolve, 25));
  assert.equal(JSON.stringify(buildTree(statuses, NOW)), before);

  // And the decisive form of the same property: a `now` far past the window
  // renders nothing while the wall clock has barely moved, so the rendering is
  // driven by the argument and cannot be driven by the real time.
  const wayLater = new Date(NOW.getTime() + 10 * STATUS_WINDOW_MS);
  const item = flatten(buildTree(statuses, wayLater)).find(
    (candidate) => candidate.commandId === "swcraft.build",
  );
  assert.equal(item?.description, undefined);

  // And the clock itself moved: `Date` is mocked to ten windows past NOW, so a
  // module reading `Date.now()` or `new Date()` instead of its `now` argument
  // would render the status as outside the window. What this does not reach: a
  // clock other than `Date` (`performance.now()`, say).
  context.mock.timers.enable({ apis: ["Date"], now: NOW.getTime() + 10 * STATUS_WINDOW_MS });
  assert.equal(JSON.stringify(buildTree(statuses, NOW)), before);
});

// --- the artifacts' state: absent, current, out of date ----------------------
//
// The panel marks an artifact rather than regenerating it on a save. Two artifacts, each shown
// on the item that generates it: the ARXML (the manifest's `outputs.arxml`) on Build, the
// views (`views_root/index.html`) on Diagrams. The rule is a function of three facts the
// adapter gathers — is the file on disk, when did its last successful generation start, and
// when did the model or the manifest last change. What this does NOT reach: whether the
// adapter gathers the facts right (a watcher event VS Code does not deliver, an mtime a tool
// forged), which is commands.ts's.

const T0 = 1_000_000;

test("decideArtifactState: no file on disk is absent, whatever was recorded", () => {
  // A fresh clone (nothing recorded), after `clean-outputs`, after a Build that
  // failed at check-model (its first step removed both roots): no file, absent.
  for (const lastSuccessAt of [undefined, T0, T0 + 10]) {
    for (const newestChangeAt of [undefined, T0 - 5, T0 + 5]) {
      assert.equal(
        decideArtifactState({ onDisk: false, lastSuccessAt, newestChangeAt }),
        "absent",
        `lastSuccessAt=${lastSuccessAt} newestChangeAt=${newestChangeAt}`,
      );
    }
  }
});

test("decideArtifactState: a file generated after the newest change is current", () => {
  assert.equal(
    decideArtifactState({ onDisk: true, lastSuccessAt: T0, newestChangeAt: T0 - 1 }),
    "current",
  );
  assert.equal(
    decideArtifactState({ onDisk: true, lastSuccessAt: T0, newestChangeAt: T0 }),
    "current",
    "a change stamped at the very instant the generation started was read by it",
  );
  assert.equal(
    decideArtifactState({ onDisk: true, lastSuccessAt: T0, newestChangeAt: undefined }),
    "current",
    "no change known (the activation scan has not answered yet) is no reason to mark",
  );
});

test("decideArtifactState: the model or the manifest changed after the last success — out of date", () => {
  assert.equal(
    decideArtifactState({ onDisk: true, lastSuccessAt: T0, newestChangeAt: T0 + 1 }),
    "outOfDate",
  );
});

test("decideArtifactState: a file on disk with no success recorded is out of date", () => {
  // A Build that failed at `validate` leaves the ARXML on disk with no success recorded; a
  // file written by a terminal build this workspace never saw is the same case. One rule,
  // erring on the safe side.
  assert.equal(
    decideArtifactState({ onDisk: true, lastSuccessAt: undefined, newestChangeAt: T0 }),
    "outOfDate",
  );
  assert.equal(
    decideArtifactState({ onDisk: true, lastSuccessAt: undefined, newestChangeAt: undefined }),
    "outOfDate",
  );
});

test("decideArtifactState: a failed Diagrams press over old views leaves them out of date", () => {
  // A failed press writes and removes nothing and records no success, so the
  // facts it leaves behind are the facts before it: old views, a newer change.
  const facts = { onDisk: true, lastSuccessAt: T0, newestChangeAt: T0 + 50 };
  assert.equal(decideArtifactState(facts), "outOfDate");
});

test("ARTIFACT_DEFINITIONS: the ARXML is Build's, the views are Diagrams'", () => {
  assert.deepEqual(
    ARTIFACT_DEFINITIONS.map((definition) => [definition.artifact, definition.commandId]),
    [
      ["arxml", "swcraft.build"],
      ["views", "swcraft.diagrams"],
    ],
  );
  const commandIds = new Set(TREE_COMMAND_DEFINITIONS.map((definition) => definition.commandId));
  for (const definition of ARTIFACT_DEFINITIONS) {
    assert.ok(commandIds.has(definition.commandId), definition.commandId);
  }
});

function itemOf(sections: ReturnType<typeof buildTree>, commandId: string) {
  const item = flatten(sections).find((candidate) => candidate.commandId === commandId);
  assert.ok(item, commandId);
  return item;
}

test("buildTree: an out-of-date artifact reads 'out of date' with the yellow dot and says what to press", () => {
  const tree = buildTree({}, NOW, { arxml: "outOfDate", views: "outOfDate" });
  const build = itemOf(tree, "swcraft.build");
  assert.equal(build.description, "out of date");
  assert.deepEqual(build.icon, OUT_OF_DATE_ICON);
  assert.match(build.tooltip ?? "", /ARXML/);
  assert.match(build.tooltip ?? "", /press Build/);
  const diagrams = itemOf(tree, "swcraft.diagrams");
  assert.equal(diagrams.description, "out of date");
  assert.deepEqual(diagrams.icon, OUT_OF_DATE_ICON);
  assert.match(diagrams.tooltip ?? "", /diagrams/);
  assert.match(diagrams.tooltip ?? "", /press Diagrams/);
});

test("OUT_OF_DATE_ICON: a filled dot in the theme's yellow", () => {
  assert.deepEqual(OUT_OF_DATE_ICON, { id: "circle-filled", color: "charts.yellow" });
});

test("buildTree: an absent artifact reads 'not generated' with the ordinary icon", () => {
  const tree = buildTree({}, NOW, { arxml: "absent", views: "absent" });
  for (const commandId of ["swcraft.build", "swcraft.diagrams"]) {
    const item = itemOf(tree, commandId);
    assert.equal(item.description, "not generated", commandId);
    assert.equal(item.icon, undefined, commandId);
  }
});

test("buildTree: a current artifact adds nothing, as before", () => {
  const tree = buildTree({}, NOW, { arxml: "current", views: "current" });
  for (const commandId of ["swcraft.build", "swcraft.diagrams"]) {
    const item = itemOf(tree, commandId);
    assert.equal(item.description, undefined, commandId);
    assert.equal(item.tooltip, undefined, commandId);
    assert.equal(item.icon, undefined, commandId);
  }
});

test("buildTree: no artifact state given renders exactly what it rendered before", () => {
  const statuses = { "swcraft.build": justRan("failure", "exit 1") };
  assert.deepEqual(buildTree(statuses, NOW, {}), buildTree(statuses, NOW));
  for (const item of flatten(buildTree(statuses, NOW))) {
    assert.equal(item.icon, undefined, item.commandId);
  }
  assert.equal(itemOf(buildTree(statuses, NOW), "swcraft.build").tooltip, "exit 1");
});

test("buildTree: the item's text is the last run while it shows, the icon the artifact's state", () => {
  // Two channels that do not mix: `failed 12:39` shows while the status window
  // keeps it, and `out of date` after; the tooltip carries both meanwhile.
  const statuses = { "swcraft.diagrams": justRan("failure", "SWCraft: Diagrams failed (exit 1).") };
  const during = itemOf(buildTree(statuses, NOW, { views: "outOfDate" }), "swcraft.diagrams");
  assert.equal(during.description, "failed 12:39");
  assert.deepEqual(during.icon, OUT_OF_DATE_ICON);
  assert.match(during.tooltip ?? "", /Diagrams failed \(exit 1\)/);
  assert.match(during.tooltip ?? "", /out of date/);

  const after = itemOf(
    buildTree(statuses, new Date(NOW.getTime() + STATUS_WINDOW_MS), { views: "outOfDate" }),
    "swcraft.diagrams",
  );
  assert.equal(after.description, "out of date");
  assert.deepEqual(after.icon, OUT_OF_DATE_ICON);

  const succeeded = itemOf(
    buildTree({ "swcraft.build": justRan("success") }, NOW, { arxml: "current" }),
    "swcraft.build",
  );
  assert.equal(succeeded.description, "built 12:39");
  assert.equal(succeeded.icon, undefined);
});

test("buildTree: an artifact state marks only its own item", () => {
  const tree = buildTree({}, NOW, { views: "outOfDate" });
  for (const item of flatten(tree)) {
    if (item.commandId === "swcraft.diagrams") {
      continue;
    }
    assert.equal(item.icon, undefined, item.commandId);
    assert.equal(item.description, undefined, item.commandId);
  }
});

// --- the badge: how many artifacts are out of date -----------------------------

test("describeBadge: nothing out of date hides the badge", () => {
  assert.equal(describeBadge({}), undefined);
  assert.equal(describeBadge({ arxml: "current", views: "current" }), undefined);
});

test("describeBadge: an absent artifact counts nothing", () => {
  // A fresh clone, or after clean-outputs: nobody is misled by a missing file.
  assert.equal(describeBadge({ arxml: "absent", views: "absent" }), undefined);
  assert.equal(describeBadge({ arxml: "absent", views: "current" }), undefined);
});

test("describeBadge: one out-of-date artifact is 1, and the tooltip names it", () => {
  const arxml = describeBadge({ arxml: "outOfDate", views: "current" });
  assert.equal(arxml?.value, 1);
  assert.match(arxml?.tooltip ?? "", /ARXML/);
  assert.doesNotMatch(arxml?.tooltip ?? "", /diagrams/);
  const views = describeBadge({ arxml: "absent", views: "outOfDate" });
  assert.equal(views?.value, 1);
  assert.match(views?.tooltip ?? "", /diagrams/);
  assert.doesNotMatch(views?.tooltip ?? "", /ARXML/);
});

test("describeBadge: both out of date is 2, the tooltip naming both", () => {
  const badge = describeBadge({ arxml: "outOfDate", views: "outOfDate" });
  assert.equal(badge?.value, 2);
  assert.match(badge?.tooltip ?? "", /ARXML/);
  assert.match(badge?.tooltip ?? "", /diagrams/);
});

test("describeBadge: a failed run counts nothing of its own — only the artifact's state does", () => {
  // The badge reads states and nothing else: `describeBadge` takes no statuses
  // at all, so a `failed 12:41` cannot reach it.
  assert.equal(describeBadge.length, 1);
  const states: Partial<Record<"arxml" | "views", ArtifactState>> = { arxml: "current" };
  assert.equal(describeBadge(states), undefined);
});

// --- a run in flight: running… (row 17) ----------------------------------------

test("RUNNING_TEXT: an item with a run in flight reads 'running…'", () => {
  assert.equal(RUNNING_TEXT, "running…");
});

test("buildTree: an item with a run in flight reads 'running…', and only that item", () => {
  const tree = buildTree({}, NOW, {}, new Set(["swcraft.build"]));
  assert.equal(itemOf(tree, "swcraft.build").description, "running…");
  for (const item of flatten(tree)) {
    if (item.commandId !== "swcraft.build") {
      assert.equal(item.description, undefined, item.commandId);
    }
  }
});

test("buildTree: a command with no artifact reads 'running…' too", () => {
  const item = itemOf(
    buildTree({}, NOW, {}, new Set(["swcraft.checkModel"])),
    "swcraft.checkModel",
  );
  assert.equal(item.description, "running…");
});

test("buildTree: 'running…' replaces a shown status and the artifact's words, never its mark", () => {
  // The text is the run; the icon and the badge stay the artifact's state.
  const statuses = { "swcraft.diagrams": justRan("failure", "SWCraft: Diagrams failed (exit 1).") };
  const item = itemOf(
    buildTree(statuses, NOW, { views: "outOfDate" }, new Set(["swcraft.diagrams"])),
    "swcraft.diagrams",
  );
  assert.equal(item.description, "running…");
  assert.deepEqual(item.icon, OUT_OF_DATE_ICON);
  assert.doesNotMatch(
    item.tooltip ?? "",
    /Diagrams failed/,
    "the previous run's failure is not this run's",
  );
  assert.match(item.tooltip ?? "", /running/i);
  assert.match(item.tooltip ?? "", /out of date/, "the artifact's state stays in the tooltip");
});

test("buildTree: the sequence out of date → running → current or failed holds", () => {
  const running = new Set(["swcraft.build"]);
  const before = itemOf(buildTree({}, NOW, { arxml: "outOfDate" }), "swcraft.build");
  assert.equal(before.description, "out of date");

  const during = itemOf(buildTree({}, NOW, { arxml: "outOfDate" }, running), "swcraft.build");
  assert.equal(during.description, "running…");

  // The outcome replaces it: the running set no longer holds the command.
  const succeeded = itemOf(
    buildTree({ "swcraft.build": justRan("success") }, NOW, { arxml: "current" }, new Set()),
    "swcraft.build",
  );
  assert.equal(succeeded.description, "built 12:39");
  assert.equal(succeeded.icon, undefined);

  const failed = itemOf(
    buildTree({ "swcraft.build": justRan("failure", "exit 1") }, NOW, { arxml: "outOfDate" }),
    "swcraft.build",
  );
  assert.equal(failed.description, "failed 12:39");
  assert.deepEqual(failed.icon, OUT_OF_DATE_ICON);
});

test("buildTree: an empty running set renders exactly what it rendered before", () => {
  const statuses = { "swcraft.build": justRan("success") };
  assert.deepEqual(
    buildTree(statuses, NOW, { arxml: "current" }, new Set()),
    buildTree(statuses, NOW, { arxml: "current" }),
  );
});

// --- RunningCommands: a run in flight, counted per id ----------------------------

test("RunningCommands: an id stays running until the last of its overlapping runs finishes", () => {
  const running = new RunningCommands();
  assert.equal(running.has("swcraft.diagrams"), false);
  running.start("swcraft.diagrams");
  running.start("swcraft.diagrams");
  running.finish("swcraft.diagrams");
  assert.equal(
    running.has("swcraft.diagrams"),
    true,
    "the second press is still queued or running",
  );
  running.finish("swcraft.diagrams");
  assert.equal(running.has("swcraft.diagrams"), false);
  running.finish("swcraft.diagrams");
  assert.equal(running.has("swcraft.diagrams"), false, "an extra finish never goes negative");
  running.start("swcraft.diagrams");
  assert.equal(running.has("swcraft.diagrams"), true);
});

test("buildTree: reads a RunningCommands as it reads a set", () => {
  const running = new RunningCommands();
  running.start("swcraft.build");
  const item = buildTree({}, new Date(0), {}, running)
    .flatMap((section) => section.items)
    .find((entry) => entry.commandId === "swcraft.build");
  assert.equal(item?.description, RUNNING_TEXT);
});

// --- ArtifactRecord: the transitions behind the states ---------------------------

/** A memento over a plain map, and an ArtifactRecord whose scans the test resolves by hand. */
function recordUnderTest(): {
  record: ArtifactRecord;
  store: Map<string, unknown>;
  scans: { folder: string; resolve: (value: number | undefined) => void }[];
  changes: () => number;
} {
  const store = new Map<string, unknown>();
  const memento = {
    get<T>(key: string): T | undefined {
      return store.get(key) as T | undefined;
    },
    update(key: string, value: unknown): void {
      if (value === undefined) {
        store.delete(key);
      } else {
        store.set(key, value);
      }
    },
  };
  const scans: { folder: string; resolve: (value: number | undefined) => void }[] = [];
  let changed = 0;
  const record = new ArtifactRecord(
    memento,
    (folder) =>
      new Promise((resolve) => {
        scans.push({ folder, resolve });
      }),
    () => {
      changed += 1;
    },
  );
  return { record, store, scans, changes: () => changed };
}

const BOTH_ON_DISK = { arxml: true, views: true };

test("ArtifactRecord: a change noted while a rescan runs is kept, the scan folded in by the maximum", async () => {
  const { record, scans } = recordUnderTest();
  record.recordGeneration("arxml", "/w", 1_000);
  const rescan = record.rescan("/w");
  record.changeStamp(2_000)(2_000);
  scans[0].resolve(500);
  await rescan;
  assert.equal(record.states("/w", BOTH_ON_DISK).arxml, "outOfDate", "the edit at 2000 survived");
});

test("ArtifactRecord: a scan and a stamp for a folder no longer shown are dropped", async () => {
  const { record, scans } = recordUnderTest();
  record.recordGeneration("arxml", "/small", 1_000);
  const stale = record.changeStamp(900);
  const first = record.rescan("/large");
  const second = record.rescan("/small");
  scans[1].resolve(500);
  await second;
  scans[0].resolve(9_000);
  await first;
  stale(9_000);
  assert.equal(record.states("/small", BOTH_ON_DISK).arxml, "current");
});

test("ArtifactRecord: a Build writes no success for an artifact its run did not generate", () => {
  const { record, store } = recordUnderTest();
  record.recordBuild("/w", 1_000, { arxml: true, views: true });
  record.recordBuild("/w", 2_000, { arxml: false, views: true });
  assert.equal(
    store.get(lastGenerationKey("arxml", "/w")),
    undefined,
    "the ARXML's success is gone",
  );
  assert.equal(store.get(lastGenerationKey("views", "/w")), 2_000);
  assert.deepEqual(record.states("/w", BOTH_ON_DISK), { arxml: "outOfDate", views: "current" });
});

test("ArtifactRecord: a success is kept per folder", () => {
  const { record } = recordUnderTest();
  record.recordGeneration("views", "/a", 1_000);
  assert.equal(record.states("/a", BOTH_ON_DISK).views, "current");
  assert.equal(record.states("/b", BOTH_ON_DISK).views, "outOfDate", "no success recorded in /b");
  assert.notEqual(lastGenerationKey("views", "/a"), lastGenerationKey("views", "/b"));
});

test("ArtifactRecord: a stamp that moves nothing redraws nothing", async () => {
  const { record, scans, changes } = recordUnderTest();
  const rescan = record.rescan("/w");
  scans[0].resolve(5_000);
  await rescan;
  const before = changes();
  record.changeStamp(4_000)(4_000);
  assert.equal(changes(), before, "an older change leaves the newest where it was");
  record.changeStamp(6_000)(6_000);
  assert.equal(changes(), before + 1);
});

// --- ArtifactRecord.opensDirectly: the in-window half the diagram lens reads ------

test("ArtifactRecord.opensDirectly: a memento success alone is not enough (the reload case)", () => {
  const { record, store } = recordUnderTest();
  store.set(lastGenerationKey("views", "/w"), 1_000);
  assert.equal(
    record.states("/w", BOTH_ON_DISK).views,
    "current",
    "the persisted half says current",
  );
  assert.equal(
    record.opensDirectly("views", "/w", BOTH_ON_DISK),
    false,
    "no generation in this window",
  );
});

test("ArtifactRecord.opensDirectly: true after a generation in this window with no later arrival", () => {
  const { record } = recordUnderTest();
  record.changeStamp(500)(500);
  record.recordGeneration("views", "/w", 1_000);
  assert.equal(record.opensDirectly("views", "/w", BOTH_ON_DISK), true);
  assert.equal(record.opensDirectly("views", "/other", BOTH_ON_DISK), false, "per folder");
  assert.equal(record.opensDirectly("arxml", "/w", BOTH_ON_DISK), false, "per artifact");
});

test("ArtifactRecord.opensDirectly: an arrival at or after the start counts, whatever mtime it carries", () => {
  for (const arrivedAt of [1_000, 1_500]) {
    const { record } = recordUnderTest();
    record.recordGeneration("views", "/w", 1_000);
    // The file's mtime is older than the generation, so the persisted half still
    // reads current: only the arrival instant decides.
    record.changeStamp(arrivedAt)(10);
    assert.equal(record.states("/w", BOTH_ON_DISK).views, "current");
    assert.equal(
      record.opensDirectly("views", "/w", BOTH_ON_DISK),
      false,
      `arrival at ${arrivedAt}`,
    );
  }
  const { record } = recordUnderTest();
  record.recordGeneration("views", "/w", 1_000);
  const stamp = record.changeStamp(1_200);
  assert.equal(
    record.opensDirectly("views", "/w", BOTH_ON_DISK),
    false,
    "the arrival counts before its stamp is called",
  );
  stamp(1_200);
});

test("ArtifactRecord.opensDirectly: an arrival raises no redraw", () => {
  const { record, changes } = recordUnderTest();
  record.recordGeneration("views", "/w", 1_000);
  const before = changes();
  record.changeStamp(2_000);
  assert.equal(changes(), before, "recording an arrival changes nothing the tree draws");
});

test("ArtifactRecord.opensDirectly: a rescan clears the in-window record and the arrival", async () => {
  const { record, scans } = recordUnderTest();
  record.recordGeneration("views", "/w", 1_000);
  const rescan = record.rescan("/w");
  scans[0].resolve(500);
  await rescan;
  assert.equal(record.states("/w", BOTH_ON_DISK).views, "current");
  assert.equal(
    record.opensDirectly("views", "/w", BOTH_ON_DISK),
    false,
    "the generation is forgotten",
  );
  record.changeStamp(1_500)(400);
  const second = record.rescan("/w");
  scans[1].resolve(400);
  await second;
  record.recordGeneration("views", "/w", 1_000);
  assert.equal(
    record.opensDirectly("views", "/w", BOTH_ON_DISK),
    true,
    "the arrival before the rescan is forgotten too",
  );
});

test("ArtifactRecord.opensDirectly: false when the views are out of date or absent", () => {
  const { record } = recordUnderTest();
  record.recordGeneration("views", "/w", 1_000);
  record.changeStamp(500)(2_000);
  assert.equal(record.states("/w", BOTH_ON_DISK).views, "outOfDate");
  assert.equal(record.opensDirectly("views", "/w", BOTH_ON_DISK), false, "out of date");
  const fresh = recordUnderTest().record;
  fresh.recordGeneration("views", "/w", 1_000);
  assert.equal(fresh.opensDirectly("views", "/w", { arxml: true, views: false }), false, "absent");
});

test("ArtifactRecord.opensDirectly: a Build records the views it generated, and clears those it did not", () => {
  const { record, store } = recordUnderTest();
  record.recordBuild("/w", 1_000, { arxml: true, views: true });
  assert.equal(record.opensDirectly("views", "/w", BOTH_ON_DISK), true, "the Build generated them");
  record.recordGeneration("views", "/w", 1_500);
  record.recordBuild("/w", 2_000, { arxml: true, views: false });
  // The Build also cleared the memento's success; put it back, so only the
  // in-window record can answer.
  store.set(lastGenerationKey("views", "/w"), 1_500);
  assert.equal(record.states("/w", BOTH_ON_DISK).views, "current");
  assert.equal(
    record.opensDirectly("views", "/w", BOTH_ON_DISK),
    false,
    "a Build that did not generate them clears the in-window record",
  );
});

test("ArtifactRecord.opensDirectly: a refused generation forgets the in-window record", () => {
  const { record } = recordUnderTest();
  record.recordGeneration("views", "/w", 1_000);
  assert.equal(record.opensDirectly("views", "/w", BOTH_ON_DISK), true);
  const before = record.states("/w", BOTH_ON_DISK);
  record.forgetWindowGeneration("views", "/w");
  assert.equal(
    record.opensDirectly("views", "/w", BOTH_ON_DISK),
    false,
    "a run that failed may have left views_root partly rewritten",
  );
  assert.deepEqual(record.states("/w", BOTH_ON_DISK), before, "the persisted half is untouched");
  record.recordGeneration("views", "/w", 2_000);
  assert.equal(
    record.opensDirectly("views", "/w", BOTH_ON_DISK),
    true,
    "the next success records again",
  );
});

test("the Model section's Resource Report item is unchanged by the Resources view", () => {
  // The item fills the Resources view; its label, command, section and verb stay as they are.
  assert.deepEqual(
    TREE_COMMAND_DEFINITIONS.find((def) => def.id === "resourceReport"),
    {
      id: "resourceReport",
      label: "Resource Report",
      commandId: "swcraft.resourceReport",
      section: "model",
      verb: "reported",
    },
  );
});

test("the Model section's Architecture Metrics item sits under Resource Report, read-only", () => {
  // One click, no question, no view of its own: it opens the report it wrote.
  assert.deepEqual(
    TREE_COMMAND_DEFINITIONS.find((def) => def.id === "metrics"),
    {
      id: "metrics",
      label: "Architecture Metrics",
      commandId: "swcraft.metrics",
      section: "model",
      verb: "reported",
    },
  );
  const ids = TREE_COMMAND_DEFINITIONS.map((def) => def.commandId);
  assert.equal(ids.indexOf("swcraft.metrics"), ids.indexOf("swcraft.resourceReport") + 1);
  const statuses = { "swcraft.metrics": justRan("success") };
  const item = itemOf(buildTree(statuses, NOW, {}), "swcraft.metrics");
  assert.match(item.description ?? "", /^reported /);
});

// --- the Model section's Resource Report item follows Build and Diagrams ---------

test("decideResourceReportState: none before a report is shown, then the artifacts' rule", () => {
  const facts = (readAt: number | undefined, newestChangeAt: number | undefined) => ({
    readAt,
    newestChangeAt,
  });
  assert.equal(decideResourceReportState(facts(undefined, 5000)), undefined, "none shown");
  assert.equal(decideResourceReportState(facts(1000, undefined)), "current", "no change seen");
  // A change before the run started is read by it; one at the very instant counts as read.
  assert.equal(decideResourceReportState(facts(1000, 900)), "current");
  assert.equal(decideResourceReportState(facts(1000, 1000)), "current");
  // A change made while the run was in flight survives the run that ends after it.
  assert.equal(decideResourceReportState(facts(1000, 1001)), "outOfDate");
  assert.equal(isOutOfDate(decideResourceReportState(facts(1000, 1001))), true);
  assert.equal(isOutOfDate(decideResourceReportState(facts(1000, 900))), false);
  assert.equal(isOutOfDate(undefined), false, "nothing shown, nothing marked");
});

test("MARKED_DEFINITIONS: the two artifacts, then the resource report on Resource Report's item", () => {
  assert.deepEqual(
    MARKED_DEFINITIONS.map((definition) => [definition.artifact, definition.commandId]),
    [
      ["arxml", "swcraft.build"],
      ["views", "swcraft.diagrams"],
      ["resources", "swcraft.resourceReport"],
    ],
  );
});

test("buildTree: Resource Report before any run shows nothing", () => {
  const item = itemOf(buildTree({}, NOW, {}), "swcraft.resourceReport");
  assert.equal(item.description, undefined);
  assert.equal(item.tooltip, undefined);
  assert.equal(item.icon, undefined);
});

test("buildTree: Resource Report after a run reads 'reported HH:MM', the report current", () => {
  const statuses = { "swcraft.resourceReport": justRan("success") };
  const item = itemOf(buildTree(statuses, NOW, { resources: "current" }), "swcraft.resourceReport");
  assert.equal(item.description, "reported 12:39");
  assert.equal(item.tooltip, undefined);
  assert.equal(item.icon, undefined);
  const later = itemOf(
    buildTree(statuses, new Date(NOW.getTime() + STATUS_WINDOW_MS), { resources: "current" }),
    "swcraft.resourceReport",
  );
  assert.equal(later.description, undefined, "a current report adds nothing after the window");
});

test("buildTree: a failed Resource Report reads what a failed Build reads", () => {
  const message = "SWCraft: Resource Report failed (exit 1).";
  const report = itemOf(
    buildTree({ "swcraft.resourceReport": justRan("failure", message) }, NOW, {
      resources: "current",
    }),
    "swcraft.resourceReport",
  );
  const build = itemOf(
    buildTree({ "swcraft.build": justRan("failure", message) }, NOW, { arxml: "current" }),
    "swcraft.build",
  );
  assert.equal(report.description, "failed 12:39");
  assert.equal(report.description, build.description);
  assert.equal(report.tooltip, message);
  assert.equal(report.icon, undefined);
});

test("buildTree: an out-of-date resource report marks Resource Report as Build is marked", () => {
  const item = itemOf(buildTree({}, NOW, { resources: "outOfDate" }), "swcraft.resourceReport");
  assert.equal(item.description, "out of date");
  assert.deepEqual(item.icon, OUT_OF_DATE_ICON);
  assert.match(item.tooltip ?? "", /The resource report is out of date/);
  assert.match(item.tooltip ?? "", /press Resource Report/);
  for (const other of flatten(buildTree({}, NOW, { resources: "outOfDate" }))) {
    if (other.commandId !== "swcraft.resourceReport") {
      assert.equal(other.icon, undefined, other.commandId);
    }
  }
});

test("describeBadge: the out-of-date resource report counts with the artifacts", () => {
  assert.equal(
    describeBadge({ arxml: "current", views: "current", resources: "current" }),
    undefined,
  );
  const alone = describeBadge({ arxml: "current", views: "current", resources: "outOfDate" });
  assert.equal(alone?.value, 1);
  assert.match(alone?.tooltip ?? "", /the resource report \(press Resource Report\)/);
  const without = describeBadge({ arxml: "outOfDate", views: "outOfDate", resources: "current" });
  assert.equal(without?.value, 2);
  assert.doesNotMatch(without?.tooltip ?? "", /resource report/);
  const all = describeBadge({ arxml: "outOfDate", views: "outOfDate", resources: "outOfDate" });
  assert.equal(all?.value, 3);
  assert.equal(
    all?.tooltip,
    "Out of date: the ARXML (press Build), the diagrams (press Diagrams), " +
      "the resource report (press Resource Report).",
  );
});

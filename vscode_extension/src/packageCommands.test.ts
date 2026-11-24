// Sync guard: every command id `commands.ts`'s `registerCommands()` registers must be declared in
// package.json's `contributes.commands`, and vice versa. A handler with no manifest entry never
// appears in the Command Palette; a manifest entry with no handler appears and errors when invoked.
// Neither drift produces any other signal.
//
// commands.ts imports "vscode", so it cannot be imported under plain `node --test`; as in
// treeViewId.test.ts, it is read as raw text and the registered ids are regex-extracted.
// treeView.ts's TREE_COMMAND_DEFINITIONS is vscode-free and imported directly.

import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as path from "node:path";
import { test } from "node:test";

import { DIAGRAM_EDITOR_COMMANDS } from "./diagramsTab";
import { IMPACT_VIEW_COMMANDS } from "./impactCore";
import { MANIFEST_FILENAME } from "./project";
import { RESOURCE_VIEW_COMMANDS } from "./resourceCore";
import {
  REVIEW_OPEN_COMMAND,
  REVIEW_REFRESH_COMMAND,
  REVIEW_VIEW_COMMANDS,
  REVIEW_VIEW_ID,
} from "./reviewCore";
import { functionBody, readManifest, readSource, withoutCommentLines } from "./testSupport";
import { TRACE_VIEW_COMMANDS } from "./traceCore";
import { PANEL_MENU_COMMANDS, TREE_COMMAND_DEFINITIONS } from "./treeView";

// tsc emits src/**/*.ts -> out/ (rootDir: src), so this compiled test lives
// at out/packageCommands.test.js; src/ and package.json are one level up.
const SRC_DIR = path.join(__dirname, "..", "src");
const PACKAGE_JSON_PATH = path.join(__dirname, "..", "package.json");

function manifestCommands(): Array<{ command: string; title: string; category?: string }> {
  const pkg = JSON.parse(fs.readFileSync(PACKAGE_JSON_PATH, "utf8")) as {
    contributes: { commands: Array<{ command: string; title: string; category?: string }> };
  };
  return pkg.contributes.commands;
}

/** Every command the SWCraft panel reaches: its tree's items and its `…` menu's entries. */
const PANEL_COMMAND_IDS: readonly string[] = [
  ...TREE_COMMAND_DEFINITIONS.map((def) => def.commandId),
  ...PANEL_MENU_COMMANDS.map((entry) => entry.commandId),
];

/** Every command id `commands.ts` registers. */
function registeredCommandIds(): string[] {
  const content = withoutCommentLines(fs.readFileSync(path.join(SRC_DIR, "commands.ts"), "utf8"));
  const ids: string[] = [];
  const pattern = /registerCommand\(\s*"([^"]+)"/g;
  let match = pattern.exec(content);
  while (match !== null) {
    ids.push(match[1]);
    match = pattern.exec(content);
  }
  return ids;
}

test("the retired counted-references lens command is declared, registered and shown nowhere", () => {
  // The *references* lens carries no count, so its click is Find All References and no lens-only
  // command lists a counted capture: not in the registrations, contributes.commands or the panel.
  const everywhere = [
    ...manifestCommands().map((cmd) => cmd.command),
    ...TREE_COMMAND_DEFINITIONS.map((def) => def.commandId),
    ...registeredCommandIds(),
  ];
  assert.equal(everywhere.includes("swcraft.showCountedReferences"), false);
  assert.equal(
    readSource("src/commands.ts").includes("swcraft.showCountedReferences"),
    false,
    "commands.ts still names the retired lens command",
  );
});

/**
 * The smallest number of activation events the manifest may declare: one per kind
 * the test knows (`onCommand:`, `onUri`, `workspaceContains:`), so an emptied list
 * fails rather than passing vacuously.
 */
const ACTIVATION_EVENT_FLOOR = 3;

test("every activation event package.json declares is one the code handles", () => {
  // An `onCommand:` event wakes the extension for a command it registers, `onUri` for the URI
  // handler `registerCommands()` registers, `workspaceContains:` for the manifest file name; any
  // other kind is new. What it does not reach: that the activation path reads that manifest (the
  // event is compared with MANIFEST_FILENAME only), and the call's spelling in a string literal or a
  // trailing `//` comment of that body, which still counts.
  const declared = readManifest().activationEvents ?? [];
  assert.ok(
    declared.length >= ACTIVATION_EVENT_FLOOR,
    `package.json declares only ${declared.length} activation events`,
  );
  const contributed = new Set(manifestCommands().map((cmd) => cmd.command));
  const registered = new Set(registeredCommandIds());
  const registering = functionBody(
    withoutCommentLines(readSource("src/commands.ts")),
    "export function registerCommands(",
  );
  const unhandled = declared.filter((event) => {
    if (event.startsWith("onCommand:")) {
      const id = event.slice("onCommand:".length);
      return !(contributed.has(id) && registered.has(id));
    }
    if (event === "onUri") {
      return !registering.includes("vscode.window.registerUriHandler(");
    }
    if (event.startsWith("workspaceContains:")) {
      return event.slice("workspaceContains:".length) !== MANIFEST_FILENAME;
    }
    return true;
  });
  assert.deepEqual(
    unhandled,
    [],
    `activation events the code does not handle: ${unhandled.join(", ")}`,
  );
});

test("package.json declares swcraft.exportComponent in contributes.commands", () => {
  const entry = manifestCommands().find((cmd) => cmd.command === "swcraft.exportComponent");
  assert.ok(
    entry,
    "package.json's contributes.commands must declare swcraft.exportComponent — " +
      "a command handler with no manifest entry never appears in the Command Palette",
  );
  assert.match(entry.title, /Export Component/);
});

test("package.json declares the allocation experiment under the title the palette shows", () => {
  const entry = manifestCommands().find((cmd) => cmd.command === "swcraft.experimentAllocation");
  assert.ok(entry, "package.json's contributes.commands must declare swcraft.experimentAllocation");
  assert.match(entry.title, /^SWCraft: Allocation Experiment \(CARMA\)$/);
});

test("package.json's contributes.commands and commands.ts's registerCommands() declare the same command ids", () => {
  const manifest = manifestCommands()
    .map((cmd) => cmd.command)
    .sort();
  const registered = [...registeredCommandIds()].sort();
  assert.ok(registered.length > 0, "expected registerCommand(...) calls in commands.ts");
  assert.deepEqual(
    manifest,
    registered,
    "contributes.commands (package.json) and registerCommands() (commands.ts) must stay in sync",
  );
});

test("every TreeView command item invokes a command declared in package.json", () => {
  const manifest = new Set(manifestCommands().map((cmd) => cmd.command));
  for (const def of TREE_COMMAND_DEFINITIONS) {
    assert.ok(
      manifest.has(def.commandId),
      `TreeView item '${def.id}' invokes '${def.commandId}', which package.json does not declare`,
    );
  }
});

// The other direction: a declared command no panel item reaches is a failure, not a silent
// omission. With the deepEqual above the three id lists are one set: declared, registered and
// reachable from the SWCraft panel. A view's own commands are reached from its menus, each list
// held to the manifest below: `TRACE_VIEW_COMMANDS`, `PANEL_MENU_COMMANDS`, `REVIEW_VIEW_COMMANDS`,
// `RESOURCE_VIEW_COMMANDS` and `IMPACT_VIEW_COMMANDS` (`swcraft.impact` among them, reached from
// the panel by a dependent row's inline action). The one command reached from the editor alone is
// `DIAGRAM_EDITOR_COMMANDS` (Show in Diagram), the *diagram* lens on every element the diagrams'
// index lists: it needs an element and a cursor, which the panel cannot supply. A command in none
// of the seven still fails.
test("every command declared in package.json is reachable from the SWCraft panel or one of its views", () => {
  const reachable = new Set([
    ...PANEL_COMMAND_IDS,
    ...TRACE_VIEW_COMMANDS,
    ...REVIEW_VIEW_COMMANDS,
    ...RESOURCE_VIEW_COMMANDS,
    ...IMPACT_VIEW_COMMANDS,
    ...DIAGRAM_EDITOR_COMMANDS,
  ]);
  for (const cmd of manifestCommands()) {
    assert.ok(
      reachable.has(cmd.command),
      `package.json declares '${cmd.command}', which neither the panel's tree nor its \`…\` menu ` +
        "nor the Trace Links, Pending Change Sets, Resources or Impact view reaches — it would " +
        "exist only in the Command Palette (or an editor menu)",
    );
  }
});

test("package.json, registerCommands() and the TreeView name exactly the same commands", () => {
  // The count is derived, not restated: a literal would be a fourth declaration of a fact the three
  // lists carry, stale on the next command added or removed. What is asserted is the relation: the
  // three lists are one set, every id is unique, and there is at least one, so the comparison is
  // not vacuous over three empty arrays.
  const manifest = manifestCommands()
    .map((cmd) => cmd.command)
    .sort();
  const tree = [
    ...PANEL_COMMAND_IDS,
    ...TRACE_VIEW_COMMANDS,
    ...REVIEW_VIEW_COMMANDS,
    ...RESOURCE_VIEW_COMMANDS,
    ...IMPACT_VIEW_COMMANDS,
    ...DIAGRAM_EDITOR_COMMANDS,
  ].sort();
  const registered = [...registeredCommandIds()].sort();
  assert.ok(manifest.length > 0, "package.json must declare at least one command");
  assert.deepEqual(
    manifest,
    tree,
    "package.json and the panel's tree or its `…` menu (or the Trace Links, Pending Change " +
      "Sets, Resources or Impact view) must name the same commands",
  );
  assert.deepEqual(manifest, registered);
  assert.equal(new Set(manifest).size, manifest.length, "command ids must be unique");
  for (const command of manifest) {
    assert.match(command, /^swcraft\./, "every command id is namespaced to this extension");
  }
});

test("the retired MCP config commands are declared, registered and shown nowhere", () => {
  // `SWCraft: Open MCP Config` / `SWCraft: Repair MCP Config` managed a generated
  // `.vscode/mcp.json`; the extension registers the MCP server itself (`mcpProvider.ts`). The
  // deepEqual above catches a leftover in exactly one list; naming them catches one in all three.
  const everywhere = [
    ...manifestCommands().map((cmd) => cmd.command),
    ...TREE_COMMAND_DEFINITIONS.map((def) => def.commandId),
    ...registeredCommandIds(),
  ];
  for (const retired of ["swcraft.openMcpConfig", "swcraft.repairMcpConfig"]) {
    assert.equal(everywhere.includes(retired), false, `${retired} is back`);
  }
});

test("the three retired pipeline commands are declared, registered and shown nowhere", () => {
  // The panel names the want, not the pipeline step: `Generate C4` is inside `Diagrams` and
  // `Build`, `Validate` inside `Build` (the terminal keeps `swcraft validate`), and no `.c4` file
  // exists for `Open Generated C4` to open. Named for the same reason as the MCP guard above.
  const everywhere = [
    ...manifestCommands().map((cmd) => cmd.command),
    ...TREE_COMMAND_DEFINITIONS.map((def) => def.commandId),
    ...registeredCommandIds(),
  ];
  for (const retired of ["swcraft.validate", "swcraft.generateC4", "swcraft.openGeneratedC4"]) {
    assert.equal(everywhere.includes(retired), false, `${retired} is back`);
  }
  const titles = manifestCommands().map((cmd) => cmd.title);
  for (const retired of [
    "SWCraft: Validate",
    "SWCraft: Generate C4",
    "SWCraft: Open Generated C4",
  ]) {
    assert.equal(titles.includes(retired), false, `the Command Palette offers '${retired}' again`);
  }
});

test("the retired Diff command is declared, registered and shown nowhere", () => {
  // Change Report took the Diff item's place, so `swcraft.diff` is in none of the three lists (the
  // set equality catches only a partial leftover). The `swcraft diff` CLI command stays.
  const everywhere = [
    ...manifestCommands().map((cmd) => cmd.command),
    ...TREE_COMMAND_DEFINITIONS.map((def) => def.commandId),
    ...registeredCommandIds(),
  ];
  assert.equal(everywhere.includes("swcraft.diff"), false, "swcraft.diff is back");
  const titles = manifestCommands().map((cmd) => cmd.title);
  assert.equal(titles.includes("SWCraft: Diff"), false, "the Command Palette offers Diff again");
  assert.equal(
    TREE_COMMAND_DEFINITIONS.some((def) => def.label === "Diff"),
    false,
    "the panel lists Diff again",
  );
});

test("package.json declares Build and Diagrams with their titles", () => {
  const byId = new Map(manifestCommands().map((cmd) => [cmd.command, cmd.title]));
  const expected: Array<[string, RegExp]> = [
    ["swcraft.build", /^SWCraft: Build$/],
    ["swcraft.diagrams", /^SWCraft: Diagrams$/],
  ];
  for (const [command, titlePattern] of expected) {
    const title = byId.get(command);
    assert.ok(title !== undefined, `package.json must declare ${command}`);
    assert.match(title, titlePattern);
  }
});

test("package.json declares Test Tables with its title, after Diagrams and Show in Diagram", () => {
  // The tables are artifacts like the diagrams, opened by one item next to Diagrams. The tab's
  // Generate and Change… are messages to the adapter, not commands, so nothing exists only in the
  // tab. In the manifest Show in Diagram directly follows Diagrams (its own test below), so Test
  // Tables follows it; the tree item stays directly after Diagrams.
  const commands = manifestCommands();
  const at = commands.findIndex((cmd) => cmd.command === "swcraft.testTables");
  assert.notEqual(at, -1, "package.json must declare swcraft.testTables");
  assert.match(commands[at].title, /^SWCraft: Test Tables$/);
  assert.equal(commands[at - 1]?.command, "swcraft.showInDiagram");
  assert.equal(commands[at - 2]?.command, "swcraft.diagrams");
});

test("package.json declares the editor-tooling health command with its title", () => {
  // A bare title and the category: the panel's `…` menu shows the title as it is, and the palette
  // prefixes the category, `SWCraft: Check Editor Tooling`.
  const entry = manifestCommands().find((cmd) => cmd.command === "swcraft.checkEditorTooling");
  assert.ok(entry !== undefined, "package.json must declare swcraft.checkEditorTooling");
  assert.match(entry.title, /^Check Editor Tooling$/);
  assert.equal(entry.category, "SWCraft");
});

test("package.json declares the 0.5.1 Check Model command with its title", () => {
  const byId = new Map(manifestCommands().map((cmd) => [cmd.command, cmd.title]));
  const title = byId.get("swcraft.checkModel");
  assert.ok(title !== undefined, "package.json must declare swcraft.checkModel");
  assert.match(title, /^SWCraft: Check Model$/);
});

test("package.json declares the guide command with its title", () => {
  // The panel menu's entry that runs `swcraft docs`. The title is pinned so the palette entry cannot
  // drift from the label the menu and the walkthrough use; it is bare and the category carries the
  // prefix: the menu shows `Open Guide`, the palette `SWCraft: Open Guide`.
  const entry = manifestCommands().find((cmd) => cmd.command === "swcraft.openGuide");
  assert.ok(entry !== undefined, "package.json must declare swcraft.openGuide");
  assert.match(entry.title, /^Open Guide$/);
  assert.equal(entry.category, "SWCraft");
});

test("package.json declares the import command with its title", () => {
  // The title ends with an ellipsis: the command asks for a file before it runs.
  const byId = new Map(manifestCommands().map((cmd) => [cmd.command, cmd.title]));
  const title = byId.get("swcraft.importRequirements");
  assert.ok(title !== undefined, "package.json must declare swcraft.importRequirements");
  assert.match(title, /^SWCraft: Import Requirements$/);
});

test("package.json declares the component import command with its title", () => {
  const byId = new Map(manifestCommands().map((cmd) => [cmd.command, cmd.title]));
  const title = byId.get("swcraft.importComponent");
  assert.ok(title !== undefined, "package.json must declare swcraft.importComponent");
  assert.match(title, /^SWCraft: Import Component$/);
  // Declared directly after Export Component, the panel's order.
  const ids = manifestCommands().map((cmd) => cmd.command);
  assert.equal(ids.indexOf("swcraft.importComponent"), ids.indexOf("swcraft.exportComponent") + 1);
});

test("package.json declares the four read-only commands with their titles", () => {
  const byId = new Map(manifestCommands().map((cmd) => [cmd.command, cmd.title]));
  const expected: Array<[string, RegExp]> = [
    ["swcraft.changeReport", /^SWCraft: Change Report$/],
    ["swcraft.impact", /^SWCraft: Impact of This Element$/],
    ["swcraft.metrics", /^SWCraft: Architecture Metrics$/],
    ["swcraft.resourceReport", /^SWCraft: Resource Report$/],
  ];
  for (const [command, titlePattern] of expected) {
    const title = byId.get(command);
    assert.ok(title !== undefined, `package.json must declare ${command}`);
    assert.match(title, titlePattern);
  }
});

/** The view's menus and the palette's, typed as far as the view-menu test reads them. */
interface MenuEntry {
  command: string;
  when?: string;
  group?: string;
}

function traceMenuProblems(manifest: {
  contributes: { menus?: Record<string, MenuEntry[]> };
}): string[] {
  const menus = manifest.contributes.menus ?? {};
  const problems: string[] = [];
  for (const id of TRACE_VIEW_COMMANDS) {
    const inView = ["view/title", "view/item/context"].some((menu) =>
      (menus[menu] ?? []).some(
        (entry) =>
          entry.command === id &&
          ((entry.when ?? "").includes("view == swcraftTrace") ||
            ((entry.when ?? "").includes("view == swcraftView") &&
              (entry.when ?? "").includes("viewItem == trace")) ||
            ((entry.when ?? "").includes("view == swcraftView") &&
              (entry.when ?? "").includes("viewItem =~ /^trace"))),
      ),
    );
    if (!inView) {
      problems.push(`${id} is in no menu of the Trace Links view or of a trace node of the panel`);
    }
    const hidden = (menus.commandPalette ?? []).some(
      (entry) => entry.command === id && entry.when === "false",
    );
    if (!hidden) {
      problems.push(`${id} is not hidden from the Command Palette`);
    }
  }
  return problems;
}

test("each trace command is in the Trace Links view's menus or a panel trace node's, and hidden from the palette", () => {
  const manifest = JSON.parse(fs.readFileSync(PACKAGE_JSON_PATH, "utf8"));
  assert.deepEqual(traceMenuProblems(manifest), []);
  // The guard fires: a manifest missing one entry fails.
  const broken = JSON.parse(fs.readFileSync(PACKAGE_JSON_PATH, "utf8"));
  broken.contributes.menus["view/item/context"] = broken.contributes.menus[
    "view/item/context"
  ].filter((entry: MenuEntry) => entry.command !== "swcraft.traceRemoveChoice");
  broken.contributes.menus.commandPalette = broken.contributes.menus.commandPalette.filter(
    (entry: MenuEntry) => entry.command !== "swcraft.traceAccept",
  );
  assert.deepEqual(traceMenuProblems(broken), [
    "swcraft.traceAccept is not hidden from the Command Palette",
    "swcraft.traceRemoveChoice is in no menu of the Trace Links view or of a trace node of the panel",
  ]);
});

test("the Trace Links view is contributed below the panel when a project is detected (the work's own section)", () => {
  const manifest = JSON.parse(fs.readFileSync(PACKAGE_JSON_PATH, "utf8"));
  const views = manifest.contributes.views.swcraft as Array<Record<string, string>>;
  assert.deepEqual(
    views.map((view) => view.id),
    ["swcraftView", "swcraftTrace", "swcraftResources", "swcraftImpact", "swcraftReview"],
  );
  assert.deepEqual(views[1], {
    id: "swcraftTrace",
    name: "Trace Links",
    when: "swcraft.trace.available",
  });
  assert.match(readSource("src/commands.ts"), /const TRACE_VIEW_ID = "swcraftTrace";/);
});

/** What is wrong with the panel's `…` menu against `PANEL_MENU_COMMANDS`, as messages. */
function panelMenuProblems(manifest: {
  contributes: { menus?: Record<string, MenuEntry[]> };
}): string[] {
  const menus = manifest.contributes.menus ?? {};
  const inPanelMenu = (menus["view/title"] ?? []).filter((entry) =>
    /(^|\s|\()view == swcraftView($|\s|\))/.test(entry.when ?? ""),
  );
  const problems: string[] = [];
  for (const expected of PANEL_MENU_COMMANDS) {
    const found = inPanelMenu.filter((entry) => entry.command === expected.commandId);
    if (found.length === 0) {
      problems.push(`${expected.commandId} is not in the SWCraft panel's \`…\` menu`);
    } else if (found.length > 1 || found[0].group !== expected.group) {
      problems.push(
        `${expected.commandId} is in the panel's \`…\` menu as ${found
          .map((entry) => entry.group ?? "(no group)")
          .join(", ")}, not once at ${expected.group}`,
      );
    }
    const hidden = (menus.commandPalette ?? []).some(
      (entry) => entry.command === expected.commandId && entry.when === "false",
    );
    if (hidden) {
      problems.push(`${expected.commandId} is hidden from the Command Palette`);
    }
  }
  const expectedIds = new Set(PANEL_MENU_COMMANDS.map((entry) => entry.commandId));
  for (const entry of inPanelMenu) {
    if (!expectedIds.has(entry.command)) {
      problems.push(`${entry.command} is in the panel's \`…\` menu but not in PANEL_MENU_COMMANDS`);
    }
  }
  return problems;
}

test("the SWCraft panel's `…` menu is exactly PANEL_MENU_COMMANDS, and neither entry is a tree item", () => {
  // Open Guide and Check Editor Tooling sit in the view title's overflow menu, not the tree, and
  // keep their palette entries.
  const manifest = JSON.parse(fs.readFileSync(PACKAGE_JSON_PATH, "utf8"));
  assert.deepEqual(panelMenuProblems(manifest), []);
  assert.deepEqual(
    PANEL_MENU_COMMANDS.map((entry) => entry.commandId),
    ["swcraft.openGuide", "swcraft.checkEditorTooling"],
  );
  const treeIds = new Set(TREE_COMMAND_DEFINITIONS.map((def) => def.commandId));
  for (const entry of PANEL_MENU_COMMANDS) {
    assert.equal(treeIds.has(entry.commandId), false, `${entry.commandId} is a tree item again`);
  }
  // A group other than `navigation` is what puts an entry in the `…` menu
  // rather than on the title bar as an icon.
  for (const entry of PANEL_MENU_COMMANDS) {
    assert.doesNotMatch(entry.group, /^navigation/, entry.commandId);
  }
  // The guard fires: a manifest without one entry, or with one hidden from the
  // palette, fails with a message naming it.
  const broken = JSON.parse(fs.readFileSync(PACKAGE_JSON_PATH, "utf8"));
  broken.contributes.menus["view/title"] = broken.contributes.menus["view/title"].filter(
    (entry: MenuEntry) => entry.command !== "swcraft.checkEditorTooling",
  );
  broken.contributes.menus.commandPalette.push({ command: "swcraft.openGuide", when: "false" });
  assert.deepEqual(panelMenuProblems(broken), [
    "swcraft.openGuide is hidden from the Command Palette",
    "swcraft.checkEditorTooling is not in the SWCraft panel's `…` menu",
  ]);
});

// --- The Pending Change Sets view and the review tab --------------------------

test("the Pending Change Sets view is contributed last, when a project is detected, with its welcome text", () => {
  const manifest = JSON.parse(fs.readFileSync(PACKAGE_JSON_PATH, "utf8"));
  const views = manifest.contributes.views.swcraft as Array<Record<string, string>>;
  assert.deepEqual(views[views.length - 1], {
    id: REVIEW_VIEW_ID,
    name: "Pending Change Sets",
    when: "swcraft.review.available",
  });
  const welcome = (manifest.contributes.viewsWelcome as Array<Record<string, string>>).filter(
    (entry) => entry.view === REVIEW_VIEW_ID,
  );
  assert.equal(welcome.length, 1);
  assert.match(welcome[0].contents, /No change set is waiting/);
  assert.match(welcome[0].contents, /SWCRAFT_REVIEW=panel/);
  // The id commands.ts creates the view under is the core's constant.
  assert.match(readSource("src/commands.ts"), /vscode\.window\.createTreeView\(REVIEW_VIEW_ID, \{/);
});

test("the review view's two commands: Refresh in its title bar, both hidden from the palette, Open on the row", () => {
  const manifest = readManifest() as unknown as {
    contributes: { commands: Array<Record<string, string>>; menus: Record<string, MenuEntry[]> };
  };
  assert.deepEqual([...REVIEW_VIEW_COMMANDS].sort(), [REVIEW_OPEN_COMMAND, REVIEW_REFRESH_COMMAND]);
  const refresh = manifest.contributes.commands.find(
    (entry) => entry.command === REVIEW_REFRESH_COMMAND,
  );
  assert.equal(refresh?.icon, "$(refresh)");
  assert.deepEqual(
    manifest.contributes.menus["view/title"].filter(
      (entry) => entry.command === REVIEW_REFRESH_COMMAND,
    ),
    [{ command: REVIEW_REFRESH_COMMAND, when: "view == swcraftReview", group: "navigation@1" }],
  );
  for (const id of REVIEW_VIEW_COMMANDS) {
    assert.ok(
      manifest.contributes.menus.commandPalette.some(
        (entry) => entry.command === id && entry.when === "false",
      ),
      `${id} is not hidden from the Command Palette`,
    );
  }
  // A row's click is the open command, by the core's constant.
  const provider = functionBody(
    withoutCommentLines(readSource("src/commands.ts")),
    "class ReviewTreeDataProvider ",
  );
  assert.match(provider, /command: REVIEW_OPEN_COMMAND,/);
});

test("no contributed or registered command approves or rejects a change set", () => {
  // Approve and Reject exist only as the review tab's buttons, so an assistant
  // that can run VS Code commands (Copilot can) has nothing to call.
  //
  // Two halves. By name: no command id but the trace view's own (a trace link
  // candidate's Accept and Reject, which edit the proposals file) says approve,
  // reject, decide or answer, and the review view's commands are exactly its two.
  // By reach: `writeDecision(`, the one call that writes a decision, appears
  // exactly once in commands.ts, inside the tab's message handler, and no command
  // handler names that handler.
  //
  // What this does NOT reach: a command that writes a decision file by hand,
  // through `fs` rather than `writeDecision` — the reviewer of that code is the guard.
  const ids = new Set([...manifestCommands().map((cmd) => cmd.command), ...registeredCommandIds()]);
  for (const id of ids) {
    if (TRACE_VIEW_COMMANDS.includes(id)) {
      continue;
    }
    assert.doesNotMatch(id, /approve|reject|decide|decision|answer/i, `${id} names a decision`);
  }
  assert.deepEqual(
    [...ids].filter((id) => /review/i.test(id)).sort(),
    [...REVIEW_VIEW_COMMANDS].sort(),
  );
  const code = withoutCommentLines(readSource("src/commands.ts"));
  assert.equal(code.match(/\bwriteDecision\(/g)?.length, 1, "writeDecision is called once");
  assert.match(functionBody(code, "function answerReviewTab("), /\bwriteDecision\(/);
  assert.match(
    code,
    /onDidReceiveMessage\(\(message: unknown\) => answerReviewTab\(tab, message\)\)/,
  );
  assert.equal(
    code.match(/\banswerReviewTab\(/g)?.length,
    2,
    "answerReviewTab is declared and called from the tab's message handler only",
  );
  for (const handler of ["function reviewOpenCommand(", "function reviewRefreshCommand("]) {
    assert.doesNotMatch(functionBody(code, handler), /answerReviewTab|writeDecision/);
  }
});

test("a click that met an earlier answer leaves the review tab live", () => {
  // `writeDecision`'s `live` refusal (an answer to an earlier copy of the
  // question at the name) is a note, never `disableReviewTab`: the question is
  // still unanswered, and a disabled tab would never be enabled again.
  const body = functionBody(
    withoutCommentLines(readSource("src/commands.ts")),
    "function answerReviewTab(",
  );
  assert.match(
    body,
    /if \(!written\.ok && written\.live === true\) \{\s*void tab\.panel\.webview\.postMessage\(\{ type: "note", line: written\.reason \}\);\s*return;\s*\}/,
  );
  assert.ok(
    body.indexOf("written.live === true") < body.indexOf("disableReviewTab(tab, written.reason)"),
    "the live refusal is decided before the tab is disabled",
  );
});

test("a click on a disabled review tab answers nothing and re-sends the tab's disabled line", () => {
  // A hidden tab's page is rebuilt with its buttons enabled when it is shown
  // again, until the re-sent disable message arrives; a click in that moment
  // must not be dropped without a word.
  const body = functionBody(
    withoutCommentLines(readSource("src/commands.ts")),
    "function answerReviewTab(",
  );
  assert.match(
    body,
    /if \(tab\.disabled !== undefined\) \{\s*disableReviewTab\(tab, tab\.disabled\);\s*return;\s*\}/,
  );
  assert.ok(
    body.indexOf("tab.disabled !== undefined") < body.indexOf("writeDecision("),
    "the disabled check comes before the write",
  );
});

/** What is wrong with the Resources view's menus and palette entries, as messages. */
function resourceMenuProblems(manifest: {
  contributes: { menus?: Record<string, MenuEntry[]> };
}): string[] {
  const menus = manifest.contributes.menus ?? {};
  // Each entry's `when` must hold every fragment: the view and the busy key.
  const inView = (menu: string, id: string, group: string, fragments: string[]): boolean =>
    (menus[menu] ?? []).some(
      (entry) =>
        entry.command === id &&
        entry.group === group &&
        ["view == swcraftResources", ...fragments].every((fragment) =>
          (entry.when ?? "").includes(fragment),
        ),
    );
  const busy = "!swcraft.resources.busy";
  const expected: Array<[string, string, string, string[]]> = [
    ["view/title", "swcraft.resourcesRefresh", "navigation@1", [busy]],
  ];
  const problems: string[] = [];
  for (const [menu, id, group, fragments] of expected) {
    if (!inView(menu, id, group, fragments)) {
      problems.push(`${id} is not in the Resources view's ${menu} at ${group}`);
    }
  }
  // The view's commands act on the view (the Trace Links rule), so the palette shows none.
  for (const id of RESOURCE_VIEW_COMMANDS) {
    const hidden = (menus.commandPalette ?? []).some(
      (entry) => entry.command === id && entry.when === "false",
    );
    if (!hidden) {
      problems.push(`${id} is shown in the Command Palette`);
    }
  }
  return problems;
}

test("the Resources view is contributed below Trace Links, collapsed, with its welcome", () => {
  const manifest = JSON.parse(fs.readFileSync(PACKAGE_JSON_PATH, "utf8"));
  const views = manifest.contributes.views.swcraft as Array<Record<string, string>>;
  assert.deepEqual(views[2], {
    id: "swcraftResources",
    name: "Resources",
    when: "swcraft.resources.available",
    visibility: "collapsed",
  });
  assert.match(readSource("src/commands.ts"), /const RESOURCES_VIEW_ID = "swcraftResources";/);
  // One welcome, one button: Resource Report, the run that fills the view without a Build.
  const welcome = (manifest.contributes.viewsWelcome as Array<Record<string, string>>).filter(
    (entry) => entry.view === "swcraftResources",
  );
  assert.equal(welcome.length, 1);
  assert.equal(
    welcome[0].contents,
    "A green Build fills this view with each core's and memory's budget, use and headroom. " +
      "Resource Report fills it without building and opens the report in a tab.\n" +
      "[Resource Report](command:swcraft.resourceReport)",
  );
  assert.equal((welcome[0].contents.match(/\]\(command:/g) ?? []).length, 1);
});

test("the Resources view's commands: titled under the SWCraft category, Refresh in its title bar, both hidden from the palette", () => {
  const byId = new Map(manifestCommands().map((cmd) => [cmd.command, cmd]));
  const expected: Array<[string, string, string | undefined]> = [
    ["swcraft.resourcesRefresh", "Refresh", "$(refresh)"],
    ["swcraft.resourcesOpen", "Open Element", undefined],
  ];
  assert.deepEqual(
    expected.map(([id]) => id),
    [...RESOURCE_VIEW_COMMANDS],
  );
  for (const [id, title, icon] of expected) {
    const command = byId.get(id) as { title: string; category?: string; icon?: string };
    assert.ok(command !== undefined, `package.json must declare ${id}`);
    assert.equal(command.title, title, id);
    assert.equal(
      command.category,
      "SWCraft",
      `${id}: the 48.1 spelling, a bare title and its category`,
    );
    assert.equal(command.icon, icon, id);
  }
  const manifest = JSON.parse(fs.readFileSync(PACKAGE_JSON_PATH, "utf8"));
  assert.deepEqual(resourceMenuProblems(manifest), []);
  // The guard fires: a Refresh a run in flight does not hide, or one in the palette, fails.
  const broken = JSON.parse(fs.readFileSync(PACKAGE_JSON_PATH, "utf8"));
  for (const entry of broken.contributes.menus["view/title"] as MenuEntry[]) {
    if (entry.command === "swcraft.resourcesRefresh") {
      entry.when = "view == swcraftResources";
    }
  }
  broken.contributes.menus.commandPalette = broken.contributes.menus.commandPalette.filter(
    (entry: MenuEntry) => entry.command !== "swcraft.resourcesRefresh",
  );
  assert.deepEqual(resourceMenuProblems(broken), [
    "swcraft.resourcesRefresh is not in the Resources view's view/title at navigation@1",
    "swcraft.resourcesRefresh is shown in the Command Palette",
  ]);
});

test("Does It Fit… is contributed, registered and shown nowhere: the question stays in the CLI", () => {
  const retired = ["swcraft.resourcesFit", "swcraft.resourcesClearFit"];
  const manifest = JSON.parse(fs.readFileSync(PACKAGE_JSON_PATH, "utf8"));
  const everywhere = [
    ...manifestCommands().map((cmd) => cmd.command),
    ...TREE_COMMAND_DEFINITIONS.map((def) => def.commandId),
    ...registeredCommandIds(),
  ];
  const menus = manifest.contributes.menus as Record<string, MenuEntry[]>;
  for (const id of retired) {
    assert.equal(everywhere.includes(id), false, `${id} is back`);
    for (const [menu, entries] of Object.entries(menus)) {
      assert.equal(
        entries.some((entry) => entry.command === id),
        false,
        `${id} is in ${menu}, the palette's own included`,
      );
    }
  }
  const titles = manifestCommands().map((cmd) => cmd.title);
  assert.equal(titles.includes("Does It Fit…"), false, "the Command Palette offers it again");
  // No `when` reads the key or the row that are gone, and no row of the view has a menu left.
  const whens = Object.values(menus).flatMap((entries) => entries.map((entry) => entry.when ?? ""));
  for (const when of whens) {
    assert.equal(when.includes("swcraft.resources.fit"), false, when);
    assert.equal(when.includes("resource.fit"), false, when);
  }
  assert.equal(
    menus["view/item/context"].some((entry) => (entry.when ?? "").includes("swcraftResources")),
    false,
  );
  assert.deepEqual(
    menus["view/title"]
      .filter((entry) => (entry.when ?? "").includes("swcraftResources"))
      .map((entry) => entry.command),
    ["swcraft.resourcesRefresh"],
    "the `?` left the title bar, the ↻ stays",
  );
});

/** What is wrong with the Impact view's menus and palette entries, as messages. */
function impactMenuProblems(manifest: {
  contributes: { menus?: Record<string, MenuEntry[]> };
}): string[] {
  const menus = manifest.contributes.menus ?? {};
  const has = (menu: string, id: string, group: string, fragments: string[]): boolean =>
    (menus[menu] ?? []).some(
      (entry) =>
        entry.command === id &&
        entry.group === group &&
        fragments.every((fragment) => (entry.when ?? "").includes(fragment)),
    );
  const view = "view == swcraftImpact";
  const busy = "!swcraft.impact.busy";
  const dependent = "viewItem == impact.dependent";
  const layerFile = "swcraft.activeEditorIsLayerFile";
  const expected: Array<[string, string, string, string[]]> = [
    ["view/title", "swcraft.impact", "navigation@1", [view, busy]],
    ["view/title", "swcraft.impactRefresh", "navigation@2", [view, "swcraft.impact.loaded", busy]],
    ["view/item/context", "swcraft.impact", "inline", [view, dependent, busy]],
    ["view/item/context", "swcraft.impact", "1_impact@1", [view, dependent, busy]],
    ["editor/context", "swcraft.impact", "1_swcraft@1", [layerFile]],
  ];
  const problems: string[] = [];
  for (const [menu, id, group, fragments] of expected) {
    if (!has(menu, id, group, fragments)) {
      problems.push(`${id} is not in ${menu} at ${group}`);
    }
  }
  // The view's title holds the question (the active editor's element) before Refresh, in that
  // order; the question is there before any answer, Refresh only once one is held.
  const titleOrder = (menus["view/title"] ?? [])
    .filter((entry) => (entry.when ?? "").includes(view))
    .map((entry) => entry.command);
  if (titleOrder.join(",") !== "swcraft.impact,swcraft.impactRefresh") {
    problems.push(`the Impact view's title holds ${titleOrder.join(", ")}`);
  }
  // The palette offers Impact of This Element only in an element file; the view's own two never.
  const palette = menus.commandPalette ?? [];
  const impactEntries = palette.filter((entry) => entry.command === "swcraft.impact");
  if (impactEntries.length !== 1 || impactEntries[0].when !== layerFile) {
    problems.push(`swcraft.impact is not in the Command Palette exactly when ${layerFile}`);
  }
  for (const id of ["swcraft.impactRefresh", "swcraft.impactOpen"]) {
    if (!palette.some((entry) => entry.command === id && entry.when === "false")) {
      problems.push(`${id} is not hidden from the Command Palette`);
    }
  }
  // No editor title button: the extension contributes none, and the pattern does not exist.
  if ((menus["editor/title"] ?? []).some((entry) => entry.command === "swcraft.impact")) {
    problems.push("swcraft.impact is in editor/title");
  }
  return problems;
}

test("the Impact view is contributed below Resources, collapsed, with its welcome and no button", () => {
  const manifest = JSON.parse(fs.readFileSync(PACKAGE_JSON_PATH, "utf8"));
  const views = manifest.contributes.views.swcraft as Array<Record<string, string>>;
  assert.deepEqual(views[3], {
    id: "swcraftImpact",
    name: "Impact",
    when: "swcraft.impact.available",
    visibility: "collapsed",
  });
  assert.match(readSource("src/commands.ts"), /const IMPACT_VIEW_ID = "swcraftImpact";/);
  // One welcome, no button: a question needs an element file in the editor.
  const welcome = (manifest.contributes.viewsWelcome as Array<Record<string, string>>).filter(
    (entry) => entry.view === "swcraftImpact",
  );
  assert.equal(welcome.length, 1);
  assert.equal(
    welcome[0].contents,
    "Ask what depends on an element: open its file and choose SWCraft: Impact of This Element " +
      "from the editor's context menu or the Command Palette.",
  );
  assert.equal(welcome[0].contents.includes("](command:"), false);
});

test("the Impact view's commands: titled, in their menus, the view's own hidden from the palette", () => {
  const byId = new Map(manifestCommands().map((cmd) => [cmd.command, cmd]));
  const expected: Array<[string, string, string | undefined, string | undefined]> = [
    ["swcraft.impact", "SWCraft: Impact of This Element", undefined, "$(references)"],
    ["swcraft.impactRefresh", "Refresh", "SWCraft", "$(refresh)"],
    ["swcraft.impactOpen", "Open Element", "SWCraft", undefined],
  ];
  assert.deepEqual(
    expected.map(([id]) => id),
    [...IMPACT_VIEW_COMMANDS],
  );
  for (const [id, title, category, icon] of expected) {
    const command = byId.get(id) as { title: string; category?: string; icon?: string };
    assert.ok(command !== undefined, `package.json must declare ${id}`);
    assert.equal(command.title, title, id);
    assert.equal(command.category, category, id);
    assert.equal(command.icon, icon, id);
  }
  const manifest = JSON.parse(fs.readFileSync(PACKAGE_JSON_PATH, "utf8"));
  assert.deepEqual(impactMenuProblems(manifest), []);
  // The guard fires: the title without its question, an editor menu without the layer-file key,
  // a Refresh shown before any question, the inline action gone, and Open in the palette each fail.
  const broken = JSON.parse(fs.readFileSync(PACKAGE_JSON_PATH, "utf8"));
  broken.contributes.menus["editor/context"][0].when = "resourceLangId == yaml";
  for (const entry of broken.contributes.menus["view/title"] as MenuEntry[]) {
    if (entry.command === "swcraft.impactRefresh") {
      entry.when = "view == swcraftImpact && !swcraft.impact.busy";
    }
  }
  broken.contributes.menus["view/item/context"] = broken.contributes.menus[
    "view/item/context"
  ].filter((entry: MenuEntry) => !(entry.command === "swcraft.impact" && entry.group === "inline"));
  broken.contributes.menus.commandPalette = broken.contributes.menus.commandPalette.filter(
    (entry: MenuEntry) => entry.command !== "swcraft.impactOpen",
  );
  broken.contributes.menus["editor/title"] = [{ command: "swcraft.impact" }];
  broken.contributes.menus["view/title"] = broken.contributes.menus["view/title"].filter(
    (entry: MenuEntry) => entry.command !== "swcraft.impact",
  );
  assert.deepEqual(impactMenuProblems(broken), [
    "swcraft.impact is not in view/title at navigation@1",
    "swcraft.impactRefresh is not in view/title at navigation@2",
    "swcraft.impact is not in view/item/context at inline",
    "swcraft.impact is not in editor/context at 1_swcraft@1",
    "the Impact view's title holds swcraft.impactRefresh",
    "swcraft.impactOpen is not hidden from the Command Palette",
    "swcraft.impact is in editor/title",
  ]);
  // A row's click is the open: the TreeItem's command, on a node that carries an element.
  const item = functionBody(
    withoutCommentLines(readSource("src/commands.ts")),
    "function impactTreeItem(",
  );
  assert.match(item, /if \(node\.element !== undefined\) \{[^}]*command: "swcraft\.impactOpen"/);
});

test("Show in Diagram: declared with its title, in the editor menu after Impact and in the palette for a model file only", () => {
  const manifest = JSON.parse(fs.readFileSync(PACKAGE_JSON_PATH, "utf8"));
  const byId = new Map(manifestCommands().map((cmd) => [cmd.command, cmd.title]));
  assert.deepEqual([...DIAGRAM_EDITOR_COMMANDS], ["swcraft.showInDiagram"]);
  assert.equal(byId.get("swcraft.showInDiagram"), "SWCraft: Show in Diagram");
  const ids = manifestCommands().map((cmd) => cmd.command);
  assert.equal(
    ids.indexOf("swcraft.showInDiagram"),
    ids.indexOf("swcraft.diagrams") + 1,
    "declared directly after Diagrams",
  );
  const menus = manifest.contributes.menus as Record<string, MenuEntry[]>;
  const layerFile = "swcraft.activeEditorIsLayerFile";
  const editor = (menus["editor/context"] ?? []).filter(
    (entry) => entry.command === "swcraft.showInDiagram",
  );
  assert.deepEqual(editor, [
    { command: "swcraft.showInDiagram", when: layerFile, group: "1_swcraft@2" },
  ]);
  const palette = (menus.commandPalette ?? []).filter(
    (entry) => entry.command === "swcraft.showInDiagram",
  );
  assert.deepEqual(palette, [{ command: "swcraft.showInDiagram", when: layerFile }]);
  // No view, panel item or title button reaches it: it is the editor's.
  for (const menu of ["view/title", "view/item/context", "editor/title"]) {
    assert.equal(
      (menus[menu] ?? []).some((entry) => entry.command === "swcraft.showInDiagram"),
      false,
      menu,
    );
  }
});

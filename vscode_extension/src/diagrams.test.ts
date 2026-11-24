// `SWCraft: Diagrams`' pure half (diagrams.ts): where a press opens, what one press reports, and
// the watch over the model that marks the artifacts out of date (the two watched patterns, one
// watch per window armed at activation, and the newest change on disk read at activation).
//
// There is no viewer: `swcraft generate diagrams` writes one HTML page per view and `index.html`
// under `views_root`, and a press opens that file. A save regenerates nothing, so nothing here
// spawns anything or runs on a timer.
//
// What this file does NOT reach: the generation itself (the CLI's, pinned on the Python side), the
// diagrams' tab (its pure half is diagramsTab.test.ts's), the notifications, and whether the real
// `createFileSystemWatcher` delivers an event for every save under the pattern; those are
// commands.ts's, pinned by source scans here and in commandCore.test.ts, and the mark after a save
// is checked by hand in the panel.
//
// No `vscode` import.
import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { test } from "node:test";

import {
  DIAGRAMS_INDEX_FILENAME,
  diagramsIndexPath,
  diagramsViewPath,
  ModelWatch,
  ModelWatchSession,
  manifestWatchPattern,
  modelRootWatchPattern,
  newestSourceChange,
  pressOfShow,
  reportDiagramsPress,
  type WatchFactory,
  type WatchPattern,
} from "./diagrams";
import { functionBody, productionModules, readSource, withoutCommentLines } from "./testSupport";

// --- the page a press opens ---------------------------------------------------

test("diagramsIndexPath: the generator's index.html directly under views_root", () => {
  assert.equal(DIAGRAMS_INDEX_FILENAME, "index.html");
  assert.equal(diagramsIndexPath("/w/views"), path.join("/w/views", "index.html"));
  assert.equal(
    diagramsIndexPath(path.join("/w", "architecture", "views")),
    path.join("/w", "architecture", "views", "index.html"),
  );
});

test("the generator writes the index a press opens", () => {
  // The committed golden of examples/basic is what `generate diagrams` writes
  // (packages/tools/tests/test_diagram_generation.py holds the two equal), so the file a
  // press opens is a file the generator produces, not a name retyped here. What
  // this does NOT reach: a generator run that exits 0 and writes no index.
  const golden = path.join(
    __dirname,
    "..",
    "..",
    "packages",
    "tools",
    "tests",
    "fixtures",
    "diagrams_golden",
    "basic",
    DIAGRAMS_INDEX_FILENAME,
  );
  assert.ok(fs.existsSync(golden), `${golden} is not there`);
});

// --- what one press reports ----------------------------------------------------

test("reportDiagramsPress: a refusal before the generation is a failure status and an error notification", () => {
  assert.deepEqual(reportDiagramsPress({ kind: "refused", message: "SWCraft: no views." }), {
    status: { kind: "failure", message: "SWCraft: no views." },
    notification: { severity: "error", message: "SWCraft: no views." },
  });
});

test("reportDiagramsPress: a refused generation is a failure status and a warning", () => {
  const message =
    "SWCraft: Diagrams failed (exit code 1). See the SWCraft output channel for details.";
  assert.deepEqual(reportDiagramsPress({ kind: "generation-refused", message }), {
    status: { kind: "failure", message },
    notification: { severity: "warning", message },
  });
});

test("reportDiagramsPress: an index shown in the tab is a success and says nothing", () => {
  assert.deepEqual(reportDiagramsPress({ kind: "opened" }), {
    status: { kind: "success" },
    notification: undefined,
  });
});

test("reportDiagramsPress: a direct open of a current page is a success and says nothing", () => {
  assert.deepEqual(
    reportDiagramsPress({
      kind: "opened",
      view: { id: "ibd-Chassis", found: true },
    }),
    { status: { kind: "success" }, notification: undefined },
  );
});

test("reportDiagramsPress: a lens's page found after the generation is shown and says nothing", () => {
  assert.deepEqual(
    reportDiagramsPress({
      kind: "opened",
      view: { id: "ibd-Chassis", found: true },
    }),
    { status: { kind: "success" }, notification: undefined },
  );
});

test("reportDiagramsPress: a lens's page gone after the generation shows the index and says so", () => {
  const report = reportDiagramsPress({
    kind: "opened",
    view: { id: "ibd-Chassis", found: false },
  });
  assert.deepEqual(report.status, { kind: "success" }, "the pages were generated");
  assert.equal(report.notification?.severity, "information");
  assert.match(report.notification?.message ?? "", /ibd-Chassis\.html is no longer generated/);
  assert.match(report.notification?.message ?? "", /the index of the diagrams was opened instead/);
});

test("reportDiagramsPress: a page the tab refuses is a failure carrying the tab's line, and no notification", () => {
  const message = "These diagram pages were generated by another SWCraft release — press Generate";
  assert.deepEqual(reportDiagramsPress({ kind: "page-refused", message }), {
    status: { kind: "failure", message },
    notification: undefined,
  });
});

test("reportDiagramsPress: no way a press ends mentions a browser", () => {
  const presses = [
    { kind: "opened" },
    {
      kind: "opened",
      view: { id: "x", found: true },
    },
    {
      kind: "opened",
      view: { id: "x", found: false },
    },
    { kind: "page-refused", message: "m" },
  ] as const;
  for (const press of presses) {
    const report = reportDiagramsPress(press);
    assert.doesNotMatch(report.notification?.message ?? "", /browser/, JSON.stringify(press));
  }
});

test("diagramsViewPath: <views_root>/<id>.html, beside the index", () => {
  assert.equal(
    diagramsViewPath("/w/views", "sequence-Brake"),
    path.join("/w/views", "sequence-Brake.html"),
  );
});

// --- the viewer is gone ---------------------------------------------------------

test("likec4Server cannot be imported: the module, its test and every use of it are gone", async () => {
  // The served LikeC4 viewer was replaced with files. What this does NOT reach: a copy of the
  // viewer under a name none of these spell, and a spelling in a comment line
  // (`withoutCommentLines`).
  for (const file of ["likec4Server.ts", "likec4Server.test.ts"]) {
    assert.equal(fs.existsSync(path.join(__dirname, "..", "src", file)), false, file);
  }
  const specifier = "./likec4Server";
  await assert.rejects(import(specifier));
  // The diagram tab takes LikeC4's published palette from a leaf of constants (`likec4Palette.ts`)
  // that imports nothing and so reaches no viewer. It is the one module allowed the spelling, and
  // its module specifier and exported names are the only spellings another module may use.
  const palette = readSource("src/likec4Palette.ts");
  assert.equal(/^\s*import\b/m.test(palette), false, "the palette module imports nothing");
  const paletteNames = [
    ...palette.matchAll(/^export (?:const|function|type|interface) ([A-Za-z0-9_]+)/gm),
  ].map((match) => match[1]);
  assert.ok(paletteNames.includes("likec4Palette"), "the palette's names are read off its source");
  const withoutPalette = (source: string) =>
    paletteNames.reduce(
      (text, name) => text.replace(new RegExp(`\\b${name}\\b`, "g"), ""),
      source.replace(/"\.\/likec4Palette"/g, ""),
    );
  const offences: string[] = [];
  for (const module of productionModules()) {
    if (module === "likec4Palette.ts") continue;
    const source = withoutPalette(withoutCommentLines(readSource(path.posix.join("src", module))));
    // Any spelling of LikeC4 in code, the pack member's and the health report's paragraph on its
    // Node.js included.
    if (/likec4/i.test(source)) {
      offences.push(`${module} spells likec4`);
    }
    // The viewer's own names.
    for (const spelling of [
      "likec4Server",
      "Likec4",
      "LIKEC4",
      "likec4Env",
      "likec4.mjs",
      "serveDiagramsTo",
      "DIAGRAMS_OUTPUT_CHANNEL",
    ]) {
      if (source.includes(spelling)) {
        offences.push(`${module} spells ${spelling}`);
      }
    }
  }
  assert.deepEqual(offences, [], "no production module reaches the retired viewer");
});

test("diagrams.ts spawns nothing and touches no environment", () => {
  // The generation is `spawnCli()`'s, with the environment every CLI command gets; no
  // environment rewriting of its own (NO_COLOR, BROWSER, TEST, the AI variables, a temp directory).
  const source = withoutCommentLines(readSource("src/diagrams.ts"));
  assert.equal(/node:child_process|\bspawn\(|process\.env|node:net/.test(source), false);
});

// --- a save regenerates nothing --------------------------------------------------
//
// On a large model one regeneration takes seconds, and a Build pressed right after a save would
// wait for it. The watch only marks: a change under `model_root` or of the manifest is reported
// with the path that changed, and the adapter turns that into the artifacts' *out of date* state.
// Nothing here, and nothing the adapter hangs on it, starts a process.

test("the regeneration on save is gone: no debounce, no rule, no scheduler, no regenerate", async () => {
  const exported = await import("./diagrams");
  for (const name of [
    "DEBOUNCE_MS",
    "decideRegeneration",
    "RegenerationScheduler",
    "watchDiagramSources",
    "DiagramsWatch",
    "DiagramsWatchSession",
  ]) {
    assert.equal(name in exported, false, `diagrams.ts still exports ${name}`);
  }
  const offences: string[] = [];
  for (const module of productionModules()) {
    const source = withoutCommentLines(readSource(path.posix.join("src", module)));
    for (const spelling of [
      "DEBOUNCE_MS",
      "decideRegeneration",
      "RegenerationScheduler",
      "RegenerationTimers",
      "watchDiagramSources",
      "regenerateDiagrams",
      "DiagramsWatchSession",
    ]) {
      if (source.includes(spelling)) {
        offences.push(`${module} spells ${spelling}`);
      }
    }
  }
  assert.deepEqual(offences, [], "no production module still reaches the regeneration on save");
});

// --- the watch: the two patterns ------------------------------------------------

class FakeWatchers {
  readonly created: Array<{
    pattern: WatchPattern;
    fire: (changedPath: string) => void;
    disposed: number;
  }> = [];

  readonly watch: WatchFactory = (pattern, onEvent) => {
    const entry = { pattern, fire: onEvent, disposed: 0 };
    this.created.push(entry);
    return {
      dispose: () => {
        entry.disposed += 1;
      },
    };
  };

  live(): WatchPattern[] {
    return this.created.filter((entry) => entry.disposed === 0).map((entry) => entry.pattern);
  }
}

const MANIFEST = "/w/swcraft.yaml";

test("ModelWatch: both patterns are built from the resolved model root and the manifest path", () => {
  const watchers = new FakeWatchers();
  const asked: string[] = [];
  new ModelWatch(
    MANIFEST,
    (manifestPath) => {
      asked.push(manifestPath);
      return "/w/architecture/model";
    },
    watchers.watch,
    () => {},
  );

  assert.deepEqual(asked, [MANIFEST]);
  assert.deepEqual(watchers.live(), [
    { base: "/w/architecture/model", glob: "**" },
    { base: "/w", glob: "swcraft.yaml" },
  ]);
  assert.deepEqual(modelRootWatchPattern("/m"), { base: "/m", glob: "**" });
  assert.deepEqual(manifestWatchPattern(MANIFEST), { base: "/w", glob: "swcraft.yaml" });
});

test("ModelWatch: a model event and a manifest event each report the path that changed", () => {
  const watchers = new FakeWatchers();
  const changes: string[] = [];
  new ModelWatch(
    MANIFEST,
    () => "/w/model",
    watchers.watch,
    (changedPath) => {
      changes.push(changedPath);
    },
  );
  watchers.created[0].fire("/w/model/swc/A.yaml");
  assert.deepEqual(changes, ["/w/model/swc/A.yaml"]);
  watchers.created[1].fire(MANIFEST);
  assert.deepEqual(changes, ["/w/model/swc/A.yaml", MANIFEST]);
  assert.equal(watchers.created.length, 2, "a manifest save that moved nothing builds no pattern");
});

test("ModelWatch: both patterns are disposed with it, and a late event reports nothing", () => {
  const watchers = new FakeWatchers();
  let changes = 0;
  const watch = new ModelWatch(
    MANIFEST,
    () => "/w/model",
    watchers.watch,
    () => {
      changes += 1;
    },
  );
  watch.dispose();
  watch.dispose();
  assert.deepEqual(
    watchers.created.map((entry) => entry.disposed),
    [1, 1],
    "each pattern disposed exactly once",
  );
  watchers.created[0].fire("/w/model/x.yaml");
  watchers.created[1].fire(MANIFEST);
  assert.equal(changes, 0);
});

test("ModelWatch: a manifest whose model_root moved yields a new model-root pattern, the old one disposed", () => {
  const watchers = new FakeWatchers();
  let modelRoot = "/w/model";
  let changes = 0;
  const watch = new ModelWatch(
    MANIFEST,
    () => modelRoot,
    watchers.watch,
    () => {
      changes += 1;
    },
  );
  const [oldModel, manifest] = watchers.created;

  modelRoot = "/w/architecture";
  manifest.fire(MANIFEST);

  assert.equal(oldModel.disposed, 1, "the old model-root pattern is disposed");
  assert.equal(manifest.disposed, 0, "the manifest pattern is kept");
  assert.equal(watchers.created.length, 3);
  assert.deepEqual(watchers.created[2].pattern, { base: "/w/architecture", glob: "**" });
  assert.deepEqual(watchers.live(), [
    { base: "/w", glob: "swcraft.yaml" },
    { base: "/w/architecture", glob: "**" },
  ]);
  assert.equal(changes, 1, "the manifest save itself is a change");

  oldModel.fire("/w/model/x.yaml");
  assert.equal(changes, 1, "an event from the disposed pattern is ignored");
  watchers.created[2].fire("/w/architecture/x.yaml");
  assert.equal(changes, 2);

  watch.dispose();
  assert.deepEqual(watchers.live(), []);
});

test("ModelWatch: a refused model root watches only the manifest, and the save that repairs it arms it", () => {
  const watchers = new FakeWatchers();
  let modelRoot: string | undefined;
  let changes = 0;
  new ModelWatch(
    MANIFEST,
    () => modelRoot,
    watchers.watch,
    () => {
      changes += 1;
    },
  );
  assert.deepEqual(watchers.live(), [{ base: "/w", glob: "swcraft.yaml" }]);

  modelRoot = "/w/model";
  watchers.created[0].fire(MANIFEST);
  assert.deepEqual(watchers.live(), [
    { base: "/w", glob: "swcraft.yaml" },
    { base: "/w/model", glob: "**" },
  ]);
  assert.equal(changes, 1);

  modelRoot = undefined;
  watchers.created[0].fire(MANIFEST);
  assert.deepEqual(watchers.live(), [{ base: "/w", glob: "swcraft.yaml" }]);
});

// --- the watch session: armed at activation, one watch at a time -----------------

/** An `arm` that records each folder it was asked for and how often each watch was disposed. */
function recordedArming(): {
  arm: (folder: string) => { dispose(): void };
  armed: string[];
  disposed: number[];
} {
  const armed: string[] = [];
  const disposed: number[] = [];
  return {
    arm: (folder) => {
      const index = armed.length;
      armed.push(folder);
      disposed.push(0);
      return {
        dispose: () => {
          disposed[index] += 1;
        },
      };
    },
    armed,
    disposed,
  };
}

test("watch session: arming a folder again replaces its watch, so a manifest gained in place is read", () => {
  // A folder with no manifest watches nothing; Onboard Project writes one in
  // place, which is no folder change, and re-arms the open folder — so arming the
  // same folder must build the watch afresh rather than keep the empty one.
  const record = recordedArming();
  const session = new ModelWatchSession(record.arm);
  assert.deepEqual(record.armed, [], "nothing is watched before it is armed");
  session.armFor("/w");
  session.armFor("/w");
  assert.deepEqual(record.armed, ["/w", "/w"]);
  assert.deepEqual(record.disposed, [1, 0], "the replaced watch is disposed, the new one kept");
});

test("watch session: another folder disposes the old watch before arming the new one", () => {
  const order: string[] = [];
  const session = new ModelWatchSession((folder) => {
    order.push(`arm ${folder}`);
    return {
      dispose: () => {
        order.push(`dispose ${folder}`);
      },
    };
  });
  session.armFor("/w");
  session.armFor("/other");
  assert.deepEqual(order, ["arm /w", "dispose /w", "arm /other"]);
});

test("watch session: dispose disposes the watch once and forgets the folder, so it can be armed again", () => {
  const record = recordedArming();
  const session = new ModelWatchSession(record.arm);
  session.armFor("/w");
  session.dispose();
  session.dispose();
  assert.deepEqual(record.disposed, [1]);
  session.armFor("/w");
  assert.deepEqual(record.armed, ["/w", "/w"], "after a folder change the folder is armed again");
  assert.deepEqual(record.disposed, [1, 0]);
});

// --- the adapter's half, read as text (commands.ts imports "vscode") -------------
//
// What these scans do NOT reach: whether VS Code delivers an event for a save under the pattern,
// and whether the panel shows the mark; both are checked by hand in a real window.

test("the watch is armed at activation, for the open folder, with no press needed", () => {
  const source = withoutCommentLines(readSource("src/commands.ts"));
  const registration = functionBody(source, "export function registerCommands(");
  assert.match(
    registration,
    /const modelWatch = new ModelWatchSession\(\(folder\) =>\s*watchModelFolder\(folder\),?\s*\)/,
  );
  assert.match(registration, /armModelWatch\(\);/, "armed at activation");
  assert.match(
    registration,
    /onDidChangeWorkspaceFolders\(\(\) => \{\s*modelWatch\.dispose\(\);\s*armModelWatch\(\);/,
    "a folder change disposes the old folder's watch and arms the open one",
  );
  assert.match(registration, /context\.subscriptions\.push\(\s*modelWatch,/);
  const press = functionBody(source, "async function diagramsCommand(");
  assert.equal(/armFor|modelWatch/.test(press), false, "a Diagrams press arms nothing");
});

test("a change under model_root or of the manifest marks and starts no process", () => {
  const source = withoutCommentLines(readSource("src/commands.ts"));
  const folder = functionBody(source, "function watchModelFolder(");
  assert.match(folder, /resolveManifest\(folder\)/);
  assert.match(folder, /new ModelWatch\(/);
  // The one resolver that answers undefined for a manifest the loader refuses.
  assert.match(folder, /resolveModelRoot,/);
  assert.match(folder, /createPatternWatcher/);
  assert.match(folder, /noteModelChange\(changedPath\)/, "the handler only notes the change");
  for (const spawn of ["spawnCli(", "runGenerateDiagrams(", "viewsRootWrites", "executeCommand("]) {
    assert.equal(folder.includes(spawn), false, `the watch's handler must not reach ${spawn}`);
  }
});

// --- the newest change on disk, read once at activation ----------------------------

function tempProject(): { root: string; manifest: string; model: string } {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "swcraft-newest-"));
  const manifest = path.join(root, "swcraft.yaml");
  const model = path.join(root, "model");
  fs.writeFileSync(manifest, "name: p\n", "utf8");
  fs.mkdirSync(path.join(model, "swc", "g"), { recursive: true });
  fs.writeFileSync(path.join(model, "swc", "g", "A.yaml"), "name: A\n", "utf8");
  fs.writeFileSync(path.join(model, "interfaces.yaml"), "x\n", "utf8");
  return { root, manifest, model };
}

/** Set every entry of the project, directories included, to *seconds*. */
function stampAll(project: { root: string }, seconds: number): void {
  const walk = (entry: string): void => {
    if (fs.statSync(entry).isDirectory()) {
      for (const child of fs.readdirSync(entry)) {
        walk(path.join(entry, child));
      }
    }
    fs.utimesSync(entry, seconds, seconds);
  };
  walk(project.root);
}

test("newestSourceChange: the newest mtime of any entry under model_root, or of the manifest", async () => {
  const project = tempProject();
  try {
    stampAll(project, 1_000);
    assert.equal(await newestSourceChange(project.model, project.manifest), 1_000_000);

    const deep = path.join(project.model, "swc", "g", "A.yaml");
    fs.utimesSync(deep, 2_000, 2_000);
    assert.equal(await newestSourceChange(project.model, project.manifest), 2_000_000);

    fs.utimesSync(project.manifest, 3_000, 3_000);
    assert.equal(
      await newestSourceChange(project.model, project.manifest),
      3_000_000,
      "a manifest change counts: it can move what is generated",
    );
  } finally {
    fs.rmSync(project.root, { recursive: true, force: true });
  }
});

test("newestSourceChange: a deletion is seen through its directory's mtime", async () => {
  // A file deleted while the window was closed leaves no file to stat; the
  // directory that held it records the removal.
  const project = tempProject();
  try {
    stampAll(project, 1_000);
    fs.rmSync(path.join(project.model, "swc", "g", "A.yaml"));
    fs.utimesSync(path.join(project.model, "swc", "g"), 4_000, 4_000);
    assert.equal(await newestSourceChange(project.model, project.manifest), 4_000_000);
  } finally {
    fs.rmSync(project.root, { recursive: true, force: true });
  }
});

test("newestSourceChange: a missing model root answers the manifest; nothing at all answers undefined", async () => {
  const project = tempProject();
  try {
    stampAll(project, 1_000);
    fs.rmSync(project.model, { recursive: true, force: true });
    fs.utimesSync(project.manifest, 5_000, 5_000);
    assert.equal(await newestSourceChange(project.model, project.manifest), 5_000_000);
    fs.rmSync(project.manifest);
    assert.equal(await newestSourceChange(project.model, project.manifest), undefined);
  } finally {
    fs.rmSync(project.root, { recursive: true, force: true });
  }
});

test("newestSourceChange: a refused model root walks nothing and answers the manifest alone", async () => {
  const project = tempProject();
  try {
    stampAll(project, 1_000);
    fs.utimesSync(path.join(project.model, "swc", "g", "A.yaml"), 9_000, 9_000);
    assert.equal(await newestSourceChange(undefined, project.manifest), 1_000_000);
  } finally {
    fs.rmSync(project.root, { recursive: true, force: true });
  }
});

test("pressOfShow: a page the tab shows is the press's `opened`; one it refuses is `page-refused` with the tab's line", () => {
  const view = { id: "ibd-Chassis", found: true };
  assert.deepEqual(pressOfShow({ kind: "shown" }, view), { kind: "opened", view });
  assert.deepEqual(pressOfShow({ kind: "shown" }), { kind: "opened" });
  const refused = pressOfShow({ kind: "refused", line: "the tab's line" }, view);
  assert.deepEqual(refused, { kind: "page-refused", message: "the tab's line" });
  assert.deepEqual(reportDiagramsPress(refused), {
    status: { kind: "failure", message: "the tab's line" },
    notification: undefined,
  });
});

// The Test Tables tab's pure half: the CSV parser, the reader's three answers,
// the pages, the message check, the annotations pick and the argv. The input is
// the committed real bundle `src/fixtures/test-tables/`, held to the CLI's
// output by `packages/tools/tests/test_test_tables_panel_fixtures.py`, so what is read
// here is what Python writes.
//
// What no test here reaches: the tab rendered in a real VS Code window (checked by hand), and the
// adapter's calls into VS Code, pinned by source shape in commandCore.test.ts.

import assert from "node:assert/strict";
import * as fs from "node:fs";
import { createRequire } from "node:module";
import * as os from "node:os";
import * as path from "node:path";
import { after, test } from "node:test";

import { REPORT_DIRECTORIES } from "./project";
import { escapeHtml, reviewCsp } from "./reviewCore";
import {
  type AnnotationsPickItem,
  acceptTestTablesMessage,
  annotationsPickItems,
  askTestTablesAnnotations,
  countsLine,
  describeAnnotations,
  EMPTY_LIST_TEXT,
  formatCell,
  formatEvents,
  formatHash,
  formatList,
  parseCsv,
  REVIEW_STATE_CLASSES,
  ROW_BAR_WIDTH,
  ROW_BARS,
  ROW_TINT_SHARES,
  ROW_TINTS,
  readTestTables,
  rowClassOf,
  STATE_BADGES,
  TEST_TABLES_COLUMN_CAP,
  TEST_TABLES_DIRECTORY,
  TEST_TABLES_EMPTY_LINE,
  TEST_TABLES_READY_MESSAGE,
  TEST_TABLES_REFUSED_LINE,
  TEST_TABLES_SCRIPT,
  TEST_TABLES_STYLE,
  TEST_TABLES_UNREADABLE_LINE,
  type TestTablesAnnotationsHost,
  type TestTablesRead,
  testTablesArgv,
  testTablesEmptyPage,
  testTablesPage,
  testTablesRefusalPage,
  testTablesUnreadablePage,
} from "./testTablesTab";

// tsc emits src/**/*.ts -> out/, so the committed fixtures are one level up, in src/.
const FIXTURE = path.join(__dirname, "..", "src", "fixtures", "test-tables");
const BUNDLE_FILES = [
  "component-placement.csv",
  "components.csv",
  "manifest.json",
  "orphaned-annotations.csv",
  "runnable-scheduling.csv",
];
const NONCE = "bm9uY2Vub25jZW5vbmNl";
/** A directory name no page may carry: the dist root and the annotations folder are under it. */
const SENTINEL = "SENTINEL-LOCATION-7f3a";

/** Every temporary directory a test made, removed once the file's tests are done. */
const temporaryDirectories: string[] = [];
after(() => {
  for (const directory of temporaryDirectories) {
    fs.rmSync(directory, { recursive: true, force: true });
  }
});

/** A fresh, empty temporary directory, removed after the file's tests. */
function temporaryDirectory(prefix = "swcraft-test-tables-"): string {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), prefix));
  temporaryDirectories.push(directory);
  return directory;
}

/** A fresh `dist_root` under a sentinel directory, holding a copy of the fixture bundle. */
function distWithBundle(): string {
  const root = temporaryDirectory();
  const dist = path.join(root, SENTINEL, "dist");
  fs.mkdirSync(path.join(dist, TEST_TABLES_DIRECTORY), { recursive: true });
  for (const name of BUNDLE_FILES) {
    fs.copyFileSync(path.join(FIXTURE, name), path.join(dist, TEST_TABLES_DIRECTORY, name));
  }
  return dist;
}

function bundleFile(dist: string, name: string): string {
  return path.join(dist, TEST_TABLES_DIRECTORY, name);
}

/** The bundle's manifest.json, edited by *edit* in place (a test's spoiled bundle). */
// biome-ignore lint/suspicious/noExplicitAny: a test edits the parsed JSON freely
function editManifest(dist: string, edit: (manifest: any) => void): void {
  const file = bundleFile(dist, "manifest.json");
  const manifest = JSON.parse(fs.readFileSync(file, "utf8"));
  edit(manifest);
  fs.writeFileSync(file, JSON.stringify(manifest));
}

function readTables(dist: string): Extract<TestTablesRead, { kind: "tables" }> {
  const read = readTestTables(dist);
  assert.equal(read.kind, "tables", read.kind === "unreadable" ? read.reason : read.kind);
  return read as Extract<TestTablesRead, { kind: "tables" }>;
}

function snapshot(directory: string): Record<string, string> {
  const files: Record<string, string> = {};
  for (const name of fs.readdirSync(directory).sort()) {
    files[name] = fs.readFileSync(path.join(directory, name)).toString("base64");
  }
  return files;
}

// --- The parser ----------------------------------------------------------------

test("parseCsv: every field quoted, an escaped quote, and a field holding , \\r \\n and \\r\\n", () => {
  assert.deepEqual(parseCsv('"a","b"\n'), [["a", "b"]]);
  assert.deepEqual(parseCsv('"a,b","say ""hi"""\n'), [["a,b", 'say "hi"']]);
  assert.deepEqual(parseCsv('"one\rtwo","three\nfour","five\r\nsix"\n"",""\n'), [
    ["one\rtwo", "three\nfour", "five\r\nsix"],
    ["", ""],
  ]);
  assert.deepEqual(parseCsv('""\n'), [[""]]);
  assert.deepEqual(parseCsv('"  spaced  "\n'), [["  spaced  "]], "never trimmed");
});

test("parseCsv: a looser text is refused, never read half", () => {
  for (const text of [
    "",
    'a,"b"\n',
    '"a",b\n',
    '"a","b"',
    '"a","b"\r\n',
    '"a"x\n',
    '"a" ,"b"\n',
    '"a\n',
    '"a","b"\n"c"',
    '"a",\n',
    '"a","b"\n\n',
    '\uFEFF"a"\n',
    '"a""\n',
  ]) {
    assert.equal(parseCsv(text), undefined, JSON.stringify(text));
  }
});

// --- The reader ----------------------------------------------------------------

test("TEST_TABLES_DIRECTORY is a report directory of project.ts", () => {
  assert.ok(REPORT_DIRECTORIES.includes(TEST_TABLES_DIRECTORY));
});

test("readTestTables: the committed bundle reads as the CLI wrote it", () => {
  const read = readTables(distWithBundle());
  assert.equal(read.project, "swcraft-basic-example");
  assert.deepEqual(
    read.tables.map((table) => [table.file, table.label, table.rows.length]),
    [
      ["components.csv", "Components", 2],
      ["component-placement.csv", "Component placement", 2],
      ["runnable-scheduling.csv", "Runnable scheduling", 2],
    ],
  );
  assert.equal(read.orphans.label, "Orphaned annotations");
  assert.deepEqual(read.orphans.rows, [
    [
      "runnable-scheduling",
      "occurrence:RetiredSWC:RetiredRun",
      "review",
      "kept, though the runnable is gone",
      "",
      "",
    ],
  ]);
  const components = read.tables[0];
  const description = components.columns.findIndex((column) => column.name === "description");
  assert.ok(
    components.rows.some(
      (row) =>
        row[description] === 'Publishes, "quoted" state\rafter a CR\nafter an LF\r\nafter a CRLF',
    ),
    'a description holding , " \\r \\n and \\r\\n is read exactly',
  );
  const scheduling = read.tables[2];
  const comment = scheduling.columns.findIndex((column) => column.name === "comment");
  assert.ok(
    scheduling.rows.some(
      (row) => row[comment] === 'Check on target,\r\nwith the bench\'s "long" run',
    ),
  );
  assert.equal(
    countsLine(scheduling),
    "Runnable scheduling: 2 rows (0 accepted, 1 stale, 0 rejected, 1 not reviewed)",
  );
  assert.equal(countsLine(read.orphans), "Orphaned annotations: 1 row");
});

test("readTestTables: no directory, or no manifest in it, is absent", () => {
  const dist = temporaryDirectory();
  assert.deepEqual(readTestTables(dist), { kind: "absent" });
  assert.deepEqual(readTestTables(path.join(dist, "missing")), { kind: "absent" });
  fs.mkdirSync(path.join(dist, TEST_TABLES_DIRECTORY));
  fs.writeFileSync(path.join(dist, TEST_TABLES_DIRECTORY, "components.csv"), '"row_id"\n');
  assert.deepEqual(readTestTables(dist), { kind: "absent" });
});

test("readTestTables: an edited, broken or partial bundle is unreadable, never a partial table", () => {
  const cases: Array<[string, (dist: string) => void]> = [
    [
      "a manifest that is not JSON",
      (dist) => fs.writeFileSync(bundleFile(dist, "manifest.json"), "{"),
    ],
    [
      "a manifest that is a list",
      (dist) => fs.writeFileSync(bundleFile(dist, "manifest.json"), "[]"),
    ],
    [
      "a manifest without its tables",
      (dist) => {
        const manifest = JSON.parse(fs.readFileSync(bundleFile(dist, "manifest.json"), "utf8"));
        delete manifest.tables;
        fs.writeFileSync(bundleFile(dist, "manifest.json"), JSON.stringify(manifest));
      },
    ],
    [
      "a manifest naming a path",
      (dist) => {
        const manifest = JSON.parse(fs.readFileSync(bundleFile(dist, "manifest.json"), "utf8"));
        manifest.tables[0].file = "../components.csv";
        fs.writeFileSync(bundleFile(dist, "manifest.json"), JSON.stringify(manifest));
      },
    ],
    ["a listed file missing", (dist) => fs.rmSync(bundleFile(dist, "components.csv"))],
    ["the orphan file missing", (dist) => fs.rmSync(bundleFile(dist, "orphaned-annotations.csv"))],
    [
      "a CSV re-aligned with spaces",
      (dist) => {
        const file = bundleFile(dist, "component-placement.csv");
        fs.writeFileSync(file, fs.readFileSync(file, "utf8").replace(/","/g, '",  "'));
      },
    ],
    [
      "a CSV saved with CRLF records",
      (dist) => {
        const file = bundleFile(dist, "orphaned-annotations.csv");
        fs.writeFileSync(file, fs.readFileSync(file, "utf8").replace(/"\n/g, '"\r\n'));
      },
    ],
    [
      "a header that is not the manifest's",
      (dist) => {
        const file = bundleFile(dist, "components.csv");
        fs.writeFileSync(file, fs.readFileSync(file, "utf8").replace('"row_id"', '"id"'));
      },
    ],
    [
      "a row removed",
      (dist) => {
        const file = bundleFile(dist, "orphaned-annotations.csv");
        fs.writeFileSync(file, `${fs.readFileSync(file, "utf8").split("\n")[0]}\n`);
      },
    ],
    [
      "a byte order mark",
      (dist) => {
        const file = bundleFile(dist, "components.csv");
        fs.writeFileSync(
          file,
          Buffer.concat([Buffer.from([0xef, 0xbb, 0xbf]), fs.readFileSync(file)]),
        );
      },
    ],
    [
      "a file that is not UTF-8",
      (dist) => {
        const file = bundleFile(dist, "components.csv");
        fs.writeFileSync(file, Buffer.concat([fs.readFileSync(file), Buffer.from([0xff, 0x0a])]));
      },
    ],
    [
      "a row of another width",
      (dist) => {
        const file = bundleFile(dist, "orphaned-annotations.csv");
        fs.writeFileSync(file, fs.readFileSync(file, "utf8").replace(/""\n$/, '"",""\n'));
      },
    ],
    [
      "the manifest naming one file twice",
      (dist) =>
        editManifest(dist, (m) => {
          m.tables[1].file = "components.csv";
          m.tables[1].columns = m.tables[0].columns;
          m.tables[1].rows = m.tables[0].rows;
          m.tables[1].review_states = m.tables[0].review_states;
        }),
    ],
    [
      "a review_states count missing",
      (dist) =>
        editManifest(dist, (m) => {
          delete m.tables[0].review_states.stale;
        }),
    ],
    [
      "a negative review_states count",
      (dist) =>
        editManifest(dist, (m) => {
          m.tables[0].review_states.stale = -1;
        }),
    ],
    [
      "review_states not an object",
      (dist) =>
        editManifest(dist, (m) => {
          m.tables[0].review_states = [];
        }),
    ],
    [
      "no orphaned_annotations object",
      (dist) =>
        editManifest(dist, (m) => {
          delete m.orphaned_annotations;
        }),
    ],
    [
      "columns not a list",
      (dist) =>
        editManifest(dist, (m) => {
          m.tables[0].columns = {};
        }),
    ],
    [
      "an empty columns list",
      (dist) =>
        editManifest(dist, (m) => {
          m.tables[0].columns = [];
        }),
    ],
    [
      "a column that is not an object",
      (dist) =>
        editManifest(dist, (m) => {
          m.tables[0].columns[0] = "row_id";
        }),
    ],
    [
      "a listed name that is a directory",
      (dist) => {
        fs.rmSync(bundleFile(dist, "components.csv"));
        fs.mkdirSync(bundleFile(dist, "components.csv"));
      },
    ],
    [
      "per-state counts that are not the rows'",
      (dist) =>
        editManifest(dist, (m) => {
          const states = m.tables[0].review_states;
          [states.accepted, states.rejected] = [states.rejected + 1, states.accepted - 1];
        }),
    ],
    [
      "a review_state outside the four, the counts unchanged",
      (dist) => {
        const file = bundleFile(dist, "components.csv");
        const text = fs.readFileSync(file, "utf8");
        fs.writeFileSync(file, text.replace('"rejected"\n', '"constructor"\n'));
        editManifest(dist, (m) => {
          m.tables[0].review_states.rejected -= 1;
          m.tables[0].review_states["not-reviewed"] += 1;
        });
      },
    ],
  ];
  for (const [name, spoil] of cases) {
    const dist = distWithBundle();
    spoil(dist);
    assert.equal(readTestTables(dist).kind, "unreadable", name);
  }
});

test("readTestTables: a link out of dist_root is refused, never followed", (context) => {
  const dist = distWithBundle();
  const outside = temporaryDirectory("swcraft-test-tables-outside-");
  const target = path.join(outside, "components.csv");
  fs.renameSync(bundleFile(dist, "components.csv"), target);
  try {
    fs.symlinkSync(target, bundleFile(dist, "components.csv"));
  } catch {
    context.skip("this host cannot create a symbolic link");
    return;
  }
  const read = readTestTables(dist);
  assert.equal(read.kind, "unreadable");
  assert.match(read.kind === "unreadable" ? read.reason : "", /outside dist_root/);
});

test("the tab writes nothing: the bundle's file list and bytes are equal after reading and rendering", () => {
  const dist = distWithBundle();
  const directory = path.join(dist, TEST_TABLES_DIRECTORY);
  const before = snapshot(directory);
  const distBefore = fs.readdirSync(dist).sort();
  const read = readTables(dist);
  testTablesPage(read, { nonce: NONCE, annotations: "review" });
  testTablesEmptyPage(NONCE);
  testTablesUnreadablePage(NONCE);
  testTablesRefusalPage(NONCE);
  assert.deepEqual(snapshot(directory), before);
  assert.deepEqual(fs.readdirSync(dist).sort(), distBefore);
  // The unreadable and the absent cases read and write nothing either.
  fs.rmSync(path.join(directory, "components.csv"));
  const spoiled = snapshot(directory);
  assert.equal(readTestTables(dist).kind, "unreadable");
  assert.deepEqual(snapshot(directory), spoiled);
  const empty = temporaryDirectory();
  assert.equal(readTestTables(empty).kind, "absent");
  assert.deepEqual(fs.readdirSync(empty), []);
});

/** The `node:fs` writers, synchronous, callback and promise forms, each replaced by a refusal in the test below. */
const FS_WRITERS = [
  "appendFile",
  "chmod",
  "copyFile",
  "cp",
  "createWriteStream",
  "link",
  "mkdir",
  "mkdtemp",
  "rename",
  "rm",
  "rmdir",
  "symlink",
  "truncate",
  "unlink",
  "utimes",
  "write",
  "writeFile",
  "writev",
] as const;

test("testTablesTab.ts holds no file system write and keeps no webview or workspace state", async (context) => {
  // Every `node:fs` writer (the `Sync` and promise forms, and an `open` for writing) is replaced by
  // a refusal, and the host handed to the annotations pick refuses every member its interface does
  // not declare; then the tab's whole surface is driven. The one write a press makes, the setting,
  // goes through the host's `rememberAnnotations` and is `commands.ts`'. Running here under plain
  // node keeps `vscode`, its webview and workspace state out of the module. Not reached (the
  // source scan reaches them): a write on a branch none of these inputs takes, and a write by
  // another route than `node:fs`.
  const dist = distWithBundle();
  const absent = temporaryDirectory();
  const unreadable = distWithBundle();
  fs.rmSync(bundleFile(unreadable, "components.csv"));
  const nodeFs: typeof fs = createRequire(__filename)("node:fs");
  const refusals: string[] = [];
  const refuse = (name: string) => () => {
    refusals.push(name);
    throw new Error(`the Test Tables tab wrote through fs.${name}`);
  };
  for (const name of FS_WRITERS) {
    for (const [target, label] of [
      [nodeFs, name],
      [nodeFs, `${name}Sync`],
      [nodeFs.promises, `promises.${name}`],
    ] as const) {
      const key = label.replace("promises.", "");
      if (typeof (target as unknown as Record<string, unknown>)[key] === "function") {
        context.mock.method(target as unknown as Record<string, () => void>, key, refuse(label));
      }
    }
  }
  const openSync = nodeFs.openSync;
  context.mock.method(
    nodeFs,
    "openSync",
    (file: fs.PathLike, flags: fs.OpenMode = "r", mode?: fs.Mode) => {
      if (flags !== "r" && flags !== nodeFs.constants.O_RDONLY) {
        refuse("openSync")();
      }
      return openSync(file, flags, mode);
    },
  );
  const declared = new Set([
    "projectDir",
    "rememberedAnnotations",
    "pickAnnotations",
    "pickAnnotationsFolder",
    "rememberAnnotations",
  ]);
  const strictHost = (host: FakeHost): FakeHost =>
    new Proxy(host, {
      get(target, property, receiver) {
        if (typeof property === "string" && !declared.has(property)) {
          refusals.push(`host.${property}`);
          throw new Error(`the Test Tables tab asked its host for ${property}`);
        }
        return Reflect.get(target, property, receiver);
      },
    });

  const read = readTables(dist);
  testTablesPage(read, { nonce: NONCE, annotations: "review" });
  assert.equal(readTestTables(absent).kind, "absent");
  assert.equal(readTestTables(unreadable).kind, "unreadable");
  testTablesEmptyPage(NONCE);
  testTablesUnreadablePage(NONCE);
  testTablesRefusalPage(NONCE);
  acceptTestTablesMessage({ type: "generate" });
  const asked = fakeHost(undefined, { pick: "none" });
  assert.deepEqual(await askTestTablesAnnotations(strictHost(asked)), { annotations: undefined });
  assert.deepEqual(asked.written, [""]);
  const stored = fakeHost("review", {});
  await askTestTablesAnnotations(strictHost(stored), { change: false });
  assert.deepEqual(stored.written, []);

  assert.deepEqual(refusals, []);
});

// --- The pages -----------------------------------------------------------------

function allPages(annotations: unknown = "review"): Array<[string, string]> {
  const read = readTables(distWithBundle());
  return [
    ["tables", testTablesPage(read, { nonce: NONCE, annotations })],
    ["empty", testTablesEmptyPage(NONCE)],
    ["unreadable", testTablesUnreadablePage(NONCE)],
    ["refusal", testTablesRefusalPage(NONCE)],
  ];
}

test("every page carries the review tab's CSP with its nonce, on its one style and one script", () => {
  for (const [name, html] of allPages()) {
    assert.ok(
      html.includes(`<meta http-equiv="Content-Security-Policy" content="${reviewCsp(NONCE)}">`),
      name,
    );
    assert.equal((html.match(/<style nonce="/g) ?? []).length, 1, name);
    assert.equal((html.match(/<script nonce="/g) ?? []).length, 1, name);
    assert.equal((html.match(/<script/g) ?? []).length, 1, name);
    assert.doesNotMatch(html, /\son[a-z]+=/i, `${name} carries an inline event handler`);
  }
  assert.throws(() => testTablesEmptyPage("not a nonce!"));
});

test("the tab's style uses theme variables only: no colour literal", () => {
  assert.doesNotMatch(TEST_TABLES_STYLE, /#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(|hwb\(|oklch\(/);
  // Every declaration that can carry a colour names a theme variable, or none.
  const declarations = [...TEST_TABLES_STYLE.matchAll(/([a-z-]+):\s*([^;}]+)/g)];
  const colourBearing = declarations.filter(([, property]) =>
    /(^|-)color$|^background$|^border(-(top|right|bottom|left))?$/.test(property),
  );
  assert.ok(colourBearing.length > 10, "the style's colour declarations are read");
  for (const [, property, raw] of colourBearing) {
    const value = raw.trim();
    assert.ok(
      /var\(--vscode-[\w-]+\)/.test(value) ||
        /^(none|transparent|2px solid transparent)$/.test(value),
      `${property}: ${value}`,
    );
  }
  // The review_state of a tinted row is a badge per theme kind: a pill of the state's
  // saturated colour with a contrasting text colour, both theme variables; in high
  // contrast a frame of the state's colour round the theme's foreground, no fill.
  for (const kind of ["dark", "light"] as const) {
    for (const state of Object.keys(ROW_TINTS)) {
      const { fill, text } = STATE_BADGES[kind][state];
      assert.match(
        fill,
        /^(var\(--vscode-charts-[a-z]+\)|color-mix\(in srgb, var\(--vscode-charts-[a-z]+\) \d+%, var\(--vscode-[\w-]+\)\))$/,
      );
      assert.match(text, /^--vscode-(editor-background|foreground)$/);
      assert.ok(
        TEST_TABLES_STYLE.includes(
          `body.vscode-${kind} tbody tr.tinted > td.${REVIEW_STATE_CLASSES[state]} > .cell { color: var(${text}); background-color: ${fill}; }`,
        ),
        `${kind} ${state}`,
      );
    }
  }
  assert.match(
    TEST_TABLES_STYLE,
    /tbody tr\.tinted > td\.badge > \.cell \{ display: inline-block;[^}]*border-radius: 9px; font-weight: 600; \}/,
  );
  // Dark text on the light charts colours of a dark theme; dark text on the orange of a light one.
  assert.equal(STATE_BADGES.dark.stale.text, "--vscode-editor-background");
  assert.equal(STATE_BADGES.light.stale.text, "--vscode-foreground");
  assert.equal(STATE_BADGES.light.accepted.text, "--vscode-editor-background");
  for (const [state, { colour }] of Object.entries(ROW_TINTS)) {
    assert.ok(
      TEST_TABLES_STYLE.includes(
        `body.vscode-high-contrast tbody tr.tinted > td.${REVIEW_STATE_CLASSES[state]} > .cell { color: var(--vscode-foreground); box-shadow: inset 0 0 0 2px var(${colour}); }`,
      ),
      state,
    );
  }
  // The hovered row's overlay never covers a badge: the overlay is on the cell, the badge's fill on its pill.
  assert.doesNotMatch(TEST_TABLES_STYLE, /tr:hover[^{]*\.cell/);
  assert.match(
    TEST_TABLES_STYLE,
    /\ntd\.state-not-reviewed \{ color: var\(--vscode-descriptionForeground\); \}/,
  );
  assert.doesNotMatch(TEST_TABLES_STYLE, /state-not-reviewed[^}]*background/);
});

test("nothing on any page is editable: no input, no form, no contenteditable, no textarea, no select", () => {
  for (const [name, html] of allPages()) {
    assert.doesNotMatch(html, /<input|<form|contenteditable|<textarea|<select/i, name);
  }
});

test("no page names a location: the sentinel directory never appears", () => {
  const absolute = path.join(os.tmpdir(), SENTINEL, "annotations");
  for (const annotations of ["review", "", undefined, absolute]) {
    for (const [name, html] of allPages(annotations)) {
      assert.equal(html.includes(SENTINEL), false, `${name} with ${String(annotations)}`);
      assert.equal(html.includes(os.tmpdir()), false, `${name} names the temporary directory`);
    }
  }
  const page = allPages(absolute)[0][1];
  assert.ok(page.includes("a folder outside the project (annotations)"));
});

test("the tables page: one sub-tab per file, the header pinned with its tooltips, the states classed", () => {
  const read = readTables(distWithBundle());
  const html = testTablesPage(read, { nonce: NONCE, annotations: "review" });
  assert.deepEqual(
    [...html.matchAll(/<button type="button" role="tab"[^>]*>([^<]*)<\/button>/g)].map((m) => m[1]),
    ["Components", "Component placement", "Runnable scheduling", "Orphaned annotations (1)"],
  );
  assert.equal((html.match(/<section class="panel"/g) ?? []).length, 4);
  assert.equal((html.match(/<section class="panel"[^>]* hidden>/g) ?? []).length, 3);
  assert.match(TEST_TABLES_STYLE, /th \{ position: sticky; top: 0;/);
  assert.match(TEST_TABLES_STYLE, /\.panel \{ min-height: 0; overflow: auto;/);
  assert.match(TEST_TABLES_STYLE, /th, td \{[^}]*white-space: nowrap;/);
  // Every header cell carries its name, owner and meaning from the manifest.
  const scheduling = read.tables[2];
  for (const column of scheduling.columns) {
    const owner = column.owner === "test" ? "the test team's column" : "SWCraft's column";
    assert.ok(
      html.includes(
        `title="${escapeHtml(`${column.name}: ${owner} (owner: ${column.owner}). ${column.meaning}`)}"><div class="cell">${escapeHtml(column.name)}</div><span class="grip" aria-hidden="true"></span></th>`,
      ),
      column.name,
    );
  }
  for (const [state, className] of Object.entries(REVIEW_STATE_CLASSES)) {
    const classes = state in ROW_TINTS ? `${className} badge` : className;
    assert.ok(
      html.includes(
        `<td class="${classes}" title="${state}"><div class="cell">${state}</div></td>`,
      ),
      state,
    );
  }
  // The top line: the manifest's project and version, the counts, the folder and its Change….
  assert.ok(html.includes("Project <b>swcraft-basic-example</b>"));
  assert.ok(html.includes("Annotations <b>review</b>"));
  assert.ok(html.includes("Components: 2 rows (1 accepted, 0 stale, 1 rejected, 0 not reviewed)"));
  assert.ok(html.includes("Orphaned annotations: 1"));
  assert.match(html, /data-message="changeAnnotations">Change…<\/button>/);
  assert.match(html, /data-message="generate">Generate<\/button>/);
  // A cell's line breaks and quotes reach the page escaped, as its text and its hover.
  assert.ok(
    html.includes("Publishes, &quot;quoted&quot; state\rafter a CR\nafter an LF\r\nafter a CRLF"),
  );
});

// --- The layout and the cells ----------------------------------------------------

test("formatList: a JSON list of names as plain comma-separated text, [] as (none)", () => {
  assert.equal(formatList('["Task_50ms","Task_100ms"]'), "Task_50ms, Task_100ms");
  assert.equal(formatList('["CORE0"]'), "CORE0");
  assert.equal(formatList("[]"), EMPTY_LIST_TEXT);
  for (const raw of ["", "Task_50ms", "[1]", '{"a":"b"}', "[", '["a",null]']) {
    assert.equal(formatList(raw), undefined, raw);
  }
});

test("formatEvents: each event its type and binding, disabled modes by their last segment", () => {
  assert.equal(
    formatEvents('[{"disabled_modes":[],"period_s":0.01,"type":"timing"}]'),
    "timing 0.01 s",
  );
  assert.equal(
    formatEvents(
      '[{"disabled_modes":[],"name":"TMT","period_s":0.05,"type":"timing"},{"name":"IT","type":"init"}]',
    ),
    "timing 0.05 s; init",
  );
  assert.equal(
    formatEvents(
      '[{"name":"OIT","operation":"ReadSnapshot","port":"pBrakeDiagnostics","type":"operation"}]',
    ),
    "operation pBrakeDiagnostics.ReadSnapshot",
  );
  assert.equal(
    formatEvents(
      '[{"disabled_modes":[{"mode_group_ref":"/P/I/EcuMode/currentMode","mode_ref":"/P/M/EcuMode/STARTUP","port":"rMode"},{"mode_group_ref":"/P/I/EcuMode/currentMode","mode_ref":"/P/M/EcuMode/SHUTDOWN","port":"rMode"}],"element":"Speed","port":"rSpeed","type":"data"}]',
    ),
    "data rSpeed.Speed (disabled in STARTUP, SHUTDOWN)",
  );
  // A returns event names its port and operation as the operation event does; a stated
  // result point's name is left to the hover, as an event's `name` is.
  assert.equal(
    formatEvents(
      '[{"operation":"ReadSnapshot","port":"rDiag","type":"call_returned"},{"name":"CR","operation":"Reset","port":"rNvm","result":"ResetDone","type":"call_returned"}]',
    ),
    "call_returned rDiag.ReadSnapshot; call_returned rNvm.Reset",
  );
  assert.equal(formatEvents("[]"), EMPTY_LIST_TEXT);
  for (const raw of ["", "timing", "[1]", '[{"period_s":0.01}]', '{"type":"timing"}']) {
    assert.equal(formatEvents(raw), undefined, raw);
  }
});

test("formatHash: the first eight characters; none, blank and short texts as read", () => {
  assert.equal(formatHash("3530bb7789909c6c"), "3530bb77");
  assert.equal(formatHash("none"), "none");
  assert.equal(formatHash(""), "");
  assert.equal(formatHash("abc"), "abc");
});

test("formatCell: drawn by column, the cell as read always its hover, [] muted and blank not", () => {
  assert.deepEqual(formatCell("tasks", '["Task_50ms","Task_100ms"]'), {
    text: "Task_50ms, Task_100ms",
    title: '["Task_50ms","Task_100ms"]',
    muted: false,
  });
  assert.deepEqual(formatCell("cores", "[]"), { text: EMPTY_LIST_TEXT, title: "[]", muted: true });
  // A blank cell (not stated) stays blank, so it reads apart from a declared empty list.
  assert.deepEqual(formatCell("tasks", ""), { text: "", title: "", muted: false });
  assert.deepEqual(formatCell("facts_hash", "e1eeadaa49177bc8"), {
    text: "e1eeadaa",
    title: "e1eeadaa49177bc8",
    muted: false,
  });
  assert.deepEqual(formatCell("accepted_facts_hash", "none"), {
    text: "none",
    title: "none",
    muted: false,
  });
  assert.equal(
    formatCell("events", '[{"disabled_modes":[],"period_s":0.1,"type":"timing"}]').text,
    "timing 0.1 s",
  );
  // Another column, or a value the rules do not recognise, is drawn as read.
  assert.deepEqual(formatCell("description", "a, b\nc"), {
    text: "a, b\nc",
    title: "a, b\nc",
    muted: false,
  });
  assert.equal(formatCell("tasks", "not json").text, "not json");
  assert.equal(formatCell("memory", '{"RAM0":2048}').text, '{"RAM0":2048}');
});

test("the layout: no break inside a word, one line capped with an ellipsis, a frozen first column", () => {
  const breaking =
    /word-break:\s*break-(all|word)|overflow-wrap:\s*(anywhere|break-word)|word-wrap:\s*break-word|hyphens:\s*auto/;
  assert.doesNotMatch(TEST_TABLES_STYLE, breaking);
  // The shared review style breaks words only inside its <pre>, which no test-tables page holds.
  for (const [name, html] of allPages()) {
    assert.doesNotMatch(html, /<pre/, name);
    assert.doesNotMatch(html.replace(/\npre \{[^}]*\}/, ""), breaking, name);
  }
  assert.equal(TEST_TABLES_COLUMN_CAP, "40ch");
  assert.match(TEST_TABLES_STYLE, /table \{ table-layout: auto;/);
  assert.match(TEST_TABLES_STYLE, /th, td \{[^}]*white-space: nowrap;/);
  assert.match(
    TEST_TABLES_STYLE,
    /\.cell \{ max-width: 40ch; overflow: hidden; text-overflow: ellipsis;\s+white-space: nowrap; \}/,
  );
  assert.match(TEST_TABLES_STYLE, /\.panel \{ min-height: 0; overflow: auto;/);
  // The first column is frozen; the corner cell sits above the pinned header and the frozen column.
  assert.match(TEST_TABLES_STYLE, /td:first-child \{ position: sticky; left: 0; z-index: 1; \}/);
  assert.match(TEST_TABLES_STYLE, /th \{ position: sticky; top: 0; z-index: 2;/);
  assert.match(TEST_TABLES_STYLE, /th:first-child \{ left: 0; z-index: 3; \}/);
});

test("a header is never cut: the CSV's column names as written, sized whole, never under the frozen column", () => {
  const read = readTables(distWithBundle());
  const html = testTablesPage(read, { nonce: NONCE, annotations: "review" });
  // Each sub-tab's header cells are the CSV's own header line, name for name, unchanged.
  const panels = html.split('<section class="panel"').slice(1);
  assert.equal(panels.length, 4);
  [...read.tables, read.orphans].forEach((table, index) => {
    const header = parseCsv(fs.readFileSync(path.join(FIXTURE, table.file), "utf-8"))?.[0];
    const shown = [
      ...panels[index].matchAll(/<th scope="col"[^>]*><div class="cell">([^<]*)<\/div>/g),
    ].map((m) => m[1]);
    assert.deepEqual(shown, header?.map(escapeHtml), table.file);
  });
  assert.ok(html.includes('<div class="cell">verification_method</div>'));
  // The cap and its ellipsis never apply to a header: it is as wide as its full name, and a
  // grip drag stops at that width.
  assert.match(TEST_TABLES_STYLE, /\nth > \.cell \{ max-width: none; overflow: visible; \}/);
  // The stop is the header text's own width (a Range over the label's contents), never the
  // label's scrollWidth: the label is as wide as its column, so that would be the current width
  // and a widened column could never be narrowed again.
  assert.match(TEST_TABLES_SCRIPT, /text\.selectNodeContents\(label\);/);
  assert.match(
    TEST_TABLES_SCRIPT,
    /const narrowest = Math\.max\(24, Math\.ceil\(text\.getBoundingClientRect\(\)\.width\)\);/,
  );
  assert.doesNotMatch(TEST_TABLES_SCRIPT, /scrollWidth/);
  assert.match(TEST_TABLES_SCRIPT, /Math\.max\(narrowest, /);
  // The sideways scroll snaps a column's start to the frozen column's right edge, so the frozen
  // column never covers the start of a header; the script keeps the frozen width and the room
  // at the end that lets the last columns snap there too.
  assert.match(
    TEST_TABLES_STYLE,
    /\.panel \{[^}]*scroll-snap-type: x mandatory; scroll-padding-left: var\(--frozen-width, 0px\); \}/,
  );
  assert.match(TEST_TABLES_STYLE, /\nth:not\(:first-child\) \{ scroll-snap-align: start; \}/);
  assert.match(TEST_TABLES_SCRIPT, /panel\.style\.setProperty\("--frozen-width", frozen \+ "px"\)/);
  assert.match(TEST_TABLES_SCRIPT, /panel\.style\.paddingRight = room \+ "px"/);
  // The room is measured against the viewport, padding included, never against the width
  // the room itself narrows: subtracting the padding it set made a second fit() undo the first.
  assert.match(TEST_TABLES_SCRIPT, /const viewport = panel\.clientWidth;/);
  assert.match(TEST_TABLES_SCRIPT, /Math\.max\(0, viewport - frozen - last\.offsetWidth\)/);
  assert.doesNotMatch(TEST_TABLES_SCRIPT, /parseFloat\(panel\.style\.paddingRight\)/);
  assert.match(TEST_TABLES_SCRIPT, /new ResizeObserver\(/);
});

test("the tables page: every non-empty cell carries the cell as read as its title; hashes and lists drawn short", () => {
  const read = readTables(distWithBundle());
  const html = testTablesPage(read, { nonce: NONCE, annotations: "review" });
  for (const table of [...read.tables, read.orphans]) {
    for (const row of table.rows) {
      row.forEach((cell, index) => {
        const view = formatCell(table.columns[index].name, cell);
        if (cell === "") {
          return;
        }
        assert.ok(
          html.includes(
            `title="${escapeHtml(cell)}"><div class="cell">${escapeHtml(view.text)}</div>`,
          ),
          `${table.file} ${table.columns[index].name}`,
        );
      });
    }
  }
  assert.ok(html.includes('title="3530bb7789909c6c"><div class="cell">3530bb77</div>'));
  assert.ok(html.includes('<div class="cell">Task_10ms</div>'));
  assert.ok(html.includes('class="muted" title="[]"><div class="cell">(none)</div>'));
  assert.ok(html.includes('<div class="cell">timing 0.01 s</div>'));
  // Each header cell carries a resize grip, and the script resizes in the page alone.
  const headers = (html.match(/<th scope="col"/g) ?? []).length;
  assert.equal(
    (html.match(/<span class="grip" aria-hidden="true"><\/span><\/th>/g) ?? []).length,
    headers,
  );
  assert.match(TEST_TABLES_SCRIPT, /addEventListener\("pointerdown"/);
  assert.match(TEST_TABLES_SCRIPT, /setPointerCapture/);
  assert.match(TEST_TABLES_STYLE, /\.grip \{[^}]*cursor: col-resize;/);
  assert.doesNotMatch(
    TEST_TABLES_SCRIPT,
    /getState|setState|localStorage|sessionStorage|indexedDB|postMessage\(\{ type: "resize/,
  );
});

test("the rows: tinted by review_state over theme variables, untinted rows striped, the hovered row overlaid", () => {
  const read = readTables(distWithBundle());
  const html = testTablesPage(read, { nonce: NONCE, annotations: "review" });
  for (const [state, { colour }] of Object.entries(ROW_TINTS)) {
    const rowClass = rowClassOf(state);
    assert.match(html, new RegExp(`<tr class="tinted ${rowClass}">`), state);
    for (const kind of ["dark", "light"] as const) {
      // The row's muted tint, its share per theme kind.
      assert.ok(
        TEST_TABLES_STYLE.includes(
          `body.vscode-${kind} tbody tr.${rowClass} > td { background-color: color-mix(in srgb, var(${colour}) ${ROW_TINT_SHARES[kind][state]}, var(--vscode-editor-background)); }`,
        ),
        `${kind} ${state}`,
      );
      // A narrow solid bar of the state's colour on the first (frozen) cell, in normal themes too.
      assert.ok(
        TEST_TABLES_STYLE.includes(
          `body.vscode-${kind} tbody tr.${rowClass} > td:first-child { box-shadow: inset ${ROW_BAR_WIDTH} 0 0 ${ROW_BARS[kind](colour)}; }`,
        ),
        `${kind} ${state}`,
      );
    }
    // In a high-contrast theme: the same bar, no fill.
    assert.ok(
      TEST_TABLES_STYLE.includes(
        `body.vscode-high-contrast tbody tr.${rowClass} > td:first-child { box-shadow: inset 4px 0 0 var(${colour}); }`,
      ),
      state,
    );
  }
  assert.equal(ROW_BAR_WIDTH, "4px");
  assert.equal(ROW_BARS.dark("--vscode-charts-red"), "var(--vscode-charts-red)");
  assert.equal(
    ROW_BARS.light("--vscode-charts-red"),
    "color-mix(in srgb, var(--vscode-charts-red) 80%, var(--vscode-editor-foreground))",
  );
  assert.deepEqual(Object.keys(ROW_TINTS).sort(), ["accepted", "rejected", "stale"]);
  // stale is orange, a hue apart from red, not yellow; a light high-contrast theme, whose
  // charts orange is a blue, takes its charts yellow (an amber).
  assert.equal(ROW_TINTS.accepted.colour, "--vscode-charts-green");
  assert.equal(ROW_TINTS.stale.colour, "--vscode-charts-orange");
  assert.equal(ROW_TINTS.rejected.colour, "--vscode-charts-red");
  assert.ok(
    TEST_TABLES_STYLE.includes(
      "body.vscode-high-contrast-light tbody tr.row-stale > td:first-child { box-shadow: inset 4px 0 0 var(--vscode-charts-yellow); }",
    ),
  );
  for (const line of TEST_TABLES_STYLE.split("\n").filter((rule) =>
    rule.includes("charts-yellow"),
  )) {
    assert.ok(line.startsWith("body.vscode-high-contrast-light "), line);
  }
  // The rejected and stale tints use their own shares, and stale's is the stronger.
  for (const kind of ["dark", "light"] as const) {
    assert.ok(
      Number.parseInt(ROW_TINT_SHARES[kind].stale, 10) >
        Number.parseInt(ROW_TINT_SHARES[kind].rejected, 10),
      kind,
    );
  }
  // not-reviewed rows and the orphan file's rows are untinted.
  const scheduling = html.slice(html.indexOf('id="panel-2"'), html.indexOf('id="panel-3"'));
  assert.equal(
    (scheduling.match(/<tr>/g) ?? []).length,
    1 + 1,
    "the header row and the not-reviewed row",
  );
  const orphans = html.slice(html.indexOf('id="panel-3"'));
  assert.doesNotMatch(orphans, /<tr class=/);
  // Every cell is opaque, so the frozen cell takes its row's tint, stripe and hover alike.
  assert.match(
    TEST_TABLES_STYLE,
    /\ntd \{ background-color: var\(--vscode-editor-background\); \}/,
  );
  assert.match(
    TEST_TABLES_STYLE,
    /tbody tr:nth-child\(even\):not\(\.tinted\) > td \{ background-color: color-mix\(in srgb,\s+var\(--vscode-foreground\) 4%, var\(--vscode-editor-background\)\); \}/,
  );
  assert.match(
    TEST_TABLES_STYLE,
    /tbody tr:hover > td \{ background-image: linear-gradient\(var\(--vscode-list-hoverBackground\),\s+var\(--vscode-list-hoverBackground\)\); \}/,
  );
  assert.match(
    TEST_TABLES_STYLE,
    /body\.vscode-high-contrast tbody tr:nth-child\(n\) > td \{ background-color: var\(--vscode-editor-background\); \}/,
  );
  assert.match(
    TEST_TABLES_STYLE,
    /body\.vscode-high-contrast tbody tr:hover > td \{ background-image: none;\s+outline: 1px dashed var\(--vscode-contrastActiveBorder\);/,
  );
  // No background rule names the frozen column alone: it never shows a colour of its own.
  assert.doesNotMatch(TEST_TABLES_STYLE, /td:first-child \{[^}]*background/);
  // Theme variables only: every colour in a row rule is a --vscode- variable.
  for (const rule of TEST_TABLES_STYLE.split("\n").filter((line) =>
    /^(tbody|body\.vscode)/.test(line),
  )) {
    assert.doesNotMatch(
      rule,
      /#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(|(?<![\w-])(red|green|yellow|white|black)\b/,
      rule,
    );
  }
});

test("the one-line pages: the empty state, the unreadable bundle and the refusal, each with Generate", () => {
  for (const [html, line] of [
    [testTablesEmptyPage(NONCE), TEST_TABLES_EMPTY_LINE],
    [testTablesUnreadablePage(NONCE), TEST_TABLES_UNREADABLE_LINE],
    [testTablesRefusalPage(NONCE), TEST_TABLES_REFUSED_LINE],
  ]) {
    assert.ok(html.includes(escapeHtml(line)), line);
    assert.match(html, /data-message="generate">Generate<\/button>/);
    assert.doesNotMatch(html, /<table/);
    assert.doesNotMatch(html, /changeAnnotations/);
  }
});

test("a table with no rows: one 'No rows.' cell across every column, and the orphan tab's count 0", () => {
  const read = readTables(distWithBundle());
  const empty = { ...read, orphans: { ...read.orphans, rows: [] } };
  const html = testTablesPage(empty, { nonce: NONCE, annotations: "review" });
  assert.ok(
    html.includes(
      `<tr><td class="empty-table" colspan="${read.orphans.columns.length}">No rows.</td></tr>`,
    ),
  );
  assert.equal((html.match(/class="empty-table"/g) ?? []).length, 1, "only the empty table");
  assert.match(html, /role="tab"[^>]*>Orphaned annotations \(0\)<\/button>/);
  assert.ok(html.includes("<span>Orphaned annotations: 0</span>"));
});

test("a data cell's markup is escaped: its text and its title, never a tag", () => {
  const read = readTables(distWithBundle());
  const hostile = '</td><script>alert("x")</script> & <b>';
  const orphans = {
    ...read.orphans,
    rows: [read.orphans.rows[0].map((cell, index) => (index === 3 ? hostile : cell))],
  };
  const html = testTablesPage({ ...read, orphans }, { nonce: NONCE, annotations: "review" });
  const escaped = escapeHtml(hostile);
  assert.ok(html.includes(`title="${escaped}"><div class="cell">${escaped}</div></td>`));
  assert.ok(!html.includes(hostile));
  const scripts = html.match(/<script/g) ?? [];
  const reference =
    testTablesPage(read, { nonce: NONCE, annotations: "review" }).match(/<script/g) ?? [];
  assert.equal(scripts.length, reference.length, "no script beyond the page's own");
});

test("a review_state of Object.prototype's names is no state: no class, no tint", () => {
  const read = readTables(distWithBundle());
  const table = read.tables[0];
  const stateColumn = table.columns.findIndex((column) => column.name === "review_state");
  for (const name of ["constructor", "toString", "__proto__"]) {
    const rows = table.rows.map((row) =>
      row.map((cell, index) => (index === stateColumn ? name : cell)),
    );
    const html = testTablesPage(
      { ...read, tables: [{ ...table, rows }, ...read.tables.slice(1)] },
      { nonce: NONCE, annotations: "review" },
    );
    assert.ok(html.includes(`<td title="${name}"><div class="cell">${name}</div></td>`), name);
    assert.doesNotMatch(html, /class="tinted (?!row-(accepted|stale|rejected)")/, name);
    assert.doesNotMatch(html, /class="[^"]*(undefined|function|\[object)/, name);
  }
});

test("the script enables the action buttons again on the host's ready message, and on nothing else", () => {
  assert.deepEqual(TEST_TABLES_READY_MESSAGE, { type: "ready" });
  assert.ok(
    TEST_TABLES_SCRIPT.includes(
      `window.addEventListener("message", (event) => {
    if (event.data !== null && typeof event.data === "object" && event.data.type === "ready") {
      for (const each of actions) each.disabled = false;
    }
  });`,
    ),
  );
  assert.equal((TEST_TABLES_SCRIPT.match(/each\.disabled = false/g) ?? []).length, 1);
});

test("describeAnnotations: relative as stored, absolute by its last segment, none yet, not chosen", () => {
  assert.equal(describeAnnotations("../x"), "../x");
  assert.equal(describeAnnotations("review/team"), "review/team");
  assert.equal(
    describeAnnotations("/home/someone/team-notes"),
    "a folder outside the project (team-notes)",
  );
  assert.equal(
    describeAnnotations("C:\\Users\\someone\\notes\\"),
    "a folder outside the project (notes)",
  );
  assert.equal(describeAnnotations(""), "none yet");
  assert.equal(describeAnnotations("   "), "none yet");
  assert.equal(describeAnnotations(undefined), "not chosen yet");
  assert.equal(describeAnnotations(3), "not chosen yet");
});

// --- The message check ---------------------------------------------------------

test("acceptTestTablesMessage: the two messages, and nothing else", () => {
  assert.equal(acceptTestTablesMessage({ type: "generate" }), "generate");
  assert.equal(acceptTestTablesMessage({ type: "changeAnnotations" }), "changeAnnotations");
  for (const message of [
    undefined,
    null,
    "generate",
    ["generate"],
    {},
    { type: "Generate" },
    { type: "apply" },
    { type: "generate", extra: 1 },
    { type: ["generate"] },
    Object.assign(Object.create({ inherited: true }), { type: "generate" }),
  ]) {
    assert.equal(acceptTestTablesMessage(message), undefined, JSON.stringify(message));
  }
});

// --- The annotations pick ------------------------------------------------------

interface FakeHost extends TestTablesAnnotationsHost {
  picks: AnnotationsPickItem[][];
  dialogs: number;
  written: string[];
}

function fakeHost(
  stored: unknown,
  answers: { pick?: AnnotationsChoiceAnswer; folder?: string },
  projectDir = path.join(os.tmpdir(), "project"),
): FakeHost {
  const host: FakeHost = {
    projectDir,
    picks: [],
    dialogs: 0,
    written: [],
    rememberedAnnotations: () => stored,
    pickAnnotations: async (items) => {
      host.picks.push(items);
      return answers.pick === undefined
        ? undefined
        : items.find((item) => item.choice === answers.pick);
    },
    pickAnnotationsFolder: async () => {
      host.dialogs += 1;
      return answers.folder;
    },
    rememberAnnotations: async (value) => {
      host.written.push(value);
    },
  };
  return host;
}

type AnnotationsChoiceAnswer = AnnotationsPickItem["choice"];

test("the pick: the first press asks; a folder inside the project is stored relative", async () => {
  const host = fakeHost(undefined, {
    pick: "choose",
    folder: path.join(os.tmpdir(), "project", "review"),
  });
  const answer = await askTestTablesAnnotations(host);
  assert.deepEqual(answer, { annotations: path.join(os.tmpdir(), "project", "review") });
  assert.deepEqual(
    host.picks[0].map((item) => item.choice),
    ["choose", "none"],
  );
  assert.equal(host.dialogs, 1);
  assert.deepEqual(host.written, ["review"]);
});

test("the pick: a folder outside the project is stored absolute", async () => {
  const outside = path.join(os.tmpdir(), "elsewhere", "annotations");
  const host = fakeHost(undefined, { pick: "choose", folder: outside });
  assert.deepEqual(await askTestTablesAnnotations(host), { annotations: outside });
  assert.deepEqual(host.written, [outside]);
});

test("the pick: none yet is stored as an empty string, and the next press does not ask", async () => {
  const first = fakeHost(undefined, { pick: "none" });
  assert.deepEqual(await askTestTablesAnnotations(first), { annotations: undefined });
  assert.deepEqual(first.written, [""]);
  const next = fakeHost("", {});
  assert.deepEqual(await askTestTablesAnnotations(next), { annotations: undefined });
  assert.deepEqual(next.picks, []);
  assert.deepEqual(next.written, []);
});

test("the pick: a stored folder is used as stored, resolved against the project, never re-written", async () => {
  const host = fakeHost("review", {});
  assert.deepEqual(await askTestTablesAnnotations(host), {
    annotations: path.join(host.projectDir, "review"),
  });
  assert.deepEqual(host.picks, []);
  assert.deepEqual(host.written, []);
});

test("the pick: Esc at the pick or in the dialog ends the press with nothing written", async () => {
  const atPick = fakeHost(undefined, {});
  assert.equal(await askTestTablesAnnotations(atPick), undefined);
  assert.deepEqual(atPick.written, []);
  const inDialog = fakeHost(undefined, { pick: "choose" });
  assert.equal(await askTestTablesAnnotations(inDialog), undefined);
  assert.equal(inDialog.dialogs, 1);
  assert.deepEqual(inDialog.written, []);
});

test("the pick: Change… asks again, offering the stored folder as last used", async () => {
  const keep = fakeHost("review", { pick: "remembered" });
  assert.deepEqual(await askTestTablesAnnotations(keep, { change: true }), {
    annotations: path.join(keep.projectDir, "review"),
  });
  assert.deepEqual(
    keep.picks[0].map((item) => [item.choice, item.description]),
    [
      ["remembered", "last used"],
      ["choose", undefined],
      ["none", undefined],
    ],
  );
  assert.deepEqual(keep.written, []);
  const none = fakeHost("review", { pick: "none" });
  assert.deepEqual(await askTestTablesAnnotations(none, { change: true }), {
    annotations: undefined,
  });
  assert.deepEqual(none.written, [""]);
  // none yet stored: Change… offers no last used folder.
  assert.deepEqual(
    annotationsPickItems("").map((item) => item.choice),
    ["choose", "none"],
  );
});

// --- The argv ------------------------------------------------------------------

test("testTablesArgv: the project, and --annotations= as one token for an absolute folder", () => {
  assert.deepEqual(testTablesArgv("swcraft.yaml", undefined), [
    "generate",
    "test-tables",
    "--project",
    "swcraft.yaml",
  ]);
  const folder = path.join(os.tmpdir(), "-review");
  assert.deepEqual(testTablesArgv("swcraft.yaml", folder), [
    "generate",
    "test-tables",
    "--project",
    "swcraft.yaml",
    `--annotations=${folder}`,
  ]);
  assert.throws(() => testTablesArgv("swcraft.yaml", "review"), /must be an absolute path/);
});

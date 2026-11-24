// Pure logic of the *Test Tables* tab: the read-only view of the bundle `swcraft generate
// test-tables` writes under `<dist_root>/test-tables/`, the annotations folder's pick, and the
// press's argv. No `vscode` import: it runs under plain `node --test`; `commands.ts` hands in the
// webview, the dialogs, the setting and the run.
//
// The tables are artifacts, like the diagrams: to change one, change the model and regenerate. So
// the tab **reads** the CSV files and `manifest.json` and **writes nothing**: no file, no
// workspace, global or webview state (the chosen sub-tab is forgotten when the tab closes). The
// CSV files stay byte for byte what goes to the test team. A press writes the bundle (the CLI) and,
// at the pick, the one setting (`commands.ts`' `rememberWorkspaceSetting()`), neither the tab's.
//
// The page is built by the review tab's `reviewTabPage()` (its CSP, a fresh nonce per render),
// never a copy. Every colour is a VS Code theme variable. No text in the page names a file system
// location: every string is the manifest's, a cell's or a fixed line, HTML-escaped, and an
// absolute annotations folder is named by its last segment alone.
//
// What the tab is not: evidence of anything. It displays a projection of the model as the CLI
// wrote it; `swcraft validate` is the sole authoritative gate, and an `accepted` it shows is the
// test side's statement, not SWCraft's.

import * as fs from "node:fs";
import * as path from "node:path";

import {
  askSettingPath,
  rememberedSettingPath,
  type SettingPathChoice,
  settingPathArgFor,
} from "./commandCore";
import { escapeHtml, reviewTabPage } from "./reviewCore";

/** The webview's view type. */
export const TEST_TABLES_VIEW_TYPE = "swcraft.testTables";

/** The tab's title. */
export const TEST_TABLES_TAB_TITLE = "Test Tables";

/** The bundle's directory under `dist_root`, an entry of `project.ts`' `REPORT_DIRECTORIES`. */
export const TEST_TABLES_DIRECTORY = "test-tables";

/** The bundle's manifest, read first. */
export const TEST_TABLES_MANIFEST = "manifest.json";

/** The setting that remembers the annotations folder: the workspace folder's own value alone. */
export const TEST_TABLES_ANNOTATIONS_SETTING = "swcraft.testTables.annotations";

/** The empty state's one line. */
export const TEST_TABLES_EMPTY_LINE = "No test tables yet.";

/** The line a bundle that cannot be read shows: never a partial table. */
export const TEST_TABLES_UNREADABLE_LINE = "The test tables cannot be read; generate them again.";

/** The line a refused press shows; the CLI's own lines are in the output channel. */
export const TEST_TABLES_REFUSED_LINE =
  "The test tables were not generated. The reason is in the SWCraft output channel.";

/** The button that runs the press again: the empty state's, the refusal page's and the top line's. */
export const TEST_TABLES_GENERATE_BUTTON = "Generate";

/** The link that asks for the annotations folder again. */
export const TEST_TABLES_CHANGE_BUTTON = "Change…";

/** The sub-tab label of each table the bundle carries; a file not named here is labelled by its stem. */
export const TEST_TABLES_LABELS: Readonly<Record<string, string>> = {
  "components.csv": "Components",
  "component-placement.csv": "Component placement",
  "runnable-scheduling.csv": "Runnable scheduling",
  "orphaned-annotations.csv": "Orphaned annotations",
};

/**
 * The `review_state` cell's class per state. The colour rides on theme variables alone
 * ({@link TEST_TABLES_STYLE}) and the cell names the state in text too, so no meaning rides on
 * colour alone. A value outside the four is shown as text with no class.
 */
export const REVIEW_STATE_CLASSES: Readonly<Record<string, string>> = {
  accepted: "state-accepted",
  rejected: "state-rejected",
  stale: "state-stale",
  "not-reviewed": "state-not-reviewed",
};

/**
 * *record*'s own value at *key*, or `undefined`: a cell's text never reaches `Object.prototype`
 * (`constructor`, `__proto__`, `toString` are no state).
 */
function ownValue<T>(record: Readonly<Record<string, T>>, key: string): T | undefined {
  return Object.hasOwn(record, key) ? record[key] : undefined;
}

/** The class a tinted row of *state* carries beside `tinted`. */
export function rowClassOf(state: string): string {
  return `row-${state}`;
}

// --- The annotations folder ---------------------------------------------------------
//
// The requirements import's pattern and code path: a resource setting read as the workspace
// folder's own value, written by the adapter's one setting writer, stored as
// `storedSettingPath()` spells it and resolved by `settingPathArgFor()`. One difference: *none
// yet* is stored as `""`, an explicit answer, so only the first press asks; the mapping's *none*
// clears its setting because that import asks every time.

/** What an item of the annotations pick stands for. */
export type AnnotationsChoice = SettingPathChoice;

/** One item of the annotations pick: a `QuickPickItem`'s fields and the choice it stands for. */
export interface AnnotationsPickItem {
  label: string;
  description?: string;
  choice: AnnotationsChoice;
}

/** The annotations pick's title and placeholder. */
export const ANNOTATIONS_PICK_TEXT = {
  title: "SWCraft: the test team's annotations folder",
  placeHolder: "The folder of the test team's columns, joined into the tables",
} as const;

/** The title of the folder dialog behind *Choose a folder…*. */
export const ANNOTATIONS_DIALOG_TITLE = "SWCraft: select the test team's annotations folder";

/** The pick's items: the remembered folder (*last used*) when stored, *Choose a folder…*, *None yet*. */
export function annotationsPickItems(remembered: unknown): AnnotationsPickItem[] {
  const stored = rememberedSettingPath(remembered);
  return [
    ...(stored === undefined
      ? []
      : [
          {
            label: `$(folder) ${stored}`,
            description: "last used",
            choice: "remembered" as const,
          },
        ]),
    { label: "Choose a folder…", choice: "choose" },
    { label: "None yet (no annotations)", choice: "none" },
  ];
}

/** What the pick needs of its host; each prompt answers `undefined` for Esc. */
export interface TestTablesAnnotationsHost {
  /** The project's directory, which a folder inside it is stored relative to. */
  projectDir: string;
  /** The setting's value in the workspace folder alone (`inspect().workspaceFolderValue`). */
  rememberedAnnotations(): unknown;
  /** The pick over *items*; the item picked. */
  pickAnnotations(items: AnnotationsPickItem[]): Promise<AnnotationsPickItem | undefined>;
  /** The folder dialog behind *Choose a folder…*: the folder's absolute path. */
  pickAnnotationsFolder(): Promise<string | undefined>;
  /** Write *value* into the workspace folder's setting; a failed write is the host's channel line, never a throw. */
  rememberAnnotations(value: string): Promise<void>;
}

/**
 * The annotations folder of one press. Asked when the workspace folder's value is not a string
 * (never answered) or on *change* (the tab's **Change…**); otherwise the stored answer is used as
 * stored. `{ annotations }` is the absolute folder for the argv, `undefined` inside for *none yet*;
 * `undefined` itself is Esc, and the press ends before any run.
 *
 * The setting is written at the pick, before the run: a folder as `storedSettingPath()` spells it,
 * *none yet* as `""`. What the folder may be is the CLI's to refuse (inside a root, overlapping the
 * output, missing); nothing is checked here.
 */
export async function askTestTablesAnnotations(
  host: TestTablesAnnotationsHost,
  options: { change: boolean } = { change: false },
): Promise<{ annotations: string | undefined } | undefined> {
  const remembered = host.rememberedAnnotations();
  if (!options.change && typeof remembered === "string") {
    return { annotations: settingPathArgFor(remembered, host.projectDir) };
  }
  const answer = await askSettingPath({
    projectDir: host.projectDir,
    remembered,
    items: annotationsPickItems(remembered),
    pick: (items) => host.pickAnnotations(items),
    pickPath: () => host.pickAnnotationsFolder(),
    remember: (value) => host.rememberAnnotations(value),
    noneValue: "",
  });
  return answer === undefined ? undefined : { annotations: answer.path };
}

/**
 * The top line's name for the stored annotations folder: a project-relative name as stored, *a
 * folder outside the project* with its last segment alone for an absolute one (the page names no
 * location), *none yet* for `""`, and *not chosen yet* when never answered.
 */
export function describeAnnotations(stored: unknown): string {
  if (typeof stored !== "string") {
    return "not chosen yet";
  }
  const value = rememberedSettingPath(stored);
  if (value === undefined) {
    return "none yet";
  }
  if (path.posix.isAbsolute(value) || path.win32.isAbsolute(value)) {
    return `a folder outside the project (${lastSegment(value, /[\\/]+/) ?? ""})`;
  }
  return value;
}

/**
 * `swcraft generate test-tables --project <manifest> [--annotations=<dir>]`. The folder is joined
 * to its option with `=`, so argparse never reads a path starting with `-` as an option; it must
 * be absolute, so the CLI never resolves it against another working directory.
 */
export function testTablesArgv(manifestRelPath: string, annotations: string | undefined): string[] {
  if (annotations !== undefined && !path.isAbsolute(annotations)) {
    throw new Error(
      `testTablesArgv: the annotations folder must be an absolute path, got '${annotations}'`,
    );
  }
  return [
    "generate",
    "test-tables",
    "--project",
    manifestRelPath,
    ...(annotations === undefined ? [] : [`--annotations=${annotations}`]),
  ];
}

// --- The CSV parser ------------------------------------------------------------

/**
 * *text* as records of fields, RFC 4180 as the CLI writes it and nothing looser: every field
 * quoted, `""` an escaped quote, a field may hold `,`, `\r`, `\n` and `\r\n`; a record ends with
 * `\n` outside quotes, and so does the text. `undefined` for an empty text, an unquoted field, a
 * stray quote or a missing final `\n`: the bundle is generated, so a deviation means it was
 * edited. Field text is returned exactly as read.
 */
export function parseCsv(text: string): string[][] | undefined {
  if (text === "") {
    return undefined;
  }
  const records: string[][] = [];
  let record: string[] = [];
  let index = 0;
  while (index < text.length) {
    if (text[index] !== '"') {
      return undefined;
    }
    index += 1;
    let field = "";
    for (;;) {
      const quote = text.indexOf('"', index);
      if (quote === -1) {
        return undefined;
      }
      field += text.slice(index, quote);
      if (text[quote + 1] === '"') {
        field += '"';
        index = quote + 2;
        continue;
      }
      index = quote + 1;
      break;
    }
    record.push(field);
    const next = text[index];
    if (next === ",") {
      index += 1;
      if (index >= text.length) {
        return undefined;
      }
      continue;
    }
    if (next === "\n") {
      records.push(record);
      record = [];
      index += 1;
      continue;
    }
    return undefined;
  }
  return record.length === 0 ? records : undefined;
}

// --- The reader ----------------------------------------------------------------

/** One column as the manifest states it. */
export interface TestTablesColumn {
  name: string;
  owner: string;
  meaning: string;
}

/** One table of the bundle: its file, label, columns, rows and review-state counts. */
export interface TestTablesTable {
  file: string;
  label: string;
  columns: TestTablesColumn[];
  rows: string[][];
  /** The manifest's counts, `accepted`, `stale`, `rejected`, `not-reviewed`; none for the orphan file. */
  reviewStates?: Readonly<Record<string, number>>;
}

/** The reader's three answers. */
export type TestTablesRead =
  | { kind: "absent" }
  | { kind: "unreadable"; reason: string }
  | {
      kind: "tables";
      project: string;
      swcraftVersion: string;
      tables: TestTablesTable[];
      orphans: TestTablesTable;
    };

/** A table's file name: one plain segment ending in `.csv`. */
const TABLE_FILE = /^[a-z0-9][a-z0-9-]*\.csv$/;

class Unreadable extends Error {}

function refuse(reason: string): never {
  throw new Unreadable(reason);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function stringOf(value: unknown, what: string): string {
  return typeof value === "string" ? value : refuse(`${what} is not a string`);
}

function countOf(value: unknown, what: string): number {
  return typeof value === "number" && Number.isInteger(value) && value >= 0
    ? value
    : refuse(`${what} is not a count`);
}

function columnsOf(value: unknown, what: string): TestTablesColumn[] {
  if (!Array.isArray(value) || value.length === 0) {
    refuse(`${what}'s columns are not a list`);
  }
  return value.map((column, index) => {
    if (!isRecord(column)) {
      refuse(`${what}'s column ${index} is not an object`);
    }
    return {
      name: stringOf(column.name, `${what}'s column ${index} name`),
      owner: stringOf(column.owner, `${what}'s column ${index} owner`),
      meaning: stringOf(column.meaning, `${what}'s column ${index} meaning`),
    };
  });
}

/**
 * The text of *name* in *directory*, read only as a regular file whose real path lies inside
 * *realDist*, decoded as strict UTF-8 (a byte order mark is kept, so the parser refuses it: the
 * CLI writes none). A link out of `dist_root` is refused, never followed.
 */
function readInside(directory: string, name: string, realDist: string): string {
  let real: string;
  try {
    real = fs.realpathSync(path.join(directory, name));
  } catch {
    refuse(`${name} is missing`);
  }
  const relative = path.relative(realDist, real);
  if (relative === "" || path.isAbsolute(relative) || relative.split(path.sep)[0] === "..") {
    refuse(`${name} lies outside dist_root`);
  }
  if (!fs.statSync(real).isFile()) {
    refuse(`${name} is not a file`);
  }
  try {
    return new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(fs.readFileSync(real));
  } catch {
    refuse(`${name} is not UTF-8`);
  }
}

/** *file*'s table: parsed, its header the manifest's columns, its row count the manifest's. */
function readTable(
  directory: string,
  realDist: string,
  file: string,
  columns: TestTablesColumn[],
  rows: number,
  reviewStates?: Readonly<Record<string, number>>,
): TestTablesTable {
  if (!TABLE_FILE.test(file)) {
    refuse(`the manifest names '${file}', not a table file`);
  }
  const records = parseCsv(readInside(directory, file, realDist));
  if (records === undefined) {
    refuse(`${file} does not parse`);
  }
  const [header, ...body] = records;
  const names = columns.map((column) => column.name);
  if (header.length !== names.length || header.some((name, index) => name !== names[index])) {
    refuse(`${file}'s header is not the manifest's column list`);
  }
  if (body.length !== rows) {
    refuse(`${file} holds ${body.length} rows, the manifest says ${rows}`);
  }
  if (body.some((record) => record.length !== names.length)) {
    refuse(`${file} has a row of another width`);
  }
  if (reviewStates !== undefined) {
    // The top line's counts are the manifest's and the tints each row's: they must agree, and every
    // row's review_state is one of the four.
    const stateColumn = names.indexOf(REVIEW_STATE_COLUMN);
    if (stateColumn === -1) {
      refuse(`${file} has no review_state column`);
    }
    if (body.some((record) => !Object.hasOwn(REVIEW_STATE_CLASSES, record[stateColumn]))) {
      refuse(`${file} holds a review_state outside the four states`);
    }
    for (const state of Object.keys(REVIEW_STATE_CLASSES)) {
      const counted = body.filter((record) => record[stateColumn] === state).length;
      if (counted !== reviewStates[state]) {
        refuse(`${file} holds ${counted} ${state} rows, the manifest says ${reviewStates[state]}`);
      }
    }
  }
  const label = TEST_TABLES_LABELS[file] ?? file.replace(/\.csv$/, "");
  return {
    file,
    label,
    columns,
    rows: body,
    ...(reviewStates === undefined ? {} : { reviewStates }),
  };
}

/**
 * The bundle under `<distRoot>/test-tables/`: `manifest.json` first, then each listed table and
 * the orphan file. **absent**: no directory or no manifest; **unreadable**: a manifest not JSON or
 * not this shape, a listed file missing or outside the real `dist_root`, a CSV that does not
 * parse, a header that is not the manifest's columns, a row or per-state count that is not the
 * manifest's, a `review_state` outside the four (never a partial table); **tables** otherwise.
 */
export function readTestTables(distRoot: string): TestTablesRead {
  const directory = path.join(distRoot, TEST_TABLES_DIRECTORY);
  if (!fs.existsSync(path.join(directory, TEST_TABLES_MANIFEST))) {
    return { kind: "absent" };
  }
  try {
    const realDist = fs.realpathSync(distRoot);
    let manifest: unknown;
    try {
      manifest = JSON.parse(readInside(directory, TEST_TABLES_MANIFEST, realDist));
    } catch (error) {
      if (error instanceof Unreadable) {
        throw error;
      }
      refuse("manifest.json is not JSON");
    }
    if (!isRecord(manifest)) {
      refuse("manifest.json is not an object");
    }
    const project = stringOf(manifest.project, "project");
    const swcraftVersion = stringOf(manifest.swcraft_version, "swcraft_version");
    if (!Array.isArray(manifest.tables) || manifest.tables.length === 0) {
      refuse("tables is not a list");
    }
    const tables = manifest.tables.map((entry, index) => {
      if (!isRecord(entry)) {
        refuse(`table ${index} is not an object`);
      }
      const file = stringOf(entry.file, `table ${index}'s file`);
      const states = entry.review_states;
      if (!isRecord(states)) {
        refuse(`${file}'s review_states is not an object`);
      }
      const reviewStates: Record<string, number> = {};
      for (const state of Object.keys(REVIEW_STATE_CLASSES)) {
        reviewStates[state] = countOf(states[state], `${file}'s ${state} count`);
      }
      return readTable(
        directory,
        realDist,
        file,
        columnsOf(entry.columns, file),
        countOf(entry.rows, `${file}'s rows`),
        reviewStates,
      );
    });
    const orphanEntry = manifest.orphaned_annotations;
    if (!isRecord(orphanEntry)) {
      refuse("orphaned_annotations is not an object");
    }
    const orphanFile = stringOf(orphanEntry.file, "the orphan file");
    const orphans = readTable(
      directory,
      realDist,
      orphanFile,
      columnsOf(orphanEntry.columns, orphanFile),
      countOf(orphanEntry.rows, "the orphan rows"),
    );
    const files = [...tables.map((table) => table.file), orphans.file];
    if (new Set(files).size !== files.length) {
      refuse("the manifest names one file twice");
    }
    return { kind: "tables", project, swcraftVersion, tables, orphans };
  } catch (error) {
    return { kind: "unreadable", reason: error instanceof Error ? error.message : String(error) };
  }
}

// --- The pages -----------------------------------------------------------------

/**
 * The widest a column is drawn before it is dragged wider: its content's width up to this cap,
 * then an ellipsis, the full value on hover. 40 characters hold every fact column of the bundle
 * whole but the longest ids, descriptions and comments.
 */
export const TEST_TABLES_COLUMN_CAP = "40ch";

/** A VS Code theme kind, by the class VS Code puts on a webview's `<body>`. */
export type ThemeKind = "dark" | "light";

/**
 * The colour of each tinted `review_state` and, for `stale`, the colour a light high-contrast
 * theme takes instead. `not-reviewed` is absent: its row is untinted.
 *
 * `stale` is orange, not yellow: in VS Code 1.145's default *Dark 2026*, `charts.yellow`
 * (`#E0B97F`) and `charts.red` (`#EF8773`) mixed into the background are a ΔE2000 of 7.6 apart,
 * hard to tell, and *Light 2026*'s `charts.yellow` is an olive (`#667309`) beside the green.
 * `charts.orange` is `#D18616`/`#CD861A` in the four default themes but `#0F4A85`, a blue, in
 * *Light High Contrast*, so that theme takes `charts.yellow` (`#895503`).
 */
export const ROW_TINTS: Readonly<
  Record<string, { colour: string; highContrastLightColour?: string }>
> = {
  accepted: { colour: "--vscode-charts-green" },
  stale: {
    colour: "--vscode-charts-orange",
    highContrastLightColour: "--vscode-charts-yellow",
  },
  rejected: { colour: "--vscode-charts-red" },
};

/**
 * The share of each state's colour in its row's tint, per theme kind: tuned so `rejected` and
 * `stale` stay apart (a ΔE2000 of 13.9 in Dark 2026, 18.1 in Dark Modern, 12.4 in Light 2026, 12.2
 * in Light Modern) while the row's text keeps at least 6.3:1 over each tint, computed from the
 * themes' colour values.
 */
export const ROW_TINT_SHARES: Readonly<Record<ThemeKind, Readonly<Record<string, string>>>> = {
  dark: { accepted: "16%", stale: "30%", rejected: "20%" },
  light: { accepted: "14%", stale: "24%", rejected: "11%" },
};

/** `color-mix()` of *colour* at *share* into *base*, both theme variables. */
function mixed(colour: string, share: string, base: string): string {
  return `color-mix(in srgb, var(${colour}) ${share}, var(${base}))`;
}

/**
 * The `review_state` badge per theme kind and state: a pill of the state's saturated colour
 * (nudged toward the foreground or background where the text needs it) with a contrasting text
 * colour, at least 4.5:1 in the four default themes. Dark themes: the background as dark text on
 * the charts colours, `rejected` lightened 15% toward the foreground. Light themes: the background
 * as white text on green (darkened 15%) and red, the foreground as dark text on orange lightened
 * 20%.
 */
export const STATE_BADGES: Readonly<
  Record<ThemeKind, Readonly<Record<string, { fill: string; text: string }>>>
> = {
  dark: {
    accepted: { fill: "var(--vscode-charts-green)", text: "--vscode-editor-background" },
    stale: { fill: "var(--vscode-charts-orange)", text: "--vscode-editor-background" },
    rejected: {
      fill: mixed("--vscode-charts-red", "85%", "--vscode-foreground"),
      text: "--vscode-editor-background",
    },
  },
  light: {
    accepted: {
      fill: mixed("--vscode-charts-green", "85%", "--vscode-foreground"),
      text: "--vscode-editor-background",
    },
    stale: {
      fill: mixed("--vscode-charts-orange", "80%", "--vscode-editor-background"),
      text: "--vscode-foreground",
    },
    rejected: { fill: "var(--vscode-charts-red)", text: "--vscode-editor-background" },
  },
};

/**
 * The narrow solid bar on a tinted row's frozen first cell: the state's colour, darkened 20% toward
 * the foreground in light themes so it stands at least 3:1 against its row's tint.
 */
export const ROW_BARS: Readonly<Record<ThemeKind, (colour: string) => string>> = {
  dark: (colour) => `var(${colour})`,
  light: (colour) => mixed(colour, "80%", "--vscode-editor-foreground"),
};

/** The bar's width, in every theme. */
export const ROW_BAR_WIDTH = "4px";

const THEME_KINDS: readonly ThemeKind[] = ["dark", "light"];

const ROW_TINT_RULES = THEME_KINDS.flatMap((kind) =>
  Object.entries(ROW_TINTS).flatMap(([state, { colour }]) => {
    const badge = STATE_BADGES[kind][state];
    const cell = REVIEW_STATE_CLASSES[state];
    const rowClass = rowClassOf(state);
    return [
      `body.vscode-${kind} tbody tr.${rowClass} > td { background-color: ${mixed(colour, ROW_TINT_SHARES[kind][state], "--vscode-editor-background")}; }`,
      `body.vscode-${kind} tbody tr.${rowClass} > td:first-child { box-shadow: inset ${ROW_BAR_WIDTH} 0 0 ${ROW_BARS[kind](colour)}; }`,
      `body.vscode-${kind} tbody tr.tinted > td.${cell} > .cell { color: var(${badge.text}); background-color: ${badge.fill}; }`,
    ];
  }),
).join("\n");

const ROW_TINT_HIGH_CONTRAST_RULES = [
  "body.vscode-high-contrast tbody tr:nth-child(n) > td { background-color: var(--vscode-editor-background); }",
  ...Object.entries(ROW_TINTS).flatMap(([state, { colour, highContrastLightColour }]) => {
    const cell = REVIEW_STATE_CLASSES[state];
    const rowClass = rowClassOf(state);
    const rules = (body: string, value: string) => [
      `${body} tbody tr.${rowClass} > td:first-child { box-shadow: inset ${ROW_BAR_WIDTH} 0 0 var(${value}); }`,
      `${body} tbody tr.tinted > td.${cell} > .cell { color: var(--vscode-foreground); box-shadow: inset 0 0 0 2px var(${value}); }`,
    ];
    return [
      ...rules("body.vscode-high-contrast", colour),
      ...(highContrastLightColour === undefined
        ? []
        : rules("body.vscode-high-contrast-light", highContrastLightColour)),
    ];
  }),
].join("\n");

// --- The cells ----------------------------------------------------------------------
//
// How a cell is drawn, never what it holds: the CSV stays byte for byte as the CLI wrote it, and
// every cell's text as read is its `title`. A value these rules do not recognise is drawn as read.

/** The columns that hold a JSON list of names: drawn as plain comma-separated text. */
export const LIST_COLUMNS: ReadonlySet<string> = new Set([
  "tasks",
  "partitions",
  "cores",
  "satisfies",
]);

/** The columns that hold a facts hash: drawn by their first {@link HASH_PREFIX} characters. */
export const HASH_COLUMNS: ReadonlySet<string> = new Set(["facts_hash", "accepted_facts_hash"]);

/** How many characters of a hash are drawn. */
export const HASH_PREFIX = 8;

/** The column of a runnable's events, a JSON list of objects: drawn compact. */
export const EVENTS_COLUMN = "events";

/** The computed review state, the last column of every table (Python's `REVIEW_STATE`): it tints its row. */
export const REVIEW_STATE_COLUMN = "review_state";

/**
 * What an empty JSON list (`[]`) is drawn as, muted and italic: a declared *nothing*, which a blank
 * cell (*not stated*) is not.
 */
export const EMPTY_LIST_TEXT = "(none)";

/** One cell as drawn: its text, its hover (the cell as read), and whether it is muted. */
export interface CellView {
  text: string;
  title: string;
  muted: boolean;
}

function parsedJson(raw: string): unknown {
  try {
    return JSON.parse(raw);
  } catch {
    return undefined;
  }
}

/** A JSON list of strings as `a, b`, `[]` as {@link EMPTY_LIST_TEXT}; `undefined` for anything else. */
export function formatList(raw: string): string | undefined {
  const value = parsedJson(raw);
  if (!Array.isArray(value) || value.some((item) => typeof item !== "string")) {
    return undefined;
  }
  return value.length === 0 ? EMPTY_LIST_TEXT : value.join(", ");
}

/**
 * The last non-empty segment of *reference* split at *separator*: `/Platform/Modes/EcuMode/RUN`
 * is `RUN`; `undefined` for a non-string or one with no segment.
 */
function lastSegment(reference: unknown, separator: RegExp = /\//): string | undefined {
  if (typeof reference !== "string") {
    return undefined;
  }
  const segments = reference.split(separator).filter((segment) => segment !== "");
  return segments.length === 0 ? undefined : segments[segments.length - 1];
}

/**
 * The events cell as one line: each event its type and binding (`timing 0.05 s`, `init`,
 * `operation Port.Operation`, `data Port.Element`, `call_returned Port.Operation`), with
 * `(disabled in A, B)` naming its disabled modes by last segment, joined by `; `; `[]` as
 * {@link EMPTY_LIST_TEXT}. An event's `name` and a returns event's `result` are left to the hover.
 * `undefined` for a cell that is not a list of typed objects.
 */
export function formatEvents(raw: string): string | undefined {
  const value = parsedJson(raw);
  if (!Array.isArray(value)) {
    return undefined;
  }
  if (value.length === 0) {
    return EMPTY_LIST_TEXT;
  }
  const parts: string[] = [];
  for (const event of value) {
    if (!isRecord(event) || typeof event.type !== "string") {
      return undefined;
    }
    let text = event.type;
    if (event.type === "timing" && typeof event.period_s === "number") {
      text += ` ${event.period_s} s`;
    } else if (event.type === "operation" || event.type === "call_returned") {
      text += ` ${String(event.port)}.${String(event.operation)}`;
    } else if (event.type === "data") {
      text += ` ${String(event.port)}.${String(event.element)}`;
    }
    const modes = Array.isArray(event.disabled_modes) ? event.disabled_modes : [];
    const named = modes.map((mode) => (isRecord(mode) ? lastSegment(mode.mode_ref) : undefined));
    if (named.length > 0) {
      text += ` (disabled in ${named.map((name) => name ?? "?").join(", ")})`;
    }
    parts.push(text);
  }
  return parts.join("; ");
}

/** A hash by its first {@link HASH_PREFIX} characters; `none`, blank or shorter text as read. */
export function formatHash(raw: string): string {
  return raw.length > HASH_PREFIX ? raw.slice(0, HASH_PREFIX) : raw;
}

/** The cell *raw* of *column* as drawn; its `title` is always the cell as read. */
export function formatCell(column: string, raw: string): CellView {
  let text: string | undefined;
  if (LIST_COLUMNS.has(column)) {
    text = formatList(raw);
  } else if (column === EVENTS_COLUMN) {
    text = formatEvents(raw);
  } else if (HASH_COLUMNS.has(column)) {
    text = formatHash(raw);
  }
  const shown = text ?? raw;
  return { text: shown, title: raw, muted: text === EMPTY_LIST_TEXT };
}

/**
 * The tab's own rules, appended to the review tab's style by `reviewTabPage()`; theme variables
 * only. The page fills the tab; the visible sub-tab is the one scroll container (both axes), so
 * the header row stays pinned and wide rows scroll sideways.
 *
 * Layout: a cell is one line (`white-space: nowrap`), as wide as its column's content up to
 * {@link TEST_TABLES_COLUMN_CAP}, then cut with an ellipsis, its full value its `title`; the table
 * keeps its content's width (`table-layout: auto`); the first column is frozen (`position: sticky;
 * left: 0`) with the corner cell above both; a header cell's grip resizes its column while the
 * page lives. A header is never cut: it is as wide as its full name and a drag stops there. Since
 * the frozen column covers whatever scrolls under it, the sideways scroll snaps
 * (`scroll-snap-type: x mandatory`) so a column starts at the frozen column's right edge
 * (`scroll-padding-left` from the script's `--frozen-width`), and the script adds room at the end
 * so the last columns snap there too.
 *
 * Rows: a whole row is tinted by its `review_state` (`accepted` green, `stale` orange, `rejected`
 * red: {@link ROW_TINTS} at {@link ROW_TINT_SHARES}), opaque, so the frozen cell hiding what scrolls
 * under it carries its row's colour, plus a {@link ROW_BAR_WIDTH} bar ({@link ROW_BARS}) where two
 * tints are close. `not-reviewed` and tables without the column are striped; the hovered row is
 * overlaid with `--vscode-list-hoverBackground` as a background image, composing over tint or
 * stripe. Every background sits on the cells, never the row, so the frozen cell takes all three. A
 * tinted row's `review_state` is a badge ({@link STATE_BADGES}) chosen per theme kind
 * (`body.vscode-dark`, `body.vscode-light`). In a high-contrast theme (`body.vscode-high-contrast`,
 * also set in the light one) no fill is drawn: the bar on the first cell, a 2px inset frame round
 * the badge with the foreground as text, and the hovered row outlined with
 * `--vscode-contrastActiveBorder`. `not-reviewed` stays uncoloured.
 */
export const TEST_TABLES_STYLE = `html, body { height: 100%; }
body { box-sizing: border-box; grid-template-rows: auto auto minmax(0, 1fr); }
p { margin: 0; }
.top { display: grid; gap: 8px; }
.actions .link { padding: 0; font-weight: 400; background: none; border: none;
  color: var(--vscode-textLink-foreground); text-decoration: underline; }
.actions .link:hover:not(:disabled) { color: var(--vscode-textLink-activeForeground); }
.counts { display: grid; gap: 2px; font-size: 12px; color: var(--vscode-descriptionForeground); }
.tabs { display: flex; flex-wrap: wrap; gap: 4px; border-bottom: 1px solid var(--vscode-panel-border); }
.tabs button { font-weight: 400; padding: 6px 12px; border-radius: 0;
  color: var(--vscode-descriptionForeground); background: none;
  border-bottom: 2px solid transparent; }
.tabs button[aria-selected="true"] { color: var(--vscode-foreground);
  border-bottom-color: var(--vscode-focusBorder); }
.panel { min-height: 0; overflow: auto; border: 1px solid var(--vscode-panel-border);
  scroll-snap-type: x mandatory; scroll-padding-left: var(--frozen-width, 0px); }
.panel[hidden] { display: none; }
table { table-layout: auto; border-collapse: separate; border-spacing: 0;
  font-family: var(--vscode-editor-font-family); font-size: var(--vscode-editor-font-size); }
th, td { padding: 4px 10px; text-align: left; vertical-align: top; white-space: nowrap;
  border-right: 1px solid var(--vscode-panel-border);
  border-bottom: 1px solid var(--vscode-panel-border); }
.cell { max-width: ${TEST_TABLES_COLUMN_CAP}; overflow: hidden; text-overflow: ellipsis;
  white-space: nowrap; }
th { position: sticky; top: 0; z-index: 2; font-weight: 600; cursor: help;
  color: var(--vscode-foreground); background: var(--vscode-editor-background); }
th > .cell { max-width: none; overflow: visible; }
th:not(:first-child) { scroll-snap-align: start; }
th:first-child { left: 0; z-index: 3; }
td { background-color: var(--vscode-editor-background); }
td:first-child { position: sticky; left: 0; z-index: 1; }
tbody tr:nth-child(even):not(.tinted) > td { background-color: color-mix(in srgb,
  var(--vscode-foreground) 4%, var(--vscode-editor-background)); }
tbody tr.tinted > td.badge > .cell { display: inline-block; vertical-align: top;
  padding: 0 8px; border-radius: 9px; font-weight: 600; }
${ROW_TINT_RULES}
tbody tr:hover > td { background-image: linear-gradient(var(--vscode-list-hoverBackground),
  var(--vscode-list-hoverBackground)); }
${ROW_TINT_HIGH_CONTRAST_RULES}
body.vscode-high-contrast tbody tr:hover > td { background-image: none;
  outline: 1px dashed var(--vscode-contrastActiveBorder); outline-offset: -1px; }
.grip { position: absolute; top: 0; right: -3px; width: 6px; height: 100%; z-index: 4;
  cursor: col-resize; }
.grip:hover, .grip.dragging { background: var(--vscode-focusBorder); }
td.empty-table { color: var(--vscode-descriptionForeground); font-style: italic; }
td.muted { color: var(--vscode-descriptionForeground); font-style: italic; }
td.state-not-reviewed { color: var(--vscode-descriptionForeground); }`;

/** The two messages the tab's script posts. */
export type TestTablesMessage = "generate" | "changeAnnotations";

/**
 * The one message the host posts to the tab: a press the tab started ended without a new page (Esc,
 * no project, or a click dropped during a press), so the disabled action buttons are enabled again.
 */
export const TEST_TABLES_READY_MESSAGE = { type: "ready" } as const;

/**
 * The tab's one script: the sub-tabs switched by their buttons (nothing kept, no `setState`); the
 * action buttons posting `{ type }` once, all disabled after a click until the next render or
 * {@link TEST_TABLES_READY_MESSAGE}; each header grip resizing its column by a drag, the width set
 * through the CSSOM (which the CSP allows; no `style` attribute in the markup), never stored.
 */
export const TEST_TABLES_SCRIPT = `(() => {
  const vscode = acquireVsCodeApi();
  // The frozen column's width and the room at the panel's end that lets the
  // last columns snap to the frozen edge: set on the visible panel now, on a
  // panel when its sub-tab is chosen, after a drag, and on every resize.
  const fit = (panel) => {
    const table = panel.querySelector("table");
    const first = panel.querySelector("th");
    const last = panel.querySelector("th:last-child");
    if (panel.hidden || table === null || first === null || last === null) return;
    const frozen = first.offsetWidth;
    panel.style.setProperty("--frozen-width", frozen + "px");
    // The viewport's width, padding included: the room this sets never changes
    // it, so a second call computes the same room and a resize cannot oscillate.
    const viewport = panel.clientWidth;
    const room = table.offsetWidth > viewport ? Math.max(0, viewport - frozen - last.offsetWidth) : 0;
    if (panel.style.paddingRight !== room + "px") panel.style.paddingRight = room + "px";
  };
  const observer = new ResizeObserver((entries) => {
    for (const entry of entries) fit(entry.target.closest(".panel"));
  });
  const tabs = Array.from(document.querySelectorAll(".tabs > button[data-panel]"));
  for (const tab of tabs) {
    tab.addEventListener("click", () => {
      for (const each of tabs) {
        const selected = each === tab;
        each.setAttribute("aria-selected", selected ? "true" : "false");
        document.getElementById(each.dataset.panel).hidden = !selected;
      }
      fit(document.getElementById(tab.dataset.panel));
    });
  }
  const actions = Array.from(document.querySelectorAll("button[data-message]"));
  for (const button of actions) {
    button.addEventListener("click", () => {
      if (button.disabled) return;
      for (const each of actions) each.disabled = true;
      vscode.postMessage({ type: button.dataset.message });
    });
  }
  window.addEventListener("message", (event) => {
    if (event.data !== null && typeof event.data === "object" && event.data.type === "${TEST_TABLES_READY_MESSAGE.type}") {
      for (const each of actions) each.disabled = false;
    }
  });
  for (const grip of Array.from(document.querySelectorAll("th > .grip"))) {
    grip.addEventListener("pointerdown", (event) => {
      event.preventDefault();
      const th = grip.parentElement;
      const column = th.cellIndex;
      const cells = Array.from(th.closest("table").rows)
        .map((row) => row.cells[column])
        .filter((cell) => cell !== undefined && cell.colSpan === 1)
        .map((cell) => cell.querySelector(".cell"))
        .filter((cell) => cell !== null);
      const startX = event.clientX;
      const label = th.querySelector(".cell");
      const startWidth = label.getBoundingClientRect().width;
      // The header text's own width, never the label's: the label is a block as
      // wide as its column, so its scroll width would be the current width and a
      // widened column could never be narrowed again.
      const text = document.createRange();
      text.selectNodeContents(label);
      const narrowest = Math.max(24, Math.ceil(text.getBoundingClientRect().width));
      grip.classList.add("dragging");
      grip.setPointerCapture(event.pointerId);
      const move = (moved) => {
        const width = Math.max(narrowest, Math.round(startWidth + moved.clientX - startX));
        for (const cell of cells) {
          cell.style.width = width + "px";
          cell.style.maxWidth = width + "px";
        }
      };
      const stop = () => {
        fit(th.closest(".panel"));
        grip.classList.remove("dragging");
        grip.removeEventListener("pointermove", move);
        grip.removeEventListener("pointerup", stop);
        grip.removeEventListener("pointercancel", stop);
      };
      grip.addEventListener("pointermove", move);
      grip.addEventListener("pointerup", stop);
      grip.addEventListener("pointercancel", stop);
    });
  }
  for (const panel of Array.from(document.querySelectorAll(".panel"))) {
    observer.observe(panel);
    const table = panel.querySelector("table");
    if (table !== null) observer.observe(table);
    if (!panel.hidden) fit(panel);
  }
})();`;

/**
 * The message *message* asks for, or `undefined`. Fails closed: a plain object with exactly one
 * key, `type`, naming one of the two messages.
 */
export function acceptTestTablesMessage(message: unknown): TestTablesMessage | undefined {
  if (!isRecord(message) || Object.getPrototypeOf(message) !== Object.prototype) {
    return undefined;
  }
  const keys = Object.keys(message);
  if (keys.length !== 1 || keys[0] !== "type") {
    return undefined;
  }
  return message.type === "generate" || message.type === "changeAnnotations"
    ? message.type
    : undefined;
}

function generateButton(primary: boolean): string {
  return `<button type="button" class="${primary ? "approve" : "reject"}" data-message="generate">${escapeHtml(TEST_TABLES_GENERATE_BUTTON)}</button>`;
}

function header(meta: readonly string[]): string[] {
  return [
    "<header>",
    `<h1>${escapeHtml(TEST_TABLES_TAB_TITLE)}</h1>`,
    ...(meta.length === 0 ? [] : [`<div class="meta">${meta.join("")}</div>`]),
    "</header>",
  ];
}

/** A one-line page — the empty state, the unreadable bundle, the refusal — with **Generate**. */
function linePage(nonce: string, line: string): string {
  return reviewTabPage({
    nonce,
    title: TEST_TABLES_TAB_TITLE,
    body: [
      ...header([]),
      `<div class="actions"><p id="status">${escapeHtml(line)}</p>${generateButton(true)}</div>`,
    ],
    style: TEST_TABLES_STYLE,
    script: TEST_TABLES_SCRIPT,
  });
}

/** The empty state: *No test tables yet.* and **Generate**. */
export function testTablesEmptyPage(nonce: string): string {
  return linePage(nonce, TEST_TABLES_EMPTY_LINE);
}

/** A bundle that cannot be read: one line and **Generate**, never a partial table. */
export function testTablesUnreadablePage(nonce: string): string {
  return linePage(nonce, TEST_TABLES_UNREADABLE_LINE);
}

/**
 * A refused press: one fixed line and **Generate**, never the previous bundle. It quotes no CLI
 * text, because the CLI names an annotations file as given, which may be an absolute path.
 */
export function testTablesRefusalPage(nonce: string): string {
  return linePage(nonce, TEST_TABLES_REFUSED_LINE);
}

/**
 * The top line's counts for *table*: `Components: 7 rows (0 accepted, 0 stale, 0 rejected, 7 not
 * reviewed)`, the states when the manifest gives them. The tab's own wording, not the CLI's.
 */
export function countsLine(table: TestTablesTable): string {
  const states = table.reviewStates;
  const rows = `${table.label}: ${table.rows.length} ${table.rows.length === 1 ? "row" : "rows"}`;
  if (states === undefined) {
    return rows;
  }
  return `${rows} (${states.accepted} accepted, ${states.stale} stale, ${states.rejected} rejected, ${states["not-reviewed"]} not reviewed)`;
}

function tableHtml(table: TestTablesTable): string[] {
  const stateColumn = table.columns.findIndex((column) => column.name === REVIEW_STATE_COLUMN);
  const lines = ["<table>", "<thead><tr>"];
  for (const column of table.columns) {
    const tooltip = `${column.name}: ${column.owner === "test" ? "the test team's column" : "SWCraft's column"} (owner: ${column.owner}). ${column.meaning}`;
    lines.push(
      `<th scope="col" title="${escapeHtml(tooltip)}"><div class="cell">${escapeHtml(column.name)}</div><span class="grip" aria-hidden="true"></span></th>`,
    );
  }
  lines.push("</tr></thead>", "<tbody>");
  if (table.rows.length === 0) {
    lines.push(`<tr><td class="empty-table" colspan="${table.columns.length}">No rows.</td></tr>`);
  }
  for (const row of table.rows) {
    const state = stateColumn === -1 ? undefined : row[stateColumn];
    const tinted = state !== undefined && ownValue(ROW_TINTS, state) !== undefined;
    const cells = row.map((cell, index) => {
      const view = formatCell(table.columns[index].name, cell);
      const stateClass = index === stateColumn ? ownValue(REVIEW_STATE_CLASSES, cell) : undefined;
      const classes = [
        ...(stateClass === undefined ? [] : [stateClass, ...(tinted ? ["badge"] : [])]),
        ...(view.muted ? ["muted"] : []),
      ];
      const classAttr = classes.length === 0 ? "" : ` class="${classes.join(" ")}"`;
      const titleAttr = view.title === "" ? "" : ` title="${escapeHtml(view.title)}"`;
      return `<td${classAttr}${titleAttr}><div class="cell">${escapeHtml(view.text)}</div></td>`;
    });
    const rowClass = tinted ? ` class="tinted ${rowClassOf(state)}"` : "";
    lines.push(`<tr${rowClass}>${cells.join("")}</tr>`);
  }
  lines.push("</tbody>", "</table>");
  return lines;
}

/**
 * The tables page: the top line (the manifest's project, SWCraft version and counts; the
 * annotations folder by {@link describeAnnotations} with **Change…**; **Generate**), one sub-tab per
 * file (the tables in the manifest's order, then the orphan file with its row count), and one
 * `<table>` per sub-tab: header pinned, first column frozen, cells by {@link formatCell}, rows
 * tinted by `review_state` with the state's cell a badge, each header its CSV name with a tooltip
 * of name, owner and meaning, and a resize grip. Nothing in it is editable.
 */
export function testTablesPage(
  read: Extract<TestTablesRead, { kind: "tables" }>,
  options: { nonce: string; annotations: unknown },
): string {
  const all = [...read.tables, read.orphans];
  const meta = [
    `<span>Project <b>${escapeHtml(read.project)}</b></span>`,
    `<span>SWCraft <b>${escapeHtml(read.swcraftVersion)}</b></span>`,
    `<span>Annotations <b>${escapeHtml(describeAnnotations(options.annotations))}</b></span>`,
  ];
  // The grid's rows: header, actions and counts first; the sub-tabs second; the visible panel takes
  // the rest.
  const top = [
    ...header(meta),
    '<div class="actions">',
    `<button type="button" class="link" data-message="changeAnnotations">${escapeHtml(TEST_TABLES_CHANGE_BUTTON)}</button>`,
    generateButton(false),
    "</div>",
    `<div class="counts">${[
      ...read.tables.map(countsLine),
      `Orphaned annotations: ${read.orphans.rows.length}`,
    ]
      .map((line) => `<span>${escapeHtml(line)}</span>`)
      .join("")}</div>`,
  ];
  const tabs = all.map((table, index) => {
    const label = table === read.orphans ? `${table.label} (${table.rows.length})` : table.label;
    return `<button type="button" role="tab" data-panel="panel-${index}" aria-selected="${index === 0 ? "true" : "false"}">${escapeHtml(label)}</button>`;
  });
  const panels = all.flatMap((table, index) => [
    `<section class="panel" id="panel-${index}" role="tabpanel"${index === 0 ? "" : " hidden"}>`,
    ...tableHtml(table),
    "</section>",
  ]);
  return reviewTabPage({
    nonce: options.nonce,
    title: TEST_TABLES_TAB_TITLE,
    body: [
      '<div class="top">',
      ...top,
      "</div>",
      `<nav class="tabs" role="tablist">${tabs.join("")}</nav>`,
      ...panels,
    ],
    style: TEST_TABLES_STYLE,
    script: TEST_TABLES_SCRIPT,
  });
}

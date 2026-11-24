// The SWCraft entries of the Problems panel: the reading of `swcraft check-model --format json`,
// the place of each finding, and the two quick fixes' logic, tested on a recorded document and a
// fake file system. No `vscode` import: this runs under plain `node --test`.
//
// The recorded document, `src/fixtures/navigation/check-model-basic.json`, is the CLI's stdout
// verbatim from
//
//   .venv/bin/python -m swcraft_tools.cli check-model \
//     --project <copy>/swcraft.yaml --format json
//
// over a copy of `examples/basic` whose requirement `REQ_EXAMPLE_001` is at version 4 and which
// carries a link at each of the three levels: `ExampleSWC`'s own `satisfies` states the stale
// `@3`, its port `pOut`'s is bare, and `ExampleConsumerSWC`'s runnable `ExampleConsumerRun` states
// the stale `@3` (its component's own `@4` is current and raises nothing). The copy's texts are
// `RECORDED_FILES` below, so a placement test reads what the run read. Re-record it the same way
// when the document's shape moves.
import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as path from "node:path";
import { test } from "node:test";

import {
  CHECK_MODEL_SUCCESS_LINE,
  CHECK_SCHEMA_VERSION,
  CheckDocumentError,
  type CheckFinding,
  CONFIRM_LINK_RULE,
  describeFindingCounts,
  FINDING_COUNTS_TOOLTIP,
  LINK_FIELDS,
  linkAt,
  MANIFEST_UNKNOWN_KEY_RULE,
  type Placement,
  PROBLEMS_FOCUS_COMMAND,
  parseCheckDocument,
  placeFinding,
  placeRefusals,
  QUICK_FIX_RULES,
  quickFixFor,
  readCheckRun,
  rehomeRefusedFile,
  requirementVersion,
  STATE_VERSION_RULE,
} from "./diagnosticsCore";
import { PIN_REFUSAL_PREFIX } from "./helpLinks";
import { loadReferences, type Reader, type References, requestReader } from "./navigationCore";
import { PROJECT_ROOT, readCliPackageSource, SCHEMAS_DIR } from "./testSupport";

const PROJECT = path.resolve("/projects/basic");
const MODEL_ROOT = path.join(PROJECT, "model");
const MANIFEST = path.join(PROJECT, "swcraft.yaml");

function shippedText(): string {
  return fs.readFileSync(path.join(SCHEMAS_DIR, "references.json"), "utf8");
}

function shipped(): References {
  return loadReferences(shippedText());
}

function doc(lines: string[]): string {
  return `${lines.join("\n")}\n`;
}

function at(...parts: string[]): string {
  return path.join(MODEL_ROOT, ...parts);
}

/** A fake file system over *files* (absolute path → text), case-insensitive. */
function fakeReader(files: Record<string, string>): Reader {
  const find = (filePath: string) =>
    Object.keys(files).find((candidate) => candidate.toLowerCase() === filePath.toLowerCase());
  return {
    readText: (filePath) => {
      const found = find(filePath);
      return found === undefined ? undefined : files[found];
    },
    listDir: (directory) => {
      const prefix = `${directory}${path.sep}`.toLowerCase();
      const entries = new Set<string>();
      for (const candidate of Object.keys(files)) {
        if (candidate.toLowerCase().startsWith(prefix)) {
          entries.add(candidate.slice(prefix.length).split(path.sep)[0]);
        }
      }
      return entries.size === 0 ? undefined : [...entries];
    },
  };
}

const MANIFEST_TEXT = doc([
  "name: swcraft-basic-example",
  'schema_version: "1.0"',
  "model_root: model",
  "dist_root: dist",
  "views_root: views",
  "outputs:",
  "  arxml: dist/model.arxml",
  "  needs: dist/needs.json",
]);

const EXAMPLE_SWC = doc([
  "name: ExampleSWC",
  "type: application",
  "description: Example software component for testing and demonstration",
  "",
  "ports:",
  "  - name: pOut",
  "    direction: provide",
  "    interface: IExample",
  "    description: Example output port providing data",
  "    satisfies:",
  "    - REQ_EXAMPLE_001",
  "",
  "runnables:",
  "  - name: ExampleRun",
  "    min_start_interval: 0.01",
  "    events:",
  "      - type: timing",
  "        period: 0.01",
  "",
  "memory:",
  "  RAM0: 2048",
  "",
  "handle_termination_and_restart: no_support",
  "satisfies:",
  "- REQ_EXAMPLE_001@3",
]);

const EXAMPLE_CONSUMER_SWC = doc([
  "name: ExampleConsumerSWC",
  "type: application",
  "description: Example software component consuming data from ExampleSWC",
  "",
  "ports:",
  "  - name: rIn",
  "    direction: require",
  "    interface: IExample",
  "    description: Example input port requiring data",
  "",
  "runnables:",
  "  - name: ExampleConsumerRun",
  "    min_start_interval: 0.01",
  "    events:",
  "      - type: timing",
  "        period: 0.01",
  "    satisfies:",
  "    - REQ_EXAMPLE_001@3",
  "",
  "memory:",
  "  RAM0: 1536",
  "",
  "handle_termination_and_restart: no_support",
  "satisfies:",
  "- REQ_EXAMPLE_001@4",
]);

const REQUIREMENT = doc([
  "id: REQ_EXAMPLE_001",
  "title: Example requirement for ExampleSWC",
  "description: The system shall provide example functionality via ExampleSWC.",
  "req_type: req",
  "version: 4",
  "source: architecture",
]);

/** The recorded copy's files the placements read. */
const RECORDED_FILES: Record<string, string> = {
  [MANIFEST]: MANIFEST_TEXT,
  [at("swc", "example", "ExampleSWC.yaml")]: EXAMPLE_SWC,
  [at("swc", "example", "ExampleConsumerSWC.yaml")]: EXAMPLE_CONSUMER_SWC,
  [at("requirements", "REQ_EXAMPLE_001.yaml")]: REQUIREMENT,
};

function recorded(): string {
  return fs.readFileSync(
    path.join(PROJECT_ROOT, "src", "fixtures", "navigation", "check-model-basic.json"),
    "utf8",
  );
}

/** The 0-based line of the *occurrence*-th line of *text* that equals *exact*. */
function lineOf(text: string, exact: string, occurrence = 0): number {
  const lines = text.split("\n");
  let seen = 0;
  for (let i = 0; i < lines.length; i += 1) {
    if (lines[i] === exact) {
      if (seen === occurrence) {
        return i;
      }
      seen += 1;
    }
  }
  throw new Error(`no line ${JSON.stringify(exact)}`);
}

/** The columns of *value* on the line *exact* of *text*. */
function columnsOf(text: string, exact: string, value: string) {
  const line = lineOf(text, exact);
  const start = exact.indexOf(value);
  assert.notEqual(start, -1);
  return { line, start, end: start + value.length };
}

function finding(overrides: Partial<CheckFinding>): CheckFinding {
  return {
    rule: "trace.link-needs-confirmation",
    severity: "warning",
    message: "a message",
    subjects: [],
    target: null,
    ...overrides,
  };
}

// --- the document ---------------------------------------------------------------

test("the recorded document parses: schemaVersion 1, the three trace warnings in the CLI's order", () => {
  assert.equal(CHECK_SCHEMA_VERSION, 1);
  const parsed = parseCheckDocument(recorded());
  assert.deepEqual(
    parsed.findings.map((f) => [f.rule, f.severity, f.target, f.subjects[0].element]),
    [
      [CONFIRM_LINK_RULE, "warning", "REQ_EXAMPLE_001", "ExampleConsumerSWC"],
      [CONFIRM_LINK_RULE, "warning", "REQ_EXAMPLE_001", "ExampleSWC"],
      [STATE_VERSION_RULE, "warning", "REQ_EXAMPLE_001", "ExampleSWC"],
    ],
  );
  assert.deepEqual(parsed.findings[2].subjects[0].path, [
    { field: "ports" },
    { member: "pOut" },
    { field: "satisfies" },
    { member: "REQ_EXAMPLE_001" },
  ]);
  assert.match(parsed.findings[0].message, /is at version 4/);
});

test("a schemaVersion other than 1 is refused as the toolchain and the extension being different releases", () => {
  assert.throws(
    () => parseCheckDocument(JSON.stringify({ schemaVersion: 2, findings: [] })),
    (error: unknown) =>
      error instanceof CheckDocumentError &&
      /schemaVersion 2 is not the 1/.test(error.message) &&
      /different releases/.test(error.message),
  );
});

test("a malformed document throws CheckDocumentError naming what is wrong", () => {
  const cases: Array<[string, RegExp]> = [
    ["not json", /not JSON/],
    ["[]", /not an object/],
    [JSON.stringify({ schemaVersion: 1 }), /findings must be an array/],
    [
      JSON.stringify({ schemaVersion: 1, findings: [{ rule: "r", message: "m" }] }),
      /severity must be/,
    ],
    [
      JSON.stringify({
        schemaVersion: 1,
        findings: [{ rule: "r", severity: "info", message: "m", subjects: [], target: null }],
      }),
      /severity must be/,
    ],
    [
      JSON.stringify({
        schemaVersion: 1,
        findings: [{ rule: "r", severity: "error", message: "m", subjects: {}, target: null }],
      }),
      /subjects must be an array/,
    ],
    [
      JSON.stringify({
        schemaVersion: 1,
        findings: [{ rule: "r", severity: "error", message: "m", subjects: [], target: 3 }],
      }),
      /target must be a string or null/,
    ],
    [
      JSON.stringify({
        schemaVersion: 1,
        findings: [
          {
            rule: "r",
            severity: "error",
            message: "m",
            subjects: [{ layer: "swc", element: "A", path: [{ field: "x", key: "y" }] }],
            target: null,
          },
        ],
      }),
      /path\[0\] must be an object with exactly one key/,
    ],
    [
      JSON.stringify({
        schemaVersion: 1,
        findings: [
          {
            rule: "r",
            severity: "error",
            message: "m",
            subjects: [{ element: "A" }],
            target: null,
          },
        ],
      }),
      /subjects\[0\] must carry a string layer/,
    ],
    [
      JSON.stringify({
        schemaVersion: 1,
        findings: [
          {
            rule: "r",
            severity: "error",
            message: "m",
            subjects: [{ layer: "swc", element: 3 }],
            target: null,
          },
        ],
      }),
      /subjects\[0\]\.element must be a string/,
    ],
    [
      JSON.stringify({
        schemaVersion: 1,
        findings: [
          {
            rule: "r",
            severity: "error",
            message: "m",
            subjects: [{ layer: "swc", element: "A", path: { field: "x" } }],
            target: null,
          },
        ],
      }),
      /subjects\[0\]\.path must be an array/,
    ],
    [
      JSON.stringify({
        schemaVersion: 1,
        findings: [
          {
            rule: "r",
            severity: "error",
            message: "m",
            subjects: [{ layer: "swc", element: "A", path: [{ field: 3 }] }],
            target: null,
          },
        ],
      }),
      /path\[0\] must be a string field, a member or a string key/,
    ],
  ];
  for (const [text, pattern] of cases) {
    assert.throws(
      () => parseCheckDocument(text),
      (error: unknown) => error instanceof CheckDocumentError && pattern.test(error.message),
      text,
    );
  }
});

// --- placement ------------------------------------------------------------------

test("the recorded component-, port- and runnable-level subjects place at the member's value columns", () => {
  const refs = shipped();
  const reader = fakeReader(RECORDED_FILES);
  const placed = parseCheckDocument(recorded()).findings.map((f) =>
    placeFinding(refs, f, MODEL_ROOT, MANIFEST, reader),
  );
  const runnableLevel = columnsOf(
    EXAMPLE_CONSUMER_SWC,
    "    - REQ_EXAMPLE_001@3",
    "REQ_EXAMPLE_001@3",
  );
  const componentLevel = columnsOf(EXAMPLE_SWC, "- REQ_EXAMPLE_001@3", "REQ_EXAMPLE_001@3");
  const portLevel = columnsOf(EXAMPLE_SWC, "    - REQ_EXAMPLE_001", "REQ_EXAMPLE_001");
  assert.deepEqual(
    placed.map(({ file, line, start, end }) => ({ file, line, start, end })),
    [
      { file: at("swc", "example", "ExampleConsumerSWC.yaml"), ...runnableLevel },
      { file: at("swc", "example", "ExampleSWC.yaml"), ...componentLevel },
      { file: at("swc", "example", "ExampleSWC.yaml"), ...portLevel },
    ],
  );
  // The CLI's rule, severity and message, verbatim; one finding, one entry.
  const findings = parseCheckDocument(recorded()).findings;
  placed.forEach((placement, index) => {
    assert.equal(placement.code, findings[index].rule);
    assert.equal(placement.severity, "warning");
    assert.equal(placement.message, findings[index].message);
    assert.deepEqual(placement.related, []);
  });
});

test("an error keeps its severity, and a finding's second subject is its related place", () => {
  const refs = shipped();
  const reader = fakeReader(RECORDED_FILES);
  const placed = placeFinding(
    refs,
    finding({
      rule: "some.rule",
      severity: "error",
      subjects: [
        { layer: "swc", element: "ExampleSWC", path: [{ field: "ports" }, { member: "pOut" }] },
        { layer: "requirements", element: "REQ_EXAMPLE_001" },
      ],
    }),
    MODEL_ROOT,
    MANIFEST,
    reader,
  );
  assert.equal(placed.severity, "error");
  // A finding whose target is null carries none.
  assert.equal("target" in placed, false);
  assert.deepEqual(
    { line: placed.line, start: placed.start, end: placed.end },
    columnsOf(EXAMPLE_SWC, "  - name: pOut", "pOut"),
  );
  assert.equal(placed.related.length, 1);
  const [related] = placed.related;
  // An element-level subject: the identity line, whole.
  assert.deepEqual(
    { file: related.file, line: related.line, start: related.start, end: related.end },
    {
      file: at("requirements", "REQ_EXAMPLE_001.yaml"),
      line: 0,
      start: 0,
      end: "id: REQ_EXAMPLE_001".length,
    },
  );
  // A related place is a place only: the entry's message stands for it.
  assert.deepEqual(Object.keys(related).sort(), ["end", "file", "line", "start"]);
});

test("a layer-wide subject, a finding with no subject and an unread artifact place at swcraft.yaml line 0", () => {
  const refs = shipped();
  const reader = fakeReader(RECORDED_FILES);
  const atManifest = {
    file: MANIFEST,
    line: 0,
    start: 0,
    end: "name: swcraft-basic-example".length,
  };
  const where = (refsOrNone: References | undefined, f: CheckFinding) => {
    const { file, line, start, end } = placeFinding(refsOrNone, f, MODEL_ROOT, MANIFEST, reader);
    return { file, line, start, end };
  };
  assert.deepEqual(where(refs, finding({ subjects: [{ layer: "swc" }] })), atManifest);
  assert.deepEqual(where(refs, finding({ subjects: [] })), atManifest);
  const elementSubject = finding({ subjects: [{ layer: "swc", element: "ExampleSWC" }] });
  assert.deepEqual(where(undefined, elementSubject), atManifest);
  // The message is never changed by a fallback.
  assert.equal(
    placeFinding(undefined, elementSubject, MODEL_ROOT, MANIFEST, reader).message,
    "a message",
  );
});

/** placeRefusals' entries for a reason that names no element file: exactly one. */
function onlyRefusal(manifestFile: string, reason: string, reader: Reader): Placement {
  const entries = placeRefusals(manifestFile, reason, reader);
  assert.equal(entries.length, 1, "a refusal naming no element file is one entry");
  return entries[0];
}

test("a refused run is one error at swcraft.yaml line 0 carrying stderr's last line", () => {
  const reader = fakeReader(RECORDED_FILES);
  const refused = onlyRefusal(
    MANIFEST,
    "some progress\nError: the manifest pins toolchain 0.15.0\n\n",
    reader,
  );
  assert.deepEqual(refused, {
    file: MANIFEST,
    line: 0,
    start: 0,
    end: "name: swcraft-basic-example".length,
    severity: "error",
    message: "Error: the manifest pins toolchain 0.15.0",
    related: [],
  });
  assert.match(onlyRefusal(MANIFEST, "", reader).message, /without a document/);
});

/** The CLI's pin refusal, after a line of progress: what a real refused run's stderr holds. */
const PIN_REFUSAL = `${PIN_REFUSAL_PREFIX} 0.19.2 and this is SWCraft 0.20.0 — install the 0.19.2 workstation archive (its swcraft_install.cmd on Windows, swcraft_install.sh elsewhere), or edit the pin if this project is meant to move to this release.`;
const PIN_STDERR = `some progress\n${PIN_REFUSAL}\n\n`;

function pinPlace(manifest: string | undefined, reason = PIN_STDERR) {
  const reader = fakeReader(manifest === undefined ? {} : { [MANIFEST]: manifest });
  return onlyRefusal(MANIFEST, reason, reader);
}

const AT_LINE_0 = { line: 0, start: 0, end: "name: p".length };

function assertPlace(entry: Placement, place: { line: number; start: number; end: number }) {
  const { line, start, end } = entry;
  assert.deepEqual({ line, start, end }, place);
}

test("a pin refusal lands on the toolchain_version line, whole, its message unchanged", () => {
  const pinLine = 'toolchain_version: "0.19.2"';
  const entry = pinPlace(doc(["name: p", "model_root: model", pinLine, "dist_root: dist"]));
  assert.deepEqual(entry, {
    file: MANIFEST,
    line: 2,
    start: 0,
    end: pinLine.length,
    severity: "error",
    message: PIN_REFUSAL,
    related: [],
  });
});

test("a pin refusal with no toolchain_version line stays on line 0", () => {
  assertPlace(pinPlace(doc(["name: p", "model_root: model"])), AT_LINE_0);
});

test("a commented toolchain_version line is no pin line", () => {
  const manifest = doc(["name: p", '# toolchain_version: "0.19.2"', "model_root: model"]);
  assertPlace(pinPlace(manifest), AT_LINE_0);
});

test("a nested or longer key is no pin line", () => {
  const manifest = doc([
    "name: p",
    "outputs:",
    '  toolchain_version: "1"',
    "toolchain_versions: 1",
  ]);
  assertPlace(pinPlace(manifest), AT_LINE_0);
});

test("a quoted toolchain_version key is the pin line", () => {
  for (const key of ['"toolchain_version"', "'toolchain_version'"]) {
    const pinLine = `${key}: "0.19.2"`;
    assertPlace(pinPlace(doc(["name: p", pinLine])), {
      line: 1,
      start: 0,
      end: pinLine.length,
    });
  }
});

test("a toolchain_version key written twice lands on the last line, the one the loader keeps", () => {
  const pinLine = 'toolchain_version: "0.19.2"';
  const manifest = doc(["name: p", 'toolchain_version: "0.18.0"', "model_root: model", pinLine]);
  assertPlace(pinPlace(manifest), { line: 3, start: 0, end: pinLine.length });
});

test("a pin line of a CRLF manifest ends before its line end", () => {
  const pinLine = 'toolchain_version: "0.19.2"';
  assertPlace(pinPlace(["name: p", pinLine, "model_root: model", ""].join("\r\n")), {
    line: 1,
    start: 0,
    end: pinLine.length,
  });
});

test("a pin refusal over an unreadable manifest stays on line 0, an empty range", () => {
  assertPlace(pinPlace(undefined), { line: 0, start: 0, end: 0 });
});

test("only the pin's Error line moves the entry: its Warning form and any other refusal do not", () => {
  const manifest = doc(["name: p", 'toolchain_version: "0.19.2"']);
  const warning = PIN_REFUSAL.replace(/^Error:/, "Warning:");
  assertPlace(pinPlace(manifest, `${warning}\n`), AT_LINE_0);
  assertPlace(pinPlace(manifest, "Error: model root does not exist: /p/model\n"), AT_LINE_0);
  // The last line decides: a pin line earlier in stderr is not this entry's message.
  assertPlace(pinPlace(manifest, `${PIN_REFUSAL}\nError: another\n`), AT_LINE_0);
  assertPlace(pinPlace(manifest, ""), AT_LINE_0);
});

// --- the status bar's counts ------------------------------------------------------

/** *errors* errors and *warnings* warnings, each at its own line of one file. */
function placements(errors: number, warnings: number): Placement[] {
  const one = (severity: "error" | "warning", line: number): Placement => ({
    file: MANIFEST,
    line,
    start: 0,
    end: 1,
    severity,
    message: `${severity} ${line}`,
    related: [],
  });
  return [
    ...Array.from({ length: errors }, (_, i) => one("error", i)),
    ...Array.from({ length: warnings }, (_, i) => one("warning", errors + i)),
  ];
}

// --- the undeclared manifest key ---------------------------------------------------

const MANIFEST_KEY_TEXT = doc([
  "name: p",
  "model_root: model",
  "arxml_verison: 4.3.0",
  "outputs:",
  "  arxml: dist/model.arxml",
  "  c4: views/model.c4",
]);

function manifestKeyPlace(target: string | null, manifest = MANIFEST_KEY_TEXT) {
  const reader = fakeReader({ [MANIFEST]: manifest });
  const unknownKey = finding({
    rule: MANIFEST_UNKNOWN_KEY_RULE,
    severity: "error",
    message: "The manifest carries the key 'arxml_verison', which it does not declare.",
    target,
  });
  return placeFinding(shipped(), unknownKey, MODEL_ROOT, MANIFEST, reader);
}

test("an undeclared manifest key is placed on its line, whole, under its rule, its message unchanged", () => {
  assert.equal(MANIFEST_UNKNOWN_KEY_RULE, "manifest.unknown-key");
  const placed = manifestKeyPlace("arxml_verison");
  assert.equal(placed.file, MANIFEST);
  assertPlace(placed, { line: 2, start: 0, end: "arxml_verison: 4.3.0".length });
  assert.equal(placed.code, "manifest.unknown-key");
  assert.equal(placed.severity, "error");
  assert.match(placed.message, /^The manifest carries the key 'arxml_verison'/);
});

test("a quoted undeclared manifest key is placed on its line", () => {
  for (const key of ['"arxml_verison"', "'arxml_verison'"]) {
    const keyLine = `${key}: 4.3.0`;
    assertPlace(manifestKeyPlace("arxml_verison", doc(["name: p", keyLine])), {
      line: 1,
      start: 0,
      end: keyLine.length,
    });
  }
});

test("a key inside outputs is placed on the outputs line", () => {
  assertPlace(manifestKeyPlace("outputs"), { line: 3, start: 0, end: "outputs:".length });
});

test("a manifest key written twice is placed on its last column-0 line, never on an indented look-alike", () => {
  const twice = doc([
    "name: p",
    "arxml_verison: 4.2.2",
    "outputs:",
    "  arxml_verison: x",
    "arxml_verison: 4.3.0",
    "rules:",
    "  arxml_verison: y",
  ]);
  assertPlace(manifestKeyPlace("arxml_verison", twice), {
    line: 4,
    start: 0,
    end: "arxml_verison: 4.3.0".length,
  });
  // Only an indented look-alike: no column-0 line matches, so line 0.
  const indentedOnly = doc(["name: p", "outputs:", "  arxml_verison: x"]);
  assertPlace(manifestKeyPlace("arxml_verison", indentedOnly), AT_LINE_0);
});

test("an undeclared manifest key with no matching line, or with no target, stays on line 0", () => {
  assertPlace(manifestKeyPlace("toolchain_verison"), AT_LINE_0);
  assertPlace(manifestKeyPlace(null), AT_LINE_0);
  // A key's text is matched literally, never as a pattern.
  assertPlace(manifestKeyPlace("arxml.verison"), AT_LINE_0);
});

test("another rule's finding with no subject and a target stays on line 0", () => {
  const reader = fakeReader({ [MANIFEST]: MANIFEST_KEY_TEXT });
  const other = finding({ target: "arxml_verison" });
  assertPlace(placeFinding(shipped(), other, MODEL_ROOT, MANIFEST, reader), AT_LINE_0);
});

test("the findings' status-bar text counts errors and warnings, singular for exactly one", () => {
  for (const [errors, warnings, text] of [
    [0, 0, "SWCraft: 0 errors · 0 warnings"],
    [1, 0, "SWCraft: 1 error · 0 warnings"],
    [0, 1, "SWCraft: 0 errors · 1 warning"],
    [2, 35, "SWCraft: 2 errors · 35 warnings"],
    [1, 1, "SWCraft: 1 error · 1 warning"],
  ] as const) {
    assert.equal(describeFindingCounts(placements(errors, warnings)), text);
  }
});

test("a refused run's single entry reads as one error", () => {
  const refused = onlyRefusal(MANIFEST, "Error: no manifest", fakeReader(RECORDED_FILES));
  assert.equal(describeFindingCounts([refused]), "SWCraft: 1 error · 0 warnings");
});

test("the findings' tooltip says what the counts are and that a click opens Problems", () => {
  assert.equal(
    FINDING_COUNTS_TOOLTIP,
    "The SWCraft model check's findings at its last run. Click to open Problems.",
  );
});

test("only severity decides the count: rule, target, related places and files do not", () => {
  const plain = placements(1, 2);
  const decorated: Placement[] = plain.map((place, index) => ({
    ...place,
    file: path.join(PROJECT, `other-${index}.yaml`),
    code: index === 0 ? undefined : CONFIRM_LINK_RULE,
    target: index === 1 ? "REQ_EXAMPLE_001" : undefined,
    message: "the same message",
    related: [{ file: MANIFEST, line: 3, start: 0, end: 2 }],
  }));
  assert.equal(describeFindingCounts(decorated), describeFindingCounts(plain));
  assert.equal(describeFindingCounts(decorated), "SWCraft: 1 error · 2 warnings");
});

test("the Problems focus command is the one VS Code derives for the markers view", () => {
  assert.equal(PROBLEMS_FOCUS_COMMAND, "workbench.panel.markers.view.focus");
});

test("a member in a flow list, and a member the file does not hold, fall back to the identity line", () => {
  const refs = shipped();
  const flow = doc(["name: Flow", "satisfies: [REQ_EXAMPLE_001@3]"]);
  const reader = fakeReader({ ...RECORDED_FILES, [at("swc", "Flow.yaml")]: flow });
  const identity = (element: string, file: string, identityLine: string) => ({
    file,
    line: 0,
    start: 0,
    end: identityLine.length,
    element,
  });
  const cases: Array<[CheckFinding, ReturnType<typeof identity>]> = [
    [
      finding({
        subjects: [
          {
            layer: "swc",
            element: "Flow",
            path: [{ field: "satisfies" }, { member: "REQ_EXAMPLE_001@3" }],
          },
        ],
      }),
      identity("Flow", at("swc", "Flow.yaml"), "name: Flow"),
    ],
    [
      finding({
        subjects: [
          {
            layer: "swc",
            element: "ExampleSWC",
            path: [{ field: "satisfies" }, { member: "REQ_EXAMPLE_001@2" }],
          },
        ],
      }),
      identity("ExampleSWC", at("swc", "example", "ExampleSWC.yaml"), "name: ExampleSWC"),
    ],
    [
      finding({
        subjects: [
          {
            layer: "swc",
            element: "ExampleSWC",
            path: [{ field: "ports" }, { member: "pGone" }, { field: "satisfies" }],
          },
        ],
      }),
      identity("ExampleSWC", at("swc", "example", "ExampleSWC.yaml"), "name: ExampleSWC"),
    ],
  ];
  for (const [f, expected] of cases) {
    const { file, line, start, end } = placeFinding(refs, f, MODEL_ROOT, MANIFEST, reader);
    const { element: _element, ...where } = expected;
    assert.deepEqual({ file, line, start, end }, where, expected.element);
  }
});

test("a subject whose element cannot be located places at swcraft.yaml line 0", () => {
  const reader = fakeReader(RECORDED_FILES);
  const atManifest = { file: MANIFEST, line: 0 };
  const where = (refs: References, f: CheckFinding) => {
    const { file, line } = placeFinding(refs, f, MODEL_ROOT, MANIFEST, reader);
    return { file, line };
  };
  // No such element, a name that is no path segment, and a layer the artifact does not list.
  for (const subject of [
    { layer: "swc", element: "Nowhere" },
    { layer: "swc", element: "../ExampleSWC" },
    { layer: "no_such_layer", element: "ExampleSWC" },
  ]) {
    assert.deepEqual(
      where(shipped(), finding({ subjects: [subject] })),
      atManifest,
      subject.element,
    );
  }
  // A layer whose file name is not its identity field: no shipped layer is one,
  // so the artifact is synthetic — the storage rule composes no file name.
  const synthetic = JSON.parse(shippedText()) as {
    layers: Record<string, Record<string, unknown>>;
  };
  synthetic.layers.swc.filenameFrom = "title";
  assert.deepEqual(
    where(
      loadReferences(JSON.stringify(synthetic)),
      finding({ subjects: [{ layer: "swc", element: "ExampleSWC" }] }),
    ),
    atManifest,
  );
});

// --- a key no field declares (`model.unknown-key`) ------------------------------

const UNKNOWN_KEY_REQUIREMENT = doc([
  "id: REQ_CHILD",
  "title: A child requirement",
  "derived___from:",
  "- REQ_EXAMPLE_001",
]);

const UNKNOWN_KEY_SWC = doc([
  "name: Typos",
  "ports:",
  "- name: pOut",
  "  direction: provide",
  "  intrface: ISig",
  "- name: pSrv",
  "  direction: provide",
  "  interface: IServer",
  "runnables:",
  "- name: R",
  "  events:",
  "  - type: timing",
  "    period: 0.01",
  "    perod: 0.01",
  "  - type: operation",
  "    port: pSrv",
  "    operation: Op",
  "    oepration: Op",
  "- name: Twice",
  "  events:",
  "  - type: timing",
  "    period: 0.01",
  "    perod: 0.01",
  "  - type: timing",
  "    period: 0.02",
  "- name: Flow",
  "  events:",
  "  - {type: timing, period: 0.01, perod: 0.01}",
  "- name: Modes",
  "  events:",
  "  - type: timing",
  "    period: 0.01",
  "    disabled_modes:",
  "    - port: rMode",
  "      mode_group_ref: /M/G",
  "      mode_ref: /M/G/OFF",
  "      mdoe: x",
  "- name: OnData",
  "  events:",
  "  - type: data",
  "    port: rIn",
  "    element: value",
  "    elemnt: value",
]);

const UNKNOWN_KEY_FILES: Record<string, string> = {
  ...RECORDED_FILES,
  [at("requirements", "REQ_CHILD.yaml")]: UNKNOWN_KEY_REQUIREMENT,
  [at("swc", "typos", "Typos.yaml")]: UNKNOWN_KEY_SWC,
};

/** A `check-model --format json` document of one `model.unknown-key` error per subject. */
function unknownKeyDocument(subjects: unknown[]): string {
  return JSON.stringify({
    schemaVersion: 1,
    findings: subjects.map((subject) => ({
      rule: "model.unknown-key",
      severity: "error",
      message: "a message",
      subjects: [subject],
      target: null,
    })),
  });
}

test("a model.unknown-key finding is placed on the key's line with the key's columns, under its rule", () => {
  const refs = shipped();
  const reader = fakeReader(UNKNOWN_KEY_FILES);
  const typos = at("swc", "typos", "Typos.yaml");
  const event = (member: Record<string, unknown>, key: string) => ({
    layer: "swc",
    element: "Typos",
    path: [{ field: "runnables" }, { member: "R" }, { field: "events" }, { member }, { key }],
  });
  const document = unknownKeyDocument([
    { layer: "requirements", element: "REQ_CHILD", path: [{ key: "derived___from" }] },
    {
      layer: "swc",
      element: "Typos",
      path: [{ field: "ports" }, { member: "pOut" }, { key: "intrface" }],
    },
    // The runnable's only timing event: `type` is its one string entry.
    event({ type: "timing", period: 0.01 }, "perod"),
    // An operation event, matched by `type`, `port` and `operation`.
    event({ type: "operation", port: "pSrv", operation: "Op" }, "oepration"),
    // A data-received event, matched by `type`, `port` and `element`. The two event
    // identities are the shapes test_validator_unknown_key.py pins for the Python side
    // (test_an_operation_and_a_data_event_are_spelled_by_their_string_identities).
    {
      layer: "swc",
      element: "Typos",
      path: [
        { field: "runnables" },
        { member: "OnData" },
        { field: "events" },
        { member: { type: "data", port: "rIn", element: "value" } },
        { key: "elemnt" },
      ],
    },
  ]);
  const placed = parseCheckDocument(document).findings.map((f) =>
    placeFinding(refs, f, MODEL_ROOT, MANIFEST, reader),
  );
  assert.deepEqual(
    placed.map(({ file, line, start, end }) => ({ file, line, start, end })),
    [
      {
        file: at("requirements", "REQ_CHILD.yaml"),
        ...columnsOf(UNKNOWN_KEY_REQUIREMENT, "derived___from:", "derived___from"),
      },
      { file: typos, ...columnsOf(UNKNOWN_KEY_SWC, "  intrface: ISig", "intrface") },
      // The first `perod` line is runnable R's.
      { file: typos, ...columnsOf(UNKNOWN_KEY_SWC, "    perod: 0.01", "perod") },
      { file: typos, ...columnsOf(UNKNOWN_KEY_SWC, "    oepration: Op", "oepration") },
      { file: typos, ...columnsOf(UNKNOWN_KEY_SWC, "    elemnt: value", "elemnt") },
    ],
  );
  // The rule is the entry's code, from which the rules page's link is resolved.
  for (const placement of placed) {
    assert.equal(placement.code, "model.unknown-key");
    assert.equal(placement.severity, "error");
  }
});

test("a finding on a scalar field is placed on the value; a stray key on a scalar keeps the key", () => {
  const refs = shipped();
  const task = doc(["name: Task_10ms", "period: 0.01", "core: CORE0"]);
  const connector = doc(["name: A_to_B", "from:", "  component: A", "  port: pOut", "  prt: pOut"]);
  const reader = fakeReader({
    ...UNKNOWN_KEY_FILES,
    [at("tasks", "Task_10ms.yaml")]: task,
    [at("connectors", "A_to_B.yaml")]: connector,
  });
  const typos = at("swc", "typos", "Typos.yaml");
  const place = (subject: unknown, rule = "some.rule") => {
    const [f] = parseCheckDocument(
      JSON.stringify({
        schemaVersion: 1,
        findings: [{ rule, severity: "error", message: "m", subjects: [subject], target: null }],
      }),
    ).findings;
    const { file, line, start, end } = placeFinding(refs, f, MODEL_ROOT, MANIFEST, reader);
    return { file, line, start, end };
  };
  // A reference finding on a port's interface: the interface it names.
  assert.deepEqual(
    place({
      layer: "swc",
      element: "Typos",
      path: [{ field: "ports" }, { member: "pSrv" }, { field: "interface" }],
    }),
    { file: typos, ...columnsOf(UNKNOWN_KEY_SWC, "  interface: IServer", "IServer") },
  );
  // A field holding a block: the key.
  assert.deepEqual(place({ layer: "swc", element: "Typos", path: [{ field: "runnables" }] }), {
    file: typos,
    ...columnsOf(UNKNOWN_KEY_SWC, "runnables:", "runnables"),
  });
  // The three shapes model.unknown-key's subject takes over a scalar value — the
  // top-level key, a key below a field, a key below a member: the stray key.
  const unknown = "model.unknown-key";
  assert.deepEqual(
    place({ layer: "tasks", element: "Task_10ms", path: [{ key: "core" }] }, unknown),
    {
      file: at("tasks", "Task_10ms.yaml"),
      ...columnsOf(task, "core: CORE0", "core"),
    },
  );
  assert.deepEqual(
    place(
      { layer: "connectors", element: "A_to_B", path: [{ field: "from" }, { key: "prt" }] },
      unknown,
    ),
    { file: at("connectors", "A_to_B.yaml"), ...columnsOf(connector, "  prt: pOut", "prt") },
  );
  assert.deepEqual(
    place(
      {
        layer: "swc",
        element: "Typos",
        path: [{ field: "ports" }, { member: "pOut" }, { key: "intrface" }],
      },
      unknown,
    ),
    { file: typos, ...columnsOf(UNKNOWN_KEY_SWC, "  intrface: ISig", "intrface") },
  );
  // An empty stray key has no key step: its subject ends at the member, whose identity it keeps.
  assert.deepEqual(
    place(
      { layer: "swc", element: "Typos", path: [{ field: "ports" }, { member: "pOut" }] },
      unknown,
    ),
    { file: typos, ...columnsOf(UNKNOWN_KEY_SWC, "- name: pOut", "pOut") },
  );
});

test("a model.unknown-key finding the text cannot single out falls back to the identity line", () => {
  const refs = shipped();
  const reader = fakeReader(UNKNOWN_KEY_FILES);
  const identityLine = {
    file: at("swc", "typos", "Typos.yaml"),
    line: 0,
    start: 0,
    end: "name: Typos".length,
  };
  const events = (runnable: string) => [
    { field: "runnables" },
    { member: runnable },
    { field: "events" },
  ];
  const mode = { port: "rMode", mode_group_ref: "/M/G", mode_ref: "/M/G/OFF" };
  const document = unknownKeyDocument([
    // Two timing events of one runnable: their string entries are equal.
    {
      layer: "swc",
      element: "Typos",
      path: [...events("Twice"), { member: { type: "timing", period: 0.01 } }, { key: "perod" }],
    },
    // A flow-style event has no key line.
    {
      layer: "swc",
      element: "Typos",
      path: [...events("Flow"), { member: { type: "timing", period: 0.01 } }, { key: "perod" }],
    },
    // A key inside a `disabled_modes` member of an event.
    {
      layer: "swc",
      element: "Typos",
      path: [
        ...events("Modes"),
        { member: { type: "timing", period: 0.01, disabled_modes: [mode] } },
        { field: "disabled_modes" },
        { member: mode },
        { key: "mdoe" },
      ],
    },
  ]);
  for (const f of parseCheckDocument(document).findings) {
    const { file, line, start, end } = placeFinding(refs, f, MODEL_ROOT, MANIFEST, reader);
    assert.deepEqual({ file, line, start, end }, identityLine);
  }
  // A `satisfies` member keeps today's answer: its own value's columns.
  const satisfies = placeFinding(
    refs,
    finding({
      subjects: [
        {
          layer: "swc",
          element: "ExampleSWC",
          path: [{ field: "satisfies" }, { member: "REQ_EXAMPLE_001@3" }],
        },
      ],
    }),
    MODEL_ROOT,
    MANIFEST,
    reader,
  );
  assert.deepEqual(
    { line: satisfies.line, start: satisfies.start, end: satisfies.end },
    columnsOf(EXAMPLE_SWC, "- REQ_EXAMPLE_001@3", "REQ_EXAMPLE_001@3"),
  );
});

// --- one run's file-system reads --------------------------------------------------

/**
 * A model of *count* requirements, `REQ_0000.yaml` …, behind a reader that
 * counts what it is asked: the listings per directory, the reads per file and
 * how many entries of the requirements listing are looked at.
 */
function countedRequirements(count: number) {
  const directory = at("requirements");
  const names = Array.from({ length: count }, (_, i) => `REQ_${String(i).padStart(4, "0")}`);
  const texts = new Map(
    names.map((name) => [path.join(directory, `${name}.yaml`), `id: ${name}\n`]),
  );
  const tally = {
    listings: new Map<string, number>(),
    reads: new Map<string, number>(),
    looked: 0,
  };
  const base: Reader = {
    readText: (file) => {
      tally.reads.set(file, (tally.reads.get(file) ?? 0) + 1);
      return texts.get(file);
    },
    listDir: (listed) => {
      tally.listings.set(listed, (tally.listings.get(listed) ?? 0) + 1);
      if (listed !== directory) {
        return undefined;
      }
      const entries = [...texts.keys()].map((file) => path.basename(file));
      // Every index read of the listing is an entry looked at.
      return new Proxy(entries, {
        get(target, key, receiver) {
          if (typeof key === "string" && /^[0-9]+$/.test(key)) {
            tally.looked += 1;
          }
          return Reflect.get(target, key, receiver);
        },
      });
    },
  };
  const findings = names.map((name) =>
    finding({
      rule: "trace.requirement-unallocated",
      subjects: [{ layer: "requirements", element: name }],
    }),
  );
  return { directory, names, texts, tally, base, findings };
}

test("one run lists the requirements directory once and looks at each entry a bounded number of times", () => {
  const refs = shipped();
  const model = countedRequirements(3000);
  const reader = requestReader(model.base);
  const placed = model.findings.map((f) => placeFinding(refs, f, MODEL_ROOT, MANIFEST, reader));
  assert.deepEqual(
    placed.map(({ file, line }) => ({ file, line })),
    model.names.map((name) => ({ file: path.join(model.directory, `${name}.yaml`), line: 0 })),
  );
  assert.deepEqual([...model.tally.listings], [[model.directory, 1]]);
  assert.ok([...model.tally.reads.values()].every((reads) => reads === 1));
  // One pass to index the listing — not one pass per finding (3000 × 3000).
  assert.ok(
    model.tally.looked <= 2 * model.names.length,
    `${model.tally.looked} entries looked at`,
  );
});

test("a file added between two runs is found by the second; a run keeps its own listing", () => {
  const refs = shipped();
  const model = countedRequirements(3);
  const added = finding({ subjects: [{ layer: "requirements", element: "REQ_NEW" }] });
  const first = requestReader(model.base);
  assert.equal(placeFinding(refs, model.findings[0], MODEL_ROOT, MANIFEST, first).line, 0);
  assert.equal(placeFinding(refs, added, MODEL_ROOT, MANIFEST, first).file, MANIFEST);
  model.texts.set(path.join(model.directory, "REQ_NEW.yaml"), "id: REQ_NEW\n");
  // The first run's snapshot stands until the run ends.
  assert.equal(placeFinding(refs, added, MODEL_ROOT, MANIFEST, first).file, MANIFEST);
  const second = requestReader(model.base);
  assert.deepEqual(
    (({ file, line }) => ({ file, line }))(placeFinding(refs, added, MODEL_ROOT, MANIFEST, second)),
    { file: path.join(model.directory, "REQ_NEW.yaml"), line: 0 },
  );
});

// --- the quick fixes -------------------------------------------------------------

test("linkAt reads the id, the stated version and the value's columns; nothing off a link path", () => {
  const stale = lineOf(EXAMPLE_SWC, "- REQ_EXAMPLE_001@3");
  assert.deepEqual(linkAt(EXAMPLE_SWC, stale), {
    id: "REQ_EXAMPLE_001",
    stated: "3",
    start: 2,
    end: 2 + "REQ_EXAMPLE_001@3".length,
    quote: "",
  });
  const bare = lineOf(EXAMPLE_SWC, "    - REQ_EXAMPLE_001");
  assert.deepEqual(linkAt(EXAMPLE_SWC, bare), {
    id: "REQ_EXAMPLE_001",
    stated: null,
    start: 6,
    end: 6 + "REQ_EXAMPLE_001".length,
    quote: "",
  });
  // A key line, a whole-value item of another field, a line past the end.
  assert.equal(linkAt(EXAMPLE_SWC, lineOf(EXAMPLE_SWC, "    satisfies:")), undefined);
  const memory = doc(["name: RAM0", "kind: memory", "accessible:", "- CORE0"]);
  assert.equal(linkAt(memory, 3), undefined);
  assert.equal(linkAt(EXAMPLE_SWC, 999), undefined);
  // A requirement's derived_from is a link field too.
  const derived = doc(["id: REQ-13", "derived_from:", "- REQ-12@3"]);
  assert.deepEqual(linkAt(derived, 2), {
    id: "REQ-12",
    stated: "3",
    start: 2,
    end: 10,
    quote: "",
  });
  // A version wider than a double keeps every digit: it is compared as written.
  const wide = doc(["name: X", "satisfies:", '- "REQ-12@123456789012345678901"']);
  assert.deepEqual(linkAt(wide, 2), {
    id: "REQ-12",
    stated: "123456789012345678901",
    start: 2,
    end: 2 + '"REQ-12@123456789012345678901"'.length,
    quote: '"',
  });
});

test("a tab between the item's `- ` and its value: the fix replaces the value alone", () => {
  // The scalar reader strips the separation whitespace, so the columns must start past it too.
  const text = doc(["name: X", "satisfies:", "- \tREQ-12@3", "- \t'REQ-12'"]);
  const lines = text.split("\n");
  const apply = (line: number, fix: { start: number; end: number; replacement: string }) =>
    lines[line].slice(0, fix.start) + fix.replacement + lines[line].slice(fix.end);
  const confirm = fixAt(CONFIRM_LINK_RULE, text, 2, "4");
  assert.ok(confirm !== undefined);
  assert.equal(apply(2, confirm), "- \tREQ-12@4");
  const state = fixAt(STATE_VERSION_RULE, text, 3, "4");
  assert.ok(state !== undefined);
  assert.equal(apply(3, state), "- \t'REQ-12@4'");
});

/** quickFixFor on *line* of *text*: the link read there, the finding naming *target*. */
function fixAt(code: string, text: string, line: number, version: string, target = "REQ-12") {
  return quickFixFor(code, target, linkAt(text, line), () => version);
}

test("the two quick fixes: confirm REQ-12@3 → REQ-12@4, state REQ-12 → REQ-12@4", () => {
  const text = doc([
    "name: SpeedCtrlSWC",
    "ports:",
    "- name: pIn",
    "  satisfies:",
    "  - REQ-12@3",
    "satisfies:",
    "- REQ-12",
  ]);
  assert.deepEqual(fixAt(CONFIRM_LINK_RULE, text, 4, "4"), {
    title: "Confirm link to REQ-12 (version 4)",
    replacement: "REQ-12@4",
    start: 4,
    end: 12,
  });
  assert.deepEqual(fixAt(STATE_VERSION_RULE, text, 6, "4"), {
    title: "State the version of REQ-12 (version 4)",
    replacement: "REQ-12@4",
    start: 2,
    end: 8,
  });
  // A quoted value keeps its quotes.
  const quoted = doc(["name: X", "satisfies:", "- 'REQ-12@3'"]);
  assert.deepEqual(fixAt(CONFIRM_LINK_RULE, quoted, 2, "4"), {
    title: "Confirm link to REQ-12 (version 4)",
    replacement: "'REQ-12@4'",
    start: 2,
    end: 12,
  });
});

test("the quick fixes on the recorded document's lines, the version read from the saved requirement", () => {
  const refs = shipped();
  const reader = fakeReader(RECORDED_FILES);
  const current = requirementVersion(refs, "REQ_EXAMPLE_001", MODEL_ROOT, reader);
  assert.equal(current, "4");
  assert.equal(requirementVersion(refs, "REQ_NOWHERE", MODEL_ROOT, reader), undefined);
  const noVersion = fakeReader({
    [at("requirements", "REQ_X.yaml")]: doc(["id: REQ_X", "title: t"]),
  });
  assert.equal(requirementVersion(refs, "REQ_X", MODEL_ROOT, noVersion), undefined);
  const placed = parseCheckDocument(recorded()).findings.map((f) =>
    placeFinding(refs, f, MODEL_ROOT, MANIFEST, reader),
  );
  const texts: Record<string, string> = RECORDED_FILES;
  const fixes = placed.map((p) =>
    quickFixFor(p.code ?? "", p.target, linkAt(texts[p.file], p.line), () => current),
  );
  assert.deepEqual(
    placed.map((p) => p.target),
    ["REQ_EXAMPLE_001", "REQ_EXAMPLE_001", "REQ_EXAMPLE_001"],
  );
  assert.deepEqual(
    fixes.map((fix) => fix?.replacement),
    ["REQ_EXAMPLE_001@4", "REQ_EXAMPLE_001@4", "REQ_EXAMPLE_001@4"],
  );
  assert.deepEqual(
    fixes.map((fix) => fix?.title),
    [
      "Confirm link to REQ_EXAMPLE_001 (version 4)",
      "Confirm link to REQ_EXAMPLE_001 (version 4)",
      "State the version of REQ_EXAMPLE_001 (version 4)",
    ],
  );
  // Each fix replaces exactly the columns the placement names.
  placed.forEach((p, index) => {
    assert.equal(fixes[index]?.start, p.start);
    assert.equal(fixes[index]?.end, p.end);
  });
});

test("no quick fix on a drifted line, off a link path, for a malformed suffix or a non-decimal version", () => {
  const text = doc([
    "name: X",
    "type: application",
    "satisfies:",
    "- REQ-12@3",
    "- REQ-12",
    "- REQ-12@x",
    "- REQ-12@0",
    "- REQ-12@",
    "- hardware:Core0",
    "- REQ-12@4",
    "data_type_mappings:",
    "- REQ-12@3",
  ]);
  // The buffer drifted: the line holds no link, or a link of the other state.
  assert.equal(fixAt(CONFIRM_LINK_RULE, text, 1, "4"), undefined);
  assert.equal(fixAt(CONFIRM_LINK_RULE, text, 4, "4"), undefined);
  assert.equal(fixAt(STATE_VERSION_RULE, text, 3, "4"), undefined);
  // The line now links to another requirement than the finding's: no fix.
  assert.equal(fixAt(CONFIRM_LINK_RULE, text, 3, "4", "REQ-13"), undefined);
  assert.equal(fixAt(STATE_VERSION_RULE, text, 4, "4", "REQ-13"), undefined);
  assert.equal(
    quickFixFor(CONFIRM_LINK_RULE, undefined, linkAt(text, 3), () => "4"),
    undefined,
  );
  // A drifted line asks for no version: the saved file is read only for the finding's own link.
  const asked: string[] = [];
  const recordingVersion = (id: string) => {
    asked.push(id);
    return "4";
  };
  assert.equal(
    quickFixFor(CONFIRM_LINK_RULE, "REQ-13", linkAt(text, 3), recordingVersion),
    undefined,
  );
  assert.equal(
    quickFixFor(CONFIRM_LINK_RULE, "REQ-12", linkAt(text, 1), recordingVersion),
    undefined,
  );
  assert.deepEqual(asked, []);
  assert.notEqual(
    quickFixFor(CONFIRM_LINK_RULE, "REQ-12", linkAt(text, 3), recordingVersion),
    undefined,
  );
  assert.deepEqual(asked, ["REQ-12"]);
  // A requirement whose saved file states no version: no fix.
  assert.equal(
    quickFixFor(CONFIRM_LINK_RULE, "REQ-12", linkAt(text, 3), () => undefined),
    undefined,
  );
  // The same lines with the finding's own requirement: the fix is offered.
  assert.notEqual(fixAt(CONFIRM_LINK_RULE, text, 3, "4"), undefined);
  assert.notEqual(fixAt(STATE_VERSION_RULE, text, 4, "4"), undefined);
  // A stated version equal to the current one, compared as written.
  const wide = doc(["name: X", "satisfies:", "- REQ-12@123456789012345678901"]);
  assert.equal(fixAt(CONFIRM_LINK_RULE, wide, 2, "123456789012345678901"), undefined);
  assert.notEqual(fixAt(CONFIRM_LINK_RULE, wide, 2, "123456789012345678902"), undefined);
  // Already at the current version: nothing left to confirm.
  assert.equal(fixAt(CONFIRM_LINK_RULE, text, 9, "4"), undefined);
  // Malformed suffixes are an error with no fix; the element form is no link to a requirement.
  for (const line of [5, 6, 7, 8]) {
    assert.equal(fixAt(CONFIRM_LINK_RULE, text, line, "4"), undefined, `line ${line}`);
    assert.equal(fixAt(STATE_VERSION_RULE, text, line, "4"), undefined, `line ${line}`);
  }
  // An item outside a satisfies / derived_from path.
  assert.equal(fixAt(CONFIRM_LINK_RULE, text, 11, "4"), undefined);
  // Another rule.
  assert.equal(fixAt("trace.link-unknown-requirement", text, 3, "4"), undefined);
  // A version the loader admits but a link cannot state.
  for (const version of ["0x4", "04", "0", "", "4.0", "-4"]) {
    assert.equal(fixAt(CONFIRM_LINK_RULE, text, 3, version), undefined, version);
  }
});

// --- one run's reading (the adapter's checkModelRun) ---------------------------

const EXECUTABLE = "/home/u/.swcraft/current/bin/swcraft";

test("readCheckRun: the recorded document's findings, and the success sentence on exit 0", () => {
  const reading = readCheckRun(
    { exitCode: 0, spawnFailed: false, stdout: recorded(), stderr: "  WARNING: …\n" },
    EXECUTABLE,
  );
  assert.equal(reading.kind, "findings");
  if (reading.kind === "findings") {
    assert.equal(reading.document.findings.length, 3);
    assert.equal(reading.summary, CHECK_MODEL_SUCCESS_LINE);
  }
  // The sentence is the CLI's own, verbatim.
  const cli = readCliPackageSource();
  assert.ok(cli.includes(`print("${CHECK_MODEL_SUCCESS_LINE}")`));
});

test("readCheckRun: a refusing document's closing line counts each class", () => {
  const finding = (severity: string) => ({
    rule: "r",
    severity,
    message: "m",
    subjects: [],
    target: null,
  });
  const stdout = JSON.stringify({
    schemaVersion: CHECK_SCHEMA_VERSION,
    findings: [finding("error"), finding("error"), finding("warning")],
  });
  const reading = readCheckRun({ exitCode: 1, spawnFailed: false, stdout, stderr: "" }, EXECUTABLE);
  assert.equal(reading.kind === "findings" ? reading.summary : undefined, "2 errors, 1 warning");
  // --warnings-as-errors: exit 1 on warnings alone.
  const warningsOnly = JSON.stringify({
    schemaVersion: CHECK_SCHEMA_VERSION,
    findings: [finding("warning")],
  });
  const refused = readCheckRun(
    { exitCode: 1, spawnFailed: false, stdout: warningsOnly, stderr: "" },
    EXECUTABLE,
  );
  assert.equal(refused.kind === "findings" ? refused.summary : undefined, "0 errors, 1 warning");
});

test("readCheckRun: a run with no document is refused, its reason stderr's, with no closing line", () => {
  const stderr = "Error: model root does not exist: /p/model\n";
  const reading = readCheckRun({ exitCode: 1, spawnFailed: false, stdout: "", stderr }, EXECUTABLE);
  assert.deepEqual(reading, { kind: "refused", reason: stderr });
  // The one entry carries stderr's last line.
  const reader: Reader = { readText: () => "name: p\n", listDir: () => undefined };
  const entry = onlyRefusal(
    "/p/swcraft.yaml",
    reading.kind === "refused" ? reading.reason : "",
    reader,
  );
  assert.equal(entry.message, "Error: model root does not exist: /p/model");
  assert.equal(entry.severity, "error");
});

test("readCheckRun: an older toolchain's usage error on --format reads as different releases", () => {
  const stderr =
    "usage: swcraft check-model [-h] --project PROJECT\n" +
    "swcraft: error: unrecognized arguments: --format json\n";
  const reading = readCheckRun({ exitCode: 2, spawnFailed: false, stdout: "", stderr }, EXECUTABLE);
  assert.equal(reading.kind, "refused");
  assert.match(reading.kind === "refused" ? reading.reason : "", /different releases/);
  assert.equal(reading.summary, undefined, "stderr already says it in the channel");
  // Another usage error keeps stderr's own words — a current toolchain's
  // usage line names --format too, and that is not a different release.
  const other = readCheckRun(
    { exitCode: 2, spawnFailed: false, stdout: "", stderr: "swcraft: error: bad\n" },
    EXECUTABLE,
  );
  assert.deepEqual(other, { kind: "refused", reason: "swcraft: error: bad\n" });
  const current =
    "usage: swcraft check-model [-h] --project PROJECT [--format {text,json}]\n" +
    "swcraft: error: argument --project: expected one argument\n";
  assert.deepEqual(
    readCheckRun({ exitCode: 2, spawnFailed: false, stdout: "", stderr: current }, EXECUTABLE),
    { kind: "refused", reason: current },
  );
});

test("readCheckRun: a run that printed nothing anywhere is refused with a closing line naming its exit", () => {
  const signalled = readCheckRun(
    { exitCode: null, spawnFailed: false, stdout: "", stderr: "" },
    EXECUTABLE,
  );
  assert.equal(signalled.kind, "refused");
  assert.match(signalled.summary ?? "", /without a document.*stopped by a signal/);
  assert.equal(signalled.kind === "refused" ? signalled.reason : "", signalled.summary);
  const silent = readCheckRun(
    { exitCode: 3, spawnFailed: false, stdout: " \n", stderr: "\n" },
    EXECUTABLE,
  );
  assert.match(silent.summary ?? "", /exit code 3/);
  // Exit 0 with no document is still no document: refused, its exit named.
  const empty = readCheckRun(
    { exitCode: 0, spawnFailed: false, stdout: "", stderr: "" },
    EXECUTABLE,
  );
  assert.equal(empty.kind, "refused");
  assert.match(empty.summary ?? "", /exit code 0/);
});

test("readCheckRun: a clean run's empty findings end with the success sentence and place nothing", () => {
  const reading = readCheckRun(
    {
      exitCode: 0,
      spawnFailed: false,
      stdout: JSON.stringify({ schemaVersion: CHECK_SCHEMA_VERSION, findings: [] }),
      stderr: "",
    },
    EXECUTABLE,
  );
  assert.deepEqual(reading, {
    kind: "findings",
    document: { findings: [] },
    summary: CHECK_MODEL_SUCCESS_LINE,
  });
});

test("readCheckRun: a launch failure names the executable; a stdout it cannot read says why", () => {
  const failed = readCheckRun(
    { exitCode: null, spawnFailed: true, stdout: "", stderr: "" },
    EXECUTABLE,
  );
  assert.equal(failed.kind, "refused");
  assert.ok((failed.kind === "refused" ? failed.reason : "").includes(EXECUTABLE));
  const garbled = readCheckRun(
    { exitCode: 0, spawnFailed: false, stdout: '{"schemaVersion": 9, "findings": []}', stderr: "" },
    EXECUTABLE,
  );
  assert.equal(garbled.kind, "refused");
  assert.match(garbled.kind === "refused" ? garbled.reason : "", /different releases/);
  assert.equal(garbled.summary, garbled.kind === "refused" ? garbled.reason : "");
  // A stdout that is not JSON at all.
  const notJson = readCheckRun(
    { exitCode: 1, spawnFailed: false, stdout: "Traceback (most recent call last):\n", stderr: "" },
    EXECUTABLE,
  );
  assert.equal(notJson.kind, "refused");
  assert.match(notJson.kind === "refused" ? notJson.reason : "", /not JSON/);
  assert.equal(notJson.summary, notJson.kind === "refused" ? notJson.reason : "");
});

test("QUICK_FIX_RULES: the two link warnings and nothing else", () => {
  assert.deepEqual([...QUICK_FIX_RULES].sort(), [CONFIRM_LINK_RULE, STATE_VERSION_RULE].sort());
});

test("LINK_FIELDS: the fields the published versioned-list-values relations end at, and no other", () => {
  const ends = shipped()
    .relations.filter((edge) => edge.resolution === "versioned-list-values")
    .flatMap((edge) => edge.paths.map((entry) => entry.path[entry.path.length - 1]));
  assert.ok(ends.length > 0);
  assert.deepEqual([...new Set(ends)].sort(), [...LINK_FIELDS].sort());
});

// --- a load refusal placed on its file ---------------------------------------------
//
// The CLI prints one `Error: Invalid <kind> YAML in <path>: <detail>` line per
// field of the first malformed element file (`ElementParseError.render_lines`,
// full path); each line is one entry on the file it names.

const REQ_FILE = at("requirements", "REQ_X.yaml");
const REQ_TEXT = doc(["version: 1", "text: Something", "derived_from:", "  - 5"]);
const BASE_FILE = at("data_types", "base", "bool_base.yaml");
const BASE_TEXT = doc(["name: bool_base", "bsae: boolean", "description: A flag"]);
const HW_FILE = at("hardware", "ECU1.yaml");
const HW_TEXT = doc([
  "name: ECU1",
  "memory:",
  "  RAM0: lots",
  "cores:",
  "  - name: C0",
  "    frequency: 7",
]);
const LOAD_MANIFEST = doc(["name: p", "model_root: model"]);

function loadReader(extra: Record<string, string> = {}): Reader {
  return fakeReader({
    [MANIFEST]: LOAD_MANIFEST,
    [REQ_FILE]: REQ_TEXT,
    [BASE_FILE]: BASE_TEXT,
    [HW_FILE]: HW_TEXT,
    ...extra,
  });
}

function loadLine(kind: string, file: string, detail: string): string {
  return `Error: Invalid ${kind} YAML in ${file}: ${detail}`;
}

function refusalsOf(reason: string, reader: Reader = loadReader()): Placement[] {
  return placeRefusals(MANIFEST, reason, reader);
}

/** *entry*'s file, line and columns: the line whole, as every load refusal entry covers it. */
function wholeLineOf(entry: Placement, file: string, text: string, line: number) {
  assert.deepEqual(
    { file: entry.file, line: entry.line, start: entry.start, end: entry.end },
    { file, line, start: 0, end: text.split("\n")[line].length },
  );
}

test("a missing field sits on the file's line 0, its message the line without Error:", () => {
  const entries = refusalsOf(`${loadLine("requirement", REQ_FILE, "id: Field required")}\n`);
  assert.equal(entries.length, 1);
  wholeLineOf(entries[0], REQ_FILE, REQ_TEXT, 0);
  assert.deepEqual(
    { severity: entries[0].severity, code: entries[0].code, related: entries[0].related },
    { severity: "error", code: undefined, related: [] },
  );
  assert.equal(entries[0].message, `Invalid requirement YAML in ${REQ_FILE}: id: Field required`);
});

test("a closed data type's unknown key sits on that key's line", () => {
  const [entry] = refusalsOf(
    loadLine("base type", BASE_FILE, "bsae: Extra inputs are not permitted"),
  );
  wholeLineOf(entry, BASE_FILE, BASE_TEXT, 1);
});

test("a key the CLI writes escaped is not found on a line: the file's line 0, or its parent's line", () => {
  // A quoted key holding a control character is written escaped (`bad\nkey`), so its
  // line is never found: a top-level key falls back to line 0, a nested one to the
  // deepest key line found above it. The message stays one line, the key escaped.
  const file = at("data_types", "base", "flag_base.yaml");
  const text = doc(["name: flag_base", '"bad\\nkey": x']);
  const [top] = refusalsOf(
    loadLine("base type", file, "bad\\nkey: Extra inputs are not permitted"),
    loadReader({ [file]: text }),
  );
  wholeLineOf(top, file, text, 0);
  assert.equal(
    top.message,
    `Invalid base type YAML in ${file}: bad\\nkey: Extra inputs are not permitted`,
  );
  const nestedText = doc(["name: ECU2", "memory:", '  "a\\tb": 1']);
  const [nested] = refusalsOf(
    loadLine("hardware", file, "memory.a\\tb: Input should be a valid integer"),
    loadReader({ [file]: nestedText }),
  );
  wholeLineOf(nested, file, nestedText, 1);
});

test("a nested field sits on the deepest key line found", () => {
  const [found] = refusalsOf(
    loadLine("hardware", HW_FILE, "memory.RAM0: Input should be a valid integer"),
  );
  wholeLineOf(found, HW_FILE, HW_TEXT, 2);
  const [prefix] = refusalsOf(loadLine("hardware", HW_FILE, "memory.RAM1: Field required"));
  wholeLineOf(prefix, HW_FILE, HW_TEXT, 1);
  // A field below a list item is cut at its first index: the list's key line.
  const [item] = refusalsOf(
    loadLine("hardware", HW_FILE, "cores[0].frequency: Input should be greater than 10"),
  );
  wholeLineOf(item, HW_FILE, HW_TEXT, 3);
  const [event] = refusalsOf(
    loadLine("hardware", HW_FILE, "cores[0]: matches none of the forms this field allows"),
  );
  wholeLineOf(event, HW_FILE, HW_TEXT, 3);
});

test("two lines are two entries on the same file, in the CLI's order", () => {
  const reason = [
    "some progress",
    loadLine("hardware", HW_FILE, "memory.RAM0: Input should be a valid integer"),
    loadLine("hardware", HW_FILE, "name: Field required"),
    "",
  ].join("\n");
  const entries = refusalsOf(reason);
  assert.equal(entries.length, 2);
  wholeLineOf(entries[0], HW_FILE, HW_TEXT, 2);
  wholeLineOf(entries[1], HW_FILE, HW_TEXT, 0);
  assert.match(entries[0].message, /memory\.RAM0: Input should be a valid integer$/);
  assert.match(entries[1].message, /name: Field required$/);
});

test("a YAML syntax line sits on its line, 1-based to 0-based", () => {
  const [entry] = refusalsOf(
    loadLine("hardware", HW_FILE, "line 3, column 9: mapping values are not allowed here"),
  );
  wholeLineOf(entry, HW_FILE, HW_TEXT, 2);
});

test("a YAML mark past the last line sits on the last line, with or without a final newline", () => {
  // PyYAML marks an end of stream after a final newline on the line below it:
  // `name: [1\n` is refused at line 2, column 1, on a file of one line.
  for (const text of ["name: [1\n", "name: [1", "name: [1\r\n"]) {
    const file = at("hardware", "ECU2.yaml");
    const [entry] = refusalsOf(
      loadLine("hardware", file, "line 2, column 1: expected ',' or ']', but got '<stream end>'"),
      loadReader({ [file]: text }),
    );
    assert.deepEqual(
      { file: entry.file, line: entry.line, start: entry.start, end: entry.end },
      { file, line: 0, start: 0, end: "name: [1".length },
      JSON.stringify(text),
    );
  }
  // A mark far past the end, and an empty file, still land on a line that exists.
  const file = at("hardware", "ECU3.yaml");
  const [far] = refusalsOf(
    loadLine("hardware", file, "line 40, column 1: unexpected end"),
    loadReader({ [file]: HW_TEXT }),
  );
  wholeLineOf(far, file, HW_TEXT, HW_TEXT.split("\n").length - 2);
  const [empty] = refusalsOf(
    loadLine("hardware", file, "line 2, column 1: unexpected end"),
    loadReader({ [file]: "" }),
  );
  assert.deepEqual([empty.line, empty.start, empty.end], [0, 0, 0]);
});

test("rehomeRefusedFile: a path under the model root's real path is re-rooted onto the root as spelled", () => {
  const spelled = path.resolve("/tmp/project/model");
  const real = path.resolve("/private/tmp/project/model");
  const realpath = (each: string): string => (each === spelled ? real : each);
  const printed = path.join(real, "hardware", "ECU1.yaml");
  assert.equal(
    rehomeRefusedFile(printed, spelled, realpath),
    path.join(spelled, "hardware", "ECU1.yaml"),
  );
  // Already spelled as the editor spells it: unchanged.
  const same = path.join(MODEL_ROOT, "hardware", "ECU1.yaml");
  assert.equal(
    rehomeRefusedFile(same, MODEL_ROOT, (each) => each),
    same,
  );
  // A directory whose name starts with `..` lies under the root: re-rooted too.
  const dotted = path.join(real, "..tmp", "ECU1.yaml");
  assert.equal(
    rehomeRefusedFile(dotted, spelled, realpath),
    path.join(spelled, "..tmp", "ECU1.yaml"),
  );
  // Outside the model root, no model root, or a root that cannot be resolved: the printed path.
  const outside = path.resolve("/elsewhere/ECU1.yaml");
  assert.equal(rehomeRefusedFile(outside, spelled, realpath), outside);
  assert.equal(rehomeRefusedFile(printed, undefined, realpath), printed);
  const failing = (): string => {
    throw new Error("ENOENT");
  };
  assert.equal(rehomeRefusedFile(printed, spelled, failing), printed);
});

test("placeRefusals reads and places each file at the path its rehome gives", () => {
  const printed = path.resolve("/private/elsewhere/model/hardware/ECU1.yaml");
  const [entry] = placeRefusals(
    MANIFEST,
    loadLine("hardware", printed, "memory.RAM0: Input should be a valid integer"),
    loadReader(),
    (file) => (file === printed ? HW_FILE : file),
  );
  wholeLineOf(entry, HW_FILE, HW_TEXT, 2);
  // The message stays the CLI's line, the printed path in it.
  assert.ok(entry.message.includes(printed));
});

test("a model validator's sentence sits on the file's line 0", () => {
  const [entry] = refusalsOf(loadLine("base type", BASE_FILE, "Value error, length must be > 0"));
  wholeLineOf(entry, BASE_FILE, BASE_TEXT, 0);
});

test("a Windows-style path parses, its drive colon no separator", () => {
  const file = "C:\\proj\\model\\data_types\\base\\bool_base.yaml";
  const [entry] = refusalsOf(
    loadLine("base type", file, "bsae: Extra inputs are not permitted"),
    loadReader({ [file]: BASE_TEXT }),
  );
  wholeLineOf(entry, file, BASE_TEXT, 1);
  assert.equal(
    entry.message,
    `Invalid base type YAML in ${file}: bsae: Extra inputs are not permitted`,
  );
});

test("a file that cannot be read sits at the manifest's line 0, the same message", () => {
  const missing = at("requirements", "REQ_GONE.yaml");
  const line = loadLine("requirement", missing, "id: Field required");
  const [entry] = refusalsOf(line);
  wholeLineOf(entry, MANIFEST, LOAD_MANIFEST, 0);
  assert.equal(entry.message, line.slice("Error: ".length));
});

test("a refusal naming no element file is the old single entry (a guard)", () => {
  const entries = refusalsOf("Error: model root does not exist: /p/model\n");
  assert.equal(entries.length, 1);
  wholeLineOf(entries[0], MANIFEST, LOAD_MANIFEST, 0);
  assert.equal(entries[0].message, "Error: model root does not exist: /p/model");
});

test("the pin refusal still sits on the toolchain_version line (a guard)", () => {
  const manifest = doc(["name: p", 'toolchain_version: "0.19.2"']);
  const entries = placeRefusals(MANIFEST, PIN_STDERR, fakeReader({ [MANIFEST]: manifest }));
  assert.equal(entries.length, 1);
  wholeLineOf(entries[0], MANIFEST, manifest, 1);
});

test("an older toolchain's Pydantic text, its URL last, is the manifest's one entry (a guard)", () => {
  const url = "For further information visit https://errors.pydantic.dev/2.9/v/missing";
  const reason = [
    loadLine("requirement", REQ_FILE, "5 validation errors for Requirement"),
    "id",
    "  Field required [type=missing, input_value={'version': 1}, input_type=dict]",
    `    ${url}`,
    "",
  ].join("\n");
  const entries = refusalsOf(reason);
  assert.equal(entries.length, 1, "one entry, not a file entry");
  wholeLineOf(entries[0], MANIFEST, LOAD_MANIFEST, 0);
  assert.equal(entries[0].message, url);
  const one = refusalsOf(
    `${loadLine("task", REQ_FILE, "1 validation error for Task")}\nperiod\n  ${url}\n`,
  );
  assert.equal(one.length, 1);
  assert.equal(one[0].file, MANIFEST);
});

test("an older toolchain's YAML parser text is placed on its file by its first line, at line 0", () => {
  const reason = [
    loadLine("hardware", HW_FILE, "mapping values are not allowed here"),
    `  in "${HW_FILE}", line 3, column 9`,
    "",
  ].join("\n");
  const entries = refusalsOf(reason);
  assert.equal(entries.length, 1);
  wholeLineOf(entries[0], HW_FILE, HW_TEXT, 0);
});

test("a detail quoting another .yaml name splits at the first .yaml: ", () => {
  const [entry] = refusalsOf(
    loadLine("base type", BASE_FILE, "Value error, see other.yaml: bsae: not here"),
  );
  wholeLineOf(entry, BASE_FILE, BASE_TEXT, 0);
});

test("a directory named x.yaml in the path keeps the whole file name", () => {
  const file = path.join(MODEL_ROOT, "x.yaml", "data_types", "base", "bool_base.yaml");
  const [entry] = refusalsOf(
    loadLine("base type", file, "bsae: Extra inputs are not permitted"),
    loadReader({ [file]: BASE_TEXT }),
  );
  wholeLineOf(entry, file, BASE_TEXT, 1);
});

test("a one-line '; '-joined text, which no check run prints, is one entry", () => {
  const entries = refusalsOf(
    loadLine(
      "hardware",
      HW_FILE,
      "memory.RAM0: Input should be a valid integer; name: Field required",
    ),
  );
  assert.equal(entries.length, 1);
  wholeLineOf(entries[0], HW_FILE, HW_TEXT, 2);
});

test("a .yml file and a line without Error: are read the same way", () => {
  const file = at("hardware", "ECU2.yml");
  const [entry] = refusalsOf(
    `Invalid hardware YAML in ${file}: memory.RAM0: Input should be a valid integer\n`,
    loadReader({ [file]: HW_TEXT }),
  );
  wholeLineOf(entry, file, HW_TEXT, 2);
});

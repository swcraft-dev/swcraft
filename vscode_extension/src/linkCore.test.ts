// The document links: every decision the two link providers make, tested on text, the shipped
// `references.json` and a fake file system. No `vscode` import: this runs under plain `node --test`.
import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as path from "node:path";
import { test } from "node:test";

import {
  COMPONENT_REPORT_HEADER,
  type DiscoveredLink,
  descriptionLinks,
  MAX_DOCUMENT_LINKS,
  MODEL_REPORT_HEADER,
  type ModelScan,
  reportKind,
  reportLinks,
  resolveCandidate,
  scanModel,
  storageNameMatches,
} from "./linkCore";
import { loadReferences, type Reader, type References } from "./navigationCore";
import { readSource, SCHEMAS_DIR } from "./testSupport";

const MODEL_ROOT = path.resolve("/projects/vc/model");

function shipped(): References {
  return loadReferences(fs.readFileSync(path.join(SCHEMAS_DIR, "references.json"), "utf8"));
}

function doc(lines: string[], eol = "\n"): string {
  return `${lines.join(eol)}${eol}`;
}

function at(...parts: string[]): string {
  return path.join(MODEL_ROOT, ...parts);
}

/** A file system of *files*; `readable: false` fails the test on any read. */
function fakeReader(files: Record<string, string>, readable = true): Reader & { reads: string[] } {
  const reads: string[] = [];
  return {
    reads,
    readText: (filePath) => {
      assert.ok(readable, `discovery read ${filePath}`);
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

const MODEL = {
  [at("swc", "application", "BrakeController.yaml")]: doc([
    "name: BrakeController",
    "description: Reads interfaces/IBrake and writes ISpeed.",
  ]),
  [at("swc", "Plain.yaml")]: doc(["name: Plain"]),
  [at("interfaces", "IBrake.yaml")]: doc(["# a comment", "name: IBrake", "kind: sender_receiver"]),
  [at("interfaces", "ISpeed.yaml")]: doc(["name: ISpeed"]),
  [at("interfaces", "Twice.yaml")]: doc(["name: Twice"]),
  [at("compositions", "Twice.yaml")]: doc(["name: Twice"]),
  [at("interfaces", "IDoor~1.yaml")]: doc(["# a collision file", "name: idoor"]),
  [at("interfaces", "Refused.yaml")]: doc(["name: Refused", "name: Again"]),
  [at("data_types", "implementation", "Speed_T.yaml")]: doc(["name: Speed_T"]),
  [at("hardware", "Core0.yaml")]: doc(["name: Core0"]),
};

function scanOf(files: Record<string, string> = MODEL): ModelScan {
  return scanModel(shipped(), MODEL_ROOT, fakeReader(files, false));
}

/** Each link as `line:start-end text → listed file's model-root-relative path`. */
function shown(text: string, links: readonly DiscoveredLink[]): string[] {
  const lines = text.split(/\r?\n/);
  return links.map((link) => {
    const word = lines[link.line].slice(link.start, link.end);
    const listed = path.relative(MODEL_ROOT, link.candidate.listed).split(path.sep).join("/");
    return `${link.line}:${word} → ${link.candidate.kind}:${listed}`;
  });
}

function storage(line: string, scan = scanOf()): string[] {
  return storageNameMatches(line, shipped(), scan).map((match) => {
    const word = line.slice(match.start, match.end);
    return match.candidate === undefined ? `${word} ✗` : `${word} ${match.candidate.kind}`;
  });
}

// --- the storage-name grammar ----------------------------------------------------

test("a storage name with and without the suffix, nested and longest-directory", () => {
  assert.deepEqual(storage("see interfaces/IBrake here"), ["interfaces/IBrake element"]);
  assert.deepEqual(storage("see interfaces/IBrake.yaml"), ["interfaces/IBrake.yaml file"]);
  assert.deepEqual(storage("swc/application/BrakeController.yaml"), [
    "swc/application/BrakeController.yaml file",
  ]);
  assert.deepEqual(storage("swc/application/BrakeController"), [
    "swc/application/BrakeController element",
  ]);
  assert.deepEqual(storage("data_types/implementation/Speed_T"), [
    "data_types/implementation/Speed_T element",
  ]);
});

test("a layer directory inside another's is matched longest first", () => {
  // No shipped layer directory is a prefix of another (the registry refuses one),
  // so the order is held on a synthetic artifact: `a` flat and `a/b` flat. Tried
  // shortest first, `a/b/X` would read as `a`'s name with a group segment — none.
  const raw = JSON.parse(fs.readFileSync(path.join(SCHEMAS_DIR, "references.json"), "utf8"));
  raw.layers.interfaces.directory = "a";
  raw.layers.connectors.directory = "a/b";
  const refs = loadReferences(JSON.stringify(raw));
  const scan = scanModel(
    refs,
    MODEL_ROOT,
    fakeReader({ [at("a", "b", "X.yaml")]: doc(["name: X"]) }, false),
  );
  const [match] = storageNameMatches("see a/b/X here", refs, scan);
  assert.deepEqual(match.candidate && [match.candidate.kind, match.candidate.layer], [
    "element",
    "connectors",
  ]);
});

test("a sentence's period is not part of a storage name", () => {
  const line = "It reads interfaces/IBrake.";
  const [match] = storageNameMatches(line, shipped(), scanOf());
  assert.equal(line.slice(match.start, match.end), "interfaces/IBrake");
  assert.equal(match.candidate?.kind, "element");
});

test("an ARXML path, a flat layer with a group segment and an unlisted name link nothing", () => {
  assert.deepEqual(storage("/PortInterfaces/interfaces/IBrake"), []);
  assert.deepEqual(storage("interfaces/SENDER-RECEIVER-INTERFACE/IBrake"), [
    "interfaces/SENDER-RECEIVER-INTERFACE/IBrake ✗",
  ]);
  assert.deepEqual(storage("interfaces/Missing"), ["interfaces/Missing ✗"]);
  assert.deepEqual(storage("interfaces/Missing.yaml"), ["interfaces/Missing.yaml ✗"]);
});

test("a suffix-less storage name prefers the file at the written storage name", () => {
  const files = {
    [at("swc", "a", "Brake.yaml")]: doc(["name: Brake"]),
    [at("swc", "b", "Brake.yaml")]: doc(["name: Brake"]),
  };
  const [match] = storageNameMatches("swc/b/Brake", shipped(), scanOf(files));
  assert.equal(match.candidate?.listed, at("swc", "b", "Brake.yaml"));
});

// --- descriptions -----------------------------------------------------------------

// Every scan here is taken through a reader that fails the test on a read, and
// discovery is handed no reader at all: discovery reads no file.
test("a description links its storage names and its bare names", () => {
  const text = MODEL[at("swc", "application", "BrakeController.yaml")];
  const links = descriptionLinks(shipped(), text, "swc", scanOf());
  assert.deepEqual(shown(text, links), [
    "1:interfaces/IBrake → element:interfaces/IBrake.yaml",
    "1:ISpeed → element:interfaces/ISpeed.yaml",
  ]);
});

test("a bare name links by exact case only, in one layer only, never to the document itself", () => {
  const text = doc([
    "name: BrakeController",
    "description: ibrake Twice BrakeController IDoor idoor Core0 x Plain",
  ]);
  assert.deepEqual(shown(text, descriptionLinks(shipped(), text, "swc", scanOf())), [
    "1:Core0 → element:hardware/Core0.yaml",
    "1:Plain → element:swc/Plain.yaml",
  ]);
});

test("a bare name a storage name covers is linked once", () => {
  const text = doc(["name: X", "description: interfaces/ISpeed"]);
  assert.deepEqual(shown(text, descriptionLinks(shipped(), text, "swc", scanOf())), [
    "1:interfaces/ISpeed → element:interfaces/ISpeed.yaml",
  ]);
});

test("a nested description, a wrapped plain value and a block are scanned; a comment is not", () => {
  const text = doc([
    "name: Brake",
    "ports:",
    "- name: rBrake",
    "  description: IBrake # ISpeed is a comment",
    "runnables:",
    "- name: Run",
    "  description: first",
    "    then ISpeed",
    "    # Core0 in a comment line",
    "notes: Core0",
    "description: |",
    "  a block naming Core0 # and Plain inside it",
    "",
    "  interfaces/IBrake.yaml",
    "type: application # Plain",
  ]);
  assert.deepEqual(shown(text, descriptionLinks(shipped(), text, "swc", scanOf())), [
    "3:IBrake → element:interfaces/IBrake.yaml",
    "7:ISpeed → element:interfaces/ISpeed.yaml",
    "11:Core0 → element:hardware/Core0.yaml",
    "11:Plain → element:swc/Plain.yaml",
    "13:interfaces/IBrake.yaml → file:interfaces/IBrake.yaml",
  ]);
});

test("a description on an item's dash line is scanned; one behind two dashes is not", () => {
  const text = doc(["name: X", "ports:", "- description: ISpeed", "- - description: Core0"]);
  assert.deepEqual(shown(text, descriptionLinks(shipped(), text, "swc", scanOf())), [
    "2:ISpeed → element:interfaces/ISpeed.yaml",
  ]);
});

test("a document whose own identity is refused excludes no name of its own", () => {
  const text = doc(["name: BrakeController", "name: Again", "description: BrakeController"]);
  assert.deepEqual(shown(text, descriptionLinks(shipped(), text, "swc", scanOf())), [
    "2:BrakeController → element:swc/application/BrakeController.yaml",
  ]);
});

test("a CRLF model file links the same ranges as its LF twin", () => {
  const lines = ["name: X", "description: ISpeed and", "  interfaces/IBrake # Core0"];
  const strip = (text: string) =>
    descriptionLinks(shipped(), text, "swc", scanOf()).map(
      ({ line, start, end }) => `${line}:${start}-${end}`,
    );
  assert.deepEqual(strip(doc(lines, "\r\n")), strip(doc(lines)));
  assert.equal(strip(doc(lines, "\r\n")).length, 2);
});

test("a one-character bare name is never linked, even when a file of that stem is listed", () => {
  const scan = scanOf({ ...MODEL, [at("hardware", "X.yaml")]: doc(["name: X"]) });
  const text = doc(["name: Y", "description: X and Core0"]);
  assert.deepEqual(shown(text, descriptionLinks(shipped(), text, "swc", scan)), [
    "1:Core0 → element:hardware/Core0.yaml",
  ]);
  // The storage name of the same file is linked: only the bare word is too short.
  assert.deepEqual(storage("hardware/X", scan), ["hardware/X element"]);
});

test("a quoted description is scanned inside its quotes", () => {
  const text = doc(["name: X", 'description: "uses ISpeed # and Core0"']);
  assert.deepEqual(shown(text, descriptionLinks(shipped(), text, "swc", scanOf())), [
    "1:ISpeed → element:interfaces/ISpeed.yaml",
    "1:Core0 → element:hardware/Core0.yaml",
  ]);
});

test("a file of no layer gets no description link", () => {
  assert.deepEqual(descriptionLinks(shipped(), doc(["description: ISpeed"]), "nope", scanOf()), []);
});

// --- reports ----------------------------------------------------------------------

const MODEL_REPORT = [
  "Import report: /exports/vehicle.arxml",
  "Wrote interfaces/IBrake.yaml for /PortInterfaces/IBrake",
  "Placed 'BrakeController' in swc/application/BrakeController.yaml.",
  "member 'x' of layer 'swc'; source package name 'Twice'; 'ISpeed' and 'Missing'",
];

test("reportKind reads the two --report headers only", () => {
  assert.equal(reportKind("Import report: a.arxml"), "model");
  assert.equal(reportKind("Component import report: a.arxml"), "component");
  assert.equal(reportKind("import report: a.arxml"), undefined);
  assert.equal(reportKind("name: X"), undefined);
});

test("the two report headers transcribe the first line each --report renderer writes", () => {
  // Duplicated across the language boundary: the first line is the only thing that makes a
  // document a report, so a renamed header must fail here. The component report's first line is
  // its \`title\` property (the module's IMPORT_REPORT_TITLE and the source).
  const firstLine = (module: string): string | undefined =>
    /(?:lines = \[|return )f"([^"{]*)\{self\.source\}"/.exec(readSource(module))?.[1];
  assert.equal(
    firstLine("../packages/tools/src/swcraft_tools/arxml_import/report.py"),
    MODEL_REPORT_HEADER,
  );
  const component = readSource("../packages/tools/src/swcraft_tools/component_exchange/report.py");
  assert.equal(/^IMPORT_REPORT_TITLE = "([^"]*)"$/m.exec(component)?.[1], COMPONENT_REPORT_HEADER);
  assert.match(
    component,
    /def title\(self\) -> str:\n(?:\s+"""[^\n]*\n)?\s+return IMPORT_REPORT_TITLE \+ self\.source\n/,
  );
  assert.match(
    component,
    /lines = \[f"# \{heading_text\(IMPORT_REPORT_TITLE \+ self\.source_name\)\}"\]/,
  );
});

test("a model report links its storage names and its quoted element names", () => {
  const text = doc(MODEL_REPORT);
  assert.deepEqual(shown(text, reportLinks(shipped(), text, scanOf())), [
    "1:interfaces/IBrake.yaml → file:interfaces/IBrake.yaml",
    "2:swc/application/BrakeController.yaml → file:swc/application/BrakeController.yaml",
    "2:BrakeController → element:swc/application/BrakeController.yaml",
    "3:ISpeed → element:interfaces/ISpeed.yaml",
  ]);
});

test("a report in CRLF line endings links the same ranges", () => {
  const lf = doc(MODEL_REPORT);
  const crlf = doc(MODEL_REPORT, "\r\n");
  const strip = (links: readonly DiscoveredLink[]) =>
    links.map(({ line, start, end }) => `${line}:${start}-${end}`);
  assert.deepEqual(
    strip(reportLinks(shipped(), crlf, scanOf())),
    strip(reportLinks(shipped(), lf, scanOf())),
  );
  assert.equal(reportLinks(shipped(), crlf, scanOf()).length, 4);
});

test("a component report links storage names only; any other document nothing", () => {
  const text = doc([
    "Component import report: /exports/brake.arxml",
    "  BrakeController 'ISpeed' interfaces/ISpeed",
  ]);
  assert.deepEqual(shown(text, reportLinks(shipped(), text, scanOf())), [
    "1:interfaces/ISpeed → element:interfaces/ISpeed.yaml",
  ]);
  assert.deepEqual(reportLinks(shipped(), doc(["Report", "interfaces/ISpeed"]), scanOf()), []);
});

// --- the cap and deduplication -------------------------------------------------

function wideScan(count: number): ModelScan {
  const files: Record<string, string> = {};
  for (let index = 0; index < count; index += 1) {
    files[at("interfaces", `I${index}.yaml`)] = "";
  }
  return scanOf(files);
}

test("one target linked many times is counted once against the cap, and shares one candidate", () => {
  const lines = ["Import report: x"];
  for (let index = 0; index < MAX_DOCUMENT_LINKS + 10; index += 1) {
    lines.push("'I0' interfaces/I0");
  }
  const links = reportLinks(shipped(), doc(lines), wideScan(1));
  assert.equal(links.length, 2 * (MAX_DOCUMENT_LINKS + 10));
  assert.equal(new Set(links.map((link) => link.candidate)).size, 1);
});

test("past the cap of distinct targets the rest of the document is not linked", () => {
  const lines = ["Import report: x"];
  for (let index = 0; index <= MAX_DOCUMENT_LINKS; index += 1) {
    lines.push(`'I${index}'`);
  }
  lines.push("'I0'");
  const links = reportLinks(shipped(), doc(lines), wideScan(MAX_DOCUMENT_LINKS + 1));
  assert.equal(links.length, MAX_DOCUMENT_LINKS);
  assert.equal(new Set(links.map((link) => link.candidate)).size, MAX_DOCUMENT_LINKS);
});

// --- resolution -------------------------------------------------------------------

function resolved(line: string): Array<{ file: string; line: number }> {
  const refs = shipped();
  const reader = fakeReader(MODEL);
  return storageNameMatches(line, refs, scanOf()).map((match) => {
    assert.ok(match.candidate);
    const target = resolveCandidate(match.candidate, refs, MODEL_ROOT, reader);
    return {
      file: path.relative(MODEL_ROOT, target.file).split(path.sep).join("/"),
      line: target.line,
    };
  });
}

test("resolution answers the file and its 1-based identity line", () => {
  assert.deepEqual(resolved("interfaces/IBrake"), [{ file: "interfaces/IBrake.yaml", line: 2 }]);
  assert.deepEqual(resolved("interfaces/IBrake.yaml"), [
    { file: "interfaces/IBrake.yaml", line: 2 },
  ]);
  assert.deepEqual(resolved("interfaces/idoor"), [{ file: "interfaces/IDoor~1.yaml", line: 2 }]);
});

test("a file gone since the scan, a layer the artifact lacks, or no model root: the listed file at line 1", () => {
  const refs = shipped();
  const listed = at("interfaces", "IBrake.yaml");
  const gone = fakeReader({});
  assert.deepEqual(
    resolveCandidate({ kind: "file", layer: "interfaces", listed }, refs, MODEL_ROOT, gone),
    {
      file: listed,
      line: 1,
    },
  );
  const reader = fakeReader(MODEL);
  assert.deepEqual(
    resolveCandidate({ kind: "file", layer: "nope", listed }, refs, MODEL_ROOT, reader),
    {
      file: listed,
      line: 1,
    },
  );
  assert.deepEqual(
    resolveCandidate(
      { kind: "element", layer: "interfaces", identity: "IBrake", listed },
      refs,
      undefined,
      reader,
    ),
    { file: listed, line: 1 },
  );
  // The positive control: the same file kind, read, answers its identity line.
  assert.deepEqual(
    resolveCandidate({ kind: "file", layer: "interfaces", listed }, refs, MODEL_ROOT, reader),
    {
      file: listed,
      line: 2,
    },
  );
});

test("a confirmation that refuses falls back to the listed file at line 1", () => {
  assert.deepEqual(resolved("interfaces/Refused"), [{ file: "interfaces/Refused.yaml", line: 1 }]);
  assert.deepEqual(resolved("interfaces/Refused.yaml"), [
    { file: "interfaces/Refused.yaml", line: 1 },
  ]);
  // The storage rule's candidate `IDoor~1.yaml` declares `idoor`, not `IDoor`.
  assert.deepEqual(resolved("interfaces/IDoor"), [{ file: "interfaces/IDoor~1.yaml", line: 1 }]);
});

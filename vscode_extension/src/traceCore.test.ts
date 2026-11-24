// The Trace Links view's pure logic (traceCore.ts). Four files under
// packages/tools/tests/fixtures/trace_panel/ are the Python side of cross-language oracles:
// the canonical proposals file the search's own dump wrote, the pending vectors
// held to `trace_records.pending`, the evidence sentences `report.md` renders, and
// the CLI's own answers. `ticked_proposals.json` is the other direction: written
// here once from a fixed list of edits, and read by the strict Python reader in
// packages/tools/tests/test_trace_panel_fixtures.py.

import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as path from "node:path";
import { test } from "node:test";

import type { LayerListing } from "./commandCore";
import { loadReferences, type Reader } from "./navigationCore";
import { PROJECT_ROOT, SCHEMAS_DIR } from "./testSupport";
import {
  ALREADY_IN_THE_LIST,
  ALREADY_LINKED,
  applyDoneMessage,
  applyNoChange,
  applyPlanOf,
  applyRowLabel,
  applySummaryOf,
  BAND_ICONS,
  bestRow,
  buildTraceApplyArgv,
  buildTraceDocumentsArgv,
  buildTraceSearchArgv,
  type CandidateRow,
  choose,
  chooseCandidates,
  chooseDocuments,
  clearDecisions,
  clearQuestion,
  componentMembers,
  confirmLink,
  DESCRIPTION_EXCERPT,
  DIRTY_REFUSAL,
  documentsInScope,
  droppedQuestion,
  droppedWithDecisions,
  EMPTY_MODES,
  type EvidenceRecord,
  evidencePhrase,
  evidenceSentence,
  evidenceText,
  excerpt,
  GROUP_ONLY_WITH_ALL,
  hiddenRows,
  holdingTraceBusy,
  ITEM_DONE,
  isDirtyRefusal,
  isDoneRequirement,
  isLinked,
  isRefusal,
  labellingArgumentOf,
  type ModePickItem,
  markStrong,
  modePickerTitle,
  modePickItems,
  modesInScope,
  NOTHING_FOUND,
  notFoundMessage,
  OTHER_ELEMENT,
  type ProposalsFile,
  parseTraceDocuments,
  pendingCount,
  pendingCounts,
  pendingInDocument,
  pendingLine,
  pendingQuestion,
  proposalsPathOf,
  readMode,
  readModes,
  readProposals,
  reject,
  removeChoice,
  requirementLocation,
  requirementState,
  rowLabel,
  rowsByStrength,
  sameAddress,
  searchNeeded,
  searchRefusedPending,
  serializeProposals,
  TRACE_APPLY_UNREADABLE_LINE,
  TRACE_APPLY_WORDING,
  TRACE_MODES,
  TRACE_NON_SPAWNING_COMMANDS,
  TRACE_VIEW_COMMANDS,
  type TraceAddress,
  type TraceDocuments,
  type TraceModes,
  type TraceNode,
  tick,
  toGoText,
  traceApplyPreview,
  traceChildren,
  traceDocuments,
  traceElementLocation,
  traceModeNodes,
  traceMoreChildren,
  traceRequirements,
  traceViewDescription,
  traceViewRoot,
  unconfirmLink,
  undoReject,
  untick,
} from "./traceCore";

const PANEL_FIXTURES = path.join(
  PROJECT_ROOT,
  "..",
  "packages",
  "tools",
  "tests",
  "fixtures",
  "trace_panel",
);
const CANONICAL = path.join(PANEL_FIXTURES, "canonical_proposals.json");
const TICKED = path.join(PANEL_FIXTURES, "ticked_proposals.json");
const MODEL_FIXTURE = path.join(PROJECT_ROOT, "src", "fixtures", "trace");

function canonicalBytes(): Buffer {
  return fs.readFileSync(CANONICAL);
}

function canonical(): ProposalsFile {
  const read = readProposals(canonicalBytes());
  assert.ok(!isRefusal(read), isRefusal(read) ? read.error : "");
  return read;
}

function entry(file: ProposalsFile, id: string) {
  const found = file.requirements.find((item) => item.id === id);
  assert.ok(found, `no entry ${id}`);
  return found;
}

function edited(result: ReturnType<typeof tick>): ProposalsFile {
  assert.ok(result.ok, result.ok ? "" : result.refusal);
  return result.file;
}

function refused(result: ReturnType<typeof tick>): string {
  assert.equal(result.ok, false, "the edit was not refused");
  return result.ok ? "" : result.refusal;
}

function readJson<T>(file: string): T {
  return JSON.parse(fs.readFileSync(file, "utf8")) as T;
}

// --- Reading and writing ---------------------------------------------------------

test("the canonical file read and serialised gives the same bytes", () => {
  assert.equal(serializeProposals(canonical()), canonicalBytes().toString("utf8"));
});

test("a tick changes that row's decision and nothing else", () => {
  const file = canonical();
  const row = entry(file, "BRK-5").candidates[0];
  const before = serializeProposals(file).split("\n");
  const after = serializeProposals(edited(tick(file, "BRK-5", row.target))).split("\n");
  assert.equal(after.length, before.length);
  const changed = before.flatMap((line, index) =>
    line === after[index] ? [] : [[line, after[index]]],
  );
  assert.deepEqual(changed, [['          "decision": null,', '          "decision": "accept",']]);
});

/** The fixed edits whose result is `ticked_proposals.json`. */
function tickedEdits(): ProposalsFile {
  let file = canonical();
  const brk5 = entry(file, "BRK-5");
  file = edited(tick(file, "BRK-5", brk5.candidates[0].target));
  file = edited(reject(file, "BRK-5", brk5.candidates[1].target));
  file = markStrong(file).file;
  file = edited(choose(file, "BRK-3", { kind: "composition", owner: null, name: "VehicleTop" }));
  file = edited(confirmLink(file, "BRK-6", 0));
  return file;
}

test("the fixed list of edits gives exactly ticked_proposals.json", () => {
  // Written once, deliberately, by: UPDATE_TRACE_FIXTURES=1 npm run test.
  const text = serializeProposals(tickedEdits());
  if (process.env.UPDATE_TRACE_FIXTURES === "1") {
    fs.writeFileSync(TICKED, text, "utf8");
  }
  assert.equal(fs.readFileSync(TICKED, "utf8"), text);
});

test("a file holding an unsafe integer is refused as unreadable", () => {
  const text = canonicalBytes()
    .toString("utf8")
    .replace(/"version": \d+/, '"version": 9007199254740993');
  assert.notEqual(text, canonicalBytes().toString("utf8"));
  const read = readProposals(Buffer.from(text, "utf8"));
  assert.ok(isRefusal(read));
  assert.match(read.error, /9007199254740993/);
});

test("a number with a fraction is refused", () => {
  const text = canonicalBytes()
    .toString("utf8")
    .replace(/"version": \d+/, '"version": 2.0');
  const read = readProposals(Buffer.from(text, "utf8"));
  assert.ok(isRefusal(read));
  assert.match(read.error, /2\.0 is not an integer/);
});

test("digits inside a string are no number", () => {
  const file = canonical();
  file.requirements[0] = { ...file.requirements[0], title: "limit 9007199254740993 and 1.5" };
  const read = readProposals(Buffer.from(serializeProposals(file), "utf8"));
  assert.ok(!isRefusal(read));
});

test("readProposals refuses each unreadable file with its reason", () => {
  const good = canonicalBytes().toString("utf8");
  const cases: Array<[string, Uint8Array, RegExp]> = [
    ["not UTF-8", Buffer.from([0x7b, 0xff, 0x7d]), /not UTF-8/],
    ["not JSON", Buffer.from("{", "utf8"), /not JSON/],
    [
      "formatVersion 2",
      Buffer.from(good.replace('"formatVersion": 1', '"formatVersion": 2'), "utf8"),
      /^\/formatVersion: must be 1$/,
    ],
    [
      "kind allocation",
      Buffer.from(good.replace('"kind": "trace"', '"kind": "allocation"'), "utf8"),
      /^\/kind: must be "trace"$/,
    ],
    [
      "a missing title",
      Buffer.from(good.replace(/"title": "[^"]*",\n/, ""), "utf8"),
      /^\/requirements\/0\/title: must be text$/,
    ],
    [
      "a decision outside the vocabulary",
      Buffer.from(good.replace('"decision": null', '"decision": "maybe"'), "utf8"),
      /decision: must be one of null, "accept", "reject"$/,
    ],
  ];
  for (const [name, bytes, reason] of cases) {
    const read = readProposals(bytes);
    assert.ok(isRefusal(read), `${name} was read`);
    assert.match(read.error, reason, name);
  }
});

test("the serialisation sorts keys by code point, as Python's sort_keys does", () => {
  const file = { ...canonical(), "\u{1F600}": 1, Ａ: 2 } as ProposalsFile;
  const text = serializeProposals(file);
  assert.ok(text.indexOf('"Ａ"') < text.indexOf('"\u{1F600}"'));
});

// --- The pending count --------------------------------------------------------------

test("pendingCount equals trace_records.pending on every vector", () => {
  const vectors = readJson<Array<{ name: string; pending: number; proposals: ProposalsFile }>>(
    path.join(PANEL_FIXTURES, "pending_vectors.json"),
  );
  assert.ok(vectors.length > 10);
  for (const vector of vectors) {
    assert.equal(pendingCount(vector.proposals), vector.pending, vector.name);
  }
  assert.equal(pendingCount(canonical()), 1, "the canonical file's one chosen entry");
});

test("the view's Apply row and the panel's pending line read one count: on every vector, both or neither", () => {
  const vectors = readJson<Array<{ name: string; pending: number; proposals: ProposalsFile }>>(
    path.join(PANEL_FIXTURES, "pending_vectors.json"),
  );
  const modes = { labelling: [], review: [] };
  for (const { name, proposals } of [...vectors, { name: "canonical", proposals: canonical() }]) {
    const root = traceViewRoot(proposals, undefined, modes, "review", 1);
    assert.equal(
      root[0]?.kind === "apply",
      pendingLine(proposals) !== undefined,
      `${name}: the Apply row exactly when the panel says something waits`,
    );
    assert.equal(root[0]?.kind === "apply", pendingCount(proposals) > 0, name);
  }
  // Ahead of an unreadable file's message, and ahead of the documents of either mode.
  const file = canonical();
  const unreadable = traceViewRoot(file, "not JSON", modes, "labelling", 1);
  assert.deepEqual(
    unreadable.slice(0, 2).map((line) => line.kind),
    ["apply", "message"],
  );
});

// --- The decisions -------------------------------------------------------------------

const RAM: TraceAddress = { kind: "memory", owner: null, name: "Ram" };

test("tick and untick: an accept with no preselection, and back", () => {
  const file = canonical();
  const target = entry(file, "BRK-5").candidates[0].target;
  const ticked = edited(tick(file, "BRK-5", target));
  const row = entry(ticked, "BRK-5").candidates[0];
  assert.equal(row.decision, "accept");
  assert.equal(row.preselected, undefined);
  assert.equal(
    entry(edited(untick(ticked, "BRK-5", target)), "BRK-5").candidates[0].decision,
    null,
  );
  assert.equal(entry(file, "BRK-5").candidates[0].decision, null, "the edits are pure");
});

test("reject and undo: from undecided or accepted, the preselection dropped", () => {
  const file = markStrong(canonical()).file;
  const item = file.requirements.find((e) => e.candidates.some((row) => row.preselected === true));
  assert.ok(item, "the canonical file has a strong row");
  const target = (item.candidates.find((row) => row.preselected === true) as CandidateRow).target;
  const rowOf = (edit: ProposalsFile) =>
    entry(edit, item.id).candidates.find((row) => sameTarget(row, target)) as CandidateRow;
  const rejected = edited(reject(file, item.id, target));
  assert.deepEqual([rowOf(rejected).decision, rowOf(rejected).preselected], ["reject", undefined]);
  assert.equal(rowOf(edited(undoReject(rejected, item.id, target))).decision, null);
  assert.match(refused(tick(rejected, item.id, target)), /undo the rejection/);
  const fromOpen = edited(
    reject(canonical(), "BRK-5", entry(canonical(), "BRK-5").candidates[0].target),
  );
  assert.equal(entry(fromOpen, "BRK-5").candidates[0].decision, "reject");
});

test("Mark strong touches only strong, undecided, open rows and adds preselected", () => {
  // The canonical file holds two strong rows; a third is made here, so one can be
  // rejected, one applied and one left to mark.
  const brk5 = entry(canonical(), "BRK-5").candidates[2].target;
  let file = withRowMarks(canonical(), "BRK-5", brk5, { band: "strong" });
  const strongRows = file.requirements.flatMap((item) =>
    item.candidates.filter((row) => row.band === "strong").map((row) => ({ id: item.id, row })),
  );
  assert.ok(strongRows.length >= 3, "the canonical file has strong rows");
  const [rejectedOne, appliedOne] = strongRows;
  file = edited(reject(file, rejectedOne.id, rejectedOne.row.target));
  file = withRowMarks(file, appliedOne.id, appliedOne.row.target, {
    decision: "accept",
    applied: true,
  });
  const { file: marked, count } = markStrong(file);
  assert.equal(count, strongRows.length - 2);
  for (const item of marked.requirements) {
    for (const row of item.candidates) {
      if (row.preselected === true) {
        assert.equal(row.band, "strong");
        assert.equal(row.decision, "accept");
      } else if (row.band !== "strong") {
        assert.equal(row.decision, null);
      }
    }
  }
  const again = markStrong(marked);
  assert.equal(again.count, 0);
  assert.equal(again.file, marked, "nothing to mark is the same file");
});

test("an untick and a re-tick of a preselected row leave a plain accept", () => {
  const marked = markStrong(canonical()).file;
  const item = marked.requirements.find((e) => e.candidates.some((r) => r.preselected === true));
  assert.ok(item);
  const target = (item.candidates.find((r) => r.preselected === true) as CandidateRow).target;
  const again = edited(tick(edited(untick(marked, item.id, target)), item.id, target));
  const row = entry(again, item.id).candidates.find(
    (r) => r.target === target || sameTarget(r, target),
  );
  assert.equal(row?.decision, "accept");
  assert.equal(row?.preselected, undefined);
});

function sameTarget(row: CandidateRow, target: TraceAddress): boolean {
  return (
    row.target.kind === target.kind &&
    row.target.owner === target.owner &&
    row.target.name === target.name
  );
}

function withRowMarks(
  file: ProposalsFile,
  id: string,
  target: TraceAddress,
  marks: Partial<CandidateRow>,
): ProposalsFile {
  return {
    ...file,
    requirements: file.requirements.map((item) =>
      item.id !== id
        ? item
        : {
            ...item,
            candidates: item.candidates.map((row) =>
              sameTarget(row, target) ? { ...row, ...marks } : row,
            ),
          },
    ),
  };
}

test("an applied or stale item refuses every edit", () => {
  const file = canonical();
  const target = entry(file, "BRK-5").candidates[0].target;
  for (const marks of [
    { decision: "accept" as const, applied: true as const },
    { decision: "reject" as const, stale: "gone" },
  ]) {
    const done = withRowMarks(file, "BRK-5", target, marks);
    for (const edit of [tick, untick, reject, undoReject]) {
      assert.equal(refused(edit(done, "BRK-5", target)), ITEM_DONE);
    }
  }
  const appliedChoice = {
    ...file,
    requirements: file.requirements.map((item) =>
      item.id === "BRK-5" ? { ...item, chosen: [{ target: RAM, applied: true as const }] } : item,
    ),
  };
  assert.equal(refused(removeChoice(appliedChoice, "BRK-5", RAM)), ITEM_DONE);
  const staleLink = {
    ...file,
    requirements: file.requirements.map((item) =>
      item.id === "BRK-6"
        ? {
            ...item,
            linked: item.linked.map((link, index) =>
              index === 0 ? { ...link, decision: "confirm" as const, stale: "gone" } : link,
            ),
          }
        : item,
    ),
  };
  assert.equal(refused(unconfirmLink(staleLink, "BRK-6", 0)), ITEM_DONE);
});

test("Clear undoes every open decision, removes open choices and counts them", () => {
  let file = tickedEdits();
  const before = pendingCount(file);
  assert.ok(before > 3);
  const target = entry(file, "BRK-5").candidates[0].target;
  file = withRowMarks(file, "BRK-5", target, { decision: "accept", applied: true });
  const { file: cleared, count } = clearDecisions(file);
  assert.equal(count, before - 1, "every open decision counts, the applied accept does not");
  assert.equal(pendingCount(cleared), 0);
  assert.equal(entry(cleared, "BRK-3").chosen.length, 0);
  assert.equal(entry(cleared, "BRK-5").candidates[0].applied, true, "an applied item stays");
  assert.equal(entry(cleared, "BRK-6").linked[0].decision, null);
  assert.equal(clearQuestion(count), `Clear ${count} decisions?`);
  assert.equal(clearQuestion(1), "Clear 1 decision?");
});

test("the pair twice is refused both ways, and a linked target too", () => {
  const file = canonical();
  const brk5 = entry(file, "BRK-5");
  assert.equal(refused(choose(file, "BRK-5", brk5.candidates[0].target)), ALREADY_IN_THE_LIST);
  assert.equal(refused(choose(file, "BRK-5", RAM)), ALREADY_IN_THE_LIST, "already chosen");
  const linkedTarget = entry(file, "BRK-6").linked[0].target;
  assert.equal(refused(choose(file, "BRK-6", linkedTarget)), ALREADY_LINKED);
  // A row whose pair is chosen (only a hand edit makes one) refuses a tick.
  const both = {
    ...file,
    requirements: file.requirements.map((item) =>
      item.id === "BRK-5" ? { ...item, chosen: [{ target: brk5.candidates[0].target }] } : item,
    ),
  };
  assert.equal(refused(tick(both, "BRK-5", brk5.candidates[0].target)), ALREADY_IN_THE_LIST);
});

test("choose appends, removeChoice removes, confirm and unconfirm switch the slot", () => {
  const file = canonical();
  const top: TraceAddress = { kind: "composition", owner: null, name: "VehicleTop" };
  const chosen = edited(choose(file, "BRK-3", top));
  assert.deepEqual(entry(chosen, "BRK-3").chosen, [{ target: top }]);
  assert.deepEqual(entry(edited(removeChoice(chosen, "BRK-3", top)), "BRK-3").chosen, []);
  const confirmed = edited(confirmLink(file, "BRK-6", 1));
  assert.equal(entry(confirmed, "BRK-6").linked[1].decision, "confirm");
  assert.equal(
    entry(edited(unconfirmLink(confirmed, "BRK-6", 1)), "BRK-6").linked[1].decision,
    null,
  );
  assert.match(
    refused(confirmLink(file, "BRK-1", 0)),
    /not in the proposals file/,
    "a confirmed link",
  );
  assert.match(refused(tick(file, "NOPE", RAM)), /not in the proposals file/);
});

// --- The tree -----------------------------------------------------------------------------

test("evidenceSentence and evidenceText equal report.md's sentences", () => {
  const cases = readJson<Array<{ records: EvidenceRecord[]; text: string }>>(
    path.join(PANEL_FIXTURES, "evidence_sentences.json"),
  );
  const kinds = new Set<string>();
  for (const { records, text } of cases) {
    assert.equal(evidenceText(records), text);
    if (records.length === 1) {
      assert.equal(evidenceSentence(records[0]), text);
      kinds.add(records[0].kind);
    }
  }
  assert.deepEqual([...kinds].sort(), ["name", "neighbours", "similar", "text"]);
});

// --- The tree by mode ----------------------------------------------------------------

const MODES: TraceModes = { labelling: ["System Spec"], review: ["Body Spec", "Brake Spec", null] };

test("the mode nodes: no documents chosen, then documents and requirements to go", () => {
  const file = canonical();
  const empty = traceModeNodes(file, EMPTY_MODES, 1);
  assert.deepEqual(
    empty.map((node) => [node.label, node.description, node.collapsible, node.contextValue]),
    [
      ["Labelling", "no documents chosen", "none", "trace.mode.labelling"],
      ["Review", "no documents chosen", "none", "trace.mode.review"],
    ],
  );
  const [labelling, review] = traceModeNodes(file, MODES, 1);
  // System Spec: 8 requirements, SYS-1 to SYS-5 linked, so 3 to go.
  assert.equal(labelling.description, "1 document · 3 of 8 to go");
  assert.match(review.description, /^3 documents · \d+ of 13 to go$/);
  assert.deepEqual([labelling.id, review.id], ["trace:1/labelling", "trace:1/review"]);
  assert.ok(
    [labelling, review].every((node) => node.collapsible === "none"),
    "the work unfolds in the view",
  );
  assert.equal(traceViewDescription(MODES, "labelling"), "Labelling · System Spec");
  assert.equal(
    traceViewDescription(MODES, "review"),
    "Review · Body Spec, Brake Spec, No document",
  );
  assert.equal(traceViewDescription(EMPTY_MODES, "review"), "Review · no documents chosen");
  assert.deepEqual(
    [readMode("labelling"), readMode(undefined), readMode("x")],
    ["labelling", "review", "review"],
  );
});

test("the documents of a mode: only the chosen ones, in the mode's order", () => {
  const file = canonical();
  const nodes = traceDocuments(file, MODES, "review", 2);
  assert.deepEqual(
    nodes.map((node) => [node.label, node.description]),
    [
      ["Body Spec", "3 requirements · 1 linked"],
      ["Brake Spec", "8 requirements · 4 linked"],
      ["No document", "2 requirements · none linked"],
    ],
  );
  assert.deepEqual(
    nodes.map((node) => node.id),
    ["trace:2/review/doc:=Body%20Spec", "trace:2/review/doc:=Brake%20Spec", "trace:2/review/doc:-"],
  );
  const missing = traceDocuments(file, { labelling: ["Elsewhere"], review: [] }, "labelling", 2);
  assert.equal(missing[0].description, "not in the proposals file — search again");
  assert.equal(traceDocuments(file, MODES, "labelling", 2)[0].collapsible, "expanded");
});

test("the requirement nodes: id and title, the state, never low accuracy", () => {
  const file = canonical();
  const brake = traceRequirements(file, "review", "Brake Spec", 1);
  const brk7 = brake.find((node) => node.requirement === "BRK-7") as TraceNode;
  assert.equal(brk7.label, "BRK-7  Pad temperature above 600 °C");
  assert.equal(
    brk7.description,
    excerpt(entry(file, "BRK-7").description),
    "no state, the text's first words",
  );
  const brk1 = brake.find((node) => node.requirement === "BRK-1") as TraceNode;
  assert.ok(brk1.description.startsWith("linked · "));
  assert.equal(brk1.contextValue, "trace.requirement.linked");
  const none = traceRequirements(file, "review", null, 1);
  assert.equal(
    (none.find((node) => node.requirement === "N-2") as TraceNode).description.split(" · ")[0],
    NOTHING_FOUND,
  );
  assert.ok(none.every((node) => !node.description.includes("low accuracy")));
  const system = traceRequirements(file, "labelling", "System Spec", 1);
  assert.match(system[0].tooltip, /Label first: /);
  assert.equal(system.length, 8, "Labelling lists every requirement of its documents");
});

test("requirementState and the counter: linked, ticked, every row rejected, to go", () => {
  let file = canonical();
  const brk8 = entry(file, "BRK-8");
  assert.equal(requirementState(brk8), undefined);
  assert.equal(
    requirementState(entry(file, "BRK-5")),
    "ticked",
    "the canonical file chooses an element for BRK-5",
  );
  file = edited(tick(file, "BRK-8", brk8.candidates[0].target));
  assert.equal(requirementState(entry(file, "BRK-8")), "ticked");
  const brk5 = entry(file, "BRK-5");
  let brk7 = canonical();
  for (const row of entry(brk7, "BRK-7").candidates) {
    brk7 = edited(reject(brk7, "BRK-7", row.target));
  }
  assert.equal(requirementState(entry(brk7, "BRK-7")), "rejected");
  assert.ok(isDoneRequirement(entry(brk7, "BRK-7")));
  const applied = withRowMarks(canonical(), "BRK-5", brk5.candidates[1].target, {
    decision: "accept",
    applied: true,
  });
  assert.ok(isLinked(entry(applied, "BRK-5")), "an applied accept links the requirement");
  assert.equal(toGoText([entry(file, "BRK-8"), entry(file, "BRK-7")]), "1 of 2 to go");
});

test("rowsByStrength and bestRow: the strongest band, then the higher score, then the earlier place", () => {
  const base = canonical().requirements[0];
  const row = (place: number, band: CandidateRow["band"], n: number): CandidateRow => ({
    ...base.candidates[0],
    place,
    band,
    score: { n, d: 1000000 },
  });
  const pick = (rows: CandidateRow[]) => bestRow({ ...base, candidates: rows });
  assert.equal(pick([row(1, "medium", 9), row(2, "strong", 1)])?.place, 2);
  assert.equal(pick([row(1, "weak", 1), row(2, "weak", 5)])?.place, 2);
  assert.equal(pick([row(2, "weak", 5), row(1, "weak", 5)])?.place, 1);
  assert.equal(pick([]), undefined);
  assert.deepEqual(
    rowsByStrength([row(1, "weak", 9), row(2, "strong", 1), row(3, "medium", 2)]).map(
      (r) => r.place,
    ),
    [2, 3, 1],
  );
});

test("an unlinked requirement: a fixed window of three rows, More candidates (N) in place, Any element…", () => {
  const file = canonical();
  const brk8 = entry(file, "BRK-8");
  const ordered = rowsByStrength(brk8.candidates);
  const children = traceChildren(brk8, "review", 1);
  assert.deepEqual(
    children.map((node) => node.kind),
    ["row", "row", "row", "more"],
  );
  const more = children.at(-1) as TraceNode;
  assert.deepEqual(
    [more.label, more.contextValue, more.collapsible, more.icon],
    ["More candidates (2)", "trace.more", "collapsed", undefined],
  );
  const inside = traceMoreChildren(brk8, "review", 1);
  assert.deepEqual(
    inside.map((node) => node.kind),
    ["row", "row", "other"],
  );
  const other = inside.at(-1) as TraceNode;
  assert.deepEqual(
    [other.label, other.contextValue, other.icon?.id],
    [OTHER_ELEMENT, "trace.other", "search"],
  );
  // A tick or a reject moves no row in or out of the window.
  let ticked = edited(tick(file, "BRK-8", ordered[0].target));
  ticked = edited(reject(ticked, "BRK-8", ordered[1].target));
  const after = traceChildren(entry(ticked, "BRK-8"), "review", 1);
  assert.deepEqual(
    after.map((node) => node.id),
    children.map((node) => node.id),
  );
  assert.deepEqual(
    after.slice(0, 3).map((node) => node.label),
    ordered.slice(0, 3).map((row) => rowLabel(row.band, row.target).label),
  );
  // A decision among the hidden rows opens More candidates, so nothing decided is out of sight.
  const hiddenTick = edited(tick(file, "BRK-8", ordered[4].target));
  assert.equal(
    (traceChildren(entry(hiddenTick, "BRK-8"), "review", 1).at(-1) as TraceNode).collapsible,
    "expanded",
  );
  assert.equal(hiddenRows(brk8).length, 2);
  // With nothing past the window, the requirement ends in Any element… itself.
  const n1 = traceChildren(entry(file, "N-1"), "review", 1);
  assert.deepEqual(
    n1.map((node) => node.kind),
    ["row", "row", "row", "other"],
  );
});

test("the requirement's text: the whole of it in the tooltip, its first words on the row", () => {
  const file = canonical();
  const brk8 = entry(file, "BRK-8");
  const node = traceRequirements(file, "review", "Brake Spec", 1).find(
    (candidate) => candidate.requirement === "BRK-8",
  ) as TraceNode;
  assert.ok(brk8.description !== "", "the canonical file carries the description");
  assert.ok(node.tooltip.includes(`**BRK-8@${brk8.version}** — Brake Spec`));
  assert.ok(node.tooltip.includes(brk8.description));
  assert.equal(node.description, excerpt(brk8.description));
  assert.equal(excerpt("  a\n  b  "), "a b");
  const long = "word ".repeat(40);
  assert.equal(excerpt(long).length, DESCRIPTION_EXCERPT);
  assert.ok(excerpt(long).endsWith("…"));
});

test("a linked requirement: its links and decisions, no undecided candidate, no Other element…", () => {
  const file = canonical();
  const brk2 = traceChildren(entry(file, "BRK-2"), "review", 1);
  assert.deepEqual(
    brk2.map((node) => node.kind),
    ["linked"],
  );
  const chose = edited(
    choose(file, "BRK-3", { kind: "composition", owner: null, name: "VehicleTop" }),
  );
  const brk3 = traceChildren(entry(chose, "BRK-3"), "review", 1);
  assert.deepEqual(
    brk3.map((node) => node.kind),
    ["chosen", "linked"],
    "a pending choice of a linked requirement stays in sight",
  );
  assert.equal(
    markStrong(
      withRowMarks(file, "BRK-2", entry(file, "BRK-2").candidates[0].target, {
        band: "strong",
      }),
    ).count,
    3,
    "Mark strong leaves a linked requirement's rows alone",
  );
});

test("the looks: Labelling kind:name, Review band·kind:name and its coloured icon, both the reason", () => {
  const file = canonical();
  const sys8 = entry(file, "SYS-8");
  const row = rowsByStrength(sys8.candidates)[0];
  const labelling = traceChildren(sys8, "labelling", 1).filter((node) => node.kind === "row");
  assert.ok(labelling.every((node) => node.icon === undefined));
  assert.deepEqual(
    [labelling[0].label, labelling[0].highlights, labelling[0].description],
    [
      rowLabel(undefined, row.target).label,
      [[0, row.target.kind.length]],
      evidencePhrase(row.evidence),
    ],
  );
  const review = traceChildren(sys8, "review", 1).filter((node) => node.kind === "row");
  assert.deepEqual(
    [review[0].label, review[0].description, review[0].icon],
    [rowLabel(row.band, row.target).label, evidencePhrase(row.evidence), BAND_ICONS[row.band]],
  );
  assert.match(review[0].tooltip, /Group: \w+ \w+, place \d/);
});

test("rowLabel: no spaces, · after the band, : after the kind, the two words highlighted", () => {
  const port = { kind: "port", owner: "BrakeController", name: "pBrakeCmd" } as TraceAddress;
  assert.deepEqual(rowLabel("strong", port), {
    label: "strong·port:BrakeController.pBrakeCmd",
    highlights: [
      [0, 6],
      [7, 11],
    ],
  });
  assert.deepEqual(rowLabel("linked", port), {
    label: "linked·port:BrakeController.pBrakeCmd",
    highlights: [
      [0, 6],
      [7, 11],
    ],
  });
  assert.deepEqual(
    rowLabel(undefined, { kind: "swc", owner: null, name: "Brake" } as TraceAddress),
    {
      label: "swc:Brake",
      highlights: [[0, 3]],
    },
  );
  assert.deepEqual(
    Object.values(BAND_ICONS).map((icon) => [icon.id, icon.color]),
    [
      ["circle-large-filled", "charts.green"],
      ["circle-large-filled", "charts.yellow"],
      ["circle-large-filled", "descriptionForeground"],
    ],
  );
});

test("applyRowLabel: the ticked links, else the rejections, else nothing", () => {
  const file = canonical();
  assert.equal(applyRowLabel(undefined), undefined);
  const ordered = rowsByStrength(entry(file, "BRK-8").candidates);
  const one = edited(tick(file, "BRK-8", ordered[0].target));
  const ticked = pendingCounts(one).ticked;
  assert.equal(applyRowLabel(one), `Apply ${ticked} ticked link${ticked === 1 ? "" : "s"}…`);
});

test("evidencePhrase: names, like, similar to, terms", () => {
  assert.equal(evidencePhrase([]), "");
  assert.equal(evidencePhrase([{ kind: "name", phrase: "pTemp" }]), "names pTemp");
  assert.equal(
    evidencePhrase([{ kind: "neighbours", share: 1, seeded: 1, of: 1, examples: ["SWR-BRK-001"] }]),
    "like SWR-BRK-001",
  );
  assert.equal(
    evidencePhrase([{ kind: "neighbours", share: 1, seeded: 3, of: 3, examples: [] }]),
    "like 3 linked requirements",
  );
  assert.equal(
    evidencePhrase([
      { kind: "similar", score: { n: 1, d: 2 }, count: 2, examples: ["A-1", "A-2"] },
    ]),
    "similar to A-1",
  );
  assert.equal(
    evidencePhrase([{ kind: "text", terms: ["brake", "pad", "wear", "check"] }]),
    "terms brake, pad, wear",
  );
});

test("every state of a candidate row's mark table, its precedence first", () => {
  const file = canonical();
  const target = rowsByStrength(entry(file, "BRK-5").candidates)[0].target;
  const look = (marks: Partial<CandidateRow>) =>
    traceChildren(entry(withRowMarks(file, "BRK-5", target, marks), "BRK-5"), "review", 1).find(
      (node) => node.item?.slot === "candidates" && sameAddress(node.item.target, target),
    ) as TraceNode;
  const band = entry(file, "BRK-5").candidates.find((row) => sameAddress(row.target, target))?.band;
  const open = look({ decision: null });
  assert.deepEqual([open.checkbox, open.contextValue], ["unchecked", "trace.row.open"]);
  assert.ok(open.label.startsWith(`${band}·`));
  const accepted = look({ decision: "accept", preselected: true });
  assert.deepEqual([accepted.checkbox, accepted.contextValue], ["checked", "trace.row.open"]);
  assert.match(accepted.tooltip, new RegExp(`— ${band}, preselected`));
  const rejected = look({ decision: "reject" });
  assert.deepEqual(
    [rejected.checkbox, rejected.description, rejected.icon?.id, rejected.contextValue],
    [undefined, "rejected", "circle-slash", "trace.row.rejected"],
  );
  const recorded = look({ decision: "reject", applied: true });
  assert.deepEqual(
    [recorded.checkbox, recorded.description, recorded.contextValue],
    [undefined, "rejected, recorded", "trace.done"],
  );
  const stale = look({ decision: "reject", stale: "SR-1 changed" });
  assert.deepEqual(
    [stale.checkbox, stale.description, stale.icon?.id, stale.contextValue],
    [undefined, "stale: SR-1 changed", "warning", "trace.done"],
  );
});

test("the ids: stable across a tick, new with a generation, unique over the tree, a / escaped", () => {
  const file = canonical();
  const before = traceChildren(entry(file, "BRK-5"), "review", 1).map((node) => node.id);
  const target = rowsByStrength(entry(file, "BRK-5").candidates)[0].target;
  const after = traceChildren(entry(edited(tick(file, "BRK-5", target)), "BRK-5"), "review", 1).map(
    (node) => node.id,
  );
  assert.deepEqual(
    before.filter((id) => !after.includes(id)),
    [],
    "the ticked row moves up and keeps its id; the next undecided one joins",
  );
  const reloaded = traceChildren(entry(file, "BRK-5"), "review", 2).map((node) => node.id);
  assert.ok(
    reloaded.every((id, index) => id !== before[index]),
    "a reload from disk is a new tree (D59)",
  );
  const slashed = { labelling: [], review: ["A/B"] };
  assert.equal(traceDocuments(file, slashed, "review", 1)[0].id, "trace:1/review/doc:=A%2FB");
  const ids = [
    ...traceModeNodes(file, MODES, 1).map((node) => node.id),
    ...TRACE_MODES.flatMap((mode) =>
      traceDocuments(file, MODES, mode, 1).flatMap((document) => [
        document.id,
        ...traceRequirements(file, mode, document.document ?? null, 1).flatMap((node) => [
          node.id,
          ...traceChildren(entry(file, node.requirement as string), mode, 1).map(
            (child) => child.id,
          ),
        ]),
      ]),
    ),
  ];
  assert.equal(new Set(ids).size, ids.length, "every id of the tree is unique");
});

test("the pending line counts what Apply will do", () => {
  const file = tickedEdits();
  const { ticked, rejected } = pendingCounts(file);
  assert.equal(ticked + rejected, pendingCount(file));
  assert.equal(rejected, 1);
  assert.equal(pendingLine(file), `${ticked} ticked, 1 rejected, not yet written`);
  assert.equal(
    pendingLine(canonical()),
    "1 ticked, not yet written",
    "the canonical file's one choice",
  );
  assert.equal(pendingLine(clearDecisions(canonical()).file), undefined);
  assert.equal(pendingLine(undefined), undefined);
});

test("the demo's case: a requirement with three links and five rows shows its three links", () => {
  const file = canonical();
  const linked = entry(file, "BRK-2");
  const three = {
    ...linked,
    linked: [0, 1, 2].map((index) => ({ ...linked.linked[0], member: `BRK-2@${index + 1}` })),
  };
  const children = traceChildren(three, "review", 3);
  assert.deepEqual(
    children.map((node) => node.kind),
    ["linked", "linked", "linked"],
  );
  assert.equal(new Set(children.map((node) => node.id)).size, 3);
});

test("a confirm Apply carried out holds: the requirement is linked and its row a link", () => {
  const file = canonical();
  const linked = entry(file, "BRK-2");
  const awaiting = { ...linked.linked[0], confirmed: false, member: `${linked.id}@0` };
  const before = { ...linked, linked: [{ ...awaiting, decision: "confirm" as const }] };
  assert.equal(requirementState(before), "ticked");
  const after = {
    ...linked,
    linked: [{ ...awaiting, decision: "confirm" as const, applied: true as const }],
  };
  assert.equal(requirementState(after), "linked");
  const [row] = traceChildren(after, "review", 1);
  assert.deepEqual(
    [row.kind, row.label, row.description, row.icon?.id],
    ["linked", rowLabel("linked", awaiting.target).label, "", "pass-filled"],
  );
  assert.ok(isDoneRequirement(after));
});

test("chosen and awaiting rows: chosen·kind:name and awaiting·kind:name, the words highlighted", () => {
  const file = canonical();
  const withChoice = file.requirements.find(
    (item) => item.chosen.length > 0,
  ) as (typeof file.requirements)[number];
  const chosen = traceChildren(withChoice, "review", 1).find(
    (node) => node.kind === "chosen",
  ) as TraceNode;
  const target = withChoice.chosen[0].target;
  assert.deepEqual(
    [chosen.label, chosen.highlights, chosen.description],
    [rowLabel("chosen", target).label, rowLabel("chosen", target).highlights, ""],
  );
  assert.ok(chosen.label.startsWith("chosen·"));
  const linked = entry(file, "BRK-2");
  const link = { ...linked.linked[0], confirmed: false };
  const awaiting = traceChildren({ ...linked, linked: [link] }, "review", 1).find(
    (node) => node.kind === "awaiting",
  ) as TraceNode;
  assert.deepEqual(
    [awaiting.label, awaiting.highlights],
    [rowLabel("awaiting", link.target).label, rowLabel("awaiting", link.target).highlights],
  );
});

test("a linked row: linked·kind:name, its check, the member only when it does not repeat the requirement", () => {
  const file = canonical();
  const linked = entry(file, "BRK-2");
  const link = linked.linked[0];
  const own = `${linked.id}@${linked.version}`;
  const rows = traceChildren(
    {
      ...linked,
      linked: [
        { ...link, member: own },
        { ...link, member: "BRK-2" },
      ],
    },
    "review",
    1,
  );
  assert.deepEqual(
    rows.map((node) => [node.label, node.highlights, node.description, node.icon?.id]),
    [
      [
        rowLabel("linked", link.target).label,
        rowLabel("linked", link.target).highlights,
        "",
        "pass-filled",
      ],
      [
        rowLabel("linked", link.target).label,
        rowLabel("linked", link.target).highlights,
        "BRK-2",
        "pass-filled",
      ],
    ],
  );
});

// --- The modes ---------------------------------------------------------------------------------

function cliDocumentsAnswer(): TraceDocuments {
  return parseTraceDocuments(answers().documents.stdout);
}

test("readModes keeps two lists of documents, a document in one mode only", () => {
  assert.deepEqual(readModes(undefined), EMPTY_MODES);
  assert.deepEqual(readModes({ labelling: ["A", 3, null], review: ["A", "B"] }), {
    labelling: ["A", null],
    review: ["B"],
  });
});

test("the mode picker: documents without a link first, ticked by the mode, the other mode named", () => {
  const answer = cliDocumentsAnswer();
  const items = modePickItems(
    answer,
    { labelling: [], review: [answer.documents[0].document] },
    "labelling",
  );
  const linkedFirst = items.findIndex((item) => !item.description.includes("none linked"));
  assert.ok(items.slice(linkedFirst).every((item) => !item.description.includes("none linked")));
  const moved = items.find(
    (item) => item.document === answer.documents[0].document,
  ) as ModePickItem;
  assert.match(moved.description, / · in Review$/);
  assert.equal(moved.picked, false);
  const group = items.find((item) => item.document === null);
  if (group !== undefined) {
    assert.equal(group.detail, GROUP_ONLY_WITH_ALL);
  }
  assert.equal(modePickerTitle("review"), "Review — which documents?");
});

test("chooseDocuments: a set per mode, a moved document leaves the other, the group only with all", () => {
  const answer = cliDocumentsAnswer();
  const scope = documentsInScope(answer).map((row) => row.document);
  const named = scope.filter((document): document is string => document !== null);
  const start: TraceModes = { labelling: [], review: [named[0], named[1]] };
  const { modes } = chooseDocuments(start, "labelling", [named[1]], answer);
  assert.deepEqual(modes, { labelling: [named[1]], review: [named[0]] });
  assert.deepEqual(labellingArgumentOf(modes), [named[1]]);
  if (scope.includes(null)) {
    const partial = chooseDocuments(start, "labelling", [null, named[0]], answer);
    assert.deepEqual(partial, {
      modes: { labelling: [named[0]], review: [named[1]] },
      leftOut: true,
    });
    const all = chooseDocuments(start, "labelling", scope, answer);
    assert.deepEqual(all.modes.review, []);
    assert.equal(labellingArgumentOf(all.modes), "all");
    // A Review choice out of an "all" Labelling takes the group with it.
    const reviewed = chooseDocuments(all.modes, "review", [named[0]], answer).modes;
    assert.deepEqual(reviewed, {
      review: [named[0]],
      labelling: scope.filter((document) => document !== null && document !== named[0]),
    });
    assert.notEqual(labellingArgumentOf(reviewed), "all");
    // So does a document new in scope that Labelling does not hold.
    const without = { labelling: scope.filter((document) => document !== named[1]), review: [] };
    assert.equal(modesInScope(without, answer).labelling.includes(null), false);
    assert.deepEqual(modesInScope(all.modes, answer), all.modes);
  }
  assert.deepEqual(modesInScope({ labelling: ["Gone"], review: [named[0]] }, answer), {
    labelling: [],
    review: [named[0]],
  });
});

test("droppedWithDecisions reads the remembered modes: a document out of scope with a decision is dropped", () => {
  const file = canonical();
  const withChoice = file.requirements.find(
    (item) => item.chosen.length > 0,
  ) as (typeof file.requirements)[number];
  const document = withChoice.document;
  const remembered: TraceModes = { labelling: [], review: [document, "Elsewhere"] };
  const chosen: TraceModes = { labelling: [], review: ["Elsewhere"] };
  assert.deepEqual(
    droppedWithDecisions(file, remembered, chosen).map((item) => item.document),
    [document],
  );
});

test("searchNeeded: no file, a pass that differs, a document missing; not for a review set inside it", () => {
  const file = canonical();
  const answer: TraceDocuments = {
    proposals: null,
    documents: ["Body Spec", "Brake Spec", "System Spec", null].map((document) => ({
      document,
      inScope: 3,
      stubs: 0,
      linked: 0,
      awaitingOnly: 0,
      withoutLink: 3,
      rejectedEarlier: 0,
      pass: "review" as const,
    })),
  };
  assert.equal(searchNeeded(undefined, EMPTY_MODES, answer), false);
  assert.equal(searchNeeded(undefined, MODES, answer), true);
  assert.equal(searchNeeded(file, MODES, answer), false);
  assert.equal(searchNeeded(file, { labelling: [], review: ["Brake Spec"] }, answer), false);
  assert.equal(searchNeeded(file, { labelling: ["Brake Spec"], review: [] }, answer), true);
  assert.equal(searchNeeded(file, { labelling: [], review: ["System Spec"] }, answer), true);
  const without = {
    ...file,
    requirements: file.requirements.filter((item) => item.document !== "Body Spec"),
  };
  assert.equal(searchNeeded(without, { labelling: [], review: ["Body Spec"] }, answer), true);
});

test("droppedWithDecisions: a document leaving both modes with unapplied decisions", () => {
  const file = tickedEdits();
  assert.deepEqual(droppedWithDecisions(file, MODES, MODES), []);
  const dropped = droppedWithDecisions(file, MODES, {
    labelling: ["System Spec"],
    review: ["Body Spec"],
  });
  assert.deepEqual(
    dropped.map((item) => item.document),
    ["Brake Spec"],
  );
  assert.equal(dropped[0].count, pendingInDocument(file, "Brake Spec"));
  assert.equal(
    droppedQuestion(dropped),
    `Brake Spec holds ${dropped[0].count} decisions not applied.`,
  );
  const cleared = clearDecisions(file, ["Brake Spec"]);
  assert.equal(pendingInDocument(cleared.file, "Brake Spec"), 0);
  assert.equal(cleared.count, dropped[0].count);
});

// --- The CLI's answers -----------------------------------------------------------------------

interface CliAnswers {
  cwd: string;
  documents: { stdout: string };
  search: { stdout: string };
  applyDryRun: { stdout: string };
  applyApplied: { stdout: string };
  applyNoChange: { stdout: string };
}

function answers(): CliAnswers {
  return readJson<CliAnswers>(path.join(PANEL_FIXTURES, "cli_answers.json"));
}

test("the argv: --documents, the search and Apply, each value one token", () => {
  assert.deepEqual(buildTraceDocumentsArgv("swcraft.yaml"), [
    "experiment",
    "trace",
    "--documents",
    "--project",
    "swcraft.yaml",
    "--format",
    "json",
  ]);
  assert.deepEqual(
    buildTraceDocumentsArgv("swcraft.yaml", " -odd.yaml ").at(5),
    "--config=-odd.yaml",
  );
  assert.deepEqual(buildTraceSearchArgv("swcraft.yaml", { discardPending: false }), [
    "experiment",
    "trace",
    "--project",
    "swcraft.yaml",
  ]);
  assert.deepEqual(
    buildTraceSearchArgv("swcraft.yaml", { config: "c.yaml", discardPending: true }).slice(4),
    ["--config=c.yaml", "--discard-pending"],
  );
  // The picker's choice: one --labelling= token, JSON, a document with a space or a quote included.
  assert.deepEqual(
    buildTraceSearchArgv("swcraft.yaml", {
      labelling: ["Brake Spec", 'a "b"'],
      discardPending: false,
    }).slice(4),
    ['--labelling=["Brake Spec","a \\"b\\""]'],
  );
  assert.deepEqual(
    buildTraceSearchArgv("swcraft.yaml", { labelling: "all", discardPending: false }).slice(4),
    ['--labelling="all"'],
  );
  assert.deepEqual(
    buildTraceSearchArgv("swcraft.yaml", { labelling: [], discardPending: false }).slice(4),
    ["--labelling=[]"],
  );
  const file = path.resolve("/work/project/-dist/proposals.json");
  assert.deepEqual(buildTraceApplyArgv("swcraft.yaml", file, { dryRun: true }), [
    "experiment",
    "trace",
    `--apply=${file}`,
    "--project",
    "swcraft.yaml",
    "--dry-run",
  ]);
  const digest = `sha256:${"a".repeat(64)}`;
  assert.equal(
    buildTraceApplyArgv("swcraft.yaml", file, { dryRun: false, expectPlan: digest }).at(-1),
    `--expect-plan=${digest}`,
  );
  assert.throws(() => buildTraceApplyArgv("swcraft.yaml", "dist/proposals.json", { dryRun: true }));
  // The imports' report option: an absolute .md file, last, one token.
  const report = path.resolve("/tmp/swcraft-import-x/import-report.md");
  assert.equal(
    buildTraceApplyArgv("swcraft.yaml", file, { dryRun: true, report }).at(-1),
    `--report=${report}`,
  );
  assert.deepEqual(
    buildTraceApplyArgv("swcraft.yaml", file, { dryRun: false, expectPlan: digest, report }).slice(
      -2,
    ),
    [`--expect-plan=${digest}`, `--report=${report}`],
  );
  assert.throws(() =>
    buildTraceApplyArgv("swcraft.yaml", file, { dryRun: true, report: path.resolve("/tmp/r.txt") }),
  );
});

test("parseTraceDocuments reads the CLI's answer and refuses another version by name", () => {
  const parsed = parseTraceDocuments(answers().documents.stdout);
  assert.equal(parsed.proposals, "dist/experiments/trace/proposals.json");
  assert.equal(parsed.documents.at(-1)?.document, null);
  const other = answers().documents.stdout.replace('"schemaVersion": 1', '"schemaVersion": 2');
  assert.throws(() => parseTraceDocuments(other), /schemaVersion 2/);
  assert.throws(() => parseTraceDocuments("{"), /JSON/);
  assert.throws(() => parseTraceDocuments('{"kind": "x"}'), /not a trace-documents/);
});

function documentsAnswer(
  rows: Array<[string | null, "review" | "labelling" | null]>,
): TraceDocuments {
  return {
    proposals: null,
    documents: rows.map(([document, pass]) => ({
      document,
      inScope: pass === null ? 0 : 4,
      stubs: 0,
      linked: document === "Z" ? 0 : 2,
      awaitingOnly: document === "A" ? 1 : 0,
      withoutLink: 1,
      rejectedEarlier: 0,
      pass,
    })),
  };
}

test("the mode picker over a synthetic answer: out-of-scope rows left out, unlinked first", () => {
  const items = modePickItems(
    documentsAnswer([
      ["A", "review"],
      ["Out", null],
      ["Z", "review"],
    ]),
    EMPTY_MODES,
    "review",
  );
  assert.deepEqual(
    items.map((item) => [item.label, item.description]),
    [
      ["Z", "4 requirements · none linked"],
      ["A", "4 requirements · 2 linked, 1 awaiting confirmation"],
    ],
  );
});

test("proposalsPathOf: the last line wins, CRLF trimmed, relative resolved", () => {
  const { cwd, search } = answers();
  assert.equal(
    proposalsPathOf(search.stdout, cwd),
    path.resolve(cwd, "/work/project/dist/experiments/trace/proposals.json"),
  );
  assert.equal(
    proposalsPathOf("Generated proposals: a/p.json\r\nGenerated proposals: b/p.json\r\n", "/w"),
    path.resolve("/w", "b/p.json"),
  );
  assert.equal(proposalsPathOf("Generated report: x\n", "/w"), undefined);
});

test("applySummaryOf, applyPlanOf and applyNoChange on the CLI's own reports", () => {
  const { applyDryRun, applyApplied, applyNoChange: nothing } = answers();
  assert.deepEqual(applySummaryOf(applyDryRun.stdout), {
    written: 1,
    confirmed: 1,
    rejected: 1,
    alreadyDone: 0,
    stale: 0,
    notWritten: 0,
    undecided: 0,
    files: 1,
  });
  assert.match(applyPlanOf(applyDryRun.stdout) ?? "", /^sha256:[0-9a-f]{64}$/);
  assert.equal(applyNoChange(applyDryRun.stdout), false);
  assert.equal(applyNoChange(applyApplied.stdout), false);
  assert.equal(applyNoChange(nothing.stdout), true);
  assert.equal(applySummaryOf(nothing.stdout)?.alreadyDone, 3);
  const crlf = applyDryRun.stdout.replace(/\n/g, "\r\n");
  assert.deepEqual(applySummaryOf(crlf), applySummaryOf(applyDryRun.stdout));
  assert.equal(applyPlanOf(crlf), applyPlanOf(applyDryRun.stdout));
  assert.equal(applyNoChange(nothing.stdout.replace(/\n/g, "\r\n")), true);
  const summary = applyDryRun.stdout.split("\n").find((line) => line.startsWith("Summary: "));
  assert.equal(applySummaryOf(`${applyDryRun.stdout}${summary}\n`), undefined, "two lines");
  const plan = applyDryRun.stdout.split("\n").find((line) => line.startsWith("Plan: "));
  assert.equal(applyPlanOf(`${applyDryRun.stdout}${plan}\n`), undefined, "two plans");
  assert.equal(applySummaryOf(""), undefined);
  assert.equal(applyPlanOf(""), undefined);
});

const ZERO = {
  written: 0,
  confirmed: 0,
  rejected: 0,
  alreadyDone: 0,
  stale: 0,
  notWritten: 0,
  undecided: 0,
  files: 0,
};

test("applyDoneMessage: the channel's line after an applied run", () => {
  const applied = applySummaryOf(answers().applyApplied.stdout);
  assert.ok(applied);
  assert.equal(
    applyDoneMessage(applied),
    "SWCraft: wrote 2 links into 1 file; recorded 1 rejection.",
  );
  assert.equal(applyDoneMessage(ZERO), "SWCraft: nothing was applied.");
});

/** A CLI run as `spawnCli()` answers it, with the Markdown report file it wrote. */
function applyRun(exitCode: number | null, stdout: string, stderr = "", reportMarkdown?: string) {
  return { exitCode, stdout, stderr, spawnFailed: false, reportMarkdown };
}

test("traceApplyPreview: Write under a plan that writes something, none under No change., a refusal or an unreadable run", () => {
  const { applyDryRun, applyNoChange: nothing } = answers();
  const write = traceApplyPreview(applyRun(0, applyDryRun.stdout, "", "# m\n"), "/s");
  assert.equal(write.kind, "write");
  assert.equal(write.kind === "write" ? write.plan : "", applyPlanOf(applyDryRun.stdout));
  // The imports' dry-run report: the CLI's Markdown form rendered, no line of the extension's.
  assert.deepEqual(write.report, { cliText: applyDryRun.stdout, notes: [], markdown: "# m\n" });
  const noChange = traceApplyPreview(applyRun(0, nothing.stdout), "/s");
  assert.deepEqual(noChange, { kind: "noChange", report: { cliText: nothing.stdout, notes: [] } });
  const refused = traceApplyPreview(applyRun(1, "", "Error: refused\n"), "/s");
  assert.equal(refused.kind, "shown");
  assert.deepEqual(refused.report.notes, ["Nothing was written."]);
  const unreadable = traceApplyPreview(applyRun(0, "Trace apply\n"), "/s");
  assert.deepEqual(unreadable, {
    kind: "shown",
    report: { cliText: "Trace apply\n", notes: [TRACE_APPLY_UNREADABLE_LINE] },
  });
  const stopped = traceApplyPreview(applyRun(null, ""), "/s");
  assert.deepEqual(stopped.report.notes, [TRACE_APPLY_WORDING.stopped, "Nothing was written."]);
});

test("traceApplyPreview: a Summary: without a Plan: line, or a Plan: without a Summary:, offers no Write", () => {
  const { applyDryRun } = answers();
  const lines = applyDryRun.stdout.split("\n");
  const summaryOnly = lines.filter((line) => !line.startsWith("Plan: ")).join("\n");
  const planOnly = lines.filter((line) => !line.startsWith("Summary: ")).join("\n");
  assert.notEqual(applySummaryOf(summaryOnly), undefined, "the case keeps its Summary: line");
  assert.notEqual(applyPlanOf(planOnly), undefined, "the case keeps its Plan: line");
  for (const stdout of [summaryOnly, planOnly]) {
    const preview = traceApplyPreview(applyRun(0, stdout), "/s");
    assert.equal(preview.kind, "shown", stdout);
    assert.deepEqual(preview.report.notes, [TRACE_APPLY_UNREADABLE_LINE], stdout);
  }
});

test("the pending question, the dirty refusal", () => {
  assert.equal(pendingQuestion(1), "1 decision in the proposals file is not applied.");
  assert.equal(pendingQuestion(4), "4 decisions in the proposals file are not applied.");
  assert.match(pendingQuestion(undefined), /cannot be read/);
  assert.equal(isDirtyRefusal(true), DIRTY_REFUSAL);
  assert.equal(isDirtyRefusal(false), undefined);
});

// --- Where an element is, and choosing one --------------------------------------------------------

function fsReader(): Reader {
  return {
    readText: (file) => {
      try {
        return fs.readFileSync(file, "utf8");
      } catch {
        return undefined;
      }
    },
    listDir: (directory) => {
      try {
        return fs.readdirSync(directory);
      } catch {
        return undefined;
      }
    },
  };
}

function references() {
  return loadReferences(fs.readFileSync(path.join(SCHEMAS_DIR, "references.json"), "utf8"));
}

const MODEL_ROOT = path.join(MODEL_FIXTURE, "model");

function lineOf(relative: string, needle: string): number {
  const lines = fs.readFileSync(path.join(MODEL_ROOT, relative), "utf8").split("\n");
  const line = lines.findIndex((text) => text.includes(needle));
  assert.notEqual(line, -1, `${needle} in ${relative}`);
  return line;
}

test("traceElementLocation finds each of the ten kinds at its own line", () => {
  const refs = references();
  const reader = fsReader();
  const cases: Array<[TraceAddress, string, string]> = [
    [{ kind: "swc", owner: null, name: "Brake" }, "swc/app/Brake.yaml", "name: Brake"],
    [{ kind: "port", owner: "Brake", name: "pCmd" }, "swc/app/Brake.yaml", "name: pCmd"],
    [
      { kind: "runnable", owner: "Brake", name: "Supervise" },
      "swc/app/Brake.yaml",
      "name: Supervise",
    ],
    [{ kind: "port", owner: "Lamp", name: "pLamp" }, "swc/app/Lamp.yaml", "name: pLamp"],
    [{ kind: "interface", owner: null, name: "ITemp" }, "interfaces/ITemp.yaml", "name: ITemp"],
    [{ kind: "composition", owner: null, name: "Top" }, "compositions/Top.yaml", "name: Top"],
    [{ kind: "task", owner: null, name: "Task10ms" }, "tasks/Task10ms.yaml", "name: Task10ms"],
    [{ kind: "processor", owner: null, name: "Mcu" }, "hardware/Mcu.yaml", "name: Mcu"],
    [{ kind: "core", owner: null, name: "Core0" }, "hardware/Core0.yaml", "name: Core0"],
    [{ kind: "memory", owner: null, name: "Ram" }, "hardware/Ram.yaml", "name: Ram"],
    [
      { kind: "partition", owner: null, name: "AppPartition" },
      "hardware/AppPartition.yaml",
      "name: AppPartition",
    ],
    [{ kind: "swc", owner: null, name: "brake" }, "swc/app/brake~1.yaml", "name: brake"],
  ];
  for (const [address, relative, needle] of cases) {
    const found = traceElementLocation(refs, address, MODEL_ROOT, reader);
    assert.ok(found, `${address.kind} ${address.name}`);
    assert.equal(found.file, path.join(MODEL_ROOT, relative), address.name);
    assert.equal(found.line, lineOf(relative, needle), address.name);
    assert.equal(found.memberFound, true);
  }
});

test("a member not found falls back to the identity line; a kind mismatch or a missing element is nothing", () => {
  const refs = references();
  const reader = fsReader();
  const missing = traceElementLocation(
    refs,
    { kind: "port", owner: "Brake", name: "pGone" },
    MODEL_ROOT,
    reader,
  );
  assert.deepEqual(missing, {
    file: path.join(MODEL_ROOT, "swc/app/Brake.yaml"),
    line: lineOf("swc/app/Brake.yaml", "name: Brake"),
    memberFound: false,
  });
  assert.equal(
    traceElementLocation(refs, { kind: "core", owner: null, name: "Mcu" }, MODEL_ROOT, reader),
    undefined,
  );
  assert.equal(
    traceElementLocation(refs, { kind: "swc", owner: null, name: "Gone" }, MODEL_ROOT, reader),
    undefined,
  );
  assert.equal(
    notFoundMessage({ kind: "port", owner: "Brake", name: "pTemp" }),
    "SWCraft: port 'pTemp' of 'Brake' is not in the model as saved; search again.",
  );
  const requirement = requirementLocation(refs, "SR-2", MODEL_ROOT, reader);
  assert.equal(requirement?.file, path.join(MODEL_ROOT, "requirements/SR-2.yaml"));
});

test("chooseCandidates: the five layers only, the external interface left out, kinds read", () => {
  const listings: LayerListing[] = [
    { layer: "swc", elements: ["Brake", "Lamp"] },
    { layer: "interfaces", elements: ["IBus", "ITemp"] },
    { layer: "connectors", elements: ["C1"] },
    { layer: "compositions", elements: ["Top"] },
    { layer: "tasks", elements: ["Task10ms"] },
    { layer: "hardware", elements: ["AppPartition", "Core0", "Mcu", "Ram", "Gone"] },
    { layer: "requirements", elements: ["SR-1"] },
  ];
  const items = chooseCandidates(listings, references(), MODEL_ROOT, fsReader());
  assert.deepEqual(
    items.map((item) => `${item.address.kind} ${item.address.name}`),
    [
      "swc Brake",
      "swc Lamp",
      "interface ITemp",
      "composition Top",
      "task Task10ms",
      "partition AppPartition",
      "core Core0",
      "processor Mcu",
      "memory Ram",
    ],
  );
});

test("componentMembers: the component, its ports and runnables, in both list layouts", () => {
  const refs = references();
  const brake = fs.readFileSync(path.join(MODEL_ROOT, "swc/app/Brake.yaml"), "utf8");
  assert.deepEqual(
    componentMembers(refs, "Brake", brake).map((item) => addressOf(item.address)),
    ["swc Brake", "port Brake.pTemp", "port Brake.pCmd", "runnable Brake.Supervise"],
  );
  const lamp = fs.readFileSync(path.join(MODEL_ROOT, "swc/app/Lamp.yaml"), "utf8");
  assert.deepEqual(
    componentMembers(refs, "Lamp", lamp).map((item) => addressOf(item.address)),
    ["swc Lamp", "port Lamp.pLamp", "runnable Lamp.Drive"],
  );
  const twice = `${brake}  - name: pTemp\n`.replace("runnables:", "runnables_x:");
  assert.ok(!componentMembers(refs, "Brake", twice).some((item) => item.label === "Supervise"));
});

function addressOf(address: TraceAddress): string {
  return address.owner === null
    ? `${address.kind} ${address.name}`
    : `${address.kind} ${address.owner}.${address.name}`;
}

// --- The commands ----------------------------------------------------------------------------------

test("the tree's commands: fifteen, dotless, and ten of them spawn nothing", () => {
  assert.equal(TRACE_VIEW_COMMANDS.length, 15);
  assert.equal(TRACE_NON_SPAWNING_COMMANDS.length, 10);
  for (const id of TRACE_VIEW_COMMANDS) {
    assert.match(id, /^swcraft\.trace[A-Za-z0-9]+$/);
  }
  for (const id of TRACE_NON_SPAWNING_COMMANDS) {
    assert.ok(TRACE_VIEW_COMMANDS.includes(id), id);
  }
  assert.deepEqual(
    TRACE_VIEW_COMMANDS.filter((id) => !TRACE_NON_SPAWNING_COMMANDS.includes(id)),
    [
      "swcraft.traceNewSearch",
      "swcraft.traceChooseMode",
      "swcraft.traceOtherElement",
      "swcraft.traceApply",
      "swcraft.traceChooseElement",
    ],
  );
});

// --- Escapes, stale nodes, hand-edited files and the busy hold -------------------------------------

test("the canonical title with every escape round-trips to Python's bytes", () => {
  const title = entry(canonical(), "BRK-8").title;
  assert.equal(title, 'Say "stop" \\ now\tthen next \u{1F697}');
  assert.ok(
    canonicalBytes()
      .toString("utf8")
      .includes('"title": "Say \\"stop\\" \\\\ now\\tthen next \u{1F697}"'),
  );
});

test("a lone surrogate escape is refused, a well-formed pair is not", () => {
  const good = canonicalBytes().toString("utf8");
  const lone = readProposals(
    Buffer.from(good.replace('"title": "Brake', '"title": "\\ud800 Brake'), "utf8"),
  );
  assert.ok(isRefusal(lone));
  assert.match(lone.error, /lone surrogate/);
  const pair = readProposals(
    Buffer.from(good.replace('"title": "Brake', '"title": "\\ud83d\\ude97 Brake'), "utf8"),
  );
  assert.ok(!isRefusal(pair));
});

test("a link confirmed by a node from before a reload must still name its target", () => {
  const file = canonical();
  const links = entry(file, "BRK-6").linked;
  assert.equal(
    entry(edited(confirmLink(file, "BRK-6", 0, links[0].target)), "BRK-6").linked[0].decision,
    "confirm",
  );
  const other: TraceAddress = { kind: "swc", owner: null, name: "Elsewhere" };
  assert.match(refused(confirmLink(file, "BRK-6", 0, other)), /not in the proposals file/);
  assert.match(refused(unconfirmLink(file, "BRK-6", 0, other)), /not in the proposals file/);
});

test("a hand-edited file holding a pair or a requirement twice still gives unique ids", () => {
  const file = canonical();
  const brk5 = entry(file, "BRK-5");
  const twice = {
    ...file,
    requirements: [
      ...file.requirements.map((item) =>
        item.id === "BRK-5"
          ? { ...item, candidates: [...item.candidates, item.candidates[0]] }
          : item,
      ),
      brk5,
    ],
  };
  const children = traceChildren(entry(twice, "BRK-5"), "review", 1).map((node) => node.id);
  assert.equal(new Set(children).size, children.length);
  assert.ok(children.at(-1)?.endsWith("~2") || children.some((id) => id.endsWith("~2")));
  const requirements = traceRequirements(twice, "review", "Brake Spec", 1).map((node) => node.id);
  assert.equal(new Set(requirements).size, requirements.length);
});

test("searchRefusedPending reads the CLI's three pending refusals and nothing else", () => {
  for (const stderr of [
    "Error: `dist/experiments/trace/proposals.json` holds 2 decisions not applied. Apply them with `--apply`, or search again with `--discard-pending`.\n",
    "Error: `dist/experiments/trace/proposals.json` cannot be read as a proposals file (/x: y), so it may hold decisions not applied. Apply it with `--apply`, or search again with `--discard-pending`.\n",
    "Error: `dist/experiments/trace/proposals.json` changed while the search ran, so it may hold decisions not applied; nothing was written.\n",
  ]) {
    assert.equal(searchRefusedPending(stderr), true, stderr);
  }
  assert.equal(searchRefusedPending("Error: the configuration file is refused: x\n"), false);
});

test("holdingTraceBusy: busy for the flow, cleared after it whatever it came to, kept when reserved", async () => {
  const states: boolean[] = [];
  const setBusy = (busy: boolean) => {
    states.push(busy);
  };
  assert.equal(await holdingTraceBusy(setBusy, false, async () => "applied"), "applied");
  assert.deepEqual(states, [true, false]);

  states.length = 0;
  await assert.rejects(
    holdingTraceBusy(setBusy, false, async () => {
      assert.deepEqual(states, [true], "busy while the flow runs");
      throw new Error("boom");
    }),
    /boom/,
  );
  assert.deepEqual(states, [true, false], "cleared after a flow that throws");

  states.length = 0;
  await assert.rejects(
    holdingTraceBusy(setBusy, true, async () => {
      throw new Error("boom");
    }),
    /boom/,
  );
  assert.deepEqual(states, [true], "a reserved hold is the caller's to clear");
});

test("holdingTraceBusy: busy is held before the flow's first await, so a second start during a flush is refused", async () => {
  let busy = false;
  const setBusy = (value: boolean) => {
    busy = value;
  };
  let releaseFlush: () => void = () => {};
  const flush = new Promise<void>((resolve) => {
    releaseFlush = resolve;
  });
  // The command flow's shape: the busy guard, then the hold, whose flow awaits the flush first.
  const start = (): Promise<string> =>
    busy
      ? Promise.resolve("refused")
      : holdingTraceBusy(setBusy, false, async () => {
          await flush;
          return "applied";
        });
  const first = start();
  assert.equal(busy, true, "busy before the flush is awaited");
  assert.equal(await start(), "refused", "a second start during the flush is refused");
  releaseFlush();
  assert.equal(await first, "applied");
  assert.equal(busy, false);
});

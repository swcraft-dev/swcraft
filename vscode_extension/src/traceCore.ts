// Pure logic of the Trace Links view (the trace link search's panel): the proposals file's reader,
// model and canonical writer, the pending count, the decision edits, the tree's descriptors and
// evidence sentences, the CLI's argv and answer parsers, the pass picker's outcome, the Apply
// wording, and where an element of an address is.
//
// No `vscode` import: it runs under plain `node --test`; `commands.ts` wires it. The panel judges
// nothing: every proposal is the search's and every write into the model is Apply's (`swcraft
// experiment trace --apply`). The one file the panel writes is the proposals file the CLI named,
// and only its decision slots (`decision`, `preselected`, the `chosen` list); `applied` and
// `stale` are Apply's alone.

import * as path from "node:path";

import {
  type AppliedRunWording,
  type CapturedRun,
  type ImportReport,
  importDryRunOutcome,
  importReportOption,
  type LayerListing,
} from "./commandCore";
import {
  type Location,
  locateSubject,
  memberLinesOf,
  type Reader,
  type References,
  resolveElement,
  type SubjectStep,
  topLevelKindOf,
} from "./navigationCore";
import { outlineOf } from "./symbolCore";

// --- The proposals file -------------------------------------------------------------

/** A target's address: `owner` names the component of a port or a runnable, `null` otherwise. */
export interface TraceAddress {
  kind: string;
  owner: string | null;
  name: string;
}

/** A rational figure as the file writes one. */
export interface Fraction {
  n: number;
  d: number;
}

/** One closed evidence record; `report.md` renders each as one fixed sentence. */
export type EvidenceRecord =
  | { kind: "name"; phrase: string }
  | {
      kind: "neighbours";
      share: number | Fraction;
      seeded: number;
      of: number;
      examples: string[];
    }
  | { kind: "similar"; score: Fraction; count: number; examples: string[] }
  | { kind: "text"; terms: string[] };

/** The bands, strongest first: the search's `BANDS`. */
export const BANDS = ["strong", "medium", "weak"] as const;
export type Band = (typeof BANDS)[number];

/** One proposed row: the search's evidence and the architect's decision. */
export interface CandidateRow {
  place: number;
  group: { kind: string; name: string; score: Fraction; evidence: EvidenceRecord[] };
  target: TraceAddress;
  fingerprint: string;
  band: Band;
  score: Fraction;
  evidence: EvidenceRecord[];
  decision: "accept" | "reject" | null;
  preselected?: true;
  applied?: true;
  stale?: string;
}

/** An accept of a target outside the rows. */
export interface ChosenEntry {
  target: TraceAddress;
  applied?: true;
  stale?: string;
}

/** A link the requirement already has; `decision` only while it awaits confirmation. */
export interface LinkedEntry {
  target: TraceAddress;
  member: string;
  version: number | null;
  confirmed: boolean;
  decision?: "confirm" | null;
  applied?: true;
  stale?: string;
}

/** One requirement's entry. */
export interface RequirementEntry {
  id: string;
  title: string;
  /** The requirement's description at the search, for display only. */
  description: string;
  version: number;
  fingerprint: string;
  document: string | null;
  pass: "review" | "labelling";
  chosenBecause: string | null;
  lowAccuracy: boolean;
  linked: LinkedEntry[];
  candidates: CandidateRow[];
  chosen: ChosenEntry[];
}

/**
 * The whole file. Every key the panel does not read (`name`, `model`, `configuration`, the
 * fingerprints, the scores) is kept as read, so a file written back differs only in the edited
 * slots.
 */
export interface ProposalsFile {
  formatVersion: 1;
  kind: "trace";
  requirements: RequirementEntry[];
  [key: string]: unknown;
}

/** A file the panel cannot read: the reason names the place (a JSON pointer) when there is one. */
export interface ProposalsRefusal {
  error: string;
}

const TARGET_KINDS: ReadonlySet<string> = new Set([
  "swc",
  "port",
  "runnable",
  "interface",
  "composition",
  "task",
  "processor",
  "core",
  "memory",
  "partition",
]);
const OWNED_KINDS: ReadonlySet<string> = new Set(["port", "runnable"]);

class Refused extends Error {}

function fail(pointer: string, sentence: string): never {
  throw new Refused(`${pointer === "" ? "top level" : pointer}: ${sentence}`);
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function objectAt(value: unknown, pointer: string): Record<string, unknown> {
  if (!isObject(value)) {
    fail(pointer, "must be an object");
  }
  return value;
}

function arrayAt(record: Record<string, unknown>, key: string, pointer: string): unknown[] {
  const value = record[key];
  if (!Array.isArray(value)) {
    fail(`${pointer}/${key}`, "must be a list");
  }
  return value;
}

function stringAt(record: Record<string, unknown>, key: string, pointer: string): string {
  if (typeof record[key] !== "string") {
    fail(`${pointer}/${key}`, "must be text");
  }
  return record[key] as string;
}

function nullableStringAt(record: Record<string, unknown>, key: string, pointer: string): void {
  if (!(key in record) || (record[key] !== null && typeof record[key] !== "string")) {
    fail(`${pointer}/${key}`, "must be text or null");
  }
}

function integerAt(record: Record<string, unknown>, key: string, pointer: string): void {
  if (!Number.isInteger(record[key])) {
    fail(`${pointer}/${key}`, "must be an integer");
  }
}

function booleanAt(record: Record<string, unknown>, key: string, pointer: string): void {
  if (typeof record[key] !== "boolean") {
    fail(`${pointer}/${key}`, "must be true or false");
  }
}

function oneOfAt(
  record: Record<string, unknown>,
  key: string,
  allowed: readonly unknown[],
  pointer: string,
): void {
  if (!(key in record) || !allowed.includes(record[key])) {
    fail(`${pointer}/${key}`, `must be one of ${allowed.map((v) => JSON.stringify(v)).join(", ")}`);
  }
}

/** `applied` (only `true`) and `stale` (text) are present with a value or absent. */
function marksAt(record: Record<string, unknown>, pointer: string): void {
  if ("applied" in record && record.applied !== true) {
    fail(`${pointer}/applied`, "must be true when present");
  }
  if ("stale" in record && typeof record.stale !== "string") {
    fail(`${pointer}/stale`, "must be text when present");
  }
}

function addressAt(value: unknown, pointer: string): void {
  const address = objectAt(value, pointer);
  oneOfAt(address, "kind", [...TARGET_KINDS], pointer);
  const kind = address.kind as string;
  const name = stringAt(address, "name", pointer);
  if (name === "") {
    fail(`${pointer}/name`, "must not be empty");
  }
  if (OWNED_KINDS.has(kind)) {
    if (typeof address.owner !== "string" || address.owner === "") {
      fail(`${pointer}/owner`, `a ${kind} names its component`);
    }
  } else if (address.owner !== null) {
    fail(`${pointer}/owner`, `a ${kind} has no owner: it must be null`);
  }
}

function evidenceAt(value: unknown, pointer: string): void {
  const records = Array.isArray(value) ? value : fail(pointer, "must be a list");
  records.forEach((item, index) => {
    const where = `${pointer}/${index}`;
    const record = objectAt(item, where);
    oneOfAt(record, "kind", ["name", "neighbours", "similar", "text"], where);
    if (record.kind === "name") {
      stringAt(record, "phrase", where);
    } else if (record.kind === "neighbours") {
      integerAt(record, "seeded", where);
      integerAt(record, "of", where);
      arrayAt(record, "examples", where);
    } else if (record.kind === "similar") {
      integerAt(record, "count", where);
      arrayAt(record, "examples", where);
    } else {
      arrayAt(record, "terms", where);
    }
  });
}

/**
 * The shape check: every key the panel reads or writes, nothing more. The full strictness (a
 * repeated key, a pair proposed twice, a `member` naming another requirement) is Apply's reader,
 * which refuses such a file; the panel then shows the CLI's message.
 */
function checkShape(document: unknown): asserts document is ProposalsFile {
  const root = objectAt(document, "");
  if (root.formatVersion !== 1) {
    fail("/formatVersion", "must be 1");
  }
  if (root.kind !== "trace") {
    fail("/kind", 'must be "trace"');
  }
  arrayAt(root, "requirements", "").forEach((value, number) => {
    const where = `/requirements/${number}`;
    const entry = objectAt(value, where);
    stringAt(entry, "id", where);
    stringAt(entry, "title", where);
    stringAt(entry, "description", where);
    integerAt(entry, "version", where);
    nullableStringAt(entry, "document", where);
    oneOfAt(entry, "pass", ["review", "labelling"], where);
    nullableStringAt(entry, "chosenBecause", where);
    booleanAt(entry, "lowAccuracy", where);
    arrayAt(entry, "candidates", where).forEach((item, index) => {
      const at = `${where}/candidates/${index}`;
      const row = objectAt(item, at);
      integerAt(row, "place", at);
      const group = objectAt(row.group, `${at}/group`);
      stringAt(group, "kind", `${at}/group`);
      stringAt(group, "name", `${at}/group`);
      evidenceAt(group.evidence, `${at}/group/evidence`);
      addressAt(row.target, `${at}/target`);
      oneOfAt(row, "band", BANDS, at);
      evidenceAt(row.evidence, `${at}/evidence`);
      oneOfAt(row, "decision", [null, "accept", "reject"], at);
      if ("preselected" in row && row.preselected !== true) {
        fail(`${at}/preselected`, "must be true when present");
      }
      objectAt(row.score, `${at}/score`);
      marksAt(row, at);
    });
    arrayAt(entry, "chosen", where).forEach((item, index) => {
      const at = `${where}/chosen/${index}`;
      const chosen = objectAt(item, at);
      addressAt(chosen.target, `${at}/target`);
      marksAt(chosen, at);
    });
    arrayAt(entry, "linked", where).forEach((item, index) => {
      const at = `${where}/linked/${index}`;
      const link = objectAt(item, at);
      addressAt(link.target, `${at}/target`);
      stringAt(link, "member", at);
      if (link.version !== null) {
        integerAt(link, "version", at);
      }
      booleanAt(link, "confirmed", at);
      if (link.confirmed === false) {
        oneOfAt(link, "decision", [null, "confirm"], at);
      }
      marksAt(link, at);
    });
  });
}

/**
 * Check every number token of *text* outside strings: an integer that is not safe is refused,
 * because `JSON.parse` would round it and a write would change a value the panel never touched (a
 * requirement version has no upper bound); a fraction or exponent is refused too: the format holds
 * integers only.
 */
function refuseUnsafeNumbers(text: string): void {
  let index = 0;
  while (index < text.length) {
    const char = text[index];
    if (char === '"') {
      index += 1;
      while (index < text.length && text[index] !== '"') {
        index += text[index] === "\\" ? 2 : 1;
      }
      index += 1;
      continue;
    }
    if (char === "-" || (char >= "0" && char <= "9")) {
      const start = index;
      index += 1;
      while (index < text.length && /[0-9.eE+-]/.test(text[index])) {
        index += 1;
      }
      const token = text.slice(start, index);
      if (!/^-?\d+$/.test(token)) {
        fail("", `the number ${token} is not an integer; the format holds integers only`);
      }
      if (!Number.isSafeInteger(Number(token))) {
        fail("", `the integer ${token} is too large for the panel to keep exactly`);
      }
      continue;
    }
    index += 1;
  }
}

const LONE_SURROGATE = /[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/;

/**
 * Refuse a string holding a lone surrogate (a `\\ud800` escape a hand edit can write): Python's
 * reader takes it, but the search's writer cannot encode it, so the panel never writes one back.
 */
function refuseLoneSurrogates(value: unknown, pointer: string): void {
  if (typeof value === "string") {
    if (LONE_SURROGATE.test(value)) {
      fail(pointer, "holds a lone surrogate escape, which is no character");
    }
  } else if (Array.isArray(value)) {
    value.forEach((item, index) => {
      refuseLoneSurrogates(item, `${pointer}/${index}`);
    });
  } else if (isObject(value)) {
    for (const [key, item] of Object.entries(value)) {
      refuseLoneSurrogates(key, pointer);
      refuseLoneSurrogates(item, `${pointer}/${key}`);
    }
  }
}

/**
 * Read a proposals file's bytes: strict UTF-8, JSON, every integer safe, then the shape check, or
 * the reason it cannot be read.
 */
export function readProposals(bytes: Uint8Array): ProposalsFile | ProposalsRefusal {
  let text: string;
  try {
    text = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(bytes);
  } catch {
    return { error: "it is not UTF-8 text" };
  }
  let document: unknown;
  try {
    document = JSON.parse(text);
  } catch (error) {
    return { error: `it is not JSON (${error instanceof Error ? error.message : String(error)})` };
  }
  try {
    refuseUnsafeNumbers(text);
    refuseLoneSurrogates(document, "");
    checkShape(document);
  } catch (error) {
    if (error instanceof Refused) {
      return { error: error.message };
    }
    throw error;
  }
  return document;
}

/** Whether a read answered a file or a refusal. */
export function isRefusal(read: ProposalsFile | ProposalsRefusal): read is ProposalsRefusal {
  return "error" in read;
}

/** Python's `str` order: by code point, which `Array.prototype.sort` (UTF-16 units) is not. */
function codePointOrder(left: string, right: string): number {
  const a = [...left];
  const b = [...right];
  for (let i = 0; i < Math.min(a.length, b.length); i += 1) {
    const difference = (a[i].codePointAt(0) ?? 0) - (b[i].codePointAt(0) ?? 0);
    if (difference !== 0) {
      return difference;
    }
  }
  return a.length - b.length;
}

function serializeValue(value: unknown, indent: string): string {
  if (Array.isArray(value)) {
    if (value.length === 0) {
      return "[]";
    }
    const inner = `${indent}  `;
    return `[\n${value.map((item) => inner + serializeValue(item, inner)).join(",\n")}\n${indent}]`;
  }
  if (isObject(value)) {
    const keys = Object.keys(value)
      .filter((key) => value[key] !== undefined)
      .sort(codePointOrder);
    if (keys.length === 0) {
      return "{}";
    }
    const inner = `${indent}  `;
    const members = keys.map(
      (key) => `${inner}${JSON.stringify(key)}: ${serializeValue(value[key], inner)}`,
    );
    return `{\n${members.join(",\n")}\n${indent}}`;
  }
  return JSON.stringify(value);
}

/**
 * The file's text exactly as the search writes it (`trace_search.proposals_json`: `json.dumps(…,
 * indent=2, ensure_ascii=False, sort_keys=True)` plus a final newline): keys sorted by code point at
 * every depth, two-space indentation, separators `","` and `": "`, non-ASCII kept. Objects are
 * written member by member: `JSON.stringify` would put an integer-like key first whatever the
 * order.
 */
export function serializeProposals(file: ProposalsFile): string {
  return `${serializeValue(file, "")}\n`;
}

// --- The pending count --------------------------------------------------------------

/** An item still to be carried out: decided, neither applied nor stale. */
function isPending(decided: boolean, item: { applied?: true; stale?: string }): boolean {
  return decided && item.applied === undefined && item.stale === undefined;
}

/**
 * `trace_records.pending` in TypeScript: every row and awaiting link with a decision, and every
 * `chosen` entry, carrying neither `applied` nor `stale`. The CLI's guard stays the authority;
 * `pending_vectors.json` holds the two equal.
 */
export function pendingCount(file: ProposalsFile): number {
  let count = 0;
  for (const entry of file.requirements) {
    for (const row of entry.candidates) {
      count += isPending(row.decision !== null, row) ? 1 : 0;
    }
    for (const chosen of entry.chosen) {
      count += isPending(true, chosen) ? 1 : 0;
    }
    for (const link of entry.linked) {
      count += isPending((link.decision ?? null) !== null, link) ? 1 : 0;
    }
  }
  return count;
}

// --- The decisions ------------------------------------------------------------------

/** What an edit answers: the new file, or the one sentence that refused it. */
export type EditResult = { ok: true; file: ProposalsFile } | { ok: false; refusal: string };

/** An edit that applies to many items: the new file and how many it changed. */
export interface BulkEdit {
  file: ProposalsFile;
  count: number;
}

export const ALREADY_IN_THE_LIST = "already in the list: tick it there";
export const ALREADY_LINKED = "already linked";
export const ITEM_DONE = "this item was carried out or found stale by Apply; it cannot be changed";
export const ITEM_GONE = "the item is not in the proposals file any more; it was reloaded";

/** Two addresses name the same target. */
export function sameAddress(left: TraceAddress, right: TraceAddress): boolean {
  return left.kind === right.kind && left.owner === right.owner && left.name === right.name;
}

function isDone(item: { applied?: true; stale?: string }): boolean {
  return item.applied !== undefined || item.stale !== undefined;
}

/** *file* with entry *id* replaced by *change(entry)*: the other entries are shared, not copied. */
function withEntry(
  file: ProposalsFile,
  id: string,
  change: (entry: RequirementEntry) => RequirementEntry | string,
): EditResult {
  const index = file.requirements.findIndex((entry) => entry.id === id);
  if (index === -1) {
    return { ok: false, refusal: ITEM_GONE };
  }
  const changed = change(file.requirements[index]);
  if (typeof changed === "string") {
    return { ok: false, refusal: changed };
  }
  const requirements = file.requirements.slice();
  requirements[index] = changed;
  return { ok: true, file: { ...file, requirements } };
}

function withRow(
  file: ProposalsFile,
  id: string,
  target: TraceAddress,
  change: (row: CandidateRow, entry: RequirementEntry) => CandidateRow | string,
): EditResult {
  return withEntry(file, id, (entry) => {
    const index = entry.candidates.findIndex((row) => sameAddress(row.target, target));
    if (index === -1) {
      return ITEM_GONE;
    }
    const row = entry.candidates[index];
    if (isDone(row)) {
      return ITEM_DONE;
    }
    const changed = change(row, entry);
    if (typeof changed === "string") {
      return changed;
    }
    const candidates = entry.candidates.slice();
    candidates[index] = changed;
    return { ...entry, candidates };
  });
}

/** *row* with *decision* and no `preselected`. */
function decided(row: CandidateRow, decision: CandidateRow["decision"]): CandidateRow {
  const { preselected: _dropped, ...rest } = row;
  return { ...rest, decision };
}

/** Tick a row: an accept the person made, so no `preselected`. */
export function tick(file: ProposalsFile, id: string, target: TraceAddress): EditResult {
  return withRow(file, id, target, (row, entry) => {
    if (entry.chosen.some((chosen) => sameAddress(chosen.target, target))) {
      return ALREADY_IN_THE_LIST;
    }
    if (row.decision === "reject") {
      return "a rejected row is not ticked: undo the rejection first";
    }
    return row.decision === "accept" ? row : decided(row, "accept");
  });
}

/** Untick a row: undecided again, its preselection gone with it. */
export function untick(file: ProposalsFile, id: string, target: TraceAddress): EditResult {
  return withRow(file, id, target, (row) => (row.decision === "accept" ? decided(row, null) : row));
}

/** Reject a row, undecided or accepted. */
export function reject(file: ProposalsFile, id: string, target: TraceAddress): EditResult {
  return withRow(file, id, target, (row) => decided(row, "reject"));
}

/** Undo a rejection: undecided again. */
export function undoReject(file: ProposalsFile, id: string, target: TraceAddress): EditResult {
  return withRow(file, id, target, (row) => (row.decision === "reject" ? decided(row, null) : row));
}

/**
 * *Mark strong*: every strong, undecided row (of *documents* when given) neither applied nor stale
 * becomes an accept marked `preselected`, a preselection the person did not tick one by one.
 */
export function markStrong(
  file: ProposalsFile,
  documents?: ReadonlyArray<string | null>,
): BulkEdit {
  let count = 0;
  const requirements = file.requirements.map((entry) => {
    if ((documents !== undefined && !documents.includes(entry.document)) || isLinked(entry)) {
      return entry; // the chosen documents only, and never a linked requirement, which shows no row
    }
    let changed = false;
    const candidates = entry.candidates.map((row) => {
      if (row.band !== "strong" || row.decision !== null || isDone(row)) {
        return row;
      }
      changed = true;
      count += 1;
      return { ...row, decision: "accept" as const, preselected: true as const };
    });
    return changed ? { ...entry, candidates } : entry;
  });
  return { file: count === 0 ? file : { ...file, requirements }, count };
}

/**
 * *Clear*: every decision not applied and not stale is undone (rows and awaiting links undecided,
 * `chosen` entries removed), of *documents* when given. *count* is how many.
 */
export function clearDecisions(
  file: ProposalsFile,
  documents?: ReadonlyArray<string | null>,
): BulkEdit {
  let count = 0;
  const requirements = file.requirements.map((entry) => {
    if (documents !== undefined && !documents.includes(entry.document)) {
      return entry;
    }
    const before = count;
    const candidates = entry.candidates.map((row) => {
      if (row.decision === null || isDone(row)) {
        return row;
      }
      count += 1;
      return decided(row, null);
    });
    const chosen = entry.chosen.filter((item) => {
      if (isDone(item)) {
        return true;
      }
      count += 1;
      return false;
    });
    const linked = entry.linked.map((link) => {
      if ((link.decision ?? null) === null || isDone(link)) {
        return link;
      }
      count += 1;
      return { ...link, decision: null };
    });
    return count === before ? entry : { ...entry, candidates, chosen, linked };
  });
  return { file: count === 0 ? file : { ...file, requirements }, count };
}

/**
 * *Choose Another Element…*: append *target* to the requirement's `chosen` list. Refused when it is
 * already one of its rows or choices (Apply's reader refuses a pair twice) or one of its links
 * (Apply would only mark it done).
 */
export function choose(file: ProposalsFile, id: string, target: TraceAddress): EditResult {
  return withEntry(file, id, (entry) => {
    if (
      entry.candidates.some((row) => sameAddress(row.target, target)) ||
      entry.chosen.some((chosen) => sameAddress(chosen.target, target))
    ) {
      return ALREADY_IN_THE_LIST;
    }
    if (entry.linked.some((link) => sameAddress(link.target, target))) {
      return ALREADY_LINKED;
    }
    return { ...entry, chosen: [...entry.chosen, { target: { ...target } }] };
  });
}

/** Untick or *Remove Choice* on a `chosen` entry: the entry removed. */
export function removeChoice(file: ProposalsFile, id: string, target: TraceAddress): EditResult {
  return withEntry(file, id, (entry) => {
    const index = entry.chosen.findIndex((chosen) => sameAddress(chosen.target, target));
    if (index === -1) {
      return ITEM_GONE;
    }
    if (isDone(entry.chosen[index])) {
      return ITEM_DONE;
    }
    return { ...entry, chosen: entry.chosen.filter((_, position) => position !== index) };
  });
}

function withAwaitingLink(
  file: ProposalsFile,
  id: string,
  index: number,
  decision: "confirm" | null,
  target?: TraceAddress,
): EditResult {
  return withEntry(file, id, (entry) => {
    const link = entry.linked[index];
    if (
      link === undefined ||
      link.confirmed ||
      (target !== undefined && !sameAddress(link.target, target))
    ) {
      return ITEM_GONE;
    }
    if (isDone(link)) {
      return ITEM_DONE;
    }
    const linked = entry.linked.slice();
    linked[index] = { ...link, decision };
    return { ...entry, linked };
  });
}

/**
 * Tick an awaiting link: confirm it at the requirement's current version. With *target*, the link
 * at *index* must still name it, so a node from before a reload never confirms another link.
 */
export function confirmLink(
  file: ProposalsFile,
  id: string,
  index: number,
  target?: TraceAddress,
): EditResult {
  return withAwaitingLink(file, id, index, "confirm", target);
}

/** Untick an awaiting link (*target* as for {@link confirmLink}). */
export function unconfirmLink(
  file: ProposalsFile,
  id: string,
  index: number,
  target?: TraceAddress,
): EditResult {
  return withAwaitingLink(file, id, index, null, target);
}

// --- The evidence sentences ---------------------------------------------------------

/** One record's fixed sentence, as `report.md` writes it (`trace_search._evidence_text`). */
export function evidenceSentence(record: EvidenceRecord): string {
  if (record.kind === "name") {
    return `the name ${record.phrase} is in the text`;
  }
  if (record.kind === "neighbours") {
    const examples = record.examples.length > 0 ? ` (e.g. ${record.examples.join(", ")})` : "";
    const noun = record.of === 1 ? "requirement" : "requirements";
    return `${record.seeded} of the ${record.of} linked ${noun} of this document link here${examples}`;
  }
  if (record.kind === "similar") {
    const examples = record.examples.length > 0 ? ` (e.g. ${record.examples.join(", ")})` : "";
    const noun = record.count === 1 ? "requirement" : "requirements";
    return `similar to ${record.count} linked ${noun}${examples}`;
  }
  return `shares the terms ${record.terms.join(", ")}`;
}

/** The records' sentences joined as the report joins them, *no evidence* for none. */
export function evidenceText(records: readonly EvidenceRecord[]): string {
  return records.length === 0 ? "no evidence" : records.map(evidenceSentence).join("; ");
}

// --- The tree -----------------------------------------------------------------------
//
// The panel's *Experiments → Trace Links* node holds two mode nodes, *Labelling* and *Review*; the
// Trace Links view shows the documents chosen for a mode, document → requirement → items. Every id
// starts with `trace:` and the file's *generation*, a counter the adapter raises on each reload
// from disk (a search, an Apply, a hand edit) and never on its own edits, so a reload is a new tree
// and an edit keeps what is expanded.

/** A ThemeIcon by id, with an optional ThemeColor id. */
export interface TraceIcon {
  id: string;
  color?: string;
}

/** What a node points at, so a command finds its item again in a reloaded file. */
export type TraceItemRef =
  | { slot: "candidates"; requirement: string; target: TraceAddress }
  | { slot: "chosen"; requirement: string; target: TraceAddress }
  | { slot: "linked"; requirement: string; index: number; target: TraceAddress };

/** The two modes; the mode is the node. */
export type TraceMode = "labelling" | "review";
export const TRACE_MODES: readonly TraceMode[] = ["labelling", "review"];
export const MODE_LABELS: Readonly<Record<TraceMode, string>> = {
  labelling: "Labelling",
  review: "Review",
};

/** One node of the tree, every decision of its look made here. */
export interface TraceNode {
  id: string;
  kind:
    | "mode"
    | "document"
    | "requirement"
    | "row"
    | "chosen"
    | "awaiting"
    | "linked"
    | "more"
    | "other";
  label: string;
  /** Ranges of *label* shown in the theme's highlight: a row's band and kind words. */
  highlights?: [number, number][];
  description: string;
  /** Markdown. */
  tooltip: string;
  icon?: TraceIcon;
  contextValue: string;
  /** `undefined`: no checkbox. */
  checkbox?: "checked" | "unchecked";
  collapsible: "none" | "collapsed" | "expanded";
  /** The mode every node below a mode node belongs to. */
  mode?: TraceMode;
  /** The document node's document (`null` for the no-document group). */
  document?: string | null;
  /** The requirement node's id, and every item node's. */
  requirement?: string;
  item?: TraceItemRef;
}

export const NO_DOCUMENT = "No document";
export const NOTHING_FOUND = "nothing found";
export const NO_DOCUMENTS_CHOSEN = "no documents chosen";
/** The undecided rows a requirement shows before *Other element…*. */
export const TOP_ROWS = 3;
/** The last row of a requirement's proposals: any element of the model, in a QuickPick. */
export const OTHER_ELEMENT = "Any element of the model…";
/** The longest part of a requirement's description its row shows. */
export const DESCRIPTION_EXCERPT = 80;

/** *text* on one line, at most *limit* characters, an ellipsis when cut. */
export function excerpt(text: string, limit = DESCRIPTION_EXCERPT): string {
  const line = text.replace(/\s+/g, " ").trim();
  return line.length <= limit ? line : `${line.slice(0, limit - 1).trimEnd()}…`;
}

/**
 * A Review row's icon: one full-size filled circle, the size of a linked row's check, coloured by
 * its band in chart colours, which every theme defines for dark and light.
 */
export const BAND_ICONS: Readonly<Record<Band, TraceIcon>> = {
  strong: { id: "circle-large-filled", color: "charts.green" },
  medium: { id: "circle-large-filled", color: "charts.yellow" },
  weak: { id: "circle-large-filled", color: "descriptionForeground" },
};

/**
 * A row's label and its highlighted words: `strong·runnable:BrakeController.ProcessBrakeRequest` in
 * Review, `runnable:BrakeController.ProcessBrakeRequest` in Labelling,
 * `linked·port:DiagnosticManager.pBrakeDiagnostics` for a link, `chosen·…` and `awaiting·…`
 * likewise: no spaces, the leading word and the kind highlighted.
 */
export function rowLabel(
  word: Band | "linked" | "chosen" | "awaiting" | undefined,
  address: TraceAddress,
): { label: string; highlights: [number, number][] } {
  const highlights: [number, number][] = [];
  let label = "";
  if (word !== undefined) {
    highlights.push([0, word.length]);
    label = `${word}·`;
  }
  highlights.push([label.length, label.length + address.kind.length]);
  const name = address.owner === null ? address.name : `${address.owner}.${address.name}`;
  return { label: `${label}${address.kind}:${name}`, highlights };
}

function encode(value: string | null): string {
  return value === null ? "-" : encodeURIComponent(value);
}

function modeId(mode: TraceMode, generation: number): string {
  return `trace:${generation}/${mode}`;
}

function documentId(mode: TraceMode, generation: number, document: string | null): string {
  return `${modeId(mode, generation)}/${document === null ? "doc:-" : `doc:=${encode(document)}`}`;
}

function requirementId(mode: TraceMode, generation: number, entry: RequirementEntry): string {
  return `${documentId(mode, generation, entry.document)}/req:${encode(entry.id)}`;
}

function addressKey(address: TraceAddress): string {
  return `${encode(address.kind)}:${encode(address.owner)}:${encode(address.name)}`;
}

/** An address as the tree writes it: `swc BrakeController`, `port BrakeController.pTemp`. */
export function addressText(address: TraceAddress): string {
  return address.owner === null
    ? `${address.kind} ${address.name}`
    : `${address.kind} ${address.owner}.${address.name}`;
}

function plural(count: number, noun: string): string {
  return `${count} ${noun}${count === 1 ? "" : "s"}`;
}

/** The best row: the strongest band, then the higher score, then the earlier place. */
export function bestRow(entry: RequirementEntry): CandidateRow | undefined {
  return rowsByStrength(entry.candidates)[0];
}

/** *rows* in best-row order: the strongest band, then the higher score, then the earlier place. */
export function rowsByStrength(rows: readonly CandidateRow[]): CandidateRow[] {
  return rows
    .map((row, index) => ({ row, index }))
    .sort(
      (left, right) =>
        BANDS.indexOf(left.row.band) - BANDS.indexOf(right.row.band) ||
        right.row.score.n - left.row.score.n ||
        left.row.place - right.row.place ||
        left.index - right.index,
    )
    .map(({ row }) => row);
}

/**
 * *nodes* with every id unique: a hand-edited file can hold a requirement id or a pair twice
 * (Apply's reader refuses both), and VS Code refuses two items of one id; a repeat gets `~2`,
 * `~3` … appended, the first keeps its id.
 */
function uniqueIds(nodes: TraceNode[]): TraceNode[] {
  const seen = new Map<string, number>();
  return nodes.map((node) => {
    const count = (seen.get(node.id) ?? 0) + 1;
    seen.set(node.id, count);
    return count === 1 ? node : { ...node, id: `${node.id}~${count}` };
  });
}

/**
 * Whether *link* holds at the requirement's current version: confirmed at the search, or confirmed
 * by an Apply since, which marks the item `applied` and leaves `confirmed` as the search found it.
 */
function holds(link: LinkedEntry): boolean {
  return link.confirmed || (link.decision === "confirm" && link.applied !== undefined);
}

/**
 * Whether the requirement is **linked**: a link that holds, or an accept or choice applied since.
 * A linked requirement asks for nothing: it shows its links and no candidate.
 */
export function isLinked(entry: RequirementEntry): boolean {
  return (
    entry.linked.some(holds) ||
    entry.candidates.some((row) => row.decision === "accept" && row.applied !== undefined) ||
    entry.chosen.some((chosen) => chosen.applied !== undefined)
  );
}

function hasOpenAccept(entry: RequirementEntry): boolean {
  return (
    entry.candidates.some((row) => row.decision === "accept" && !isDone(row)) ||
    entry.chosen.some((chosen) => !isDone(chosen)) ||
    entry.linked.some((link) => link.decision === "confirm" && !isDone(link))
  );
}

function everyRowRejected(entry: RequirementEntry): boolean {
  return entry.candidates.length > 0 && entry.candidates.every((row) => row.decision === "reject");
}

/**
 * The requirement's state: `linked`, `ticked` (an open accept, choice or confirm), `rejected`
 * (every row rejected), `nothing found` (no row, no link), or `undefined`: still to go.
 */
export function requirementState(entry: RequirementEntry): string | undefined {
  if (isLinked(entry)) {
    return "linked";
  }
  if (hasOpenAccept(entry)) {
    return "ticked";
  }
  if (everyRowRejected(entry)) {
    return "rejected";
  }
  if (entry.candidates.length === 0 && entry.chosen.length === 0 && entry.linked.length === 0) {
    return NOTHING_FOUND;
  }
  return undefined;
}

/** Whether the requirement is **done**: linked, ticked or every row rejected. */
export function isDoneRequirement(entry: RequirementEntry): boolean {
  const state = requirementState(entry);
  return state === "linked" || state === "ticked" || state === "rejected";
}

/** The two document sets, one per mode; `null` is the no-document group. */
export interface TraceModes {
  labelling: Array<string | null>;
  review: Array<string | null>;
}

export const EMPTY_MODES: TraceModes = { labelling: [], review: [] };

/** The remembered modes as `workspaceState` holds them, or the empty modes for anything else. */
export function readModes(value: unknown): TraceModes {
  const list = (item: unknown): Array<string | null> =>
    Array.isArray(item)
      ? item.filter((document) => document === null || typeof document === "string")
      : [];
  if (typeof value !== "object" || value === null) {
    return EMPTY_MODES;
  }
  const record = value as Record<string, unknown>;
  const labelling = list(record.labelling);
  const review = list(record.review).filter((document) => !labelling.includes(document));
  return { labelling, review };
}

/** The entries of *mode*'s documents, in the mode's document order, each in the file's order. */
export function modeEntries(
  file: ProposalsFile,
  modes: TraceModes,
  mode: TraceMode,
): RequirementEntry[] {
  return modes[mode].flatMap((document) =>
    file.requirements.filter((entry) => entry.document === document),
  );
}

/** `9 of 12 to go` over *entries*. */
export function toGoText(entries: readonly RequirementEntry[]): string {
  const toGo = entries.filter((entry) => !isDoneRequirement(entry)).length;
  return `${toGo} of ${entries.length} to go`;
}

/**
 * The two mode nodes: *no documents chosen*, or `2 documents · 9 of 24 to go`. A click on either
 * opens its picker; Mark strong is the Review node's alone.
 */
export function traceModeNodes(
  file: ProposalsFile | undefined,
  modes: TraceModes,
  generation: number,
): TraceNode[] {
  return TRACE_MODES.map((mode) => {
    const documents = modes[mode];
    let description = NO_DOCUMENTS_CHOSEN;
    if (documents.length > 0) {
      description = plural(documents.length, "document");
      if (file !== undefined) {
        description += ` · ${toGoText(modeEntries(file, modes, mode))}`;
      }
    }
    return {
      id: modeId(mode, generation),
      kind: "mode",
      label: MODE_LABELS[mode],
      description,
      tooltip:
        mode === "labelling"
          ? "**Labelling**: link the requirements of the chosen documents by hand, the ones to label first on top. Click to choose the documents; the Trace Links view shows them."
          : "**Review**: check the search's proposals for the chosen documents. Click to choose the documents; the Trace Links view shows them.",
      contextValue: `trace.mode.${mode}`,
      // The work unfolds in the Trace Links view, never under the mode node.
      collapsible: "none",
      mode,
    };
  });
}

/**
 * The Trace Links view's description: the mode shown and its documents, `Labelling · SRS Thermal
 * Protection`, or `Review · no documents chosen`.
 */
export function traceViewDescription(modes: TraceModes, mode: TraceMode): string {
  const documents = modes[mode];
  return `${MODE_LABELS[mode]} · ${
    documents.length === 0
      ? NO_DOCUMENTS_CHOSEN
      : documents.map((document) => document ?? NO_DOCUMENT).join(", ")
  }`;
}

/** The mode last shown, as `workspaceState` holds it; Review for anything else. */
export function readMode(value: unknown): TraceMode {
  return value === "labelling" ? "labelling" : "review";
}

/** The document nodes of *mode*: `12 requirements · 3 linked`, in the mode's order. */
export function traceDocuments(
  file: ProposalsFile | undefined,
  modes: TraceModes,
  mode: TraceMode,
  generation: number,
): TraceNode[] {
  const documents = modes[mode];
  return documents.map((document) => {
    const entries =
      file === undefined ? [] : file.requirements.filter((entry) => entry.document === document);
    const linked = entries.filter(isLinked).length;
    const description =
      entries.length === 0
        ? "not in the proposals file — search again"
        : `${plural(entries.length, "requirement")} · ${linked === 0 ? "none" : linked} linked`;
    return {
      id: documentId(mode, generation, document),
      kind: "document",
      label: document ?? NO_DOCUMENT,
      description,
      tooltip: `${document ?? NO_DOCUMENT} — ${MODE_LABELS[mode]}, ${description}`,
      contextValue: "trace.document",
      collapsible: entries.length === 0 ? "none" : documents.length <= 1 ? "expanded" : "collapsed",
      mode,
      document,
    };
  });
}

/** The requirement nodes of *document* in *mode*, in the file's order: `ID  Title`, its state. */
export function traceRequirements(
  file: ProposalsFile,
  mode: TraceMode,
  document: string | null,
  generation: number,
): TraceNode[] {
  return uniqueIds(
    file.requirements
      .filter((entry) => entry.document === document)
      .map((entry) => {
        // The requirement's text, readable in the view: the tooltip holds it whole, the row its first
        // words after the state.
        const tooltip = [
          `**${entry.id}@${entry.version}** — ${entry.document ?? NO_DOCUMENT}`,
          ...(entry.title !== "" ? [`**${entry.title}**`] : []),
          ...(entry.description.trim() !== "" ? [entry.description] : []),
        ];
        if (entry.chosenBecause !== null) {
          tooltip.push(`*Label first: ${entry.chosenBecause}*`);
        }
        const state = requirementState(entry);
        return {
          id: requirementId(mode, generation, entry),
          kind: "requirement",
          label: entry.title === "" ? entry.id : `${entry.id}  ${entry.title}`,
          description: [state, excerpt(entry.description)]
            .filter((part): part is string => part !== undefined && part !== "")
            .join(" · "),
          tooltip: tooltip.join("\n\n"),
          contextValue: isLinked(entry) ? "trace.requirement.linked" : "trace.requirement",
          collapsible: "collapsed",
          mode,
          requirement: entry.id,
        };
      }),
  );
}

/** The look of a decidable item in its state, in precedence order. */
function stateOf(
  item: { applied?: true; stale?: string },
  decision: string | null,
  open: { icon?: TraceIcon; description: string },
): Pick<TraceNode, "icon" | "description" | "checkbox"> & { done: boolean } {
  if (item.stale !== undefined) {
    return { icon: { id: "warning" }, description: `stale: ${item.stale}`, done: true };
  }
  if (item.applied !== undefined && decision === "reject") {
    return {
      icon: { id: "circle-slash", color: "errorForeground" },
      description: "rejected, recorded",
      done: true,
    };
  }
  if (item.applied !== undefined) {
    return { icon: { id: "check" }, description: "in the model", done: true };
  }
  if (decision === "reject") {
    return {
      icon: { id: "circle-slash", color: "errorForeground" },
      description: "rejected",
      done: false,
    };
  }
  return {
    ...open,
    checkbox: decision === null ? "unchecked" : "checked",
    done: false,
  };
}

/**
 * The row's short phrase: its first record, `names pTemp`, `like SWR-BRK-001`, `similar to
 * SWR-BRK-004` or `terms brake, temperature`; empty without a record. The tooltip keeps
 * `report.md`'s full sentences.
 */
export function evidencePhrase(records: readonly EvidenceRecord[]): string {
  const record = records[0];
  if (record === undefined) {
    return "";
  }
  if (record.kind === "name") {
    return `names ${record.phrase}`;
  }
  if (record.kind === "neighbours") {
    return record.examples.length > 0
      ? `like ${record.examples[0]}`
      : `like ${plural(record.seeded, "linked requirement")}`;
  }
  if (record.kind === "similar") {
    return record.examples.length > 0
      ? `similar to ${record.examples[0]}`
      : `similar to ${plural(record.count, "linked requirement")}`;
  }
  return `terms ${record.terms.slice(0, 3).join(", ")}`;
}

function rowNode(
  entry: RequirementEntry,
  row: CandidateRow,
  mode: TraceMode,
  parent: string,
): TraceNode {
  const sentences = row.evidence.map(evidenceSentence);
  // The band is in Review's label, the reason in the description, in both modes.
  const open = {
    icon: mode === "labelling" ? undefined : BAND_ICONS[row.band],
    description: evidencePhrase(row.evidence),
  };
  const { done, ...state } = stateOf(row, row.decision, open);
  const tooltip = [
    `**${addressText(row.target)}** — ${row.band}${row.preselected === true ? ", preselected" : ""}`,
    `Group: ${row.group.kind} ${row.group.name}, place ${row.place}`,
    ...(sentences.length > 0 ? sentences : ["no evidence"]).map((sentence) => `- ${sentence}`),
  ];
  return {
    id: `${parent}/row:${addressKey(row.target)}`,
    kind: "row",
    ...rowLabel(mode === "labelling" ? undefined : row.band, row.target),
    tooltip: tooltip.join("\n\n"),
    ...state,
    contextValue: done
      ? "trace.done"
      : row.decision === "reject"
        ? "trace.row.rejected"
        : "trace.row.open",
    collapsible: "none",
    mode,
    requirement: entry.id,
    item: { slot: "candidates", requirement: entry.id, target: row.target },
  };
}

/**
 * The rows past the first {@link TOP_ROWS} by strength, whatever their state, held by *More
 * candidates (N)*. The window is fixed for the file shown: a tick or a reject moves no row in or
 * out of it.
 */
export function hiddenRows(entry: RequirementEntry): CandidateRow[] {
  return rowsByStrength(entry.candidates).slice(TOP_ROWS);
}

function otherNode(entry: RequirementEntry, mode: TraceMode, parent: string): TraceNode {
  return {
    id: `${parent}/other`,
    kind: "other",
    label: OTHER_ELEMENT,
    description: "",
    tooltip: "Link an element the search did not propose: any element of the model.",
    icon: { id: "search" },
    contextValue: "trace.other",
    collapsible: "none",
    mode,
    requirement: entry.id,
  };
}

/** The children of *More candidates (N)*: the rows past the window, then *Any element of the model…*. */
export function traceMoreChildren(
  entry: RequirementEntry,
  mode: TraceMode,
  generation: number,
): TraceNode[] {
  const parent = `${requirementId(mode, generation, entry)}/more`;
  return uniqueIds([
    ...hiddenRows(entry).map((row) => rowNode(entry, row, mode, parent)),
    otherNode(entry, mode, parent),
  ]);
}

/**
 * The children of one requirement. A linked requirement: its links, decided rows and choices, no
 * undecided candidate. Any other: its first three rows by strength, whatever their state (a fixed
 * window), then its choices and links, then *More candidates (N)* with the rest or, with none
 * left, *Any element of the model…*.
 */
export function traceChildren(
  entry: RequirementEntry,
  mode: TraceMode,
  generation: number,
): TraceNode[] {
  const parent = requirementId(mode, generation, entry);
  const nodes: TraceNode[] = [];
  const linked = isLinked(entry);
  const decided = entry.candidates.filter((row) => row.decision !== null || isDone(row));
  // A linked requirement asks for nothing, but a decision it holds (from an older file) stays in
  // sight.
  const shown = linked ? decided : rowsByStrength(entry.candidates).slice(0, TOP_ROWS);
  for (const row of shown) {
    nodes.push(rowNode(entry, row, mode, parent));
  }
  for (const chosen of entry.chosen) {
    const { done, ...state } = stateOf(chosen, "accept", {
      icon: { id: "add" },
      // *chosen* is the label's first word.
      description: "",
    });
    nodes.push({
      id: `${parent}/chosen:${addressKey(chosen.target)}`,
      kind: "chosen",
      ...rowLabel("chosen", chosen.target),
      tooltip: `**${addressText(chosen.target)}**, chosen outside the proposals`,
      ...state,
      contextValue: done ? "trace.done" : "trace.chosen",
      collapsible: "none",
      mode,
      requirement: entry.id,
      item: { slot: "chosen", requirement: entry.id, target: chosen.target },
    });
  }
  entry.linked.forEach((link, index) => {
    const id = `${parent}/linked:${index}`;
    const item: TraceItemRef = {
      slot: "linked",
      requirement: entry.id,
      index,
      target: link.target,
    };
    if (holds(link)) {
      // A confirm applied since the search wrote the current version in place of the old member.
      const member = link.confirmed ? link.member : `${entry.id}@${entry.version}`;
      nodes.push({
        id,
        kind: "linked",
        ...rowLabel("linked", link.target),
        // The member as written, unless it only repeats the requirement above it.
        description: member === `${entry.id}@${entry.version}` ? "" : member,
        tooltip: `**${addressText(link.target)}** satisfies \`${member}\` already`,
        icon: { id: "pass-filled" },
        contextValue: "trace.linked",
        collapsible: "none",
        mode,
        requirement: entry.id,
        item,
      });
      return;
    }
    const decision = link.decision ?? null;
    const { done, ...state } = stateOf(link, decision, {
      icon: { id: "question" },
      description: `${link.member} → ${entry.id}@${entry.version}`,
    });
    nodes.push({
      id,
      kind: "awaiting",
      ...rowLabel("awaiting", link.target),
      tooltip:
        `**${addressText(link.target)}** satisfies \`${link.member}\`; ticked, Apply confirms ` +
        `it at version ${entry.version}`,
      ...state,
      contextValue: done ? "trace.done" : "trace.awaiting",
      collapsible: "none",
      mode,
      requirement: entry.id,
      item,
    });
  });
  if (!linked) {
    const hidden = hiddenRows(entry);
    if (hidden.length === 0) {
      nodes.push(otherNode(entry, mode, parent));
    } else {
      nodes.push({
        id: `${parent}/more`,
        kind: "more",
        label: `More candidates (${hidden.length})`,
        description: "",
        tooltip: "The search's other proposals for this requirement, and any element of the model.",
        contextValue: "trace.more",
        // Open when it holds a decision, so nothing decided is out of sight.
        collapsible: hidden.some((row) => row.decision !== null || isDone(row))
          ? "expanded"
          : "collapsed",
        mode,
        requirement: entry.id,
      });
    }
  }
  return uniqueIds(nodes);
}

/** The pending decisions by what Apply will do with them: ticks (accepts, choices, confirms) and rejections. */
export interface PendingCounts {
  ticked: number;
  rejected: number;
}

export function pendingCounts(file: ProposalsFile): PendingCounts {
  const counts: PendingCounts = { ticked: 0, rejected: 0 };
  for (const entry of file.requirements) {
    for (const row of entry.candidates) {
      if (isPending(row.decision !== null, row)) {
        counts[row.decision === "reject" ? "rejected" : "ticked"] += 1;
      }
    }
    for (const chosen of entry.chosen) {
      counts.ticked += isPending(true, chosen) ? 1 : 0;
    }
    for (const link of entry.linked) {
      counts.ticked += isPending((link.decision ?? null) !== null, link) ? 1 : 0;
    }
  }
  return counts;
}

/** The pending line on *Trace Links* and the view's message: `2 ticked, 1 rejected, not yet written`, or nothing. */
export function pendingLine(file: ProposalsFile | undefined): string | undefined {
  if (file === undefined) {
    return undefined;
  }
  const { ticked, rejected } = pendingCounts(file);
  const parts = [
    ...(ticked > 0 ? [`${ticked} ticked`] : []),
    ...(rejected > 0 ? [`${rejected} rejected`] : []),
  ];
  return parts.length === 0 ? undefined : `${parts.join(", ")}, not yet written`;
}

/**
 * The always-visible row at the top of the Trace Links view while *file* holds unwritten decisions
 * (the title bar's icons show only on hover): `Apply 3 ticked links…`, or the rejections when
 * nothing is ticked; nothing when nothing is pending.
 */
export function applyRowLabel(file: ProposalsFile | undefined): string | undefined {
  if (file === undefined) {
    return undefined;
  }
  const { ticked, rejected } = pendingCounts(file);
  if (ticked > 0) {
    return `Apply ${plural(ticked, "ticked link")}…`;
  }
  return rejected > 0 ? `Apply ${plural(rejected, "rejection")}…` : undefined;
}

/** A line of the Trace Links view's top level that is not a document: the Apply row or the unreadable file's message. */
export type TraceLine = { kind: "apply"; text: string } | { kind: "message"; text: string };

/**
 * The Trace Links view's top level: the Apply row while anything waits (from the same
 * `pendingCounts` as {@link pendingLine}, so the two never disagree), then the unreadable file's
 * message, then *mode*'s documents.
 */
export function traceViewRoot(
  file: ProposalsFile | undefined,
  refusal: string | undefined,
  modes: TraceModes,
  mode: TraceMode,
  generation: number,
): (TraceNode | TraceLine)[] {
  const lines: TraceLine[] = [];
  const apply = applyRowLabel(file);
  if (apply !== undefined) {
    lines.push({ kind: "apply", text: apply });
  }
  if (refusal !== undefined) {
    lines.push({ kind: "message", text: unreadableMessage(refusal) });
  }
  return [...lines, ...traceDocuments(file, modes, mode, generation)];
}

/** The one node shown for a file the panel cannot read. */
export function unreadableMessage(reason: string): string {
  return `The proposals file cannot be read: ${reason}. Search again.`;
}

// --- The CLI's answers --------------------------------------------------------------

/** `swcraft experiment trace --documents`'s answer version this extension reads. */
export const TRACE_DOCUMENTS_SCHEMA_VERSION = 1;

/** One row of the Documents table (`trace_search.documents_table`). */
export interface TraceDocumentRow {
  document: string | null;
  inScope: number;
  stubs: number;
  linked: number;
  awaitingOnly: number;
  withoutLink: number;
  rejectedEarlier: number;
  pass: "review" | "labelling" | null;
}

/** The parsed `--documents` answer. */
export interface TraceDocuments {
  /** Project-relative POSIX name of the proposals file a search would replace, `null` when none. */
  proposals: string | null;
  documents: TraceDocumentRow[];
}

function configArgument(config: string | undefined): string[] {
  const file = config?.trim() ?? "";
  return file === "" ? [] : [`--config=${file}`];
}

/** `swcraft experiment trace --documents --project <manifest> [--config=<file>] --format json`. */
export function buildTraceDocumentsArgv(manifestRelPath: string, config?: string): string[] {
  return [
    "experiment",
    "trace",
    "--documents",
    "--project",
    manifestRelPath,
    ...configArgument(config),
    "--format",
    "json",
  ];
}

/** Parse the `--documents` answer, or throw a user-facing sentence (a version it does not read by name). */
export function parseTraceDocuments(stdout: string): TraceDocuments {
  let document: unknown;
  try {
    document = JSON.parse(stdout);
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error);
    throw new Error(`could not parse the documents answer as JSON: ${detail}`);
  }
  if (!isObject(document) || document.kind !== "trace-documents") {
    throw new Error("the documents answer is not a trace-documents document.");
  }
  if (document.schemaVersion !== TRACE_DOCUMENTS_SCHEMA_VERSION) {
    throw new Error(
      `unsupported documents answer schemaVersion ${JSON.stringify(document.schemaVersion) ?? "undefined"} ` +
        `(this extension reads ${TRACE_DOCUMENTS_SCHEMA_VERSION}). The SWCraft CLI and the installed ` +
        "extension are different versions.",
    );
  }
  const proposals = document.proposals;
  if (proposals !== null && typeof proposals !== "string") {
    throw new Error("the documents answer's `proposals` is neither a name nor null.");
  }
  if (!Array.isArray(document.documents)) {
    throw new Error("the documents answer has no `documents` list.");
  }
  const rows = document.documents.map((row, index) => {
    const fine =
      isObject(row) &&
      (row.document === null || typeof row.document === "string") &&
      ["inScope", "stubs", "linked", "awaitingOnly", "withoutLink", "rejectedEarlier"].every(
        (key) => Number.isInteger(row[key]),
      ) &&
      [null, "review", "labelling"].includes(row.pass as string | null);
    if (!fine) {
      throw new Error(`the documents answer's row ${index} is not a Documents table row.`);
    }
    return row as unknown as TraceDocumentRow;
  });
  return { proposals, documents: rows };
}

/** The rows a search can label or review: the ones in scope. */
export function documentsInScope(answer: TraceDocuments): TraceDocumentRow[] {
  return answer.documents.filter((row) => row.pass !== null);
}

/** The modes kept to the documents in scope: a remembered document no longer in scope is dropped. */
export function modesInScope(modes: TraceModes, answer: TraceDocuments): TraceModes {
  const scope = documentsInScope(answer).map((row) => row.document);
  return withTheGroupWhole(
    {
      labelling: modes.labelling.filter((document) => scope.includes(document)),
      review: modes.review.filter((document) => scope.includes(document)),
    },
    scope,
  );
}

/**
 * *modes* with the no-document group left out of Labelling unless Labelling holds every document
 * of *scope* (the group is labelled only with every document, `--labelling=all`). Otherwise a
 * Review choice, or a document new in scope, would be labelled by `"all"` while shown elsewhere or
 * nowhere.
 */
function withTheGroupWhole(modes: TraceModes, scope: ReadonlyArray<string | null>): TraceModes {
  return modes.labelling.includes(null) &&
    !scope.every((document) => modes.labelling.includes(document))
    ? { ...modes, labelling: modes.labelling.filter((document) => document !== null) }
    : modes;
}

/** One item of a mode's document picker. */
export interface ModePickItem {
  label: string;
  description: string;
  detail?: string;
  picked: boolean;
  document: string | null;
}

/** The picker's title for *mode*. */
export function modePickerTitle(mode: TraceMode): string {
  return `${MODE_LABELS[mode]} — which documents?`;
}

/** Shown under the no-document group in the Labelling picker. */
export const GROUP_ONLY_WITH_ALL = "labelled only together with every document";

/** Shown when the no-document group was ticked for Labelling without every document. */
export const GROUP_LEFT_OUT =
  "SWCraft: requirements with no document are labelled only together with every document; the group was left out.";

function otherMode(mode: TraceMode): TraceMode {
  return mode === "labelling" ? "review" : "labelling";
}

/**
 * The picker's items for *mode*: every document in scope, those with no link first, each
 * `12 requirements · none linked`, ticked when the mode holds it, and marked `in Review` (or `in
 * Labelling`) when the other mode does; choosing it moves it.
 */
export function modePickItems(
  answer: TraceDocuments,
  modes: TraceModes,
  mode: TraceMode,
): ModePickItem[] {
  const rows = documentsInScope(answer);
  const ordered = [
    ...rows.filter((row) => row.linked === 0),
    ...rows.filter((row) => row.linked > 0),
  ];
  const other = otherMode(mode);
  return ordered.map((row) => {
    const linked = row.linked === 0 ? "none linked" : `${row.linked} linked`;
    const awaiting = row.awaitingOnly > 0 ? `, ${row.awaitingOnly} awaiting confirmation` : "";
    const parts = [`${plural(row.inScope, "requirement")} · ${linked}${awaiting}`];
    if (modes[other].includes(row.document)) {
      parts.push(`in ${MODE_LABELS[other]}`);
    }
    const item: ModePickItem = {
      label: row.document ?? NO_DOCUMENT,
      description: parts.join(" · "),
      picked: modes[mode].includes(row.document),
      document: row.document,
    };
    if (mode === "labelling" && row.document === null) {
      item.detail = GROUP_ONLY_WITH_ALL;
    }
    return item;
  });
}

/**
 * The modes after *picked* was chosen for *mode*: that mode's set replaced, in the answer's order,
 * and every chosen document gone from the other mode. The no-document group is labelled only with
 * every document in scope (the search's `pass_of`); ticked for Labelling without them it is left
 * out and *leftOut* says so.
 */
export function chooseDocuments(
  modes: TraceModes,
  mode: TraceMode,
  picked: ReadonlyArray<string | null>,
  answer: TraceDocuments,
): { modes: TraceModes; leftOut: boolean } {
  const scope = documentsInScope(answer).map((row) => row.document);
  let chosen = scope.filter((document) => picked.includes(document));
  let leftOut = false;
  if (
    mode === "labelling" &&
    chosen.includes(null) &&
    !scope.every((document) => chosen.includes(document))
  ) {
    chosen = chosen.filter((document) => document !== null);
    leftOut = true;
  }
  const other = otherMode(mode);
  const next = {
    [mode]: chosen,
    [other]: modes[other].filter((document) => !chosen.includes(document)),
  } as unknown as TraceModes;
  return { modes: withTheGroupWhole(next, scope), leftOut };
}

/** `--labelling`'s value for the modes: `"all"` when Labelling holds the no-document group, else its documents. */
export function labellingArgumentOf(modes: TraceModes): "all" | string[] {
  return modes.labelling.includes(null)
    ? "all"
    : modes.labelling.filter((document): document is string => document !== null);
}

/**
 * Whether a choice must search: no file read; a chosen document whose entries carry another pass
 * than its mode; or a chosen document with requirements in scope and no entry in the file.
 * Otherwise the choice only changes what is shown.
 */
export function searchNeeded(
  file: ProposalsFile | undefined,
  modes: TraceModes,
  answer: TraceDocuments,
): boolean {
  if (modes.labelling.length === 0 && modes.review.length === 0) {
    return false;
  }
  if (file === undefined) {
    return true;
  }
  for (const mode of TRACE_MODES) {
    for (const document of modes[mode]) {
      const entries = file.requirements.filter((entry) => entry.document === document);
      if (entries.some((entry) => entry.pass !== mode)) {
        return true;
      }
      const row = answer.documents.find((candidate) => candidate.document === document);
      if (entries.length === 0 && row !== undefined && row.inScope > 0) {
        return true;
      }
    }
  }
  return false;
}

/** The decisions not applied in *document*'s entries. */
export function pendingInDocument(file: ProposalsFile, document: string | null): number {
  return pendingCount({
    ...file,
    requirements: file.requirements.filter((entry) => entry.document === document),
  });
}

/**
 * The documents a choice takes out of both modes while they hold decisions not applied: their
 * decisions would be out of sight and Apply would still carry them out, so the panel asks first.
 */
export function droppedWithDecisions(
  file: ProposalsFile | undefined,
  before: TraceModes,
  after: TraceModes,
): Array<{ document: string | null; count: number }> {
  if (file === undefined) {
    return [];
  }
  const kept = new Set([...after.labelling, ...after.review]);
  return [...before.labelling, ...before.review]
    .filter((document) => !kept.has(document))
    .map((document) => ({ document, count: pendingInDocument(file, document) }))
    .filter((dropped) => dropped.count > 0);
}

/** The question before such a choice. */
export function droppedQuestion(
  dropped: ReadonlyArray<{ document: string | null; count: number }>,
): string {
  const names = dropped.map((item) => item.document ?? NO_DOCUMENT).join(", ");
  const count = dropped.reduce((sum, item) => sum + item.count, 0);
  return `${names} ${dropped.length === 1 ? "holds" : "hold"} ${plural(count, "decision")} not applied.`;
}

export const DISCARD_THEM_BUTTON = "Discard Them";

/**
 * `swcraft experiment trace --project <manifest> [--config=<file>] [--labelling=<JSON>]
 * [--discard-pending]`. The picker's choice is `--labelling`'s JSON, one `=`-form token: it
 * overrides the configuration's `search.labelling` for this run and is never written into the file
 * (the CLI records it in the run's report).
 */
export function buildTraceSearchArgv(
  manifestRelPath: string,
  options: { config?: string; labelling?: "all" | string[]; discardPending: boolean },
): string[] {
  return [
    "experiment",
    "trace",
    "--project",
    manifestRelPath,
    ...configArgument(options.config),
    ...(options.labelling === undefined
      ? []
      : [`--labelling=${JSON.stringify(options.labelling)}`]),
    ...(options.discardPending ? ["--discard-pending"] : []),
  ];
}

const PROPOSALS_LINE_PREFIX = "Generated proposals: ";

/**
 * The proposals file a search wrote: the **last** `Generated proposals: ` line,
 * resolved against *cwd* (`experimentReportPath()`'s rule), or `undefined`.
 */
export function proposalsPathOf(stdout: string, cwd: string): string | undefined {
  let found: string | undefined;
  for (const raw of stdout.split("\n")) {
    const line = raw.endsWith("\r") ? raw.slice(0, -1) : raw;
    if (line.startsWith(PROPOSALS_LINE_PREFIX)) {
      const value = line.slice(PROPOSALS_LINE_PREFIX.length).trim();
      if (value !== "") {
        found = value;
      }
    }
  }
  return found === undefined ? undefined : path.resolve(cwd, found);
}

/** One Apply run: the dry run, or the applied run bound to the previewed plan by `--expect-plan`. */
export type TraceApplyRunOptions = { dryRun: true } | { dryRun: false; expectPlan: string };

/**
 * `swcraft experiment trace --apply=<file> --project <manifest>` and `--dry-run` or
 * `--expect-plan=<digest>`, then `--report=<report>` when given: the absolute `.md` file the CLI
 * writes the report's Markdown form to (`importReportOption()` throws for any other path). *file*
 * must be absolute, so the CLI never reads it against another directory; the `=` form keeps a value
 * starting with `-` one token.
 */
export function buildTraceApplyArgv(
  manifestRelPath: string,
  file: string,
  options: TraceApplyRunOptions & { report?: string },
): string[] {
  if (!path.isAbsolute(file)) {
    throw new Error(
      `buildTraceApplyArgv: the proposals file must be an absolute path, got '${file}'`,
    );
  }
  const argv = ["experiment", "trace", `--apply=${file}`, "--project", manifestRelPath];
  argv.push(options.dryRun ? "--dry-run" : `--expect-plan=${options.expectPlan}`);
  if (options.report !== undefined) {
    argv.push(importReportOption(options.report));
  }
  return argv;
}

/** Apply's six counts and its file count, from its `Summary:` line. */
export interface ApplySummary {
  written: number;
  confirmed: number;
  rejected: number;
  alreadyDone: number;
  stale: number;
  notWritten: number;
  undecided: number;
  files: number;
}

const APPLY_SUMMARY_LINE =
  /^Summary: (\d+) written, (\d+) confirmed, (\d+) rejected, (\d+) already done, (\d+) stale, (\d+) not written, (\d+) undecided; (\d+) files\r?$/gm;
const APPLY_PLAN_LINE = /^Plan: (sha256:[0-9a-f]{64})\r?$/gm;
const NO_CHANGE_LINE = /^No change\.\r?$/m;

/** The `Summary:` line's counts when exactly one line matches, else `undefined` (`summaryOf()`'s rule). */
export function applySummaryOf(stdout: string): ApplySummary | undefined {
  const matches = [...stdout.matchAll(APPLY_SUMMARY_LINE)];
  if (matches.length !== 1) {
    return undefined;
  }
  const [written, confirmed, rejected, alreadyDone, stale, notWritten, undecided, files] =
    matches[0].slice(1).map(Number);
  return { written, confirmed, rejected, alreadyDone, stale, notWritten, undecided, files };
}

/** The plan digest when exactly one `Plan:` line is printed, else `undefined`. */
export function applyPlanOf(stdout: string): string | undefined {
  const matches = [...stdout.matchAll(APPLY_PLAN_LINE)];
  return matches.length === 1 ? matches[0][1] : undefined;
}

/** Whether the report carries the `No change.` line: the plan holds no operation. */
export function applyNoChange(stdout: string): boolean {
  return NO_CHANGE_LINE.test(stdout);
}

/** The title of the Apply report's tab, the imports' rendered tab; the next Apply report reuses it. */
export const APPLY_REPORT_TITLE = "Trace Apply Report";

/** The information message after an applied run, read off its own `Summary:` line. */
export function applyDoneMessage(summary: ApplySummary): string {
  const parts: string[] = [];
  const links = summary.written + summary.confirmed;
  if (links > 0) {
    parts.push(`wrote ${plural(links, "link")} into ${plural(summary.files, "file")}`);
  }
  if (summary.rejected > 0) {
    parts.push(`recorded ${plural(summary.rejected, "rejection")}`);
  }
  if (summary.alreadyDone > 0) {
    parts.push(`marked ${summary.alreadyDone} already done`);
  }
  if (summary.stale > 0) {
    parts.push(`marked ${summary.stale} stale`);
  }
  if (summary.notWritten > 0) {
    parts.push(`left ${plural(summary.notWritten, "link")} to add by hand`);
  }
  return `SWCraft: ${parts.length === 0 ? "nothing was applied" : parts.join("; ")}.`;
}

/**
 * Apply's own lines in the imports' dry-run sequence (`traceApplyFlow()`), in place of the imports'
 * wording: a run stopped by a signal, a failed applied run's last line, and the line of one applied
 * before its `--report` file failed, recognised by the CLI's own `Error: the decisions were
 * applied` (`cli/report_files.py`'s `write_report_file()` sentence for an applied `--apply`).
 */
export const TRACE_APPLY_WORDING: AppliedRunWording = {
  stopped: "Apply stopped without a word (stopped by a signal).",
  incomplete: "Apply did not complete. Check what it changed with Change Report or git status.",
  reportFailed:
    "The decisions were applied, but the report could not be shown. Check what they changed with Change Report or git status.",
  appliedError: /^Error: the decisions were applied\b/m,
};

/** The note under a dry run whose `Summary:` or `Plan:` line cannot be read: no Write is offered. */
export const TRACE_APPLY_UNREADABLE_LINE =
  "The dry run's summary or plan could not be read, so there is nothing to write from it. Nothing was written.";

/**
 * What an Apply dry run came to, as the preview tab shows it: *write*, its report with **Write** and
 * **Cancel** and the plan digest the applied run is bound to; *noChange*, the report saying *No
 * change.* with no button; *shown*, a refused run or one whose `Summary:` or `Plan:` line cannot be
 * read ({@link TRACE_APPLY_UNREADABLE_LINE}), with no button either.
 */
export type TraceApplyPreview =
  | { kind: "write"; report: ImportReport; plan: string }
  | { kind: "noChange"; report: ImportReport }
  | { kind: "shown"; report: ImportReport };

/**
 * *run*, an Apply dry run, as its preview: the imports' own dry-run outcome
 * (`importDryRunOutcome()` with {@link TRACE_APPLY_WORDING}), then *No change.* checked before the
 * plan is read, then exactly one `Summary:` and one `Plan:` line for Write.
 */
export function traceApplyPreview(run: CapturedRun, executable: string): TraceApplyPreview {
  const outcome = importDryRunOutcome(run, executable, TRACE_APPLY_WORDING);
  if (outcome.kind !== "report") {
    return { kind: "shown", report: outcome.report };
  }
  if (applyNoChange(run.stdout)) {
    return { kind: "noChange", report: outcome.report };
  }
  const plan = applyPlanOf(run.stdout);
  if (applySummaryOf(run.stdout) === undefined || plan === undefined) {
    return {
      kind: "shown",
      report: { ...outcome.report, notes: [...outcome.report.notes, TRACE_APPLY_UNREADABLE_LINE] },
    };
  }
  return { kind: "write", report: outcome.report, plan };
}

// --- The pending question before a search -------------------------------------------

export const PENDING_APPLY_BUTTON = "Apply…";
export const PENDING_DISCARD_BUTTON = "Discard and Search";

/** The modal's message: the count, or the file that cannot be read. */
export function pendingQuestion(count: number | undefined): string {
  if (count === undefined) {
    return "The proposals file cannot be read, so it may hold decisions not applied.";
  }
  return `${plural(count, "decision")} in the proposals file ${count === 1 ? "is" : "are"} not applied.`;
}

/** The CLI refused the search over the previous proposals file (its pending guard). */
export function searchRefusedPending(stderr: string): boolean {
  return /not applied\. Apply|cannot be read as a proposals file|changed while the search ran/.test(
    stderr,
  );
}

export const SEARCH_REFUSED_PENDING =
  "The search refused to overwrite the proposals file: it holds decisions not applied, cannot be read as a proposals file, or changed meanwhile (see the SWCraft output channel). Discard it and search?";

/** A choice refused after the dropped-documents question: their decisions still wait. */
export const DROPPED_STILL_PENDING =
  "SWCraft: those documents still hold decisions not applied, so the choice was not made; nothing else changed.";
export const STILL_PENDING =
  "SWCraft: some decisions are still not applied; see the dry-run report. The search did not run.";

// --- The view's busy hold around a flow -------------------------------------------

/**
 * Run *flow* with the view busy: *setBusy(true)* before, *setBusy(false)* in `finally` (a throw
 * included) unless *reserved*: a caller holding busy for a longer flow of its own (the search flow
 * around its Apply) clears it itself. Answers what *flow* answered.
 */
export async function holdingTraceBusy<T>(
  setBusy: (busy: boolean) => void,
  reserved: boolean,
  flow: () => Promise<T>,
): Promise<T> {
  setBusy(true);
  try {
    return await flow();
  } finally {
    if (!reserved) {
      setBusy(false);
    }
  }
}

// --- The messages the adapter shows ---------------------------------------------

export const BUSY_REFUSAL = "SWCraft: a run is in progress; try again when it ends.";
export const CHANGED_ON_DISK =
  "SWCraft: the proposals file changed on disk (a search or an Apply ran); the view was reloaded and your last change was not saved.";
export const DIRTY_REFUSAL =
  "SWCraft: save or revert the proposals file first; it has unsaved changes in an editor.";

/** The refusal when the proposals file is open with unsaved changes, else `undefined`. */
export function isDirtyRefusal(dirty: boolean): string | undefined {
  return dirty ? DIRTY_REFUSAL : undefined;
}

/** *Clear*'s modal. */
export function clearQuestion(count: number): string {
  return `Clear ${plural(count, "decision")}?`;
}

// --- Where an element is ------------------------------------------------------------

/** The layer of each target kind, and the collection of the two owned kinds. */
const KIND_LAYERS: Readonly<Record<string, string>> = {
  swc: "swc",
  port: "swc",
  runnable: "swc",
  interface: "interfaces",
  composition: "compositions",
  task: "tasks",
  processor: "hardware",
  core: "hardware",
  memory: "hardware",
  partition: "hardware",
};
const MEMBER_COLLECTIONS: Readonly<Record<string, string>> = {
  port: "ports",
  runnable: "runnables",
};
const HARDWARE_KINDS: ReadonlySet<string> = new Set(["processor", "core", "memory", "partition"]);

/** Where an address is: the file and line, and whether the member itself was found. */
export interface TraceLocation extends Location {
  /** `false` when a port or runnable was not found and the line is the component's identity. */
  memberFound: boolean;
}

/**
 * The file and line of *address* through navigation's own functions: the storage rule
 * (`resolveElement`), then, for a port or runnable, the member's identity line in the component's
 * file (`locateSubject`), falling back to the component's identity line. A hardware element whose
 * file holds another kind is no answer. `undefined` when nothing is found.
 */
export function traceElementLocation(
  refs: References,
  address: TraceAddress,
  modelRoot: string,
  reader: Reader,
): TraceLocation | undefined {
  const layer = KIND_LAYERS[address.kind];
  const facts = layer === undefined ? undefined : refs.layers.get(layer);
  if (facts === undefined) {
    return undefined;
  }
  const collection = MEMBER_COLLECTIONS[address.kind];
  const element = collection === undefined ? address.name : address.owner;
  if (element === null) {
    return undefined;
  }
  const found = resolveElement(facts, element, modelRoot, reader);
  if (found === undefined) {
    return undefined;
  }
  const text = reader.readText(found.file);
  if (text === undefined) {
    return undefined;
  }
  if (HARDWARE_KINDS.has(address.kind) && topLevelKindOf(text, facts) !== address.kind) {
    return undefined;
  }
  if (collection === undefined) {
    return { ...found, memberFound: true };
  }
  const topKind = topLevelKindOf(text, facts) ?? "swc";
  const steps: SubjectStep[] = [{ field: collection }, { member: address.name }];
  const member = locateSubject(refs, layer, topKind, text, steps);
  return member === undefined
    ? { file: found.file, line: found.line, memberFound: false }
    : { file: found.file, line: member.line, memberFound: true };
}

/** The requirement's file and identity line, or `undefined`. */
export function requirementLocation(
  refs: References,
  id: string,
  modelRoot: string,
  reader: Reader,
): Location | undefined {
  const facts = refs.layers.get("requirements");
  return facts === undefined ? undefined : resolveElement(facts, id, modelRoot, reader);
}

/** The message when an address is not in the model as saved. */
export function notFoundMessage(address: TraceAddress): string {
  const owned = address.owner === null ? "" : ` of '${address.owner}'`;
  return `SWCraft: ${address.kind} '${address.name}'${owned} is not in the model as saved; search again.`;
}

// --- An element chosen outside the list ---------------------------------------------

/** The layers a link may be written into (the search's `LINKABLE_LAYERS`, requirements aside). */
export const CHOOSABLE_LAYERS = ["swc", "interfaces", "compositions", "tasks", "hardware"] as const;

/** One element the first picker offers. */
export interface ChoiceItem {
  label: string;
  description: string;
  address: TraceAddress;
}

/**
 * The first picker's elements from a `swcraft list` answer: the five linkable layers only, in the
 * listing's order; an interface's or hardware element's kind read lexically from its file
 * (`topLevelKindOf`); an external interface (never a target), an unreadable file or an unknown kind
 * left out.
 */
export function chooseCandidates(
  listings: readonly LayerListing[],
  refs: References,
  modelRoot: string,
  reader: Reader,
): ChoiceItem[] {
  const items: ChoiceItem[] = [];
  for (const listing of listings) {
    if (!(CHOOSABLE_LAYERS as readonly string[]).includes(listing.layer)) {
      continue;
    }
    const facts = refs.layers.get(listing.layer);
    for (const name of listing.elements) {
      let kind: string | undefined;
      if (listing.layer === "swc") {
        kind = "swc";
      } else if (listing.layer === "compositions") {
        kind = "composition";
      } else if (listing.layer === "tasks") {
        kind = "task";
      } else if (facts !== undefined) {
        const found = resolveElement(facts, name, modelRoot, reader);
        const text = found === undefined ? undefined : reader.readText(found.file);
        const top = text === undefined ? undefined : topLevelKindOf(text, facts);
        if (listing.layer === "interfaces") {
          kind = top === "sender_receiver" || top === "client_server" ? "interface" : undefined;
        } else {
          kind = top !== undefined && HARDWARE_KINDS.has(top) ? top : undefined;
        }
      }
      if (kind !== undefined) {
        items.push({ label: name, description: kind, address: { kind, owner: null, name } });
      }
    }
  }
  return items;
}

/**
 * A component's members for the second picker: the component itself, then its ports and runnables
 * as `outlineOf` reads them, each kept only when `memberLinesOf` finds its identity line once.
 */
export function componentMembers(refs: References, component: string, text: string): ChoiceItem[] {
  const items: ChoiceItem[] = [
    {
      label: component,
      description: "the component itself",
      address: { kind: "swc", owner: null, name: component },
    },
  ];
  const root = outlineOf(refs, text, "swc")[0];
  for (const [group, kind] of [
    ["ports", "port"],
    ["runnables", "runnable"],
  ] as const) {
    const symbols = root?.children.find((child) => child.name === group)?.children ?? [];
    for (const symbol of symbols) {
      if (memberLinesOf(text, [group], "name", symbol.name).length === 1) {
        items.push({
          label: symbol.name,
          description: kind,
          address: { kind, owner: component, name: symbol.name },
        });
      }
    }
  }
  return items;
}

// --- The commands -------------------------------------------------------------------

/** The tree's own commands: hidden from the palette, reached from the trace nodes' menus and clicks. */
export const TRACE_VIEW_COMMANDS: readonly string[] = [
  "swcraft.traceNewSearch",
  "swcraft.traceChooseMode",
  "swcraft.traceOtherElement",
  "swcraft.traceOpenRequirement",
  "swcraft.traceMarkStrong",
  "swcraft.traceClear",
  "swcraft.traceApply",
  "swcraft.traceOpenReport",
  "swcraft.traceOpenProposals",
  "swcraft.traceAccept",
  "swcraft.traceReject",
  "swcraft.traceUndoReject",
  "swcraft.traceOpenElement",
  "swcraft.traceChooseElement",
  "swcraft.traceRemoveChoice",
];

/** The view's commands that spawn nothing: they edit the proposals file or open files. */
export const TRACE_NON_SPAWNING_COMMANDS: readonly string[] = [
  "swcraft.traceOpenRequirement",
  "swcraft.traceMarkStrong",
  "swcraft.traceClear",
  "swcraft.traceOpenReport",
  "swcraft.traceOpenProposals",
  "swcraft.traceAccept",
  "swcraft.traceReject",
  "swcraft.traceUndoReject",
  "swcraft.traceOpenElement",
  "swcraft.traceRemoveChoice",
];

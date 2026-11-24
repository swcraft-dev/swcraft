// Pure logic of the SWCraft entries in the Problems panel and their two quick
// fixes: the reading of `swcraft check-model --format json`, the place of each
// finding in the model's files, and the link edit a trace warning offers.
//
// No `vscode` import: it runs under plain `node --test`. The collection, the
// code-action provider and the run live in commands.ts.
//
// The findings are the CLI's, verbatim. The CLI names no file, so each subject
// is placed lexically with navigation's scanner (`resolveElement`, then
// `locateSubject`), reading the saved files; a placement decides nothing about
// the model. A fallback lands on the element's identity line, the manifest, or
// line 0 of a refused element file (`placeSubject()`, `placeFinding()`,
// `placeRefusals()`) and never changes the message.
//
// A quick fix is one replacement of a link member's value (`REQ-12@3` or
// `REQ-12` → `REQ-12@4`), offered as an edit of the open buffer; this module
// writes nothing.

import * as path from "node:path";

import type { ChildCapture } from "./commandCore";
import { PIN_REFUSAL_PREFIX } from "./helpLinks";
import {
  DIFFERENT_RELEASES,
  itemScalarAt,
  itemValueRange,
  keyPathLine,
  type LayerFacts,
  locateSubject,
  type Reader,
  type References,
  resolveElement,
  type SubjectStep,
  splitLines,
  topLevelKindOf,
  topLevelScalar,
  versionedLinkId,
} from "./navigationCore";

export type { SubjectStep } from "./navigationCore";

/** The `schemaVersion` of `swcraft check-model --format json` this reads (`cli.CHECK_MODEL_SCHEMA_VERSION`). */
export const CHECK_SCHEMA_VERSION = 1;

/** What a refused run's entry and closing line say when nothing else says why. */
const NO_DOCUMENT = "swcraft check-model ended without a document of its findings";

/**
 * The manifest's pin line, found lexically: the key at column 0, quoted or not
 * (a commented, flow-style or indented key does not match). Not navigationCore's
 * scanner, which reads no quoted key and answers nothing for a duplicated key,
 * where the loader keeps the last line, the one `manifestPlace()` takes.
 */
const PIN_LINE = /^["']?toolchain_version["']?\s*:/;

/**
 * The rule of an undeclared manifest key (`cli.MANIFEST_UNKNOWN_KEY_RULE`): a
 * finding with no subject whose `target` is the top-level key it sits under.
 */
export const MANIFEST_UNKNOWN_KEY_RULE = "manifest.unknown-key";

/** *key* at column 0 of the manifest, quoted or not: {@link PIN_LINE}'s reading for any key. */
function manifestKeyLine(key: string): RegExp {
  const escaped = key.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return new RegExp(`^["']?${escaped}["']?\\s*:`);
}

/** The warning a stated, stale link raises: the quick fix confirms it. */
export const CONFIRM_LINK_RULE = "trace.link-needs-confirmation";

/** The warning a bare link raises: the quick fix states the version. */
export const STATE_VERSION_RULE = "trace.link-version-not-stated";

/** One subject of a finding: the layer, the element (absent for a layer-wide one), the path. */
export interface CheckSubject {
  layer: string;
  element?: string;
  /** Absent means `[]`: the subject is the element itself. */
  path?: SubjectStep[];
}

/** One finding of the run, as the CLI wrote it. */
export interface CheckFinding {
  rule: string;
  severity: "error" | "warning";
  message: string;
  subjects: CheckSubject[];
  target: string | null;
}

/** The document: errors first, then warnings, each in the CLI's own order. */
export interface CheckDocument {
  findings: CheckFinding[];
}

/** A `check-model` document the extension cannot read: the message names what is wrong. */
export class CheckDocumentError extends Error {
  constructor(message: string) {
    super(`swcraft check-model --format json: ${message}`);
    this.name = "CheckDocumentError";
  }
}

/** A JSON object: not `null`, not an array. */
export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/**
 * The error a malformed subject throws: each reader of `Subject.to_json`'s form
 * names its own document (`check-model`'s {@link CheckDocumentError} by default).
 */
export type SubjectShapeFailure = (message: string) => Error;

const checkDocumentFailure: SubjectShapeFailure = (message) => new CheckDocumentError(message);

/** One path step: an object with exactly one of `field`, `member`, `key`; a field or key a string. */
function parseStep(raw: unknown, where: string, fail: SubjectShapeFailure): SubjectStep {
  if (!isRecord(raw) || Object.keys(raw).length !== 1) {
    throw fail(`${where} must be an object with exactly one key`);
  }
  if ("member" in raw) {
    return { member: raw.member };
  }
  if (typeof raw.field === "string") {
    return { field: raw.field };
  }
  if (typeof raw.key === "string") {
    return { key: raw.key };
  }
  throw fail(`${where} must be a string field, a member or a string key`);
}

/**
 * One subject in `Subject.to_json`'s form — the layer, the element (absent for
 * a layer-wide one) and the path — read by `check-model`'s document and by the
 * Resources view's (`resourceCore.ts`), each throwing its own error (*fail*).
 */
export function parseSubject(
  raw: unknown,
  where: string,
  fail: SubjectShapeFailure = checkDocumentFailure,
): CheckSubject {
  if (!isRecord(raw) || typeof raw.layer !== "string") {
    throw fail(`${where} must carry a string layer`);
  }
  const subject: CheckSubject = { layer: raw.layer };
  if ("element" in raw) {
    if (typeof raw.element !== "string") {
      throw fail(`${where}.element must be a string`);
    }
    subject.element = raw.element;
  }
  if ("path" in raw) {
    if (!Array.isArray(raw.path)) {
      throw fail(`${where}.path must be an array`);
    }
    subject.path = raw.path.map((step: unknown, index: number) =>
      parseStep(step, `${where}.path[${index}]`, fail),
    );
  }
  return subject;
}

function parseFinding(raw: unknown, where: string): CheckFinding {
  if (!isRecord(raw)) {
    throw new CheckDocumentError(`${where} must be an object`);
  }
  if (typeof raw.rule !== "string" || typeof raw.message !== "string") {
    throw new CheckDocumentError(`${where} must carry a string rule and message`);
  }
  if (raw.severity !== "error" && raw.severity !== "warning") {
    throw new CheckDocumentError(`${where}.severity must be "error" or "warning"`);
  }
  if (!Array.isArray(raw.subjects)) {
    throw new CheckDocumentError(`${where}.subjects must be an array`);
  }
  if (raw.target !== null && typeof raw.target !== "string") {
    throw new CheckDocumentError(`${where}.target must be a string or null`);
  }
  return {
    rule: raw.rule,
    severity: raw.severity,
    message: raw.message,
    subjects: raw.subjects.map((subject: unknown, index: number) =>
      parseSubject(subject, `${where}.subjects[${index}]`),
    ),
    target: raw.target,
  };
}

/**
 * Parse `swcraft check-model --format json`'s stdout. Throws a
 * {@link CheckDocumentError} on invalid JSON, a `schemaVersion` other than
 * {@link CHECK_SCHEMA_VERSION} (the message names the one it got — the
 * toolchain and the extension are from different releases), or a finding that
 * is not the shape the CLI writes. Nothing is re-derived: the severity is the
 * document's own.
 */
export function parseCheckDocument(json: string): CheckDocument {
  let raw: unknown;
  try {
    raw = JSON.parse(json);
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error);
    throw new CheckDocumentError(`not JSON: ${detail}`);
  }
  if (!isRecord(raw)) {
    throw new CheckDocumentError("the document is not an object");
  }
  if (raw.schemaVersion !== CHECK_SCHEMA_VERSION) {
    throw new CheckDocumentError(
      `schemaVersion ${JSON.stringify(raw.schemaVersion)} is not the ${CHECK_SCHEMA_VERSION} ` +
        `this extension reads — ${DIFFERENT_RELEASES}`,
    );
  }
  if (!Array.isArray(raw.findings)) {
    throw new CheckDocumentError("findings must be an array");
  }
  return {
    findings: raw.findings.map((finding: unknown, index: number) =>
      parseFinding(finding, `findings[${index}]`),
    ),
  };
}

// --- placement ----------------------------------------------------------------

/** A place in a file: a line and the columns `[start, end)` on it. */
export interface Place {
  file: string;
  line: number;
  start: number;
  end: number;
}

/** One Problems entry: its place, the CLI's severity, message and rule, its further places. */
export interface Placement extends Place {
  severity: "error" | "warning";
  message: string;
  /** The rule id; absent for a refused run, which has no rule. */
  code?: string;
  /**
   * The finding's `target` — for the two link warnings, the requirement the
   * link names; absent when the finding has none. The quick fix offers its
   * edit only while the line still links to this requirement.
   */
  target?: string;
  /** The finding's further subjects, each placed the same way. */
  related: Place[];
}

/** *line* of *text* whole, as columns: `[0, 0)` when the text is absent or has no such line. */
function wholeLine(text: string | undefined, line: number): { start: number; end: number } {
  const raw = text === undefined ? undefined : splitLines(text)[line];
  return { start: 0, end: raw === undefined ? 0 : raw.length };
}

/**
 * The manifest's last line matching *at*, whole; line 0, whole, without *at*, or
 * when no line matches: every fallback's place.
 */
function manifestPlace(manifestFile: string, reader: Reader, at?: RegExp): Place {
  const text = reader.readText(manifestFile);
  const matches =
    at === undefined || text === undefined ? [] : splitLines(text).map((raw) => at.test(raw));
  const line = Math.max(matches.lastIndexOf(true), 0);
  return { file: manifestFile, line, ...wholeLine(text, line) };
}

/**
 * Where one subject is: its path's range, else its identity line, else the
 * manifest's line 0. Exported for the Resources view, whose budget findings
 * carry subjects of this form and open the first one's place.
 */
export function placeSubject(
  refs: References | undefined,
  subject: CheckSubject,
  modelRoot: string,
  manifestFile: string,
  reader: Reader,
): Place {
  const facts: LayerFacts | undefined = refs?.layers.get(subject.layer);
  if (refs === undefined || facts === undefined || subject.element === undefined) {
    return manifestPlace(manifestFile, reader);
  }
  const found = resolveElement(facts, subject.element, modelRoot, reader);
  const text = found === undefined ? undefined : reader.readText(found.file);
  if (found === undefined || text === undefined) {
    return manifestPlace(manifestFile, reader);
  }
  const steps = subject.path ?? [];
  const topKind = topLevelKindOf(text, facts);
  const range =
    steps.length === 0 || topKind === undefined
      ? undefined
      : locateSubject(refs, subject.layer, topKind, text, steps);
  if (range !== undefined) {
    return { file: found.file, ...range };
  }
  return { file: found.file, line: found.line, ...wholeLine(text, found.line) };
}

/**
 * The Problems entry of *finding*: placed at its first subject, its further
 * subjects as `related`. *refs* `undefined` (an artifact that could not be read)
 * places every subject at *manifestFile* line 0; so is a finding with no subject,
 * except an undeclared manifest key ({@link MANIFEST_UNKNOWN_KEY_RULE}) with a
 * string `target`, placed on that key's last column-0 line (line 0 when none).
 */
export function placeFinding(
  refs: References | undefined,
  finding: CheckFinding,
  modelRoot: string,
  manifestFile: string,
  reader: Reader,
): Placement {
  const [first, ...rest] = finding.subjects;
  const primary: Placement = {
    ...(first === undefined
      ? manifestPlace(
          manifestFile,
          reader,
          finding.rule === MANIFEST_UNKNOWN_KEY_RULE && typeof finding.target === "string"
            ? manifestKeyLine(finding.target)
            : undefined,
        )
      : placeSubject(refs, first, modelRoot, manifestFile, reader)),
    severity: finding.severity,
    message: finding.message,
    code: finding.rule,
    related: rest.map((subject) => placeSubject(refs, subject, modelRoot, manifestFile, reader)),
  };
  if (finding.target !== null) {
    primary.target = finding.target;
  }
  return primary;
}

/**
 * One line of a load refusal (`ElementParseError.render_lines`), after an
 * optional `Error: `: the kind, the file — lazy, so it ends at the first
 * `.yaml: ` and a detail quoting another file name stays in the detail — and
 * the detail.
 */
const LOAD_REFUSAL_LINE = /^Invalid (.+?) YAML in (.+?\.ya?ml): (.+)$/;

/** A detail that is an older toolchain's Pydantic header: its text is placed as before. */
const PYDANTIC_HEADER = /^\d+ validation errors? for \S+$/;

/** A YAML syntax error's detail: `line L, column C: <problem>`, both 1-based. */
const YAML_MARK_DETAIL = /^line (\d+), column \d+: /;

/** A field's detail: `<field>: <msg>`, the field keys and `[i]` indices. */
const FIELD_DETAIL = /^([^\s:]+): /;

/**
 * The 0-based line of *text* a load refusal's *detail* blames: a YAML mark's
 * line, clamped to the last line (PyYAML marks an end of stream on the empty
 * line after a final newline); a field's longest key-path prefix
 * {@link keyPathLine} finds, cut at its first index (`ports[0].interface` →
 * `ports`); else line 0.
 */
function detailLine(text: string, detail: string): number {
  const mark = YAML_MARK_DETAIL.exec(detail);
  if (mark !== null) {
    const lines = splitLines(text);
    const last = Math.max(lines.length - (lines[lines.length - 1] === "" ? 2 : 1), 0);
    return Math.min(Math.max(Number(mark[1]) - 1, 0), last);
  }
  const field = FIELD_DETAIL.exec(detail);
  if (field === null) {
    return 0;
  }
  const keyPath = field[1].split("[")[0].split(".");
  for (let length = keyPath.length; length > 0; length -= 1) {
    const line = keyPathLine(text, keyPath.slice(0, length));
    if (line !== undefined) {
      return line;
    }
  }
  return 0;
}

/**
 * *file*, a path the CLI printed, re-rooted onto *modelRoot* as the extension
 * spells it: the CLI prints the resolved path, which differs where the model
 * root holds a symbolic link (macOS's `/tmp` is `/private/tmp`). Any other file,
 * or a model root unknown or unresolvable, keeps the printed path.
 */
export function rehomeRefusedFile(
  file: string,
  modelRoot: string | undefined,
  realpath: (each: string) => string,
): string {
  if (modelRoot === undefined) {
    return file;
  }
  let real: string;
  try {
    real = realpath(modelRoot);
  } catch {
    return file;
  }
  const relative = path.relative(real, file);
  if (
    relative === "" ||
    relative === ".." ||
    relative.startsWith(`..${path.sep}`) ||
    path.isAbsolute(relative)
  ) {
    return file;
  }
  return path.join(modelRoot, relative);
}

/**
 * The entries of a run that produced no document (exit ≠ 0 and an empty
 * stdout, or a stdout that does not parse).
 *
 * Each `Invalid <kind> YAML in <file>: <detail>` line of *reason* (after an
 * optional `Error: `) is one whole-line error entry on the file it names, at
 * the path *rehome* gives ({@link detailLine}); an unreadable file goes to the
 * manifest's line 0.
 *
 * Otherwise (no such line, or an older toolchain's Pydantic header) it is one
 * entry at *manifestFile* line 0 carrying the last non-blank line of *reason*,
 * or {@link NO_DOCUMENT} when it is empty. The pin refusal
 * ({@link PIN_REFUSAL_PREFIX}) sits on the pin line ({@link PIN_LINE}) as the
 * manifest reads now; a `toolchain_version` the manifest loader refuses is
 * another refusal and stays on line 0.
 */
export function placeRefusals(
  manifestFile: string,
  reason: string,
  reader: Reader,
  rehome: (file: string) => string = (file) => file,
): Placement[] {
  const lines = reason
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line !== "");
  const loadLines = lines.flatMap((line) => {
    const message = line.startsWith("Error: ") ? line.slice("Error: ".length) : line;
    const match = LOAD_REFUSAL_LINE.exec(message);
    return match === null ? [] : [{ message, printed: match[2], detail: match[3] }];
  });
  if (loadLines.length > 0 && !loadLines.some((entry) => PYDANTIC_HEADER.test(entry.detail))) {
    return loadLines.map(({ message, printed, detail }) => {
      const file = rehome(printed);
      const text = reader.readText(file);
      if (text === undefined) {
        return { ...manifestPlace(manifestFile, reader), severity: "error", message, related: [] };
      }
      const line = detailLine(text, detail);
      return { file, line, ...wholeLine(text, line), severity: "error", message, related: [] };
    });
  }
  const message = lines.length === 0 ? NO_DOCUMENT : lines[lines.length - 1];
  return [
    {
      ...manifestPlace(
        manifestFile,
        reader,
        message.startsWith(PIN_REFUSAL_PREFIX) ? PIN_LINE : undefined,
      ),
      severity: "error",
      message,
      related: [],
    },
  ];
}

// --- the status bar's counts ------------------------------------------------------

/**
 * The command that opens and focuses the Problems view: the focus command VS Code
 * derives for `workbench.panel.markers.view`. `workbench.actions.view.problems`
 * would toggle the panel, closing it when already open.
 */
export const PROBLEMS_FOCUS_COMMAND = "workbench.panel.markers.view.focus";

/** The findings' status-bar item's hover text: what its counts are and what a click does. */
export const FINDING_COUNTS_TOOLTIP =
  "The SWCraft model check's findings at its last run. Click to open Problems.";

/** *count* and *noun*, the noun in the plural unless the count is exactly one. */
function counted(count: number, noun: string): string {
  return `${count} ${noun}${count === 1 ? "" : "s"}`;
}

/**
 * The findings' status-bar text for *placements*: `SWCraft: <e> error(s) · <w>
 * warning(s)`. Named, because VS Code's own counts sum every source. A refused
 * run is its one error.
 */
export function describeFindingCounts(placements: readonly Placement[]): string {
  let errors = 0;
  let warnings = 0;
  for (const place of placements) {
    if (place.severity === "error") {
      errors += 1;
    } else {
      warnings += 1;
    }
  }
  return `SWCraft: ${counted(errors, "error")} · ${counted(warnings, "warning")}`;
}

// --- the two quick fixes --------------------------------------------------------

/** A requirement version as a link states it: `trace.is_version`'s grammar. */
const VERSION = /^[1-9][0-9]*$/;

/**
 * The fields whose members are links to a requirement: the `versioned-list-values`
 * relations of `references.json` (`diagnosticsCore.test.ts` holds them equal).
 */
export const LINK_FIELDS: ReadonlySet<string> = new Set(["satisfies", "derived_from"]);

/** One link member on a line: the requirement id, the stated version, the value's columns. */
export interface LinkHit {
  id: string;
  /**
   * The version after `@` as written, or `null` for a bare link. Kept as text so
   * it compares exactly past 2^53.
   */
  stated: string | null;
  start: number;
  end: number;
  /** The value's quote character (`'` or `"`), or `""` for a plain scalar. */
  quote: string;
}

/**
 * The link member on *line* (0-based) of *text*, or `undefined`: a block
 * sequence scalar ({@link itemScalarAt}) directly under a link field, split by
 * {@link versionedLinkId}. Nothing for an element form (`hardware:Core0`), an
 * empty id or a malformed suffix (`@x`, `@0`, `@`: an error with no fix). The
 * columns are the value's as written, quotes included.
 */
export function linkAt(text: string, line: number): LinkHit | undefined {
  const item = itemScalarAt(text, line);
  if (item === undefined || item.path.length < 2) {
    return undefined;
  }
  if (!LINK_FIELDS.has(item.path[item.path.length - 2])) {
    return undefined;
  }
  const id = versionedLinkId(item.value);
  if (id === undefined || id === "") {
    return undefined;
  }
  let stated: string | null = null;
  if (id !== item.value) {
    const suffix = item.value.slice(id.length + 1);
    if (!VERSION.test(suffix)) {
      return undefined;
    }
    stated = suffix;
  }
  const range = itemValueRange(text, line);
  if (range === undefined) {
    return undefined;
  }
  const first = splitLines(text)[line][range.start];
  const quote = first === "'" || first === '"' ? first : "";
  return { id, stated, start: range.start, end: range.end, quote };
}

/** One quick fix: its title, and the text replacing the columns `[start, end)` of the line. */
export interface QuickFix {
  title: string;
  replacement: string;
  start: number;
  end: number;
}

/**
 * The quick fix of a diagnostic of rule *code* naming requirement *target*,
 * given *link* (what the buffer's line holds now, {@link linkAt}) and
 * *currentVersion* (the saved file's version); or `undefined`. *currentVersion*
 * is asked only once the line still links to *target*.
 *
 * {@link CONFIRM_LINK_RULE} on a stale stated link: *Confirm link to REQ-12
 * (version 4)*; {@link STATE_VERSION_RULE} on a bare link: *State the version of
 * REQ-12 (version 4)*; either replaces the value with `REQ-12@4`, quotes kept.
 * Nothing for any other rule, a buffer that drifted since the run, or a version
 * that is not a positive decimal (the loader admits `0x4`, a malformed suffix).
 */
export function quickFixFor(
  code: string,
  target: string | undefined,
  link: LinkHit | undefined,
  currentVersion: (id: string) => string | undefined,
): QuickFix | undefined {
  if (link === undefined || link.id !== target) {
    return undefined;
  }
  const version = currentVersion(link.id);
  if (version === undefined || !VERSION.test(version)) {
    return undefined;
  }
  let title: string;
  if (code === CONFIRM_LINK_RULE && link.stated !== null && link.stated !== version) {
    title = `Confirm link to ${link.id} (version ${version})`;
  } else if (code === STATE_VERSION_RULE && link.stated === null) {
    title = `State the version of ${link.id} (version ${version})`;
  } else {
    return undefined;
  }
  return {
    title,
    replacement: `${link.quote}${link.id}@${version}${link.quote}`,
    start: link.start,
    end: link.end,
  };
}

/**
 * The current version of requirement *id* as its saved file writes it (the single
 * column-0 `version:` value, as text), or `undefined` when there is none.
 */
export function requirementVersion(
  refs: References,
  id: string,
  modelRoot: string,
  reader: Reader,
): string | undefined {
  const facts = refs.layers.get("requirements");
  const found = facts === undefined ? undefined : resolveElement(facts, id, modelRoot, reader);
  const text = found === undefined ? undefined : reader.readText(found.file);
  const read = text === undefined ? undefined : topLevelScalar(text, "version");
  return read?.status === "one" ? read.value : undefined;
}

// --- one run's reading ----------------------------------------------------------

/** The `source` of every SWCraft entry in the Problems panel, and what a quick fix keys on. */
export const DIAGNOSTIC_SOURCE = "swcraft";

/** The rules whose entries carry a quick fix: the two link warnings. */
export const QUICK_FIX_RULES: ReadonlySet<string> = new Set([
  CONFIRM_LINK_RULE,
  STATE_VERSION_RULE,
]);

/**
 * `check-model`'s success sentence, verbatim (`cli.cmd_check_model`); under
 * `--format json` the CLI omits it, so the extension writes it.
 */
export const CHECK_MODEL_SUCCESS_LINE = "Validation passed (no artifacts generated).";

/** What one `check-model --format json` run gave back: the exit, and both streams whole. */
export type CheckRun = ChildCapture;

/**
 * One run, read: its findings and the channel's closing line, or a refusal and
 * its reason — the entries {@link placeRefusals} places — with a closing line
 * only when the channel would otherwise not say why (`summary` absent: stderr's
 * own `Error:` line, or the launch-failure line, is already there).
 */
export type CheckRunReading =
  | { kind: "findings"; document: CheckDocument; summary: string }
  | { kind: "refused"; reason: string; summary?: string };

/** argparse's refusal of an option an older `check-model` does not declare. */
const OLDER_TOOLCHAIN = /unrecognized arguments:.*--format/;

function plural(count: number, noun: string): string {
  return `${count} ${noun}${count === 1 ? "" : "s"}`;
}

/**
 * Read one `check-model --format json` run; the exit code is the verdict, this
 * decides only what the panel and the channel say.
 *
 * - The CLI could not be started: refused, naming *executable*.
 * - An empty stdout: refused. Exit 2 with argparse's `unrecognized arguments:`
 *   naming `--format` is an older toolchain that does not know the option
 *   (different releases); otherwise the reason is stderr, or, when stderr is
 *   empty too, a closing line naming the exit.
 * - A stdout that does not parse: refused with the parse error, also the
 *   closing line.
 * - A document: its findings; the closing line is
 *   {@link CHECK_MODEL_SUCCESS_LINE} on exit 0 (warnings alone exit 0), else
 *   the count of each class.
 */
export function readCheckRun(run: CheckRun, executable: string): CheckRunReading {
  if (run.spawnFailed) {
    return { kind: "refused", reason: `the SWCraft CLI could not be started (${executable})` };
  }
  if (run.stdout.trim() === "") {
    if (run.exitCode === 2 && OLDER_TOOLCHAIN.test(run.stderr)) {
      return {
        kind: "refused",
        reason: `swcraft check-model does not know --format json — ${DIFFERENT_RELEASES}`,
      };
    }
    if (run.stderr.trim() === "") {
      const summary =
        `${NO_DOCUMENT} ` +
        `(${run.exitCode === null ? "stopped by a signal" : `exit code ${run.exitCode}`})`;
      return { kind: "refused", reason: summary, summary };
    }
    return { kind: "refused", reason: run.stderr };
  }
  let document: CheckDocument;
  try {
    document = parseCheckDocument(run.stdout);
  } catch (error) {
    const reason = error instanceof Error ? error.message : String(error);
    return { kind: "refused", reason, summary: reason };
  }
  const errors = document.findings.filter((finding) => finding.severity === "error").length;
  const warnings = document.findings.length - errors;
  return {
    kind: "findings",
    document,
    summary:
      run.exitCode === 0
        ? CHECK_MODEL_SUCCESS_LINE
        : `${plural(errors, "error")}, ${plural(warnings, "warning")}`,
  };
}

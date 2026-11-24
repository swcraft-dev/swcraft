// Pure logic of the document links: a storage name or an element name in a model file's
// `description` values, and in an import report, as a link to the element's file. No `vscode`
// import: it runs under plain `node --test`; the providers live in commands.ts.
//
// Discovery (`descriptionLinks`, `reportLinks`) answers **candidates** from one directory scan of
// the model root (`ModelScan`) and the document's own text, reading no file, so a report of a
// thousand names costs one scan. VS Code resolves a link only when it carries no target, so the
// provider leaves it unset and `resolveCandidate` confirms the one link the user hovers or clicks.
//
// A **storage name** (`<layer directory>/[<group>/…]<name>[.yaml]`, directories matched
// longest-first, a flat layer taking no group segment) links in both document kinds. A **bare
// name** (an identifier-shaped word in a description, a single-quoted token in a model import
// report) links only by exact stem, only when exactly one layer holds that stem, never to a `~N`
// collision file and never to the description's own element. The component import report prints
// its names bare, so it links storage names only.
//
// What it does not reach: another project's storage name; an ARXML source path (`/Pkg/Name`,
// refused by the look-behind); a word in a trailing comment of a block description (block lines
// are scanned whole); a quoted report token that is no element but equals an element's stem (it
// links to that element); anything past {@link MAX_DOCUMENT_LINKS} distinct targets.

import {
  fileIdentity,
  fold,
  type LayerFacts,
  lineShapes,
  type Reader,
  type References,
  resolveElement,
  splitLines,
  topLevelScalar,
} from "./navigationCore";
import { collisionBase, isCollisionStem, layerFiles } from "./symbolCore";

/**
 * At most this many distinct targets are linked in one document; past it the rest is not linked.
 * Sized from the quoted tokens of real tools' import reports, with room for storage names and a
 * larger export.
 */
export const MAX_DOCUMENT_LINKS = 5_000;

/**
 * The shortest bare name a description or report links: a single letter (`a`,
 * `I`) is prose far more often than an element.
 */
export const MIN_BARE_NAME_LENGTH = 2;

/** The first line of `swcraft import model --report` (`arxml_import/report.py`). */
export const MODEL_REPORT_HEADER = "Import report: ";
/** The first line of `swcraft import component --report` (`component_exchange/report.py`). */
export const COMPONENT_REPORT_HEADER = "Component import report: ";

// --- the scan ---------------------------------------------------------------

/** One listed file of a layer, as the scan holds it. */
export interface ScannedFile {
  layer: string;
  file: string;
  stem: string;
  /** The model-root-relative storage name without the suffix, POSIX: `swc/application/Brake`. */
  storage: string;
}

/** One directory scan of the model root: what discovery chooses candidates from. */
export interface ModelScan {
  /** Every listed file by its exact storage name with the suffix (`interfaces/IBrake.yaml`). */
  byFileName: ReadonlyMap<string, ScannedFile>;
  /** The files of each layer by their case-folded stem — every storage-rule candidate of a name. */
  byFoldedStem: ReadonlyMap<string, ReadonlyMap<string, readonly ScannedFile[]>>;
  /** The non-collision files by their exact stem: a bare name's lookup. */
  byStem: ReadonlyMap<string, readonly ScannedFile[]>;
}

function pushTo<K, V>(map: Map<K, V[]>, key: K, value: V): void {
  const list = map.get(key);
  if (list === undefined) {
    map.set(key, [value]);
  } else {
    list.push(value);
  }
}

/** The scan of *modelRoot*: symbolCore's `layerFiles` walk, listing directories and reading no file. */
export function scanModel(refs: References, modelRoot: string, reader: Reader): ModelScan {
  const byFileName = new Map<string, ScannedFile>();
  const byFoldedStem = new Map<string, Map<string, ScannedFile[]>>();
  const byStem = new Map<string, ScannedFile[]>();
  for (const listed of layerFiles(refs, modelRoot, reader)) {
    const { facts } = listed;
    const storage = listed.container === "" ? listed.stem : `${listed.container}/${listed.stem}`;
    const scanned: ScannedFile = {
      layer: listed.layer,
      file: listed.file,
      stem: listed.stem,
      storage,
    };
    byFileName.set(storage + facts.fileSuffix, scanned);
    let layerStems = byFoldedStem.get(listed.layer);
    if (layerStems === undefined) {
      layerStems = new Map();
      byFoldedStem.set(listed.layer, layerStems);
    }
    // A `Name~2` file is a candidate of `Name` by the storage rule.
    pushTo(layerStems, fold(collisionBase(listed.stem)), scanned);
    if (!isCollisionStem(listed.stem)) {
      pushTo(byStem, listed.stem, scanned);
    }
  }
  return { byFileName, byFoldedStem, byStem };
}

// --- candidates ---------------------------------------------------------------

/**
 * What a link names, chosen from the scan alone: the element of *layer* whose
 * identity is *identity* (a bare name or a storage name without the suffix),
 * or the listed *file* (a storage name with it). `listed` is the file the
 * resolution falls back to.
 */
export type LinkCandidate =
  | { kind: "element"; layer: string; identity: string; listed: string }
  | { kind: "file"; layer: string; listed: string };

/** One link of a document: a 0-based line, its character range, and its candidate. */
export interface DiscoveredLink {
  line: number;
  start: number;
  end: number;
  candidate: LinkCandidate;
}

/** A resolved link's target: the file, and the **1-based** line its `#L<n>` fragment names. */
export interface LinkTarget {
  file: string;
  line: number;
}

function escapeRegExp(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

// A segment that cannot end in a sentence's period.
const SEGMENT = "[A-Za-z0-9_~-]+(?:\\.[A-Za-z0-9_~-]+)*";

interface StorageGrammar {
  pattern: RegExp;
  byDirectory: ReadonlyMap<string, [string, LayerFacts]>;
}

// One grammar per parsed artifact: the artifact is loaded once per activation.
const grammars = new WeakMap<References, StorageGrammar>();

/** The storage-name grammar over *refs*' layer directories, longest first. */
function storageGrammar(refs: References): StorageGrammar {
  const known = grammars.get(refs);
  if (known !== undefined) {
    return known;
  }
  const byDirectory = new Map<string, [string, LayerFacts]>();
  for (const [layer, facts] of refs.layers) {
    byDirectory.set(facts.directory, [layer, facts]);
  }
  const directories = [...byDirectory.keys()]
    .sort((left, right) => right.length - left.length || (left < right ? -1 : 1))
    .map(escapeRegExp);
  const pattern = new RegExp(
    `(?<![A-Za-z0-9_./~-])(${directories.join("|")})/((?:${SEGMENT}/)*${SEGMENT})(?![A-Za-z0-9_/~-])`,
    "g",
  );
  const grammar = { pattern, byDirectory };
  grammars.set(refs, grammar);
  return grammar;
}

const DESCRIPTION_NAME = /(?<![A-Za-z0-9_./~-])[A-Za-z_][A-Za-z0-9_]*(?![A-Za-z0-9_/~-])/g;
const REPORT_NAME = /'([A-Za-z0-9_.~-]+)'/g;

/** One storage-name match on a line: its range and its candidate, or `undefined` when it names nothing listed. */
export interface StorageNameMatch {
  start: number;
  end: number;
  candidate: LinkCandidate | undefined;
}

/**
 * Every storage name on *line*. A trailing `fileSuffix` makes it a file name, linked when the scan
 * lists exactly that file; otherwise its last segment is an identity, linked when the scan lists a
 * storage-rule candidate in that layer (preferring the file at the written storage name). A flat
 * layer's name with a group segment (`interfaces/SENDER-RECEIVER-INTERFACE/X`, an ARXML tag) names
 * nothing.
 */
export function storageNameMatches(
  line: string,
  refs: References,
  scan: ModelScan,
): StorageNameMatch[] {
  const { pattern, byDirectory } = storageGrammar(refs);
  const found: StorageNameMatch[] = [];
  for (const match of line.matchAll(pattern)) {
    const start = match.index ?? 0;
    const end = start + match[0].length;
    const [layer, facts] = byDirectory.get(match[1]) as [string, LayerFacts];
    const segments = match[2].split("/");
    found.push({ start, end, candidate: storageCandidate(layer, facts, segments, scan) });
  }
  return found;
}

function storageCandidate(
  layer: string,
  facts: LayerFacts,
  segments: readonly string[],
  scan: ModelScan,
): LinkCandidate | undefined {
  if (facts.nesting === "flat" && segments.length > 1) {
    return undefined;
  }
  const written = `${facts.directory}/${segments.join("/")}`;
  const last = segments[segments.length - 1];
  if (last.endsWith(facts.fileSuffix) && last.length > facts.fileSuffix.length) {
    const listed = scan.byFileName.get(written);
    return listed === undefined ? undefined : { kind: "file", layer, listed: listed.file };
  }
  const candidates = scan.byFoldedStem.get(layer)?.get(fold(last));
  if (candidates === undefined || candidates.length === 0) {
    return undefined;
  }
  const exact = candidates.find((candidate) => candidate.storage === written);
  return { kind: "element", layer, identity: last, listed: (exact ?? candidates[0]).file };
}

/**
 * A bare *name*'s candidate: the element whose file's stem is exactly *name* in the one layer that
 * holds it; two layers, a `~N` file only, or *own* (the document's element) name nothing.
 */
function bareCandidate(
  name: string,
  scan: ModelScan,
  own: { layer: string; identity: string } | undefined,
): LinkCandidate | undefined {
  const files = scan.byStem.get(name);
  if (files === undefined || new Set(files.map((file) => file.layer)).size !== 1) {
    return undefined;
  }
  const layer = files[0].layer;
  if (own !== undefined && own.layer === layer && own.identity === name) {
    return undefined;
  }
  return { kind: "element", layer, identity: name, listed: files[0].file };
}

function candidateKey(candidate: LinkCandidate): string {
  return candidate.kind === "file"
    ? `file\u0000${candidate.listed}`
    : `element\u0000${candidate.layer}\u0000${candidate.identity}`;
}

/**
 * The links of one document, its distinct targets capped at {@link MAX_DOCUMENT_LINKS}: a target
 * already linked links again for free; the first new target past the cap ends the links.
 */
class LinkCollector {
  readonly links: DiscoveredLink[] = [];
  private readonly seen = new Map<string, LinkCandidate>();
  full = false;

  add(line: number, start: number, end: number, candidate: LinkCandidate): void {
    if (this.full) {
      return;
    }
    const key = candidateKey(candidate);
    let shared = this.seen.get(key);
    if (shared === undefined) {
      if (this.seen.size >= MAX_DOCUMENT_LINKS) {
        this.full = true;
        return;
      }
      this.seen.set(key, candidate);
      shared = candidate;
    }
    this.links.push({ line, start, end, candidate: shared });
  }
}

/** How a document kind links bare names: the words it reads, and each word's candidate. */
interface BareRule {
  pattern: RegExp;
  lookup: (name: string) => LinkCandidate | undefined;
}

/**
 * The links of *line* from *from* to *to*: storage names first, then, when *bare* says how, the
 * bare names no storage name covers.
 */
function scanStretch(
  collector: LinkCollector,
  refs: References,
  scan: ModelScan,
  raw: string,
  line: number,
  from: number,
  to: number,
  bare: BareRule | undefined,
): void {
  const text = raw.slice(from, to);
  const covered: Array<[number, number]> = [];
  for (const match of storageNameMatches(text, refs, scan)) {
    covered.push([match.start, match.end]);
    if (match.candidate !== undefined) {
      collector.add(line, from + match.start, from + match.end, match.candidate);
    }
  }
  if (bare === undefined) {
    return;
  }
  for (const match of text.matchAll(bare.pattern)) {
    const whole = match.index ?? 0;
    const name = match[1] ?? match[0];
    const start = match[1] === undefined ? whole : whole + 1;
    const end = start + name.length;
    if (covered.some(([left, right]) => start < right && end > left)) {
      continue;
    }
    if (name.length < MIN_BARE_NAME_LENGTH) {
      continue;
    }
    const candidate = bare.lookup(name);
    if (candidate !== undefined) {
      collector.add(line, from + start, from + end, candidate);
    }
  }
}

/** Where a plain value's comment starts on *raw* at or after *from*, or the line's end. */
function commentStart(raw: string, from: number): number {
  const found = /[ \t]#/.exec(raw.slice(from));
  return found === null ? raw.length : from + found.index;
}

/**
 * The links in the `description` values of a model file of *layer*: every `description` key line
 * at any depth, its value, its continuation lines (a wrapped plain scalar) and its `|` / `>` block
 * lines. On a plain value's lines text after ` #` is a comment and not scanned; a block's lines
 * are scanned whole. Reads no file.
 */
export function descriptionLinks(
  refs: References,
  text: string,
  layer: string,
  scan: ModelScan,
): DiscoveredLink[] {
  const facts = refs.layers.get(layer);
  if (facts === undefined) {
    return [];
  }
  const identity = topLevelScalar(text, facts.identityField);
  const own = identity.status === "one" ? { layer, identity: identity.value } : undefined;
  const bare: BareRule = {
    pattern: DESCRIPTION_NAME,
    lookup: (name) => bareCandidate(name, scan, own),
  };
  const shapes = lineShapes(text);
  const raws = splitLines(text);
  const collector = new LinkCollector();
  for (let index = 0; index < shapes.length && !collector.full; index += 1) {
    const shape = shapes[index];
    if (shape.kind !== "key" || shape.key !== "description" || shape.dashes.length > 1) {
      continue;
    }
    const raw = raws[index];
    const valueStart = raw.length - shape.rest.length;
    const value = shape.rest.trim();
    const block = value.startsWith("|") || value.startsWith(">");
    const quoted = value.startsWith("'") || value.startsWith('"');
    if (!block) {
      const end = quoted ? raw.length : commentStart(raw, valueStart);
      scanStretch(collector, refs, scan, raw, index, valueStart, end, bare);
    }
    for (let next = index + 1; next < shapes.length && !collector.full; next += 1) {
      const following = shapes[next];
      if (following.kind === "blank") {
        continue;
      }
      if (following.indent <= shape.keyCol) {
        break;
      }
      if (!block && following.kind === "comment") {
        continue;
      }
      const line = raws[next];
      const end = block || quoted ? line.length : commentStart(line, 0);
      scanStretch(collector, refs, scan, line, next, 0, end, bare);
    }
  }
  return collector.links;
}

/** Which import report *firstLine* opens, or `undefined` for any other document. */
export function reportKind(firstLine: string): "model" | "component" | undefined {
  if (firstLine.startsWith(MODEL_REPORT_HEADER)) {
    return "model";
  }
  if (firstLine.startsWith(COMPONENT_REPORT_HEADER)) {
    return "component";
  }
  return undefined;
}

/**
 * The links of an import report, or none when its first line is neither `--report` header (the
 * file name is the user's, so the content decides). Storage names on every line; a model report's
 * single-quoted tokens as bare names. Reads no file.
 */
export function reportLinks(refs: References, text: string, scan: ModelScan): DiscoveredLink[] {
  const raws = splitLines(text);
  const kind = reportKind(raws[0] ?? "");
  if (kind === undefined) {
    return [];
  }
  const bare: BareRule | undefined =
    kind === "model"
      ? { pattern: REPORT_NAME, lookup: (name) => bareCandidate(name, scan, undefined) }
      : undefined;
  const collector = new LinkCollector();
  for (let index = 0; index < raws.length && !collector.full; index += 1) {
    const raw = raws[index];
    scanStretch(collector, refs, scan, raw, index, 0, raw.length, bare);
  }
  return collector.links;
}

/**
 * The target of one link, read on hover or click: the element by navigationCore's storage rule, or
 * the listed file's single column-0 identity line, 1-based as a `#L<n>` fragment wants it. A
 * refusing confirmation, or no model root (the manifest gone since), falls back to the listed
 * file's line 1.
 */
export function resolveCandidate(
  candidate: LinkCandidate,
  refs: References,
  modelRoot: string | undefined,
  reader: Reader,
): LinkTarget {
  const facts = refs.layers.get(candidate.layer);
  const fallback = { file: candidate.listed, line: 1 };
  if (facts === undefined || modelRoot === undefined) {
    return fallback;
  }
  if (candidate.kind === "element") {
    const found = resolveElement(facts, candidate.identity, modelRoot, reader);
    return found === undefined ? fallback : { file: found.file, line: found.line + 1 };
  }
  const identity = fileIdentity(facts, candidate.listed, reader);
  return identity === undefined ? fallback : { file: candidate.listed, line: identity.line + 1 };
}

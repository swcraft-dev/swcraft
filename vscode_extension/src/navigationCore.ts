// Pure logic of the editor's navigation: the key path under the cursor, the reference,
// relation or member reference it names, the file and line it resolves to, the hover card,
// the locations of an element's dependents read from `swcraft impact --format json` (the
// CLI answers who refers; this only finds each answer's file and line), and the CodeLens
// plan. No `vscode` import; the providers live in commands.ts.
//
// ## What this reads, and why that is not a model read
//
// Validation and every semantic interpretation stay behind the CLI. This module reads model
// files **lexically and read-only** (keys of decreasing indentation, one scalar on a line, a
// column-0 identity or discriminator line), resolves only the references the model contract
// declares (`editor_schema.py`'s `schemas/references.json`, never re-derived here), and
// writes nothing.
//
// ## Why there is no YAML parser
//
// The extension has no runtime dependency. The scanner meets the model's two block styles
// (indentless and indented sequences) and **refuses** everything else: flow collections,
// block and multi-line scalars, a quoted value with an escape, anchors, aliases, tags, and a
// key repeated on the selected path. A refusal answers `undefined`, never a jump to a value
// the loader does not hold (the loader keeps the **last** of a repeated key).
//
// What it does not reach: a plain scalar the loader reads as a non-string (`null`, `12`) is
// read as its text; a collection keyed by name (a `"*"` step matches a sequence item only;
// no published path steps through such a map, unchecked here); a `---` marker or directive
// above the cursor's path; and Unicode normalisation.

import * as path from "node:path";

import { type ChildCapture, codeFence, escapeMarkdown } from "./commandCore";
import { isListedView } from "./diagrams";
import type { BlockRead, ElementViews } from "./diagramsTab";

/** One layer of `references.json`, as `editor_schema.references_artifact()` writes it. */
export interface LayerFacts {
  name: string;
  token: string;
  directory: string;
  /** `"flat"`, or the placement field's name of a nesting layer (`"group"` for `swc`). */
  nesting: string;
  fileSuffix: string;
  identityField: string;
  filenameFrom: string;
  discriminator: string | null;
  /** The guide corpus's page id of the layer's reference page (`reference/model/interfaces`). */
  guidePage: string;
  /** Each top-level kind, with the anchor of its section on the layer's guide page. */
  topLevelKinds: ReadonlyArray<{
    kind: string;
    discriminatorValue: string | null;
    guideAnchor: string;
  }>;
}

/** One key path a reference edge is reached by, from one top-level kind. */
export interface ReferencePath {
  topKind: string;
  path: readonly string[];
}

/**
 * One `fieldDecl.reference` edge of the contract, the part the providers read. The row's
 * `sourceKind` and `sourceField` are not read, so not checked either: a damaged key no
 * feature uses does not turn navigation off.
 */
export interface ReferenceEdge {
  field: string;
  sourceLayer: string;
  targetLayer: string;
  targetIdentityField: string;
  paths: readonly ReferencePath[];
}

/**
 * How a relation's value names its target: each list item, each key of a map,
 * or each list item read up to its first `@` (`REQ-12@3` names `REQ-12`; an
 * item containing `:` is an element reference and names no target).
 */
export type RelationResolution = "list-values" | "map-keys" | "versioned-list-values";

/**
 * One `fieldDecl.relation` edge of the contract. The generator publishes only a relation
 * whose `targetField` is the target layer's identity field; `paths` end at the field, and
 * the member step (`"*"` for a list item, any key for a map) is the reader's.
 */
export interface RelationEdge {
  field: string;
  sourceLayer: string;
  targetLayer: string;
  /** The top-level kind the target must hold (`core`, not any `hardware` element). */
  targetKind: string;
  targetField: string;
  resolution: RelationResolution;
  paths: readonly ReferencePath[];
}

/** One declared `collection` of the contract: the outline's input. Its `field` is not read, so not checked either. */
export interface CollectionFacts {
  layer: string;
  collectionKind: "list" | "map";
  memberIdentity: "field" | "whole_value" | "composite";
  identityField: string | null;
  identityFields: readonly string[];
  memberKinds: readonly string[];
  discriminatorField: string | null;
  paths: readonly ReferencePath[];
}

/**
 * One collection a member reference may name a member of: on a top-level element of *kind*
 * in *layer*, at *path*; members compared by `identityField`, or (`null`) a whole-value list
 * of scalars compared by the item itself. The row's `id` is not read, so not checked either.
 */
export interface MemberReferenceTarget {
  layer: string;
  kind: string;
  path: readonly string[];
  identityField: string | null;
}

/**
 * One `fieldDecl.memberReference` of the contract: the value names a member of one of
 * `collections`, on the element holding the field when `via` is `null`, else on the
 * element the sibling field `via`'s own member reference reaches.
 */
export interface MemberReferenceEdge {
  field: string;
  sourceLayer: string;
  collections: readonly MemberReferenceTarget[];
  via: string | null;
  paths: readonly ReferencePath[];
}

/** The parsed artifact. A `Map`, so a layer named like an `Object` member is just a key. */
export interface References {
  layers: ReadonlyMap<string, LayerFacts>;
  references: readonly ReferenceEdge[];
  relations: readonly RelationEdge[];
  collections: readonly CollectionFacts[];
  memberReferences: readonly MemberReferenceEdge[];
}

/** A malformed `references.json`: the message names the key that is wrong. */
export class ReferencesArtifactError extends Error {
  constructor(message: string) {
    super(`references.json: ${message}`);
    this.name = "ReferencesArtifactError";
  }
}

/**
 * The file system as navigation sees it: two synchronous questions. A file is found through
 * its directory's listing only, so a location carries the name on disk, never a composed
 * spelling a case-insensitive file system happened to accept.
 */
export interface Reader {
  /** The file's text, or `undefined` when it cannot be read. */
  readText(filePath: string): string | undefined;
  /** The entry names of a directory, or `undefined` when it is none or cannot be listed. */
  listDir(directory: string): string[] | undefined;
}

/**
 * *base* for one request: each file's text and each directory's listing is asked once and
 * kept until the request ends, so every answer is drawn from one snapshot (the hover's card
 * from the text the resolver confirmed; thousands of Check Model findings in one directory
 * from one listing). A new reader per request, so the next sees files added in between.
 */
export function requestReader(base: Reader): Reader {
  const texts = new Map<string, string | undefined>();
  const listings = new Map<string, string[] | undefined>();
  return {
    readText: (filePath) => {
      if (!texts.has(filePath)) {
        texts.set(filePath, base.readText(filePath));
      }
      return texts.get(filePath);
    },
    listDir: (directory) => {
      if (!listings.has(directory)) {
        listings.set(directory, base.listDir(directory));
      }
      return listings.get(directory);
    },
  };
}

/**
 * A model file's bytes as text, or `undefined` when they are not UTF-8: a strict decode,
 * because the loader refuses such a file rather than reading replacement characters. A
 * leading byte-order mark is kept, as `fs.readFileSync(…, "utf8")` keeps it.
 */
export function decodeModelText(bytes: Uint8Array): string | undefined {
  try {
    return new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(bytes);
  } catch {
    return undefined;
  }
}

/**
 * A resolved target: the file and the 0-based line of its identity, or, for a requirement's
 * dependent located at its member line, that line with the member value's UTF-16 columns
 * `[start, end)`. The columns are optional; definition and symbols use the line alone.
 */
export interface Location {
  file: string;
  line: number;
  start?: number;
  end?: number;
}

// --- the artifact ----------------------------------------------------------

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function requireString(owner: Record<string, unknown>, key: string, where: string): string {
  const value = owner[key];
  if (typeof value !== "string" || value === "") {
    throw new ReferencesArtifactError(`${where}.${key} must be a non-empty string`);
  }
  return value;
}

function requireStringOrNull(
  owner: Record<string, unknown>,
  key: string,
  where: string,
): string | null {
  const value = owner[key];
  if (value === null) {
    return null;
  }
  if (typeof value !== "string" || value === "") {
    throw new ReferencesArtifactError(`${where}.${key} must be a non-empty string or null`);
  }
  return value;
}

function requireArray(owner: Record<string, unknown>, key: string, where: string): unknown[] {
  const value = owner[key];
  if (!Array.isArray(value) || value.length === 0) {
    throw new ReferencesArtifactError(`${where}.${key} must be a non-empty array`);
  }
  return value;
}

/** An array, possibly empty, of non-empty strings. */
function requireStrings(owner: Record<string, unknown>, key: string, where: string): string[] {
  const value = owner[key];
  if (!Array.isArray(value) || !value.every((item) => typeof item === "string" && item !== "")) {
    throw new ReferencesArtifactError(`${where}.${key} must be an array of non-empty strings`);
  }
  return value as string[];
}

function requireOneOf<T extends string>(
  owner: Record<string, unknown>,
  key: string,
  where: string,
  allowed: readonly T[],
): T {
  const value = owner[key];
  if (typeof value !== "string" || !(allowed as readonly string[]).includes(value)) {
    throw new ReferencesArtifactError(`${where}.${key} must be one of ${allowed.join(", ")}`);
  }
  return value as T;
}

function requireLayer(
  owner: Record<string, unknown>,
  key: string,
  where: string,
  layers: ReadonlyMap<string, LayerFacts>,
): string {
  const layer = requireString(owner, key, where);
  if (!layers.has(layer)) {
    throw new ReferencesArtifactError(`${where}.${key} names '${layer}', which is no layer`);
  }
  return layer;
}

/** A kind that must be one of the top-level kinds of *layerName* (a layer `requireLayer` accepted). */
function requireTopLevelKind(
  owner: Record<string, unknown>,
  key: string,
  where: string,
  layers: ReadonlyMap<string, LayerFacts>,
  layerName: string,
): string {
  const kind = requireString(owner, key, where);
  const layer = layers.get(layerName);
  if (layer === undefined || !layer.topLevelKinds.some((entry) => entry.kind === kind)) {
    throw new ReferencesArtifactError(
      `${where}.${key} names '${kind}', which is no top-level kind of ${layerName}`,
    );
  }
  return kind;
}

/** A non-empty list of non-empty key steps. */
function parseSteps(owner: Record<string, unknown>, key: string, where: string): string[] {
  const steps = requireArray(owner, key, where);
  if (!steps.every((step) => typeof step === "string" && step !== "")) {
    throw new ReferencesArtifactError(`${where}.${key} must hold non-empty strings only`);
  }
  return steps as string[];
}

function parsePaths(owner: Record<string, unknown>, where: string): ReferencePath[] {
  return requireArray(owner, "paths", where).map((entry, pathIndex) => {
    const at = `${where}.paths[${pathIndex}]`;
    if (!isRecord(entry)) {
      throw new ReferencesArtifactError(`${at} must be an object`);
    }
    return { topKind: requireString(entry, "topKind", at), path: parseSteps(entry, "path", at) };
  });
}

function parseLayer(name: string, raw: unknown): LayerFacts {
  const where = `layers.${name}`;
  if (!isRecord(raw)) {
    throw new ReferencesArtifactError(`${where} must be an object`);
  }
  const discriminator = requireStringOrNull(raw, "discriminator", where);
  const topLevelKinds = requireArray(raw, "topLevelKinds", where).map((entry, index) => {
    const at = `${where}.topLevelKinds[${index}]`;
    if (!isRecord(entry)) {
      throw new ReferencesArtifactError(`${at} must be an object`);
    }
    const discriminatorValue = requireStringOrNull(entry, "discriminatorValue", at);
    if ((discriminator === null) !== (discriminatorValue === null)) {
      throw new ReferencesArtifactError(
        `${at}.discriminatorValue must be null exactly when ${where}.discriminator is`,
      );
    }
    return {
      kind: requireString(entry, "kind", at),
      discriminatorValue,
      guideAnchor: requireString(entry, "guideAnchor", at),
    };
  });
  return {
    name,
    token: requireString(raw, "token", where),
    directory: requireString(raw, "directory", where),
    nesting: requireString(raw, "nesting", where),
    fileSuffix: requireString(raw, "fileSuffix", where),
    identityField: requireString(raw, "identityField", where),
    filenameFrom: requireString(raw, "filenameFrom", where),
    discriminator,
    guidePage: requireString(raw, "guidePage", where),
    topLevelKinds,
  };
}

function parseEdge(
  index: number,
  raw: unknown,
  layers: ReadonlyMap<string, LayerFacts>,
): ReferenceEdge {
  const where = `references[${index}]`;
  if (!isRecord(raw)) {
    throw new ReferencesArtifactError(`${where} must be an object`);
  }
  const sourceLayer = requireLayer(raw, "sourceLayer", where, layers);
  const targetLayer = requireLayer(raw, "targetLayer", where, layers);
  const paths = parsePaths(raw, where);
  return {
    field: requireString(raw, "field", where),
    sourceLayer,
    targetLayer,
    targetIdentityField: requireString(raw, "targetIdentityField", where),
    paths,
  };
}

function parseRelation(
  index: number,
  raw: unknown,
  layers: ReadonlyMap<string, LayerFacts>,
): RelationEdge {
  const where = `relations[${index}]`;
  if (!isRecord(raw)) {
    throw new ReferencesArtifactError(`${where} must be an object`);
  }
  const sourceLayer = requireLayer(raw, "sourceLayer", where, layers);
  const targetLayer = requireLayer(raw, "targetLayer", where, layers);
  return {
    field: requireString(raw, "field", where),
    sourceLayer,
    targetLayer,
    targetKind: requireTopLevelKind(raw, "targetKind", where, layers, targetLayer),
    targetField: requireString(raw, "targetField", where),
    resolution: requireOneOf(raw, "resolution", where, [
      "list-values",
      "map-keys",
      "versioned-list-values",
    ] as const),
    paths: parsePaths(raw, where),
  };
}

function parseCollection(
  index: number,
  raw: unknown,
  layers: ReadonlyMap<string, LayerFacts>,
): CollectionFacts {
  const where = `collections[${index}]`;
  if (!isRecord(raw)) {
    throw new ReferencesArtifactError(`${where} must be an object`);
  }
  return {
    layer: requireLayer(raw, "layer", where, layers),
    collectionKind: requireOneOf(raw, "collectionKind", where, ["list", "map"] as const),
    memberIdentity: requireOneOf(raw, "memberIdentity", where, [
      "field",
      "whole_value",
      "composite",
    ] as const),
    identityField: requireStringOrNull(raw, "identityField", where),
    identityFields: requireStrings(raw, "identityFields", where),
    memberKinds: requireStrings(raw, "memberKinds", where),
    discriminatorField: requireStringOrNull(raw, "discriminatorField", where),
    paths: parsePaths(raw, where),
  };
}

function parseMemberReference(
  index: number,
  raw: unknown,
  layers: ReadonlyMap<string, LayerFacts>,
): MemberReferenceEdge {
  const where = `memberReferences[${index}]`;
  if (!isRecord(raw)) {
    throw new ReferencesArtifactError(`${where} must be an object`);
  }
  const collections = requireArray(raw, "collections", where).map((entry, collectionIndex) => {
    const at = `${where}.collections[${collectionIndex}]`;
    if (!isRecord(entry)) {
      throw new ReferencesArtifactError(`${at} must be an object`);
    }
    const layer = requireLayer(entry, "layer", at, layers);
    return {
      layer,
      kind: requireTopLevelKind(entry, "kind", at, layers, layer),
      path: parseSteps(entry, "path", at),
      identityField: requireStringOrNull(entry, "identityField", at),
    };
  });
  return {
    field: requireString(raw, "field", where),
    sourceLayer: requireLayer(raw, "sourceLayer", where, layers),
    collections,
    via: requireStringOrNull(raw, "via", where),
    paths: parsePaths(raw, where),
  };
}

/** The array *key* of *document*, each row parsed by *parse*. */
function parseSection<T>(
  document: Record<string, unknown>,
  key: string,
  parse: (index: number, raw: unknown, layers: ReadonlyMap<string, LayerFacts>) => T,
  layers: ReadonlyMap<string, LayerFacts>,
): T[] {
  const rows = document[key];
  if (!Array.isArray(rows)) {
    throw new ReferencesArtifactError(`${key} must be an array`);
  }
  return rows.map((raw, index) => parse(index, raw, layers));
}

/**
 * Parse and validate `references.json`'s text.
 *
 * Throws {@link ReferencesArtifactError} naming the wrong key; activation turns it into one
 * warning that navigation is off. Only the shape the providers read is checked, plus each
 * edge row's `field`; keys not read are not checked either. The artifact ships with the
 * `.vsix` built from the same commit, so this guards a damaged file, not a contract.
 */
export function loadReferences(text: string): References {
  let document: unknown;
  try {
    document = JSON.parse(text);
  } catch (error) {
    throw new ReferencesArtifactError(`not JSON (${(error as Error).message})`);
  }
  if (!isRecord(document)) {
    throw new ReferencesArtifactError("the document must be an object");
  }
  const rawLayers = document.layers;
  if (!isRecord(rawLayers) || Object.keys(rawLayers).length === 0) {
    throw new ReferencesArtifactError("layers must be a non-empty object");
  }
  const layers = new Map<string, LayerFacts>();
  for (const [name, raw] of Object.entries(rawLayers)) {
    layers.set(name, parseLayer(name, raw));
  }
  return {
    layers,
    references: parseSection(document, "references", parseEdge, layers),
    relations: parseSection(document, "relations", parseRelation, layers),
    collections: parseSection(document, "collections", parseCollection, layers),
    memberReferences: parseSection(document, "memberReferences", parseMemberReference, layers),
  };
}

// --- the line scanner ------------------------------------------------------

type LineKind = "blank" | "comment" | "key" | "other";

/** One line as the scanner reads it — exported read-only for the outline and the links. */
export interface LineShape {
  kind: LineKind;
  /** Leading spaces. */
  indent: number;
  /** The columns of the `- ` sequence indicators the line opens with. */
  dashes: number[];
  /** The key and its column (`kind === "key"` only). */
  key: string;
  keyCol: number;
  /** The text after the key's `:` (`kind === "key"` only). */
  rest: string;
  /**
   * The text after the indentation and the `- ` indicators (`kind === "other"`
   * only): a scalar sequence item's value as written, `- Core0` → `Core0`.
   */
  after: string;
}

// A model key: the contract's field names are lower snake case. A quoted or
// otherwise unusual key is not a key to the scanner (no navigation through it).
const KEY = /^([A-Za-z_][A-Za-z0-9_-]*):(?=[ \t]|$)/;

/** A text's lines, LF or CRLF: the scanner's split. */
export function splitLines(text: string): string[] {
  return text.split(/\r?\n/);
}

function parseLine(raw: string): LineShape {
  const shape: LineShape = {
    kind: "other",
    indent: 0,
    dashes: [],
    key: "",
    keyCol: 0,
    rest: "",
    after: "",
  };
  if (raw.trim() === "") {
    shape.kind = "blank";
    return shape;
  }
  let pos = 0;
  while (raw[pos] === " ") {
    pos += 1;
  }
  shape.indent = pos;
  if (raw[pos] === "\t") {
    // A tab in the indentation: YAML forbids it, and the scanner does not guess
    // its column. Read as deeper than everything, so a value it follows is
    // refused as continued and no walk takes it for a key or an opener.
    shape.indent = Number.MAX_SAFE_INTEGER;
    return shape;
  }
  if (raw[pos] === "#") {
    shape.kind = "comment";
    return shape;
  }
  while (raw[pos] === "-" && (raw[pos + 1] === " " || pos + 1 === raw.length)) {
    shape.dashes.push(pos);
    pos += 1;
    while (raw[pos] === " ") {
      pos += 1;
    }
  }
  const match = KEY.exec(raw.slice(pos));
  if (match !== null) {
    shape.kind = "key";
    shape.key = match[1];
    shape.keyCol = pos;
    shape.rest = raw.slice(pos + match[0].length);
  } else {
    shape.after = raw.slice(pos);
  }
  return shape;
}

// One request reads the same text several times (the key path, the top-level kind, the
// value, the hover's card), so the last text's shapes are kept. This one-entry memo is the
// module's only state: it holds no answer and is reused only for the identical text.
let lastText: string | undefined;
let lastShapes: LineShape[] = [];

function shapesOf(text: string): LineShape[] {
  if (text !== lastText) {
    lastShapes = splitLines(text).map(parseLine);
    lastText = text;
  }
  return lastShapes;
}

/**
 * The scanner's reading of every line of *text*, the one scan Go to Definition, the outline
 * and the links read. Read-only: the array is the module's memo of the last text.
 */
export function lineShapes(text: string): ReadonlyArray<Readonly<LineShape>> {
  return shapesOf(text);
}

/** A blank or comment line: no key, no item. */
export function isSkippable(shape: Readonly<LineShape>): boolean {
  return shape.kind === "blank" || shape.kind === "comment";
}

/** The key carries no value on its line: a block collection (or nothing) follows. */
export function hasEmptyValue(shape: Readonly<LineShape>): boolean {
  const rest = shape.rest.trim();
  return rest === "" || rest.startsWith("#");
}

/** A line that can own a block collection: one key, at most one `- ` before it, no value. */
function isOwnerLine(shape: LineShape): boolean {
  return shape.kind === "key" && shape.dashes.length <= 1 && hasEmptyValue(shape);
}

type Opener =
  | { type: "top" }
  | { type: "item"; line: number; dashCol: number }
  | { type: "key"; line: number };

/**
 * What opens the mapping the key on *line* (at column *keyCol*) belongs to: the
 * top of the file, a sequence item's `- ` line, or a parent key; `undefined`
 * when the lines above are not one of the shapes the scanner reads.
 */
function mappingOpener(
  shapes: readonly LineShape[],
  line: number,
  keyCol: number,
): Opener | undefined {
  const own = shapes[line];
  if (own.dashes.length === 1) {
    return { type: "item", line, dashCol: own.dashes[0] };
  }
  for (let i = line - 1; i >= 0; i -= 1) {
    const shape = shapes[i];
    if (isSkippable(shape) || shape.indent > keyCol) {
      continue;
    }
    if (shape.kind !== "key") {
      // At this column or left of it, and no key: a `- x` scalar item, a `-`
      // alone, a document marker, a tab. Not a shape the scanner reads — except
      // a sibling's indentless sequence at the key's own column.
      if (shape.indent === keyCol && shape.dashes.length > 0) {
        continue;
      }
      return undefined;
    }
    if (shape.dashes.length > 1) {
      return undefined;
    }
    if (shape.dashes.length === 1) {
      if (shape.indent === keyCol) {
        // An indentless sequence that is a sibling key's value.
        continue;
      }
      if (shape.keyCol === keyCol) {
        return { type: "item", line: i, dashCol: shape.dashes[0] };
      }
      if (shape.keyCol < keyCol && hasEmptyValue(shape)) {
        return { type: "key", line: i };
      }
      return undefined;
    }
    if (shape.indent === keyCol) {
      // A sibling key of the same mapping.
      continue;
    }
    return hasEmptyValue(shape) ? { type: "key", line: i } : undefined;
  }
  return keyCol === 0 ? { type: "top" } : undefined;
}

/**
 * The key line whose value is the block sequence holding the item at *line*
 * (its `- ` at *dashCol*): a key left of the dash (an indented sequence) or at
 * the dash's own column (the writer's indentless one), with no value of its own.
 */
function sequenceOwner(
  shapes: readonly LineShape[],
  line: number,
  dashCol: number,
): number | undefined {
  for (let i = line - 1; i >= 0; i -= 1) {
    const shape = shapes[i];
    if (isSkippable(shape) || shape.indent > dashCol) {
      continue;
    }
    if (shape.indent === dashCol && shape.dashes.length > 0) {
      // A sibling item of the same sequence.
      continue;
    }
    if (!isOwnerLine(shape) || shape.keyCol > dashCol) {
      return undefined;
    }
    return i;
  }
  return undefined;
}

/**
 * Does the key on *line* occur exactly once in its own mapping?
 *
 * The mapping runs from its opener (the item's `- ` line, the line after the
 * parent key, or the top of the file) to the next line left of the key's
 * column — which is also where a sibling item at the item's column ends it.
 * Every key of that mapping sits at the key's column, so a second line naming
 * the same key there, before or after *line*, is a duplicate the loader
 * resolves to the last occurrence.
 */
function occursOnceInItsMapping(
  shapes: readonly LineShape[],
  line: number,
  opener: Opener,
): boolean {
  const { key, keyCol } = shapes[line];
  const itemLine = opener.type === "item" ? opener.line : -1;
  const start = opener.type === "top" ? 0 : opener.type === "item" ? opener.line : opener.line + 1;
  let count = 0;
  for (let i = start; i < shapes.length; i += 1) {
    const shape = shapes[i];
    if (isSkippable(shape)) {
      continue;
    }
    if (i !== itemLine && shape.indent < keyCol) {
      break;
    }
    if (shape.kind === "key" && shape.keyCol === keyCol && shape.key === key) {
      count += 1;
    }
  }
  return count === 1;
}

/** A sequence item's key-path step: `editor_schema._MEMBER_STEP`, as `references.json` holds it. */
const MEMBER_STEP = "*";

/**
 * The key path of the key on *line* (0-based), `["ports", "*", "interface"]` for an
 * `interface:` line inside a `ports:` item, or `undefined`.
 *
 * Walks upward collecting the keys of decreasing column; a sequence item is one `"*"` step,
 * in both sequence styles. Every key on the path must occur once in its own mapping
 * (identical keys in two separate items are two mappings). The value is read by
 * {@link scalarValueAt} alone.
 */
export function keyPathAt(text: string, line: number): string[] | undefined {
  const shapes = shapesOf(text);
  if (!Number.isInteger(line) || line < 0 || line >= shapes.length) {
    return undefined;
  }
  if (shapes[line].kind !== "key" || shapes[line].dashes.length > 1) {
    return undefined;
  }
  const reversed: string[] = [];
  let current = line;
  for (;;) {
    const shape = shapes[current];
    reversed.push(shape.key);
    const opener = mappingOpener(shapes, current, shape.keyCol);
    if (opener === undefined || !occursOnceInItsMapping(shapes, current, opener)) {
      return undefined;
    }
    if (opener.type === "top") {
      break;
    }
    let owner: number | undefined;
    if (opener.type === "item") {
      reversed.push(MEMBER_STEP);
      owner = sequenceOwner(shapes, opener.line, opener.dashCol);
    } else {
      owner = opener.line;
    }
    if (owner === undefined || !isOwnerLine(shapes[owner])) {
      return undefined;
    }
    current = owner;
  }
  return reversed.reverse();
}

/**
 * The lines of the sequence item opened on *line* (0-based), first and last inclusive, or
 * `undefined` when *line* opens no item (no `- `, or `- - `).
 *
 * The item runs to the line before the next non-blank, non-comment line indented at or left
 * of its dash column (one rule for both sequence styles). Trailing blank and comment lines
 * are not part of it.
 */
export function itemExtent(
  shapes: ReadonlyArray<Readonly<LineShape>>,
  line: number,
): { first: number; last: number } | undefined {
  if (!Number.isInteger(line) || line < 0 || line >= shapes.length) {
    return undefined;
  }
  if (shapes[line].dashes.length !== 1) {
    return undefined;
  }
  const dashCol = shapes[line].dashes[0];
  let last = line;
  for (let i = line + 1; i < shapes.length; i += 1) {
    const shape = shapes[i];
    if (isSkippable(shape)) {
      continue;
    }
    if (shape.indent <= dashCol) {
      break;
    }
    last = i;
  }
  return { first: line, last };
}

/**
 * The scalar sequence item on *line* (0-based), `- Core0` under `accessible:`, as its
 * owner's key path plus `"*"` and its value, or `undefined`.
 *
 * The value follows the one scalar reader's rules (an escape, an unclosed quote, a block
 * scalar, a flow collection, an anchor, an alias or a tag refused); an item with a
 * continuation line deeper than the dash is refused, as is a nested `- - x`; and the owner
 * key and every ancestor must occur once in their mappings ({@link keyPathAt}). A flow list
 * (`satisfies: [A, B]`) has no item line and is not read.
 */
export function itemScalarAt(
  text: string,
  line: number,
): { path: string[]; value: string } | undefined {
  const shapes = shapesOf(text);
  if (!Number.isInteger(line) || line < 0 || line >= shapes.length) {
    return undefined;
  }
  const shape = shapes[line];
  if (shape.kind !== "other" || shape.dashes.length !== 1) {
    return undefined;
  }
  const value = scalarOf(shape.after);
  if (value === undefined) {
    return undefined;
  }
  const dashCol = shape.dashes[0];
  for (let i = line + 1; i < shapes.length; i += 1) {
    const next = shapes[i];
    if (isSkippable(next)) {
      continue;
    }
    if (next.indent > dashCol) {
      return undefined;
    }
    break;
  }
  const owner = sequenceOwner(shapes, line, dashCol);
  if (owner === undefined) {
    return undefined;
  }
  const ownerPath = keyPathAt(text, owner);
  return ownerPath === undefined ? undefined : { path: [...ownerPath, MEMBER_STEP], value };
}

// The first characters that make a value something other than a plain scalar:
// a block scalar, an anchor, an alias, a tag, a flow collection, a comment, and
// the reserved indicators.
const NOT_PLAIN_START = new Set([
  "|",
  ">",
  "&",
  "*",
  "!",
  "{",
  "}",
  "[",
  "]",
  "#",
  "@",
  "`",
  "%",
  ",",
]);

/** The text after a closing quote must be nothing or a comment. */
function endsCleanly(after: string): boolean {
  return after.trim() === "" || /^[ \t]+#/.test(after);
}

function scalarOf(rest: string): string | undefined {
  const text = rest.replace(/^[ \t]+/, "");
  if (text === "") {
    return undefined;
  }
  const first = text[0];
  if (first === "'" || first === '"') {
    for (let i = 1; i < text.length; i += 1) {
      if (first === '"' && text[i] === "\\") {
        return undefined;
      }
      if (text[i] === first) {
        if (first === "'" && text[i + 1] === "'") {
          return undefined;
        }
        return endsCleanly(text.slice(i + 1)) ? text.slice(1, i) : undefined;
      }
    }
    // No closing quote on the line: a multi-line quoted scalar.
    return undefined;
  }
  if (NOT_PLAIN_START.has(first)) {
    return undefined;
  }
  if ((first === "-" || first === "?" || first === ":") && /^.(?:[ \t]|$)/.test(text)) {
    return undefined;
  }
  const comment = /[ \t]#/.exec(text);
  const value = (comment === null ? text : text.slice(0, comment.index)).trimEnd();
  if (value === "" || /:(?:[ \t]|$)/.test(value)) {
    return undefined;
  }
  return value;
}

/**
 * The scalar value of the key on *line* (0-based), unquoted, or `undefined`.
 *
 * **The one reader of a value navigation reads.** Quote-aware: a `#` inside quotes is part
 * of the value; only a `#` after whitespace past the value starts a comment. Refused: no
 * value, a block scalar, a flow collection, an anchor, an alias, a tag, a quoted value with
 * an escape (any `\` in double quotes, `''` in single), a quoted value not closed on its
 * line, and a value followed by a non-blank, non-comment line deeper than the key (a
 * continuation the loader joins into the value).
 */
export function scalarValueAt(text: string, line: number): string | undefined {
  const shapes = shapesOf(text);
  if (!Number.isInteger(line) || line < 0 || line >= shapes.length) {
    return undefined;
  }
  const shape = shapes[line];
  if (shape.kind !== "key" || shape.dashes.length > 1) {
    return undefined;
  }
  const value = scalarOf(shape.rest);
  if (value === undefined) {
    return undefined;
  }
  for (let i = line + 1; i < shapes.length; i += 1) {
    const next = shapes[i];
    if (isSkippable(next)) {
      continue;
    }
    if (next.indent > shape.keyCol) {
      return undefined;
    }
    break;
  }
  return value;
}

/** The lines carrying *key* at column 0, outside any sequence item. */
function columnZeroKeyLines(shapes: readonly LineShape[], key: string): number[] {
  const lines: number[] = [];
  shapes.forEach((shape, index) => {
    if (
      shape.kind === "key" &&
      shape.indent === 0 &&
      shape.dashes.length === 0 &&
      shape.key === key
    ) {
      lines.push(index);
    }
  });
  return lines;
}

/** What a column-0 key reads as: absent, refused (two lines, or a value refused), or one value. */
export type TopLevelScalar =
  | { status: "absent" }
  | { status: "refused" }
  | { status: "one"; line: number; value: string };

/**
 * The column-0 *key* of *text*: absent; `refused` when two or more lines carry it (the
 * loader keeps the last, navigation does not guess) or when {@link scalarValueAt} refuses
 * its value; else its line and value. An indented key of the same name is never it.
 */
export function topLevelScalar(text: string, key: string): TopLevelScalar {
  const lines = columnZeroKeyLines(shapesOf(text), key);
  if (lines.length === 0) {
    return { status: "absent" };
  }
  if (lines.length > 1) {
    return { status: "refused" };
  }
  const value = scalarValueAt(text, lines[0]);
  return value === undefined ? { status: "refused" } : { status: "one", line: lines[0], value };
}

/**
 * The saved *file*'s single column-0 identity line and its value ({@link topLevelScalar}),
 * or `undefined` when the file is unreadable or the identity is absent or refused.
 */
export function fileIdentity(
  facts: LayerFacts,
  file: string,
  reader: Reader,
): { line: number; value: string } | undefined {
  const text = reader.readText(file);
  const identity = text === undefined ? undefined : topLevelScalar(text, facts.identityField);
  return identity?.status === "one" ? { line: identity.line, value: identity.value } : undefined;
}

/**
 * The top-level kind *text* holds in *layer*: the layer's one kind, or the kind
 * whose discriminator value its single column-0 discriminator line carries.
 */
export function topLevelKindOf(text: string, layer: LayerFacts): string | undefined {
  if (layer.discriminator === null) {
    return layer.topLevelKinds.length === 1 ? layer.topLevelKinds[0].kind : undefined;
  }
  const read = topLevelScalar(text, layer.discriminator);
  if (read.status !== "one") {
    return undefined;
  }
  return layer.topLevelKinds.find((kind) => kind.discriminatorValue === read.value)?.kind;
}

/** Two key paths, step by step. */
export function sameKeyPath(left: readonly string[], right: readonly string[]): boolean {
  return left.length === right.length && left.every((step, index) => step === right[index]);
}

/** The top-level kind of the buffer *text*, a file of *layer*, or `undefined`. */
function bufferTopKind(refs: References, text: string, layer: string): string | undefined {
  const facts = refs.layers.get(layer);
  return facts === undefined ? undefined : topLevelKindOf(text, facts);
}

/** The first of *edges* leaving *layer* whose `paths` hold `(topKind, path)`, or `undefined`. */
function edgeAt<E extends { sourceLayer: string; paths: readonly ReferencePath[] }>(
  edges: readonly E[],
  layer: string,
  topKind: string,
  path: readonly string[],
  accept: (edge: E) => boolean = () => true,
): E | undefined {
  return edges.find(
    (edge) =>
      edge.sourceLayer === layer &&
      accept(edge) &&
      edge.paths.some((entry) => entry.topKind === topKind && sameKeyPath(entry.path, path)),
  );
}

/**
 * The reference edge *keyPath* names in a file of *layer* whose text is *text*, or
 * `undefined`.
 *
 * The top-level kind comes first, since one field name may lead to two targets by kind
 * (`base` on a `value_type` and on a `reference_type`); a multi-kind file answers only with
 * exactly one readable column-0 discriminator line. A `"*"` step matches a sequence item
 * only.
 */
export function referenceAt(
  refs: References,
  text: string,
  layer: string,
  keyPath: readonly string[],
): ReferenceEdge | undefined {
  const topKind = bufferTopKind(refs, text, layer);
  return topKind === undefined ? undefined : edgeAt(refs.references, layer, topKind, keyPath);
}

// --- the definition resolver -----------------------------------------------

/**
 * Is *value* one legal file-name segment, its composed file name included?
 * `model_registry.path_refusal`'s character rules: non-blank, not `.` or `..`,
 * no `..`, no separator, no NUL or other control character.
 */
function isPathSegment(value: string, suffix: string): boolean {
  if (value.trim() === "" || value === "." || value === "..") {
    return false;
  }
  const fileName = value + suffix;
  if (fileName.includes("..") || /[/\\]/.test(value)) {
    return false;
  }
  // Python's `str.isprintable()` refuses every C0/C1 control and the Unicode line and
  // paragraph separators.
  for (const char of value) {
    const code = char.codePointAt(0) ?? 0;
    if (code < 0x20 || (code >= 0x7f && code <= 0x9f) || code === 0x2028 || code === 0x2029) {
      return false;
    }
  }
  return true;
}

/** `str.casefold()`'s approximation: upper then lower folds `ß` to `ss` as casefold does. */
export function fold(value: string): string {
  return value.toUpperCase().toLowerCase();
}

type Confirmation =
  | { status: "confirmed"; line: number }
  | { status: "mismatch" }
  | { status: "refused" };

/** Does the file at *file* declare *value* as its single column-0 identity? */
function confirm(reader: Reader, file: string, identityField: string, value: string): Confirmation {
  const text = reader.readText(file);
  if (text === undefined) {
    return { status: "mismatch" };
  }
  const read = topLevelScalar(text, identityField);
  if (read.status === "refused") {
    return { status: "refused" };
  }
  if (read.status === "one" && read.value === value) {
    return { status: "confirmed", line: read.line };
  }
  return { status: "mismatch" };
}

/**
 * Code-unit order, as `Array.prototype.sort()` without a comparator orders strings:
 * the same on every platform and locale. Shared with `onboarding.ts`'s walk down.
 */
export function compareCodeUnits(left: string, right: string): number {
  return left < right ? -1 : left > right ? 1 : 0;
}

/**
 * One directory listing's entries of one file suffix, grouped by folded spelling: `plain`
 * by the whole folded name, `suffixed` by the folded stem through its `~` (`foo~` for
 * `Foo~2.yaml`) with `N` read. Built once per listing, so resolving N names against M
 * entries folds M names, not N × M.
 */
interface CandidateIndex {
  plain: Map<string, string[]>;
  suffixed: Map<string, Array<{ entry: string; n: number }>>;
}

/**
 * The index of each listing by suffix, keyed by the listing array itself, so it lives
 * exactly as long as the {@link requestReader} listing it indexes.
 */
const candidateIndexes = new WeakMap<readonly string[], Map<string, CandidateIndex>>();

/** *entries*' {@link CandidateIndex} for *suffix*, built on first use. */
function candidateIndex(entries: readonly string[], suffix: string): CandidateIndex {
  let bySuffix = candidateIndexes.get(entries);
  if (bySuffix === undefined) {
    bySuffix = new Map();
    candidateIndexes.set(entries, bySuffix);
  }
  const known = bySuffix.get(suffix);
  if (known !== undefined) {
    return known;
  }
  const foldedSuffix = fold(suffix);
  const index: CandidateIndex = { plain: new Map(), suffixed: new Map() };
  for (const entry of entries) {
    const folded = fold(entry);
    const plain = index.plain.get(folded) ?? [];
    plain.push(entry);
    index.plain.set(folded, plain);
    if (!folded.endsWith(foldedSuffix)) {
      continue;
    }
    // `<stem>~<N><suffix>`: the digits run from the last `~` to the suffix.
    const base = folded.slice(0, folded.length - foldedSuffix.length);
    const tilde = base.lastIndexOf("~");
    const digits = base.slice(tilde + 1);
    if (tilde >= 0 && /^[0-9]+$/.test(digits)) {
      const stem = base.slice(0, tilde + 1);
      const suffixed = index.suffixed.get(stem) ?? [];
      suffixed.push({ entry, n: Number(digits) });
      index.suffixed.set(stem, suffixed);
    }
  }
  bySuffix.set(suffix, index);
  return index;
}

/**
 * The candidates of one directory, in fixed order: the exact plain spelling, the plain name
 * in other cases, then the `~N` names by `N`, matched case-insensitively
 * (`model_state.free_storage_path`'s collision rule). An entry that is the plain name is
 * not also a `~N` one.
 */
function listedCandidates(entries: readonly string[], value: string, suffix: string): string[] {
  const index = candidateIndex(entries, suffix);
  const plains = [...(index.plain.get(fold(value + suffix)) ?? [])];
  const suffixed = (index.suffixed.get(`${fold(value)}~`) ?? []).filter(
    (candidate) => !plains.includes(candidate.entry),
  );
  const exact = value + suffix;
  const exactFirst = (entry: string): number => (entry === exact ? 0 : 1);
  plains.sort(
    (left, right) => exactFirst(left) - exactFirst(right) || compareCodeUnits(left, right),
  );
  suffixed.sort((left, right) => left.n - right.n || compareCodeUnits(left.entry, right.entry));
  return [...plains, ...suffixed.map((candidate) => candidate.entry)];
}

/**
 * How many directory levels below a nesting layer's directory are searched: the reader's
 * `**` glob has no bound, but a symbolic-link cycle must not walk forever. A component
 * stored deeper is not reached.
 */
export const MAX_GROUP_DEPTH = 8;

/**
 * The directories a file of *target* may sit in, each with its listing: the layer directory
 * alone for a flat layer; for a nesting one the layer directory itself (placement `""`)
 * and then every directory below it down to {@link MAX_GROUP_DEPTH} levels (`swc/a/` before
 * `swc/a/b/`, siblings in code-unit order), the placements `model_state.placement_from_location`
 * reports.
 *
 * Lazy: a directory is listed only when the caller asks for the next place, so a hit lists
 * nothing after it. Go to Symbol in Workspace (`symbolCore.ts`) walks every place through
 * this same walk.
 */
export function* placesOf(
  target: LayerFacts,
  layerDirectory: string,
  reader: Reader,
): Generator<{ place: string; entries: readonly string[] }> {
  function* walk(
    directory: string,
    depth: number,
  ): Generator<{ place: string; entries: readonly string[] }> {
    const entries = reader.listDir(directory);
    if (entries === undefined) {
      return;
    }
    yield { place: directory, entries };
    if (target.nesting === "flat" || depth >= MAX_GROUP_DEPTH) {
      return;
    }
    for (const entry of [...entries].sort()) {
      yield* walk(path.join(directory, entry), depth + 1);
    }
  }
  yield* walk(layerDirectory, 0);
}

/**
 * The file and identity line *value* resolves to through *edge*, or `undefined`.
 *
 * By the storage rule, always confirmed, always through a directory listing (so the
 * location carries the on-disk name): in each place of the layer, the plain name
 * `<value><fileSuffix>` exactly, then in another case, then the `~N` names by `N`, each
 * answering only when its single column-0 identity line holds *value*. A candidate with two
 * identity lines, or an identity {@link scalarValueAt} refuses, ends the resolution with no
 * answer rather than being passed over. A value that is no single path segment, and a layer
 * whose file name is not composed from its identity, resolve nothing.
 */
export function resolveDefinition(
  refs: References,
  edge: ReferenceEdge,
  value: string,
  modelRoot: string,
  reader: Reader,
): Location | undefined {
  const target = refs.layers.get(edge.targetLayer);
  if (target === undefined || target.identityField !== edge.targetIdentityField) {
    return undefined;
  }
  return resolveElement(target, value, modelRoot, reader);
}

/**
 * The element of *target* whose identity is *value*, by {@link resolveDefinition}'s rule,
 * for a caller holding a layer rather than an edge (Find All References maps each `impact`
 * row's layer token to one).
 */
export function resolveElement(
  target: LayerFacts,
  value: string,
  modelRoot: string,
  reader: Reader,
): Location | undefined {
  if (target.filenameFrom !== target.identityField) {
    return undefined;
  }
  if (!isPathSegment(value, target.fileSuffix)) {
    return undefined;
  }
  const layerDirectory = path.join(modelRoot, ...target.directory.split("/"));
  for (const { place, entries } of placesOf(target, layerDirectory, reader)) {
    for (const entry of listedCandidates(entries, value, target.fileSuffix)) {
      const file = path.join(place, entry);
      const answer = confirm(reader, file, target.identityField, value);
      if (answer.status === "confirmed") {
        return { file, line: answer.line };
      }
      if (answer.status === "refused") {
        return undefined;
      }
    }
  }
  return undefined;
}

// --- relations ---------------------------------------------------------------

/** A relation under the cursor: the edge, and the value naming its target. */
export interface RelationHit {
  edge: RelationEdge;
  value: string;
}

/**
 * The relation *line* (0-based) of a file of *layer* names, or `undefined`.
 *
 * A `list-values` or `versioned-list-values` relation is read on a scalar item of the
 * field's list ({@link itemScalarAt}; the value is the whole item, and
 * {@link resolveRelation} strips the version); a `map-keys` one on a key line directly
 * under the field (`RAM: 2048` under `memory:`), whose **key** is the value (a key the
 * scanner's key pattern does not read is not one). The top-level kind is read first, as in
 * {@link referenceAt}.
 */
export function relationAt(
  refs: References,
  text: string,
  layer: string,
  line: number,
): RelationHit | undefined {
  const topKind = bufferTopKind(refs, text, layer);
  return topKind === undefined ? undefined : relationOn(refs, text, layer, topKind, line);
}

/** {@link relationAt} on a buffer whose top-level kind *topKind* is already read. */
function relationOn(
  refs: References,
  text: string,
  layer: string,
  topKind: string,
  line: number,
): RelationHit | undefined {
  const item = itemScalarAt(text, line);
  if (item !== undefined) {
    const edge = edgeAt(
      refs.relations,
      layer,
      topKind,
      item.path.slice(0, -1),
      (candidate) =>
        candidate.resolution === "list-values" || candidate.resolution === "versioned-list-values",
    );
    return edge === undefined ? undefined : { edge, value: item.value };
  }
  const keyPath = keyPathAt(text, line);
  if (keyPath === undefined || keyPath.length < 2) {
    return undefined;
  }
  const edge = edgeAt(
    refs.relations,
    layer,
    topKind,
    keyPath.slice(0, -1),
    (candidate) => candidate.resolution === "map-keys",
  );
  return edge === undefined ? undefined : { edge, value: keyPath[keyPath.length - 1] };
}

/**
 * The file and identity line the relation *edge*'s *value* resolves to, or `undefined`:
 * {@link resolveElement}'s rule, and then the target's top-level kind must be the
 * relation's `targetKind` (a `hardware` file holding a `processor` is not the `core`
 * `accessible` names). A relation whose target field is not the target layer's identity
 * resolves nothing. A `versioned-list-values` value is read up to its first `@`, and a
 * value containing `:` (the element form of `derived_from`) resolves nothing, as the
 * kernel's resolver skips it.
 */
export function resolveRelation(
  refs: References,
  edge: RelationEdge,
  value: string,
  modelRoot: string,
  reader: Reader,
): Location | undefined {
  const target = refs.layers.get(edge.targetLayer);
  if (target === undefined || target.identityField !== edge.targetField) {
    return undefined;
  }
  const name = edge.resolution === "versioned-list-values" ? versionedLinkId(value) : value;
  const found = name === undefined ? undefined : resolveElement(target, name, modelRoot, reader);
  const text = found === undefined ? undefined : reader.readText(found.file);
  if (found === undefined || text === undefined) {
    return undefined;
  }
  return topLevelKindOf(text, target) === edge.targetKind ? found : undefined;
}

/**
 * The requirement id a `versioned-list-values` member names: the text before its first `@`,
 * unconditionally (a malformed suffix is the validator's finding), or `undefined` for a
 * member containing `:`. The extension's one split of a link, also read by the quick fixes
 * (`diagnosticsCore.ts`).
 */
export function versionedLinkId(value: string): string | undefined {
  if (value.includes(":")) {
    return undefined;
  }
  const at = value.indexOf("@");
  return at === -1 ? value : value.slice(0, at);
}

/**
 * The relation whose members may also take the element form `<token>:<name>`: the
 * requirement's `derived_from`, by its contract identifier.
 * `packages/tools/tests/test_trace.py` holds it to a relation of `references.json` whose
 * field is the one the validator reads the element form in.
 */
const ELEMENT_FORM_RELATION = "field:requirements.requirement.derived_from";

/**
 * The five linkable layers (`trace.LINKABLE_LAYERS`, by layer name) an element-form member
 * may name. Their **tokens** are read off `references.json`, never spelled here.
 * `packages/tools/tests/test_trace.py` holds the set equal to the Python one.
 */
const LINKABLE_LAYERS: ReadonlySet<string> = new Set([
  "swc",
  "interfaces",
  "compositions",
  "tasks",
  "hardware",
]);

/**
 * The element an element-form member of `derived_from` names (`hardware:Core0`), at its
 * identity line, with its layer; or `undefined`. The text before the first `:` must be the
 * token of one of the {@link LINKABLE_LAYERS}; a bare, unknown or other layer's token
 * (`connector:X`) names nothing. Found by {@link resolveElement}; its top-level kind is not
 * asked, as `trace.parse_element_reference` asks none.
 */
function elementFormTarget(
  refs: References,
  edge: RelationEdge,
  value: string,
  modelRoot: string,
  reader: Reader,
): { target: Location; targetLayer: string } | undefined {
  if (edge.field !== ELEMENT_FORM_RELATION) {
    return undefined;
  }
  const colon = value.indexOf(":");
  if (colon === -1) {
    return undefined;
  }
  const token = value.slice(0, colon);
  const facts = [...refs.layers.values()].find(
    (candidate) => LINKABLE_LAYERS.has(candidate.name) && candidate.token === token,
  );
  if (facts === undefined) {
    return undefined;
  }
  const target = resolveElement(facts, value.slice(colon + 1), modelRoot, reader);
  return target === undefined ? undefined : { target, targetLayer: facts.name };
}

/**
 * Every member line of *text* naming the requirement *id*: the value range of each scalar
 * item of a list at one of *relationPaths* (the `versioned-list-values` relation paths into
 * `requirements` published for the file's layer and kind, a `"*"` step standing for every
 * member on the way) whose text before the first `@` is *id*: a stated `@3`, a bare id and
 * a malformed `@x` all match, as the `impact` row carries the id alone.
 *
 * Each item is read by {@link itemScalarAt}, so what it refuses is no member line here: a
 * flow list has no item line, and a continued, escaped, anchored or aliased item, or one
 * under a repeated key, is not read. The ranges are in file order, quotes included.
 */
export function requirementMemberLines(
  text: string,
  relationPaths: ReadonlyArray<readonly string[]>,
  id: string,
): SubjectRange[] {
  const found: SubjectRange[] = [];
  for (let i = 0; i < shapesOf(text).length; i += 1) {
    const item = itemScalarAt(text, i);
    if (
      item === undefined ||
      versionedLinkId(item.value) !== id ||
      !relationPaths.some((owner) => sameKeyPath(item.path, [...owner, MEMBER_STEP]))
    ) {
      continue;
    }
    const range = itemValueRange(text, i);
    if (range !== undefined) {
      found.push(range);
    }
  }
  return found;
}

// --- member references -----------------------------------------------------------

/** A line of a live buffer: its text, the 0-based line, the file it is saved as, and its layer. */
export interface BufferLine {
  text: string;
  line: number;
  file: string;
  layer: string;
}

/** A member reference under the cursor: the edge, its key path, the value, and the buffer's top-level kind. */
export interface MemberReferenceHit {
  edge: MemberReferenceEdge;
  keyPath: readonly string[];
  value: string;
  topKind: string;
}

/**
 * The member reference the key on *line* (0-based) of a file of *layer* declares (`port:` /
 * `element:` in a runnable's `writes` item), with its value, or `undefined`.
 */
export function memberReferenceAt(
  refs: References,
  text: string,
  layer: string,
  line: number,
): MemberReferenceHit | undefined {
  const topKind = bufferTopKind(refs, text, layer);
  return topKind === undefined ? undefined : memberReferenceOn(refs, text, layer, topKind, line);
}

/** {@link memberReferenceAt} on a buffer whose top-level kind *topKind* is already read. */
function memberReferenceOn(
  refs: References,
  text: string,
  layer: string,
  topKind: string,
  line: number,
): MemberReferenceHit | undefined {
  const keyPath = keyPathAt(text, line);
  const edge =
    keyPath === undefined ? undefined : edgeAt(refs.memberReferences, layer, topKind, keyPath);
  const value = edge === undefined ? undefined : scalarValueAt(text, line);
  return keyPath === undefined || edge === undefined || value === undefined
    ? undefined
    : { edge, keyPath, value, topKind };
}

/**
 * The lines of *text* that hold *value* as a member of the collection at *path* (from the
 * top-level kind): each item's `identityField:` line, or, for a whole-value list
 * (`identityField` `null`), each `- <value>` item line. Read by the one key-path walk and
 * the one scalar reader, so whatever they refuse is no member. *within* (inclusive, default
 * the whole text) bounds the search to one enclosing item, which is how
 * {@link locateSubject} walks down a subject's path.
 */
export function memberLinesOf(
  text: string,
  path: readonly string[],
  identityField: string | null,
  value: string,
  within?: { first: number; last: number },
): number[] {
  const shapes = shapesOf(text);
  const found: number[] = [];
  const first = within === undefined ? 0 : Math.max(0, within.first);
  const last = within === undefined ? shapes.length - 1 : Math.min(shapes.length - 1, within.last);
  for (let i = first; i <= last; i += 1) {
    const shape = shapes[i];
    if (identityField === null) {
      if (shape.kind !== "other" || shape.dashes.length !== 1) {
        continue;
      }
      const item = itemScalarAt(text, i);
      if (
        item !== undefined &&
        item.value === value &&
        sameKeyPath(item.path, [...path, MEMBER_STEP])
      ) {
        found.push(i);
      }
      continue;
    }
    if (shape.kind !== "key" || shape.key !== identityField) {
      continue;
    }
    const keyPath = keyPathAt(text, i);
    if (
      keyPath !== undefined &&
      sameKeyPath(keyPath, [...path, MEMBER_STEP, identityField]) &&
      scalarValueAt(text, i) === value
    ) {
      found.push(i);
    }
  }
  return found;
}

/**
 * The line of the key *key* in the same sequence-item mapping as the key on *line*, read at
 * *expectedPath*, or `undefined` when the item holds no such key or holds it twice.
 */
function siblingLine(
  text: string,
  line: number,
  key: string,
  expectedPath: readonly string[],
): number | undefined {
  const shapes = shapesOf(text);
  const own = shapes[line];
  const opener = mappingOpener(shapes, line, own.keyCol);
  if (opener === undefined || opener.type !== "item") {
    return undefined;
  }
  for (let i = opener.line; i < shapes.length; i += 1) {
    const shape = shapes[i];
    if (isSkippable(shape)) {
      continue;
    }
    if (i !== opener.line && shape.indent < own.keyCol) {
      break;
    }
    if (shape.kind === "key" && shape.keyCol === own.keyCol && shape.key === key) {
      const keyPath = keyPathAt(text, i);
      return keyPath !== undefined && sameKeyPath(keyPath, expectedPath) ? i : undefined;
    }
  }
  return undefined;
}

/**
 * Where the member reference *hit* on the buffer line *at* points: exactly one member, or
 * `undefined`.
 *
 * Without `via` (`port:`): in the **buffer itself**, the item of a listed collection of the
 * buffer's own kind whose identity line reads the value. With `via` (`element:`,
 * `operation:`): the item's sibling `via` key is resolved through its own member reference
 * (which must have no `via`) to a port; that port's one `reference` edge into the listed
 * collections' layer (its `interface:`) is resolved by the storage rule; and in the
 * **saved** target the collection listed for the target's kind is searched (an item's
 * `name:` line, or a stub's `- <value>` line). A port with no `interface:` line, an
 * unresolved interface, an unlisted kind, a value the target does not hold, or a repeated
 * sibling key answers nothing.
 */
export function resolveMemberReference(
  refs: References,
  hit: MemberReferenceHit,
  at: BufferLine,
  modelRoot: string,
  reader: Reader,
): { target: Location; targetLayer: string } | undefined {
  const { edge, topKind } = hit;
  if (edge.via === null) {
    const found = onlyOne(membersIn(at.text, edge, at.layer, topKind, hit.value));
    return found === undefined
      ? undefined
      : { target: { file: at.file, line: found }, targetLayer: at.layer };
  }
  const memberLine = viaMember(refs, hit, at);
  const target =
    memberLine === undefined
      ? undefined
      : crossingTarget(refs, edge, at, topKind, memberLine, modelRoot, reader);
  if (target === undefined) {
    return undefined;
  }
  const targetKind = topLevelKindOf(target.text, target.facts);
  const found =
    targetKind === undefined
      ? undefined
      : onlyOne(membersIn(target.text, edge, target.facts.name, targetKind, hit.value));
  return found === undefined
    ? undefined
    : { target: { file: target.file, line: found }, targetLayer: target.facts.name };
}

/** The one line of *lines*, or `undefined` for none or several. */
function onlyOne(lines: readonly number[]): number | undefined {
  return lines.length === 1 ? lines[0] : undefined;
}

/**
 * The lines of *text* (a file of *layer* holding top-level kind *topKind*) that hold
 * *value* as a member of a collection *edge* lists for that kind.
 */
function membersIn(
  text: string,
  edge: MemberReferenceEdge,
  layer: string,
  topKind: string,
  value: string,
): number[] {
  return edge.collections
    .filter((target) => target.layer === layer && target.kind === topKind)
    .flatMap((target) => memberLinesOf(text, target.path, target.identityField, value));
}

/**
 * The line of the one member the sibling `via` key of *hit*'s item names, resolved in the
 * buffer through `via`'s own member reference (which must have no `via`), or `undefined`.
 */
function viaMember(refs: References, hit: MemberReferenceHit, at: BufferLine): number | undefined {
  const { edge, topKind } = hit;
  if (edge.via === null) {
    return undefined;
  }
  const viaPath = [...hit.keyPath.slice(0, -1), edge.via];
  const viaLine = siblingLine(at.text, at.line, edge.via, viaPath);
  const viaValue = viaLine === undefined ? undefined : scalarValueAt(at.text, viaLine);
  const viaEdge = edgeAt(
    refs.memberReferences,
    at.layer,
    topKind,
    viaPath,
    (candidate) => candidate.via === null,
  );
  return viaValue === undefined || viaEdge === undefined
    ? undefined
    : onlyOne(membersIn(at.text, viaEdge, at.layer, topKind, viaValue));
}

/**
 * The saved file the member on *memberLine* reaches through its one `reference` edge into
 * the layer owning *edge*'s collections (the port's `interface:`), with its text and its
 * layer's facts, or `undefined`.
 */
function crossingTarget(
  refs: References,
  edge: MemberReferenceEdge,
  at: BufferLine,
  topKind: string,
  memberLine: number,
  modelRoot: string,
  reader: Reader,
): { file: string; text: string; facts: LayerFacts } | undefined {
  const targetLayers = new Set(edge.collections.map((target) => target.layer));
  const memberPath = keyPathAt(at.text, memberLine)?.slice(0, -1);
  if (memberPath === undefined || targetLayers.size !== 1) {
    return undefined;
  }
  const [targetLayer] = targetLayers;
  const crossings = refs.references.flatMap((candidate) =>
    candidate.sourceLayer === at.layer && candidate.targetLayer === targetLayer
      ? candidate.paths
          .filter(
            (entry) =>
              entry.topKind === topKind &&
              entry.path.length === memberPath.length + 1 &&
              sameKeyPath(entry.path.slice(0, -1), memberPath),
          )
          .map((entry) => ({ crossing: candidate, path: entry.path }))
      : [],
  );
  if (crossings.length !== 1) {
    return undefined;
  }
  const { crossing, path: crossingPath } = crossings[0];
  const crossingLine = siblingLine(
    at.text,
    memberLine,
    crossingPath[crossingPath.length - 1],
    crossingPath,
  );
  const targetName = crossingLine === undefined ? undefined : scalarValueAt(at.text, crossingLine);
  const found =
    targetName === undefined
      ? undefined
      : resolveDefinition(refs, crossing, targetName, modelRoot, reader);
  const facts = refs.layers.get(targetLayer);
  const text = found === undefined ? undefined : reader.readText(found.file);
  return found === undefined || facts === undefined || text === undefined
    ? undefined
    : { file: found.file, text, facts };
}

// --- Go to Definition: the three edges, in order ---------------------------------

/** What Go to Definition found, and through which kind of edge. */
export interface DefinitionTarget {
  target: Location;
  targetLayer: string;
  /** A member reference's target is a line inside an element: no hover card is drawn for it. */
  edgeKind: "reference" | "relation" | "memberReference";
}

/**
 * Go to Definition on *line* of the live buffer *text*, a file of *layer* at *file*: a
 * `reference` first, then a `relation`, then a `memberReference` (the contract makes a
 * field at most one of the three, so the order only fixes which reader runs first).
 * `undefined` when none answers.
 *
 * A `derived_from` member in the element form (`hardware:Core0`) answers through the
 * relation branch with `targetLayer` the **element's** layer, so the hover draws that
 * element's card; any other member holding `:` resolves nothing.
 */
export function definitionTarget(
  refs: References,
  text: string,
  layer: string,
  line: number,
  file: string,
  modelRoot: string,
  reader: Reader,
): DefinitionTarget | undefined {
  const topKind = bufferTopKind(refs, text, layer);
  if (topKind === undefined) {
    return undefined;
  }
  const keyPath = keyPathAt(text, line);
  const edge = keyPath === undefined ? undefined : edgeAt(refs.references, layer, topKind, keyPath);
  const value = edge === undefined ? undefined : scalarValueAt(text, line);
  if (edge !== undefined && value !== undefined) {
    const target = resolveDefinition(refs, edge, value, modelRoot, reader);
    return target === undefined
      ? undefined
      : { target, targetLayer: edge.targetLayer, edgeKind: "reference" };
  }
  const relation = relationOn(refs, text, layer, topKind, line);
  if (relation !== undefined) {
    const element = elementFormTarget(refs, relation.edge, relation.value, modelRoot, reader);
    if (element !== undefined) {
      return { ...element, edgeKind: "relation" };
    }
    const target = resolveRelation(refs, relation.edge, relation.value, modelRoot, reader);
    return target === undefined
      ? undefined
      : { target, targetLayer: relation.edge.targetLayer, edgeKind: "relation" };
  }
  const member = memberReferenceOn(refs, text, layer, topKind, line);
  const found =
    member === undefined
      ? undefined
      : resolveMemberReference(refs, member, { text, line, file, layer }, modelRoot, reader);
  return found === undefined ? undefined : { ...found, edgeKind: "memberReference" };
}

// --- the hover card --------------------------------------------------------

/** How many of the target file's first lines the hover's excerpt shows. */
export const HOVER_EXCERPT_LINES = 12;

/** What a hover on a reference shows of its target. */
export interface HoverCard {
  /** The target's top-level kind: the layer's one kind, or its single `kind:` line's. */
  kind: string;
  /** The target's single column-0 identity. */
  identity: string;
  /** The column-0 `description:` value joined over its lines, or `undefined`. */
  description: string | undefined;
  /** The target file's first {@link HOVER_EXCERPT_LINES} lines. */
  excerpt: string;
}

// A block scalar's header: `|` or `>`, an optional chomping and indentation
// indicator, an optional comment.
const BLOCK_HEADER = /^[|>][-+0-9]*(?:[ \t]+#.*)?$/;

/**
 * The column-0 `description:` of *text*: its line's value plus every following line deeper
 * than the key (a wrapped plain scalar, or a `|`/`>` block), joined with spaces; `undefined`
 * when absent, empty, or carried by two lines.
 *
 * Display only, so not {@link scalarValueAt}, which refuses a continued value: a description
 * is shown, never navigated from. A quoted first line is unquoted, escapes shown as written.
 */
function descriptionOf(text: string): string | undefined {
  const shapes = shapesOf(text);
  const raw = splitLines(text);
  const lines = columnZeroKeyLines(shapes, "description");
  if (lines.length !== 1) {
    return undefined;
  }
  const parts: string[] = [];
  const first = shapes[lines[0]].rest.trim();
  // A quoted value that does not close on its first line closes on a later one.
  let openQuote: string | undefined;
  if (first !== "" && !BLOCK_HEADER.test(first) && !first.startsWith("#")) {
    const quote = first[0];
    if (quote === "'" || quote === '"') {
      const close = first.indexOf(quote, 1);
      if (close > 0) {
        parts.push(first.slice(1, close));
      } else {
        parts.push(first.slice(1));
        openQuote = quote;
      }
    } else {
      const comment = /[ \t]#/.exec(first);
      parts.push(comment === null ? first : first.slice(0, comment.index).trimEnd());
    }
  }
  for (let i = lines[0] + 1; i < shapes.length; i += 1) {
    if (shapes[i].kind === "blank") {
      continue;
    }
    if (shapes[i].indent <= 0) {
      break;
    }
    parts.push(raw[i].trim());
  }
  let joined = parts
    .filter((part) => part !== "")
    .join(" ")
    .trim();
  if (openQuote !== undefined && joined.endsWith(openQuote)) {
    joined = joined.slice(0, -1).trimEnd();
  }
  return joined === "" ? undefined : joined;
}

/**
 * The hover card of the target whose text is *targetText*, in *layer*, or `undefined`.
 *
 * The identity is the single column-0 identity line, the kind the layer's one top-level
 * kind or the one its single discriminator line names, both read by {@link scalarValueAt}.
 * Two such lines, a refused value or an undeclared kind answer no card: the card confirms
 * what Go to Definition confirms and nothing more. No CLI and no index.
 */
export function hoverCard(targetText: string, layer: LayerFacts): HoverCard | undefined {
  const identity = topLevelScalar(targetText, layer.identityField);
  if (identity.status !== "one") {
    return undefined;
  }
  const kind = topLevelKindOf(targetText, layer);
  if (kind === undefined) {
    return undefined;
  }
  const excerpt = splitLines(targetText)
    .slice(0, HOVER_EXCERPT_LINES)
    .join("\n")
    .replace(/\s+$/, "");
  return { kind, identity: identity.value, description: descriptionOf(targetText), excerpt };
}

/**
 * The card as Markdown: `**<kind>** <identity>`, the description, then the excerpt in a
 * `yaml` fence longer than any backtick run inside it; model text is never rendered as
 * Markdown of its own.
 */
export function hoverMarkdown(card: HoverCard): string {
  const fence = codeFence(card.excerpt);
  const parts = [`**${escapeMarkdown(card.kind)}** ${escapeMarkdown(card.identity)}`];
  if (card.description !== undefined) {
    parts.push(escapeMarkdown(card.description));
  }
  parts.push(`${fence}yaml\n${card.excerpt}\n${fence}`);
  return parts.join("\n\n");
}

// --- subject locations -------------------------------------------------------

/**
 * One step of a finding's subject, as `swcraft check-model --format json` spells it
 * (`diagnostics.SubjectStep.to_json()`): a declared field, a collection member named by its
 * identity (a string, a dict of a composite identity's fields, or the whole value), or a
 * map key.
 */
export type SubjectStep = { field: string } | { member: unknown } | { key: string };

/** A place on one line: the 0-based line and the UTF-16 columns `[start, end)`. */
export interface SubjectRange {
  line: number;
  start: number;
  end: number;
}

/**
 * The columns of the scalar written from column *start* of *raw*, quotes included
 * ({@link scalarOf} has refused every quoted form whose closing quote is not the first).
 */
function scalarSpan(raw: string, start: number, value: string): { start: number; end: number } {
  const quote = raw[start];
  if (quote === "'" || quote === '"') {
    const close = raw.indexOf(quote, start + 1);
    return { start, end: close === -1 ? raw.length : close + 1 };
  }
  return { start, end: start + value.length };
}

/** The value columns of the key on *line*, or `undefined` when {@link scalarValueAt} refuses it. */
function keyValueRange(text: string, line: number): SubjectRange | undefined {
  const value = scalarValueAt(text, line);
  if (value === undefined) {
    return undefined;
  }
  const raw = splitLines(text)[line];
  const shape = shapesOf(text)[line];
  let start = shape.keyCol + shape.key.length + 1;
  while (raw[start] === " " || raw[start] === "\t") {
    start += 1;
  }
  return { line, ...scalarSpan(raw, start, value) };
}

/**
 * The value columns of the scalar sequence item on *line* (`REQ-12@3` in `  - REQ-12@3`),
 * or `undefined` when {@link itemScalarAt} refuses it. The quick fixes replace exactly
 * these columns.
 */
export function itemValueRange(text: string, line: number): SubjectRange | undefined {
  const item = itemScalarAt(text, line);
  if (item === undefined) {
    return undefined;
  }
  const raw = splitLines(text)[line];
  // `after` keeps a tab of the separation (`- \tREQ-12@3`), which scalarOf strips.
  let start = raw.length - shapesOf(text)[line].after.length;
  while (raw[start] === " " || raw[start] === "\t") {
    start += 1;
  }
  return { line, ...scalarSpan(raw, start, item.value) };
}

/** The one key line of *text* in *within* whose key path is *keyPath*, or `undefined`. */
function keyLineIn(
  text: string,
  keyPath: readonly string[],
  within: { first: number; last: number },
): number | undefined {
  const shapes = shapesOf(text);
  const name = keyPath[keyPath.length - 1];
  const lines: number[] = [];
  for (let i = within.first; i <= within.last; i += 1) {
    const shape = shapes[i];
    if (shape.kind !== "key" || shape.key !== name) {
      continue;
    }
    const found = keyPathAt(text, i);
    if (found !== undefined && sameKeyPath(found, keyPath)) {
      lines.push(i);
    }
  }
  return onlyOne(lines);
}

/**
 * The one key line of the whole *text* whose key path is *keyPath* (`["memory", "RAM0"]`),
 * or `undefined`: absent, written twice, or inside a flow collection. The Resources view's
 * line finder (`resourceCore.resourceLine`).
 */
export function keyPathLine(text: string, keyPath: readonly string[]): number | undefined {
  if (keyPath.length === 0) {
    return undefined;
  }
  return keyLineIn(text, keyPath, { first: 0, last: shapesOf(text).length - 1 });
}

/** The sequence item the key on *line* belongs to, as its extent, or `undefined`. */
function enclosingItem(text: string, line: number): { first: number; last: number } | undefined {
  const shapes = shapesOf(text);
  const opener = mappingOpener(shapes, line, shapes[line].keyCol);
  return opener?.type === "item" ? itemExtent(shapes, opener.line) : undefined;
}

/** The declared collection of *layer*'s top-level kind *topKind* at *keyPath*, or `undefined`. */
function collectionAt(
  refs: References,
  layer: string,
  topKind: string,
  keyPath: readonly string[],
): CollectionFacts | undefined {
  return refs.collections.find(
    (collection) =>
      collection.layer === layer &&
      collection.paths.some((p) => p.topKind === topKind && sameKeyPath(p.path, keyPath)),
  );
}

/**
 * The item of the composite-identity collection at *keyPath* whose identity fields read
 * *identity*'s string values (at least one; a field left `null` or out is not compared), as
 * the item's extent and the first compared field's value range; `undefined` for none,
 * several, or a non-string identity value.
 */
function compositeMember(
  text: string,
  keyPath: readonly string[],
  fields: readonly string[],
  identity: unknown,
  within: { first: number; last: number },
): { item: { first: number; last: number }; range: SubjectRange } | undefined {
  if (typeof identity !== "object" || identity === null || Array.isArray(identity)) {
    return undefined;
  }
  const values = identity as Record<string, unknown>;
  let candidates: Map<number, number> | undefined;
  for (const field of fields) {
    const value = values[field];
    if (value === undefined || value === null) {
      continue;
    }
    if (typeof value !== "string") {
      return undefined;
    }
    const here = new Map<number, number>();
    for (const line of memberLinesOf(text, keyPath, field, value, within)) {
      const item = enclosingItem(text, line);
      if (item !== undefined && (candidates === undefined || candidates.has(item.first))) {
        here.set(item.first, candidates?.get(item.first) ?? line);
      }
    }
    candidates = here;
  }
  if (candidates === undefined || candidates.size !== 1) {
    return undefined;
  }
  const [[first, line]] = [...candidates];
  const item = itemExtent(shapesOf(text), first);
  const range = keyValueRange(text, line);
  return item === undefined || range === undefined ? undefined : { item, range };
}

/**
 * The fields a whole-value member with a mapping identity is matched by: its **string**
 * entries, each the text of a key line in the item; or `undefined` when
 * {@link locateSubject} does not match it that way. Only a collection declaring a
 * `discriminatorField` (an event list) qualifies, and only an identity carrying that
 * discriminator as a string. A number (`period`) or a list is not compared, its text
 * spelling being the user's.
 */
function mappingIdentityFields(
  collection: CollectionFacts,
  identity: unknown,
): readonly string[] | undefined {
  const discriminator = collection.discriminatorField;
  if (
    collection.memberIdentity !== "whole_value" ||
    discriminator === null ||
    typeof identity !== "object" ||
    identity === null ||
    Array.isArray(identity)
  ) {
    return undefined;
  }
  const entries = Object.entries(identity as Record<string, unknown>);
  if (!entries.some(([field, value]) => field === discriminator && typeof value === "string")) {
    return undefined;
  }
  return entries.filter(([, value]) => typeof value === "string").map(([field]) => field);
}

/**
 * Where *steps* (a finding's subject path inside one element) lead in *text*, a file of
 * *layer* holding *topKind*: the inverse of {@link keyPathAt} for block-style documents, or
 * `undefined`.
 *
 * Each step is searched inside the item the previous member step reached. A `field` or
 * `key` step finds the one key line at the path (the key's columns; a **last** `field` step
 * with a scalar value takes the value's). A `member` step reads the collection
 * `references.json` declares there and finds its one member: by `- <value>` item, identity
 * line, composite fields, or an event's string entries. The range is the last step's.
 *
 * What it does not reach (each an `undefined` the caller turns into the identity line): an
 * empty path; a flow collection; a member, key or field absent or held twice; two events
 * with equal string entries; an undeclared collection; a non-string identity for a field or
 * composite collection; a mapping without a declared discriminator, or without it as a
 * string; a step below a scalar member; whatever the scanner refuses. The value's columns
 * do not reach a field holding a block or a multi-line scalar.
 */
export function locateSubject(
  refs: References,
  layer: string,
  topKind: string,
  text: string,
  steps: readonly SubjectStep[],
): SubjectRange | undefined {
  const shapes = shapesOf(text);
  let within = { first: 0, last: shapes.length - 1 };
  const keyPath: string[] = [];
  let range: SubjectRange | undefined;
  let atWholeValue = false;
  for (const [index, step] of steps.entries()) {
    if (atWholeValue) {
      return undefined;
    }
    if (!("member" in step)) {
      const name = "field" in step ? step.field : step.key;
      const line = keyLineIn(text, [...keyPath, name], within);
      if (line === undefined) {
        return undefined;
      }
      keyPath.push(name);
      // A last field step on a scalar marks the value; anything else (a block, an empty or
      // refused value, a `key` step, an inner field) keeps the key.
      const value =
        "field" in step && index === steps.length - 1 ? keyValueRange(text, line) : undefined;
      range = value ?? { line, start: shapes[line].keyCol, end: shapes[line].keyCol + name.length };
      continue;
    }
    const collection = collectionAt(refs, layer, topKind, keyPath);
    if (collection === undefined || collection.collectionKind !== "list") {
      return undefined;
    }
    const identity = step.member;
    const fields =
      collection.memberIdentity === "composite"
        ? collection.identityFields
        : mappingIdentityFields(collection, identity);
    if (fields !== undefined) {
      const found = compositeMember(text, keyPath, fields, identity, within);
      if (found === undefined) {
        return undefined;
      }
      within = found.item;
      range = found.range;
    } else {
      if (typeof identity !== "string") {
        return undefined;
      }
      const identityField = collection.memberIdentity === "field" ? collection.identityField : null;
      if (collection.memberIdentity === "field" && identityField === null) {
        return undefined;
      }
      const line = onlyOne(memberLinesOf(text, keyPath, identityField, identity, within));
      if (line === undefined) {
        return undefined;
      }
      // A whole-value item is its own `- <value>` line; an identity line sits
      // somewhere in its member's mapping, whose item the next step searches.
      const item = identityField === null ? itemExtent(shapes, line) : enclosingItem(text, line);
      range = identityField === null ? itemValueRange(text, line) : keyValueRange(text, line);
      if (item === undefined || range === undefined) {
        return undefined;
      }
      within = item;
      atWholeValue = identityField === null;
    }
    keyPath.push(MEMBER_STEP);
  }
  return range;
}

// --- Find All References -----------------------------------------------------

/** The `schemaVersion` of `swcraft impact --format json` this reads (`query.QUERY_SCHEMA_VERSION`). */
export const IMPACT_SCHEMA_VERSION = 3;

/**
 * What a CLI document of another `schemaVersion`, or a CLI that does not know an option the
 * extension passes, means: spelled once for this module's `impact` reader and
 * diagnosticsCore's `check-model` reader.
 */
export const DIFFERENT_RELEASES = "the toolchain and the extension are from different releases";

/**
 * One dependent of an `impact` walk: its layer token, its name and, when given, its
 * `detail` (how the walk reached it, `via port rIn`), shown beside the name.
 */
export interface ImpactRow {
  layer: string;
  name: string;
  /** The row's `detail` when it is a non-empty string; absent otherwise (the CLI's "none" is `""`). */
  detail?: string;
}

/** The part of `query.format_related_json`'s walk document navigation reads. */
export interface ImpactDocument {
  /** The name the walk was asked about, when given as a string: the id the member-line pass compares. */
  name?: string;
  /** Keyed by the layer token of each element the name matched. */
  matched: ReadonlyMap<string, readonly ImpactRow[]>;
}

/** An `impact` document navigation cannot read: the message names what is wrong. */
export class ImpactDocumentError extends Error {
  constructor(message: string) {
    super(`swcraft impact --format json: ${message}`);
    this.name = "ImpactDocumentError";
  }
}

/**
 * Parse `swcraft impact --format json`'s stdout. Throws an {@link ImpactDocumentError} on
 * invalid JSON, a `schemaVersion` other than {@link IMPACT_SCHEMA_VERSION} (named in the
 * message: a stale toolchain), or a `matched` that is not an object of row lists each with a
 * string `layer` and `name`. The top-level `name` is kept when a string; a row's `detail`
 * when a non-empty string (anything else is "no detail", never a refusal). Other keys are
 * not read.
 */
export function parseImpactDocument(json: string): ImpactDocument {
  let raw: unknown;
  try {
    raw = JSON.parse(json);
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error);
    throw new ImpactDocumentError(`not JSON: ${detail}`);
  }
  if (!isRecord(raw)) {
    throw new ImpactDocumentError("the document is not an object");
  }
  if (raw.schemaVersion !== IMPACT_SCHEMA_VERSION) {
    throw new ImpactDocumentError(
      `schemaVersion ${JSON.stringify(raw.schemaVersion)} is not the ${IMPACT_SCHEMA_VERSION} ` +
        `this extension reads — ${DIFFERENT_RELEASES}`,
    );
  }
  if (!isRecord(raw.matched)) {
    throw new ImpactDocumentError("matched must be an object");
  }
  const matched = new Map<string, ImpactRow[]>();
  for (const [token, rows] of Object.entries(raw.matched)) {
    if (!Array.isArray(rows)) {
      throw new ImpactDocumentError(`matched.${token} must be an array`);
    }
    matched.set(
      token,
      rows.map((row: unknown, index: number) => {
        if (!isRecord(row) || typeof row.layer !== "string" || typeof row.name !== "string") {
          throw new ImpactDocumentError(
            `matched.${token}[${index}] must carry a string layer and name`,
          );
        }
        return typeof row.detail === "string" && row.detail !== ""
          ? { layer: row.layer, name: row.name, detail: row.detail }
          : { layer: row.layer, name: row.name };
      }),
    );
  }
  return typeof raw.name === "string" ? { name: raw.name, matched } : { matched };
}

/**
 * The element name Find All References asks about on *line* of a file of *layer*, or
 * `undefined`: *line* must be the file's **only** column-0 identity line, its value one
 * {@link scalarValueAt} reads; otherwise no CLI runs.
 */
export function identityAt(
  refs: References,
  text: string,
  layer: string,
  line: number,
): string | undefined {
  const facts = refs.layers.get(layer);
  if (facts === undefined) {
    return undefined;
  }
  const read = topLevelScalar(text, facts.identityField);
  return read.status === "one" && read.line === line ? read.value : undefined;
}

/**
 * Find All References on a document with unsaved changes: `swcraft impact` reads the
 * **saved** file, so the request is answered only when the saved text (*savedText*,
 * `undefined` when unreadable) declares the same single identity *name* the buffer does.
 * Otherwise no CLI runs and the answer is a message to save first, never the dependents of
 * a name the saved model does not hold. `undefined` means: ask the CLI. *prefix* opens the
 * message (the Impact view passes its own).
 */
export function unsavedIdentityAnswer(
  refs: References,
  savedText: string | undefined,
  layer: string,
  name: string,
  prefix: string = REFERENCES_PREFIX,
): ReferencesAnswer | undefined {
  const facts = refs.layers.get(layer);
  const saved =
    facts === undefined || savedText === undefined
      ? undefined
      : topLevelScalar(savedText, facts.identityField);
  if (saved?.status === "one" && saved.value === name) {
    return undefined;
  }
  return {
    locations: [],
    message: {
      severity: "information",
      text:
        `${prefix}: save the file first — the CLI reads the saved model, ` +
        `and the saved file does not declare '${name}'.`,
    },
  };
}

/** The dependents found in the model files, and the rows whose file was not. */
export interface DependentLocations {
  locations: Location[];
  /** The rows {@link resolveElement} could not locate — reported, never dropped silently. */
  unlocated: ImpactRow[];
}

/**
 * The locations of the dependents *doc* lists for the element of *layer*: the rows of
 * `matched[<layer's token>]` only (the name may match an element of another layer too),
 * each placed by {@link dependentLocations}, the placement the Impact view uses too. A row
 * whose token names no layer, or whose file the storage rule does not confirm (deeper than
 * {@link MAX_GROUP_DEPTH}, or refused), is returned in `unlocated`.
 *
 * When *layer* is a `versioned-list-values` target (today `requirements`) and the document names
 * the id, a located row is placed at **every** member line of its file naming that id, the
 * value's columns set; a row whose file holds no such block-list member keeps its identity
 * line (a flow-list occurrence adds no location). Any other *layer* reads no dependent's
 * file.
 */
export function referenceLocations(
  refs: References,
  doc: ImpactDocument,
  layer: string,
  modelRoot: string,
  reader: Reader,
): DependentLocations {
  const facts = refs.layers.get(layer);
  if (facts === undefined) {
    return { locations: [], unlocated: [] };
  }
  const locations: Location[] = [];
  const unlocated: ImpactRow[] = [];
  for (const row of doc.matched.get(facts.token) ?? []) {
    const placed = dependentLocations(refs, row, layer, doc.name, modelRoot, reader);
    if (placed.length === 0) {
      unlocated.push(row);
    } else {
      locations.push(...placed);
    }
  }
  return { locations, unlocated };
}

/**
 * The layer of *refs* whose token (the CLI's spelling: `swc`, `interface`) is *token*, or
 * `undefined`: the one token-to-layer lookup.
 */
export function layerOfToken(refs: References, token: string): LayerFacts | undefined {
  for (const facts of refs.layers.values()) {
    if (facts.token === token) {
      return facts;
    }
  }
  return undefined;
}

/**
 * Where one dependent *row* of a walk about an element of *rootLayer* (a contract layer
 * name) is placed: the per-row half of {@link referenceLocations}, shared with the Impact
 * view (which takes the first entry).
 *
 * - **Empty** when the row is unlocated (its token names no layer, or
 *   {@link resolveElement} does not find its file).
 * - When *rootLayer* is a `versioned-list-values` target of the row's layer and *docName*
 *   is the id: **every** member line of the row's file naming it, in file order.
 * - Otherwise the row's **identity line** (another root layer, no name, or a file holding
 *   no such member: a transitive dependent, or a flow-list link).
 *
 * The line is the element's, never the edge's: the port a row was reached through is not
 * placed.
 */
export function dependentLocations(
  refs: References,
  row: ImpactRow,
  rootLayer: string,
  docName: string | undefined,
  modelRoot: string,
  reader: Reader,
): Location[] {
  const rowLayer = layerOfToken(refs, row.layer);
  if (rowLayer === undefined) {
    return [];
  }
  const found = resolveElement(rowLayer, row.name, modelRoot, reader);
  if (found === undefined) {
    return [];
  }
  // memberLocations reads no file when no relation of the row's layer targets *rootLayer*.
  const members =
    docName === undefined
      ? []
      : memberLocations(refs, rowLayer, rootLayer, found.file, docName, reader);
  return members.length === 0 ? [found] : members;
}

/**
 * The member lines of the dependent *file*, of *dependentLayer*, naming *id* through a
 * `versioned-list-values` relation into *targetLayer*, as locations; none when the file or
 * its kind cannot be read or no path is published for that kind. The file is not read at
 * all when no such relation exists: this is the one test of whether a layer is a link
 * target.
 */
function memberLocations(
  refs: References,
  dependentLayer: LayerFacts,
  targetLayer: string,
  file: string,
  id: string,
  reader: Reader,
): Location[] {
  const edges = refs.relations.filter(
    (edge) =>
      edge.resolution === "versioned-list-values" &&
      edge.targetLayer === targetLayer &&
      edge.sourceLayer === dependentLayer.name,
  );
  const text = edges.length === 0 ? undefined : reader.readText(file);
  const topKind = text === undefined ? undefined : topLevelKindOf(text, dependentLayer);
  if (text === undefined || topKind === undefined) {
    return [];
  }
  const paths = edges.flatMap((edge) =>
    edge.paths.filter((entry) => entry.topKind === topKind).map((entry) => entry.path),
  );
  return requirementMemberLines(text, paths, id).map(({ line, start, end }) => ({
    file,
    line,
    start,
    end,
  }));
}

/** What Find All References answers: the locations, and at most one message to show. */
export interface ReferencesAnswer {
  locations: Location[];
  message?: { severity: "information" | "warning"; text: string };
}

const REFERENCES_PREFIX = "SWCraft: Find All References";

/**
 * Map one `impact` run to the answer, never throwing:
 *
 * - *cancelled*: nothing (a stopped child is not reported as a timeout);
 * - the CLI could not be started: the install hint;
 * - `exitCode: null` (the bound or a signal stopped it): the stderr's last line, or a fixed
 *   sentence when empty;
 * - another non-zero exit (the CLI could not read the project, most often a sibling file
 *   that does not parse): the stderr, or a fixed sentence when empty;
 * - exit 0: the document's locations, *declaration* first when given and not repeated; an
 *   unreadable document is no locations and the reason. Shown beside them as information:
 *   the dependents whose file was not found, and a non-empty stderr (a `toolchain_version`
 *   mismatch is a `Warning:` there).
 */
export function referencesAnswer(
  refs: References,
  capture: ChildCapture,
  layer: string,
  modelRoot: string,
  reader: Reader,
  options: { cancelled: boolean; executable: string; declaration?: Location },
): ReferencesAnswer {
  if (options.cancelled) {
    return { locations: [] };
  }
  const stderr = capture.stderr.trim();
  if (capture.spawnFailed) {
    return {
      locations: [],
      message: {
        severity: "warning",
        text:
          `${REFERENCES_PREFIX}: the SWCraft CLI could not be started (${options.executable}). ` +
          "Run `SWCraft: Check Editor Tooling` for the diagnosis, or `SWCraft: Install Toolchain` " +
          "to install the toolchain from the workstation archive.",
      },
    };
  }
  if (capture.exitCode === null) {
    const lines = stderr.split("\n");
    const last = lines[lines.length - 1].trim();
    return {
      locations: [],
      message: {
        severity: "warning",
        text: `${REFERENCES_PREFIX}: ${last === "" ? "the CLI was stopped before it answered." : last}`,
      },
    };
  }
  if (capture.exitCode !== 0) {
    const reason = `the CLI could not read the project (exit ${capture.exitCode}).`;
    return {
      locations: [],
      message: {
        severity: "warning",
        text: `${REFERENCES_PREFIX}: ${stderr === "" ? reason : `${reason} ${stderr}`}`,
      },
    };
  }
  let doc: ImpactDocument;
  try {
    doc = parseImpactDocument(capture.stdout);
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error);
    return {
      locations: [],
      message: { severity: "warning", text: `${REFERENCES_PREFIX}: ${detail}` },
    };
  }
  const found = referenceLocations(refs, doc, layer, modelRoot, reader);
  const declaration = options.declaration;
  const locations =
    declaration === undefined
      ? found.locations
      : [
          declaration,
          ...found.locations.filter(
            (location) => location.file !== declaration.file || location.line !== declaration.line,
          ),
        ];
  const notes: string[] = [];
  if (found.unlocated.length > 0) {
    const names = found.unlocated.map((row) => `${row.name} (${row.layer})`).join(", ");
    const count = found.unlocated.length;
    notes.push(
      `${count} ${count === 1 ? "dependent" : "dependents"} the CLI named could not be ` +
        `located in the model files and ${count === 1 ? "is" : "are"} not listed: ${names}.`,
    );
  }
  if (stderr !== "") {
    notes.push(stderr);
  }
  return notes.length === 0
    ? { locations }
    : {
        locations,
        message: { severity: "information", text: `${REFERENCES_PREFIX}: ${notes.join(" ")}` },
      };
}

// --- CodeLens --------------------------------------------------------------------

/**
 * The layers whose element has a diagram page of its own, and the page's view-id prefix (a
 * composition's `ibd-` view, a sequence's `sequence-` view). **Hard-coded**, duplicated from
 * `writer/diagrams/__init__.py`'s file-name rule: a renamed prefix needs an extension
 * change.
 */
export const DIAGRAM_VIEW_PREFIXES: ReadonlyMap<string, string> = new Map([
  ["compositions", "ibd-"],
  ["sequences", "sequence-"],
]);

/** One lens of an element's identity line, in the order VS Code draws them. */
export type LensItem =
  | { kind: "references" }
  | { kind: "impact"; name: string }
  | { kind: "diagram"; view: string }
  | { kind: "show-in-diagram"; file: string };

/** The lenses of one model file: on its identity *line*, for the element *name*. */
export interface LensPlan {
  line: number;
  name: string;
  lenses: LensItem[];
}

/**
 * What the diagrams' index says about one model file: *file*, the storage name the index
 * lists for it (`diagramsTab.ts`' `indexedStorageName()`, `undefined` when none), and
 * *elements*, the index's elements block as read (`undefined` without an index).
 */
export interface LensIndex {
  file: string | undefined;
  elements: BlockRead<ReadonlyMap<string, ElementViews>> | undefined;
}

/**
 * The lenses of a file of *layer*, or `undefined` without a single readable column-0
 * identity line ({@link identityAt}'s rule). *references* and *impact* always; then at most
 * one diagram lens:
 *
 * - *diagram*: for a layer of {@link DIAGRAM_VIEW_PREFIXES} whose page `<prefix><name>.html`
 *   is **listed** in *viewsListing* under exactly that name (a case variant or a `~N` page
 *   does not count; an `undefined` listing answers none), opened directly;
 * - *show-in-diagram*: for **any** other element the diagrams' index lists with at least one
 *   view. An absent, unreadable or malformed index, or a file the index does not list (a
 *   duplicate identity included), answers none.
 */
export function lensPlan(
  refs: References,
  text: string,
  layer: string,
  viewsListing: readonly string[] | undefined,
  index?: LensIndex,
): LensPlan | undefined {
  const facts = refs.layers.get(layer);
  if (facts === undefined) {
    return undefined;
  }
  const read = topLevelScalar(text, facts.identityField);
  if (read.status !== "one") {
    return undefined;
  }
  const lenses: LensItem[] = [{ kind: "references" }, { kind: "impact", name: read.value }];
  const prefix = DIAGRAM_VIEW_PREFIXES.get(layer);
  const view = prefix === undefined ? undefined : `${prefix}${read.value}`;
  if (view !== undefined && isListedView(viewsListing, view)) {
    lenses.push({ kind: "diagram", view });
  } else if (
    index?.file !== undefined &&
    index.elements?.kind === "ok" &&
    (index.elements.value.get(index.file)?.views.length ?? 0) > 0
  ) {
    lenses.push({ kind: "show-in-diagram", file: index.file });
  }
  return { line: read.line, name: read.value, lenses };
}

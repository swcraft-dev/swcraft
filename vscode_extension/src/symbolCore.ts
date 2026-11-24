// Pure logic of the outline and of Go to Symbol in Workspace: the collections of one open model
// file and their members, and every element of the project by its file name, from a directory
// scan. No `vscode` import: it runs under plain `node --test`; the providers live in commands.ts.
//
// Nothing here parses YAML: every line is read through navigationCore's read-only helpers, so
// whatever Go to Definition refuses is refused here too (a duplicated collection key yields no
// group, a flow collection none, a refused identity names its member `[i]`). A second parser could
// drift from the one navigation trusts.
//
// The outline reads the open buffer only; the collections are the contract's, from
// `references.json`'s `collections` section. The workspace scan lists directories and reads no
// file except a `~N` collision file's identity line. Nothing is kept between requests.
//
// What it does not reach: a scalar field (a component's `type`) is not in the outline; a group
// whose items the scanner does not read shows the members it does; the symbol kinds are
// presentation only, and an unknown layer or member kind falls back so a contract addition needs
// no code.

import * as path from "node:path";

import {
  fileIdentity,
  hasEmptyValue,
  isSkippable,
  itemExtent,
  itemScalarAt,
  keyPathAt,
  type LayerFacts,
  type LineShape,
  lineShapes,
  placesOf,
  type Reader,
  type References,
  sameKeyPath,
  scalarValueAt,
  topLevelKindOf,
  topLevelScalar,
} from "./navigationCore";

// --- the kind tables (presentation only) -------------------------------------

/**
 * The root symbol's kind per layer, a `vscode.SymbolKind` member's name; also the kind Go to
 * Symbol in Workspace shows, which reads no `kind:` line.
 */
const ROOT_KIND_BY_LAYER: ReadonlyMap<string, string> = new Map([
  ["swc", "Class"],
  ["compositions", "Module"],
  ["interfaces", "Interface"],
  ["connectors", "Operator"],
  ["application_types", "TypeParameter"],
  ["implementation_types", "TypeParameter"],
  ["base_types", "TypeParameter"],
  ["requirements", "Key"],
  ["tasks", "Event"],
  ["sequences", "Function"],
  ["hardware", "Object"],
  ["mapping_sets", "Namespace"],
  ["units", "Constant"],
  ["data_constraints", "Constant"],
  ["compu_methods", "Operator"],
]);

/** The root's kind where the top-level kind decides it, over the layer's default. */
const ROOT_KIND_BY_TOP_KIND: ReadonlyMap<string, string> = new Map([
  ["enum", "Enum"],
  ["record", "Struct"],
  ["array", "Array"],
  ["implementation_record", "Struct"],
  ["implementation_array", "Array"],
  ["texttable", "Enum"],
  ["linear", "Operator"],
]);

/** A member's kind by the collection's member kind. */
const MEMBER_KIND: ReadonlyMap<string, string> = new Map([
  ["port", "Interface"],
  ["composition_port", "Interface"],
  ["runnable", "Function"],
  ["task_runnable_ref", "Function"],
  ["sequence_step", "Function"],
  ["timing_event", "Event"],
  ["init_event", "Event"],
  ["operation_event", "Event"],
  ["data_event", "Event"],
  ["call_returned_event", "Event"],
  ["data_read", "Property"],
  ["data_write", "Property"],
  ["port_com_spec", "Property"],
  ["operation", "Method"],
  ["operation_call", "Method"],
  ["data_element", "Field"],
  ["record_field", "Field"],
  ["implementation_element", "Field"],
  ["argument", "Variable"],
  ["enum_literal", "EnumMember"],
  ["table_entry", "EnumMember"],
  ["disabled_mode", "EnumMember"],
  ["composition_member", "Object"],
  ["delegation", "Operator"],
  ["map", "Operator"],
]);

const FALLBACK_ROOT_KIND = "Object";
const FALLBACK_MEMBER_KIND = "Field";
const SCALAR_ITEM_KIND = "Constant";
const MAP_KEY_KIND = "Key";

/** The kind tables, read-only, for the test that checks each name against `vscode.SymbolKind`. */
export const SYMBOL_KIND_TABLES = {
  rootByLayer: ROOT_KIND_BY_LAYER,
  rootByTopKind: ROOT_KIND_BY_TOP_KIND,
  byMemberKind: MEMBER_KIND,
  constants: [FALLBACK_ROOT_KIND, FALLBACK_MEMBER_KIND, SCALAR_ITEM_KIND, MAP_KEY_KIND],
} as const;

/** The kind of a layer's element: its top-level kind's when the table has one, else the layer's. */
export function rootKind(layer: string, topKind: string | undefined): string {
  const byKind = topKind === undefined ? undefined : ROOT_KIND_BY_TOP_KIND.get(topKind);
  return byKind ?? ROOT_KIND_BY_LAYER.get(layer) ?? FALLBACK_ROOT_KIND;
}

/**
 * The kind of a collection's members: the one kind every declared member kind maps to; a scalar
 * item of a whole-value list without a mapped kind is a `Constant`; anything else the fallback.
 */
function memberKind(memberKinds: readonly string[], scalar: boolean): string {
  const kinds = new Set(memberKinds.map((kind) => MEMBER_KIND.get(kind)));
  if (kinds.size === 1) {
    const [only] = kinds;
    if (only !== undefined) {
      return only;
    }
  }
  return scalar ? SCALAR_ITEM_KIND : FALLBACK_MEMBER_KIND;
}

// --- the outline --------------------------------------------------------------------

/** A span of the document, 0-based: line and character. */
export interface Span {
  startLine: number;
  startCharacter: number;
  endLine: number;
  endCharacter: number;
}

/** One symbol of the outline: the element, a collection group, or a member. */
export interface OutlineSymbol {
  name: string;
  detail: string;
  /** A `vscode.SymbolKind` member's name; the adapter maps it, an unknown one falls back. */
  kind: string;
  /** Contains `selection` and every child's `range`. */
  range: Span;
  selection: Span;
  children: OutlineSymbol[];
}

/** A member's name when its identity is not readable: shown rather than dropped. */
function unnamed(index: number): string {
  return `[${index}]`;
}

interface Group {
  keyLine: number;
  symbol: OutlineSymbol;
  members: Array<{ first: number; last: number; symbol: OutlineSymbol }>;
}

/** The outline's reading of one document: its lines, their shapes and its text. */
interface Doc {
  text: string;
  lines: readonly string[];
  shapes: ReadonlyArray<Readonly<LineShape>>;
}

function lineSpan(doc: Doc, first: number, last: number): Span {
  return {
    startLine: first,
    startCharacter: 0,
    endLine: last,
    endCharacter: doc.lines[last]?.length ?? 0,
  };
}

/** From *column* to the end of *line*. */
function tailSpan(doc: Doc, line: number, column: number): Span {
  const length = doc.lines[line]?.length ?? column;
  return { startLine: line, startCharacter: column, endLine: line, endCharacter: length };
}

/** The key on *line*, by its own characters. */
function keySpan(doc: Doc, line: number): Span {
  const shape = doc.shapes[line];
  return {
    startLine: line,
    startCharacter: shape.keyCol,
    endLine: line,
    endCharacter: shape.keyCol + shape.key.length,
  };
}

/**
 * The value of *field* in the item on lines *first*–*last* whose key path is *itemPath*, or
 * `undefined`.
 */
function itemField(
  doc: Doc,
  first: number,
  last: number,
  itemPath: readonly string[],
  field: string,
): { line: number; value: string } | undefined {
  const wanted = [...itemPath, field];
  for (let i = first; i <= last; i += 1) {
    const shape = doc.shapes[i];
    if (shape.kind !== "key" || shape.key !== field) {
      continue;
    }
    const keyPath = keyPathAt(doc.text, i);
    if (keyPath === undefined || !sameKeyPath(keyPath, wanted)) {
      continue;
    }
    const value = scalarValueAt(doc.text, i);
    return value === undefined ? undefined : { line: i, value };
  }
  return undefined;
}

/**
 * The `- ` lines of the block sequence under the key on *keyLine*: the first non-blank,
 * non-comment line after it opens the first item (at the key's column, the writer's indentless
 * style, or right of it), and every later line opening a `- ` at that dash column is a sibling; a
 * line left of it, or at it without a dash, ends the sequence. None when no item follows.
 */
function sequenceItems(doc: Doc, keyLine: number): number[] {
  const keyCol = doc.shapes[keyLine].keyCol;
  const items: number[] = [];
  let dashCol: number | undefined;
  for (let i = keyLine + 1; i < doc.shapes.length; i += 1) {
    const shape = doc.shapes[i];
    if (isSkippable(shape)) {
      continue;
    }
    const opensItem = shape.dashes.length > 0 && shape.dashes[0] === shape.indent;
    if (dashCol === undefined) {
      if (!opensItem || shape.indent < keyCol) {
        return [];
      }
      dashCol = shape.indent;
      items.push(i);
      continue;
    }
    if (shape.indent > dashCol) {
      continue;
    }
    if (shape.indent === dashCol && opensItem) {
      items.push(i);
      continue;
    }
    break;
  }
  return items;
}

/** One list member: its name, its selection and its kind. */
function listMember(
  doc: Doc,
  collection: References["collections"][number],
  itemPath: readonly string[],
  itemLine: number,
  last: number,
  index: number,
): { name: string; selection: Span; kind: string } {
  const dashCol = doc.shapes[itemLine].dashes[0];
  const fallback = { name: unnamed(index), selection: tailSpan(doc, itemLine, dashCol) };
  const named = (value: { line: number; value: string } | undefined, scalar: boolean) => ({
    ...(value === undefined
      ? fallback
      : {
          name: value.value,
          selection: tailSpan(doc, value.line, doc.shapes[value.line].keyCol),
        }),
    kind: memberKind(collection.memberKinds, scalar),
  });
  if (collection.memberIdentity === "field" && collection.identityField !== null) {
    return named(itemField(doc, itemLine, last, itemPath, collection.identityField), false);
  }
  if (collection.memberIdentity === "composite") {
    const parts = collection.identityFields
      .map((field) => itemField(doc, itemLine, last, itemPath, field))
      .filter((part): part is { line: number; value: string } => part !== undefined);
    if (parts.length === 0) {
      return named(undefined, false);
    }
    return named({ line: parts[0].line, value: parts.map((part) => part.value).join(".") }, false);
  }
  if (collection.discriminatorField !== null) {
    return named(itemField(doc, itemLine, last, itemPath, collection.discriminatorField), false);
  }
  const scalar = itemScalarAt(doc.text, itemLine);
  if (scalar !== undefined && sameKeyPath(scalar.path, itemPath)) {
    return {
      name: scalar.value,
      selection: tailSpan(doc, itemLine, dashCol),
      kind: memberKind(collection.memberKinds, true),
    };
  }
  return named(undefined, true);
}

function listGroup(
  doc: Doc,
  collection: References["collections"][number],
  groupPath: readonly string[],
  keyLine: number,
): Group | undefined {
  const items = sequenceItems(doc, keyLine);
  if (items.length === 0) {
    return undefined;
  }
  const itemPath = [...groupPath, "*"];
  const members: Group["members"] = [];
  items.forEach((itemLine, index) => {
    // `- - x` opens no item the scanner reads: dropped, its index still counted.
    const extent = itemExtent(doc.shapes, itemLine);
    if (extent === undefined) {
      return;
    }
    const member = listMember(doc, collection, itemPath, itemLine, extent.last, index);
    members.push({
      first: extent.first,
      last: extent.last,
      symbol: {
        name: member.name,
        detail: "",
        kind: member.kind,
        range: lineSpan(doc, extent.first, extent.last),
        selection: member.selection,
        children: [],
      },
    });
  });
  return finishGroup(doc, groupPath, keyLine, members, "Array");
}

function mapGroup(doc: Doc, groupPath: readonly string[], keyLine: number): Group | undefined {
  const keyCol = doc.shapes[keyLine].keyCol;
  const members: Group["members"] = [];
  let childCol: number | undefined;
  let open: { line: number; last: number } | undefined;
  const close = (): void => {
    if (open === undefined) {
      return;
    }
    const { line, last } = open;
    open = undefined;
    const keyPath = keyPathAt(doc.text, line);
    const key = doc.shapes[line].key;
    if (keyPath === undefined || !sameKeyPath(keyPath, [...groupPath, key])) {
      return;
    }
    members.push({
      first: line,
      last,
      symbol: {
        name: key,
        detail: scalarValueAt(doc.text, line) ?? "",
        kind: MAP_KEY_KIND,
        range: lineSpan(doc, line, last),
        selection: keySpan(doc, line),
        children: [],
      },
    });
  };
  for (let i = keyLine + 1; i < doc.shapes.length; i += 1) {
    const shape = doc.shapes[i];
    if (isSkippable(shape)) {
      continue;
    }
    if (childCol === undefined) {
      if (shape.indent <= keyCol || shape.dashes.length > 0) {
        break;
      }
      childCol = shape.indent;
    }
    if (shape.indent > childCol) {
      if (open !== undefined) {
        open.last = i;
      }
      continue;
    }
    if (shape.indent < childCol || shape.dashes.length > 0) {
      break;
    }
    close();
    // A line at the members' column that is no key the scanner reads (a quoted key) is dropped, and
    // the map goes on.
    if (shape.kind === "key") {
      open = { line: i, last: i };
    }
  }
  close();
  if (childCol === undefined) {
    return undefined;
  }
  return finishGroup(doc, groupPath, keyLine, members, "Object");
}

function finishGroup(
  doc: Doc,
  groupPath: readonly string[],
  keyLine: number,
  members: Group["members"],
  kind: string,
): Group {
  const last = members.reduce((end, member) => Math.max(end, member.last), keyLine);
  const count = members.length;
  return {
    keyLine,
    members,
    symbol: {
      name: groupPath[groupPath.length - 1],
      detail: `${count} ${count === 1 ? "member" : "members"}`,
      kind,
      range: lineSpan(doc, keyLine, last),
      selection: keySpan(doc, keyLine),
      children: [],
    },
  };
}

/**
 * The outline of a file of *layer*: the element as the root, its declared collections as groups
 * and their members, nested by line containment (`operations` → `arguments`, `runnables` →
 * `events` / `reads` / `writes` / `calls`).
 *
 * - The **root** is the single column-0 identity line's value; without one the groups are listed
 *   at top level.
 * - A **group** is a key line whose key path equals a declared collection path of the file's
 *   top-level kind, with an empty value and at least one item (a list) or key (a map) below; a
 *   flow collection or a duplicated key is none.
 * - A list **member** is named by its identity field (`field`), its present identity values joined
 *   with `.` (`composite`), its discriminator value or its scalar (`whole_value`), else `[i]`; a map
 *   member by its key, its value's first line as the detail.
 *
 * An unknown layer answers nothing.
 */
export function outlineOf(refs: References, text: string, layer: string): OutlineSymbol[] {
  const facts = refs.layers.get(layer);
  if (facts === undefined) {
    return [];
  }
  const shapes = lineShapes(text);
  const doc: Doc = { text, lines: text.split(/\r?\n/), shapes };
  const topKind = topLevelKindOf(text, facts);
  const byLastStep = new Map<
    string,
    Array<{ collection: References["collections"][number]; path: readonly string[] }>
  >();
  if (topKind !== undefined) {
    for (const collection of refs.collections) {
      if (collection.layer !== layer) {
        continue;
      }
      for (const entry of collection.paths) {
        if (entry.topKind !== topKind) {
          continue;
        }
        const step = entry.path[entry.path.length - 1];
        const candidates = byLastStep.get(step) ?? [];
        candidates.push({ collection, path: entry.path });
        byLastStep.set(step, candidates);
      }
    }
  }

  const groups: Group[] = [];
  for (let line = 0; line < shapes.length; line += 1) {
    const shape = shapes[line];
    if (shape.kind !== "key" || shape.dashes.length > 1 || !hasEmptyValue(shape)) {
      continue;
    }
    const candidates = byLastStep.get(shape.key);
    if (candidates === undefined) {
      continue;
    }
    const keyPath = keyPathAt(text, line);
    const found =
      keyPath === undefined
        ? undefined
        : candidates.find((candidate) => sameKeyPath(candidate.path, keyPath));
    if (found === undefined || keyPath === undefined) {
      continue;
    }
    const group =
      found.collection.collectionKind === "map"
        ? mapGroup(doc, keyPath, line)
        : listGroup(doc, found.collection, keyPath, line);
    if (group !== undefined) {
      groups.push(group);
    }
  }

  // A group's parent is the innermost member of another group whose lines hold its key line.
  const topLevel: OutlineSymbol[] = [];
  for (const group of groups) {
    let parent: { first: number; symbol: OutlineSymbol } | undefined;
    for (const other of groups) {
      if (other === group) {
        continue;
      }
      for (const member of other.members) {
        if (
          member.first <= group.keyLine &&
          group.keyLine <= member.last &&
          (parent === undefined || member.first > parent.first)
        ) {
          parent = member;
        }
      }
    }
    (parent === undefined ? topLevel : parent.symbol.children).push(group.symbol);
  }
  for (const group of groups) {
    group.symbol.children = group.members.map((member) => member.symbol);
  }

  const identity = topLevelScalar(text, facts.identityField);
  if (identity.status !== "one") {
    return topLevel;
  }
  const lastLine = doc.lines.length - 1;
  return [
    {
      name: identity.value,
      detail: topKind ?? "",
      kind: rootKind(layer, topKind),
      range: lineSpan(doc, 0, lastLine),
      selection: tailSpan(doc, identity.line, 0),
      children: topLevel,
    },
  ];
}

// --- Go to Symbol in Workspace ------------------------------------------------------

/** One element as the workspace scan names it. */
export interface WorkspaceSymbol {
  /** The file's stem, or a `~N` file's identity. */
  name: string;
  /** The storage directory the file sits in, model-root-relative, POSIX: `swc/application`. */
  container: string;
  /** The layer's default root kind (the scan reads no `kind:` line). */
  kind: string;
  layer: string;
  file: string;
}

/** A collision file's stem: its identity is not its stem. */
const COLLISION_STEM = /~[0-9]+$/;

/** The case-insensitive subsequence match VS Code's API asks of a provider. */
export function matchesQuery(query: string, name: string): boolean {
  const wanted = query.toLowerCase();
  const candidate = name.toLowerCase();
  let at = 0;
  for (const char of wanted) {
    at = candidate.indexOf(char, at);
    if (at === -1) {
      return false;
    }
    at += 1;
  }
  return true;
}

/** One file of a layer as the directory scan lists it; nothing is read. */
export interface LayerFile {
  layer: string;
  /** The layer's facts, as the artifact declares them. */
  facts: LayerFacts;
  file: string;
  /** The file name without the layer's `fileSuffix`. */
  stem: string;
  /** The storage directory the file sits in, model-root-relative, POSIX: `swc/application`. */
  container: string;
}

/**
 * Every file of every layer under *modelRoot*, from a directory scan reading no file: each layer
 * directory (a nesting layer's group directories too, through `placesOf`) and each entry ending in
 * the layer's `fileSuffix` (case-exact, as the reader's glob matches), in the layers' order and
 * code-unit order within a directory. An unreadable directory is skipped; a flat layer's
 * subdirectory and other suffixes are not listed. Shared by Go to Symbol in Workspace and
 * `linkCore.ts`.
 */
export function* layerFiles(
  refs: References,
  modelRoot: string,
  reader: Reader,
): Generator<LayerFile> {
  for (const [layer, facts] of refs.layers) {
    const directory = path.join(modelRoot, ...facts.directory.split("/"));
    for (const { place, entries } of placesOf(facts, directory, reader)) {
      const container = path.relative(modelRoot, place).split(path.sep).join("/");
      for (const entry of [...entries].sort()) {
        if (!entry.endsWith(facts.fileSuffix) || entry.length === facts.fileSuffix.length) {
          continue;
        }
        const stem = entry.slice(0, entry.length - facts.fileSuffix.length);
        yield { layer, facts, file: path.join(place, entry), stem, container };
      }
    }
  }
}

/**
 * Every element under *modelRoot* whose name matches *query*. The name is the file's stem; a `~N`
 * file is read for its single column-0 identity line (refused: the stem).
 */
export function workspaceSymbols(
  refs: References,
  modelRoot: string,
  reader: Reader,
  query: string,
): WorkspaceSymbol[] {
  const found: WorkspaceSymbol[] = [];
  for (const { layer, facts, file, stem, container } of layerFiles(refs, modelRoot, reader)) {
    const name = isCollisionStem(stem) ? collisionName(facts, file, stem, reader) : stem;
    if (matchesQuery(query, name)) {
      found.push({ name, container, kind: rootKind(layer, undefined), layer, file });
    }
  }
  return found;
}

/** Is *stem* a collision file's (`Name~2`), whose identity is not its stem? */
export function isCollisionStem(stem: string): boolean {
  return COLLISION_STEM.test(stem);
}

/** The identity a stem is a storage-rule candidate of: `Name~2` → `Name`, any other stem itself. */
export function collisionBase(stem: string): string {
  return stem.replace(COLLISION_STEM, "");
}

function collisionName(facts: LayerFacts, file: string, stem: string, reader: Reader): string {
  return fileIdentity(facts, file, reader)?.value ?? stem;
}

/**
 * The 0-based line Go to Symbol opens *file* at, read on pick: its single column-0 identity line,
 * else line 0.
 */
export function resolveSymbolLine(facts: LayerFacts, file: string, reader: Reader): number {
  return fileIdentity(facts, file, reader)?.line ?? 0;
}

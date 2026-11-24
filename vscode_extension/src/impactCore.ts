// Pure logic of the Impact view (`swcraft impact --format json`'s panel): the tree's nodes with
// every label, id and context value, the view's description, and the element the command asks
// about, from its argument or the active editor.
//
// No `vscode` import: it runs under plain `node --test`; `commands.ts` wires it. The view computes
// nothing: the rows are the CLI's walk, parsed by navigationCore's `parseImpactDocument` (Find All
// References' parser too), and the question under the cursor is resolved by `definitionTarget`,
// Go to Definition's resolver.

import { layerOf } from "./layerFile";
import {
  definitionTarget,
  fileIdentity,
  type ImpactDocument,
  ImpactDocumentError,
  type ImpactRow,
  keyPathAt,
  layerOfToken,
  memberReferenceAt,
  parseImpactDocument,
  type Reader,
  type References,
  referenceAt,
  relationAt,
  scalarValueAt,
  topLevelScalar,
  unsavedIdentityAnswer,
} from "./navigationCore";
import { FAILED_ROW, resourceViewDescription } from "./resourceCore";

// --- The tree -----------------------------------------------------------------------

/** A layer node holding at most this many rows is expanded; above it, collapsed. */
export const IMPACT_EXPAND_AT_MOST = 20;

/** What every message of the command starts with; the dirty-buffer answer's prefix too. */
export const IMPACT_PREFIX = "SWCraft: Impact";

/** What every node id of the view starts with: how a command tells one of its nodes. */
export const IMPACT_ID_PREFIX = "impact:";

/** The four context values `package.json`'s `viewItem` clauses can name. */
export const IMPACT_ROOT_CONTEXT = "impact.root";
export const IMPACT_LAYER_CONTEXT = "impact.layer";
export const IMPACT_DEPENDENT_CONTEXT = "impact.dependent";
export const IMPACT_MESSAGE_CONTEXT = "impact.message";

/** The message row under a root nothing depends on. */
export const NO_DEPENDENTS_ROW = "Nothing depends on it.";

/** The one message row of an answer whose `matched` is empty. */
export function noLayerDeclaresRow(name: string): string {
  return `No layer declares '${name}'.`;
}

/** One element a node stands for: its layer token as the CLI wrote it, and its name. */
export interface ImpactElement {
  token: string;
  name: string;
}

/** One node of the tree. */
export interface ImpactNode {
  /** Carries the question: a Refresh keeps the expansion, a new question does not. */
  id: string;
  label: string;
  description: string;
  tooltip: string;
  contextValue: string;
  collapsible: "none" | "collapsed" | "expanded";
  children: ImpactNode[];
  /**
   * The element a click opens and a dependent row's inline action asks about: a root's matched
   * element, a row's dependent. Absent on a layer node, a message row and a root whose token no
   * layer carries.
   */
  element?: ImpactElement;
  /**
   * On a dependent row: its root's contract layer name, which `dependentLocations` places the row
   * against (a requirement root opens its direct dependents at the line naming it). Absent elsewhere
   * and when the root's token no layer carries; a click then passes `""`, which no relation targets,
   * so the row opens at its identity line.
   */
  rootLayer?: string;
  /**
   * On a root and its dependent rows: the name the root shows (the document's `name`, else the
   * question), the id `dependentLocations` looks for on a row's member lines.
   */
  rootName?: string;
}

/** What the view shows: the description beside its title and the root nodes. */
export interface ImpactTree {
  description: string;
  nodes: ImpactNode[];
}

/** The view's state as the description reads it; `commands.ts` decides *outOfDate*. */
export interface ImpactViewState {
  /** The name asked; the document's own `name` is preferred when it carries one. */
  question?: string;
  outOfDate: boolean;
  failed: boolean;
}

function dependentsText(count: number): string {
  if (count === 0) {
    return "no dependents";
  }
  return `${count} ${count === 1 ? "dependent" : "dependents"}`;
}

function messageNode(id: string, label: string): ImpactNode {
  return {
    id,
    label,
    description: "",
    tooltip: "",
    contextValue: IMPACT_MESSAGE_CONTEXT,
    collapsible: "none",
    children: [],
  };
}

/**
 * The layer nodes of one root: one per layer its *rows* name, in `references.json`'s order, then
 * one per token no layer carries, labelled with the token, in first-named order. Each holds its
 * rows in the walk's order; a row has no children (the walk is already transitive).
 */
function layerNodes(
  refs: References,
  rows: readonly ImpactRow[],
  rootId: string,
  rootLayer: string | undefined,
  rootName: string,
): ImpactNode[] {
  const byToken = new Map<string, ImpactRow[]>();
  for (const row of rows) {
    const group = byToken.get(row.layer);
    if (group === undefined) {
      byToken.set(row.layer, [row]);
    } else {
      group.push(row);
    }
  }
  const known: Array<{ token: string; label: string }> = [];
  for (const facts of refs.layers.values()) {
    if (byToken.has(facts.token)) {
      known.push({ token: facts.token, label: facts.name });
    }
  }
  const knownTokens = new Set(known.map((entry) => entry.token));
  const unknown = [...byToken.keys()]
    .filter((token) => !knownTokens.has(token))
    .map((token) => ({ token, label: token }));
  return [...known, ...unknown].map(({ token, label }) => {
    const group = byToken.get(token) ?? [];
    const layerId = `${rootId}:layer:${token}`;
    const seen = new Map<string, number>();
    const children = group.map((row): ImpactNode => {
      const count = (seen.get(row.name) ?? 0) + 1;
      seen.set(row.name, count);
      // One walk lists an element once; a repeat would still need an id of its own.
      const id = `${rootId}:row:${token}:${row.name}${count === 1 ? "" : `~${count}`}`;
      const detail = row.detail ?? "";
      const node: ImpactNode = {
        id,
        label: row.name,
        description: detail,
        tooltip: detail === "" ? `${label} '${row.name}'` : `${label} '${row.name}'\n${detail}`,
        contextValue: IMPACT_DEPENDENT_CONTEXT,
        collapsible: "none",
        children: [],
        element: { token, name: row.name },
        rootName,
      };
      if (rootLayer !== undefined) {
        node.rootLayer = rootLayer;
      }
      return node;
    });
    return {
      id: layerId,
      label,
      description: String(group.length),
      tooltip: "",
      contextValue: IMPACT_LAYER_CONTEXT,
      collapsible: group.length <= IMPACT_EXPAND_AT_MOST ? "expanded" : "collapsed",
      children,
    };
  });
}

/**
 * The Impact view after a run: one expanded root per entry of `matched`, in the document's order,
 * described `<layer> · <n> dependents`, its layer nodes ({@link layerNodes}) under it, or the one
 * {@link NO_DEPENDENTS_ROW}. An empty `matched` is the one {@link noLayerDeclaresRow} row, so the
 * tree is never empty after a run.
 *
 * With no *doc*: nothing (the welcome) or, after a failed run, the one `FAILED_ROW`. A failed run
 * with a document keeps its tree. The description is the Resources view's, by its precedence:
 * failed, then out of date, then empty.
 */
export function impactNodes(
  doc: ImpactDocument | undefined,
  refs: References,
  state: ImpactViewState,
): ImpactTree {
  const description = resourceViewDescription(state.outOfDate, state.failed);
  if (doc === undefined) {
    return {
      description,
      nodes: state.failed ? [messageNode(`${IMPACT_ID_PREFIX}message`, FAILED_ROW)] : [],
    };
  }
  const asked = doc.name ?? state.question ?? "";
  if (doc.matched.size === 0) {
    return {
      description,
      nodes: [messageNode(`${IMPACT_ID_PREFIX}${asked}:message`, noLayerDeclaresRow(asked))],
    };
  }
  const nodes = [...doc.matched].map(([token, rows]): ImpactNode => {
    const facts = layerOfToken(refs, token);
    const layerLabel = facts?.name ?? token;
    const rootId = `${IMPACT_ID_PREFIX}${asked}:${token}`;
    const children =
      rows.length === 0
        ? [messageNode(`${rootId}:message`, NO_DEPENDENTS_ROW)]
        : layerNodes(refs, rows, rootId, facts?.name, asked);
    const root: ImpactNode = {
      id: rootId,
      label: asked,
      description: `${layerLabel} · ${dependentsText(rows.length)}`,
      tooltip: `${layerLabel} '${asked}'`,
      contextValue: IMPACT_ROOT_CONTEXT,
      collapsible: "expanded",
      children,
      rootName: asked,
    };
    if (facts !== undefined) {
      root.element = { token, name: asked };
    }
    return root;
  });
  return { description, nodes };
}

/** The node a view command was invoked on — a click's or a menu's — or `undefined`. */
export function impactNodeOf(node: unknown): ImpactNode | undefined {
  return typeof node === "object" &&
    node !== null &&
    typeof (node as { id?: unknown }).id === "string" &&
    (node as { id: string }).id.startsWith(IMPACT_ID_PREFIX) &&
    typeof (node as { contextValue?: unknown }).contextValue === "string"
    ? (node as ImpactNode)
    : undefined;
}

// --- The view's commands and its run ------------------------------------------------

/**
 * Every command the Impact view reaches, in package.json's order: `swcraft.impact` itself (the
 * dependent row's inline action), its title's Refresh and a row's click.
 */
export const IMPACT_VIEW_COMMANDS: readonly string[] = [
  "swcraft.impact",
  "swcraft.impactRefresh",
  "swcraft.impactOpen",
];

/** The view's commands that spawn nothing: a row's click opens a file. */
export const IMPACT_NON_SPAWNING_COMMANDS: readonly string[] = ["swcraft.impactOpen"];

/**
 * The warning when a clicked node's file is not found: *layer* is the contract layer name, or the
 * token itself when no layer carries it.
 */
export function impactFileNotFoundMessage(layer: string, name: string): string {
  return `${IMPACT_PREFIX}: the file of ${layer} '${name}' was not found.`;
}

/** The contract layer name of *token*, or the token itself when no layer carries it. */
export function impactLayerLabel(refs: References, token: string): string {
  return layerOfToken(refs, token)?.name ?? token;
}

/**
 * An exit 0's stdout read as the walk document by `parseImpactDocument`, or its refusal as one
 * line (another `schemaVersion` names `DIFFERENT_RELEASES`). A refused document is a failed run:
 * the view keeps its last tree.
 */
export function readImpactDocument(stdout: string): ImpactDocument | { error: string } {
  try {
    return parseImpactDocument(stdout);
  } catch (error) {
    if (error instanceof ImpactDocumentError) {
      return { error: error.message };
    }
    throw error;
  }
}

/**
 * The one line a run leaves in the output channel after the CLI's own echo and stderr. *read* is
 * {@link readImpactDocument}'s answer for an exit 0, `undefined` for any other exit.
 */
export function impactRunLine(
  exitCode: number | null,
  read: ImpactDocument | { error: string } | undefined,
): string {
  if (read === undefined) {
    const exit = exitCode === null ? "no exit code" : `exit code ${exitCode}`;
    return `SWCraft: swcraft impact ended with ${exit}; the Impact view keeps its last answer.`;
  }
  return "error" in read
    ? `SWCraft: ${read.error}`
    : "SWCraft: the answer is shown in the Impact view (exit code 0).";
}

// --- The view's state across runs and folder changes --------------------------------

/** What the view keeps between runs; `commands.ts` holds it beside its busy flag and stamp. */
export interface ImpactAnswer {
  /** The last document the view could read; kept across a failed run. */
  document?: ImpactDocument;
  /**
   * The name the shown document answered, which Refresh asks again; with no document yet, the name
   * a failed run asked.
   */
  question?: string;
  /** The reference table the last run read, which the tree is drawn with. */
  refs?: References;
  /**
   * When the run that read *document* started: a change stamped after it marks the answer, even one
   * made during the run.
   */
  readAt?: number;
  /**
   * The workspace folder the held question was asked in; a change to another folder drops it.
   */
  root?: string;
  /** The last run failed: a non-zero exit, or a document the view could not read. */
  failed: boolean;
}

/** One finished run, as {@link impactAfterRun} reads it. */
export interface ImpactRunResult {
  /** The workspace folder the run was asked in. */
  root: string;
  name: string;
  refs: References;
  /** When the run started: a shown answer's `readAt`. */
  startedAt: number;
  /** {@link readImpactDocument}'s answer for an exit 0; `undefined` for any other exit. */
  read: ImpactDocument | { error: string } | undefined;
}

/**
 * The view's state after *run* (`commands.ts` drops a run that ended after a folder change). A
 * read document replaces the answer and takes the run's start as its stamp. A failure keeps the
 * last good document, its question and stamp, and marks the view failed; with no document yet it
 * holds the failed name, so Refresh retries it. Either way the run's folder is recorded.
 */
export function impactAfterRun<S extends ImpactAnswer>(state: S, run: ImpactRunResult): S {
  if (run.read === undefined || "error" in run.read) {
    return {
      ...state,
      question: state.document === undefined ? run.name : state.question,
      refs: run.refs,
      root: run.root,
      failed: true,
    };
  }
  return {
    ...state,
    document: run.read,
    question: run.name,
    refs: run.refs,
    readAt: run.startedAt,
    root: run.root,
    failed: false,
  };
}

/**
 * The view's state after a workspace-folder change, *openRoot* being the folder open first now:
 * what was asked in another folder is dropped with its stamp, so the view shows its welcome again;
 * otherwise *state* itself.
 */
export function impactAfterFolderChange<S extends ImpactAnswer>(
  state: S,
  openRoot: string | undefined,
): S {
  if (state.root === undefined || state.root === openRoot) {
    return state;
  }
  return {
    ...state,
    document: undefined,
    question: undefined,
    readAt: undefined,
    root: undefined,
    failed: false,
  };
}

// --- The question -------------------------------------------------------------------

/** The message when there is no element file to ask from (no editor, or not a layer file). */
export const NO_ELEMENT_FILE_MESSAGE = `${IMPACT_PREFIX}: open an element file of the model to ask what depends on its element.`;

/** The message when the file declares no single identity line. */
export const NO_IDENTITY_MESSAGE = `${IMPACT_PREFIX}: this file declares no single element name, so there is nothing to ask.`;

/** The message when the reference under the cursor resolves to nothing. */
export function notFoundMessage(value: string): string {
  return `${IMPACT_PREFIX}: '${value}' under the cursor is not found in the model, so there is nothing to ask.`;
}

/** The active text editor, as the command hands it in. */
export interface ImpactEditor {
  /** The document's file system path (a `file:` document's `fsPath`). */
  file: string;
  text: string;
  /** The primary cursor's 0-based line (`selection.active.line`). */
  line: number;
  isDirty: boolean;
}

/** What {@link impactQuestion} reads besides its argument. */
export interface ImpactQuestionContext {
  refs: References;
  /** The project's resolved `model_root`, or `undefined` when there is none. */
  modelRoot: string | undefined;
  /** The saved files: the targets' identities, the saved text of a dirty or a menu file. */
  reader: Reader;
  /** `vscode.window.activeTextEditor`, or `undefined` (none, or a non-text editor). */
  editor: ImpactEditor | undefined;
}

/** The command's answer: a name to ask the CLI about, or a message and no run. */
export type ImpactQuestion = { name: string } | { message: string };

/**
 * The `file:` path of a `Uri`-shaped *arg* (the editor context menu's document, the *impact*
 * lens's document), or `undefined`.
 */
function menuFileOf(arg: unknown): string | undefined {
  if (typeof arg !== "object" || arg === null) {
    return undefined;
  }
  const uri = arg as { scheme?: unknown; fsPath?: unknown };
  return uri.scheme === "file" && typeof uri.fsPath === "string" ? uri.fsPath : undefined;
}

/** The value of the reference *line* holds, by Go to Definition's three readers in its order. */
function referenceValueAt(
  refs: References,
  text: string,
  layer: string,
  line: number,
): string | undefined {
  const keyPath = keyPathAt(text, line);
  const edge = keyPath === undefined ? undefined : referenceAt(refs, text, layer, keyPath);
  const value = edge === undefined ? undefined : scalarValueAt(text, line);
  if (value !== undefined) {
    return value;
  }
  return (
    relationAt(refs, text, layer, line)?.value ?? memberReferenceAt(refs, text, layer, line)?.value
  );
}

/** The file's own element, read from *text*: its single identity line's value, or the message. */
function ownElement(
  ctx: ImpactQuestionContext,
  layer: string,
  text: string | undefined,
  dirtyFile: string | undefined,
): ImpactQuestion {
  const facts = ctx.refs.layers.get(layer);
  const read =
    facts === undefined || text === undefined
      ? undefined
      : topLevelScalar(text, facts.identityField);
  if (read?.status !== "one") {
    return { message: NO_IDENTITY_MESSAGE };
  }
  if (dirtyFile !== undefined) {
    const unsaved = unsavedIdentityAnswer(
      ctx.refs,
      ctx.reader.readText(dirtyFile),
      layer,
      read.value,
      IMPACT_PREFIX,
    );
    if (unsaved?.message !== undefined) {
      return { message: unsaved.message.text };
    }
  }
  return { name: read.value };
}

/**
 * The element `swcraft.impact` asks about, from its *arg*:
 *
 * 1. **a non-empty string** (the *impact* CodeLens's name) is that name; with *lensUri*, whose
 *    saved file declares another identity (an unsaved rename), it is the *save first* answer, and
 *    no run;
 * 2. **a dependent node** of the view (the row's inline action) is the row's name;
 * 3. **anything else** (the palette, the editor context menu's `Uri`) reads the **active editor**
 *    at its primary cursor's line:
 *    - a resolving reference asks about its target by `definitionTarget`, named by the target
 *      file's saved identity (`fileIdentity`); a member reference resolves to its **holder**; a
 *      target in this same file is the file's own element;
 *    - a reference that resolves to nothing is {@link notFoundMessage}, and no run; an edge with
 *      no readable value counts as none;
 *    - any other line asks about **the file's own element**: its single identity line, else
 *      {@link NO_IDENTITY_MESSAGE}; a dirty buffer whose saved file declares another identity is
 *      the *save first* answer, and no run.
 *
 *    A menu `Uri` that is not the active document's asks about that file's own element from its
 *    saved text, never another editor's cursor. No editor, or a file in no layer, is
 *    {@link NO_ELEMENT_FILE_MESSAGE}.
 *
 * **What the resolution does not reach**: a name the references do not declare as one (a
 * component in a `description`, a connector endpoint's `port:`, a task entry's `runnable:`) asks
 * about the file's own element; a member reference reaches its holder, never the member; a link in
 * a flow list is read as Go to Definition reads it; a `derived_from` element form
 * (`hardware:Core0`) asks about that element; the question is a **name**, so a target two layers
 * declare gives two roots. The CLI reads the saved model, so an unsaved edit's dependents are not
 * counted.
 */
export function impactQuestion(
  arg: unknown,
  ctx: ImpactQuestionContext,
  lensUri?: unknown,
): ImpactQuestion {
  if (typeof arg === "string" && arg !== "") {
    const lensFile = menuFileOf(lensUri);
    const lensLayer =
      lensFile === undefined ? undefined : layerOf(lensFile, ctx.modelRoot, ctx.refs.layers);
    const unsaved =
      lensFile === undefined || lensLayer === undefined
        ? undefined
        : unsavedIdentityAnswer(
            ctx.refs,
            ctx.reader.readText(lensFile),
            lensLayer,
            arg,
            IMPACT_PREFIX,
          );
    return unsaved?.message !== undefined ? { message: unsaved.message.text } : { name: arg };
  }
  const node = impactNodeOf(arg);
  if (node?.contextValue === IMPACT_DEPENDENT_CONTEXT && node.element !== undefined) {
    return { name: node.element.name };
  }
  const { refs, modelRoot, reader, editor } = ctx;
  const menuFile = menuFileOf(arg);
  if (menuFile !== undefined && (editor === undefined || editor.file !== menuFile)) {
    const layer = layerOf(menuFile, modelRoot, refs.layers);
    return layer === undefined
      ? { message: NO_ELEMENT_FILE_MESSAGE }
      : ownElement(ctx, layer, reader.readText(menuFile), undefined);
  }
  const layer = editor === undefined ? undefined : layerOf(editor.file, modelRoot, refs.layers);
  if (editor === undefined || modelRoot === undefined || layer === undefined) {
    return { message: NO_ELEMENT_FILE_MESSAGE };
  }
  const own = (): ImpactQuestion =>
    ownElement(ctx, layer, editor.text, editor.isDirty ? editor.file : undefined);
  const value = referenceValueAt(refs, editor.text, layer, editor.line);
  if (value === undefined) {
    return own();
  }
  const found = definitionTarget(
    refs,
    editor.text,
    layer,
    editor.line,
    editor.file,
    modelRoot,
    reader,
  );
  if (found === undefined) {
    return { message: notFoundMessage(value) };
  }
  if (found.target.file === editor.file) {
    return own();
  }
  const facts = refs.layers.get(found.targetLayer);
  const identity = facts === undefined ? undefined : fileIdentity(facts, found.target.file, reader);
  return identity === undefined ? { message: notFoundMessage(value) } : { name: identity.value };
}

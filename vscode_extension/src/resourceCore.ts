// Pure logic of the Resources view (`swcraft resource-report --format json`'s panel): the
// document's reader, the tree's nodes with every label, icon and colour, and the view's
// description. No `vscode` import: it runs under plain `node --test`; `commands.ts` wires it. The
// panel computes nothing: every figure, status and sentence is the CLI's (`resource_report.py`).

import { type CheckSubject, isRecord, parseSubject } from "./diagnosticsCore";
import { keyPathLine } from "./navigationCore";

// --- The document -------------------------------------------------------------------

/** `swcraft resource-report --format json`'s document version this extension reads. */
export const RESOURCE_REPORT_SCHEMA_VERSION = 1;

/** The two kinds of resource, as the document spells them. */
export type ResourceKind = "core" | "memory";
export const RESOURCE_KINDS: readonly ResourceKind[] = ["core", "memory"];

/** The three statuses `resource_analysis.budget_status` writes, verbatim. */
export const STATUS_OK = "within budget";
export const STATUS_BREACH = "over budget";
export const STATUS_UNKNOWN = "unknown (not every consumer declared a budget)";
export type ResourceStatus = typeof STATUS_OK | typeof STATUS_BREACH | typeof STATUS_UNKNOWN;
const STATUSES: ReadonlySet<string> = new Set([STATUS_OK, STATUS_BREACH, STATUS_UNKNOWN]);

/** The finding code whose entries the view folds into one row: the undeclared consumers name them. */
export const MISSING_BUDGET = "missing_budget";

/** One consumer of a resource: a core's task or a memory's component. */
export interface ResourceConsumer {
  name: string;
  layer: "tasks" | "swc";
  /** `format_amount`'s string: `12.00%`, `1536 bytes`, `not declared`. */
  amount: string;
  declared: boolean;
  /** A task's `wcet … / period …`; empty for a component. */
  detail: string;
}

/** One budget finding, unfolded, with its subjects in `Subject.to_json`'s form. */
export interface ResourceFinding {
  severity: "error" | "info";
  code: string;
  message: string;
  subjects: CheckSubject[];
}

/** One core or memory: its overview row, its consumers in the document's order and its findings. */
export interface ResourceRow {
  name: string;
  kind: ResourceKind;
  enforcement: "strict" | "advisory";
  /** A core's. */
  utilization?: string;
  /** A memory's. */
  size?: number;
  /** A memory's. */
  used?: string;
  limit: string;
  headroom: string;
  physical_headroom: string;
  status: ResourceStatus;
  consumers: ResourceConsumer[];
  findings: ResourceFinding[];
}

/** One `TOPOLOGY` record; it carries no subject, so its row has no click. */
export interface TopologyEntry {
  rule: string;
  message: string;
}

/**
 * The document, every key the view reads. The CLI's `fit` keys, a `--fit-*` run's
 * answer, are not read: a document from a terminal parses and draws no answer.
 */
export interface ResourceDocument {
  schemaVersion: 1;
  findings: { errors: number; informational: number };
  topology: TopologyEntry[];
  cores: ResourceRow[];
  memories: ResourceRow[];
}

/** A document the view cannot read: one line naming why. */
export interface ResourceRefusal {
  error: string;
}

/** The refusal of another `schemaVersion`: the toolchain and the extension are from different releases. */
export const OTHER_RELEASE =
  "This report was written by another SWCraft release; update the toolchain or the extension.";

class ShapeError extends Error {}

function stringAt(raw: Record<string, unknown>, key: string, where: string): string {
  const value = raw[key];
  if (typeof value !== "string") {
    throw new ShapeError(`${where}.${key} must be a string`);
  }
  return value;
}

function arrayAt(raw: Record<string, unknown>, key: string, where: string): unknown[] {
  const value = raw[key];
  if (!Array.isArray(value)) {
    throw new ShapeError(`${where}.${key} must be an array`);
  }
  return value;
}

function recordOf(raw: unknown, where: string): Record<string, unknown> {
  if (!isRecord(raw)) {
    throw new ShapeError(`${where} must be an object`);
  }
  return raw;
}

/** A subject in `Subject.to_json`'s form, by `check-model`'s reader: one parser of the one wire format. */
function readSubject(raw: unknown, where: string): CheckSubject {
  return parseSubject(raw, where, (message) => new ShapeError(message));
}

function parseConsumer(raw: unknown, where: string, layer: "tasks" | "swc"): ResourceConsumer {
  const record = recordOf(raw, where);
  if (record.layer !== layer) {
    throw new ShapeError(`${where}.layer must be "${layer}"`);
  }
  if (typeof record.declared !== "boolean") {
    throw new ShapeError(`${where}.declared must be a boolean`);
  }
  return {
    name: stringAt(record, "name", where),
    layer,
    amount: stringAt(record, "amount", where),
    declared: record.declared,
    detail: stringAt(record, "detail", where),
  };
}

function parseFinding(raw: unknown, where: string): ResourceFinding {
  const record = recordOf(raw, where);
  if (record.severity !== "error" && record.severity !== "info") {
    throw new ShapeError(`${where}.severity must be "error" or "info"`);
  }
  return {
    severity: record.severity,
    code: stringAt(record, "code", where),
    message: stringAt(record, "message", where),
    subjects: arrayAt(record, "subjects", where).map((subject, index) =>
      readSubject(subject, `${where}.subjects[${index}]`),
    ),
  };
}

function parseRow(raw: unknown, where: string, kind: ResourceKind): ResourceRow {
  const record = recordOf(raw, where);
  if (record.kind !== kind) {
    throw new ShapeError(`${where}.kind must be "${kind}"`);
  }
  if (record.enforcement !== "strict" && record.enforcement !== "advisory") {
    throw new ShapeError(`${where}.enforcement must be "strict" or "advisory"`);
  }
  const status = stringAt(record, "status", where);
  if (!STATUSES.has(status)) {
    throw new ShapeError(
      `${where}.status ${JSON.stringify(status)} is not a status this view knows`,
    );
  }
  const layer = kind === "core" ? "tasks" : "swc";
  const row: ResourceRow = {
    name: stringAt(record, "name", where),
    kind,
    enforcement: record.enforcement,
    limit: stringAt(record, "limit", where),
    headroom: stringAt(record, "headroom", where),
    physical_headroom: stringAt(record, "physical_headroom", where),
    status: status as ResourceStatus,
    consumers: arrayAt(record, "consumers", where).map((consumer, index) =>
      parseConsumer(consumer, `${where}.consumers[${index}]`, layer),
    ),
    findings: arrayAt(record, "findings", where).map((finding, index) =>
      parseFinding(finding, `${where}.findings[${index}]`),
    ),
  };
  if (kind === "core") {
    row.utilization = stringAt(record, "utilization", where);
  } else {
    if (typeof record.size !== "number" || !Number.isInteger(record.size)) {
      throw new ShapeError(`${where}.size must be a whole number`);
    }
    // `JSON.parse` rounds an integer above 2^53 - 1 to a neighbour that `Number.isInteger` still
    // accepts; the tooltip would show the wrong size, so such a document is refused.
    if (!Number.isSafeInteger(record.size)) {
      throw new ShapeError(
        `${where}.size is larger than this view can show exactly (${Number.MAX_SAFE_INTEGER} bytes)`,
      );
    }
    row.size = record.size;
    row.used = stringAt(record, "used", where);
  }
  return row;
}

/**
 * Read `swcraft resource-report --format json`'s stdout. Another `schemaVersion` is
 * {@link OTHER_RELEASE}; a document not of the CLI's shape is refused naming the first place that
 * is not, as a JSON-ish path (`memories[0].status …`). Never throws: every refusal is a reason the
 * caller writes into the output channel.
 */
export function parseResourceDocument(json: string): ResourceDocument | ResourceRefusal {
  let raw: unknown;
  try {
    raw = JSON.parse(json);
  } catch (error) {
    return {
      error: `the report is not JSON: ${error instanceof Error ? error.message : String(error)}`,
    };
  }
  if (!isRecord(raw)) {
    return { error: "the report is not a JSON object" };
  }
  if (raw.schemaVersion !== RESOURCE_REPORT_SCHEMA_VERSION) {
    return { error: OTHER_RELEASE };
  }
  try {
    const counts = recordOf(raw.findings, "findings");
    const errors = counts.errors;
    const informational = counts.informational;
    if (!Number.isInteger(errors) || !Number.isInteger(informational)) {
      throw new ShapeError("findings must count errors and informational as whole numbers");
    }
    return {
      schemaVersion: RESOURCE_REPORT_SCHEMA_VERSION,
      findings: { errors: errors as number, informational: informational as number },
      topology: arrayAt(raw, "topology", "the report").map((entry, index) => {
        const record = recordOf(entry, `topology[${index}]`);
        return {
          rule: stringAt(record, "rule", `topology[${index}]`),
          message: stringAt(record, "message", `topology[${index}]`),
        };
      }),
      cores: arrayAt(raw, "cores", "the report").map((row, index) =>
        parseRow(row, `cores[${index}]`, "core"),
      ),
      memories: arrayAt(raw, "memories", "the report").map((row, index) =>
        parseRow(row, `memories[${index}]`, "memory"),
      ),
    };
  } catch (error) {
    if (error instanceof ShapeError) {
      return { error: `the report cannot be read: ${error.message}` };
    }
    throw error;
  }
}

export function isResourceRefusal(
  read: ResourceDocument | ResourceRefusal,
): read is ResourceRefusal {
  return "error" in read;
}

// --- The tree -----------------------------------------------------------------------
//
// The colours are the theme ids the panel's other views use (`charts.green`, `charts.yellow`,
// `errorForeground`), plus `charts.blue` for an `info` finding; the icons are the Problems panel's
// `error` / `warning` / `info` and the Trace Links view's `check`.

/** A ThemeIcon by id, with an optional ThemeColor id. */
export interface ResourceIcon {
  id: string;
  color?: string;
}

export const ICON_YES: ResourceIcon = { id: "check", color: "charts.green" };
export const ICON_NO: ResourceIcon = { id: "error", color: "errorForeground" };
export const ICON_WARNING: ResourceIcon = { id: "warning", color: "charts.yellow" };
export const ICON_INFO: ResourceIcon = { id: "info", color: "charts.blue" };

/** A resource row's icon, by its status. */
export const STATUS_ICONS: Readonly<Record<ResourceStatus, ResourceIcon>> = {
  [STATUS_OK]: ICON_YES,
  [STATUS_BREACH]: ICON_NO,
  [STATUS_UNKNOWN]: ICON_WARNING,
};

/** One node of the tree. */
export interface ResourceNode {
  /** Stable over a refresh, so VS Code keeps a row's expansion. */
  id: string;
  label: string;
  description: string;
  /** Plain text. */
  tooltip: string;
  icon?: ResourceIcon;
  contextValue: string;
  collapsible: "none" | "collapsed" | "expanded";
  children: ResourceNode[];
  /**
   * What a click opens, as a finding's subject is placed: a core or a memory is
   * `{layer: "hardware", element}`, a consumer its own layer and name, a finding its first subject.
   * Absent: no click (a section, a topology record, the folded zeros, a message).
   */
  open?: CheckSubject;
  /**
   * The key paths, in preference order, whose line in *open*'s file answers the row
   * ({@link resourceLine}). Absent on a finding, which opens at its subject's place.
   */
  lineKeys?: readonly (readonly string[])[];
}

/** What the view shows: the description beside its title and the root nodes. */
export interface ResourceTree {
  description: string;
  nodes: ResourceNode[];
}

export const FAILED_DESCRIPTION = "the last run failed — see the SWCraft output channel";
export const FAILED_ROW = "The last run failed — see the SWCraft output channel.";
export const OUT_OF_DATE_DESCRIPTION = "out of date — Refresh";
export const NO_RESOURCES_ROW = "The model declares no core and no memory.";

const SECTION_LABELS: Readonly<Record<ResourceKind, string>> = {
  core: "Cores",
  memory: "Memories",
};
const KIND_PLURALS: Readonly<Record<ResourceKind, string>> = { core: "cores", memory: "memories" };

function plural(count: number, noun: string): string {
  return `${count} ${noun}${count === 1 ? "" : "s"}`;
}

/** The view's description, by precedence: a failed run, then an out-of-date tree, else empty. */
export function resourceViewDescription(outOfDate: boolean, failed: boolean): string {
  if (failed) {
    return FAILED_DESCRIPTION;
  }
  return outOfDate ? OUT_OF_DATE_DESCRIPTION : "";
}

/** The verb after `plural(count, …)`: `1 consumer declares`, `2 consumers declare`. */
function declares(count: number): string {
  return count === 1 ? "declares" : "declare";
}

/** A resource row's description, the CLI's strings: `14336 of 24576 bytes · headroom 10240`. */
export function resourceDescription(row: ResourceRow): string {
  return row.kind === "core"
    ? `${row.utilization} of ${row.limit} · headroom ${row.headroom}`
    : `${row.used} of ${row.limit} bytes · headroom ${row.headroom}`;
}

/** The physical headroom as the CLI's `PHYSICAL HEADROOM` line says it. */
function physicalText(row: ResourceRow): string {
  return row.kind === "core"
    ? `${row.physical_headroom} of the core`
    : `${row.physical_headroom} bytes of a ${row.size} byte size`;
}

/**
 * A resource row's tooltip: `strict · within budget · physical headroom 18432 bytes of a 32768
 * byte size · 5 consumers, 1 undeclared`, the undeclared count only when there is one.
 */
export function resourceTooltip(row: ResourceRow): string {
  const undeclared = row.consumers.filter((consumer) => !consumer.declared).length;
  const consumers =
    plural(row.consumers.length, "consumer") + (undeclared > 0 ? `, ${undeclared} undeclared` : "");
  return [row.enforcement, row.status, `physical headroom ${physicalText(row)}`, consumers].join(
    " · ",
  );
}

/** The zero a declared consumer of *kind* reads when it declares nothing more: `format_amount`'s spelling. */
function zeroAmount(kind: ResourceKind): string {
  return kind === "core" ? "0.00%" : "0 bytes";
}

/** The folded row's label: `3 consumers declare 0 bytes`, a core's `… declare 0%`. */
export function zerosLabel(kind: ResourceKind, count: number): string {
  return `${plural(count, "consumer")} ${declares(count)} 0${kind === "core" ? "%" : " bytes"}`;
}

/** The folded `missing_budget` row's label: `2 consumers declare no budget`. */
export function missingBudgetLabel(count: number): string {
  return `${plural(count, "consumer")} ${declares(count)} no budget`;
}

/** A consumer's description: its amount, and a task's detail in parentheses. */
function consumerDescription(consumer: ResourceConsumer): string {
  return consumer.detail === "" ? consumer.amount : `${consumer.amount} (${consumer.detail})`;
}

// --- The line a click opens at --------------------------------------------------

/** A core's or a memory's limit in its `hardware/` file: `threshold`, else `size`. */
const LIMIT_KEYS: readonly (readonly string[])[] = [["threshold"], ["size"]];

/**
 * A consumer's budget of *row*'s resource in its own file: a task's `wcet` (what a core's
 * utilization is computed from), or a component's `memory:` entry naming the memory.
 */
function budgetKeys(row: ResourceRow): readonly (readonly string[])[] {
  return row.kind === "core" ? [["wcet"]] : [["memory", row.name]];
}

/**
 * The 0-based line of *text* a row's click puts the cursor on: the line of the first of
 * *lineKeys* the text holds exactly once as a block-style key. Falls back to line 0 when the key
 * is absent, written twice, or inside a flow collection (`memory: {RAM0: 2048}` has no key line).
 */
export function resourceLine(text: string, lineKeys: readonly (readonly string[])[]): number {
  for (const keyPath of lineKeys) {
    const line = keyPathLine(text, keyPath);
    if (line !== undefined) {
      return line;
    }
  }
  return 0;
}

/**
 * The rows under one resource: the consumers in the document's order with the declared zeros
 * folded into one last row, then the findings, the `missing_budget` ones folded into one row at
 * the first one's place.
 */
function resourceChildren(row: ResourceRow, id: string): ResourceNode[] {
  const children: ResourceNode[] = [];
  const zero = zeroAmount(row.kind);
  let zeros = 0;
  row.consumers.forEach((consumer, index) => {
    if (consumer.declared && consumer.amount === zero) {
      zeros += 1;
      return;
    }
    children.push({
      id: `${id}:consumer:${index}`,
      label: consumer.name,
      description: consumerDescription(consumer),
      tooltip: "",
      icon: consumer.declared ? undefined : ICON_WARNING,
      contextValue: "resource.consumer",
      collapsible: "none",
      children: [],
      open: { layer: consumer.layer, element: consumer.name },
      lineKeys: budgetKeys(row),
    });
  });
  if (zeros > 0) {
    children.push({
      id: `${id}:zeros`,
      label: zerosLabel(row.kind, zeros),
      description: "",
      tooltip: "",
      contextValue: "resource.consumer",
      collapsible: "none",
      children: [],
    });
  }
  const missing = row.findings.filter((finding) => finding.code === MISSING_BUDGET);
  row.findings.forEach((finding, index) => {
    if (finding.code === MISSING_BUDGET && finding !== missing[0]) {
      return;
    }
    const node: ResourceNode = {
      id: `${id}:finding:${index}`,
      label: finding.code === MISSING_BUDGET ? missingBudgetLabel(missing.length) : finding.message,
      description: "",
      tooltip: finding.code === MISSING_BUDGET ? "" : finding.message,
      icon: finding.severity === "error" ? ICON_NO : ICON_INFO,
      contextValue: "resource.finding",
      collapsible: "none",
      children: [],
    };
    if (finding.subjects.length > 0) {
      node.open = finding.subjects[0];
    }
    children.push(node);
  });
  return children;
}

function messageNode(label: string): ResourceNode {
  return {
    id: "resource:message",
    label,
    description: "",
    tooltip: "",
    contextValue: "resource.message",
    collapsible: "none",
    children: [],
  };
}

/** What every node id of the view starts with: how a command tells one of its nodes. */
export const RESOURCE_ID_PREFIX = "resource:";

/** The node a view command was invoked on — a click's or a menu's — or `undefined`. */
export function resourceNodeOf(node: unknown): ResourceNode | undefined {
  return typeof node === "object" &&
    node !== null &&
    typeof (node as { id?: unknown }).id === "string" &&
    (node as { id: string }).id.startsWith(RESOURCE_ID_PREFIX) &&
    typeof (node as { contextValue?: unknown }).contextValue === "string"
    ? (node as ResourceNode)
    : undefined;
}

/**
 * The Resources view after a run: the *Cores*, *Memories* and *Topology (n)* sections, each shown
 * only with a member and expanded, every resource row collapsed. A document with no section is the
 * one {@link NO_RESOURCES_ROW}, so the tree is never empty after a run.
 *
 * With no *document*: nothing (the welcome) or, after a failed run, the one {@link FAILED_ROW}. A
 * failed run with a document keeps that document's tree.
 */
export function resourceNodes(
  document: ResourceDocument | undefined,
  outOfDate: boolean,
  failed: boolean,
): ResourceTree {
  const description = resourceViewDescription(outOfDate, failed);
  if (document === undefined) {
    return { description, nodes: failed ? [messageNode(FAILED_ROW)] : [] };
  }
  const nodes: ResourceNode[] = [];
  for (const kind of RESOURCE_KINDS) {
    const rows = kind === "core" ? document.cores : document.memories;
    if (rows.length === 0) {
      continue;
    }
    const seen = new Map<string, number>();
    const children = rows.map((row) => {
      const count = (seen.get(row.name) ?? 0) + 1;
      seen.set(row.name, count);
      // Two hardware elements of one name (`hardware.duplicate-name`) would share an id.
      const id = `resource:${kind}:${row.name}${count === 1 ? "" : `~${count}`}`;
      const rowChildren = resourceChildren(row, id);
      const node: ResourceNode = {
        id,
        label: row.name,
        description: resourceDescription(row),
        tooltip: resourceTooltip(row),
        icon: STATUS_ICONS[row.status],
        contextValue: `resource.${kind}`,
        collapsible: rowChildren.length === 0 ? "none" : "collapsed",
        children: rowChildren,
        open: { layer: "hardware", element: row.name },
        lineKeys: LIMIT_KEYS,
      };
      return node;
    });
    nodes.push({
      id: `resource:section:${KIND_PLURALS[kind]}`,
      label: SECTION_LABELS[kind],
      description: "",
      tooltip: "",
      contextValue: "resource.section",
      collapsible: "expanded",
      children,
    });
  }
  if (document.topology.length > 0) {
    nodes.push({
      id: "resource:section:topology",
      label: `Topology (${document.topology.length})`,
      description: "",
      tooltip: "",
      contextValue: "resource.section",
      collapsible: "expanded",
      children: document.topology.map((record, index) => ({
        id: `resource:topology:${index}`,
        label: `${record.rule}: ${record.message}`,
        description: "",
        tooltip: `${record.rule}: ${record.message}`,
        icon: ICON_NO,
        contextValue: "resource.topology",
        collapsible: "none",
        children: [],
      })),
    });
  }
  if (
    document.cores.length === 0 &&
    document.memories.length === 0 &&
    document.topology.length === 0
  ) {
    nodes.push(messageNode(NO_RESOURCES_ROW));
  }
  return { description, nodes };
}

// --- The view's commands and its runs -----------------------------------------------

/** Every command of the Resources view, in the order package.json declares them. */
export const RESOURCE_VIEW_COMMANDS: readonly string[] = [
  "swcraft.resourcesRefresh",
  "swcraft.resourcesOpen",
];

/** The view's commands that spawn nothing: the open opens a file. */
export const RESOURCE_NON_SPAWNING_COMMANDS: readonly string[] = ["swcraft.resourcesOpen"];

/** What a run of the report is asked for: *report* true is a press, which also writes the file. */
export type ResourceReportOptions = { report: boolean };

/**
 * The output channel's closing line of one run, below the run's echo and stderr. *read* is the
 * parser's answer for an exit 0, `undefined` for any other exit. With *report* (a press), exit 2
 * is taken for a release without `--report`, the only option a press adds; it cannot be told from
 * another usage error, and the stderr above says which.
 */
export function resourceRunLine(
  exitCode: number | null,
  read: ResourceDocument | ResourceRefusal | undefined,
  { report }: ResourceReportOptions,
): string {
  if (read !== undefined && isResourceRefusal(read)) {
    return `SWCraft: ${read.error}`;
  }
  if (read !== undefined) {
    return "SWCraft: the report is shown in the Resources view (exit code 0).";
  }
  if (report && exitCode === 2) {
    return "SWCraft: swcraft refused the report's options: it is another SWCraft release; update the toolchain or the extension. The Resources view keeps its last report.";
  }
  const exit = exitCode === null ? "no exit code" : `exit code ${exitCode}`;
  return `SWCraft: swcraft resource-report ended with ${exit}; the Resources view keeps its last report.`;
}

/**
 * The warning when the element a clicked row names is not in the model as saved. A layer-wide
 * finding names none and opens at the manifest's first line, as the Problems panel does.
 */
export function resourceNotFoundMessage(subject: { layer: string; element: string }): string {
  return `SWCraft: '${subject.element}' (${subject.layer}) is not in the model as saved; refresh the Resources view.`;
}

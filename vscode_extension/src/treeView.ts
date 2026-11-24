// Pure logic: SWCraft Activity Bar TreeView item descriptors.
//
// No `vscode` import: it runs under plain `node --test`. `commands.ts`'s
// `SwcraftTreeDataProvider`, the thin adapter and the only place that constructs `vscode.TreeItem`
// instances, converts these descriptors into VS Code API objects.
//
// Each command item's `commandId` names a command `registerCommands()` already registers; the
// adapter wires the item's click to it, so the tree never duplicates a command's logic.

/** The result of one command run, before the adapter stamps it with a completion time. */
export type CommandOutcome = { kind: "success" } | { kind: "failure"; message: string };

/**
 * A recorded command status: an outcome plus the instant the run completed. The stamp lets
 * `describeStatus()` render a time and decide whether the status is still inside the window; it
 * is applied only in `recordCommandStatus()` in commands.ts, the one clock read on this path.
 */
export type CommandStatus = CommandOutcome & { at: Date };

/**
 * How long a recorded status stays on its tree item, in milliseconds. `describeStatus()` renders
 * nothing past it, and the adapter arms a `setTimeout` of the same length to drop the status and
 * refresh. The renderer is the authority; the timer only makes the clear prompt.
 */
export const STATUS_WINDOW_MS = 30_000;

/** An item's text while its command runs; the outcome replaces it. */
export const RUNNING_TEXT = "running…";

/** An item's tooltip while its command runs: where the run can be followed. */
const RUNNING_TOOLTIP = "Running — the SWCraft output channel is showing it.";

/** Which of the three TreeView sections a command item is filed under. */
export type Section = "model" | "experiments" | "setup";

export interface ProjectState {
  /** Absolute path to the open workspace root, or undefined when no folder is open. */
  workspaceRoot: string | undefined;
  /** Whether `swcraft.yaml` was detected at the workspace root. */
  hasManifest: boolean;
}

export interface ProjectNodeDescriptor {
  label: string;
  description: string | undefined;
}

export interface CommandItemDescriptor {
  /** Stable id for the tree item, distinct from `commandId`. */
  id: string;
  label: string;
  /** The VS Code command id this item invokes when clicked. */
  commandId: string;
  /** Last-run status suffix shown as the tree item description, or undefined if never run. */
  description: string | undefined;
  /** The failure detail and the artifact's state, as the tree item tooltip, or undefined for neither. */
  tooltip: string | undefined;
  /** The mark of an out-of-date artifact, drawn by the adapter as a `ThemeIcon`; undefined otherwise. */
  icon: ArtifactIcon | undefined;
}

export interface SectionDescriptor {
  /**
   * The section tree item's id; never equal to any command item's id. Model's and Experiments' are
   * their `TREE_SECTIONS` id; Setup's carries its default and a generation (`setupSectionId()`), so
   * a changed default reaches VS Code.
   */
  id: string;
  label: string;
  /** Whether the adapter draws this section collapsed by default; only Setup ever is. */
  collapsedByDefault: boolean;
  /** The command items filed under this section, in display order. */
  items: CommandItemDescriptor[];
}

interface CommandDefinition {
  id: string;
  label: string;
  commandId: string;
  /** Which TreeView section this command is grouped under. */
  section: Section;
  /**
   * Past-tense verb naming what a successful run did ("built", "checked", ...), declared beside the
   * label so the status text is a per-command fact, not a switch in the renderer.
   */
  verb: string;
}

/**
 * The commands of the Activity Bar TreeView, grouped into the three sections `buildTree()` renders,
 * in display order. The count is deliberately not written here: `packageCommands.test.ts` derives
 * it from the manifest and pins every `commandId` here to a declared `contributes.commands` entry,
 * and every declared entry back to an item here, to `PANEL_MENU_COMMANDS` or to the Trace Links,
 * Resources or Impact view. Impact is asked from the element's file and answered in the Impact
 * view, so it has no item.
 *
 * Model: `Build` first (it ends with `swcraft validate`, the gate), `Check Model` under it, then
 * `Diagrams`, the read-only reports, and the three commands that ask a question before they run.
 *
 * Experiments: one item per `EXPERIMENT_KINDS` kind; no artifact state, since a save regenerates
 * no report.
 *
 * Setup, in the order of doing it: `Install Toolchain`, `Update Toolchain`, `Onboard Project`.
 * Help and diagnostics live in the panel's `…` menu (`PANEL_MENU_COMMANDS`).
 */
export const TREE_COMMAND_DEFINITIONS: readonly CommandDefinition[] = [
  {
    id: "build",
    label: "Build",
    commandId: "swcraft.build",
    section: "model",
    verb: "built",
  },
  {
    id: "checkModel",
    label: "Check Model",
    commandId: "swcraft.checkModel",
    section: "model",
    verb: "checked",
  },
  {
    id: "diagrams",
    label: "Diagrams",
    commandId: "swcraft.diagrams",
    section: "model",
    verb: "opened",
  },
  {
    id: "testTables",
    label: "Test Tables",
    commandId: "swcraft.testTables",
    section: "model",
    verb: "generated",
  },
  {
    id: "changeReport",
    label: "Change Report",
    commandId: "swcraft.changeReport",
    section: "model",
    verb: "reported",
  },
  {
    id: "resourceReport",
    label: "Resource Report",
    commandId: "swcraft.resourceReport",
    section: "model",
    verb: "reported",
  },
  {
    id: "metrics",
    label: "Architecture Metrics",
    commandId: "swcraft.metrics",
    section: "model",
    verb: "reported",
  },
  {
    id: "exportComponent",
    label: "Export Component",
    commandId: "swcraft.exportComponent",
    section: "model",
    verb: "exported",
  },
  {
    id: "importComponent",
    label: "Import Component",
    commandId: "swcraft.importComponent",
    section: "model",
    verb: "imported",
  },
  {
    id: "importRequirements",
    label: "Import Requirements",
    commandId: "swcraft.importRequirements",
    section: "model",
    verb: "imported",
  },
  {
    id: "experimentAllocation",
    label: "Allocation (CARMA)",
    commandId: "swcraft.experimentAllocation",
    section: "experiments",
    verb: "ran",
  },
  {
    id: "experimentTrace",
    label: "Trace Links",
    commandId: "swcraft.experimentTrace",
    section: "experiments",
    verb: "ran",
  },
  {
    id: "installToolchain",
    label: "Install Toolchain",
    commandId: "swcraft.installToolchain",
    section: "setup",
    verb: "installed",
  },
  {
    id: "updateToolchain",
    label: "Update Toolchain",
    commandId: "swcraft.updateToolchain",
    section: "setup",
    verb: "updated",
  },
  {
    id: "onboardProject",
    label: "Onboard Project",
    commandId: "swcraft.onboardProject",
    section: "setup",
    verb: "onboarded",
  },
];

/**
 * The entries of the SWCraft panel's `…` menu (`package.json`'s `menus["view/title"]` for
 * `view == swcraftView`), in order. A group other than `navigation` puts an entry in the overflow
 * menu rather than on the title bar; `9_help` keeps them at its foot, where VS Code keeps help. The
 * guide leads: it is the general question. Both keep their Command Palette entries;
 * `packageCommands.test.ts` holds the manifest's entries to exactly this list.
 */
export const PANEL_MENU_COMMANDS: readonly { commandId: string; group: string }[] = [
  { commandId: "swcraft.openGuide", group: "9_help@1" },
  { commandId: "swcraft.checkEditorTooling", group: "9_help@2" },
];

/**
 * The three sections, in display order, with their labels. VS Code's TreeView has no separator
 * item, so the grouping is real collapsible nodes. Model and Experiments are always expanded by
 * default; Setup is collapsed once the project is set up (`setupSectionCollapsed()`).
 *
 * A section id is prefixed so it never collides with a command item's id (VS Code requires tree
 * item ids unique across the tree). Setup's `section:setup` is a base: the id drawn is
 * `section:setup:<expanded|collapsed>:<generation>` (`setupSectionId()`).
 */
export const TREE_SECTIONS: readonly { id: string; label: string; section: Section }[] = [
  { id: "section:model", label: "Model", section: "model" },
  { id: "section:experiments", label: "Experiments", section: "experiments" },
  { id: "section:setup", label: "Setup", section: "setup" },
];

/** Setup's id base, found by section rather than position, which `setupSectionId()` extends. */
const SETUP_SECTION_BASE_ID = setupSectionEntryId();

function setupSectionEntryId(): string {
  const entry = TREE_SECTIONS.find((candidate) => candidate.section === "setup");
  if (entry === undefined) {
    throw new Error("TREE_SECTIONS carries no Setup section");
  }
  return entry.id;
}

// --- The Setup section's default: collapsed once the project is set up --------------
//
// Setup is out of the way once it has done its job. The adapter reads the three facts from the
// disk; whether they collapse the section, and the id that makes VS Code apply a changed default,
// are decided here.

/** The three facts that say the window's project is set up. The adapter reads them; nothing here does. */
export interface SetupFacts {
  /** The resolved `swcraft` executable is a file on disk. */
  toolchainInstalled: boolean;
  /** `swcraft.yaml` is at the workspace root — the project node's own fact. */
  hasManifest: boolean;
  /** The agent profile Onboard Project always writes (and `swcraft init` alone does not) is a file. */
  hasAgentProfile: boolean;
}

/**
 * Whether Setup is collapsed by default: exactly when the toolchain is installed, a project is
 * open and it is onboarded. Any fact missing leaves it expanded: a setup command is next.
 */
export function setupSectionCollapsed(facts: SetupFacts): boolean {
  return facts.toolchainInstalled && facts.hasManifest && facts.hasAgentProfile;
}

/**
 * Setup's default and its generation. VS Code keeps an item's expansion by its id and ignores a
 * changed `collapsibleState` for an id it has drawn, so the id carries the default, and a
 * generation, because two fixed ids would restore an old toggle (a user who collapsed the expanded
 * default, finished setup, then lost the profile would get his collapse back with a fact missing).
 */
export interface SetupSectionState {
  collapsedByDefault: boolean;
  generation: number;
}

/** The state `buildTree()` draws when the adapter passes none: expanded, generation 0. */
const INITIAL_SETUP_SECTION: SetupSectionState = { collapsedByDefault: false, generation: 0 };

/**
 * Advance Setup's state: *previous* unchanged while the default holds (so the id and the user's
 * toggling are kept), the next generation when it changed; with no previous state, generation 0.
 */
export function nextSetupSection(
  previous: SetupSectionState | undefined,
  collapsedByDefault: boolean,
): SetupSectionState {
  if (previous === undefined) {
    return { ...INITIAL_SETUP_SECTION, collapsedByDefault };
  }
  if (previous.collapsedByDefault === collapsedByDefault) {
    return previous;
  }
  return { collapsedByDefault, generation: previous.generation + 1 };
}

/** Setup's tree item id for *state*: `section:setup:<expanded|collapsed>:<generation>`. */
export function setupSectionId(state: SetupSectionState): string {
  return `${SETUP_SECTION_BASE_ID}:${state.collapsedByDefault ? "collapsed" : "expanded"}:${state.generation}`;
}

/** `workspaceState` key: Setup's last state, so a window reload draws the same id while the default holds. */
export const SETUP_SECTION_KEY = "swcraft.setupSection";

/** The two commands that swap the installed toolchain while they run. */
const TOOLCHAIN_SWAP_COMMANDS: readonly string[] = [
  "swcraft.installToolchain",
  "swcraft.updateToolchain",
];

/** *value* as a `SetupSectionState`, or `undefined` when the memento holds nothing of that shape. */
function readSetupSection(value: unknown): SetupSectionState | undefined {
  if (typeof value !== "object" || value === null) {
    return undefined;
  }
  const { collapsedByDefault, generation } = value as Record<string, unknown>;
  if (
    typeof collapsedByDefault !== "boolean" ||
    typeof generation !== "number" ||
    !Number.isSafeInteger(generation) ||
    generation < 0
  ) {
    return undefined;
  }
  return { collapsedByDefault, generation };
}

/**
 * Setup's state across refreshes and window reloads, kept in the workspace memento: VS Code
 * restores an item's expansion by its id after a reload, and restarting at generation 0 would draw
 * another id and drop the user's toggle.
 *
 * While Install or Update Toolchain runs, the facts are not read and the last state is kept: the
 * installer swaps the installed copy, and a refresh then would see no executable and move the
 * default twice. The run's closing refresh reads them again. With no state yet the facts are read
 * regardless.
 */
export class SetupSectionRecord {
  private state: SetupSectionState | undefined;

  constructor(private readonly memento: ArtifactMemento) {
    this.state = readSetupSection(memento.get(SETUP_SECTION_KEY));
  }

  /** The state to draw now; *collapsedByDefault* is called only when the facts are read. */
  advance(collapsedByDefault: () => boolean, running: RunningSet): SetupSectionState {
    const swapping = TOOLCHAIN_SWAP_COMMANDS.some((commandId) => running.has(commandId));
    if (swapping && this.state !== undefined) {
      return this.state;
    }
    const next = nextSetupSection(this.state, collapsedByDefault());
    if (next !== this.state) {
      this.state = next;
      void this.memento.update(SETUP_SECTION_KEY, next);
    }
    return next;
  }
}

/**
 * The root node describing the detected project, shown above the sections so the tree says which
 * project (if any) the commands act on.
 */
export function buildProjectNode(state: ProjectState): ProjectNodeDescriptor {
  if (state.workspaceRoot === undefined) {
    return { label: "No workspace open", description: "Open a folder to get started" };
  }
  if (!state.hasManifest) {
    return { label: "No SWCraft project detected", description: "Run Onboard Project" };
  }
  return { label: "SWCraft project detected", description: state.workspaceRoot };
}

/** `HH:MM` in the viewer's local time, the wall clock they just ran the command by. */
function formatClockTime(at: Date): string {
  const hours = String(at.getHours()).padStart(2, "0");
  const minutes = String(at.getMinutes()).padStart(2, "0");
  return `${hours}:${minutes}`;
}

/**
 * One command item's status line: a verb and a time, `built 12:39`, `failed 12:41`.
 *
 * *verb* is declared beside the command's label in `TREE_COMMAND_DEFINITIONS`. A failure renders
 * `failed` for every command: what failed is the item's own label beside it.
 *
 * *now* is a parameter, never a clock read, so the string is a function of its arguments. A status
 * older than `STATUS_WINDOW_MS` renders nothing, tooltip included, even if the adapter's timer
 * never fires (a reload, a disposed provider). A status stamped in the future (a clock stepped
 * backwards) is treated as current.
 */
export function describeStatus(
  status: CommandStatus | undefined,
  verb: string,
  now: Date,
): {
  description: string | undefined;
  tooltip: string | undefined;
} {
  if (status === undefined) {
    return { description: undefined, tooltip: undefined };
  }
  if (now.getTime() - status.at.getTime() >= STATUS_WINDOW_MS) {
    return { description: undefined, tooltip: undefined };
  }
  const time = formatClockTime(status.at);
  if (status.kind === "success") {
    return { description: `${verb} ${time}`, tooltip: undefined };
  }
  return { description: `failed ${time}`, tooltip: status.message };
}

/**
 * Build the two-level command tree: one descriptor per section, each holding its command items in
 * `TREE_COMMAND_DEFINITIONS`' order with their last-run status (*statuses*, keyed by command id) as
 * description and tooltip.
 *
 * Pure: it does not read or write any state and never calls a clock; *now* is supplied by the
 * adapter, which owns the status map and calls this on every `getChildren()`. A status older than
 * `STATUS_WINDOW_MS` at *now* renders as none.
 *
 * *artifacts* is each marked output's state, rendered on the item that renews it
 * (`MARKED_DEFINITIONS`) by `describeArtifactItem()`; an output it does not name marks nothing.
 *
 * *running* holds the command ids with a run in flight: such an item reads `RUNNING_TEXT` in place
 * of the last run's text and the artifact's words until the adapter records the outcome (*out of
 * date → running… → built 12:39*). The icon stays the artifact's state, and the badge
 * (`describeBadge()`) never sees a run.
 *
 * *setup* is the Setup section's state (`nextSetupSection()`), deciding its `collapsedByDefault`
 * and id; absent, Setup is expanded at generation 0.
 */
export function buildTree(
  statuses: Readonly<Partial<Record<string, CommandStatus>>>,
  now: Date,
  artifacts: ArtifactStates = {},
  running: RunningSet = new Set<string>(),
  setup: SetupSectionState = INITIAL_SETUP_SECTION,
): SectionDescriptor[] {
  return TREE_SECTIONS.map((section) => ({
    id: section.section === "setup" ? setupSectionId(setup) : section.id,
    label: section.label,
    collapsedByDefault: section.section === "setup" ? setup.collapsedByDefault : false,
    items: TREE_COMMAND_DEFINITIONS.filter((def) => def.section === section.section).map((def) => {
      const status = running.has(def.commandId)
        ? { description: RUNNING_TEXT, tooltip: RUNNING_TOOLTIP }
        : describeStatus(statuses[def.commandId], def.verb, now);
      const artifact = MARKED_DEFINITIONS.find((entry) => entry.commandId === def.commandId);
      const state = artifact === undefined ? undefined : artifacts[artifact.artifact];
      const { description, tooltip, icon } =
        artifact === undefined || state === undefined
          ? { ...status, icon: undefined }
          : describeArtifactItem(status, artifact, state);
      return { id: def.id, label: def.label, commandId: def.commandId, description, tooltip, icon };
    }),
  }));
}

// --- The artifacts' state: absent, current, out of date -----------------------------
//
// A save regenerates nothing. The two artifacts a reader waits for, the ARXML and the diagram
// pages, are each marked on the item that generates them with one of three states, decided here
// from facts the adapter gathers (the clock and the disk), so every state is a function of its
// arguments.
//
// Two channels that do not mix: the item's *text* is the last run while the status window keeps
// it, then the artifact's state; the *icon* and the view's *badge* are the artifact's state alone.
// So a failed run is never counted by the badge, and a state is never hidden by a run.
//
// A run's outcome is never coloured, which would put a presentation rule into the adapter. What is
// coloured is an artifact's state, a fact about a file on disk; the rule choosing the mark
// (`OUT_OF_DATE_ICON`) lives in this pure half, and the adapter only turns it into a `ThemeIcon`.

/** The two artifacts on disk the panel marks, and whose generations `ArtifactRecord` keeps. */
export type Artifact = "arxml" | "views";

/**
 * Everything the panel marks: the two artifacts on disk, and the Resources view's report
 * (`resources`). The mark is the view's document's state (`decideResourceReportState()`), never
 * the file a press writes, drawn on *Resource Report* and counted by the badge. Only
 * `ArtifactRecord` and `ArtifactDefinition` mean a file on disk; elsewhere in the marking
 * vocabulary *artifact* names any marked output.
 */
export type MarkedOutput = Artifact | "resources";

/**
 * An artifact's state.
 *
 * - `absent`: the file is not on disk (a fresh clone, after `clean-outputs`, after a `Build` that
 *   failed at `check-model`, whose first step moved the ARXML aside as `model_old.arxml`, never
 *   read here). Not marked and not counted.
 * - `current`: the last successful generation started at or after the newest change of the model or
 *   the manifest.
 * - `outOfDate`: the file is on disk and either the model or manifest changed after the last
 *   successful generation, or none is recorded (a `Build` failed at `validate`, a terminal's file).
 */
export type ArtifactState = "absent" | "current" | "outOfDate";

/** The state of each artifact, as far as the adapter knows it. */
export type ArtifactStates = Readonly<Partial<Record<MarkedOutput, ArtifactState>>>;

/** Everything the rule reads. Times are milliseconds on the file system's wall clock. */
export interface ArtifactFacts {
  /** The artifact's file exists: the manifest's `outputs.arxml`, or `views_root/index.html`. */
  onDisk: boolean;
  /** When the last successful generation of it *started*, or undefined when none is recorded. */
  lastSuccessAt: number | undefined;
  /** The newest change under `model_root` or of the manifest, or undefined when none is known. */
  newestChangeAt: number | undefined;
}

/**
 * The state rule. A change stamped at the very instant a generation started counts as read by it;
 * a generation is stamped with its *start*, so a change made while it ran marks its output.
 */
export function decideArtifactState(facts: ArtifactFacts): ArtifactState {
  if (!facts.onDisk) {
    return "absent";
  }
  if (facts.lastSuccessAt === undefined) {
    return "outOfDate";
  }
  if (facts.newestChangeAt !== undefined && facts.newestChangeAt > facts.lastSuccessAt) {
    return "outOfDate";
  }
  return "current";
}

/**
 * The state of the report the Resources view shows: none before the first run (the view's document
 * is no file to call *not generated*), otherwise `decideArtifactState()` with the run that read the
 * shown document as the generation, stamped with its start, so a change made during the run marks
 * it *out of date*. A failed run keeps the last document and so its stamp and state: the failure
 * is the item's text, never its mark.
 */
export function decideResourceReportState(facts: {
  /** When the run that read the shown document started; `undefined` with none shown. */
  readAt: number | undefined;
  /** The newest change under `model_root` or of the manifest the watch stamped. */
  newestChangeAt: number | undefined;
}): ArtifactState | undefined {
  if (facts.readAt === undefined) {
    return undefined;
  }
  return decideArtifactState({
    onDisk: true,
    lastSuccessAt: facts.readAt,
    newestChangeAt: facts.newestChangeAt,
  });
}

/** Whether *state* carries the *out of date* mark — what the Resources view's description reads. */
export function isOutOfDate(state: ArtifactState | undefined): boolean {
  return state === "outOfDate";
}

/** One marked output: the item that renews it, and the words its sentences use. */
export interface MarkedDefinition {
  artifact: MarkedOutput;
  /** The command whose item shows the artifact's state, and which renews it. */
  commandId: string;
  /** The artifact as a sentence names it. */
  name: string;
  /** Whether *name* takes a plural verb ("the diagrams are", "the ARXML is"). */
  plural: boolean;
}

/** One artifact on disk: a marked output `ArtifactRecord` keeps the generations of. */
export interface ArtifactDefinition extends MarkedDefinition {
  artifact: Artifact;
}

/**
 * The two artifacts, in the badge tooltip's order: the ARXML is `Build`'s (whose last step is its
 * gate), the views `Diagrams`'. A sentence names the item by its label in
 * `TREE_COMMAND_DEFINITIONS`, so a relabel moves the sentence with it.
 */
export const ARTIFACT_DEFINITIONS: readonly ArtifactDefinition[] = [
  { artifact: "arxml", commandId: "swcraft.build", name: "the ARXML", plural: false },
  { artifact: "views", commandId: "swcraft.diagrams", name: "the diagrams", plural: true },
];

/**
 * Everything the tree marks and the badge counts, in the badge tooltip's order: the two artifacts,
 * then the Resources view's report, renewed by `Resource Report`.
 */
export const MARKED_DEFINITIONS: readonly MarkedDefinition[] = [
  ...ARTIFACT_DEFINITIONS,
  {
    artifact: "resources",
    commandId: "swcraft.resourceReport",
    name: "the resource report",
    plural: false,
  },
];

/** The label of *definition*'s item, as a sentence tells the reader to press it. */
function pressLabel(definition: MarkedDefinition): string {
  const item = TREE_COMMAND_DEFINITIONS.find(
    (candidate) => candidate.commandId === definition.commandId,
  );
  return item?.label ?? definition.commandId;
}

/** A codicon and a theme colour id, which the adapter turns into `new ThemeIcon(id, new ThemeColor(color))`. */
export interface ArtifactIcon {
  id: string;
  color: string;
}

/** The mark of an out-of-date artifact: a filled dot in the theme's yellow. */
export const OUT_OF_DATE_ICON: ArtifactIcon = { id: "circle-filled", color: "charts.yellow" };

/** The sentence an artifact's state adds to its item's tooltip, or undefined for `current`. */
function artifactSentence(definition: MarkedDefinition, state: ArtifactState): string | undefined {
  const name = definition.name.charAt(0).toUpperCase() + definition.name.slice(1);
  const press = pressLabel(definition);
  if (state === "outOfDate") {
    return (
      `${name} ${definition.plural ? "are" : "is"} out of date: the model or the ` +
      `manifest changed after the last successful generation — press ${press} to ` +
      "generate again."
    );
  }
  if (state === "absent") {
    return `${name} ${definition.plural ? "have" : "has"} not been generated — press ${press}.`;
  }
  return undefined;
}

/**
 * The item of an artifact's command: the last run's text while its status shows, the state's words
 * after it, the state's mark as the icon, and both sentences in the tooltip.
 */
function describeArtifactItem(
  status: { description: string | undefined; tooltip: string | undefined },
  definition: MarkedDefinition,
  state: ArtifactState,
): {
  description: string | undefined;
  tooltip: string | undefined;
  icon: ArtifactIcon | undefined;
} {
  const stateText =
    state === "outOfDate" ? "out of date" : state === "absent" ? "not generated" : undefined;
  const sentence = artifactSentence(definition, state);
  const tooltip = [status.tooltip, sentence].filter((part) => part !== undefined).join("\n\n");
  return {
    description: status.description ?? stateText,
    tooltip: tooltip === "" ? undefined : tooltip,
    icon: state === "outOfDate" ? OUT_OF_DATE_ICON : undefined,
  };
}

/**
 * The view's badge: how many marked outputs are out of date, with a tooltip naming them, or
 * undefined (hidden) when none is. An absent artifact and a failed run count nothing.
 */
export function describeBadge(
  artifacts: ArtifactStates,
): { value: number; tooltip: string } | undefined {
  const outOfDate = MARKED_DEFINITIONS.filter(
    (definition) => artifacts[definition.artifact] === "outOfDate",
  );
  if (outOfDate.length === 0) {
    return undefined;
  }
  const named = outOfDate.map(
    (definition) => `${definition.name} (press ${pressLabel(definition)})`,
  );
  return { value: outOfDate.length, tooltip: `Out of date: ${named.join(", ")}.` };
}

// --- The record behind the states: what the adapter keeps between events ------------
//
// The adapter reads the disk and the clock; these classes hold what it read, and every transition
// is a function of their arguments.

/** The ids of the commands with a run in flight: what `buildTree()`'s `running` reads. */
export interface RunningSet {
  has(commandId: string): boolean;
}

/**
 * The runs in flight, counted per command id. Two presses of one command can overlap (the second
 * queued in the adapter's serialised runner), so the id stays until the *last* run finishes; a set
 * would drop `running…` while the second is still queued or running.
 */
export class RunningCommands implements RunningSet {
  private readonly counts = new Map<string, number>();

  /** One more run of *commandId* is in flight. */
  start(commandId: string): void {
    this.counts.set(commandId, (this.counts.get(commandId) ?? 0) + 1);
  }

  /** One run of *commandId* finished, whatever its outcome. */
  finish(commandId: string): void {
    const count = (this.counts.get(commandId) ?? 0) - 1;
    if (count > 0) {
      this.counts.set(commandId, count);
    } else {
      this.counts.delete(commandId);
    }
  }

  has(commandId: string): boolean {
    return this.counts.has(commandId);
  }
}

/** `vscode.Memento`'s two methods the record uses, without importing `vscode`. */
export interface ArtifactMemento {
  get<T>(key: string): T | undefined;
  update(key: string, value: unknown): unknown;
}

/** The memento key of *artifact*'s last successful generation in *folder*. */
export function lastGenerationKey(artifact: Artifact, folder: string): string {
  return `swcraft.lastSuccessfulGeneration.${artifact}:${folder}`;
}

/**
 * The facts `decideArtifactState()` reads for the project open in this window, minus *on disk*,
 * which the adapter asks when the tree is drawn.
 *
 * - **The last successful generation**, per artifact and folder, is kept in the memento
 *   (`workspaceState`), so it survives a reload.
 * - **The newest change** is window-wide and re-read by `rescan()` whenever the watch is armed. A
 *   rescan starts a new *generation*: a scan or watcher stamp for a folder no longer shown lands
 *   after the reset and is dropped. A change noted while a scan of the *same* folder runs is kept,
 *   folded in by the maximum.
 *
 * A second half is **in memory only**, never written to the memento: which artifacts were generated
 * *in this window* (`windowGenerations`) and when the latest watcher event *arrived*
 * (`lastArrivalAt`, whatever mtime the file carries). `opensDirectly()` reads both, because the
 * persisted half is computed from mtimes and survives a reload: after a restart, or a `cp -p` into
 * `model_root`, the views can read `current` by mtimes alone, which is not enough for the diagram
 * lens's direct open. A `rescan()` clears it (a new arming may be a new folder); a reload empties
 * it, so the first lens click after one regenerates.
 */
export class ArtifactRecord {
  private newestChangeAt: number | undefined;
  private generation = 0;
  /** Artifact × folder (`lastGenerationKey`) → the start of its generation in this window. */
  private readonly windowGenerations = new Map<string, number>();
  /** The delivery instant of the latest watcher event in this window, or undefined. */
  private lastArrivalAt: number | undefined;

  constructor(
    private readonly state: ArtifactMemento,
    private readonly scan: (folder: string) => Promise<number | undefined>,
    private readonly onDidChange: () => void,
  ) {}

  /** Forget the known change and read the newest one under *folder*. */
  async rescan(folder: string): Promise<void> {
    this.generation += 1;
    const generation = this.generation;
    this.newestChangeAt = undefined;
    this.windowGenerations.clear();
    this.lastArrivalAt = undefined;
    this.onDidChange();
    const scanned = await this.scan(folder);
    if (generation !== this.generation) {
      return;
    }
    if (scanned !== undefined) {
      this.moveNewest(scanned);
    }
  }

  /**
   * A stamp for one watcher event arriving at *arrivedAt*: call it with the event's instant once
   * known; a stamp that outlives a rescan is dropped. The arrival itself is recorded at once and
   * raises no `onDidChange`: it changes only what `opensDirectly()` answers.
   */
  changeStamp(arrivedAt: number): (at: number) => void {
    this.lastArrivalAt =
      this.lastArrivalAt === undefined ? arrivedAt : Math.max(this.lastArrivalAt, arrivedAt);
    const generation = this.generation;
    return (at) => {
      if (generation === this.generation) {
        this.moveNewest(at);
      }
    };
  }

  /** Record *artifact*'s successful generation in *folder*, started at *startedAt*. */
  recordGeneration(artifact: Artifact, folder: string, startedAt: number): void {
    void this.state.update(lastGenerationKey(artifact, folder), startedAt);
    this.windowGenerations.set(lastGenerationKey(artifact, folder), startedAt);
    this.onDidChange();
  }

  /**
   * Forget *artifact*'s generation in *folder* in this window, after a failed run that may have left
   * its output partly rewritten, so the next `opensDirectly()` answers false. Nothing drawn changes,
   * so no redraw is raised.
   */
  forgetWindowGeneration(artifact: Artifact, folder: string): void {
    this.windowGenerations.delete(lastGenerationKey(artifact, folder));
  }

  /**
   * Record what one `Build` run in *folder* left generated: its first step moved the ARXML aside and
   * removed both roots, so an artifact it did not generate has no success any more.
   */
  recordBuild(folder: string, startedAt: number, generated: Record<Artifact, boolean>): void {
    for (const definition of ARTIFACT_DEFINITIONS) {
      const key = lastGenerationKey(definition.artifact, folder);
      void this.state.update(key, generated[definition.artifact] ? startedAt : undefined);
      if (generated[definition.artifact]) {
        this.windowGenerations.set(key, startedAt);
      } else {
        this.windowGenerations.delete(key);
      }
    }
    this.onDidChange();
  }

  /**
   * May *artifact*'s output in *folder* be opened with no generation? True exactly when `states()`
   * reads it `current`, a generation of it is recorded in this window, and no watcher event arrived
   * at or after that generation's start (an event during the run counts against it).
   */
  opensDirectly(artifact: Artifact, folder: string, onDisk: Record<Artifact, boolean>): boolean {
    if (this.states(folder, onDisk)[artifact] !== "current") {
      return false;
    }
    const startedAt = this.windowGenerations.get(lastGenerationKey(artifact, folder));
    if (startedAt === undefined) {
      return false;
    }
    return this.lastArrivalAt === undefined || this.lastArrivalAt < startedAt;
  }

  /** Each artifact's state in *folder*, given which of the artifacts' files exist. */
  states(folder: string, onDisk: Record<Artifact, boolean>): ArtifactStates {
    const states: Partial<Record<Artifact, ArtifactState>> = {};
    for (const definition of ARTIFACT_DEFINITIONS) {
      states[definition.artifact] = decideArtifactState({
        onDisk: onDisk[definition.artifact],
        lastSuccessAt: this.state.get<number>(lastGenerationKey(definition.artifact, folder)),
        newestChangeAt: this.newestChangeAt,
      });
    }
    return states;
  }

  private moveNewest(at: number): void {
    const before = this.newestChangeAt;
    this.newestChangeAt = before === undefined ? at : Math.max(before, at);
    if (this.newestChangeAt !== before) {
      this.onDidChange();
    }
  }
}

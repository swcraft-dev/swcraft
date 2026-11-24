// Pure logic: argv construction, manifest resolution and exit-code mapping, shared by the
// SWCraft commands.
//
// No `vscode` import (this module and its imports run under plain `node --test`);
// `commands.ts` wires it to the VS Code API. The import report's helpers, `liesOutside()`
// and `withImportReportFile()`, touch files only through the `fs` functions they are handed.

import type { ChildProcess } from "node:child_process";
import type * as nodeFs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

import { isListedView } from "./diagrams";
import { findManifest, MANIFEST_FILENAME } from "./project";
import type { ResourceReportOptions } from "./resourceCore";
import type { Platform } from "./toolchain";
import type { Artifact } from "./treeView";

// --- Platform detection ------------------------------------------------------

/**
 * Map Node's `process.platform` to the `Platform` type `toolchain.ts` uses to select POSIX
 * vs. Windows path layouts.
 */
export function currentPlatform(nodePlatform: NodeJS.Platform = process.platform): Platform {
  return nodePlatform === "win32" ? "windows" : "posix";
}

// --- Manifest resolution ---------------------------------------------------

export interface ManifestResolved {
  ok: true;
  /** Absolute path to the detected `swcraft.yaml`. */
  manifestPath: string;
  /** Path relative to the workspace root, forward-slash normalized. */
  manifestRelPath: string;
}

export interface ManifestError {
  ok: false;
  /** User-facing error message, suitable for a VS Code error notification. */
  message: string;
}

export type ManifestResolution = ManifestResolved | ManifestError;

/**
 * Resolve the project manifest for *workspaceRoot*, or produce the
 * user-facing error result when no `swcraft.yaml` is detected.
 */
export function resolveManifest(workspaceRoot: string): ManifestResolution {
  const manifestPath = findManifest(workspaceRoot);
  if (manifestPath === undefined) {
    return {
      ok: false,
      message: `SWCraft: no ${MANIFEST_FILENAME} found at the workspace root. Open a SWCraft project workspace to use this command.`,
    };
  }
  const manifestRelPath = path
    .relative(workspaceRoot, manifestPath)
    .split(/[\\/]+/)
    .join("/");
  return { ok: true, manifestPath, manifestRelPath };
}

// --- CLI argv construction --------------------------------------------------

/**
 * The CLI passthrough commands: a `swcraft` subcommand this extension spawns and whose text
 * output it shows verbatim in the output channel.
 *
 * `impact` and `resource-report` add **no** behaviour beyond their argv: nothing is parsed,
 * and `resource-report`'s exit code is passed through (0 even over budget, because
 * `swcraft validate` is the gate and the read-only commands are not). `check-model` is the
 * cheap edit loop: the same model checks as `validate`, writing nothing and skipping the
 * AUTOSAR XSD compilation that dominates `validate` on a host with a licensed schema.
 *
 * `validate` and `generate diagrams` run as steps of `Build` (see `buildBuildSteps()` for
 * why a step is not a kind). `change-report` answers the working tree against `HEAD` as a
 * written Markdown report the panel opens in the preview; its run reads the
 * `Generated report:` line back from stdout (`experimentReportPath()`; a Resource Report
 * press reads it from stderr, its stdout being the view's document).
 */
export type CliCommandKind =
  | "change-report"
  | "check-model"
  | "impact"
  | "metrics"
  | "resource-report";

/**
 * The VS Code command id each kind is registered under, and the key its last-run status is
 * recorded against in the TreeView's status map.
 *
 * A table, not string surgery on the kind: the ids are also spelled in package.json's
 * `contributes.commands`, in `registerCommands()` and in `TREE_COMMAND_DEFINITIONS`, held in
 * step by `packageCommands.test.ts`.
 */
export const COMMAND_IDS: Record<CliCommandKind, string> = {
  "change-report": "swcraft.changeReport",
  "check-model": "swcraft.checkModel",
  impact: "swcraft.impact",
  metrics: "swcraft.metrics",
  "resource-report": "swcraft.resourceReport",
};

/**
 * The `swcraft` subcommand words each kind spawns, as the `cli/` package's parsers spell them
 * (`change-report` and `resource-report` are each one hyphenated subcommand).
 */
const CLI_SUBCOMMANDS: Record<CliCommandKind, readonly string[]> = {
  "change-report": ["change-report"],
  "check-model": ["check-model"],
  impact: ["impact"],
  metrics: ["metrics"],
  "resource-report": ["resource-report"],
};

/** The one kind whose subcommand takes a positional argument. */
const KIND_TAKING_AN_ARGUMENT: CliCommandKind = "impact";

/**
 * Build the argv (after the resolved `swcraft` executable) for a CLI passthrough command.
 *
 * *arg* is the positional only `impact` takes, trimmed so pasted whitespace never reads as
 * "element not found"; an *arg* for any other kind throws rather than being dropped. The
 * other kinds take nothing but `--project`: `change-report`'s default (HEAD vs. the working
 * tree) is the panel's question, and `metrics`' revisions are {@link metricsArgv}'s.
 *
 * The positional comes **last, after a bare `--`**: argparse reads a token starting with `-`
 * as an option (exit 2), and a `Requirement.id` is a free string.
 *
 * No production caller spawns the `impact` argv (the Impact view runs
 * {@link buildImpactTreeArgv}); the kind stays as the status key, over which
 * `CLI_SUBCOMMANDS` is total.
 */
export function buildCliArgv(
  kind: CliCommandKind,
  manifestRelPath: string,
  arg?: string,
): string[] {
  if (kind === KIND_TAKING_AN_ARGUMENT) {
    if (arg === undefined || arg.trim() === "") {
      throw new Error(`the '${kind}' command needs an element name`);
    }
  } else if (arg !== undefined) {
    throw new Error(`the '${kind}' command takes no positional argument`);
  }
  const positional = arg === undefined ? [] : ["--", arg.trim()];
  return [...CLI_SUBCOMMANDS[kind], "--project", manifestRelPath, ...positional];
}

// --- Build and Diagrams' generation ------------------------------------------

/** One step of `Build`: the words a warning names it by, and the argv it spawns. */
export interface BuildStep {
  /** The subcommand words of `argv`, as the `$ swcraft …` echo shows them. */
  label: string;
  /** The argv after the resolved `swcraft` executable, `--project` included. */
  argv: readonly string[];
  /**
   * True for the one step whose findings fill the Problems panel (`check-model`).
   * It only routes the step: the adapter runs it with `--format json` appended
   * ({@link checkModelJsonArgv}), and its exit code still decides as every
   * step's does. `argv` itself stays the template `moon.yml` task's.
   */
  feedsDiagnostics?: boolean;
  /**
   * True for the one step whose stdout names the build report (`validate`, its
   * argv ending in `--report`): the adapter keeps that stdout for the report's
   * preview ({@link buildRunEnding}). Its exit code decides as every step's does.
   */
  writesReport?: boolean;
}

/**
 * `generate diagrams`' subcommand words: `Build`'s fourth step and `Diagrams`' first, spelled
 * once so the two can never differ.
 */
const GENERATE_DIAGRAMS_SUBCOMMAND: readonly string[] = ["generate", "diagrams"];

/** The step whose findings fill the Problems panel: `check-model`, Build's second. */
const CHECK_MODEL_SUBCOMMAND: readonly string[] = ["check-model"];

/** Build's last step, the gate whose `--report` writes the build report. */
const VALIDATE_SUBCOMMAND: readonly string[] = ["validate"];

/**
 * The subcommand words of each `Build` step, in the order they run.
 *
 * **The order is *a* topological order of the template's task graph, not *the* order**:
 * `clean-outputs` and `check-model` first because every generator depends on them,
 * `validate` last because it depends on all three generators (its report lists what they
 * wrote); the generators are siblings, in the order the template's header reads.
 * `commandCore.test.ts` asserts every edge of `templates/vehicle-architecture/moon.yml` is
 * respected and that these are exactly the tasks `moon run :build` reaches.
 */
const BUILD_STEP_SUBCOMMANDS: readonly (readonly string[])[] = [
  ["clean-outputs"],
  CHECK_MODEL_SUBCOMMAND,
  ["generate", "arxml"],
  GENERATE_DIAGRAMS_SUBCOMMAND,
  ["generate", "needs"],
  VALIDATE_SUBCOMMAND,
];

/**
 * The words `validate`'s argv carries after `--project <manifest>`: `--report`, which writes
 * `<dist_root>/build-report.md`, as the template's `validate-arxml` args do. The report never
 * changes `validate`'s exit code.
 */
const VALIDATE_TRAILING_ARGS: readonly string[] = ["--report"];

/**
 * The six steps of `SWCraft: Build`, each run with `--project <manifest>`: exactly what
 * `moon run :build` runs in the template, in one of that graph's orders. `check-model` is
 * marked `feedsDiagnostics`; `validate` is marked `writesReport`, its argv ending in
 * `--report`.
 *
 * **A step is not a `CliCommandKind`.** A kind is 1:1 with a command id and a status key;
 * `Build` records **one** status for the whole run, which six kinds would turn into six
 * records overwriting each other.
 */
export function buildBuildSteps(manifestRelPath: string): readonly BuildStep[] {
  return BUILD_STEP_SUBCOMMANDS.map((subcommand) => {
    const label = subcommand.join(" ");
    if (subcommand === CHECK_MODEL_SUBCOMMAND) {
      return { label, argv: [...subcommand, "--project", manifestRelPath], feedsDiagnostics: true };
    }
    if (subcommand === VALIDATE_SUBCOMMAND) {
      const argv = [...subcommand, "--project", manifestRelPath, ...VALIDATE_TRAILING_ARGS];
      return { label, argv, writesReport: true };
    }
    return { label, argv: [...subcommand, "--project", manifestRelPath] };
  });
}

/**
 * *baseArgv* (`buildCliArgv("check-model", …)`'s, or Build's marked step's) with
 * `--format json` appended: the run whose stdout is the one document of the findings the
 * Problems panel shows. Appended, never inserted, so every pinned argv stays what it is.
 */
export function checkModelJsonArgv(baseArgv: readonly string[]): string[] {
  return [...baseArgv, "--format", "json"];
}

/**
 * *baseArgv* (`buildCliArgv("resource-report", …)`'s) with `--format json` appended: the
 * Resources view's run, whose stdout is the document the view draws. With *report* (a press
 * of Resource Report) `--report` follows and the run also writes the report the press opens;
 * the view's Refresh and the refresh after a green Build write no file, so a green Build
 * opens one tab, its build report.
 */
export function resourceReportJsonArgv(
  baseArgv: readonly string[],
  { report }: ResourceReportOptions,
): string[] {
  return [...baseArgv, "--format", "json", ...(report ? ["--report"] : [])];
}

/**
 * The setting that holds the revisions Architecture Metrics compares against: a string
 * array, empty by default, resource scope. Each entry is a revision the CLI reads as git
 * reads one, never a program.
 */
export const METRICS_SINCE_SETTING = "swcraft.metricsSince";

/**
 * *baseArgv* (`buildCliArgv("metrics", …)`'s) with `--report`, so the run writes
 * `<dist_root>/metrics/metrics.md` and prints its `Generated report:` line, then one
 * `--since=<rev>` word per entry of *since* in order (the first is the base, the others
 * points of the series), or `--since-head` when *since* is empty, so the click compares
 * against HEAD as Change Report does. The CLI refuses the two together as a usage error.
 *
 * Each revision is **one word**, `--since=<rev>`: a separate word opening with `-` would be
 * read by argparse as an option (exit 2), while the joined word reaches the command, whose
 * refusal names the revision. Entries are passed untrimmed, as the user spelled them.
 */
export function metricsArgv(baseArgv: readonly string[], since: readonly string[]): string[] {
  const base = since.length === 0 ? ["--since-head"] : since.map((rev) => `--since=${rev}`);
  return [...baseArgv, "--report", ...base];
}

/**
 * The refusal of a `swcraft.metricsSince` value that is not a list of strings (a
 * hand-edited `settings.json` can hold anything; VS Code's schema only warns), or
 * `undefined` for a usable one. Unset reads as the empty list.
 */
export function metricsSinceRefusal(value: unknown): string | undefined {
  if (
    value === undefined ||
    value === null ||
    (Array.isArray(value) && value.every((entry) => typeof entry === "string"))
  ) {
    return undefined;
  }
  return `SWCraft: the setting ${METRICS_SINCE_SETTING} must be a list of revisions, so the metrics did not run`;
}

/**
 * `swcraft generate diagrams --project <manifest>`, the generation `Diagrams` runs, built
 * from the same words as `Build`'s step.
 */
export function buildGenerateDiagramsArgv(manifestRelPath: string): string[] {
  return [...GENERATE_DIAGRAMS_SUBCOMMAND, "--project", manifestRelPath];
}

/**
 * Runs tasks one at a time, in the order `run()` was called: a task started while another
 * runs waits for it, and a task that rejects does not stop the next (its caller still
 * receives the rejection).
 *
 * Two things write `views_root` in one window: `Build` (whose `clean-outputs` deletes the
 * directory) and a `Diagrams` press. Unserialised, a press during `Build` races the removal,
 * which can fail with "Directory not empty", so the adapter sends both through one instance.
 *
 * **`busy`** is true from the call that queues a task until the last queued task has
 * settled, and turns false before that task's caller resumes; the toolchain commands read it
 * to refuse while a `Build` or a generation runs.
 *
 * What it does **not** reach: two windows on one folder are two instances, and a `swcraft`
 * run from a terminal goes through neither; both can still race a `Build`.
 */
export class SerialRunner {
  private tail: Promise<unknown> = Promise.resolve();
  private pending = 0;

  get busy(): boolean {
    return this.pending > 0;
  }

  run<T>(task: () => Promise<T>): Promise<T> {
    this.pending += 1;
    const result = this.tail
      .then(() => task())
      .finally(() => {
        this.pending -= 1;
      });
    this.tail = result.then(
      () => undefined,
      () => undefined,
    );
    return result;
  }
}

/**
 * The sentence `Build` and `Diagrams` refuse with while `Install Toolchain` or
 * `Update Toolchain` runs in the same window: an update repoints the installation's
 * `current` link and removes the other version directories, so an overlapping `Build` could
 * run later steps from another version or lose its tree. The toolchain command refuses the
 * other way round with `viewsRootWriteRunningRefusal()`.
 */
export function toolchainRunningRefusal(title: string): string {
  return (
    `SWCraft: ${title} was not started — a toolchain install is running in this window and may ` +
    `replace the installation ${title} runs from; run ${title} again when it has finished ` +
    "(the SWCraft output channel is showing it)."
  );
}

/**
 * The sentence `Install Toolchain` and `Update Toolchain` refuse with while a `Build`, its
 * closing refresh or a `Diagrams` generation runs in the same window
 * (`toolchainRunningRefusal()`'s other direction). It names no command: both toolchain
 * commands refuse with the same sentence.
 */
export function viewsRootWriteRunningRefusal(): string {
  return (
    "SWCraft: the toolchain run was not started — a Build or a diagram generation is running in " +
    "this window from the installation it may replace; run the toolchain command again when it " +
    "has finished (the SWCraft output channel is showing it)."
  );
}

/**
 * What a finished CLI run answers its caller and its failure notification: the deciding
 * exit code, the child's whole stderr, and whether it could not be launched at all. A
 * refusal the CLI words on stderr (a project pinned to another release) is recognised from
 * it, never from the exit code alone, and a timed-out run (`exitCode: null`, started, so
 * not `spawnFailed`) is never mistaken for one that was not.
 */
export interface RunOutput {
  exitCode: number | null;
  stderr: string;
  spawnFailed: boolean;
}

/** What one `Build` run came to: success, or the first failing step with its run. */
export type BuildRunResult =
  | { kind: "success" }
  | { kind: "failure"; step: BuildStep; run: RunOutput };

/**
 * Run *steps* in order through *runStep* and stop at the first whose exit code is not `0`,
 * a `null` included (an executable that could not be started, which the next steps would
 * only repeat). The failure carries that step's run only. Pure apart from *runStep*.
 */
export async function runBuildSteps(
  steps: readonly BuildStep[],
  runStep: (step: BuildStep, index: number) => Promise<RunOutput>,
): Promise<BuildRunResult> {
  for (const [index, step] of steps.entries()) {
    const { exitCode, stderr, spawnFailed } = await runStep(step, index);
    if (mapExitCodeToOutcome(exitCode).kind !== "success") {
      return { kind: "failure", step, run: { exitCode, stderr, spawnFailed } };
    }
  }
  return { kind: "success" };
}

// --- What a running command says -----------------------------------------------
//
// While a run is in flight the panel says so in three places and raises no popup: the
// view's progress bar, a status-bar text, and the item's `running…` (treeView.ts's
// `RUNNING_TEXT`). **What is shown is only what is known**: `Build`'s six steps are started
// here, so its text names the step and its place; every other command is one run whose
// progress the CLI does not report, so it gets words and no fraction. No percentage of
// time, and no cancel: stopping a run is its own decision.

/**
 * Every panel command that **spawns the toolchain**, and the name its indicator calls it by.
 *
 * A table, since a label is not a transformation of an id, and the **one** place the set is
 * written: `commandCore.test.ts` derives the set of ids `commands.ts` actually wraps from
 * its source and asserts the two equal.
 *
 * `swcraft.checkEditorTooling` is a `…` menu entry (treeView.ts's `PANEL_MENU_COMMANDS`), so
 * it has no `running…` text; the progress bar and status-bar text still show it.
 * `swcraft.openGuide` is **not** a member: its child is a server not expected to exit
 * (`GuideSession`). The Resources view's *Refresh* is labelled `Resource Report`: it is
 * that report's run without a file, recorded under `swcraft.resourceReport`.
 */
export const SPAWNING_COMMAND_LABELS: Readonly<Record<string, string>> = {
  "swcraft.build": "Build",
  "swcraft.changeReport": "Change Report",
  "swcraft.checkModel": "Check Model",
  "swcraft.checkEditorTooling": "Check Editor Tooling",
  "swcraft.diagrams": "Diagrams",
  "swcraft.experimentAllocation": "Allocation (CARMA)",
  "swcraft.experimentTrace": "Trace Links",
  "swcraft.exportComponent": "Export Component",
  "swcraft.impact": "Impact",
  "swcraft.impactRefresh": "Impact",
  "swcraft.importComponent": "Import Component",
  "swcraft.importRequirements": "Import Requirements",
  "swcraft.installToolchain": "Install Toolchain",
  "swcraft.metrics": "Architecture Metrics",
  "swcraft.onboardProject": "Onboard Project",
  "swcraft.resourceReport": "Resource Report",
  "swcraft.resourcesRefresh": "Resource Report",
  "swcraft.testTables": "Test Tables",
  "swcraft.traceApply": "Apply Trace Decisions",
  "swcraft.traceChooseElement": "Choose Another Element",
  "swcraft.traceChooseMode": "Trace Link Search",
  "swcraft.traceNewSearch": "Trace Link Search",
  "swcraft.traceOtherElement": "Choose Another Element",
  "swcraft.updateToolchain": "Update Toolchain",
};

/**
 * The status-bar text of a run with nothing else to say, `Check Model — running…` (the
 * grammar of `BUILD_WAITING_TEXT` and `describeBuildProgress()`).
 */
export function describeRunning(commandId: string): string {
  return `${SPAWNING_COMMAND_LABELS[commandId] ?? commandId} — running…`;
}

/** A `Build`'s text before its first step starts: it may wait its turn behind a diagram generation. */
export const BUILD_WAITING_TEXT = "Build — waiting…";

/** A `Diagrams` press's text while its generation runs: indeterminate, no fraction. */
export const DIAGRAMS_PROGRESS_TEXT = "generating diagrams…";

/**
 * A `Build`'s text while *steps*[*index*] runs, `Build — 3/6 generate arxml`, reported as
 * each step starts, so it advances by step and never by an estimate of time.
 */
export function describeBuildProgress(steps: readonly BuildStep[], index: number): string {
  return `Build — ${index + 1}/${steps.length} ${steps[index].label}`;
}

/** Which of the two artifacts the panel marks a `Build` run generated (treeView.ts's `recordBuild()`). */
export type BuildRunGenerations = Record<Artifact, boolean>;

/**
 * Which artifacts *result*'s run generated successfully: what the panel records as each
 * artifact's last successful generation (treeView.ts's `decideArtifactState()`).
 *
 * The run's first step moves the ARXML aside and removes both roots, so nothing the panel
 * reads survives from before it. **The views**
 * count once `generate diagrams` exited 0, even if a later step failed. **The ARXML** counts
 * only when the whole run succeeded: a run that failed at `validate`, the last step, leaves
 * the file on disk with no success, which the panel reads *out of date*. The diagrams step
 * is found by its words, never by an index, because the generators' order is a choice.
 */
export function buildRunGenerations(result: BuildRunResult): BuildRunGenerations {
  if (result.kind === "success") {
    return { arxml: true, views: true };
  }
  const labels = BUILD_STEP_SUBCOMMANDS.map((subcommand) => subcommand.join(" "));
  const failedAt = labels.indexOf(result.step.label);
  const diagramsAt = labels.indexOf(GENERATE_DIAGRAMS_SUBCOMMAND.join(" "));
  return { arxml: false, views: failedAt > diagramsAt };
}

/**
 * The one warning a `Build` run shows, or `undefined` when it succeeded:
 * `outcomeMessage()`'s sentence with the failing step named, as in
 * `Onboard Project (swcraft init)`.
 */
export function buildRunMessage(result: BuildRunResult): string | undefined {
  if (result.kind === "success") {
    return undefined;
  }
  return outcomeMessage(`Build (${result.step.label})`, {
    kind: "failure",
    exitCode: result.run.exitCode,
  });
}

/** Where a finished `Build` run leaves the user ({@link buildRunEnding}); a report ending carries the stdout to read. */
export type BuildRunEnding =
  | { readonly kind: "problems" }
  | { readonly kind: "report"; readonly stdout: string }
  | { readonly kind: "none" };

/**
 * Where a finished `Build` run leaves the user, decided after its status is recorded and its
 * warning shown:
 *
 * 1. `"problems"` when the run failed at `check-model` (`feedsDiagnostics`): without
 *    `--warnings-as-errors` that step fails exactly on an error or a refused project, both
 *    placed in Problems. Read off the run's own *result*, never off the Problems collection,
 *    which an overlapping Check Model press may have filled. No report opens: none was
 *    written.
 * 2. `"report"` when the `validate` step ran, passed or failed; *validateStdout* is its
 *    stdout (`undefined` when it never started), which holds the `Generated report:` line.
 * 3. `"none"` otherwise: a failure at `clean-outputs` or a generator.
 *
 * Warnings alone never reach `"problems"`: the run succeeded.
 */
export function buildRunEnding(
  result: BuildRunResult,
  validateStdout: string | undefined,
): BuildRunEnding {
  if (result.kind === "failure" && result.step.feedsDiagnostics === true) {
    return { kind: "problems" };
  }
  if (validateStdout !== undefined) {
    return { kind: "report", stdout: validateStdout };
  }
  return { kind: "none" };
}

/**
 * Whether a finished `Build` run refreshes the Resources view: only a success, since a
 * report over a model the build refused would present figures as though nothing happened.
 */
export function buildRunRefreshesResources(result: BuildRunResult): boolean {
  return result.kind === "success";
}

// --- Export Component -----------------------------------------------------------

/**
 * Build the argv for `swcraft generate arxml --component <swcName>`. `--output` is omitted:
 * the CLI (`cli/build_commands.py`'s `cmd_generate`) computes
 * `<dist_root>/components/<swcName>.arxml` (`~N` for a case-only collision) from the
 * manifest. The name is trimmed so pasted whitespace never reads as "component not found".
 *
 * **The name is passed as `--component=<name>`, one token.** The free-text fallback (shown
 * when `swcraft list` could not answer) accepts a leading `-`, which argparse would read as
 * an option when split across two tokens (exit 2, a usage dump); a `--` cannot help an
 * *option value*. With `=` the CLI answers `Error: SWC '-foo' not found in …`.
 */
export function buildExportComponentArgv(manifestRelPath: string, swcName: string): string[] {
  return ["generate", "arxml", `--component=${swcName.trim()}`, "--project", manifestRelPath];
}

// --- The imports: a dry run, its report, then one question ----------------
//
// The extension has no import logic of its own: it runs the CLI twice, always a dry run
// first, then, only on the person's yes, the same argv without `--dry-run`. Both imports
// behave alike: the dry run's Markdown report opens in the import tab (`importTabCore.ts`)
// with the apply button and Cancel under it, kept in no file of the project; the applied
// run's report replaces it in the same tab. Below are the vscode-free parts of those flows.

/**
 * `swcraft import requirements --source=<file> [--mapping=<file>] [--baseline=<text>]
 * --project <manifest> [--dry-run] [--report=<file.md>]`. The applied run's argv is the dry
 * run's minus `--dry-run` (with its own report file), so the person applies exactly what
 * the preview ran.
 *
 * Every value is joined to its option with `=` (the `buildExportComponentArgv()`
 * hardening). The source and mapping must be absolute, so the CLI never resolves them
 * against another working directory; a relative path throws. *mapping* absent means
 * SWCraft's own names. *baseline* absent or empty means no `--baseline`
 * ({@link baselineArgFor}): the CLI takes the file's own version or refuses. *report* is
 * the Markdown report file the CLI writes ({@link importReportOption}).
 */
export function buildImportRequirementsArgv(
  manifestRelPath: string,
  file: string,
  options: { dryRun: boolean; report?: string; mapping?: string; baseline?: string },
): string[] {
  if (!path.isAbsolute(file)) {
    throw new Error(
      `buildImportRequirementsArgv: the source file must be an absolute path, got '${file}'`,
    );
  }
  if (options.mapping !== undefined && !path.isAbsolute(options.mapping)) {
    throw new Error(
      `buildImportRequirementsArgv: the mapping file must be an absolute path, got '${options.mapping}'`,
    );
  }
  const argv = ["import", "requirements", `--source=${file}`];
  if (options.mapping !== undefined) {
    argv.push(`--mapping=${options.mapping}`);
  }
  if (options.baseline !== undefined && options.baseline !== "") {
    argv.push(`--baseline=${options.baseline}`);
  }
  argv.push("--project", manifestRelPath);
  if (options.dryRun) {
    argv.push("--dry-run");
  }
  if (options.report !== undefined) {
    argv.push(importReportOption(options.report));
  }
  return argv;
}

// --- The requirements import's mapping and the delivery's version -------------
//
// The mapping is remembered in the resource-scoped setting below, written at the pick
// (before the run, so a run refused for another reason does not forget a good mapping) and
// cleared when the person picks SWCraft's own names. The extension does not read the
// mapping, and of the needs file only `current_version` ({@link fileDeliveryVersion});
// everything else is the CLI's to refuse.

/** The setting that remembers the requirements import's mapping file. */
export const REQUIREMENTS_MAPPING_SETTING = "swcraft.requirementsImport.mapping";

/** What an item of the mapping pick stands for. */
export type MappingChoice = SettingPathChoice;

/** One item of the mapping pick: a `QuickPickItem`'s fields and the choice it stands for. */
export interface MappingPickItem {
  label: string;
  description?: string;
  choice: MappingChoice;
}

/** The mapping pick's title and placeholder. */
export const MAPPING_PICK_TEXT = {
  title: "SWCraft: how the requirements team's needs map to SWCraft's",
  placeHolder: "The mapping file of this import",
} as const;

/** The title of the open dialog behind *Choose another mapping file…*. */
export const MAPPING_DIALOG_TITLE = "SWCraft: select the mapping file";

/** The delivery's version box: its title, its two prompts, its placeholder and its refusal of an empty value. */
export const BASELINE_PROMPT_TEXT = {
  title: "SWCraft: the delivery's version",
  /** The box prefilled with the file's own version. */
  prompt:
    "The delivery's version, as the file names it. Press Enter to record it, or give another: the requirements team's baseline or commit.",
  /** The box empty, because the file names no version. */
  promptRequired:
    "The file names no version. Enter the delivery's version: the requirements team's baseline or commit of this delivery.",
  placeHolder: "for example v2.3 or a commit id",
  required: "Enter the delivery's version: the requirements team's baseline or commit.",
} as const;

/**
 * The file's own version, which the version box is prefilled with: the top-level
 * `current_version` of *text* when it is a non-blank string, else `undefined`. A leading
 * byte order mark is skipped, as the CLI's reader skips it. Only this key is read, so the
 * dry run can still refuse the file.
 */
export function fileDeliveryVersion(text: string): string | undefined {
  let document: unknown;
  try {
    document = JSON.parse(text.replace(/^\uFEFF/, ""));
  } catch {
    return undefined;
  }
  if (typeof document !== "object" || document === null || Array.isArray(document)) {
    return undefined;
  }
  const value = (document as Record<string, unknown>).current_version;
  return typeof value === "string" && value.trim() !== "" ? value : undefined;
}

/**
 * The version box's options for a file whose own version is *fileVersion*: prefilled with
 * it, or empty with the prompt that says what to enter. A blank value is refused in the box,
 * as the CLI refuses it.
 */
export function baselineBoxOptions(fileVersion: string | undefined): {
  title: string;
  prompt: string;
  placeHolder: string;
  value: string;
  validateInput: (value: string) => string | undefined;
} {
  return {
    title: BASELINE_PROMPT_TEXT.title,
    prompt:
      fileVersion === undefined ? BASELINE_PROMPT_TEXT.promptRequired : BASELINE_PROMPT_TEXT.prompt,
    placeHolder: BASELINE_PROMPT_TEXT.placeHolder,
    value: fileVersion ?? "",
    validateInput: (value) => (value.trim() === "" ? BASELINE_PROMPT_TEXT.required : undefined),
  };
}

/**
 * The `--baseline` the runs carry for the box's answer *typed*: none when it is the file's
 * own version unchanged (the CLI then takes the file's and reports *(from the file)*), else
 * the value as typed.
 */
export function baselineArgFor(typed: string, fileVersion: string | undefined): string | undefined {
  return typed === fileVersion ? undefined : typed;
}

/**
 * A path setting's *stored* value, trimmed; `undefined` for an empty, blank or non-string
 * value. Shared, with the two helpers below, by the requirements import's mapping file and
 * the test tables' annotations folder.
 */
export function rememberedSettingPath(stored: unknown): string | undefined {
  const value = typeof stored === "string" ? stored.trim() : "";
  return value === "" ? undefined : value;
}

/**
 * The mapping pick's items: the remembered mapping, marked *last used*, when the setting
 * names one, then *Choose another mapping file…* and *SWCraft's own names (no mapping)*.
 */
export function mappingPickItems(remembered: unknown): MappingPickItem[] {
  const stored = rememberedSettingPath(remembered);
  return [
    ...(stored === undefined
      ? []
      : [{ label: `$(file) ${stored}`, description: "last used", choice: "remembered" as const }]),
    { label: "Choose another mapping file…", choice: "choose" },
    { label: "SWCraft's own names (no mapping)", choice: "none" },
  ];
}

/**
 * How a path setting stores a picked absolute *file* or folder: relative to *projectDir*, in
 * `/` form, when inside it; absolute when outside, never climbing out with `..`, which
 * would name another file once the project moves. A relative *file* throws.
 */
export function storedSettingPath(file: string, projectDir: string): string {
  if (!path.isAbsolute(file)) {
    throw new Error(`storedSettingPath: the picked path must be absolute, got '${file}'`);
  }
  const absolute = path.resolve(file);
  const relative = path.relative(path.resolve(projectDir), absolute);
  // Another drive on Windows gives an absolute `relative`.
  const inside =
    relative !== "" && !path.isAbsolute(relative) && relative.split(path.sep)[0] !== "..";
  return inside ? relative.split(path.sep).join("/") : absolute;
}

/** What an item of a path setting's pick stands for: the stored path, a new one, or none. */
export type SettingPathChoice = "remembered" | "choose" | "none";

/**
 * The pick behind a path setting ({@link askSettingPath}): its items, the two prompts (each
 * `undefined` for Esc), the setting's writer and what *none* is stored as.
 */
export interface SettingPathAsk<
  Item extends { choice: SettingPathChoice },
  None extends string | undefined,
> {
  /** The project's directory, which a path inside it is stored relative to. */
  projectDir: string;
  /** The setting's stored value, as read. */
  remembered: unknown;
  /** The pick's items, built from *remembered*. */
  items: Item[];
  pick(items: Item[]): Promise<Item | undefined>;
  /** The dialog behind *choose*: the picked path, absolute. */
  pickPath(): Promise<string | undefined>;
  remember(value: string | None): Promise<void>;
  /** What *none* is stored as: `undefined` clears the setting, `""` is an explicit answer. */
  noneValue: None;
}

/**
 * One pass of a path setting's pick: *remembered* resolves the stored value, *none* stores
 * {@link SettingPathAsk.noneValue} and answers no path, *choose* opens the dialog and stores
 * its path as {@link storedSettingPath} spells it. `{ path }` is the absolute path the argv
 * carries (`undefined` inside it for none); `undefined` itself is Esc. The setting is
 * written at the pick, before any run; the path is the CLI's to refuse.
 */
export async function askSettingPath<
  Item extends { choice: SettingPathChoice },
  None extends string | undefined,
>(ask: SettingPathAsk<Item, None>): Promise<{ path: string | undefined } | undefined> {
  const picked = await ask.pick(ask.items);
  if (picked === undefined) {
    return undefined;
  }
  if (picked.choice === "remembered") {
    return { path: settingPathArgFor(ask.remembered, ask.projectDir) };
  }
  if (picked.choice === "none") {
    await ask.remember(ask.noneValue);
    return { path: undefined };
  }
  const chosen = await ask.pickPath();
  if (chosen === undefined) {
    return undefined;
  }
  await ask.remember(storedSettingPath(chosen, ask.projectDir));
  return { path: chosen };
}

/**
 * The absolute path the argv carries (`--mapping=`, `--annotations=`) for a path setting's
 * *stored* value, resolved against *projectDir*; `undefined` for none.
 */
export function settingPathArgFor(stored: unknown, projectDir: string): string | undefined {
  const value = rememberedSettingPath(stored);
  return value === undefined ? undefined : path.resolve(projectDir, value);
}

// --- The import report's Markdown file ----------------------------------------
//
// Both imports pass `--report=<file>.md` and the CLI writes its report's Markdown form there
// (stdout keeps the text). The extension reads that file, renders it in the import tab and
// deletes it. The file lives in a one-run directory under the OS temporary directory,
// removed after the run whatever it came to: never in the project, never in VS Code's
// storage.

/** The name of the import report's Markdown file inside its one-run directory. */
export const IMPORT_REPORT_FILE_NAME = "import-report.md";

/**
 * The heading the CLI's Markdown component report opens with, up to the file's name
 * (`report.py`'s `render_markdown()`, its colon escaped by `markdown_cells.heading_text()`).
 * The import tab cuts it to the file's name (`importTabCore.tabReportMarkdown()`).
 */
export const COMPONENT_MARKDOWN_HEADING = "# Component import report\\: ";

/**
 * The heading Trace Apply's report opens with, up to the search's name
 * (`trace_apply.py`'s `render_report_markdown()`). The tab's title already says *Trace Apply
 * Report*, so the tab cuts the heading to `search '<name>'`.
 */
export const TRACE_APPLY_MARKDOWN_HEADING = "# Trace Apply Report — ";

/**
 * The first line's start of each Markdown report the CLI writes for the import tab: the
 * component report, `requirements_import.py`'s `render_report_markdown()` and Trace Apply's.
 * A report file opening with none of them is not taken as Markdown
 * ({@link withImportReportFile}): an older toolchain writes its TEXT report to any
 * `--report` path, `.md` included.
 */
export const IMPORT_MARKDOWN_HEADINGS: readonly string[] = [
  COMPONENT_MARKDOWN_HEADING,
  "# Requirements import\\: ",
  TRACE_APPLY_MARKDOWN_HEADING,
];

/** Whether *text* is a Markdown import report: it opens with one of {@link IMPORT_MARKDOWN_HEADINGS}. */
export function isImportMarkdown(text: string | undefined): text is string {
  return text !== undefined && IMPORT_MARKDOWN_HEADINGS.some((each) => text.startsWith(each));
}

/**
 * `--report=<file>`, one token, for an absolute `.md` path; anything else throws (the CLI
 * writes Markdown only for a `.md` path, and resolves a relative one against its own working
 * directory).
 */
export function importReportOption(file: string): string {
  if (!path.isAbsolute(file) || !file.toLowerCase().endsWith(".md")) {
    throw new Error(`the import report file must be an absolute .md path, got '${file}'`);
  }
  return `--report=${file}`;
}

/** The file operations the import report's helpers are handed: `node:fs` in the host and the tests. */
export type ImportReportFs = Pick<typeof nodeFs, "realpathSync" | "promises">;

/**
 * Whether *file* lies outside *folder*: neither the folder itself nor below it, compared as
 * given and with symbolic links resolved by *realpath* where they exist (macOS's `/var` is
 * `/private/var`).
 */
export function liesOutside(
  file: string,
  folder: string,
  realpath: (each: string) => string,
): boolean {
  const real = (each: string): string => {
    try {
      return realpath(each);
    } catch {
      return path.resolve(each);
    }
  };
  for (const [child, parent] of [
    [path.resolve(file), path.resolve(folder)],
    [real(file), real(folder)],
  ]) {
    // Outside is a first segment of exactly `..`: `..tmp` is a folder's child like any other.
    const relative = path.relative(parent, child);
    const escapes =
      relative === ".." || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative);
    if (!escapes) {
      return false;
    }
  }
  return true;
}

/**
 * Run one import with a fresh Markdown report file and hand back the run with the file's
 * text as `reportMarkdown`. The file is {@link IMPORT_REPORT_FILE_NAME} in a directory made
 * for this run alone (`mkdtemp` under *tmpRoot*, the OS temporary directory by default),
 * removed in a `finally` whatever the run came to.
 *
 * When no such directory can be made, or it would lie in the workspace folder, the run goes
 * ahead without `--report` and *log* says why. A run that wrote no file, or a file not
 * opening with one of {@link IMPORT_MARKDOWN_HEADINGS} (an older toolchain's text report),
 * has no `reportMarkdown`; its report is the fenced stdout. Every file operation goes
 * through *fs*.
 */
export async function withImportReportFile(
  workspaceRoot: string,
  run: (report: string | undefined) => Promise<CapturedRun>,
  fs: ImportReportFs,
  options: { tmpRoot?: string; log?: (line: string) => void } = {},
): Promise<CapturedRun> {
  const log = options.log ?? (() => {});
  let directory: string;
  try {
    directory = await fs.promises.mkdtemp(
      path.join(options.tmpRoot ?? os.tmpdir(), "swcraft-import-"),
    );
  } catch (error) {
    log(
      `SWCraft: no temporary directory for the import report (${errorText(error)}); the report is the CLI's printed text`,
    );
    return run(undefined);
  }
  try {
    if (!liesOutside(directory, workspaceRoot, fs.realpathSync)) {
      log(
        "SWCraft: the temporary directory lies in the workspace folder, so the import writes no report file there; the report is the CLI's printed text",
      );
      return await run(undefined);
    }
    const report = path.join(directory, IMPORT_REPORT_FILE_NAME);
    const result = await run(report);
    let markdown: string | undefined;
    try {
      markdown = await fs.promises.readFile(report, { encoding: "utf8" });
    } catch {
      markdown = undefined;
    }
    return isImportMarkdown(markdown) ? { ...result, reportMarkdown: markdown } : result;
  } finally {
    await fs.promises.rm(directory, { recursive: true, force: true }).catch((error: unknown) => {
      log(
        `SWCraft: the import report's temporary directory could not be removed (${errorText(error)})`,
      );
    });
  }
}

/** An error's message, or the value as text. */
function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/** The import tab's apply button: the only choice that runs the applied import (the trace Apply's word). */
export const IMPORT_APPLY_BUTTON = "Write";

/** The import tab's secondary button: a no that closes the tab and runs nothing. */
export const IMPORT_CANCEL_BUTTON = "Cancel";

/** The import tab's button after an applied run that exited 0: it runs `SWCraft: Check Model`, nothing else. */
export const IMPORT_CHECK_MODEL_BUTTON = "Check Model";

/** The title of a requirements import report, which is not saved anywhere; the next report reuses it. */
export const IMPORT_REPORT_TITLE = "Requirements Import Report";

/** The line beside **Check Model** after an applied import: its own count, or a plain sentence without one. */
export function importDoneMessage(file: string, count: string | undefined): string {
  const name = path.basename(file);
  return count === undefined ? `${name} was imported.` : `${name} was imported: ${count}.`;
}

/**
 * The CLI report's fixed summary line, the last header line of every
 * `swcraft import requirements` report. `$` under `/m` also matches before a `\r`, so CRLF
 * holds.
 */
const IMPORT_SUMMARY_LINE =
  /^Summary: (\d+ added, \d+ changed, \d+ removed, \d+ stubs created, \d+ source updates)$/gm;

/**
 * The count after `Summary: ` when exactly one line matches the whole grammar, verbatim;
 * `undefined` when none does (an older toolchain) and when several do (a second match is a
 * value the CLI failed to escape, and no choice would be safe). The count is displayed beside
 * **Check Model** and never judged.
 */
export function summaryOf(stdout: string): string | undefined {
  const matches = [...stdout.matchAll(IMPORT_SUMMARY_LINE)];
  return matches.length === 1 ? matches[0][1] : undefined;
}

/** The last line of a refused dry run: a dry run writes nothing, in every case. */
export const NOTHING_WRITTEN_LINE = "Nothing was written.";

/**
 * The last line of a failed applied run. It never says *Nothing was written*: a run stopped
 * during the write can leave files partly replaced, since the importer's restore is a Python
 * exception handler.
 */
export const IMPORT_INCOMPLETE_LINE =
  "The import did not complete. Check what it changed with Change Report or git status.";

/**
 * The last line of an applied run the CLI says was applied before its report failed, in
 * place of {@link IMPORT_INCOMPLETE_LINE}, which would contradict the CLI's line above it.
 */
export const IMPORT_REPORT_FAILED_LINE =
  "The import was applied, but its report could not be shown. Check what it changed with Change Report or git status.";

/**
 * The start of that CLI line on stderr (`cli/import_commands.py`'s
 * `Error: {_import_done(args)}…` for an applied run of either import). Read for the closing
 * line alone: it decides no button and no outcome.
 */
const IMPORT_APPLIED_ERROR = /^Error: the import was applied\b/m;

/**
 * The extension's own lines for one kind of applied run, added to its report: *stopped*, a
 * run stopped by a signal with nothing printed; *incomplete*, a failed run's last line;
 * *reportFailed*, chosen by *appliedError*, a run applied before its report failed. Trace
 * Apply, sharing the imports' dry-run sequence, has its own (`traceCore.ts`).
 */
export interface AppliedRunWording {
  stopped: string;
  incomplete: string;
  reportFailed: string;
  appliedError: RegExp;
}

/** The imports' wording: the lines above, unchanged. */
export const IMPORT_RUN_WORDING: AppliedRunWording = {
  stopped: "The import stopped without a word (stopped by a signal).",
  incomplete: IMPORT_INCOMPLETE_LINE,
  reportFailed: IMPORT_REPORT_FAILED_LINE,
  appliedError: IMPORT_APPLIED_ERROR,
};

/**
 * One CLI run, as `spawnCli()` answers it; an import's run also carries the
 * Markdown report file the CLI wrote, read by {@link withImportReportFile}.
 */
export interface CapturedRun extends ChildCapture {
  reportMarkdown?: string;
}

/**
 * What an import report shows: the CLI's text as printed (empty when nothing to read), the
 * extension's own lines, each a paragraph, and the CLI's warning lines when there are any
 * ({@link importWarningLines}). *markdown* is the CLI's `--report` file, present only for a
 * run that exited 0 and wrote it; the tab renders it in place of the fenced *cliText*,
 * which stays as the plain-text fallback.
 */
export interface ImportReport {
  cliText: string;
  notes: string[];
  warnings?: string[];
  markdown?: string;
}

/** *report* with *run*'s Markdown report file, when it wrote one. */
function withMarkdown(report: ImportReport, run: CapturedRun): ImportReport {
  return run.reportMarkdown === undefined ? report : { ...report, markdown: run.reportMarkdown };
}

/**
 * The lines of *outputs* that start with `Warning:` or `WARNING:`, in order: the CLI's own
 * warning lines (a model value the CLI prints is indented or follows a word of its own),
 * shown in a block above the verbatim report. CRLF line ends are trimmed.
 */
export function importWarningLines(...outputs: string[]): string[] {
  const lines: string[] = [];
  for (const output of outputs) {
    for (const line of output.split("\n")) {
      const trimmed = line.replace(/\r$/, "");
      if (/^(?:Warning|WARNING):/.test(trimmed)) {
        lines.push(trimmed);
      }
    }
  }
  return lines;
}

/** *report* with *run*'s warning lines, stdout's then stderr's; unchanged when it printed none. */
function withWarnings(report: ImportReport, run: CapturedRun): ImportReport {
  const warnings = importWarningLines(run.stdout, run.stderr);
  return warnings.length === 0 ? report : { ...report, warnings };
}

/**
 * *text* with every Markdown metacharacter backslash-escaped, so it renders as written: `&`
 * among them, so an entity-like run such as `&amp;` is not decoded.
 */
export function escapeMarkdown(text: string): string {
  return text.replace(/[\\`*_{}[\]()#+\-.!<>|~&]/g, "\\$&");
}

/**
 * The shortest backtick fence no backtick run of *text* can close: one longer than the
 * longest run, and never shorter than three.
 */
export function codeFence(text: string): string {
  let longest = 0;
  for (const run of text.match(/`+/g) ?? []) {
    longest = Math.max(longest, run.length);
  }
  return "`".repeat(Math.max(3, longest + 1));
}

/**
 * The extension's own sentence for a run that printed nothing to read (a CLI that could not
 * be started, or one stopped by a signal with an empty stderr); `undefined` otherwise.
 */
function silentRunSentence(
  run: CapturedRun,
  executable: string,
  wording: AppliedRunWording,
): string | undefined {
  if (run.spawnFailed) {
    return `The SWCraft CLI could not be started (${executable}).`;
  }
  if (run.exitCode === null && run.stderr.trim() === "") {
    return wording.stopped;
  }
  return undefined;
}

/** A refused dry run's report: the stderr the CLI printed, every line kept, then {@link NOTHING_WRITTEN_LINE}. */
export function importRefusalReport(stderr: string): ImportReport {
  return { cliText: stderr, notes: [NOTHING_WRITTEN_LINE] };
}

/** What a dry run came to, and its report. */
export type ImportDryRun =
  | { kind: "report"; report: ImportReport }
  | { kind: "refused"; report: ImportReport };

/**
 * A dry run's outcome and its report, for either import. Exit 0 is the report, stdout as
 * printed, with no line of the extension's (the tab's **Write** and **Cancel** stand under
 * it). Every other run is refused, with its stderr (or the extension's sentence when it
 * printed nothing), then {@link NOTHING_WRITTEN_LINE}.
 */
export function importDryRunOutcome(
  run: CapturedRun,
  executable: string,
  wording: AppliedRunWording,
): ImportDryRun {
  const silent = silentRunSentence(run, executable, wording);
  if (silent !== undefined) {
    return { kind: "refused", report: { cliText: "", notes: [silent, NOTHING_WRITTEN_LINE] } };
  }
  if (run.exitCode === 0) {
    return {
      kind: "report",
      report: withMarkdown(withWarnings({ cliText: run.stdout, notes: [] }, run), run),
    };
  }
  return { kind: "refused", report: withWarnings(importRefusalReport(run.stderr), run) };
}

/**
 * An applied run's outcome and its report, for either import: exit 0 is stdout alone; any
 * other run is its stderr (or the extension's sentence), then {@link IMPORT_INCOMPLETE_LINE},
 * never *Nothing was written*, or {@link IMPORT_REPORT_FAILED_LINE} when stderr says only
 * the report failed. A failed run offers no button.
 */
export function importAppliedOutcome(
  run: CapturedRun,
  executable: string,
  wording: AppliedRunWording,
): { kind: "imported" | "failed"; report: ImportReport } {
  const silent = silentRunSentence(run, executable, wording);
  if (silent !== undefined) {
    return { kind: "failed", report: { cliText: "", notes: [silent, wording.incomplete] } };
  }
  if (run.exitCode === 0) {
    return {
      kind: "imported",
      report: withMarkdown(withWarnings({ cliText: run.stdout, notes: [] }, run), run),
    };
  }
  return {
    kind: "failed",
    report: withWarnings(
      {
        cliText: run.stderr,
        notes: [wording.appliedError.test(run.stderr) ? wording.reportFailed : wording.incomplete],
      },
      run,
    ),
  };
}

// --- `swcraft import component`: the same flow, and Show Update -------------
//
// The panel passes `--update` only after the exit status the CLI marks as the one `--update`
// resolves ({@link UPDATE_RESOLVES_EXIT}), and never reads a refusal's text to decide.

/**
 * The exit status of the one refusal `--update` resolves: a run without `--update` whose
 * only conflict is the component's own divergence. Held equal to `cli/import_commands.py`'s
 * `UPDATE_RESOLVES_EXIT` by a source test.
 */
export const UPDATE_RESOLVES_EXIT = 3;

/** The import tab's button after an exit 3: it runs the `--dry-run --update` preview. */
export const SHOW_UPDATE_BUTTON = "Show Update";

/** The import tab's apply button after **Show Update**: the only choice that runs the applied update. */
export const IMPORT_UPDATE_BUTTON = "Update";

/** The title of a component import report; never saved, and the next report reuses it. */
export const COMPONENT_IMPORT_REPORT_TITLE = "Component Import Report";

/** The extension's line under the refusal `--update` resolves. */
export const COMPONENT_UPDATE_OFFER_LINE =
  "The project already has this component, and the file changes it. Show Update shows what an update would replace and what it keeps.";

/**
 * `swcraft import component --source=<file> --project <manifest> [--dry-run] [--update]
 * [--report=<file.md>]`. The applied run's argv is its dry run's minus `--dry-run`, so
 * `--update` reaches an applied run only after the update dry run. No argv carries
 * `--replace-access-lists`: that stays a terminal command. The file is one `=` token and
 * must be absolute; a relative path throws.
 */
export function buildImportComponentArgv(
  manifestRelPath: string,
  file: string,
  options: { dryRun: boolean; update: boolean; report?: string },
): string[] {
  if (!path.isAbsolute(file)) {
    throw new Error(
      `buildImportComponentArgv: the source file must be an absolute path, got '${file}'`,
    );
  }
  const argv = ["import", "component", `--source=${file}`, "--project", manifestRelPath];
  if (options.dryRun) {
    argv.push("--dry-run");
  }
  if (options.update) {
    argv.push("--update");
  }
  if (options.report !== undefined) {
    argv.push(importReportOption(options.report));
  }
  return argv;
}

/** What a component dry run came to, and its report. */
export type ComponentDryRun =
  | { kind: "report"; report: ImportReport }
  | { kind: "updateResolves"; report: ImportReport }
  | { kind: "refused"; report: ImportReport };

/**
 * A component dry run's outcome: {@link importDryRunOutcome}, plus
 * {@link UPDATE_RESOLVES_EXIT} on a run without `--update`, the refusal the update resolves
 * (its stderr, then {@link COMPONENT_UPDATE_OFFER_LINE}). An exit 3 with `--update` is
 * refused as any other.
 */
export function componentDryRunOutcome(
  run: CapturedRun,
  executable: string,
  update: boolean,
): ComponentDryRun {
  if (!run.spawnFailed && run.exitCode === UPDATE_RESOLVES_EXIT && !update) {
    return {
      kind: "updateResolves",
      report: withWarnings({ cliText: run.stderr, notes: [COMPONENT_UPDATE_OFFER_LINE] }, run),
    };
  }
  return importDryRunOutcome(run, executable, IMPORT_RUN_WORDING);
}

// --- `swcraft list`: the model listing the QuickPicks are built from --------
//
// The extension never reads the model itself; it asks the CLI (the VS Code UI boundary; see
// the note at the head of layerFile.ts). `swcraft list` is the read-only subcommand that
// answers.

/**
 * The `schemaVersion` of the `swcraft list --format json` document this extension reads
 * (`listing.py`'s `LISTING_SCHEMA_VERSION`). It guards an unhealthy install whose CLI and
 * extension differ: `parseListJson()` refuses such a document by name rather than
 * mis-reading a shape it does not know.
 */
export const LISTING_SCHEMA_VERSION = 1;

/** One layer's entry from a parsed `swcraft list --format json` document. */
export interface LayerListing {
  /** The layer name, as the model contract's registry spells it (`swc`, `interfaces`, …). */
  layer: string;
  /**
   * The elements' identities, in the CLI's order (the order `swcraft diff` renders). Empty
   * for a layer whose directory does not exist: "no SWCs" and "no such layer" are different
   * answers.
   */
  elements: string[];
}

/**
 * Build the argv for `swcraft list [LAYER] --project <manifest> --format json`. *layer*
 * omitted lists every layer; `"swc"` narrows it to the components, for Export Component.
 */
export function buildListArgv(manifestRelPath: string, layer?: string): string[] {
  const positional = layer === undefined ? [] : [layer];
  return ["list", ...positional, "--project", manifestRelPath, "--format", "json"];
}

/**
 * Build the argv Find All References runs:
 * `swcraft impact --project <manifest> --format json --depth 1 -- <name>`. `--depth 1`
 * because Shift+F12 asks who refers to the element directly (the default is the whole
 * transitive walk); `--` keeps a name starting with `-` a name.
 */
export function buildImpactJsonArgv(manifestRelPath: string, name: string): string[] {
  return ["impact", "--project", manifestRelPath, "--format", "json", "--depth", "1", "--", name];
}

/**
 * Build the argv the Impact view runs: `swcraft impact --project <manifest> --format json
 * -- <name>`, with **no `--depth`**, the whole transitive walk, because the view asks what a
 * change to the element touches.
 */
export function buildImpactTreeArgv(manifestRelPath: string, name: string): string[] {
  return ["impact", "--project", manifestRelPath, "--format", "json", "--", name];
}

// --- The two commands a CodeLens runs with an argument -----------------------------
//
// `swcraft.impact`'s argument is decoded by impactCore's `impactQuestion`.

/**
 * A view id as the generator spells one (`writer/diagrams/__init__.py`'s `VIEW_ID_RE`,
 * duplicated): ASCII letters, digits, `_` and `-`, with an optional `~<n>` suffix, so no
 * separator, no `..` and no drive can pass.
 */
export const VIEW_ID_PATTERN = /^[A-Za-z0-9_-]+(?:~[1-9][0-9]*)?$/;

/**
 * `swcraft.diagrams`' optional argument: the id of `{view: "<id>"}` when it has
 * {@link VIEW_ID_PATTERN}'s shape **and** `<id>.html` is listed in *viewsListing* (the
 * `views_root` entries, `undefined` when unlistable); anything else (no argument, the
 * panel's `TreeItem`, `../x`, an absolute path, an id with no page) is `undefined`, and the
 * press opens the index.
 *
 * With `options.regenerate` (the tab's **Generate**) the press generates first, so the id
 * needs its shape alone: a page removed under the tab is shown again if the generation
 * wrote it, else the index. Every read still passes `resolveViewsFile()`.
 */
export function diagramsArgument(
  arg: unknown,
  viewsListing: readonly string[] | undefined,
  options: { regenerate?: boolean } = {},
): string | undefined {
  if (typeof arg !== "object" || arg === null || !("view" in arg)) {
    return undefined;
  }
  const view = (arg as { view: unknown }).view;
  if (typeof view !== "string" || !VIEW_ID_PATTERN.test(view)) {
    return undefined;
  }
  return options.regenerate === true || isListedView(viewsListing, view) ? view : undefined;
}

/**
 * Parse a `swcraft list --format json` document into one entry per layer, in the document's
 * key order (the contract's `displayOrder`).
 *
 * Throws, with a user-facing message, on invalid JSON, a `schemaVersion` other than
 * `LISTING_SCHEMA_VERSION` (named in the message), a missing or non-object `layers`, or a
 * layer whose `elements` is not an array of strings. Every caller falls back to the
 * free-text prompt on a throw, never a silent empty list: an empty list is a *valid* answer
 * ("this project has no SWCs").
 */
export function parseListJson(stdout: string): LayerListing[] {
  let document: unknown;
  try {
    document = JSON.parse(stdout);
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error);
    throw new Error(`could not parse the output of \`swcraft list\` as JSON: ${detail}`);
  }
  if (typeof document !== "object" || document === null || Array.isArray(document)) {
    throw new Error("the output of `swcraft list` is not a JSON object.");
  }
  const record = document as Record<string, unknown>;
  const version = record.schemaVersion;
  if (version !== LISTING_SCHEMA_VERSION) {
    throw new Error(
      `unsupported \`swcraft list\` schemaVersion ${JSON.stringify(version) ?? "undefined"} ` +
        `(this extension reads ${LISTING_SCHEMA_VERSION}). The SWCraft CLI in this project's ` +
        "environment and the installed extension are different versions.",
    );
  }
  const layers = record.layers;
  if (typeof layers !== "object" || layers === null || Array.isArray(layers)) {
    throw new Error("the output of `swcraft list` has no `layers` object.");
  }
  return Object.entries(layers as Record<string, unknown>).map(([layer, entry]) => {
    const elements =
      typeof entry === "object" && entry !== null
        ? (entry as Record<string, unknown>).elements
        : undefined;
    if (!Array.isArray(elements) || elements.some((name) => typeof name !== "string")) {
      throw new Error(
        `the \`swcraft list\` entry for layer '${layer}' has no string element list.`,
      );
    }
    return { layer, elements: elements as string[] };
  });
}

/**
 * The elements of *layer* in a parsed listing, or `undefined` when the document does not
 * carry that layer. An empty list is an *answer* ("no SWCs yet"); a missing key is not, and
 * the caller falls back to the free-text prompt.
 */
export function elementsInLayer(listings: LayerListing[], layer: string): string[] | undefined {
  return listings.find((listing) => listing.layer === layer)?.elements;
}

/**
 * `validateInput` for the Export Component SWC-name prompt: an empty or whitespace-only name
 * gets an inline error; anything else is accepted, since an unknown name is the CLI's to
 * reject (the extension never reads the model).
 *
 * This prompt is the **fallback**, raised only when `swcraft list swc` fails, where the
 * extension knows least about the model.
 */
export function validateSwcNameInput(value: string): string | undefined {
  return value.trim() === "" ? "Enter a SWC name (the component to export)." : undefined;
}

// --- Which prompt to raise, given what `swcraft list` answered ---------------
//
// Export Component's three-way branch, kept out of `commands.ts` so it is testable: inverted
// there, a project with no SWCs would get an empty dead-end picker and nothing would fail.
// `commands.ts` keeps only the `vscode.window.*` calls.

/** One QuickPick item: the element's identity, with its layer as the description. */
export interface NamePromptItem {
  label: string;
  /** The layer the element came from, shown and matched on. Absent for a single-layer prompt. */
  description?: string;
}

/**
 * What to raise for an element-name prompt, given what the CLI could answer (three outcomes,
 * because the *listing* has three answers):
 *
 * - `pick`: the CLI named elements, so the user chooses from them;
 * - `inform`: the CLI answered that there is nothing to choose; an empty QuickPick or a
 *   text box whose every answer the CLI will reject is a dead end, so this says so and
 *   cancels;
 * - `input`: the CLI could not answer (`undefined`), so the free-text prompt stands in. An
 *   empty list and a failed listing never take the same branch.
 *
 * `note` is a line for the output channel only (`listModel()` logs its own failures).
 * `severity` picks an `inform`'s notification: `info` by default (no SWCs is a young
 * project, not a fault); an environment failure asks for `warning`
 * (`decideCliUnavailablePrompt()`).
 */
export type NamePromptDecision =
  | { kind: "pick"; items: NamePromptItem[] }
  | { kind: "inform"; message: string; note?: string; severity?: "info" | "warning" }
  | { kind: "input"; note?: string };

/**
 * The prompt decision when the CLI could not be **started at all**, as opposed to running
 * and failing to answer.
 *
 * A failed listing still leaves a CLI that runs, so the free-text box is a real route
 * (`decideExportComponentPrompt(undefined)`). A spawn failure means the command collecting
 * the name cannot succeed either, so this says what happened, names the executable that does
 * not run, points at the diagnosis, and cancels.
 */
export function decideCliUnavailablePrompt(executable: string): NamePromptDecision {
  return {
    kind: "inform",
    severity: "warning",
    message:
      `SWCraft: the SWCraft CLI could not be started (${executable}), so the project's elements ` +
      "could not be listed and the command cannot run. Run `SWCraft: Check Editor Tooling` for " +
      "the diagnosis, or `SWCraft: Install Toolchain` to install the toolchain from the " +
      "workstation archive.",
  };
}

/** The layer Export Component's picker is built from — `cli/read_commands.py`'s `list` positional spells it. */
export const SWC_LAYER = "swc";

/**
 * Export Component's prompt decision, from a parsed `swcraft list swc` document (or
 * `undefined` when there is none). A document with no `swc` key is *not* "no SWCs"
 * (`elementsInLayer()`), so it falls back to free text and says why in the note.
 */
export function decideExportComponentPrompt(
  listings: LayerListing[] | undefined,
): NamePromptDecision {
  if (listings === undefined) {
    return { kind: "input" };
  }
  const names = elementsInLayer(listings, SWC_LAYER);
  if (names === undefined) {
    return {
      kind: "input",
      note: "SWCraft: `swcraft list swc` reported no `swc` layer; falling back to typing the name.",
    };
  }
  if (names.length === 0) {
    return {
      kind: "inform",
      message:
        "SWCraft: this project has no SWCs to export. Add one under the model's `swc/` layer first.",
    };
  }
  return { kind: "pick", items: names.map((name) => ({ label: name })) };
}

/** The QuickPick / InputBox wording for one name prompt, kept out of the adapter so it is readable in one place. */
export interface NamePromptText {
  title: string;
  pickPlaceHolder: string;
  inputPrompt: string;
  inputPlaceHolder: string;
}

export const EXPORT_COMPONENT_PROMPT_TEXT: NamePromptText = {
  title: "SWCraft: Export Component",
  pickPlaceHolder: "Select the SWC to export as a component-scoped ARXML file",
  inputPrompt: "Name of the SWC to export as a component-scoped ARXML file",
  inputPlaceHolder: "e.g. SpeedSensor",
};

// --- Onboarding: preflight check + `swcraft init` argv ----------------------
//
// Onboarding targets a folder the user picks, possibly with no `swcraft.yaml` yet, so it
// cannot use `buildCliArgv()`'s `--project <manifest>` shape; it needs `swcraft init <path>`
// plus a cheap preflight probe, since a `.vsix`-only install has no guarantee the resolved
// `swcraft` executable is there at all.

/**
 * Build the argv for `swcraft init <targetDir>`. `cmd_init` skips files that already exist,
 * so a target folder with unrelated content is safe.
 */
export function buildInitArgv(targetDir: string): string[] {
  return ["init", targetDir];
}

/**
 * Build the argv for the onboarding preflight probe, `swcraft --help`: argparse's `--help`
 * exits 0 without a subcommand, so it confirms the resolved executable runs before any file
 * is written.
 */
export function buildPreflightArgv(): string[] {
  return ["--help"];
}

export type PreflightOutcome = { kind: "ok" } | { kind: "cli-not-found" };

/**
 * Interpret the preflight probe's spawn result. *spawnFailed* is true when the child could
 * not be launched (no executable; Node's `spawn` `"error"` event); otherwise *exitCode* is
 * its exit code, `0` on a successful `--help`. A non-zero exit means the launcher started
 * and its venv could not (a `pyvenv.cfg` naming a removed base interpreter,
 * `checkToolchain()`'s second finding).
 *
 * Both are the same outcome, "there is no working SWCraft to run", and
 * `preflightNotFoundMessage` names the one repair.
 */
export function mapPreflightResult(
  exitCode: number | null,
  spawnFailed: boolean,
): PreflightOutcome {
  if (spawnFailed || exitCode !== 0) {
    return { kind: "cli-not-found" };
  }
  return { kind: "ok" };
}

/**
 * User-facing, actionable message for a failed preflight probe, distinct from
 * `outcomeMessage()`'s "failed (exit code N)" because the fix is an installation, not the
 * model. It names the workstation installer, not a `pip install`: a project installs
 * nothing the extension would then spawn.
 */
export function preflightNotFoundMessage(executable: string): string {
  return (
    `SWCraft: SWCraft CLI not found (tried "${executable} --help"). ` +
    "Install the toolchain from the workstation archive — run `SWCraft: Install Toolchain`, " +
    "or point the `swcraft.executable` setting at a swcraft executable if you are working " +
    "from a repo checkout. Onboarding was not performed — no files were written."
  );
}

export interface OnboardingSteps {
  /** Always run first: the "does the resolved executable run at all" probe. */
  preflightArgv: string[];
  /**
   * The `swcraft init <targetDir>` argv to run before applying the onboarding plan, or
   * `undefined` when *hasExistingManifest* is true. The single source of the "run swcraft
   * init?" decision; `onboarding.ts`'s `planOnboarding()` does not duplicate it.
   */
  initArgv: string[] | undefined;
}

/**
 * Decide the CLI-spawn steps for `SWCraft: Onboard Project` against *targetDir*: always
 * preflight, then `swcraft init` unless *hasExistingManifest*. `commands.ts` runs them, then
 * `onboarding.ts`'s `planOnboarding()`/`applyOnboardingPlan()` for the agent files.
 */
export function planOnboardingSteps(
  targetDir: string,
  hasExistingManifest: boolean,
): OnboardingSteps {
  return {
    preflightArgv: buildPreflightArgv(),
    initArgv: hasExistingManifest ? undefined : buildInitArgv(targetDir),
  };
}

/**
 * User-facing message for `onboarding.ts`'s `applyOnboardingPlan()` throwing (e.g. a broken
 * `dist/onboarding-assets/`): a synchronous throw with no exit code, so not
 * `outcomeMessage()`'s wording.
 */
export function onboardingApplyFailedMessage(error: unknown): string {
  const detail = error instanceof Error ? error.message : String(error);
  return (
    `SWCraft: onboarding failed while writing project files (${detail}). ` +
    "See the SWCraft output channel for details."
  );
}

/**
 * User-facing message for *Onboard Project* when the bundled client table
 * (`agentClients.ts`'s `loadClientTable()`) cannot be read or is malformed: the command
 * stops before asking which clients, and nothing has been written.
 */
export function clientTableUnreadableMessage(error: unknown): string {
  const detail = error instanceof Error ? error.message : String(error);
  return (
    `SWCraft: Onboard Project stopped: the bundled client table could not be read (${detail}), ` +
    "so nothing was written. Reinstall the extension."
  );
}

// --- Exit-code -> outcome mapping -------------------------------------------

export type CommandOutcome = { kind: "success" } | { kind: "failure"; exitCode: number | null };

export function mapExitCodeToOutcome(exitCode: number | null): CommandOutcome {
  return exitCode === 0 ? { kind: "success" } : { kind: "failure", exitCode };
}

/** Where a failed run's details are, by default: the shared output channel. */
export const OUTPUT_CHANNEL_DETAILS = "the SWCraft output channel";

/**
 * Where Check Model's failure points: the Problems panel, which holds the findings (a
 * refused run is an entry there too); it reveals no output channel.
 */
export const PROBLEMS_PANEL_DETAILS = "the Problems panel";

/** Where a failure warning sends the user: one of the two places above. */
export type DetailsAt = typeof OUTPUT_CHANNEL_DETAILS | typeof PROBLEMS_PANEL_DETAILS;

/**
 * User-facing warning for a non-zero or unknown exit code, or `undefined` on success.
 * *detailsAt* is where the user is sent for details.
 */
export function outcomeMessage(
  commandLabel: string,
  outcome: CommandOutcome,
  detailsAt: DetailsAt = OUTPUT_CHANNEL_DETAILS,
): string | undefined {
  if (outcome.kind === "success") {
    return undefined;
  }
  const codeText = outcome.exitCode === null ? "unknown" : String(outcome.exitCode);
  return `SWCraft: ${commandLabel} failed (exit code ${codeText}). See ${detailsAt} for details.`;
}

// --- Capturing a child's output ---------------------------------------------

/** What `captureChild()` resolves to: `commands.ts`'s `spawnCliCapture()` result. */
export interface ChildCapture {
  exitCode: number | null;
  spawnFailed: boolean;
  stdout: string;
  stderr: string;
}

/**
 * Collect *child*'s stdout and stderr and settle **exactly once**: on `close`, on `error`
 * (`spawnFailed`), when *timeoutMs* elapses, or when *signal* aborts, whichever comes
 * first. The timeout and the abort kill the child and settle `exitCode: null` (it *was*
 * started, so never `spawnFailed`); the timeout appends its "did not answer within" line to
 * stderr, the abort nothing. Settling clears the timer and the abort listener, so a reused
 * signal keeps none. *executable* only names the child in the timeout line.
 *
 * The abort check comes after the child's listeners, so a signal aborted before the call
 * still finds an `error` listener on a child that failed to start.
 */
export function captureChild(
  child: ChildProcess,
  executable: string,
  timeoutMs?: number,
  signal?: AbortSignal,
): Promise<ChildCapture> {
  return new Promise<ChildCapture>((resolve) => {
    let stdout = "";
    let stderr = "";
    let settled = false;
    let timer: NodeJS.Timeout | undefined;
    const onAbort = (): void => {
      child.kill();
      finish({ exitCode: null, spawnFailed: false, stdout, stderr });
    };
    const finish = (result: ChildCapture): void => {
      if (settled) {
        return;
      }
      settled = true;
      if (timer !== undefined) {
        clearTimeout(timer);
      }
      signal?.removeEventListener("abort", onAbort);
      resolve(result);
    };
    if (timeoutMs !== undefined) {
      timer = setTimeout(() => {
        // "Ran and did not answer" (`exitCode: null`), not `spawnFailed`: the two are different
        // findings to `checkToolchain()`.
        child.kill();
        finish({
          exitCode: null,
          spawnFailed: false,
          stdout,
          stderr: `${stderr}SWCraft: ${executable} did not answer within ${timeoutMs} ms and was stopped.\n`,
        });
      }, timeoutMs);
    }
    child.stdout?.on("data", (chunk: Buffer) => {
      stdout += chunk.toString();
    });
    child.stderr?.on("data", (chunk: Buffer) => {
      stderr += chunk.toString();
    });
    child.on("error", (err) => {
      finish({ exitCode: null, spawnFailed: true, stdout, stderr: `${stderr}${err.message}\n` });
    });
    child.on("close", (code) => finish({ exitCode: code, spawnFailed: false, stdout, stderr }));
    if (signal?.aborted) {
      onAbort();
    } else {
      signal?.addEventListener("abort", onAbort);
    }
  });
}

// --- Experiments (the panel's *Experiments* section) --------------------------

/**
 * The kinds of experiment the extension runs, one per `swcraft experiment <kind>`: the
 * allocation experiment (CARMA) and the trace link search. `commandCore.test.ts` holds this
 * table's keys equal to `cli/experiment_command.py`'s `kind` choices.
 */
export type ExperimentKind = "allocation" | "trace";

/** What the extension knows about one experiment kind. */
export interface ExperimentKindEntry {
  /**
   * The command id, dotless: `toolchainInstall.test.ts`'s handler scan reads
   * `swcraft\.[A-Za-z0-9]+`, so a dot inside the id would read as no command.
   */
  commandId: string;
  /** The panel item's label, and the name the run indicator shows. */
  label: string;
  /** The per-kind setting naming the configuration file, relative to the project directory. */
  setting: string;
}

/**
 * One entry per kind: the command, the panel label and the setting. The one place a kind is
 * written; the other spellings of the id are held to this table by the tests.
 */
export const EXPERIMENT_KINDS: Readonly<Record<ExperimentKind, ExperimentKindEntry>> = {
  allocation: {
    commandId: "swcraft.experimentAllocation",
    label: "Allocation (CARMA)",
    setting: "swcraft.experiments.allocation.config",
  },
  trace: {
    commandId: "swcraft.experimentTrace",
    label: "Trace Links",
    setting: "swcraft.experiments.trace.config",
  },
};

/**
 * The argv of `swcraft experiment <kind> --project <manifest> [--config=<file>]`.
 *
 * *config* is trimmed; empty means no configuration file (the kind's defaults). It is one
 * `=` token so a value starting with `-` is not read by argparse as an option.
 */
export function buildExperimentArgv(
  kind: ExperimentKind,
  manifestRelPath: string,
  config?: string,
): string[] {
  const file = config?.trim() ?? "";
  return [
    "experiment",
    kind,
    "--project",
    manifestRelPath,
    ...(file === "" ? [] : [`--config=${file}`]),
  ];
}

const REPORT_LINE_PREFIX = "Generated report: ";

/**
 * The value of the **last** line of *output* starting with *prefix*: the text after it,
 * trimmed (a trailing `\r` included); lines with an empty value are ignored. `undefined`
 * when no line carries a value.
 */
export function lastPrefixedLine(output: string, prefix: string): string | undefined {
  let found: string | undefined;
  for (const raw of output.split("\n")) {
    const line = raw.endsWith("\r") ? raw.slice(0, -1) : raw;
    if (line.startsWith(prefix)) {
      const value = line.slice(prefix.length).trim();
      if (value !== "") {
        found = value;
      }
    }
  }
  return found;
}

/**
 * Does *value* hold a C0 control character, DEL or a C1 control character? A code-point
 * test, because the linter refuses control characters in a regular expression.
 */
function holdsControlCharacter(value: string): boolean {
  for (let index = 0; index < value.length; index += 1) {
    const code = value.charCodeAt(index);
    if (code <= 0x1f || (code >= 0x7f && code <= 0x9f)) {
      return true;
    }
  }
  return false;
}

/**
 * The report a successful `swcraft experiment`, `change-report`, `validate --report`,
 * `metrics --report` or `resource-report --report` run wrote, read off the stream holding
 * the line (stdout, except `resource-report --format json`, whose line is on stderr): the
 * text after `Generated report: ` on the **last** such line (a transcript of two runs names
 * the later), resolved against *cwd*.
 *
 * `undefined` when no line names a report, or when the value holds a control character: the
 * CLI never prints such a path (`print_generated_report()` in `cli/report_files.py` warns
 * instead). What the parse cannot see is a newline inside the path, which splits it; the
 * CLI's refusal closes that, and `reportInsideWorkspace()` bounds what any line can name.
 */
export function experimentReportPath(output: string, cwd: string): string | undefined {
  const found = lastPrefixedLine(output, REPORT_LINE_PREFIX);
  if (found === undefined || holdsControlCharacter(found)) {
    return undefined;
  }
  return path.resolve(cwd, found);
}

/**
 * *report* when it lies inside *workspaceRoot*, both resolved through *realpath*, else
 * `undefined` (also when either cannot be resolved). Every report the CLI writes is inside
 * the project, and the project is inside the workspace folder, so a line naming a file
 * anywhere else did not come from the run and nothing is previewed.
 */
export function reportInsideWorkspace(
  report: string,
  workspaceRoot: string,
  realpath: (candidate: string) => string,
): string | undefined {
  let real: string;
  let root: string;
  try {
    real = realpath(report);
    root = realpath(workspaceRoot);
  } catch {
    return undefined;
  }
  const relative = path.relative(root, real);
  // The first segment is compared whole: `..dist` is a folder inside the workspace.
  const first = relative.split(/[\\/]/)[0];
  if (relative === "" || first === ".." || path.isAbsolute(relative)) {
    return undefined;
  }
  return real;
}

/**
 * The line `swcraft generate arxml --component <name>` prints for the file it wrote:
 * `OUTPUT <path>` (`ComponentExportReport.render()` in
 * `packages/tools/src/swcraft_tools/component_exchange/report.py`), pinned by
 * `packages/tools/tests/test_component_export.py`'s `TestExportReportRenderer`.
 */
export const EXPORT_OUTPUT_LINE_PREFIX = "OUTPUT ";

/**
 * The ARXML a successful component export wrote: the last `OUTPUT ` line's value, resolved
 * against *cwd*; `undefined` when no line names a file.
 */
export function exportedArxmlPath(stdout: string, cwd: string): string | undefined {
  const found = lastPrefixedLine(stdout, EXPORT_OUTPUT_LINE_PREFIX);
  return found === undefined ? undefined : path.resolve(cwd, found);
}

/**
 * A child's output kept as raw chunks, decoded once, so a multi-byte UTF-8 character split
 * across two chunks is decoded whole. `spawnCli()` keeps stdout this way because the
 * report path, which may hold any character, is read from it.
 */
export function decodeChunks(chunks: Buffer[]): string {
  return Buffer.concat(chunks).toString("utf8");
}

/**
 * The refusal for an experiment's configuration setting holding something other than a
 * path (a `settings.json` can hold any value, and VS Code hands it over unchecked).
 * `undefined` and `null` are no configuration, a string is the path, anything else is
 * refused before a run.
 */
export function experimentSettingRefusal(setting: string, value: unknown): string | undefined {
  if (value === undefined || value === null || typeof value === "string") {
    return undefined;
  }
  return `SWCraft: the setting ${setting} must be the path of a configuration file, so the experiment did not run`;
}

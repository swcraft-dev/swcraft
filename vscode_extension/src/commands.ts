// VS Code adapter: wires the SWCraft commands to commandCore.ts's pure logic, plus the
// Activity Bar views that surface them beyond the Command Palette.
//
// One of only two files (with extension.ts) allowed to `import "vscode"`. All decisions
// (argv construction, exit-code interpretation, manifest resolution, MCP server definitions,
// tree item descriptors) are delegated to the plain modules.

import { spawn } from "node:child_process";
import { randomBytes } from "node:crypto";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { StringDecoder } from "node:string_decoder";
import * as vscode from "vscode";
import {
  CLIENT_PICK_TITLE,
  type ClientTable,
  chosenClients,
  clientFilesFor,
  clientInUse,
  clientLabelsFor,
  clientNamed,
  clientPickItems,
  companionApplies,
  conflictWarnings,
  loadClientTable,
  NO_CLIENT_CHOSEN_MESSAGE,
} from "./agentClients";
import {
  checkClientSwcraft,
  clientSwcraftTrigger,
  clientSwcraftWarning,
  presentClientServerFiles,
} from "./clientSwcraft";
import {
  BUILD_WAITING_TEXT,
  buildBuildSteps,
  buildCliArgv,
  buildExperimentArgv,
  buildExportComponentArgv,
  buildGenerateDiagramsArgv,
  buildImpactJsonArgv,
  buildImpactTreeArgv,
  buildImportComponentArgv,
  buildImportRequirementsArgv,
  buildListArgv,
  buildRunEnding,
  buildRunGenerations,
  buildRunMessage,
  buildRunRefreshesResources,
  type CapturedRun,
  type ChildCapture,
  COMMAND_IDS,
  captureChild,
  checkModelJsonArgv,
  clientTableUnreadableMessage,
  currentPlatform,
  type DetailsAt,
  DIAGRAMS_PROGRESS_TEXT,
  decideCliUnavailablePrompt,
  decideExportComponentPrompt,
  decodeChunks,
  describeBuildProgress,
  describeRunning,
  diagramsArgument,
  EXPERIMENT_KINDS,
  EXPORT_COMPONENT_PROMPT_TEXT,
  type ExperimentKind,
  experimentReportPath,
  experimentSettingRefusal,
  exportedArxmlPath,
  type ImportReport,
  type LayerListing,
  MAPPING_DIALOG_TITLE,
  MAPPING_PICK_TEXT,
  METRICS_SINCE_SETTING,
  mapExitCodeToOutcome,
  mapPreflightResult,
  metricsArgv,
  metricsSinceRefusal,
  type NamePromptDecision,
  type NamePromptText,
  onboardingApplyFailedMessage,
  outcomeMessage,
  PROBLEMS_PANEL_DETAILS,
  parseListJson,
  planOnboardingSteps,
  preflightNotFoundMessage,
  REQUIREMENTS_MAPPING_SETTING,
  type RunOutput,
  reportInsideWorkspace,
  resolveManifest,
  resourceReportJsonArgv,
  runBuildSteps,
  SerialRunner,
  SPAWNING_COMMAND_LABELS,
  SWC_LAYER,
  toolchainRunningRefusal,
  validateSwcNameInput,
  viewsRootWriteRunningRefusal,
  withImportReportFile,
} from "./commandCore";
import {
  type CheckRun,
  DIAGNOSTIC_SOURCE,
  describeFindingCounts,
  FINDING_COUNTS_TOOLTIP,
  linkAt,
  type Place,
  type Placement,
  PROBLEMS_FOCUS_COMMAND,
  placeFinding,
  placeRefusals,
  placeSubject,
  QUICK_FIX_RULES,
  quickFixFor,
  readCheckRun,
  rehomeRefusedFile,
  requirementVersion,
} from "./diagnosticsCore";
import {
  DIAGRAMS_INDEX_FILENAME,
  type DiagramsPress,
  diagramsIndexPath,
  diagramsViewPath,
  ModelWatch,
  ModelWatchSession,
  newestSourceChange,
  pressOfShow,
  reportDiagramsPress,
  type TabShow,
  viewPageName,
  type WatchDisposable,
  type WatchPattern,
} from "./diagrams";
import {
  acceptOpen,
  besideColumn,
  chooseView,
  DIAGRAMS_TAB_TITLE,
  DIAGRAMS_TAB_VIEW_TYPE,
  type DiagramIndex,
  followLink,
  GENERATE_DIAGRAMS_BUTTON,
  generateArgument,
  indexedStorageName,
  lexicalStorageName,
  MISSING_PAGE_LINE,
  memberAtLine,
  memberLineOf,
  modelAllowlist,
  NO_SCRIPT_HASH_LINE,
  noDiagramsLine,
  notOnAnyDiagramLine,
  OUT_OF_DATE_MESSAGE,
  outlineRoot,
  PAGE_SCRIPT_HASH_FILE,
  parseModelBlock,
  parsePageScriptHash,
  parseTabMessage,
  pickItems,
  pickPlaceholder,
  planShow,
  type RevealRequest,
  readDiagramIndex,
  realPathIndex,
  refusalDocument,
  refusalLine,
  resolveViewsFile,
  revealName,
  SHOW_IN_DIAGRAM_NO_ELEMENT_LINE,
  SHOW_IN_DIAGRAM_NO_MODEL_ROOT_LINE,
  showInDiagramArgument,
  type TabEntry,
  TabState,
  tabTitle,
  viewsForElement,
  webviewDocumentRereading,
} from "./diagramsTab";
import { guideLens, guideLensCommand } from "./guideLens";
import {
  GUIDE_OUTPUT_CHANNEL_NAME,
  GuideSession,
  type GuideSpawnContext,
  serveGuideTo,
} from "./guideServer";
import {
  AGENT_PROFILE_RELATIVE_PATH,
  buildHealthReport,
  checkAgentProfile,
  checkBundledSchemas,
  checkModelGuard,
  checkToolchain,
  copilotNotUsed,
  countProblems,
  declaredSchemaFiles,
  type HealthInput,
  type InstalledExtension,
  NOT_VALIDATION_EVIDENCE,
  probeOncePerPath,
  type ResourceHealth,
  summaryLine,
  type VersionProbe,
} from "./healthCheck";
import {
  diagnosticRule,
  guideCommandArg,
  guidePageUrl,
  guideTabOpen,
  HELP_TOPICS,
  type HelpTopic,
  helpTopicForRun,
  helpUri,
  modelLinkAction,
  parseHelpUri,
  RULES_PAGE,
  RuleLinkResolver,
  uriRoute,
} from "./helpLinks";
import {
  type ImpactAnswer,
  type ImpactEditor,
  type ImpactNode,
  type ImpactTree,
  impactAfterFolderChange,
  impactAfterRun,
  impactFileNotFoundMessage,
  impactLayerLabel,
  impactNodeOf,
  impactNodes,
  impactQuestion,
  impactRunLine,
  readImpactDocument,
} from "./impactCore";
import {
  IMPORT_TAB_VIEW_TYPE,
  type ImportFlowHost,
  type ImportReportRendering,
  ImportTabs,
  importComponentFlow,
  importRequirementsFlow,
  type RequirementsImportHost,
  renderImportReport,
  type TraceApplyOutcome,
  traceApplyFlow,
} from "./importTabCore";
import { isLayerFilePath, LAYER_FILE_CONTEXT_KEY, layerDirectoryRules, layerOf } from "./layerFile";
import {
  type DiscoveredLink,
  descriptionLinks,
  type LinkCandidate,
  reportKind,
  reportLinks,
  resolveCandidate,
  scanModel,
} from "./linkCore";
import { MCP_PROVIDER_ID, type McpServerDescriptor, SwcraftMcpServerProvider } from "./mcpProvider";
import {
  type DefinitionTarget,
  decodeModelText,
  definitionTarget,
  dependentLocations,
  hoverCard,
  hoverMarkdown,
  identityAt,
  type LensIndex,
  type LensItem,
  type LensPlan,
  lensPlan,
  loadReferences,
  type Reader,
  type References,
  type ReferencesAnswer,
  referencesAnswer,
  requestReader,
  resolveElement,
  unsavedIdentityAnswer,
} from "./navigationCore";
import {
  ADD_AGENT_PROFILE_CHOICE,
  AGENT_ASSET_NAME,
  AGENT_PROFILE_NOTICE,
  AGENT_PROFILE_NOTICE_CLIENT,
  AGENT_PROFILE_NOTICE_DISMISSED_KEY,
  agentProfileNoticeAction,
  agentProfilePath,
  applyAgentProfilePlan,
  applyCompanionSettings,
  applyOnboardingPlan,
  type CompanionOutcome,
  codexHookOutsideGitMessage,
  DISMISS_AGENT_PROFILE_CHOICE,
  entryExists,
  HOOK_ASSET_NAME,
  hookPath,
  keptCompanionMessage,
  type OnboardingPlan,
  onboardedMessages,
  onboardingRefusal,
  planAgentProfileNotice,
  planOnboarding,
  shouldOfferAgentProfile,
  writtenFiles,
} from "./onboarding";
import {
  findManifest,
  MANIFEST_FILENAME,
  readManifestPaths,
  resolveModelRoot,
  resolveViewsRoot,
} from "./project";
import {
  isResourceRefusal,
  parseResourceDocument,
  type ResourceDocument,
  type ResourceNode,
  type ResourceReportOptions,
  type ResourceTree,
  resourceLine,
  resourceNodeOf,
  resourceNodes,
  resourceNotFoundMessage,
  resourceRunLine,
} from "./resourceCore";
import {
  answerPlan,
  isPendingFileName,
  listedReviewKeys,
  localUtcOffsetMinutes,
  NO_LONGER_WAITING,
  newNonce,
  PENDING_SUFFIX,
  type PendingRecord,
  parseTabAnswer,
  pendingFileName,
  REVIEW_AVAILABLE_KEY,
  REVIEW_OPEN_COMMAND,
  REVIEW_REREAD_MS,
  REVIEW_TAB_VIEW_TYPE,
  REVIEW_VIEW_ID,
  type ReviewEntry,
  type ReviewNode,
  readReviewEntry,
  reviewBadgeTooltip,
  reviewHtml,
  reviewIdentity,
  reviewKey,
  reviewNodes,
  reviewsDir,
  reviewTabTitle,
  reviewTransitions,
  sameReview,
  windowReviews,
  writeDecision,
} from "./reviewCore";
import { channelQuote } from "./storageName";
import {
  type OutlineSymbol,
  outlineOf,
  resolveSymbolLine,
  type Span,
  workspaceSymbols,
} from "./symbolCore";
import {
  ANNOTATIONS_DIALOG_TITLE,
  ANNOTATIONS_PICK_TEXT,
  acceptTestTablesMessage,
  askTestTablesAnnotations,
  readTestTables,
  TEST_TABLES_ANNOTATIONS_SETTING,
  TEST_TABLES_READY_MESSAGE,
  TEST_TABLES_TAB_TITLE,
  TEST_TABLES_VIEW_TYPE,
  type TestTablesAnnotationsHost,
  type TestTablesRead,
  testTablesArgv,
  testTablesEmptyPage,
  testTablesPage,
  testTablesRefusalPage,
  testTablesUnreadablePage,
} from "./testTablesTab";
import {
  EXECUTABLE_SETTING,
  type Platform,
  parseSwcraftVersion,
  resolveBareCommand,
  resolveOnPath,
  resolveSwcraft,
  SWCRAFT_COMMAND_NAME,
  toolchainHome,
} from "./toolchain";
import {
  ARCHIVE_SOURCE_ITEMS,
  type ArchiveSourceItem,
  archiveVersion,
  buildInstallerArgv,
  buildUnpackArgv,
  decideUpdate,
  findZipUnpacker,
  INSTALL_TOOLCHAIN_COMMAND_ID,
  installerFailureMessage,
  offersReload,
  pathAdviceAfterRun,
  pathLookup,
  pendingReloadNote,
  quoteArgvForEcho,
  settingShadowsInstallationNote,
  sideloadNote,
  UPDATE_TOOLCHAIN_COMMAND_ID,
  versionChangedNotice,
} from "./toolchainInstall";
import {
  BUSY_REFUSAL,
  buildTraceApplyArgv,
  buildTraceDocumentsArgv,
  buildTraceSearchArgv,
  CHANGED_ON_DISK,
  choose,
  chooseCandidates,
  chooseDocuments,
  clearDecisions,
  clearQuestion,
  componentMembers,
  confirmLink,
  DIRTY_REFUSAL,
  DISCARD_THEM_BUTTON,
  DROPPED_STILL_PENDING,
  droppedQuestion,
  droppedWithDecisions,
  type EditResult,
  GROUP_LEFT_OUT,
  holdingTraceBusy,
  isDirtyRefusal,
  isRefusal,
  labellingArgumentOf,
  MODE_LABELS,
  markStrong,
  modePickerTitle,
  modePickItems,
  modesInScope,
  notFoundMessage,
  PENDING_APPLY_BUTTON,
  PENDING_DISCARD_BUTTON,
  type ProposalsFile,
  parseTraceDocuments,
  pendingCount,
  pendingLine,
  pendingQuestion,
  proposalsPathOf,
  readMode,
  readModes,
  readProposals,
  reject,
  removeChoice,
  requirementLocation,
  SEARCH_REFUSED_PENDING,
  STILL_PENDING,
  searchNeeded,
  searchRefusedPending,
  serializeProposals,
  TRACE_MODES,
  type TraceDocuments,
  type TraceLine,
  type TraceMode,
  type TraceModes,
  type TraceNode,
  tick,
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
import {
  type Artifact,
  ArtifactRecord,
  type ArtifactState,
  type ArtifactStates,
  buildProjectNode,
  buildTree,
  type CommandItemDescriptor,
  type CommandOutcome,
  type CommandStatus,
  decideResourceReportState,
  describeBadge,
  isOutOfDate,
  RunningCommands,
  type SectionDescriptor,
  SetupSectionRecord,
  STATUS_WINDOW_MS,
  setupSectionCollapsed,
} from "./treeView";

// The shared channel: every command whose child runs to completion writes its answer here.
// The guide server has its own channel because it logs one line per HTTP request for as
// long as the window is open.
const OUTPUT_CHANNEL_NAME = "SWCraft";
const TREE_VIEW_ID = "swcraftView";

/** How long `swcraft --version` is given to answer: well above an interpreter start. */
const VERSION_PROBE_TIMEOUT_MS = 15_000;

/**
 * How long `swcraft list --format json` is given to answer. Longer than the version probe
 * because it reads every element file, and still bounded: a CLI that never returns would
 * be a QuickPick that never opens.
 */
const LIST_PROBE_TIMEOUT_MS = 60_000;

/**
 * Whether a toolchain run is in flight in this window.
 *
 * A run takes minutes with no cancellation, and two runs against the same `--home` can have
 * one delete the version directory the other just pointed `current` at. Module scope because
 * there is one installation home per user. `Build` and a `Diagrams` press refuse while it
 * is taken, because each runs from the installation the run may replace; `toolchainCommand`
 * refuses while `viewsRootWrites` is busy or the post-Build refresh runs. The flag is taken
 * in the same synchronous turn as those checks. Does not reach a second window, a
 * terminal running the installer, or the commands that write nothing under `views_root`.
 */
let toolchainRunInFlight = false;

/**
 * Every `swcraft` run in this window that writes `views_root`, one at a time: a whole
 * `Build` and a `Diagrams` press's `generate diagrams`. Does not reach two windows on one
 * folder or a terminal run (see `SerialRunner`).
 */
const viewsRootWrites = new SerialRunner();

function getWorkspaceRoot(): string | undefined {
  return vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
}

// The last-run status of each command, keyed by command id, shown on its tree item.
const commandStatuses = new Map<string, CommandStatus>();
let treeDataProvider: SwcraftTreeDataProvider | undefined;

// One pending clearing timer per command id, so a re-run replaces its timer instead of
// stacking one that would clear the newer status early.
const statusTimers = new Map<string, ReturnType<typeof setTimeout>>();

function clearStatusTimer(commandId: string): void {
  const pending = statusTimers.get(commandId);
  if (pending !== undefined) {
    clearTimeout(pending);
    statusTimers.delete(commandId);
  }
}

function clearStatusTimers(): void {
  for (const pending of statusTimers.values()) {
    clearTimeout(pending);
  }
  statusTimers.clear();
}

/**
 * Record what a command run did and when, and refresh the TreeView. The one place the
 * completion time is stamped.
 *
 * The status clears itself after `STATUS_WINDOW_MS`: the renderer already hides an old
 * status, but a tree nobody touches never re-renders, so the timer makes the clear prompt.
 */
function recordCommandStatus(commandId: string, outcome: CommandOutcome): void {
  commandStatuses.set(commandId, { ...outcome, at: new Date() });
  clearStatusTimer(commandId);
  statusTimers.set(
    commandId,
    setTimeout(() => {
      statusTimers.delete(commandId);
      commandStatuses.delete(commandId);
      treeDataProvider?.refresh();
    }, STATUS_WINDOW_MS),
  );
  treeDataProvider?.refresh();
}

/**
 * The command ids with a run in flight, which the tree renders as `running…`; every command
 * that spawns the toolchain enters it (`SPAWNING_COMMAND_LABELS`). Counted per id, because
 * two presses of one command overlap while the second waits in `viewsRootWrites`.
 */
const runningCommands = new RunningCommands();

/**
 * Run *task* while the panel says so, with no popup: the view's progress bar, a status-bar
 * text that each `report()` moves, and the item's `running…`. Neither location offers
 * cancellation or a fraction, so a `Build`'s steps are carried by the status-bar text.
 *
 * It wraps the **spawning** part of a command, never a prompt before it: a picker or dialog
 * is the user's turn, not a run's. The flag is dropped in `finally` before the caller
 * records the outcome, so the outcome replaces `running…` rather than racing it.
 */
async function withRunIndicator<T>(
  commandId: string,
  text: string,
  task: (report: (text: string) => void) => Promise<T>,
): Promise<T> {
  runningCommands.start(commandId);
  treeDataProvider?.refresh();
  try {
    return await vscode.window.withProgress({ location: { viewId: TREE_VIEW_ID } }, () =>
      vscode.window.withProgress({ location: vscode.ProgressLocation.Window }, (statusBar) => {
        statusBar.report({ message: text });
        return task((next) => statusBar.report({ message: next }));
      }),
    );
  } finally {
    runningCommands.finish(commandId);
    treeDataProvider?.refresh();
  }
}

// --- The artifacts' state: absent, current, out of date -------------------------
//
// A save regenerates nothing; the artifacts are marked out of date on the items that
// generate them. This is the half that reads the clock and the disk (the decisions are
// treeView.ts's).

/**
 * The disk half of the artifacts' record (`ArtifactRecord`):
 *
 * - **The last successful generation**, per artifact and folder, lives in `workspaceState`
 *   (the files cannot serve: `generate diagrams` keeps a file it would rewrite unchanged). It
 *   is the instant the generation *started*, so a change made while it waited or ran marks
 *   its output.
 * - **The newest change** is read from disk when the watch is armed (so a change made while
 *   the window was closed marks the artifacts) and moved by each watcher event to the file's
 *   mtime (`noteModelChange()`).
 *
 * Does **not** reach: a terminal write or build (a terminal build records no success, so
 * its output reads *out of date*, the safe side), or a file whose mtime predates the last
 * success (`mv`, `cp -p`, an unpacked archive, FAT's two-second mtime), which leaves the
 * artifacts *current*. The mtime is used over the delivery time because a save just
 * before a press is often delivered after it started.
 */
async function scanNewestChange(folder: string): Promise<number | undefined> {
  const resolution = resolveManifest(folder);
  if (!resolution.ok) {
    return undefined;
  }
  return newestSourceChange(resolveModelRoot(resolution.manifestPath), resolution.manifestPath);
}

/**
 * `scanNewestChange()` for the tracker's rescan, its answer handed to the Resources view's
 * report too: an arming drops the stamps of events still waiting on their `stat()`, and
 * without this the report would stay *current* after such a change. A scan answered after
 * a newer arming marks nothing.
 */
async function scanNewestChangeForBoth(folder: string): Promise<number | undefined> {
  const generation = modelWatchGeneration;
  const newest = await scanNewestChange(folder);
  if (newest !== undefined && generation === modelWatchGeneration) {
    markResourcesOutOfDate(newest);
  }
  return newest;
}

/**
 * A watcher event: record its arrival now (what the diagram lens's direct open reads), then
 * stamp it with the file's mtime, or now for a deletion. The Resources and Impact views
 * take the same stamp unless the watch was armed again first.
 */
function noteModelChange(changedPath: string): void {
  const stamp = artifactTracker?.changeStamp(Date.now());
  // The stat is asynchronous: an answer after the watch was armed again belongs to the old
  // folder and is dropped.
  const generation = modelWatchGeneration;
  const noted = (changedAt: number): void => {
    if (generation !== modelWatchGeneration) {
      return;
    }
    markResourcesOutOfDate(changedAt);
    markImpactOutOfDate(changedAt);
    stamp?.(changedAt);
  };
  void fs.promises.stat(changedPath).then(
    (stat) => noted(stat.mtimeMs),
    () => noted(Date.now()),
  );
}

/** Which artifacts' files exist for the project in *folder*, or undefined with no manifest. */
function artifactsOnDisk(folder: string): Record<Artifact, boolean> | undefined {
  const resolution = resolveManifest(folder);
  if (!resolution.ok) {
    return undefined;
  }
  // A manifest whose paths the CLI refuses: both items read *absent*, and a press answers
  // with the refusal.
  const paths = readManifestPaths(resolution.manifestPath);
  const accepted = paths.refusal === undefined;
  return {
    arxml: accepted && fs.existsSync(paths.resolved["outputs.arxml"]),
    views: accepted && fs.existsSync(diagramsIndexPath(paths.resolved.views_root)),
  };
}

/**
 * Each marked output's state: the artifacts' for the project in *folder*, and the
 * Resources view's report, so the Model item, the badge and the view agree.
 */
function artifactStates(folder: string | undefined): ArtifactStates {
  const resources = resourceReportState();
  const report: ArtifactStates = resources === undefined ? {} : { resources };
  if (folder === undefined || artifactTracker === undefined) {
    return report;
  }
  const onDisk = artifactsOnDisk(folder);
  if (onDisk === undefined) {
    return report;
  }
  return { ...artifactTracker.states(folder, onDisk), ...report };
}

/** Set by `registerCommands()`; the tree reads it, and Build and Diagrams record into it. */
let artifactTracker: ArtifactRecord | undefined;

/** The window's guide server and channel: what *Learn more* and a rule link open the guide with. */
let helpGuide: { session: GuideSession; channel: vscode.OutputChannel } | undefined;

/** The Problems panel's rule links, mapped through `asExternalUri` (`RuleLinkResolver`). */
let ruleLinkResolver: RuleLinkResolver<vscode.Uri> | undefined;

/** The `SWCraft` view, set by `registerTreeView()`; its badge counts the out-of-date artifacts. */
let treeView: vscode.TreeView<SwcraftTreeNode> | undefined;

/** Redraw the tree and the badge after an artifact's facts, or the Resources view's state, changed. */
function artifactsChanged(): void {
  treeDataProvider?.refresh();
  if (treeView !== undefined) {
    treeView.badge = describeBadge(artifactStates(getWorkspaceRoot()));
  }
  diagramsTab.syncOutOfDate();
}

function showNoWorkspaceError(): void {
  notifyWithHelp(
    "error",
    "SWCraft: no workspace folder is open. Open a SWCraft project workspace to use this command.",
    "project",
  );
}

/** Resolve the manifest for the current workspace, surfacing an error notification on failure. */
function resolveManifestOrNotify():
  | { workspaceRoot: string; manifestPath: string; manifestRelPath: string }
  | undefined {
  const workspaceRoot = getWorkspaceRoot();
  if (workspaceRoot === undefined) {
    showNoWorkspaceError();
    return undefined;
  }
  const resolution = resolveManifest(workspaceRoot);
  if (!resolution.ok) {
    notifyWithHelp("error", resolution.message, "project");
    return undefined;
  }
  return {
    workspaceRoot,
    manifestPath: resolution.manifestPath,
    manifestRelPath: resolution.manifestRelPath,
  };
}

/** A CLI run's whole result: commandCore's `RunOutput` plus the child's stdout. */
interface SpawnCliResult extends RunOutput {
  /** The child's whole stdout, also streamed to the channel unless the caller asked for no echo. */
  stdout: string;
}

/**
 * The `swcraft.executable` setting's value, or `undefined` when it is unset.
 *
 * Read with no resource scope: the setting names a machine's installation, so a
 * folder-less window must read the same value.
 */
function executableSetting(): string | undefined {
  return vscode.workspace.getConfiguration().get<string>(EXECUTABLE_SETTING);
}

/**
 * The `swcraft` executable this window spawns: the workstation installation, or the
 * `swcraft.executable` override. It takes no workspace root, because which executable runs
 * is a property of the machine, not of the open folder.
 */
function swcraftResolution() {
  return resolveSwcraft(
    executableSetting(),
    process.env,
    os.homedir(),
    currentPlatform(),
    fs.existsSync,
  );
}

function resolveSwcraftExecutable(): string {
  return swcraftResolution().executable;
}

/** `fs.existsSync`, narrowed to "and it is a **file**" — `project.ts`'s question. */
function isFile(candidate: string): boolean {
  try {
    return fs.statSync(candidate).isFile();
  } catch {
    return false;
  }
}

/**
 * The environment every CLI spawn runs in: the extension host's own, plus `PYTHONUTF8=1`.
 *
 * On Windows, Python >= 3.11 writes a piped stdout/stderr in the locale encoding unless
 * UTF-8 mode is on, so a non-ASCII project path in an error or traceback came back
 * mojibake. The switch is a no-op where the locale is already UTF-8.
 */
function cliEnv(): NodeJS.ProcessEnv {
  return { ...process.env, PYTHONUTF8: "1" };
}

/**
 * Spawn *executable* with *argv* in *cwd*, streaming stdout/stderr to *outputChannel* and
 * resolving once the process exits or fails to launch.
 *
 * **The echo is the canonical command line, not the spawn's first argument:**
 * `$ swcraft validate --project swcraft.yaml` is what a reader can retype in a terminal.
 * The spawn uses the absolute path so it depends on no `PATH` (a GUI-launched VS Code does
 * not inherit the shell's); the launch-failure line and *Check Editor Tooling* name it.
 */
function spawnCli(
  executable: string,
  argv: string[],
  cwd: string,
  outputChannel: vscode.OutputChannel,
  options: { echoStdout?: boolean } = {},
): Promise<SpawnCliResult> {
  // `echoStdout: false`: the stdout is a JSON document the caller reads, never written to
  // the channel.
  const echoStdout = options.echoStdout ?? true;
  outputChannel.appendLine(`$ ${SWCRAFT_COMMAND_NAME} ${argv.join(" ")}`);
  return new Promise<SpawnCliResult>((resolve) => {
    const child = spawn(executable, argv, { cwd, env: cliEnv() });
    // The captured streams are decoded once at the end, and the channel is fed through one
    // streaming decoder per stream, so a multi-byte character split across chunks survives.
    const stdoutChunks: Buffer[] = [];
    const stdout = (): string => decodeChunks(stdoutChunks);
    const stderrChunks: Buffer[] = [];
    const stderr = (): string => decodeChunks(stderrChunks);
    const stdoutDecoder = new StringDecoder("utf8");
    const stderrDecoder = new StringDecoder("utf8");
    child.stdout?.on("data", (chunk: Buffer) => {
      stdoutChunks.push(chunk);
      if (echoStdout) {
        outputChannel.append(stdoutDecoder.write(chunk));
      }
    });
    child.stderr?.on("data", (chunk: Buffer) => {
      stderrChunks.push(chunk);
      outputChannel.append(stderrDecoder.write(chunk));
    });
    child.on("error", (err) => {
      outputChannel.appendLine(
        `SWCraft: failed to launch the SWCraft CLI (${executable}): ${err.message}`,
      );
      resolve({ exitCode: null, spawnFailed: true, stdout: stdout(), stderr: stderr() });
    });
    child.on("close", (code) => {
      // A character still incomplete at the end is flushed as the replacement character.
      outputChannel.append(stdoutDecoder.end() + stderrDecoder.end());
      resolve({ exitCode: code, spawnFailed: false, stdout: stdout(), stderr: stderr() });
    });
  });
}

/**
 * `spawnCli()`'s capture sibling: the child's stdout/stderr are collected and returned, and
 * no output channel is touched. A QuickPick needs the stdout as a value, and a picker must
 * not pop the output panel open; the caller decides what to do with `stderr`.
 *
 * *signal* stops the child as the timeout does: the result is `exitCode: null`, not
 * `spawnFailed`, and the caller that aborted reads it in that light. The settle logic is
 * commandCore's `captureChild()`.
 */
function spawnCliCapture(
  executable: string,
  argv: string[],
  cwd: string,
  timeoutMs?: number,
  signal?: AbortSignal,
): Promise<ChildCapture> {
  return captureChild(
    spawn(executable, argv, { cwd, env: cliEnv() }),
    executable,
    timeoutMs,
    signal,
  );
}

/**
 * What running `swcraft list [layer] --format json` produced:
 *
 * - `listed`: a parsed document; an empty list is still an answer.
 * - `unanswered`: the CLI **ran** and could not answer (a non-zero exit, such as an
 *   unparseable element file, or a document `parseListJson()` refuses). The user can still
 *   type the name.
 * - `unavailable`: the CLI **could not be started**. Nothing typed can succeed, so this
 *   never takes the fallback branch (`decideCliUnavailablePrompt()`).
 *
 * Every failing answer writes its detail to the channel without showing it; for
 * `unavailable` the caller raises a notification too.
 */
type ListResult =
  | { kind: "listed"; listings: LayerListing[] }
  | { kind: "unanswered" }
  | { kind: "unavailable" };

/**
 * Run `swcraft list [layer] --format json` for the detected project and parse it. *layer*
 * omitted lists every layer.
 */
async function listModel(
  executable: string,
  workspaceRoot: string,
  manifestRelPath: string,
  layer: string | undefined,
  outputChannel: vscode.OutputChannel,
): Promise<ListResult> {
  const argv = buildListArgv(manifestRelPath, layer);
  // Bounded because it runs behind a picker; longer than the version probe because it reads
  // the whole model.
  const result = await spawnCliCapture(executable, argv, workspaceRoot, LIST_PROBE_TIMEOUT_MS);
  if (result.spawnFailed) {
    outputChannel.appendLine(`$ ${SWCRAFT_COMMAND_NAME} ${argv.join(" ")}`);
    outputChannel.append(result.stderr);
    outputChannel.appendLine(
      `SWCraft: the CLI could not be started (${executable}) — no SWCraft command can run until ` +
        "that is fixed. Run `SWCraft: Check Editor Tooling`.",
    );
    return { kind: "unavailable" };
  }
  if (result.exitCode !== 0) {
    outputChannel.appendLine(`$ ${SWCRAFT_COMMAND_NAME} ${argv.join(" ")}`);
    outputChannel.append(result.stderr);
    outputChannel.appendLine(
      "SWCraft: could not list the project's elements; falling back to typing the name.",
    );
    return { kind: "unanswered" };
  }
  try {
    return { kind: "listed", listings: parseListJson(result.stdout) };
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error);
    outputChannel.appendLine(`$ ${SWCRAFT_COMMAND_NAME} ${argv.join(" ")}`);
    outputChannel.appendLine(`SWCraft: ${detail}`);
    outputChannel.appendLine("SWCraft: falling back to typing the name.");
    return { kind: "unanswered" };
  }
}

/**
 * Raise the prompt a `NamePromptDecision` asks for, and return the chosen element name, or
 * `undefined` for every "do not run the command" outcome (Esc, or nothing to choose).
 * *Which* prompt to raise is `decideExportComponentPrompt()`'s, testable under `node --test`.
 */
async function runNamePrompt(
  decision: NamePromptDecision,
  text: NamePromptText,
  validateInput: (value: string) => string | undefined,
  outputChannel: vscode.OutputChannel,
): Promise<string | undefined> {
  if (decision.kind !== "pick" && decision.note !== undefined) {
    // Logged, never shown: the fallback is visible in the log without hijacking the panel.
    outputChannel.appendLine(decision.note);
  }
  if (decision.kind === "inform") {
    // `severity` is the decision's: "no SWCs yet" and "the CLI could not be started" both
    // cancel, and only the second is something wrong.
    if (decision.severity === "warning") {
      void vscode.window.showWarningMessage(decision.message);
    } else {
      void vscode.window.showInformationMessage(decision.message);
    }
    return undefined;
  }
  if (decision.kind === "pick") {
    // Esc resolves to undefined, which cancels the command; it never runs with no name.
    const picked = await vscode.window.showQuickPick(decision.items, {
      title: text.title,
      placeHolder: text.pickPlaceHolder,
      matchOnDescription: true,
    });
    return picked?.label;
  }
  return await vscode.window.showInputBox({
    title: text.title,
    prompt: text.inputPrompt,
    placeHolder: text.inputPlaceHolder,
    validateInput,
  });
}

/**
 * Ask for the SWC to export: a QuickPick of the project's components when
 * `swcraft list` could name them, an actionable message when it answered that
 * there are none, and the free-text prompt when it could not answer at all.
 */
async function promptForSwcName(
  executable: string,
  workspaceRoot: string,
  manifestRelPath: string,
  outputChannel: vscode.OutputChannel,
): Promise<string | undefined> {
  const result = await listModel(
    executable,
    workspaceRoot,
    manifestRelPath,
    SWC_LAYER,
    outputChannel,
  );
  return await runNamePrompt(
    namePromptDecision(result, decideExportComponentPrompt, executable),
    EXPORT_COMPONENT_PROMPT_TEXT,
    validateSwcNameInput,
    outputChannel,
  );
}

/**
 * Turn a `ListResult` into the prompt decision for it. `unavailable` never reaches
 * *decide*: "the CLI could not be started" is not a fact about the model.
 */
function namePromptDecision(
  result: ListResult,
  decide: (listings: LayerListing[] | undefined) => NamePromptDecision,
  executable: string,
): NamePromptDecision {
  if (result.kind === "unavailable") {
    return decideCliUnavailablePrompt(executable);
  }
  return decide(result.kind === "listed" ? result.listings : undefined);
}

/**
 * Record a CLI run's outcome under *commandId* from its exit code, and on a failure show
 * the warning `outcomeMessage()` words for *label*, with the help topic read from what the
 * CLI said (`helpTopicForRun()`). Answers whether the run succeeded, so a caller's own
 * success step runs only then. *detailsAt* is where the warning sends the user;
 * `outcomeMessage()`'s default when left out.
 */
function recordCliOutcome(
  commandId: string,
  label: string,
  run: RunOutput,
  detailsAt?: DetailsAt,
): boolean {
  const outcome = mapExitCodeToOutcome(run.exitCode);
  const message = outcomeMessage(label, outcome, detailsAt);
  recordCommandStatus(
    commandId,
    outcome.kind === "success"
      ? { kind: "success" }
      : { kind: "failure", message: message ?? "failed" },
  );
  if (message !== undefined) {
    notifyWithHelp("warning", message, helpTopicForRun(run));
    return false;
  }
  return true;
}

/** The button a refusal whose answer is a guide section carries. */
const LEARN_MORE = "Learn more";

/**
 * Show *message* at *severity*, with *Learn more* when *topic* names a guide section; a
 * press opens that section in the served guide. The one place the button is offered.
 */
function notifyWithHelp(
  severity: "warning" | "error",
  message: string,
  topic: HelpTopic | undefined,
): void {
  const guide = helpGuide;
  if (topic === undefined || guide === undefined) {
    if (severity === "error") {
      void vscode.window.showErrorMessage(message);
    } else {
      void vscode.window.showWarningMessage(message);
    }
    return;
  }
  const shown =
    severity === "error"
      ? vscode.window.showErrorMessage(message, LEARN_MORE)
      : vscode.window.showWarningMessage(message, LEARN_MORE);
  void shown.then((choice) => {
    if (choice === LEARN_MORE) {
      const { page, anchor } = HELP_TOPICS[topic];
      void openGuidePage(guide.session, guide.channel, page, anchor);
    }
  });
}

/**
 * `SWCraft: Check Model`: one `swcraft check-model --format json` run ({@link checkModelRun})
 * inside the run indicator. Its findings fill the Problems panel; nothing is written.
 *
 * The answer is the Problems view, which every finished run opens and focuses (a refused
 * run is an entry at the manifest's line), so the channel is not revealed and a failure's
 * warning points to the Problems panel ({@link PROBLEMS_PANEL_DETAILS}).
 */
async function checkModelCommand(
  outputChannel: vscode.OutputChannel,
  collection: vscode.DiagnosticCollection,
  artifactPath: string,
): Promise<void> {
  const resolved = resolveManifestOrNotify();
  if (resolved === undefined) {
    return;
  }
  const { workspaceRoot, manifestRelPath } = resolved;
  const executable = resolveSwcraftExecutable();
  const commandId = COMMAND_IDS["check-model"];
  const run = await withRunIndicator(commandId, describeRunning(commandId), () =>
    checkModelRun(
      executable,
      workspaceRoot,
      buildCliArgv("check-model", manifestRelPath),
      manifestRelPath,
      artifactPath,
      outputChannel,
      collection,
    ),
  );
  recordCliOutcome(commandId, "Check Model", run, PROBLEMS_PANEL_DETAILS);
  void vscode.commands.executeCommand(PROBLEMS_FOCUS_COMMAND);
}

/**
 * One `check-model` run whose findings fill the Problems panel: Check Model's run and
 * Build's step. *baseArgv* gets `--format json` appended; the JSON stdout is captured, not
 * echoed, and one closing line replaces the success sentence.
 *
 * The reference table is read **per run**, never at registration, so a damaged
 * `references.json` takes no command with it; then every finding is placed at the
 * manifest's line 0. The previous run's entries are replaced whole. Answers the run's
 * result untouched. The entries are the CLI's findings over the **saved** model, not the
 * gate (`swcraft validate` is), and no save runs this.
 *
 * Check Model and Build can overlap: only the latest-started run publishes its entries
 * ({@link checkModelRunsStarted}). A Build that stops before its `check-model` step leaves
 * the previous entries in place.
 */
async function checkModelRun(
  executable: string,
  workspaceRoot: string,
  baseArgv: readonly string[],
  manifestRelPath: string,
  artifactPath: string,
  outputChannel: vscode.OutputChannel,
  collection: vscode.DiagnosticCollection,
): Promise<SpawnCliResult> {
  checkModelRunsStarted += 1;
  const generation = checkModelRunsStarted;
  const run = await spawnCli(
    executable,
    checkModelJsonArgv(baseArgv),
    workspaceRoot,
    outputChannel,
    { echoStdout: false },
  );
  const reading = readCheckRun(run satisfies CheckRun, executable);
  const manifestFile = path.resolve(workspaceRoot, manifestRelPath);
  // One snapshot per run: every finding is placed against one text of each file.
  const reader = fsRequestReader();
  const modelRoot = resolveModelRoot(manifestFile);
  let placements: Placement[];
  if (reading.kind === "refused") {
    placements = placeRefusals(manifestFile, reading.reason, reader, (file) =>
      rehomeRefusedFile(file, modelRoot, fs.realpathSync),
    );
  } else {
    let refs: References | undefined;
    if (reading.document.findings.length > 0) {
      if (modelRoot === undefined) {
        outputChannel.appendLine(
          "SWCraft: the manifest's model_root could not be read, so every finding is placed at " +
            "the manifest's first line.",
        );
      } else {
        try {
          refs = loadReferences(fs.readFileSync(artifactPath, { encoding: "utf8" }));
        } catch (error) {
          outputChannel.appendLine(
            `SWCraft: the extension's reference table could not be read (${
              error instanceof Error ? error.message : String(error)
            }), so every finding is placed at the manifest's first line.`,
          );
        }
      }
    }
    placements = reading.document.findings.map((finding) =>
      placeFinding(refs, finding, modelRoot ?? workspaceRoot, manifestFile, reader),
    );
  }
  if (reading.summary !== undefined) {
    outputChannel.appendLine(reading.summary);
  }
  const ruleLinks = await resolveRuleLinks(
    placements.flatMap((place) => (place.code === undefined ? [] : [place.code])),
    outputChannel,
  );
  if (generation === checkModelRunsStarted) {
    publishPlacements(collection, placements, ruleLinks);
  }
  return run;
}

/** How many `check-model` runs have started in this window: {@link checkModelRun}'s run number. */
let checkModelRunsStarted = 0;

/**
 * The findings' status-bar item (`SWCraft: 0 errors · 35 warnings`), set and shown by
 * {@link publishPlacements}, so it shows what the Problems entries show. A click opens
 * Problems.
 */
let findingsStatusItem: vscode.StatusBarItem | undefined;

/**
 * The `target` of each published SWCraft entry that has one, keyed by {@link findingKey}:
 * what {@link quickFixesOf} checks the line's link against. A `vscode.Diagnostic` has no
 * field for it, and a code action receives a copy, so it is looked up by file, rule and
 * message.
 */
const findingTargets = new Map<string, string>();

/** The key of one entry in {@link findingTargets}: the file as a URI, so both sides spell it alike. */
function findingKey(file: vscode.Uri, code: string, message: string): string {
  return JSON.stringify([file.toString(), code, message]);
}

/** A place's columns on its one line, as a `vscode.Range`. */
function placementRange(place: Place): vscode.Range {
  return new vscode.Range(place.line, place.start, place.line, place.end);
}

/**
 * Replace *collection*'s entries whole with *placements*: one `vscode.Diagnostic` each, the
 * CLI's severity, source, code and message, a finding's further subjects as related
 * information.
 */
function publishPlacements(
  collection: vscode.DiagnosticCollection,
  placements: readonly Placement[],
  ruleLinks: ReadonlyMap<string, vscode.Uri>,
): void {
  const byFile = new Map<string, vscode.Diagnostic[]>();
  collection.clear();
  findingTargets.clear();
  for (const place of placements) {
    const diagnostic = new vscode.Diagnostic(
      placementRange(place),
      place.message,
      place.severity === "error"
        ? vscode.DiagnosticSeverity.Error
        : vscode.DiagnosticSeverity.Warning,
    );
    diagnostic.source = DIAGNOSTIC_SOURCE;
    if (place.code !== undefined) {
      // The rule name, drawn as a link to its row on the guide's Checks page when the link
      // resolved; the plain name otherwise. Read back through `diagnosticRule()`.
      const link = ruleLinks.get(place.code);
      diagnostic.code = link === undefined ? place.code : { value: place.code, target: link };
    }
    if (place.related.length > 0) {
      diagnostic.relatedInformation = place.related.map(
        (other) =>
          new vscode.DiagnosticRelatedInformation(
            new vscode.Location(vscode.Uri.file(other.file), placementRange(other)),
            place.message,
          ),
      );
    }
    if (place.code !== undefined && place.target !== undefined) {
      findingTargets.set(
        findingKey(vscode.Uri.file(place.file), place.code, place.message),
        place.target,
      );
    }
    const entries = byFile.get(place.file) ?? [];
    entries.push(diagnostic);
    byFile.set(place.file, entries);
  }
  for (const [file, entries] of byFile) {
    collection.set(vscode.Uri.file(file), entries);
  }
  if (findingsStatusItem !== undefined) {
    findingsStatusItem.text = describeFindingCounts(placements);
    findingsStatusItem.tooltip = FINDING_COUNTS_TOOLTIP;
    findingsStatusItem.show();
  }
}

/**
 * An experiment's panel item and palette command: spawn `swcraft experiment <kind>
 * --project <manifest>`, with `--config=<file>` when the kind's setting names a file, and
 * on success open the report it wrote in the Markdown preview.
 *
 * The setting (`swcraft.experiments.<kind>.config`) is read with the project folder as its
 * resource; it names an input file the CLI reads as data, never a program. Empty or unset
 * runs the kind's defaults. A missing report or preview is one channel line and the run
 * stays a success: the CLI succeeded.
 */
async function experimentCommand(
  kind: ExperimentKind,
  outputChannel: vscode.OutputChannel,
): Promise<void> {
  // The trace kind's flow is the view's: the documents, the pending question and the search.
  if (kind === "trace") {
    return traceSearchCommand(outputChannel);
  }
  const resolved = resolveManifestOrNotify();
  if (resolved === undefined) {
    return;
  }
  const { workspaceRoot, manifestRelPath } = resolved;
  const executable = resolveSwcraftExecutable();
  const { commandId, label, setting } = EXPERIMENT_KINDS[kind];
  const config = vscode.workspace
    .getConfiguration(undefined, vscode.Uri.file(workspaceRoot))
    .get<unknown>(setting);
  const refusal = experimentSettingRefusal(setting, config);
  if (refusal !== undefined) {
    outputChannel.appendLine(refusal);
    void vscode.window.showWarningMessage(refusal);
    return;
  }
  const argv = buildExperimentArgv(
    kind,
    manifestRelPath,
    typeof config === "string" ? config : undefined,
  );
  outputChannel.show(true);
  const run = await withRunIndicator(commandId, describeRunning(commandId), () =>
    spawnCli(executable, argv, workspaceRoot, outputChannel),
  );

  if (!recordCliOutcome(commandId, label, run)) {
    return;
  }
  await previewGeneratedReport(run.stdout, workspaceRoot, outputChannel);
}

/**
 * The report tail of the commands that write a Markdown report: read the report's path off
 * *output* (the `Generated report:` line, resolved against *workspaceRoot*; Resource Report
 * hands in its stderr, since its stdout is the view's document) and open it in the
 * Markdown preview. Called only after the outcome is recorded, so nothing here changes it:
 * no path printed, a report no longer on disk (a Build removes `dist/`; none of the others is
 * serialised with it), a path outside the workspace folder (not the run's own, so nothing
 * opens) and a preview that cannot open are each one channel line.
 */
async function previewGeneratedReport(
  output: string,
  workspaceRoot: string,
  outputChannel: vscode.OutputChannel,
): Promise<void> {
  const report = experimentReportPath(output, workspaceRoot);
  if (report === undefined) {
    outputChannel.appendLine(
      "SWCraft: the run printed no report path, so there is nothing to preview",
    );
    return;
  }
  if (!isFile(report)) {
    outputChannel.appendLine(
      `SWCraft: the report ${report} is not on disk any more, so there is nothing to preview`,
    );
    return;
  }
  const inside = reportInsideWorkspace(report, workspaceRoot, fs.realpathSync.native);
  if (inside === undefined) {
    outputChannel.appendLine(
      `SWCraft: the report path ${report} the run printed is outside the workspace folder, so nothing is previewed`,
    );
    return;
  }
  await openMarkdownPreview(vscode.Uri.file(inside), report, outputChannel);
}

/**
 * Open *uri* in the built-in Markdown preview, then `markdown.preview.refresh`: the
 * Markdown extension caches a document's rendering, so a report rewritten in place would
 * otherwise show the first run's text in a reopened preview. A preview that cannot open
 * (that extension disabled) is one channel line naming *name*.
 */
async function openMarkdownPreview(
  uri: vscode.Uri,
  name: string,
  outputChannel: vscode.OutputChannel,
): Promise<void> {
  try {
    await vscode.commands.executeCommand("markdown.showPreview", uri);
    await vscode.commands.executeCommand("markdown.preview.refresh");
  } catch (error) {
    const reason = error instanceof Error ? error.message : String(error);
    outputChannel.appendLine(
      `SWCraft: the report ${name} could not be opened in the Markdown preview: ${reason}`,
    );
  }
}

// --- The imports' tab: the review tab's form, holding an import's report ------
//
// The thin host of `importTabCore.ts`' controller (`ImportTabs`) and flows: one webview tab
// per report title, reused by the next report of the same title. It is not the review
// channel: nothing here touches the SWCraft home's `reviews/` or a decision record.

/**
 * *report* rendered for its tab through the built-in Markdown extension's
 * `markdown.api.render`. With that extension disabled the call rejects and the tab shows
 * the plain-text fallback. Rendered once per report.
 */
function importReportRendering(report: ImportReport): Promise<ImportReportRendering> {
  return renderImportReport(report, (markdown) =>
    vscode.commands.executeCommand<unknown>("markdown.api.render", markdown),
  );
}

/** The imports' tabs: the review tab's webview options, one panel per title, a fresh nonce per page. */
const importTabs = new ImportTabs({
  openPanel(title, events) {
    const panel = vscode.window.createWebviewPanel(
      IMPORT_TAB_VIEW_TYPE,
      title,
      { viewColumn: vscode.ViewColumn.Active, preserveFocus: false },
      { enableScripts: true, localResourceRoots: [] },
    );
    panel.webview.onDidReceiveMessage(events.message);
    panel.onDidDispose(events.disposed);
    return {
      setHtml: (html) => {
        panel.webview.html = html;
      },
      reveal: () => panel.reveal(undefined, false),
      dispose: () => panel.dispose(),
    };
  },
  nonce: newNonce,
});

/**
 * One import's flow host. A refusal is an error notification and records no outcome, since
 * the import that holds the item is still running.
 */
function importFlowHost(
  commandId: string,
  executable: string,
  workspaceRoot: string,
  dialog: { filters: Record<string, string[]>; title: string },
): ImportFlowHost {
  const label = SPAWNING_COMMAND_LABELS[commandId];
  return {
    tabs: importTabs,
    label,
    executable,
    pickFile: async () => {
      const picked = await vscode.window.showOpenDialog({
        canSelectFiles: true,
        canSelectFolders: false,
        canSelectMany: false,
        defaultUri: vscode.Uri.file(workspaceRoot),
        filters: dialog.filters,
        openLabel: "Import this file",
        title: dialog.title,
      });
      return picked === undefined || picked.length === 0 ? undefined : picked[0].fsPath;
    },
    render: importReportRendering,
    record: (run) => {
      recordCliOutcome(commandId, label, run);
    },
    checkModel: async () => {
      await vscode.commands.executeCommand("swcraft.checkModel");
    },
    refuse: (message) => {
      void vscode.window.showErrorMessage(message);
    },
  };
}

/**
 * One import run of *commandId* inside an indicator of its own, stdout kept out of the
 * channel, its Markdown report written into a temporary file `withImportReportFile()` reads
 * and removes.
 */
function importRun(
  commandId: string,
  executable: string,
  workspaceRoot: string,
  outputChannel: vscode.OutputChannel,
  argv: (report: string | undefined) => string[],
): Promise<CapturedRun> {
  outputChannel.show(true);
  return withRunIndicator(commandId, describeRunning(commandId), () =>
    withImportReportFile(
      workspaceRoot,
      (report) =>
        spawnCli(executable, argv(report), workspaceRoot, outputChannel, { echoStdout: false }),
      fs,
      { log: (line) => outputChannel.appendLine(line) },
    ),
  );
}

/**
 * `SWCraft: Change Report`: one click, no question. Spawns `swcraft change-report` with no
 * revisions (the working tree against `HEAD`) and on success opens the `change-report.md`
 * it wrote in the Markdown preview. The CLI prints the report's absolute path, so the
 * spawn's cwd does not move it. It writes nothing into the model.
 */
async function changeReportCommand(outputChannel: vscode.OutputChannel): Promise<void> {
  const resolved = resolveManifestOrNotify();
  if (resolved === undefined) {
    return;
  }
  const { workspaceRoot, manifestRelPath } = resolved;
  const executable = resolveSwcraftExecutable();
  const argv = buildCliArgv("change-report", manifestRelPath);
  outputChannel.show(true);
  const commandId = COMMAND_IDS["change-report"];
  const run = await withRunIndicator(commandId, describeRunning(commandId), () =>
    spawnCli(executable, argv, workspaceRoot, outputChannel),
  );
  if (!recordCliOutcome(commandId, "Change Report", run)) {
    return;
  }
  await previewGeneratedReport(run.stdout, workspaceRoot, outputChannel);
}

/**
 * `SWCraft: Architecture Metrics`: one click, no question. Reads `swcraft.metricsSince` (a
 * value that is not a list of revisions is refused before the run), spawns `swcraft metrics
 * --report` with one `--since=<rev>` per entry, or `--since-head` when empty, and on success
 * opens the `metrics.md` it wrote. The command exits 0 whenever it could read the project,
 * so a non-zero exit is a project or a named revision it could not read. It writes nothing
 * into the model.
 */
async function metricsCommand(outputChannel: vscode.OutputChannel): Promise<void> {
  const resolved = resolveManifestOrNotify();
  if (resolved === undefined) {
    return;
  }
  const { workspaceRoot, manifestRelPath } = resolved;
  const since = vscode.workspace
    .getConfiguration(undefined, vscode.Uri.file(workspaceRoot))
    .get<unknown>(METRICS_SINCE_SETTING);
  const refusal = metricsSinceRefusal(since);
  if (refusal !== undefined) {
    outputChannel.appendLine(refusal);
    void vscode.window.showWarningMessage(refusal);
    return;
  }
  const executable = resolveSwcraftExecutable();
  const argv = metricsArgv(
    buildCliArgv("metrics", manifestRelPath),
    Array.isArray(since) ? (since as string[]) : [],
  );
  outputChannel.show(true);
  const commandId = COMMAND_IDS.metrics;
  const run = await withRunIndicator(commandId, describeRunning(commandId), () =>
    spawnCli(executable, argv, workspaceRoot, outputChannel),
  );
  if (!recordCliOutcome(commandId, "Architecture Metrics", run)) {
    return;
  }
  await previewGeneratedReport(run.stdout, workspaceRoot, outputChannel);
}

/**
 * `SWCraft: Export Component`: choose a SWC, then spawn `swcraft generate arxml --component
 * <name>`. The CLI computes the output path (`<dist_root>/components/<name>.arxml`,
 * `<name>~N.arxml` for a case-only twin) and prints it on an `OUTPUT <path>` line.
 *
 * On success the exported ARXML opens in a non-preview tab; when no path was printed, the
 * file is not on disk or the editor cannot open it, an information notification names the
 * path instead, plus one channel line with the reason. The recorded outcome is the exit
 * code in every case.
 *
 * The picker's names come from `swcraft list swc --format json`, never from a walk of the
 * model: the extension reads no model file here. When the listing fails the prompt falls
 * back to free text, and an unknown name surfaces as the CLI's non-zero exit.
 */
async function exportComponentCommand(outputChannel: vscode.OutputChannel): Promise<void> {
  const resolved = resolveManifestOrNotify();
  if (resolved === undefined) {
    return;
  }
  const { workspaceRoot, manifestRelPath } = resolved;

  const executable = resolveSwcraftExecutable();

  const swcName = await promptForSwcName(executable, workspaceRoot, manifestRelPath, outputChannel);
  if (swcName === undefined) {
    // Cancelled, or a project with no SWCs (which said so): no status, nothing spawned.
    // showInputBox never accepts a value while validateInput returns an error.
    return;
  }

  const argv = buildExportComponentArgv(manifestRelPath, swcName);
  outputChannel.show(true);

  const run = await withRunIndicator(
    "swcraft.exportComponent",
    describeRunning("swcraft.exportComponent"),
    () => spawnCli(executable, argv, workspaceRoot, outputChannel),
  );

  if (!recordCliOutcome("swcraft.exportComponent", "Export Component", run)) {
    return;
  }
  // The export lands under dist/ at a path the user may not know, so show the file. Nothing
  // below changes the recorded outcome.
  const name = swcName.trim();
  const exported = exportedArxmlPath(run.stdout, workspaceRoot);
  if (exported === undefined) {
    outputChannel.appendLine(
      "SWCraft: the export printed no output path, so there is no file to open",
    );
    void vscode.window.showInformationMessage(
      `SWCraft: exported component '${name}'; the run printed no output path (see the SWCraft output channel).`,
    );
    return;
  }
  const notOpened = (reason: string): void => {
    outputChannel.appendLine(
      `SWCraft: the exported file ${exported} could not be opened: ${reason}`,
    );
    void vscode.window.showInformationMessage(
      `SWCraft: exported component '${name}' to ${exported}.`,
    );
  };
  if (!isFile(exported)) {
    notOpened("the file is not on disk any more");
    return;
  }
  try {
    const document = await vscode.workspace.openTextDocument(vscode.Uri.file(exported));
    await vscode.window.showTextDocument(document, { preview: false });
  } catch (error) {
    notOpened(error instanceof Error ? error.message : String(error));
  }
}

/**
 * `SWCraft: Import Requirements`: pick a requirements file, run `swcraft import
 * requirements --dry-run` over it, show its report in the import tab with **Write** and
 * **Cancel**, and apply the import (the same argv without `--dry-run`) only on **Write**.
 *
 * Every judgement is the CLI's; the extension reads no model file. Between the file and the
 * dry run the flow asks for the mapping file (remembered in a setting) and the delivery's
 * version (prefilled with the file's `current_version`); both runs carry the same answers,
 * and the project and executable are resolved once. Each run has its own indicator; a
 * second start while one is busy is refused. The applied run is a second run over the
 * model as it is then, never a replay of the preview.
 */
async function importRequirementsCommand(outputChannel: vscode.OutputChannel): Promise<void> {
  const resolved = resolveManifestOrNotify();
  if (resolved === undefined) {
    return;
  }
  const { workspaceRoot, manifestRelPath } = resolved;
  const executable = resolveSwcraftExecutable();
  const commandId = "swcraft.importRequirements";
  await importRequirementsFlow(
    requirementsImportHost(
      importFlowHost(commandId, executable, workspaceRoot, {
        filters: { JSON: ["json"] },
        title: "SWCraft: select a requirements file to import",
      }),
      workspaceRoot,
      path.dirname(resolved.manifestPath),
      outputChannel,
    ),
    (file, options) =>
      importRun(commandId, executable, workspaceRoot, outputChannel, (report) =>
        buildImportRequirementsArgv(manifestRelPath, file, { ...options, report }),
      ),
  );
}

/**
 * The extension's **one setting writer**: *value* written into *setting* in the workspace
 * folder's `.vscode/settings.json`, or cleared for `undefined`. Each caller reads its setting
 * back as `inspect().workspaceFolderValue` alone, so a user or workspace-file value is
 * neither used nor left behind. A failed write is one channel line, never a throw.
 */
async function rememberWorkspaceSetting(
  workspaceRoot: string,
  setting: string,
  value: string | undefined,
  outputChannel: vscode.OutputChannel,
): Promise<void> {
  try {
    await vscode.workspace
      .getConfiguration(undefined, vscode.Uri.file(workspaceRoot))
      .update(setting, value, vscode.ConfigurationTarget.WorkspaceFolder);
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error);
    outputChannel.appendLine(`SWCraft: the setting ${setting} was not written: ${detail}`);
  }
}

/**
 * The requirements import's questions over VS Code, beside *base*'s tab, runs and record.
 * The mapping setting is read in the workspace folder alone. The needs file is read once
 * as UTF-8; an unreadable one is an empty box, and the CLI's refusal stays the authority.
 */
function requirementsImportHost(
  base: ImportFlowHost,
  workspaceRoot: string,
  projectDir: string,
  outputChannel: vscode.OutputChannel,
): RequirementsImportHost {
  const configuration = vscode.workspace.getConfiguration(
    undefined,
    vscode.Uri.file(workspaceRoot),
  );
  return {
    ...base,
    projectDir,
    rememberedMapping: () =>
      configuration.inspect<unknown>(REQUIREMENTS_MAPPING_SETTING)?.workspaceFolderValue,
    pickMapping: (items) =>
      Promise.resolve(
        vscode.window.showQuickPick(items, { ...MAPPING_PICK_TEXT, ignoreFocusOut: true }),
      ),
    pickMappingFile: async () => {
      const dialog = await vscode.window.showOpenDialog({
        canSelectFiles: true,
        canSelectFolders: false,
        canSelectMany: false,
        defaultUri: vscode.Uri.file(projectDir),
        filters: { YAML: ["yaml", "yml"] },
        openLabel: "Use this mapping",
        title: MAPPING_DIALOG_TITLE,
      });
      return dialog === undefined || dialog.length === 0 ? undefined : dialog[0].fsPath;
    },
    rememberMapping: (value) =>
      rememberWorkspaceSetting(workspaceRoot, REQUIREMENTS_MAPPING_SETTING, value, outputChannel),
    readFile: async (file) => {
      try {
        return await fs.promises.readFile(file, { encoding: "utf8" });
      } catch {
        return undefined;
      }
    },
    askVersion: (options) =>
      Promise.resolve(vscode.window.showInputBox({ ...options, ignoreFocusOut: true })),
  };
}

/**
 * `SWCraft: Import Component`: pick a component ARXML, run `swcraft import component
 * --dry-run` over it, show its report in the import tab with **Write** and **Cancel**, and
 * apply the import (the last dry run's argv without `--dry-run`) only on the apply button.
 * An exit 3 offers **Show Update**, which runs the `--dry-run --update` preview into the
 * same tab.
 *
 * What is written, kept and refused is the CLI's; the extension reads no model file. The
 * tab's buttons are disabled while a run it started is busy, and a second start is refused.
 * It does not save open editors, remember a file, wait for a Build or Check Model, pass
 * `--replace-access-lists`, or run `check-model` itself (only the tab's button does).
 */
async function importComponentCommand(outputChannel: vscode.OutputChannel): Promise<void> {
  const resolved = resolveManifestOrNotify();
  if (resolved === undefined) {
    return;
  }
  const { workspaceRoot, manifestRelPath } = resolved;
  const executable = resolveSwcraftExecutable();
  const commandId = "swcraft.importComponent";
  await importComponentFlow(
    importFlowHost(commandId, executable, workspaceRoot, {
      filters: { ARXML: ["arxml"] },
      title: "SWCraft: select a component ARXML to import",
    }),
    (file, options) =>
      importRun(commandId, executable, workspaceRoot, outputChannel, (report) =>
        buildImportComponentArgv(manifestRelPath, file, { ...options, report }),
      ),
  );
}

/**
 * Absolute path to `dist/onboarding-assets/` of the running install (copied there by
 * `esbuild.mjs`); a `.vsix` install's path is only known at runtime.
 */
function onboardingAssetsDir(context: vscode.ExtensionContext): string {
  return path.join(context.extensionPath, "dist", "onboarding-assets");
}

async function onboardProjectCommand(
  context: vscode.ExtensionContext,
  outputChannel: vscode.OutputChannel,
  mcpProvider: SwcraftMcpServerProvider,
): Promise<void> {
  // The picker opens on the first workspace folder, the one `resolveManifest` reads.
  const workspaceRoot = getWorkspaceRoot();
  const picked = await vscode.window.showOpenDialog({
    canSelectFiles: false,
    canSelectFolders: true,
    canSelectMany: false,
    defaultUri: workspaceRoot === undefined ? undefined : vscode.Uri.file(workspaceRoot),
    openLabel: "Onboard this folder",
    title: "SWCraft: select a project folder to onboard",
  });
  if (picked === undefined || picked.length === 0) {
    return;
  }
  const targetDir = picked[0].fsPath;
  // A folder a parent's manifest covers, or one that holds a project below it, is refused:
  // onboarding never makes a nested project.
  const workspaceFolders = (vscode.workspace.workspaceFolders ?? []).map(
    (folder) => folder.uri.fsPath,
  );
  const refusal = onboardingRefusal(targetDir, workspaceFolders);
  if (refusal !== undefined) {
    outputChannel.appendLine(refusal);
    recordCommandStatus("swcraft.onboardProject", { kind: "failure", message: refusal });
    void vscode.window.showErrorMessage(refusal);
    return;
  }
  // Which chat clients to set up: the row whose `preselectInHosts` names this host's
  // `uriScheme` is pre-picked, else the table's default. A cancelled or empty pick writes
  // nothing.
  let table: ClientTable;
  try {
    table = loadClientTable(onboardingAssetsDir(context));
  } catch (err) {
    const message = clientTableUnreadableMessage(err);
    outputChannel.appendLine(message);
    recordCommandStatus("swcraft.onboardProject", { kind: "failure", message });
    void vscode.window.showErrorMessage(message);
    return;
  }
  const pickedClients = await vscode.window.showQuickPick(
    clientPickItems(table, vscode.env.uriScheme),
    {
      canPickMany: true,
      title: CLIENT_PICK_TITLE,
    },
  );
  const chosen = chosenClients(pickedClients);
  if (chosen === undefined) {
    if (pickedClients !== undefined) {
      void vscode.window.showInformationMessage(NO_CLIENT_CHOSEN_MESSAGE);
    }
    return;
  }
  await withRunIndicator("swcraft.onboardProject", describeRunning("swcraft.onboardProject"), () =>
    runOnboarding(targetDir, table, chosen, context, outputChannel, mcpProvider),
  );
}

/** `onboardProjectCommand`'s body once a folder and the clients have been chosen: the part that spawns. */
async function runOnboarding(
  targetDir: string,
  table: ClientTable,
  chosen: readonly string[],
  context: vscode.ExtensionContext,
  outputChannel: vscode.OutputChannel,
  mcpProvider: SwcraftMcpServerProvider,
): Promise<void> {
  // The machine's installation: the folder being onboarded does not choose the executable.
  const executable = resolveSwcraftExecutable();
  outputChannel.show(true);
  outputChannel.appendLine(`SWCraft: onboarding ${targetDir}`);

  const hasExistingManifest = fs.existsSync(path.join(targetDir, MANIFEST_FILENAME));
  const steps = planOnboardingSteps(targetDir, hasExistingManifest);

  // Preflight: confirm the executable is there and runs before writing anything; a
  // `.vsix`-only install does not guarantee it.
  const preflightResult = await spawnCli(executable, steps.preflightArgv, targetDir, outputChannel);
  const preflightOutcome = mapPreflightResult(
    preflightResult.exitCode,
    preflightResult.spawnFailed,
  );
  if (preflightOutcome.kind === "cli-not-found") {
    const message = preflightNotFoundMessage(executable);
    outputChannel.appendLine(message);
    recordCommandStatus("swcraft.onboardProject", { kind: "failure", message });
    void vscode.window.showErrorMessage(message);
    return;
  }

  // Read before `swcraft init`, which writes `.vscode/settings.json` itself: that file is
  // then completed with the companion keys rather than kept.
  const companionAbsentBeforeInit =
    steps.initArgv !== undefined &&
    !entryExists(path.join(targetDir, ...table.copilotCompanionSettings.file.split("/")));

  // `swcraft init` only for a target with no `swcraft.yaml` yet.
  if (steps.initArgv !== undefined) {
    const initResult = await spawnCli(executable, steps.initArgv, targetDir, outputChannel);
    const initOutcome = mapExitCodeToOutcome(initResult.exitCode);
    if (initOutcome.kind !== "success") {
      const message =
        outcomeMessage("Onboard Project (swcraft init)", initOutcome) ??
        "SWCraft: onboarding failed during swcraft init.";
      recordCommandStatus("swcraft.onboardProject", { kind: "failure", message });
      void vscode.window.showErrorMessage(message);
      return;
    }
  }

  const files = clientFilesFor(table, chosen);
  let plan: OnboardingPlan;
  let kept: string[];
  let companion: CompanionOutcome | undefined;
  try {
    // Asset copies only: the chosen clients' files (a file that is not SWCraft's, or behind
    // a symbolic link, is kept and named), then the companion settings file when absent.
    plan = planOnboarding(targetDir, files);
    kept = applyOnboardingPlan(plan, onboardingAssetsDir(context));
    companion = companionApplies(table, chosen)
      ? applyCompanionSettings(targetDir, table.copilotCompanionSettings, companionAbsentBeforeInit)
      : undefined;
  } catch (err) {
    const message = onboardingApplyFailedMessage(err);
    outputChannel.appendLine(message);
    recordCommandStatus("swcraft.onboardProject", { kind: "failure", message });
    void vscode.window.showErrorMessage(message);
    return;
  }

  recordCommandStatus("swcraft.onboardProject", { kind: "success" });
  // The folder just gained a `swcraft.yaml`, which may turn "no server" into "one server";
  // nothing else would make VS Code ask again.
  mcpProvider.refresh();
  // A folder with no manifest watched nothing, and gaining one is no folder change.
  armModelWatch();
  void updateResourcesAvailable();
  // The closing lines: the clients, what was written, each kept file with what to add by
  // hand, the table's clash warnings, and a line when the Codex hook lands outside a git
  // repository (its host runs it only in one).
  const codexHook = table.clients.find((client) => client.name === CODEX_CLIENT)?.hook ?? null;
  const closing = onboardedMessages(
    targetDir,
    {
      clientLabels: clientLabelsFor(table, chosen),
      written: [
        ...writtenFiles(plan, kept),
        ...(companion?.kind === "written" ? [table.copilotCompanionSettings.file] : []),
      ],
      kept,
      companionKept:
        companion?.kind === "kept"
          ? keptCompanionMessage(targetDir, table.copilotCompanionSettings, companion.destPath)
          : undefined,
      warnings: conflictWarnings(table, chosen),
      outsideGit:
        codexHook !== null && files.includes(codexHook)
          ? codexHookOutsideGitMessage(targetDir)
          : undefined,
    },
    table,
  );
  for (const line of closing) {
    outputChannel.appendLine(line);
  }
  void vscode.window.showInformationMessage(closing.join(" "));
  // The client files may have just been written: check the `swcraft` their chats start.
  void checkClientSwcraftOnPath(context, targetDir).catch(logClientSwcraftFailure);
}

/**
 * `SWCraft: Build`: run, one after another, the six commands `moon run :build` runs in the
 * template (`clean-outputs`, `check-model`, `generate arxml`, `generate diagrams`, `generate
 * needs`, `validate`), so every artifact comes out of the editor with no moon and no terminal.
 *
 * Steps, order and stop rule are `buildBuildSteps()`/`runBuildSteps()`'s; the first step
 * that does not exit 0 ends the run. **One status is recorded for the whole run**, and one
 * warning names the step that failed. A run that stops before the generation leaves
 * `views_root` absent; when the views were generated, the diagrams' tab re-reads its page
 * and the diagram lenses are redrawn. The whole run goes through `viewsRootWrites`, and it
 * refuses while a toolchain run is in flight. What it generated is recorded for the panel,
 * stamped before the run waited its turn: the views once `generate diagrams` succeeded, the
 * ARXML only when the whole run did.
 *
 * **How the run ends** (`buildRunEnding()`): a failure at `check-model` moves the focus to
 * Problems; a run whose `validate` step ran, passed or failed, opens the build report its
 * `--report` wrote; any other failure ends at the warning. A green run then refreshes the
 * Resources view outside the queue, so a failed refresh leaves Build green.
 */
async function buildCommand(
  outputChannel: vscode.OutputChannel,
  collection: vscode.DiagnosticCollection,
  artifactPath: string,
): Promise<void> {
  const resolved = resolveManifestOrNotify();
  if (resolved === undefined) {
    return;
  }
  if (toolchainRunInFlight) {
    const refusal = toolchainRunningRefusal("Build");
    recordCommandStatus("swcraft.build", { kind: "failure", message: refusal });
    void vscode.window.showErrorMessage(refusal);
    return;
  }
  const { workspaceRoot, manifestRelPath } = resolved;
  const executable = resolveSwcraftExecutable();
  outputChannel.show(true);

  const startedAt = Date.now();
  const steps = buildBuildSteps(manifestRelPath);
  // The `validate` step's stdout, kept for the report's preview.
  let validateStdout: string | undefined;
  const result = await withRunIndicator("swcraft.build", BUILD_WAITING_TEXT, (report) =>
    viewsRootWrites.run(() =>
      runBuildSteps(steps, async (step, index) => {
        report(describeBuildProgress(steps, index));
        // The `check-model` step fills the Problems panel; its exit code still decides.
        if (step.feedsDiagnostics === true) {
          return checkModelRun(
            executable,
            workspaceRoot,
            step.argv,
            manifestRelPath,
            artifactPath,
            outputChannel,
            collection,
          );
        }
        const run = await spawnCli(executable, [...step.argv], workspaceRoot, outputChannel);
        if (step.writesReport === true) {
          validateStdout = run.stdout;
        }
        return run;
      }),
    ),
  );
  artifactTracker?.recordBuild(workspaceRoot, startedAt, buildRunGenerations(result));
  if (buildRunGenerations(result).views) {
    // The pages were rewritten: the open tab re-reads its page, and the diagram lenses
    // read the new index whether or not a tab is open.
    diagramsTab.reloadShown();
    refreshDiagramLenses();
  }

  const message = buildRunMessage(result);
  recordCommandStatus(
    "swcraft.build",
    message === undefined ? { kind: "success" } : { kind: "failure", message },
  );
  if (message !== undefined && result.kind === "failure") {
    notifyWithHelp("warning", message, helpTopicForRun(result.run));
  }
  const ending = buildRunEnding(result, validateStdout);
  if (ending.kind === "problems") {
    void vscode.commands.executeCommand(PROBLEMS_FOCUS_COMMAND);
  } else if (ending.kind === "report") {
    await previewGeneratedReport(ending.stdout, workspaceRoot, outputChannel);
  }
  if (buildRunRefreshesResources(result)) {
    await refreshResourcesAfterBuild(resolved, outputChannel);
  }
}

/**
 * `SWCraft: Diagrams`: generate the diagram pages into `views_root` and show `index.html`
 * in the diagrams' tab (`DiagramsTab`, a webview reading those files).
 *
 * In order, each refusal returning before the next step:
 *
 * 1. **The written directory**: a `views_root` the CLI refuses is refused here, in its sentence.
 * 2. **The generation**: `swcraft generate diagrams`; a refusal opens nothing and keeps the
 *    last good pages.
 * 3. **The record**: the views' generation, stamped before it was queued; a refusal records
 *    nothing.
 * 4. **The open**: the window's one diagrams' tab; a refused page ends the press as
 *    `page-refused`. `options.regenerate` (the tab's **Generate**) skips the direct open.
 *
 * **The direct open**: after the `views_root` check, a *diagram* CodeLens press opens
 * `<id>.html` with **no generation** when the page is listed now, no `views_root` run is in
 * flight, the views read `current`, and they were generated **in this window** with no
 * watched event since (`ArtifactRecord.opensDirectly()`, an in-memory fact, since the
 * persisted `current` comes from mtimes). It runs no CLI and records nothing. The panel's
 * item and the palette pass no `{view}`, so they always regenerate.
 *
 * What the direct open does **not** reach:
 *
 * - output written outside this window (the watch covers `model_root/**` and the manifest):
 *   after a press here, a terminal or another window may rewrite `views_root`, and the lens
 *   opens whatever page is listed now;
 * - a terminal `clean-outputs` or a deleted page (unlisted, so the full press runs);
 * - an event the watcher delivers late or never (`files.watcherExclude`, a dropped burst, a
 *   network file system, a click before a save's delivery): the lens can open an old page;
 * - a toolchain installed or updated in this window (the **Diagrams** item renews);
 * - an input the generator reads outside the two watched patterns, if any;
 * - a second window on the folder (its model edits arrive as events, its writes do not);
 * - an unsaved buffer, which the generation does not read either.
 *
 * The press does **not** reach a Remote SSH, WSL or Codespaces window (unmeasured, like the
 * whole press on Windows).
 *
 * Every path past the manifest ends in `finishDiagramsPress()` and records a status (an
 * item with none reads as broken); `reportDiagramsPress()` decides status and notification.
 */
async function diagramsCommand(
  outputChannel: vscode.OutputChannel,
  arg?: unknown,
  options: { regenerate?: boolean } = {},
): Promise<void> {
  const regenerate = options.regenerate === true;
  const resolved = resolveManifestOrNotify();
  if (resolved === undefined) {
    return;
  }
  const { workspaceRoot, manifestPath, manifestRelPath } = resolved;
  if (toolchainRunInFlight) {
    finishDiagramsPress({ kind: "refused", message: toolchainRunningRefusal("Diagrams") });
    return;
  }

  const views = resolveViewsRoot(manifestPath);
  if (!views.ok) {
    finishDiagramsPress({ kind: "refused", message: views.reason });
    return;
  }

  // A *diagram* CodeLens names one element's page, accepted only when the page is listed
  // now; otherwise the press opens the index. The tab's Generate asks by shape alone, so a
  // page removed under the tab is shown again once generated.
  const requested = diagramsArgument(arg, listViewsRoot(views.path), { regenerate });
  // The direct open: no CLI, no channel, no indicator, no record.
  const onDisk = requested === undefined ? undefined : artifactsOnDisk(workspaceRoot);
  if (
    !regenerate &&
    requested !== undefined &&
    onDisk !== undefined &&
    !viewsRootWrites.busy &&
    artifactTracker?.opensDirectly("views", workspaceRoot, onDisk) === true
  ) {
    const view = { id: requested, found: true };
    const shown = diagramsTab.show(views.path, { view: requested, fragment: "" });
    finishDiagramsPress(pressOfShow(shown, view));
    return;
  }
  outputChannel.show(true);
  const startedAt = Date.now();
  const generated = await withRunIndicator("swcraft.diagrams", DIAGRAMS_PROGRESS_TEXT, () =>
    runGenerateDiagrams(workspaceRoot, manifestRelPath, outputChannel),
  );
  const message = outcomeMessage("Diagrams", mapExitCodeToOutcome(generated.exitCode));
  if (message !== undefined) {
    // A failed run may have left `views_root` partly rewritten: the next lens click
    // regenerates rather than opening it.
    artifactTracker?.forgetWindowGeneration("views", workspaceRoot);
    finishDiagramsPress({ kind: "generation-refused", message }, generated);
    return;
  }

  artifactTracker?.recordGeneration("views", workspaceRoot, startedAt);
  // The requested page if the generation still wrote it, else the index — one open either way.
  const view =
    requested === undefined
      ? undefined
      : { id: requested, found: fs.existsSync(diagramsViewPath(views.path, requested)) };
  // A page this generation wrote that the tab still refuses as another release's is the
  // toolchain's release, not a stale page: `refusalLine()` says so.
  const shown = diagramsTab.show(
    views.path,
    { view: view?.found === true ? view.id : undefined, fragment: "" },
    { justGenerated: true },
  );
  finishDiagramsPress(pressOfShow(shown, view));
}

/** The entries directly under *viewsRoot*, or `undefined` when it cannot be listed. */
function listViewsRoot(viewsRoot: string): string[] | undefined {
  try {
    return fs.readdirSync(viewsRoot);
  } catch {
    return undefined;
  }
}

// --- Show in Diagram: the index, the command, the lens's refresh ---------------

/** The index last read: its file's real path and stamp, its reading, and the real paths of its names per model root. */
let diagramIndexCache:
  | {
      path: string;
      stamp: string;
      index: DiagramIndex;
      realPaths?: { modelRoot: string; byReal: Map<string, string[]> };
    }
  | undefined;

/**
 * The diagrams' index of *viewsRoot*, read only through `resolveViewsFile()` and cached by
 * mtime and size; `undefined` when there is no index file to read.
 */
function diagramIndexEntry(viewsRoot: string): typeof diagramIndexCache {
  const file = resolveViewsFile(viewsRoot, DIAGRAMS_INDEX_FILENAME);
  if (file.kind === "refused") {
    return undefined;
  }
  let stamp: string;
  try {
    const stat = fs.statSync(file.path);
    stamp = `${stat.mtimeMs}:${stat.size}`;
  } catch {
    return undefined;
  }
  if (diagramIndexCache?.path === file.path && diagramIndexCache.stamp === stamp) {
    return diagramIndexCache;
  }
  let html: string;
  try {
    html = fs.readFileSync(file.path, { encoding: "utf8" });
  } catch {
    return undefined;
  }
  diagramIndexCache = { path: file.path, stamp, index: readDiagramIndex(html) };
  return diagramIndexCache;
}

/**
 * The storage name the index lists for the file at *documentPath*: lexically first, then by
 * real path (computed once per index and model root, only when needed).
 */
function indexedNameOf(
  entry: NonNullable<typeof diagramIndexCache>,
  modelRoot: string,
  documentPath: string,
): string | undefined {
  const elements = entry.index.elements;
  if (elements.kind !== "ok") {
    return undefined;
  }
  let documentReal: string | undefined;
  try {
    documentReal = fs.realpathSync.native(documentPath);
  } catch {
    documentReal = undefined;
  }
  return indexedStorageName(
    elements.value,
    lexicalStorageName(modelRoot, documentPath),
    documentReal,
    () => {
      if (entry.realPaths?.modelRoot !== modelRoot) {
        entry.realPaths = { modelRoot, byReal: realPathIndex(modelRoot, elements.value.keys()) };
      }
      return entry.realPaths.byReal;
    },
  );
}

/** Fired after every press and every tab reload: the *diagram* lenses read the index again. */
let diagramLensChanges: vscode.EventEmitter<void> | undefined;

/**
 * The reference table `registerNavigation()` loaded, for the diagrams' tab and Show in
 * Diagram; `undefined` until it registers or when it could not be read (the identity line,
 * or line 0, then).
 */
let navigationReferences: References | undefined;

/** Ask VS Code to redraw the lenses: the index may have moved. */
function refreshDiagramLenses(): void {
  diagramLensChanges?.fire();
}

/**
 * The element the active editor shows, for Show in Diagram: its file, its name (the
 * outline's root, else the file's stem) and the member at the cursor. `undefined` when the
 * active editor is no model file of a layer under *modelRoot*.
 */
function activeDiagramElement(
  modelRoot: string,
): { documentPath: string; name: string; member: string | null } | undefined {
  const editor = vscode.window.activeTextEditor;
  if (editor === undefined || editor.document.uri.scheme !== "file") {
    return undefined;
  }
  const documentPath = editor.document.uri.fsPath;
  const refs = navigationReferences;
  const layer = refs === undefined ? undefined : layerOf(documentPath, modelRoot, refs.layers);
  if (refs === undefined || layer === undefined) {
    return undefined;
  }
  const outline = outlineOf(refs, editor.document.getText(), layer);
  return {
    documentPath,
    name: outlineRoot(outline)?.name ?? path.basename(documentPath, path.extname(documentPath)),
    member: memberAtLine(outline, editor.selection.active.line),
  };
}

/** Show in Diagram's two notices, each with **Generate Diagrams**, which runs the press. */
async function offerGenerateDiagrams(line: string): Promise<void> {
  const choice = await vscode.window.showInformationMessage(line, GENERATE_DIAGRAMS_BUTTON);
  if (choice === GENERATE_DIAGRAMS_BUTTON) {
    await vscode.commands.executeCommand("swcraft.diagrams");
  }
}

/**
 * `SWCraft: Show in Diagram`: select and centre the element (the active editor's, with the
 * member at the cursor, or the lens's `{file, name}`) on a view of the diagrams' index that
 * draws it: a jump, never a search.
 *
 * The view is `chooseView()`'s: the one the tab shows when it draws the element, else the
 * only one, else a quick pick. An element no view draws, and a missing or empty index, each
 * get a notice offering **Generate Diagrams**.
 *
 * It runs no CLI and generates nothing: it reads `index.html` as last generated, out of
 * date or not. Does not reach an unsaved buffer's new member, or views a terminal
 * generation wrote while the tab showed an older page.
 */
async function showInDiagramCommand(arg?: unknown): Promise<void> {
  const resolved = resolveManifestOrNotify();
  if (resolved === undefined) {
    return;
  }
  const views = resolveViewsRoot(resolved.manifestPath);
  if (!views.ok) {
    void vscode.window.showWarningMessage(views.reason);
    return;
  }
  const modelRoot = currentModelRoot();
  if (modelRoot === undefined) {
    void vscode.window.showWarningMessage(SHOW_IN_DIAGRAM_NO_MODEL_ROOT_LINE);
    return;
  }
  const fromLens = showInDiagramArgument(arg);
  const active = fromLens === undefined ? activeDiagramElement(modelRoot) : undefined;
  if (fromLens === undefined && active === undefined) {
    void vscode.window.showInformationMessage(SHOW_IN_DIAGRAM_NO_ELEMENT_LINE);
    return;
  }
  const name = fromLens?.name ?? active?.name ?? "";
  const member = active?.member ?? null;
  const entry = diagramIndexEntry(views.path);
  if (entry === undefined || entry.index.elements.kind !== "ok") {
    await offerGenerateDiagrams(noDiagramsLine(name));
    return;
  }
  const file =
    fromLens?.file ??
    (active === undefined ? undefined : indexedNameOf(entry, modelRoot, active.documentPath));
  const drawing =
    file === undefined ? [] : viewsForElement(entry.index.elements.value, file, member);
  const choice = chooseView(drawing, diagramsTab.shownViewIn(views.path));
  if (choice.kind === "none" || file === undefined) {
    await offerGenerateDiagrams(notOnAnyDiagramLine(name));
    return;
  }
  let view: string | undefined;
  if (choice.kind === "view") {
    view = choice.view;
  } else {
    const picked = await vscode.window.showQuickPick(
      pickItems(choice.views, entry.index.headings),
      { placeHolder: pickPlaceholder(name, choice.views.length) },
    );
    view = picked?.view;
  }
  if (view === undefined) {
    return;
  }
  diagramsTab.show(
    views.path,
    { view, fragment: "" },
    { reveal: { file, member, name: revealName(name, member) } },
  );
}

// --- The diagrams' tab: the adapter over diagramsTab.ts -------------------------

/** What the diagrams' tab needs from the window, handed over by `registerCommands()`. */
interface DiagramsTabEffects {
  /** The shared SWCraft channel: every refused link, open or page is one line there. */
  channel: vscode.OutputChannel;
  /** The installed extension's directory: `schemas/` holds the page script's hash and the reference table. */
  extensionPath: string;
  /** Does the artifact tracker read the views *out of date* now? */
  outOfDate(): boolean;
  /** The tab's **Generate**: the press, regenerating always, asked for *arg*'s page. */
  generate(arg: { view: string } | undefined): void;
  /** This extension's release, named by the line a page the press just generated is refused with. */
  extensionVersion: string;
}

/**
 * The diagrams' tab: **one** `WebviewPanel` per window, reused and revealed by a press, a
 * link and a reveal, closed only by the user. Every decision is `diagramsTab.ts`'s; this
 * holds the panel and a `TabState` and carries out what the pure functions answer:
 *
 * - **a page** is read only as `resolveViewsFile()` resolves it (a regular file inside the
 *   real `views_root`) and set as `webviewDocument()`'s rewrite with a fresh nonce; a
 *   refused or missing page is `refusalDocument()`'s line with **Generate**, plus one channel
 *   line naming the file name only;
 * - **the order of a show** is `planShow()`'s: a reveal on the page already shown reveals
 *   the panel, then posts the reveal (no timer);
 * - **the page's messages** go through `parseTabMessage()`: `navigate` follows a link over
 *   the current listing (a page in the tab, an `.svg` or `report.txt` beside), `open` opens a
 *   model file the page's allowlist names, `reload`, `back`, `forward`, and `generate`;
 * - **the out-of-date line** follows the tracker while a page is shown.
 *
 * Does **not** reach the webview's own behaviour, or a window reload, which closes the
 * panel (no serializer). The panel is `retainContextWhenHidden`, so a post reaches a hidden
 * page.
 */
class DiagramsTab {
  private effects: DiagramsTabEffects | undefined;
  private panel: vscode.WebviewPanel | undefined;
  private readonly state = new TabState();
  private scriptHash: string | undefined;

  /** Hand over the window's effects; `registerCommands()` calls it once. */
  configure(effects: DiagramsTabEffects): void {
    this.effects = effects;
  }

  /** The view the open panel shows from *viewsRoot*; `undefined` for the index or none. */
  shownViewIn(viewsRoot: string): string | undefined {
    return this.panel === undefined ? undefined : this.state.shownViewIn(viewsRoot);
  }

  /**
   * Show *entry* of *viewsRoot* in the panel, with `options.reveal` applied, in
   * `planShow()`'s order; a page from another `views_root` is always rendered, and the
   * history starts again there. `options.justGenerated` is the press's own generation.
   * Answers shown, or the refusal line the tab shows instead.
   */
  show(
    viewsRoot: string,
    entry: TabEntry,
    options: { reveal?: RevealRequest; justGenerated?: boolean } = {},
  ): TabShow {
    const { reveal, justGenerated = false } = options;
    const shown = this.panel === undefined ? undefined : this.state.shownIn(viewsRoot);
    let answer: TabShow = { kind: "shown" };
    for (const step of planShow(shown, { entry, reveal })) {
      switch (step.kind) {
        case "render":
          this.state.visit(viewsRoot, step.entry);
          answer = this.render(step.reveal, justGenerated);
          break;
        case "reveal-panel":
          this.panel?.reveal(undefined, false);
          break;
        case "post-reveal":
          void this.panel?.webview.postMessage({ type: "reveal", ...step.reveal });
          break;
      }
    }
    return answer;
  }

  /** Re-read the page the open panel shows: a Build regenerated the views. */
  reloadShown(): void {
    if (this.panel !== undefined && this.state.history.current !== undefined) {
      this.render(null);
    }
  }

  /** Tell the page shown when the tracker's reading of the views moved; nothing when it did not. */
  syncOutOfDate(): void {
    if (this.panel === undefined || this.state.shownOutOfDate === undefined) {
      return;
    }
    const outOfDate = this.effects?.outOfDate() ?? false;
    if (this.state.outOfDateMoved(outOfDate)) {
      void this.panel.webview.postMessage({ type: OUT_OF_DATE_MESSAGE, outOfDate });
    }
  }

  private log(line: string): void {
    this.effects?.channel.appendLine(line);
  }

  /**
   * The page script's hash this release shipped (`schemas/diagram-page-script.json`),
   * cached; re-read once on an `other-release` refusal, since a same-version .vsix
   * installed over a running copy replaces the file without restarting the host.
   */
  private pageScriptHash(): string | undefined {
    if (this.scriptHash === undefined) {
      this.scriptHash = this.readPageScriptHash();
    }
    return this.scriptHash;
  }

  /** The hash file read now, or `undefined` when it cannot be read or is not its shape. */
  private readPageScriptHash(): string | undefined {
    if (this.effects === undefined) {
      return undefined;
    }
    try {
      return parsePageScriptHash(
        fs.readFileSync(path.join(this.effects.extensionPath, "schemas", PAGE_SCRIPT_HASH_FILE), {
          encoding: "utf8",
        }),
      );
    } catch {
      return undefined;
    }
  }

  private ensurePanel(): vscode.WebviewPanel {
    if (this.panel !== undefined) {
      return this.panel;
    }
    const panel = vscode.window.createWebviewPanel(
      DIAGRAMS_TAB_VIEW_TYPE,
      DIAGRAMS_TAB_TITLE,
      { viewColumn: vscode.ViewColumn.Active, preserveFocus: false },
      {
        enableScripts: true,
        localResourceRoots: [],
        enableCommandUris: false,
        enableFindWidget: false,
        retainContextWhenHidden: true,
      },
    );
    panel.webview.onDidReceiveMessage((message) => this.onMessage(message));
    panel.onDidChangeViewState(() => this.state.panelMovedTo(panel.viewColumn));
    panel.onDidDispose(() => {
      this.panel = undefined;
      this.state.disposed();
    });
    this.panel = panel;
    return panel;
  }

  /**
   * Set the page of the history's current place, read afresh, with *reveal* in its state;
   * *justGenerated* when the press has just generated it.
   */
  private render(reveal: RevealRequest | null, justGenerated = false): TabShow {
    const entry = this.state.history.current;
    const viewsRoot = this.state.viewsRoot;
    const panel = this.ensurePanel();
    const nonce = newNonce();
    if (entry === undefined || viewsRoot === undefined) {
      return this.refuse(panel, nonce, MISSING_PAGE_LINE, "no page is recorded");
    }
    const name = entry.view === undefined ? DIAGRAMS_INDEX_FILENAME : viewPageName(entry.view);
    const file = resolveViewsFile(viewsRoot, name);
    let html: string | undefined;
    if (file.kind === "file") {
      try {
        html = fs.readFileSync(file.path, { encoding: "utf8" });
      } catch {
        html = undefined;
      }
    }
    if (html === undefined) {
      const reason = file.kind === "refused" ? file.reason : "the file cannot be read";
      return this.refuse(panel, nonce, MISSING_PAGE_LINE, `${name}: ${reason}`);
    }
    const noScriptHash = (): TabShow =>
      this.refuse(panel, nonce, NO_SCRIPT_HASH_LINE, `${name}: no page script hash`);
    const scriptHash = this.pageScriptHash();
    if (scriptHash === undefined) {
      return noScriptHash();
    }
    const outOfDate = this.effects?.outOfDate() ?? false;
    const rendered = webviewDocumentRereading(
      html,
      {
        nonce,
        scriptHash,
        state: {
          fragment: entry.fragment,
          reveal,
          canBack: this.state.history.canBack,
          canForward: this.state.history.canForward,
          outOfDate,
        },
      },
      () => this.readPageScriptHash(),
    );
    this.scriptHash = rendered.scriptHash;
    const document = rendered.document;
    if (document === undefined) {
      return noScriptHash();
    }
    if (document.kind === "refused") {
      const line = refusalLine(document, justGenerated, this.effects?.extensionVersion ?? "");
      return this.refuse(panel, nonce, line, `${name}: ${document.reason}`);
    }
    this.state.showedPage(modelAllowlist(parseModelBlock(html)), outOfDate);
    panel.title = tabTitle(html, entry.view);
    panel.webview.html = document.html;
    return { kind: "shown" };
  }

  /** Show the tab's own one-line page with **Generate** in place of a page, and log why. */
  private refuse(panel: vscode.WebviewPanel, nonce: string, line: string, detail: string): TabShow {
    this.state.showedRefusal();
    panel.title = DIAGRAMS_TAB_TITLE;
    panel.webview.html = refusalDocument(nonce, line);
    this.log(`SWCraft: the diagrams' tab did not show a page (${channelQuote(detail)}).`);
    return { kind: "refused", line };
  }

  private onMessage(raw: unknown): void {
    const message = parseTabMessage(raw);
    if (message === undefined) {
      return;
    }
    switch (message.type) {
      case "navigate":
        void this.navigate(message.href);
        return;
      case "open":
        void this.open(message.file, message.member);
        return;
      case "reload":
        // The index may have been rewritten outside this window: the lenses read it again.
        this.reloadShown();
        refreshDiagramLenses();
        return;
      case "back":
        if (this.state.history.back() !== undefined) {
          this.render(null);
        }
        return;
      case "forward":
        if (this.state.history.forward() !== undefined) {
          this.render(null);
        }
        return;
      case "generate":
        this.effects?.generate(generateArgument(this.state.history.current));
        return;
    }
  }

  /** A listed page in the tab, an `.svg` or `report.txt` beside; anything else one line. */
  private async navigate(href: string): Promise<void> {
    const viewsRoot = this.state.viewsRoot;
    if (viewsRoot === undefined) {
      return;
    }
    // Only a listed regular file inside the real views_root; anything else is ignored with
    // one line and the page stays.
    const target = followLink(href, viewsRoot, listViewsRoot(viewsRoot));
    if (target.kind === "refused") {
      this.log(
        `SWCraft: a diagram link was not followed: ${target.reason} ("${channelQuote(href)}").`,
      );
      return;
    }
    if (target.kind === "page") {
      this.show(viewsRoot, { view: target.view, fragment: target.fragment });
      return;
    }
    await vscode.commands.executeCommand("vscode.open", vscode.Uri.file(target.path), {
      viewColumn: this.openColumn(),
    });
  }

  /** An element the page's own block lists, under the real `model_root`, opened beside. */
  private async open(file: string, member: string | null): Promise<void> {
    const modelRoot = currentModelRoot();
    if (modelRoot === undefined) {
      this.log(
        "SWCraft: a diagram element was not opened: no SWCraft project is open in this window.",
      );
      return;
    }
    const accepted = acceptOpen(file, member, this.state.allowlist, modelRoot);
    if (accepted.kind === "refused") {
      this.log(
        `SWCraft: a diagram element was not opened: ${accepted.reason} ("${channelQuote(file)}").`,
      );
      return;
    }
    const column = await openModelFileAt(
      { realPath: accepted.path, storageName: file, modelRoot, member },
      this.openColumn(),
      this.effects?.channel,
    );
    if (column !== undefined) {
      this.state.editorColumn = column;
    }
  }

  /** Beside the panel where it stands now, never over it. */
  private openColumn(): vscode.ViewColumn {
    const column = besideColumn(this.state.editorColumn, this.panel?.viewColumn);
    return column === "beside" ? vscode.ViewColumn.Beside : column;
  }
}

/** The window's diagrams' tab; `registerCommands()` hands it its effects. */
const diagramsTab = new DiagramsTab();

/**
 * Open a model file in *column* at *member*'s line: *realPath* is the resolved file opened;
 * its layer is read from *storageName*'s place under *modelRoot*, the line from the opened
 * text's outline (the identity line for a `null` or unknown member, or with no reference
 * table). Answers the editor's column, or `undefined` when the file could not be opened
 * (one line on *channel*).
 */
async function openModelFileAt(
  target: { realPath: string; storageName: string; modelRoot: string; member: string | null },
  column: vscode.ViewColumn,
  channel: vscode.OutputChannel | undefined,
): Promise<vscode.ViewColumn | undefined> {
  let document: vscode.TextDocument;
  try {
    document = await vscode.workspace.openTextDocument(vscode.Uri.file(target.realPath));
  } catch {
    channel?.appendLine(
      `SWCraft: ${channelQuote(target.storageName)} could not be opened in the editor.`,
    );
    return undefined;
  }
  const refs = navigationReferences;
  const layer =
    refs === undefined
      ? undefined
      : layerOf(
          path.join(target.modelRoot, ...target.storageName.split("/")),
          target.modelRoot,
          refs.layers,
        );
  const line =
    refs === undefined || layer === undefined
      ? 0
      : memberLineOf(outlineOf(refs, document.getText(), layer), target.member);
  const position = new vscode.Position(line, 0);
  const editor = await vscode.window.showTextDocument(document, {
    viewColumn: column,
    preserveFocus: false,
    selection: new vscode.Range(position, position),
  });
  editor.revealRange(
    new vscode.Range(position, position),
    vscode.TextEditorRevealType.InCenterIfOutsideViewport,
  );
  return editor.viewColumn;
}

/**
 * The model route of the extension's `UriHandler`:
 * `vscode://<extension id>/model?path=<storage name>` opens that file of the project open
 * in this window at its identity line. A refused link or unresolved file is one channel
 * line; a window with no project gets one notification, which quotes nothing from the link.
 */
async function handleModelUri(
  uri: vscode.Uri,
  extensionId: string,
  channel: vscode.OutputChannel,
): Promise<void> {
  const modelRoot = currentModelRoot();
  const action = modelLinkAction(uri, extensionId, modelRoot);
  switch (action.kind) {
    case "notify":
      void vscode.window.showWarningMessage(action.message);
      return;
    case "log":
      channel.appendLine(action.line);
      return;
    case "open":
      if (modelRoot !== undefined) {
        await openModelFileAt(
          { realPath: action.path, storageName: action.name, modelRoot, member: null },
          vscode.ViewColumn.Active,
          channel,
        );
      }
      return;
  }
}

/**
 * Record `reportDiagramsPress()`'s status under `swcraft.diagrams` and raise its
 * notification. *generation* is the `generate diagrams` run a refused generation came
 * from, for the help topic.
 */
function finishDiagramsPress(press: DiagramsPress, generation?: RunOutput): void {
  // Every press may have rewritten the index the diagram lenses read.
  refreshDiagramLenses();
  const report = reportDiagramsPress(press);
  recordCommandStatus("swcraft.diagrams", report.status);
  const notification = report.notification;
  if (notification === undefined) {
    return;
  }
  if (notification.severity === "information") {
    void vscode.window.showInformationMessage(notification.message);
    return;
  }
  notifyWithHelp(
    notification.severity,
    notification.message,
    generation === undefined ? undefined : helpTopicForRun(generation),
  );
}

// --- Test Tables: the generated tables in a read-only tab ----------------------
//
// The thin host of `testTablesTab.ts`. The tab reads the bundle the CLI wrote and writes
// nothing; a press writes the bundle (by the CLI) and, at the pick, the one setting.

/** The window's *Test Tables* tab, while it is open: one per window, revealed and re-rendered by every press. */
let testTablesPanel: vscode.WebviewPanel | undefined;

/** True while a press runs, from its start to its render: a second press then runs nothing. */
let testTablesPressBusy = false;

/**
 * The annotations pick over VS Code: the quick pick, the folder dialog and the setting,
 * read as the workspace folder's own value.
 */
function testTablesAnnotationsHost(
  workspaceRoot: string,
  projectDir: string,
  outputChannel: vscode.OutputChannel,
): TestTablesAnnotationsHost {
  return {
    projectDir,
    // Read afresh: a configuration object is a snapshot, and the tab's top line reads the
    // value the pick has just written.
    rememberedAnnotations: () =>
      vscode.workspace
        .getConfiguration(undefined, vscode.Uri.file(workspaceRoot))
        .inspect<unknown>(TEST_TABLES_ANNOTATIONS_SETTING)?.workspaceFolderValue,
    pickAnnotations: (items) =>
      Promise.resolve(
        vscode.window.showQuickPick(items, { ...ANNOTATIONS_PICK_TEXT, ignoreFocusOut: true }),
      ),
    pickAnnotationsFolder: async () => {
      const dialog = await vscode.window.showOpenDialog({
        canSelectFiles: false,
        canSelectFolders: true,
        canSelectMany: false,
        defaultUri: vscode.Uri.file(projectDir),
        openLabel: "Use this folder",
        title: ANNOTATIONS_DIALOG_TITLE,
      });
      return dialog === undefined || dialog.length === 0 ? undefined : dialog[0].fsPath;
    },
    rememberAnnotations: (value) =>
      rememberWorkspaceSetting(
        workspaceRoot,
        TEST_TABLES_ANNOTATIONS_SETTING,
        value,
        outputChannel,
      ),
  };
}

/**
 * Show *html* in the window's *Test Tables* tab: the open one revealed, or a new one (no
 * `retainContextWhenHidden`). Its two messages run the same press
 * (`acceptTestTablesMessage()` fails closed on anything else).
 */
function showTestTablesTab(html: string, outputChannel: vscode.OutputChannel): void {
  if (testTablesPanel === undefined) {
    const panel = vscode.window.createWebviewPanel(
      TEST_TABLES_VIEW_TYPE,
      TEST_TABLES_TAB_TITLE,
      { viewColumn: vscode.ViewColumn.Active, preserveFocus: false },
      { enableScripts: true, localResourceRoots: [] },
    );
    panel.webview.onDidReceiveMessage((message: unknown) => {
      const accepted = acceptTestTablesMessage(message);
      if (accepted === undefined) {
        return;
      }
      void testTablesCommand(outputChannel, { change: accepted === "changeAnnotations" });
    });
    panel.onDidDispose(() => {
      if (testTablesPanel === panel) {
        testTablesPanel = undefined;
      }
    });
    testTablesPanel = panel;
  } else {
    testTablesPanel.reveal(undefined, false);
  }
  testTablesPanel.webview.html = html;
}

/**
 * `SWCraft: Test Tables`: one press regenerates the tables from the current model and
 * opens them in the read-only tab. In order: the annotations folder, asked when never
 * answered or when *change* (Esc ends the press before any run); `swcraft generate
 * test-tables [--annotations=<dir>]` inside the run indicator; the outcome on the tree item;
 * then the tab, the tables from `<dist_root>/test-tables/` on exit 0, the refusal page
 * otherwise. The tab's **Generate** and **Change…** run this same press.
 *
 * One press at a time: two runs would rename into one directory, and two picks would stack.
 * A press that ends without a page posts `TEST_TABLES_READY_MESSAGE` to an open tab, which
 * re-enables its buttons. Not gated against a toolchain run (it writes under `dist_root`
 * alone), and nothing watches the bundle: a terminal run shows at the next press.
 */
async function testTablesCommand(
  outputChannel: vscode.OutputChannel,
  options: { change: boolean } = { change: false },
): Promise<void> {
  if (testTablesPressBusy) {
    return;
  }
  testTablesPressBusy = true;
  let rendered = false;
  try {
    const resolved = resolveManifestOrNotify();
    if (resolved === undefined) {
      return;
    }
    const { workspaceRoot, manifestPath, manifestRelPath } = resolved;
    const commandId = "swcraft.testTables";
    const host = testTablesAnnotationsHost(
      workspaceRoot,
      path.dirname(manifestPath),
      outputChannel,
    );
    const chosen = await askTestTablesAnnotations(host, options);
    if (chosen === undefined) {
      return;
    }
    const executable = resolveSwcraftExecutable();
    outputChannel.show(true);
    const run = await withRunIndicator(commandId, describeRunning(commandId), () =>
      spawnCli(
        executable,
        testTablesArgv(manifestRelPath, chosen.annotations),
        workspaceRoot,
        outputChannel,
      ),
    );
    const generated = recordCliOutcome(commandId, SPAWNING_COMMAND_LABELS[commandId], run);
    const nonce = newNonce();
    if (!generated) {
      showTestTablesTab(testTablesRefusalPage(nonce), outputChannel);
      rendered = true;
      return;
    }
    // The CLI accepted the manifest, so a path refusal is unlikely; if the reader refuses
    // anyway, nothing is read from where the paths point.
    const paths = readManifestPaths(manifestPath);
    const read: TestTablesRead =
      paths.refusal === undefined
        ? readTestTables(paths.resolved.dist_root)
        : { kind: "unreadable", reason: "the manifest's paths are refused" };
    const html =
      read.kind === "tables"
        ? testTablesPage(read, { nonce, annotations: host.rememberedAnnotations() })
        : read.kind === "absent"
          ? testTablesEmptyPage(nonce)
          : testTablesUnreadablePage(nonce);
    if (read.kind === "unreadable") {
      outputChannel.appendLine(`SWCraft: the test tables cannot be read: ${read.reason}`);
    }
    showTestTablesTab(html, outputChannel);
    rendered = true;
  } finally {
    testTablesPressBusy = false;
    if (!rendered && testTablesPanel !== undefined) {
      void testTablesPanel.webview.postMessage(TEST_TABLES_READY_MESSAGE);
    }
  }
}

/**
 * The watch over a folder's model: `<model_root>/**` and `swcraft.yaml`, each event noted by
 * the artifact tracker and nothing else; **no process is started**, however often. The
 * patterns and their rebuilding when a manifest save moves `model_root` are `ModelWatch`'s.
 * A folder with no manifest watches nothing until it is armed again.
 */
function watchModelFolder(folder: string): WatchDisposable {
  const resolution = resolveManifest(folder);
  if (!resolution.ok) {
    return { dispose: () => {} };
  }
  return new ModelWatch(
    resolution.manifestPath,
    resolveModelRoot,
    createPatternWatcher,
    (changedPath) => noteModelChange(changedPath),
  );
}

/** The window's one model watch, set by `registerCommands()`. */
let modelWatchSession: ModelWatchSession | undefined;
/** Moved by every `armModelWatch()`: a change stamp taken before it is dropped (`noteModelChange()`). */
let modelWatchGeneration = 0;

/**
 * Arm the window's watch for the folder open in it, if any, and read that folder's newest
 * change afresh. Activation, a workspace-folder change and a successful *Onboard Project*
 * call it (writing `swcraft.yaml` in place is no workspace-folder change).
 */
function armModelWatch(): void {
  modelWatchGeneration += 1;
  const folder = getWorkspaceRoot();
  if (modelWatchSession !== undefined && folder !== undefined) {
    modelWatchSession.armFor(folder);
    void artifactTracker?.rescan(folder);
  }
  artifactsChanged();
}

/**
 * One `vscode.workspace.createFileSystemWatcher` over *pattern*, every create,
 * change and delete under it reported with its path, disposed as one.
 */
function createPatternWatcher(
  pattern: WatchPattern,
  onEvent: (changedPath: string) => void,
): vscode.Disposable {
  const watcher = vscode.workspace.createFileSystemWatcher(
    new vscode.RelativePattern(vscode.Uri.file(pattern.base), pattern.glob),
  );
  return vscode.Disposable.from(
    watcher.onDidCreate((uri) => onEvent(uri.fsPath)),
    watcher.onDidChange((uri) => onEvent(uri.fsPath)),
    watcher.onDidDelete((uri) => onEvent(uri.fsPath)),
    watcher,
  );
}

/**
 * `swcraft generate diagrams` for *manifestRelPath*, into the shared channel, waiting its
 * turn in `viewsRootWrites`. The one spelling of the run a `Diagrams` press makes.
 */
async function runGenerateDiagrams(
  workspaceRoot: string,
  manifestRelPath: string,
  outputChannel: vscode.OutputChannel,
): Promise<SpawnCliResult> {
  return viewsRootWrites.run(() =>
    spawnCli(
      resolveSwcraftExecutable(),
      buildGenerateDiagramsArgv(manifestRelPath),
      workspaceRoot,
      outputChannel,
    ),
  );
}

/**
 * The spawner behind `SWCraft: Open Guide`: `spawnCli()`'s executable and environment, but
 * no `close` handling: `swcraft docs` serves until stopped, and the child's life is
 * `GuideSession`'s.
 */
function spawnGuideChild(context: GuideSpawnContext, argv: readonly string[]) {
  return spawn(context.executable, [...argv], { cwd: context.cwd, env: cliEnv() });
}

/**
 * `SWCraft: Open Guide`: serve the user guide that shipped with this install and show it in
 * an editor tab beside the work (`openGuidePage()`).
 *
 * The CLI serves (`swcraft docs --no-browser`, so it opens nothing) and VS Code opens. The
 * URL is mapped through `vscode.env.asExternalUri`, which forwards the loopback port to the
 * user's machine in a Remote SSH, WSL or Codespaces window. When the open or the mapping
 * throws, the raw address goes to `vscode.env.openExternal`.
 *
 * **Needs no project**: `swcraft docs` takes no `--project`, so a reader with no folder
 * open gets the guide.
 */
async function openGuideCommand(
  session: GuideSession,
  guideChannel: vscode.OutputChannel,
  arg?: unknown,
): Promise<void> {
  // The `guide` lens passes `{page, anchor}`; anything else opens the front page.
  const target = guideCommandArg(arg);
  await openGuidePage(session, guideChannel, target?.page ?? "", target?.anchor);
}

/**
 * Serve the guide (the running one, or `swcraft docs --no-browser` started now) and show
 * *page* (a path under the served root, `""` for the root) at *anchor* in the window's one
 * guide tab. Open Guide, a rule link and *Learn more* all open the guide here. When it
 * cannot be served, an error notification says why and nothing opens.
 *
 * **One guide tab per window** (`guideTabOpen()`). Where VS Code has its integrated browser
 * (1.143.1 does, and there `simpleBrowser.show` opens a new tab per call), its own open
 * command runs with `reuseUrlFilter` set to the served guide's root (`guideTabFilter()`), so
 * a tab already on THIS guide server is navigated and revealed. Elsewhere
 * `simpleBrowser.show` reuses its one view. No tab is ever closed. Does not reach:
 * - a guide tab moved to another window (a new one opens here);
 * - a second guide tab the user opened by hand (VS Code reuses the first match);
 * - a tab on another site or an earlier port is never matched (on the Simple Browser path
 *   its one view is reused whatever it shows); `guideTabFilter()` names the addresses its
 *   filter does not reach;
 * - the filter is an option of an internal workbench command read from VS Code 1.143.1's
 *   code, not a documented API: a VS Code that drops it opens a new tab per call;
 * - the `openExternal` fallback opens a browser tab each time;
 * - the tab outlives the server and is restored pointing at a dead port: it cannot be
 *   closed, since `vscode.window.tabGroups` gives an integrated browser tab no `input`.
 */
async function openGuidePage(
  session: GuideSession,
  guideChannel: vscode.OutputChannel,
  page: string,
  anchor?: string,
): Promise<void> {
  const workspaceRoot = getWorkspaceRoot();
  const executable = resolveSwcraftExecutable();
  // The workspace folder when there is one: `cli.main()` loads a `.env` from the working
  // directory, and a project's `SWCRAFT_GUIDE_SITE` there is meant for this command. Never
  // the extension host's own cwd.
  const cwd = workspaceRoot ?? os.homedir();

  // Everything this run writes goes to the GUIDE channel alone (`serveGuideTo()`), appended
  // and not shown on success: the run ends in the guide's page.
  const outcome = await serveGuideTo(session, { executable, cwd }, guideChannel);
  // No status is recorded: Open Guide has no tree item. The notification is the answer.
  if (outcome.kind !== "serving") {
    // Only the invocation that ran the start notifies: a waiter on it (`shared`) would
    // repeat it, and a `stopped` session belongs to a window going away.
    if (outcome.kind !== "stopped" && outcome.shared !== true) {
      void vscode.window.showErrorMessage(outcome.message);
    }
    return;
  }
  const address = guidePageUrl(outcome.url, page, anchor);

  try {
    // Inside the `try`: an unmappable address takes the same fallback, never an unhandled
    // rejection.
    const external = await vscode.env.asExternalUri(vscode.Uri.parse(address));
    const open = guideTabOpen(
      await vscode.commands.getCommands(true),
      external.toString(true),
      page,
    );
    await vscode.commands.executeCommand(open.command, ...open.args);
    return;
  } catch {
    // No integrated browser and the Simple Browser disabled, the open command threw, or the
    // address could not be mapped: fall back to the user's own browser.
  }
  // The fallback: `openExternal` forwards the loopback port itself.
  const opened = await vscode.env.openExternal(vscode.Uri.parse(address));
  if (!opened) {
    // Advisory, as in the CLI: a host with no registered handler reports failure while the
    // address works, so the run is a success and the user is handed the address.
    void vscode.window.showInformationMessage(
      `SWCraft: the guide is being served at ${address} — this window could not open a browser for it.`,
    );
  }
}

/**
 * The extension's `UriHandler` for guide links: a rule link in the Problems panel, or a
 * guide section's link in the assistant's chat answer. A link of this scheme can be raised
 * by any web page, so the URI is validated first (`parseHelpUri()`), and a refused one is
 * one line in the guide channel and opens nothing.
 */
async function handleHelpUri(uri: vscode.Uri, extensionId: string): Promise<void> {
  const guide = helpGuide;
  if (guide === undefined) {
    return;
  }
  const request = parseHelpUri(uri, extensionId);
  if (request.kind === "refused") {
    guide.channel.appendLine(`SWCraft: a guide link was not opened: ${request.reason}.`);
    return;
  }
  await openGuidePage(guide.session, guide.channel, request.page, request.anchor);
}

/**
 * How long a `check-model` run waits for its rules' links before it publishes; it bounds a
 * host whose mapping is slow, so the Problems entries are never held for it.
 */
const RULE_LINK_WAIT_MS = 1_000;

/**
 * The links of *rules* for one run: each rule's link
 * (`<uriScheme>://<extension id>/guide?page=reference/rules/&anchor=<rule>`) through
 * `vscode.env.asExternalUri`, used unaltered. On the desktop the result is kept for the
 * window; otherwise it serves this run only (`RuleLinkResolver`, which states what that does
 * not reach). The run waits at most {@link RULE_LINK_WAIT_MS}; a rule unresolved then is its
 * plain name. A call past `RULE_LINK_CALL_LIMIT_MS`, or a rejected one, is one line on
 * *channel* and is asked again by the next run.
 */
async function resolveRuleLinks(
  rules: readonly string[],
  channel: vscode.OutputChannel,
): Promise<ReadonlyMap<string, vscode.Uri>> {
  if (ruleLinkResolver === undefined) {
    return new Map();
  }
  return ruleLinkResolver.resolve(rules, RULE_LINK_WAIT_MS, (line) => channel.appendLine(line));
}

/**
 * Ask the resolved executable for its version: `swcraft --version`, captured.
 *
 * Run from the home directory: this asks about the installation, and `cli.main()` loads a
 * `.env` from the working directory. Only run when the executable exists: the health check
 * has a better sentence for a launch failure. **Bounded**, because a hanging executable would hang
 * *Check Editor Tooling*, the surface meant to diagnose it, and in *Update Toolchain* would
 * keep the unpacked wheels in the temp directory.
 */
async function probeToolchainVersion(executable: string): Promise<VersionProbe> {
  const result = await spawnCliCapture(
    executable,
    ["--version"],
    os.homedir(),
    VERSION_PROBE_TIMEOUT_MS,
  );
  return {
    ok: !result.spawnFailed && result.exitCode === 0,
    output: `${result.stdout}${result.stderr}`,
  };
}

/**
 * The workstation toolchain's health, read fresh: the one health reading that runs a
 * process (the version probe, skipped when the executable is absent). Shared by *Check
 * Editor Tooling* and the toolchain commands' after-run report, so the two cannot disagree
 * about the same machine.
 */
async function readToolchainHealth(
  context: vscode.ExtensionContext,
  setting: string | undefined,
  reading: ToolchainReading = readToolchainOnce(),
): Promise<ResourceHealth> {
  const { platform } = reading;
  const resolution = resolveSwcraft(setting, process.env, os.homedir(), platform, fs.existsSync);
  return checkToolchain({
    resolution,
    extensionVersion: extensionVersion(context),
    version: resolution.exists ? await reading.probe(resolution.executable) : undefined,
    pathResolution: reading.pathResolution,
    platform,
  });
}

/**
 * One run's reading of the bare `swcraft` on `PATH` and its version probe, shared by the
 * toolchain's and the model guard's rows so `PATH` is resolved once and each executable is
 * spawned once: two readings could disagree within one report.
 */
interface ToolchainReading {
  platform: Platform;
  /** Where a bare `swcraft` resolves on this window's `PATH`, or `undefined`. */
  pathResolution: string | undefined;
  probe: (executable: string) => Promise<VersionProbe>;
}

/** A fresh {@link ToolchainReading}: `PATH` resolved now, nothing probed yet. */
function readToolchainOnce(): ToolchainReading {
  const platform = currentPlatform();
  return {
    platform,
    pathResolution: resolveOnPath(process.env, platform, fs.existsSync),
    // A call, not the bare function: the trust test's text call graph follows a call, so
    // a value would hide this command's spawn from it.
    probe: probeOncePerPath((executable) => probeToolchainVersion(executable), platform),
  };
}

/**
 * The version of the installed toolchain, or `undefined` when it is absent, did not start,
 * or printed no version. *Update Toolchain*'s `decideUpdate()` treats all three alike: an
 * installation that will not start is what that command repairs.
 */
async function installedToolchainVersion(setting: string | undefined): Promise<string | undefined> {
  const resolution = resolveSwcraft(
    setting,
    process.env,
    os.homedir(),
    currentPlatform(),
    fs.existsSync,
  );
  if (!resolution.exists) {
    return undefined;
  }
  const probe = await probeToolchainVersion(resolution.executable);
  return probe.ok ? parseSwcraftVersion(probe.output) : undefined;
}

/**
 * `SWCraft: Check Editor Tooling`: report which declared pack members this host has, the
 * health of the workstation toolchain, and whether the bundled JSON Schemas shipped, and
 * say in the report what it could not see.
 *
 * The pack is **this extension's own `packageJSON.extensionPack`**, never a retyped list.
 * `vscode.extensions.all` lists enabled extensions only, so "missing" and "disabled" are
 * one verdict, and the report says so.
 */
async function checkEditorToolingCommand(
  context: vscode.ExtensionContext,
  outputChannel: vscode.OutputChannel,
): Promise<void> {
  const declaredPack: string[] = Array.isArray(context.extension.packageJSON?.extensionPack)
    ? (context.extension.packageJSON.extensionPack as string[])
    : [];
  const installed: InstalledExtension[] = vscode.extensions.all.map((extension) => {
    const manifest: { version?: unknown; engines?: { vscode?: unknown } } =
      extension.packageJSON ?? {};
    return {
      id: extension.id,
      version: typeof manifest.version === "string" ? manifest.version : undefined,
      engineRange:
        typeof manifest.engines?.vscode === "string" ? manifest.engines.vscode : undefined,
    };
  });

  const schemasDir = path.join(context.extensionPath, "schemas");
  // The required inventory is the installed manifest's own `contributes.yamlValidation`,
  // never a retyped list that would go stale silently.
  const declaredSchemas = declaredSchemaFiles(
    context.extension.packageJSON?.contributes?.yamlValidation,
  );
  // The two rows that spawn, under the indicator: the toolchain's (the same
  // `readToolchainHealth()` the toolchain commands report with) and the model guard's (the
  // `swcraft` on PATH the project's hook runs), sharing one `readToolchainOnce()`.
  const [toolchain, modelGuard] = await withRunIndicator(
    "swcraft.checkEditorTooling",
    describeRunning("swcraft.checkEditorTooling"),
    async () => {
      const reading = readToolchainOnce();
      return [
        await readToolchainHealth(context, executableSetting(), reading),
        await readModelGuardHealth(context, reading),
      ] as const;
    },
  );
  const input: HealthInput = {
    vscodeVersion: vscode.version,
    declaredPack,
    installed,
    toolchain,
    schemas: checkBundledSchemas(readSchemaInventory(schemasDir), schemasDir, declaredSchemas),
    agentProfile: readAgentProfileHealth(context),
    modelGuard,
  };

  outputChannel.show(true);
  outputChannel.appendLine("");
  for (const line of buildHealthReport(input)) {
    outputChannel.appendLine(line);
  }

  // The check succeeded whatever it found: a finding travels in the notification, since a
  // report nobody opens would be silent. No status: this command has no tree item.
  const problems = countProblems(input);
  if (problems > 0) {
    void vscode.window.showWarningMessage(
      `SWCraft: ${summaryLine(problems)} See the SWCraft output channel for the report.`,
    );
    return;
  }
  void vscode.window.showInformationMessage(
    `SWCraft: no problems found. ${NOT_VALIDATION_EVIDENCE}`,
  );
}

/** A file's bytes, or `undefined` when it cannot be read (absent included). */
function readBytesOrUndefined(file: string): Uint8Array | undefined {
  try {
    return fs.readFileSync(file);
  } catch {
    return undefined;
  }
}

/**
 * The folder of the SWCraft project in this window (the first workspace folder's root
 * manifest, as every project command resolves it), or `undefined` when there is none.
 */
function swcraftProjectDir(): string | undefined {
  const root = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
  const manifest = root === undefined ? undefined : findManifest(root);
  return manifest === undefined ? undefined : path.dirname(manifest);
}

/**
 * The "Copilot agent profile" row: the project's profile against this install's bundled
 * one, or `undefined` (no row) when the window holds no SWCraft project.
 */
function readAgentProfileHealth(context: vscode.ExtensionContext): ResourceHealth | undefined {
  const projectDir = swcraftProjectDir();
  if (projectDir === undefined) {
    return undefined;
  }
  const copilot = copilotUnused(context, projectDir);
  if (copilot !== undefined) {
    return copilotNotUsed("Copilot agent profile", copilot);
  }
  return checkAgentProfile(
    readBytesOrUndefined(agentProfilePath(projectDir)),
    readBytesOrUndefined(path.join(onboardingAssetsDir(context), AGENT_ASSET_NAME)),
  );
}

/**
 * The "Copilot model guard" row: the project's `.github/hooks/swcraft.json` against the
 * bundled hook, and the `swcraft` the hook runs (a bare name on this window's `PATH`)
 * probed for its version through *reading*. `undefined` (no row) when the window holds no
 * SWCraft project. Every decision is `checkModelGuard()`'s.
 */
async function readModelGuardHealth(
  context: vscode.ExtensionContext,
  reading: ToolchainReading,
): Promise<ResourceHealth | undefined> {
  const projectDir = swcraftProjectDir();
  if (projectDir === undefined) {
    return undefined;
  }
  const copilot = copilotUnused(context, projectDir);
  if (copilot !== undefined) {
    return copilotNotUsed("Copilot model guard", copilot);
  }
  const { pathResolution } = reading;
  return checkModelGuard({
    hook: readBytesOrUndefined(hookPath(projectDir)),
    bundled: readBytesOrUndefined(path.join(onboardingAssetsDir(context), HOOK_ASSET_NAME)),
    pathResolution,
    pathVersion: pathResolution === undefined ? undefined : await reading.probe(pathResolution),
    extensionVersion: extensionVersion(context),
  });
}

/** The client whose hook its host runs only inside a git repository (`codexHookOutsideGitMessage()`). */
const CODEX_CLIENT = "codex";

/** Whether the entry *relative* (POSIX, from *projectDir*) is present; an unreadable one counts as present. */
function projectEntryPresent(projectDir: string, relative: string): boolean {
  try {
    return entryExists(path.join(projectDir, ...relative.split("/")));
  } catch {
    return true;
  }
}

/**
 * Whether the project in *projectDir* uses Copilot: `undefined` when it does, or when the
 * bundled table cannot be read (Copilot's rows are then judged without it); otherwise the
 * row's label, for `copilotNotUsed()`.
 */
function copilotUnused(context: vscode.ExtensionContext, projectDir: string): string | undefined {
  let table: ClientTable;
  try {
    table = loadClientTable(onboardingAssetsDir(context));
  } catch {
    return undefined;
  }
  return clientInUse(table, AGENT_PROFILE_NOTICE_CLIENT, (relative) =>
    projectEntryPresent(projectDir, relative),
  )
    ? undefined
    : clientNamed(table, AGENT_PROFILE_NOTICE_CLIENT).label;
}

/**
 * Whether *candidate* is a file this process may execute, the rule a bare-name lookup
 * applies. `X_OK` reads as existence on Windows, so no platform check is needed.
 */
function isExecutableFile(candidate: string): boolean {
  try {
    if (!fs.statSync(candidate).isFile()) {
      return false;
    }
    fs.accessSync(candidate, fs.constants.X_OK);
    return true;
  } catch {
    return false;
  }
}

/** *candidate*'s text as UTF-8, or `undefined` when it is absent or cannot be read. */
function readTextOrUndefined(candidate: string): string | undefined {
  try {
    return fs.readFileSync(candidate, "utf8");
  } catch {
    return undefined;
  }
}

/**
 * The warning `clientSwcraft.ts` decides for *projectDir*, from this window's readings: the
 * client server files present, the executable SWCraft spawns, where a bare `swcraft`
 * resolves on the extension host's `PATH` (the one the Codex and Claude Code extensions
 * inherit), and each `--version` by the bounded probe.
 */
async function readClientSwcraftWarning(projectDir: string): Promise<string | undefined> {
  const clientFiles = presentClientServerFiles(
    projectDir,
    loadClientTable(clientTableDir),
    readTextOrUndefined,
  );
  if (clientFiles.length === 0) {
    return undefined;
  }
  const platform = currentPlatform();
  const finding = await checkClientSwcraft({
    clientFiles,
    own: swcraftResolution(),
    onPath: resolveBareCommand(SWCRAFT_COMMAND_NAME, process.env, platform, isExecutableFile),
    platform,
    realpath: (candidate) => fs.realpathSync(candidate),
    // A call, not the bare function: the trust test's text call graph follows a call.
    probe: (executable) => probeToolchainVersion(executable),
  });
  return clientSwcraftWarning(finding, clientFiles);
}

/** Where `readClientSwcraftWarning()` reads the client table, set before the trigger runs. */
let clientTableDir = "";

/** The window's one trigger: at most one warning, however often it is called. */
const clientSwcraftOnPath = clientSwcraftTrigger(readClientSwcraftWarning, (message) => {
  void vscode.window.showWarningMessage(message);
});

/**
 * Check that the `swcraft` Codex's and Claude Code's chats will start in *projectDir* is the
 * one SWCraft uses: on activation and after *Onboard Project* wrote the client files, never
 * on a save. A folder without `.codex/config.toml` or `.mcp.json` naming a bare `swcraft` is
 * not checked; what it does not reach is in `clientSwcraft.ts`'s header.
 */
export function checkClientSwcraftOnPath(
  context: vscode.ExtensionContext,
  projectDir: string | undefined = swcraftProjectDir(),
): Promise<void> {
  clientTableDir = onboardingAssetsDir(context);
  return projectDir === undefined ? Promise.resolve() : clientSwcraftOnPath(projectDir);
}

/** A failed reading is logged, never raised: the check is advice, not a gate. */
export function logClientSwcraftFailure(error: unknown): void {
  console.error(
    `SWCraft: the check of the swcraft on PATH failed (${
      error instanceof Error ? error.message : String(error)
    }).`,
  );
}

/**
 * The activation-time notice: in a SWCraft project with no SWCraft Architect agent, offer
 * to add it. Every decision is `onboarding.ts`'s; this reads the facts and acts on the
 * answer. *Add* writes the profile and, only where absent, Copilot's instructions and
 * model-guard hook (no other client's file, no companion settings), never overwrites a file
 * and never runs `swcraft init`. *Not for this workspace* is remembered in `workspaceState`;
 * a notice dismissed without a choice is offered again next time.
 */
export async function offerAgentProfile(context: vscode.ExtensionContext): Promise<void> {
  const projectDir = swcraftProjectDir();
  const profileExists = projectDir !== undefined && fs.existsSync(agentProfilePath(projectDir));
  const dismissed = context.workspaceState.get<boolean>(AGENT_PROFILE_NOTICE_DISMISSED_KEY, false);
  // A table that cannot be read leaves the notice as without one; *Add* then reports the
  // failure.
  const copilotInUse = projectDir !== undefined && copilotUnused(context, projectDir) === undefined;
  if (
    projectDir === undefined ||
    !shouldOfferAgentProfile({ projectDir, profileExists, dismissed, copilotInUse })
  ) {
    return;
  }
  const choice = await vscode.window.showInformationMessage(
    AGENT_PROFILE_NOTICE,
    ADD_AGENT_PROFILE_CHOICE,
    DISMISS_AGENT_PROFILE_CHOICE,
  );
  const action = agentProfileNoticeAction(choice);
  if (action === "remember") {
    await context.workspaceState.update(AGENT_PROFILE_NOTICE_DISMISSED_KEY, true);
    return;
  }
  if (action !== "apply") {
    return;
  }
  let written: string[];
  try {
    written = applyAgentProfilePlan(
      planAgentProfileNotice(projectDir, loadClientTable(onboardingAssetsDir(context))),
      onboardingAssetsDir(context),
    );
  } catch (err) {
    const detail = err instanceof Error ? err.message : String(err);
    void vscode.window.showErrorMessage(
      `SWCraft: the SWCraft Architect agent could not be added (${detail}).`,
    );
    return;
  }
  const names = written.map((file) => path.relative(projectDir, file).split(path.sep).join("/"));
  void vscode.window.showInformationMessage(
    names.length === 0
      ? "SWCraft: nothing was written — the files already exist."
      : `SWCraft: added ${names.join(", ")}.`,
  );
}

/**
 * The `*.schema.json` basenames in the installed extension's `schemas/` directory, or
 * `undefined` when it could not be read: an empty and an absent directory are different
 * findings, fixed by different things.
 */
function readSchemaInventory(directory: string): string[] | undefined {
  try {
    return fs.readdirSync(directory).filter((name) => name.endsWith(".schema.json"));
  } catch {
    return undefined;
  }
}

// --- The two toolchain commands ----------------------------------------------
//
// `SWCraft: Install Toolchain` and `SWCraft: Update Toolchain` run the **archive's own**
// installer: this extension embeds neither `install.py` nor a wheel. Every decision is
// `toolchainInstall.ts`'s; here are the picker, the temporary directory, the spawn and the
// notifications.

/** Which of the two commands is running: its status key and its dialog title. */
type ToolchainCommandMode = "install" | "update";

const TOOLCHAIN_COMMANDS: Record<ToolchainCommandMode, { id: string; title: string }> = {
  install: { id: INSTALL_TOOLCHAIN_COMMAND_ID, title: "Install the SWCraft toolchain" },
  update: { id: UPDATE_TOOLCHAIN_COMMAND_ID, title: "Update the SWCraft toolchain" },
};

/** What the user pointed the command at, and how it must be read. */
interface ArchiveSource {
  source: ArchiveSourceItem["source"];
  /** The path the user picked — the one the refusal sentences name. */
  picked: string;
}

/**
 * Ask what the archive is (the zip **or** an unpacked directory) and then where it is.
 *
 * Two steps, because a dialog with both `canSelectFiles` and `canSelectFolders` degrades
 * to a directory selector on Windows and Linux, leaving the zip unpickable. `undefined`
 * means cancelled at either step: nothing spawned, no status recorded.
 */
async function pickArchiveSource(title: string): Promise<ArchiveSource | undefined> {
  // `kind` rides along on the item and comes back on the picked one.
  const items: Array<ArchiveSourceItem & vscode.QuickPickItem> = ARCHIVE_SOURCE_ITEMS.map(
    (entry) => ({ ...entry }),
  );
  const item = await vscode.window.showQuickPick(items, {
    title,
    placeHolder: "What are you pointing SWCraft at?",
    ignoreFocusOut: true,
  });
  if (item === undefined) {
    return undefined;
  }
  const zip = item.source === "zip";
  const picked = await vscode.window.showOpenDialog({
    title,
    canSelectFiles: zip,
    canSelectFolders: !zip,
    canSelectMany: false,
    filters: zip ? { "SWCraft workstation archive": ["zip"] } : undefined,
    openLabel: zip ? "Install from archive" : "Install from folder",
  });
  const chosen = picked?.[0];
  return chosen === undefined ? undefined : { source: item.source, picked: chosen.fsPath };
}

interface StreamedRun {
  exitCode: number | null;
  /** The signal that ended the child, when one did: `close` then gives `exitCode: null`. */
  signal: NodeJS.Signals | null;
  spawnFailed: boolean;
  /** Everything the child wrote, both pipes, in arrival order — what the channel showed. */
  output: string;
  /**
   * What the child wrote to **stderr** alone, where a failure's last sentence is read from:
   * two pipes have no order between them, so *output* can carry a stdout line after it.
   */
  errors: string;
}

/**
 * Spawn *argv* streaming to *outputChannel* **and** keeping what it wrote: the installer's
 * transcript belongs in the channel as it happens, and its **last sentence** is what a
 * failure notification shows. The echo is the argv itself, since this command line is how
 * a rollback is done.
 */
function spawnStreaming(
  argv: string[],
  cwd: string,
  outputChannel: vscode.OutputChannel,
): Promise<StreamedRun> {
  outputChannel.appendLine(`$ ${quoteArgvForEcho(argv)}`);
  return new Promise<StreamedRun>((resolve) => {
    let output = "";
    let errors = "";
    const child = spawn(argv[0], argv.slice(1), { cwd, env: cliEnv() });
    const collect = (chunk: Buffer): string => {
      const text = chunk.toString();
      output += text;
      outputChannel.append(text);
      return text;
    };
    child.stdout?.on("data", collect);
    child.stderr?.on("data", (chunk: Buffer) => {
      errors += collect(chunk);
    });
    child.on("error", (err) => {
      outputChannel.appendLine(`SWCraft: failed to launch ${argv[0]}: ${err.message}`);
      resolve({
        exitCode: null,
        signal: null,
        spawnFailed: true,
        output: `${output}${err.message}\n`,
        errors: `${errors}${err.message}\n`,
      });
    });
    child.on("close", (code, signal) =>
      resolve({ exitCode: code, signal, spawnFailed: false, output, errors }),
    );
  });
}

/**
 * The names an archive directory carries, as `archiveVersion()` reads them: the root's
 * entries plus `wheels/`'s. An unreadable directory yields an empty list, which
 * `archiveVersion()` refuses as not an archive.
 */
function archiveMemberNames(root: string): string[] {
  const read = (directory: string): string[] => {
    try {
      return fs.readdirSync(directory);
    } catch {
      return [];
    }
  };
  return [
    ...read(root),
    ...read(path.join(root, "wheels")).map((name) => path.posix.join("wheels", name)),
  ];
}

/**
 * `SWCraft: Install Toolchain` / `SWCraft: Update Toolchain`.
 *
 * The steps run in the order in which each can refuse without having changed anything:
 * nothing picked, no program to open a zip with, a zip that will not unpack, a directory
 * that names no version, for *Update* an archive older than the installed one, and on
 * Windows a path `cmd.exe` would re-read. Only then is the archive's own wrapper run (it
 * bootstraps the archive's interpreter, so no Python is looked for here), and only the
 * installer touches the installation.
 *
 * On success: the MCP provider's change event fires (the executable may not have existed
 * when the definitions were computed); the toolchain health is re-read into the channel, so
 * the run reports what it left behind; and *Reload Window* is offered **only** when the
 * archive's version differs from this extension's *and* the `.vsix` was not skipped
 * (without `code` there is no newer extension to reload into, and `versionChangedNotice`
 * says so).
 */
async function toolchainCommand(
  mode: ToolchainCommandMode,
  context: vscode.ExtensionContext,
  outputChannel: vscode.OutputChannel,
  mcpProvider: SwcraftMcpServerProvider,
): Promise<void> {
  const command = TOOLCHAIN_COMMANDS[mode];
  const fail = (message: string): void => {
    recordCommandStatus(command.id, { kind: "failure", message });
    void vscode.window.showErrorMessage(message);
  };

  if (toolchainRunInFlight) {
    fail(
      "SWCraft: a toolchain install is already running in this window — wait for it to finish " +
        "(the SWCraft output channel is showing it).",
    );
    return;
  }
  // A Build, its closing refresh or a press's generation runs from the installation this
  // run may replace. Checked in the same synchronous turn the flag is taken in.
  if (viewsRootWrites.busy || resourceRefreshRun !== undefined) {
    fail(viewsRootWriteRunningRefusal());
    return;
  }
  toolchainRunInFlight = true;
  let reloadPrompt: string | undefined;
  try {
    // The picker is the user's turn, so it is asked before the indicator goes up; the
    // in-flight guard is already taken, so a second press is refused meanwhile.
    const source = await pickArchiveSource(command.title);
    if (source !== undefined) {
      reloadPrompt = await withRunIndicator(command.id, describeRunning(command.id), () =>
        runToolchainCommand(mode, context, outputChannel, mcpProvider, command, fail, source),
      );
    }
    // Cancelled at either step: nothing spawned, no status recorded.
  } finally {
    toolchainRunInFlight = false;
  }

  // After the `finally`: a notification with a button resolves only when answered, and the
  // in-flight guard must not wait for the user.
  if (reloadPrompt !== undefined) {
    const choice = await vscode.window.showInformationMessage(reloadPrompt, "Reload Window");
    if (choice === "Reload Window") {
      void vscode.commands.executeCommand("workbench.action.reloadWindow");
    }
  }
}

/**
 * `toolchainCommand`'s body, with the in-flight guard taken and the archive *source*
 * chosen. Returns the *Reload Window* offer's message when the run earns one rather than
 * making the offer itself, so the caller can release the flag first.
 */
async function runToolchainCommand(
  mode: ToolchainCommandMode,
  context: vscode.ExtensionContext,
  outputChannel: vscode.OutputChannel,
  mcpProvider: SwcraftMcpServerProvider,
  command: { id: string; title: string },
  fail: (message: string) => void,
  source: ArchiveSource,
): Promise<string | undefined> {
  const platform = currentPlatform();

  outputChannel.show(true);
  // Only a zip needs a temporary directory, removed in the `finally`: the installer copies
  // what it needs out of it before it exits. Created inside the try, so a full disk or an
  // unwritable TMPDIR is a `fail()` sentence like every other refusal.
  let workDir: string | undefined;
  try {
    let archiveRoot = source.picked;
    if (source.source === "zip") {
      // Only a zip can be refused for lack of a program to open it; the folder route needs
      // nothing.
      const lookup = findZipUnpacker(
        process.env,
        platform,
        pathLookup(process.env, platform, fs.existsSync),
      );
      if ("refusal" in lookup) {
        fail(lookup.refusal);
        return;
      }
      try {
        workDir = fs.mkdtempSync(path.join(os.tmpdir(), "swcraft-archive-"));
      } catch (error) {
        fail(
          `SWCraft: a temporary directory could not be created in ${os.tmpdir()} ` +
            `(${error instanceof Error ? error.message : String(error)}) — nothing was installed. ` +
            "Free some space, or unpack the archive yourself and point this command at the folder.",
        );
        return;
      }
      const unpacked = await spawnStreaming(
        buildUnpackArgv(lookup.unpacker, source.picked, workDir),
        os.homedir(),
        outputChannel,
      );
      if (unpacked.spawnFailed || unpacked.exitCode !== 0) {
        fail(
          `SWCraft: ${source.picked} could not be unpacked — see the SWCraft output channel. ` +
            "Pick the archive zip you were handed, or the folder it was extracted into.",
        );
        return;
      }
      archiveRoot = workDir;
    }

    const version = archiveVersion(archiveMemberNames(archiveRoot), source.picked);
    if ("refusal" in version) {
      fail(version.refusal);
      return;
    }

    if (mode === "update") {
      // `undefined`, not `executableSetting()`: this command installs into
      // `toolchainHome(...)` whatever the override says, so the archive is compared with
      // what is in the home.
      const decision = decideUpdate(version.version, await installedToolchainVersion(undefined));
      if ("refusal" in decision) {
        fail(decision.refusal);
        return;
      }
    }

    const home = toolchainHome(process.env, os.homedir(), platform);
    const installer = buildInstallerArgv(archiveRoot, home, platform, process.env);
    if ("refusal" in installer) {
      fail(installer.refusal);
      return;
    }
    const run = await spawnStreaming(installer.argv, os.homedir(), outputChannel);
    // Emitted **before** the failure return: two of the three outcomes `sideloadNote`
    // answers are refusals naming a `.vsix` the `finally` removes.
    const sideload = sideloadNote(run.output, source, version.version);
    if (sideload !== undefined) {
      outputChannel.appendLine(sideload);
    }
    if (run.spawnFailed || run.exitCode !== 0) {
      fail(installerFailureMessage(run.output, run.exitCode, run.errors, run.signal));
      return;
    }

    mcpProvider.refresh();
    // The installation the run wrote into `home`, not the override.
    const health = await readToolchainHealth(context, undefined);
    outputChannel.appendLine("");
    outputChannel.appendLine(health.message);
    const shadowed = settingShadowsInstallationNote(executableSetting(), home);
    if (shadowed !== undefined) {
      outputChannel.appendLine(shadowed);
    }
    const note = pendingReloadNote(version.version, extensionVersion(context), run.output);
    if (note !== undefined) {
      outputChannel.appendLine(note);
    }
    recordCommandStatus(command.id, { kind: "success" });

    if (!offersReload(version.version, extensionVersion(context))) {
      // The version did not change, so the entry is likely already on this window's PATH.
      // The installer edits no shell file on macOS or Linux, so `pathAdviceAfterRun` reads
      // the transcript for what to advise.
      void vscode.window.showInformationMessage(
        `SWCraft: the toolchain in ${home} is ${version.version}. ` +
          pathAdviceAfterRun(platform, run.output),
      );
      return;
    }
    // The archive's `.vsix` is a different version from this window's, but only if it went
    // in: the installer skips it when `code` is not on the host's PATH (the default for a
    // GUI-launched macOS VS Code) and still exits 0. One reading of the transcript decides.
    const notice = versionChangedNotice(version.version, home, run.output);
    if (!notice.offersReloadButton) {
      void vscode.window.showWarningMessage(notice.message);
      return undefined;
    }
    return notice.message;
  } finally {
    if (workDir !== undefined) {
      // `force: true` suppresses ENOENT only: the retries wait out a Windows handle about to
      // close, the catch reports one that does not.
      try {
        fs.rmSync(workDir, { recursive: true, force: true, maxRetries: 3, retryDelay: 100 });
      } catch (error) {
        outputChannel.appendLine(
          `SWCraft: ${workDir} could not be removed (${error instanceof Error ? error.message : String(error)}) — ` +
            "the toolchain run itself is unaffected; remove the directory yourself.",
        );
      }
    }
  }
}

/**
 * Register the SWCraft commands and their two OutputChannels. Called once from
 * `activate()`. `packageCommands.test.ts` checks this registers exactly `package.json`'s
 * `contributes.commands`.
 *
 * *mcpProvider* is `registerMcpServerProvider()`'s: the toolchain commands fire its change
 * event after a successful install.
 *
 * **Two channels, split by lifetime**: every other command finishes writing and stops, so
 * `SWCraft` reads as a transcript, while Open Guide's server logs one line per HTTP request
 * until the window closes. Do not hand the server the shared channel.
 */
export function registerCommands(
  context: vscode.ExtensionContext,
  mcpProvider: SwcraftMcpServerProvider,
): void {
  const outputChannel = vscode.window.createOutputChannel(OUTPUT_CHANNEL_NAME);
  context.subscriptions.push(outputChannel);
  const guideChannel = vscode.window.createOutputChannel(GUIDE_OUTPUT_CHANNEL_NAME);
  context.subscriptions.push(guideChannel);
  // The SWCraft entries of the Problems panel, filled by Check Model and Build's
  // `check-model` step alone; a save runs nothing.
  const diagnostics = vscode.languages.createDiagnosticCollection(DIAGNOSTIC_SOURCE);
  context.subscriptions.push(diagnostics);
  // The findings' counts in the status bar: hidden until the first publish.
  const findingsItem = vscode.window.createStatusBarItem(
    "swcraft.findings",
    vscode.StatusBarAlignment.Left,
  );
  findingsItem.name = "SWCraft Findings";
  findingsItem.command = PROBLEMS_FOCUS_COMMAND;
  findingsStatusItem = findingsItem;
  context.subscriptions.push(findingsItem);
  const artifactPath = referencesArtifactPath(context);

  // One guide server per window, pushed onto `context.subscriptions` because a spawned
  // child outlives the extension host: `dispose()` on deactivate is what stops it.
  const guideSession = new GuideSession(spawnGuideChild);
  context.subscriptions.push(guideSession);
  helpGuide = { session: guideSession, channel: guideChannel };

  // VS Code hands a URI of its own scheme naming this extension (a rule link, a guide
  // section link from the assistant, a model link) to this handler; `onUri` wakes the
  // extension for one.
  const extensionId = context.extension.id;
  ruleLinkResolver = new RuleLinkResolver<vscode.Uri>(vscode.env.uriScheme, (rule) => {
    const link = helpUri(vscode.env.uriScheme, extensionId, RULES_PAGE, rule);
    return link === undefined ? undefined : vscode.env.asExternalUri(vscode.Uri.from(link));
  });
  context.subscriptions.push(
    vscode.window.registerUriHandler({
      handleUri: (uri) =>
        uriRoute(uri) === "model"
          ? handleModelUri(uri, extensionId, outputChannel)
          : handleHelpUri(uri, extensionId),
    }),
  );

  // The diagrams' tab reads its page script's hash from this installed extension.
  diagramsTab.configure({
    channel: outputChannel,
    extensionPath: context.extensionPath,
    outOfDate: () => isOutOfDate(artifactStates(getWorkspaceRoot()).views),
    generate: (arg) => void diagramsCommand(outputChannel, arg, { regenerate: true }),
    extensionVersion: extensionVersion(context),
  });

  // The artifacts' state and the model watch whose events mark them out of date. The watch
  // is armed at activation, so a window where Diagrams was never pressed still marks the
  // ARXML; re-armed on a workspace-folder change.
  artifactTracker = new ArtifactRecord(
    context.workspaceState,
    scanNewestChangeForBoth,
    artifactsChanged,
  );
  const modelWatch = new ModelWatchSession((folder) => watchModelFolder(folder));
  modelWatchSession = modelWatch;
  armModelWatch();
  context.subscriptions.push(
    modelWatch,
    vscode.workspace.onDidChangeWorkspaceFolders(() => {
      modelWatch.dispose();
      armModelWatch();
    }),
  );

  context.subscriptions.push(
    vscode.commands.registerCommand("swcraft.build", () =>
      buildCommand(outputChannel, diagnostics, artifactPath),
    ),
    vscode.commands.registerCommand("swcraft.checkModel", () =>
      checkModelCommand(outputChannel, diagnostics, artifactPath),
    ),
    // `swcraft.diagrams` and `swcraft.impact` take one optional argument, what a CodeLens
    // passes; their other callers pass none, the panel's `TreeItem` (refused by the
    // decoder), the editor menu's `Uri` or the Impact view's row (`impactQuestion`). The
    // *impact* lens passes a second, its document's `Uri`, for the save-first check.
    vscode.commands.registerCommand("swcraft.diagrams", (arg) =>
      diagramsCommand(outputChannel, arg),
    ),
    // The editor menu and the palette pass none (the active editor's element); the
    // *diagram* lens of an element without a page of its own passes `{file, name}`.
    vscode.commands.registerCommand("swcraft.showInDiagram", (arg) => showInDiagramCommand(arg)),
    vscode.commands.registerCommand("swcraft.testTables", () => testTablesCommand(outputChannel)),
    vscode.commands.registerCommand("swcraft.exportComponent", () =>
      exportComponentCommand(outputChannel),
    ),
    vscode.commands.registerCommand("swcraft.importComponent", () =>
      importComponentCommand(outputChannel),
    ),
    vscode.commands.registerCommand("swcraft.importRequirements", () =>
      importRequirementsCommand(outputChannel),
    ),
    // One literal registration per experiment kind: a loop over
    // `EXPERIMENT_KINDS` would hide the id from every registration scan.
    vscode.commands.registerCommand("swcraft.experimentAllocation", () =>
      experimentCommand("allocation", outputChannel),
    ),
    vscode.commands.registerCommand("swcraft.experimentTrace", () =>
      experimentCommand("trace", outputChannel),
    ),
    // The Trace Links view's own commands, hidden from the palette. Each resolves the
    // project first; Apply and the listing behind the choice spawn the toolchain.
    vscode.commands.registerCommand("swcraft.traceMarkStrong", () => traceMarkStrongCommand()),
    vscode.commands.registerCommand("swcraft.traceClear", () => traceClearCommand()),
    vscode.commands.registerCommand("swcraft.traceApply", () => traceApplyCommand(outputChannel)),
    vscode.commands.registerCommand("swcraft.traceOpenReport", () => traceOpenReportCommand()),
    vscode.commands.registerCommand("swcraft.traceOpenProposals", () =>
      traceOpenProposalsCommand(),
    ),
    vscode.commands.registerCommand("swcraft.traceAccept", (node) => traceAcceptCommand(node)),
    vscode.commands.registerCommand("swcraft.traceReject", (node) => traceRejectCommand(node)),
    vscode.commands.registerCommand("swcraft.traceUndoReject", (node) =>
      traceUndoRejectCommand(node),
    ),
    vscode.commands.registerCommand("swcraft.traceOpenElement", (node) =>
      traceOpenElementCommand(node),
    ),
    vscode.commands.registerCommand("swcraft.traceChooseElement", (node) =>
      traceChooseElementCommand(node, outputChannel),
    ),
    vscode.commands.registerCommand("swcraft.traceNewSearch", () =>
      traceNewSearchCommand(outputChannel),
    ),
    vscode.commands.registerCommand("swcraft.traceChooseMode", (node) =>
      traceChooseModeCommand(node, outputChannel),
    ),
    vscode.commands.registerCommand("swcraft.traceOtherElement", (node) =>
      traceOtherElementCommand(node, outputChannel),
    ),
    vscode.commands.registerCommand("swcraft.traceOpenRequirement", (node) =>
      traceOpenRequirementCommand(node),
    ),
    vscode.commands.registerCommand("swcraft.traceRemoveChoice", (node) =>
      traceRemoveChoiceCommand(node),
    ),
    // The read-only commands: `resource-report` fills the Resources view and opens its
    // report; `change-report` (HEAD vs. the working tree) opens its report; `impact` asks
    // about the active editor's element (or the lens's or the row's) and fills the Impact
    // view.
    vscode.commands.registerCommand("swcraft.changeReport", () =>
      changeReportCommand(outputChannel),
    ),
    vscode.commands.registerCommand("swcraft.impact", (arg, lensUri) =>
      impactCommand(outputChannel, arg, lensUri),
    ),
    // The Impact view's ↻ asks the shown question again; a row's click runs nothing.
    vscode.commands.registerCommand("swcraft.impactRefresh", () =>
      impactRefreshCommand(outputChannel),
    ),
    vscode.commands.registerCommand("swcraft.impactOpen", (node) => impactOpenCommand(node)),
    vscode.commands.registerCommand("swcraft.resourceReport", () =>
      resourceReportCommand(outputChannel, { report: true }),
    ),
    // Architecture Metrics: its revisions from `swcraft.metricsSince` (HEAD when empty).
    vscode.commands.registerCommand("swcraft.metrics", () => metricsCommand(outputChannel)),
    // The Resources view's ↻ is Resource Report's run with no file and no tab.
    vscode.commands.registerCommand("swcraft.resourcesRefresh", () =>
      resourceReportCommand(outputChannel, { report: false }),
    ),
    vscode.commands.registerCommand("swcraft.resourcesOpen", (node) => resourcesOpenCommand(node)),
    vscode.commands.registerCommand("swcraft.onboardProject", () =>
      onboardProjectCommand(context, outputChannel, mcpProvider),
    ),
    // Needs `context` for the declared `extensionPack` and the schemas' directory.
    vscode.commands.registerCommand("swcraft.checkEditorTooling", () =>
      checkEditorToolingCommand(context, outputChannel),
    ),
    // The one command whose child is not expected to exit, hence `GuideSession`.
    vscode.commands.registerCommand("swcraft.openGuide", (arg) =>
      openGuideCommand(guideSession, guideChannel, arg),
    ),
    // The two toolchain commands need `context` for the extension's version (compared with
    // the archive's to decide on a reload) and the MCP provider, whose change event a
    // successful run fires. Registered by literal, as every one above:
    // `packageCommands.test.ts` reads the registered set from the call's first string
    // literal (comment lines skipped) and pins it to `package.json`.
    vscode.commands.registerCommand("swcraft.installToolchain", () =>
      toolchainCommand("install", context, outputChannel, mcpProvider),
    ),
    vscode.commands.registerCommand("swcraft.updateToolchain", () =>
      toolchainCommand("update", context, outputChannel, mcpProvider),
    ),
  );
}

// --- Activity Bar TreeView ---------------------------------------------------
//
// `SwcraftTreeDataProvider` owns the `vscode.TreeItem` construction and delegates every
// decision (items, grouping, labels, status text) to treeView.ts. A command item's
// `command` names a registered `swcraft.*` id, so a click takes the Command Palette's path.
//
// Three levels: the project node, the sections ("Model", "Experiments", "Setup"), and the
// command items. Model and Experiments are expanded by default (VS Code has no separator
// item, so a grouping must be visible without a click); Setup is expanded until the project
// is set up (toolchain installed, root `swcraft.yaml`, agent profile onboarded). A section
// node has no `command`. The setup facts are read once per refresh in the project node's
// `getChildren()` (skipped while a toolchain install runs), with no file watcher: a profile
// deleted by hand is seen at the next refresh.

type SwcraftTreeNode =
  | { kind: "project" }
  | { kind: "section"; descriptor: SectionDescriptor }
  | { kind: "command"; descriptor: CommandItemDescriptor }
  | { kind: "trace"; element: TraceElement };

/** The *Trace Links* command item, which holds the two mode nodes rather than running a command. */
const TRACE_ROOT_COMMAND = "swcraft.experimentTrace";

class SwcraftTreeDataProvider implements vscode.TreeDataProvider<SwcraftTreeNode> {
  private readonly onDidChangeTreeDataEmitter = new vscode.EventEmitter<void>();
  readonly onDidChangeTreeData = this.onDidChangeTreeDataEmitter.event;

  /**
   * Setup's default and generation, kept in `workspaceState` so a reload draws the same id
   * while the default holds; the default is spelled in the id, so a state VS Code restores
   * for it was the user's own toggle of that default.
   */
  constructor(private readonly setupSection: SetupSectionRecord) {}

  /** Fired by `recordCommandStatus()` whenever a command's last-run status changes. */
  refresh(): void {
    this.onDidChangeTreeDataEmitter.fire();
  }

  /**
   * Released on deactivate (`registerTreeView()` pushes the provider onto
   * `context.subscriptions`): the event emitter, which VS Code does not own, and every
   * pending status-clearing timer, whose callback would fire an event off a disposed
   * emitter.
   */
  dispose(): void {
    clearStatusTimers();
    this.onDidChangeTreeDataEmitter.dispose();
  }

  getTreeItem(element: SwcraftTreeNode): vscode.TreeItem {
    if (element.kind === "project") {
      const state = { workspaceRoot: getWorkspaceRoot(), hasManifest: this.hasManifest() };
      const node = buildProjectNode(state);
      const item = new vscode.TreeItem(node.label, vscode.TreeItemCollapsibleState.Expanded);
      item.description = node.description;
      item.contextValue = "swcraftProjectNode";
      return item;
    }
    if (element.kind === "section") {
      // VS Code applies a changed default only under a new id, so both come from the pure side.
      const item = new vscode.TreeItem(
        element.descriptor.label,
        element.descriptor.collapsedByDefault
          ? vscode.TreeItemCollapsibleState.Collapsed
          : vscode.TreeItemCollapsibleState.Expanded,
      );
      item.id = element.descriptor.id;
      item.contextValue = "swcraftSectionNode";
      return item;
    }
    if (element.kind === "trace") {
      return traceTreeItem(element.element);
    }
    const descriptor = element.descriptor;
    if (descriptor.commandId === TRACE_ROOT_COMMAND) {
      // *Trace Links* holds Labelling and Review; the work is the Trace Links view's.
      const item = new vscode.TreeItem(descriptor.label, vscode.TreeItemCollapsibleState.Expanded);
      item.id = descriptor.id;
      item.description = pendingLine(traceState.proposals) ?? descriptor.description;
      item.tooltip = descriptor.tooltip;
      item.contextValue = "trace.root";
      return item;
    }
    const item = new vscode.TreeItem(descriptor.label, vscode.TreeItemCollapsibleState.None);
    item.id = descriptor.id;
    // An outcome's status line is not coloured (`TreeItem.description` takes no colour); an
    // artifact's out-of-date state is marked with the descriptor's icon (`OUT_OF_DATE_ICON`).
    item.description = descriptor.description;
    item.tooltip = descriptor.tooltip;
    if (descriptor.icon !== undefined) {
      item.iconPath = new vscode.ThemeIcon(
        descriptor.icon.id,
        new vscode.ThemeColor(descriptor.icon.color),
      );
    }
    item.command = { command: descriptor.commandId, title: descriptor.label };
    item.contextValue = "swcraftCommandItem";
    return item;
  }

  getChildren(element?: SwcraftTreeNode): SwcraftTreeNode[] {
    if (element === undefined) {
      return [{ kind: "project" }];
    }
    if (element.kind === "project") {
      const statuses = Object.fromEntries(commandStatuses);
      const artifacts = artifactStates(getWorkspaceRoot());
      const setup = this.setupSection.advance(
        () => setupSectionCollapsed(this.setupFacts()),
        runningCommands,
      );
      return buildTree(statuses, new Date(), artifacts, runningCommands, setup).map(
        (descriptor) => ({ kind: "section", descriptor }) as const,
      );
    }
    if (element.kind === "section") {
      return element.descriptor.items.map(
        (descriptor) => ({ kind: "command", descriptor }) as const,
      );
    }
    if (element.kind === "command" && element.descriptor.commandId === TRACE_ROOT_COMMAND) {
      return traceRootChildren().map((child) => ({ kind: "trace", element: child }) as const);
    }
    // A mode node unfolds nothing here: the work is the Trace Links view's.
    return [];
  }

  private hasManifest(): boolean {
    const root = getWorkspaceRoot();
    if (root === undefined) {
      return false;
    }
    return resolveManifest(root).ok;
  }

  /**
   * The three facts `setupSectionCollapsed()` decides on, read from the disk; `isFile()`
   * throughout, since a directory is not the file setup writes. Does not reach a
   * `swcraft.executable` holding a bare command name (not a file at that path, so Setup
   * stays expanded). Not called while a toolchain command runs.
   */
  private setupFacts() {
    const root = getWorkspaceRoot();
    return {
      toolchainInstalled: isFile(swcraftResolution().executable),
      hasManifest: this.hasManifest(),
      hasAgentProfile: root !== undefined && isFile(path.join(root, AGENT_PROFILE_RELATIVE_PATH)),
    };
  }
}

/**
 * The running extension's own version, or `""` when the manifest carries none.
 *
 * Read from the *installed* manifest, never from a constant, which would be one more
 * version site to keep in step. The health check compares it with `swcraft --version`, and
 * every MCP server definition carries it (VS Code refreshes a server's tools when it
 * changes).
 */
function extensionVersion(context: vscode.ExtensionContext): string {
  const declared = context.extension.packageJSON?.version;
  return typeof declared === "string" ? declared : "";
}

/**
 * Turn one descriptor into the API's own class. `cwd` is the one field the constructor
 * does not take, so it is assigned after, as a `Uri`.
 */
function toStdioDefinition(descriptor: McpServerDescriptor): vscode.McpStdioServerDefinition {
  const definition = new vscode.McpStdioServerDefinition(
    descriptor.label,
    descriptor.command,
    descriptor.args,
    descriptor.env,
    descriptor.version,
  );
  definition.cwd = vscode.Uri.file(descriptor.cwd);
  return definition;
}

/**
 * Publish the SWCraft MCP server to VS Code. Called once from `activate()`, and returned
 * so a later caller can fire its change event.
 *
 * Registered **during activation**: VS Code consults the providers when a chat message is
 * submitted. The provider id must be the one `package.json` declares; on a mismatch the
 * provider is silently never consulted (`mcpProvider.test.ts` reads both). Every decision is
 * `mcpProvider.ts`'s; this supplies the open folders, the executable and the version.
 */
export function registerMcpServerProvider(
  context: vscode.ExtensionContext,
): SwcraftMcpServerProvider {
  const emitter = new vscode.EventEmitter<void>();
  context.subscriptions.push(emitter);
  const provider = new SwcraftMcpServerProvider(
    emitter,
    () => {
      const resolution = swcraftResolution();
      return {
        folders: (vscode.workspace.workspaceFolders ?? []).map((folder) => folder.uri.fsPath),
        executable: resolution.executable,
        version: extensionVersion(context),
        // Read, not assumed, so `refresh()` after an install has something to change.
        toolchainPresent: resolution.exists,
      };
    },
    // `statSync().isFile()`, not `existsSync`: a *directory* named `swcraft.yaml` publishes
    // nothing, as `project.ts`'s `findManifest()` finds nothing there.
    isFile,
  );
  context.subscriptions.push(
    vscode.lm.registerMcpServerDefinitionProvider(MCP_PROVIDER_ID, {
      onDidChangeMcpServerDefinitions: emitter.event,
      provideMcpServerDefinitions: () => provider.definitions().map(toStdioDefinition),
    }),
  );
  // A folder added to or removed from a multi-root window changes the set and every label,
  // and `swcraft.executable` names the command every definition spawns. *Onboard Project*
  // fires the event itself after it writes a `swcraft.yaml`.
  context.subscriptions.push(
    vscode.workspace.onDidChangeWorkspaceFolders(() => provider.refresh()),
    vscode.workspace.onDidChangeConfiguration((event) => {
      if (event.affectsConfiguration(EXECUTABLE_SETTING)) {
        provider.refresh();
      }
    }),
  );
  return provider;
}

/**
 * Register the SWCraft Activity Bar TreeView (`swcraftView`). Called once from
 * `activate()`, after `registerCommands()`, whose commands its items invoke.
 */
export function registerTreeView(context: vscode.ExtensionContext): void {
  const provider = new SwcraftTreeDataProvider(new SetupSectionRecord(context.workspaceState));
  treeDataProvider = provider;
  // `createTreeView`, because only a `TreeView` carries the `badge` counting the
  // out-of-date artifacts on the activity-bar icon.
  treeView = vscode.window.createTreeView(TREE_VIEW_ID, { treeDataProvider: provider });
  context.subscriptions.push(
    treeView,
    // The provider itself too: disposing the view disposes nothing the provider owns.
    provider,
  );
  artifactsChanged();
}

/**
 * The workspace project's `model_root`, read from its manifest now, or
 * `undefined` when there is no workspace, no manifest or no model root.
 */
function currentModelRoot(): string | undefined {
  const root = getWorkspaceRoot();
  const manifest = root === undefined ? undefined : findManifest(root);
  return manifest === undefined ? undefined : resolveModelRoot(manifest);
}

/**
 * The project's `model_root` and *document*'s layer under it, or `undefined` when the
 * document is no saved file, there is no model root, or the file is in no layer: the gate
 * every navigation provider on a model file shares.
 */
function modelFileOf(
  refs: References,
  document: vscode.TextDocument,
): { modelRoot: string; layer: string } | undefined {
  if (document.uri.scheme !== "file") {
    return undefined;
  }
  const modelRoot = currentModelRoot();
  const layer =
    modelRoot === undefined ? undefined : layerOf(document.uri.fsPath, modelRoot, refs.layers);
  return modelRoot === undefined || layer === undefined ? undefined : { modelRoot, layer };
}

/**
 * Keep `swcraft.activeEditorIsLayerFile` in step with the active editor. Called once from
 * `activate()`.
 *
 * The walkthrough's *Edit a layer file* step completes on this key, so it must go back to
 * `false` whenever the active editor is not a layer file. A **path test**, not a model read:
 * `model_root` from `swcraft.yaml`, the layer directories from this extension's
 * `contributes.yamlValidation` globs; the file is never opened or parsed.
 */
export function registerLayerFileContext(context: vscode.ExtensionContext): void {
  const yamlValidation = (context.extension?.packageJSON?.contributes?.yamlValidation ??
    []) as Array<{ fileMatch?: string[] }>;
  const rules = layerDirectoryRules(yamlValidation.flatMap((entry) => entry.fileMatch ?? []));

  const update = (editor: vscode.TextEditor | undefined): void => {
    const filePath =
      editor?.document.uri.scheme === "file" ? editor.document.uri.fsPath : undefined;
    void vscode.commands.executeCommand(
      "setContext",
      LAYER_FILE_CONTEXT_KEY,
      isLayerFilePath(filePath, currentModelRoot(), rules),
    );
  };

  context.subscriptions.push(vscode.window.onDidChangeActiveTextEditor(update));
  // `onDidChangeActiveTextEditor` does not fire for the editor already active at
  // activation, so seed the key from it.
  update(vscode.window.activeTextEditor);
}

// --- Navigation ----------------------------------------------------------------

/** The generated reference table, shipped beside the schemas (`editor_schema.py`). */
const REFERENCES_ARTIFACT = "references.json";

/**
 * Where the installed extension's reference table is: navigation loads it once when it
 * registers, and `checkModelRun()` per run.
 */
function referencesArtifactPath(context: vscode.ExtensionContext): string {
  return path.join(context.extensionPath, "schemas", REFERENCES_ARTIFACT);
}

/**
 * navigationCore's `Reader` over the real file system: synchronous, read-only and silent:
 * an unreadable or non-UTF-8 file ({@link decodeModelText}) is `undefined`, never a throw.
 */
const fsReader: Reader = {
  readText: (filePath) => {
    try {
      return decodeModelText(fs.readFileSync(filePath));
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

/** navigationCore's {@link requestReader} over {@link fsReader}: one snapshot of the files per request. */
function fsRequestReader(): Reader {
  return requestReader(fsReader);
}

/**
 * The reference, relation or member reference under *position* in *document* and where it
 * resolves, or `undefined`. The document must be a file of a layer under the project's
 * `model_root`, read from the manifest at every request; nothing is kept between requests.
 * The line is read from the live buffer, as is a `port:` member reference's target; every
 * other target from disk, through *reader*.
 */
function definitionAt(
  refs: References,
  document: vscode.TextDocument,
  position: vscode.Position,
  reader: Reader = fsReader,
): DefinitionTarget | undefined {
  const at = modelFileOf(refs, document);
  return at === undefined
    ? undefined
    : definitionTarget(
        refs,
        document.getText(),
        at.layer,
        position.line,
        document.uri.fsPath,
        at.modelRoot,
        reader,
      );
}

/** Show a Find All References answer's one message, if it has one, at its severity. */
function showReferencesMessage(answer: ReferencesAnswer): void {
  if (answer.message?.severity === "information") {
    void vscode.window.showInformationMessage(answer.message.text);
  } else if (answer.message !== undefined) {
    void vscode.window.showWarningMessage(answer.message.text);
  }
}

/**
 * Find All References on an element's identity line: who refers to the element, answered
 * by one `swcraft impact --format json --depth 1` run per request, with no index and no
 * answer kept. The line must be the file's only column-0 identity line, or no CLI runs. The
 * run is bounded by `LIST_PROBE_TIMEOUT_MS` (a whole-model read) and stopped when VS Code
 * cancels the request. A document with unsaved changes asks the CLI only when its saved
 * text declares the same identity. Not a registered command, so no running indicator.
 */
async function provideReferencesAt(
  refs: References,
  document: vscode.TextDocument,
  position: vscode.Position,
  includeDeclaration: boolean,
  token: vscode.CancellationToken,
): Promise<vscode.Location[] | undefined> {
  if (document.uri.scheme !== "file") {
    return undefined;
  }
  const workspaceRoot = getWorkspaceRoot();
  const resolution = workspaceRoot === undefined ? undefined : resolveManifest(workspaceRoot);
  if (workspaceRoot === undefined || resolution === undefined || !resolution.ok) {
    return undefined;
  }
  const modelRoot = resolveModelRoot(resolution.manifestPath);
  const layer =
    modelRoot === undefined ? undefined : layerOf(document.uri.fsPath, modelRoot, refs.layers);
  if (modelRoot === undefined || layer === undefined) {
    return undefined;
  }
  const name = identityAt(refs, document.getText(), layer, position.line);
  if (name === undefined) {
    return undefined;
  }
  // The CLI reads the saved file: an unsaved identity edit gets a "save first" message,
  // never the dependents of a name not on disk.
  const unsaved = document.isDirty
    ? unsavedIdentityAnswer(refs, fsReader.readText(document.uri.fsPath), layer, name)
    : undefined;
  if (unsaved !== undefined) {
    showReferencesMessage(unsaved);
    return [];
  }
  const executable = resolveSwcraftExecutable();
  const argv = buildImpactJsonArgv(resolution.manifestRelPath, name);
  const controller = new AbortController();
  const cancellation = token.onCancellationRequested(() => controller.abort());
  try {
    const capture = await spawnCliCapture(
      executable,
      argv,
      workspaceRoot,
      LIST_PROBE_TIMEOUT_MS,
      controller.signal,
    );
    const answer = referencesAnswer(refs, capture, layer, modelRoot, fsReader, {
      cancelled: token.isCancellationRequested,
      executable,
      declaration: includeDeclaration
        ? { file: document.uri.fsPath, line: position.line }
        : undefined,
    });
    showReferencesMessage(answer);
    // A member line carries the link's columns; every other location is a line.
    return answer.locations.map(
      (found) =>
        new vscode.Location(
          vscode.Uri.file(found.file),
          found.start === undefined || found.end === undefined
            ? new vscode.Position(found.line, 0)
            : new vscode.Range(found.line, found.start, found.line, found.end),
        ),
    );
  } finally {
    cancellation.dispose();
  }
}

/** A symbol kind's name from symbolCore as `vscode.SymbolKind`; an unknown name falls back. */
function symbolKind(name: string, fallback: vscode.SymbolKind): vscode.SymbolKind {
  const kind = (vscode.SymbolKind as unknown as Record<string, vscode.SymbolKind | undefined>)[
    name
  ];
  return typeof kind === "number" ? kind : fallback;
}

function rangeOf(span: Span): vscode.Range {
  return new vscode.Range(span.startLine, span.startCharacter, span.endLine, span.endCharacter);
}

function documentSymbolOf(symbol: OutlineSymbol): vscode.DocumentSymbol {
  const found = new vscode.DocumentSymbol(
    symbol.name,
    symbol.detail,
    symbolKind(symbol.kind, vscode.SymbolKind.Field),
    rangeOf(symbol.range),
    rangeOf(symbol.selection),
  );
  found.children = symbol.children.map(documentSymbolOf);
  return found;
}

/**
 * A Go to Symbol in Workspace answer: the file alone, no range, so VS Code calls
 * `resolveWorkspaceSymbol` for the symbol the user picks. The layer is kept for that.
 */
class FileSymbol extends vscode.SymbolInformation {
  constructor(
    name: string,
    kind: vscode.SymbolKind,
    container: string,
    readonly layer: string,
    readonly file: string,
  ) {
    // A `uri` with no `range` is the API's documented partial location; it is set after
    // `super`, whose constructor takes only a `vscode.Location`.
    super(
      name,
      kind,
      container,
      new vscode.Location(vscode.Uri.file(file), new vscode.Position(0, 0)),
    );
    this.location = { uri: vscode.Uri.file(file) } as vscode.Location;
  }
}

// --- CodeLens --------------------------------------------------------------------

/**
 * The click of the *references* lens: Find All References at the identity line, as
 * Shift+F12 does. The lens carries no count, so it is complete when provided and runs no
 * CLI until clicked.
 */
function referencesLensCommand(uri: vscode.Uri, line: number, title: string): vscode.Command {
  return {
    title,
    command: "editor.action.findReferences",
    arguments: [uri, new vscode.Position(line, 0)],
  };
}

/** One lens of navigationCore's plan as a `vscode.CodeLens`. */
function codeLensOf(
  item: LensItem,
  document: vscode.TextDocument,
  plan: LensPlan,
): vscode.CodeLens {
  const line = plan.line;
  const range = new vscode.Range(line, 0, line, 0);
  switch (item.kind) {
    case "references":
      // Dirty or saved alike: the click's Find All References answers either.
      return new vscode.CodeLens(range, referencesLensCommand(document.uri, line, "references"));
    case "impact":
      return new vscode.CodeLens(range, {
        title: "impact",
        command: "swcraft.impact",
        // The document's Uri beside the name: the click asks to save first when the saved
        // file does not declare the name the live buffer's lens shows.
        arguments: [item.name, document.uri],
      });
    case "diagram":
      return new vscode.CodeLens(range, {
        title: "diagram",
        command: "swcraft.diagrams",
        arguments: [{ view: item.view }],
      });
    case "show-in-diagram":
      // Show in Diagram on the element (no member: the lens is on the identity line).
      return new vscode.CodeLens(range, {
        title: "diagram",
        command: "swcraft.showInDiagram",
        arguments: [{ file: item.file, name: plan.name }],
      });
  }
}

/**
 * The lenses of *document*: navigationCore's `lensPlan` on the live buffer of a model file,
 * the *diagram* lens for a composition's or sequence's page listed in `views_root` now, else
 * for an element the diagrams' index lists, and last the *guide* lens (`guideLens()`). No
 * CLI runs here, and there is no `resolveCodeLens`, so VS Code waits for nothing before it
 * draws the row.
 */
function codeLensesOf(refs: References, document: vscode.TextDocument): vscode.CodeLens[] {
  const at = modelFileOf(refs, document);
  const workspaceRoot = getWorkspaceRoot();
  const manifest = workspaceRoot === undefined ? undefined : findManifest(workspaceRoot);
  if (at === undefined || manifest === undefined) {
    return [];
  }
  const { layer, modelRoot } = at;
  const views = resolveViewsRoot(manifest);
  const entry = views.ok ? diagramIndexEntry(views.path) : undefined;
  const index: LensIndex = {
    file: entry === undefined ? undefined : indexedNameOf(entry, modelRoot, document.uri.fsPath),
    elements: entry?.index.elements,
  };
  const text = document.getText();
  const plan = lensPlan(refs, text, layer, views.ok ? listViewsRoot(views.path) : undefined, index);
  if (plan === undefined) {
    return [];
  }
  const lenses = plan.lenses.map((item) => codeLensOf(item, document, plan));
  const guide = guideLens(refs, text, layer);
  if (guide !== undefined) {
    lenses.push(
      new vscode.CodeLens(new vscode.Range(plan.line, 0, plan.line, 0), guideLensCommand(guide)),
    );
  }
  return lenses;
}

/**
 * The CodeLens provider. Its one refresh is the diagrams' index, which changes outside the
 * model watch: a press and a tab reload fire `diagramLensChanges`; a save refreshes nothing.
 */
function codeLensProvider(refs: References, changes: vscode.Event<void>): vscode.CodeLensProvider {
  return {
    onDidChangeCodeLenses: changes,
    provideCodeLenses: (document) => codeLensesOf(refs, document),
  };
}

// --- document links --------------------------------------------------------------

/**
 * A link as discovery answers it: no `target`, so VS Code calls `resolveDocumentLink`, and
 * linkCore's candidate (chosen from the directory scan, no file read) kept for it.
 */
class CandidateLink extends vscode.DocumentLink {
  constructor(
    range: vscode.Range,
    private readonly candidate: LinkCandidate,
  ) {
    super(range);
  }

  /**
   * Confirm the candidate and set the target at its identity line (`#L<n>`), or at the
   * listed file's top when the confirmation refuses.
   */
  resolve(refs: References): CandidateLink {
    const found = resolveCandidate(this.candidate, refs, currentModelRoot(), fsReader);
    this.target = vscode.Uri.file(found.file).with({ fragment: `L${found.line}` });
    return this;
  }
}

/** The two link providers: `description` values of model files, and the import reports. */
function documentLinkProviders(
  refs: References,
): [vscode.DocumentLinkProvider<CandidateLink>, vscode.DocumentLinkProvider<CandidateLink>] {
  const linksOf = (found: readonly DiscoveredLink[]): CandidateLink[] =>
    found.map(
      (link) =>
        new CandidateLink(
          new vscode.Range(link.line, link.start, link.line, link.end),
          link.candidate,
        ),
    );
  const descriptions: vscode.DocumentLinkProvider<CandidateLink> = {
    provideDocumentLinks(document) {
      const at = modelFileOf(refs, document);
      return at === undefined
        ? []
        : linksOf(
            descriptionLinks(
              refs,
              document.getText(),
              at.layer,
              scanModel(refs, at.modelRoot, fsReader),
            ),
          );
    },
    resolveDocumentLink: (link) => link.resolve(refs),
  };
  const reports: vscode.DocumentLinkProvider<CandidateLink> = {
    provideDocumentLinks(document) {
      // The first line decides: any other document costs this one line.
      if (document.lineCount === 0 || reportKind(document.lineAt(0).text) === undefined) {
        return [];
      }
      const modelRoot = currentModelRoot();
      if (modelRoot === undefined) {
        return [];
      }
      return linksOf(reportLinks(refs, document.getText(), scanModel(refs, modelRoot, fsReader)));
    },
    resolveDocumentLink: (link) => link.resolve(refs),
  };
  return [descriptions, reports];
}

// --- quick fixes -----------------------------------------------------------------

/**
 * The quick fixes of *document*'s SWCraft entries: for each entry of the two link warnings
 * whose line still holds a link to the finding's requirement (checked against
 * {@link findingTargets}), the requirement's version is read from its **saved** file and
 * `quickFixFor` decides the one action, if any. VS Code asks on every selection change, so
 * the entries are filtered before anything is read.
 *
 * The action is a `WorkspaceEdit` of the open buffer alone: it saves and runs nothing, and
 * the next Check Model clears the entry. It is **not** marked preferred, so Auto Fix does
 * not apply it unpicked.
 *
 * Only an **explicit** request (`Invoke`: Ctrl+./Cmd+., the hover's or the Problems panel's
 * Quick Fix) gets an action: VS Code asks with `Automatic` for the light bulb and for
 * `editor.codeActionsOnSave`, whose save participant applies what it gets. So a save never
 * applies a fix; the price is no proactive light bulb.
 */
function quickFixesOf(
  refs: References,
  document: vscode.TextDocument,
  diagnostics: readonly vscode.Diagnostic[],
  triggerKind: vscode.CodeActionTriggerKind,
): vscode.CodeAction[] {
  if (triggerKind !== vscode.CodeActionTriggerKind.Invoke) {
    return [];
  }
  const ours: { diagnostic: vscode.Diagnostic; code: string }[] = [];
  for (const diagnostic of diagnostics) {
    const code = diagnosticRule(diagnostic.code);
    if (
      diagnostic.source === DIAGNOSTIC_SOURCE &&
      code !== undefined &&
      QUICK_FIX_RULES.has(code)
    ) {
      ours.push({ diagnostic, code });
    }
  }
  if (ours.length === 0) {
    return [];
  }
  const at = modelFileOf(refs, document);
  if (at === undefined) {
    return [];
  }
  const text = document.getText();
  const reader = fsRequestReader();
  const actions: vscode.CodeAction[] = [];
  for (const { diagnostic, code } of ours) {
    const line = diagnostic.range.start.line;
    const target = findingTargets.get(findingKey(document.uri, code, diagnostic.message));
    const fix = quickFixFor(code, target, linkAt(text, line), (id) =>
      requirementVersion(refs, id, at.modelRoot, reader),
    );
    if (fix === undefined) {
      continue;
    }
    const action = new vscode.CodeAction(fix.title, vscode.CodeActionKind.QuickFix);
    action.edit = new vscode.WorkspaceEdit();
    action.edit.replace(
      document.uri,
      new vscode.Range(line, fix.start, line, fix.end),
      fix.replacement,
    );
    action.diagnostics = [diagnostic];
    actions.push(action);
  }
  return actions;
}

/**
 * Register the navigation providers: Go to Definition (a reference, a relation, a member
 * reference), the hover, Find All References, the outline, Go to Symbol in Workspace, the
 * CodeLens of an element's identity line, the document links in `description` values and
 * import reports, and the quick fixes of the two link warnings. Called **last** from
 * `activate()`, inside a guard: it throws when `schemas/references.json` is missing or
 * malformed, and the caller turns that into one warning.
 *
 * The providers read model files **read-only and lexically**, decide nothing about
 * validity and add no diagnostic. Definition, hover, the outline, the symbol scan and the
 * links run no CLI; Find All References and the *references* lens ask the read-only
 * `swcraft impact`. A document outside a layer of `model_root` gets
 * nothing, except an import report, decided by its first line alone.
 */
export function registerNavigation(context: vscode.ExtensionContext): void {
  const artifact = referencesArtifactPath(context);
  const refs = loadReferences(fs.readFileSync(artifact, { encoding: "utf8" }));
  navigationReferences = refs;
  const selector: vscode.DocumentSelector = { language: "yaml", scheme: "file" };
  const lensChanges = new vscode.EventEmitter<void>();
  diagramLensChanges = lensChanges;
  context.subscriptions.push(lensChanges);

  const definitionProvider: vscode.DefinitionProvider = {
    provideDefinition(document, position) {
      const found = definitionAt(refs, document, position);
      return found === undefined
        ? undefined
        : new vscode.Location(
            vscode.Uri.file(found.target.file),
            new vscode.Position(found.target.line, 0),
          );
    },
  };

  const hoverProvider: vscode.HoverProvider = {
    provideHover(document, position) {
      // The resolver's confirmation already read the target; the card reuses that read. A
      // member reference's target is a line inside an element: no card.
      const reader = fsRequestReader();
      const resolved = definitionAt(refs, document, position, reader);
      const found = resolved?.edgeKind === "memberReference" ? undefined : resolved;
      const facts = found === undefined ? undefined : refs.layers.get(found.targetLayer);
      const text = found === undefined ? undefined : reader.readText(found.target.file);
      const card = facts === undefined || text === undefined ? undefined : hoverCard(text, facts);
      return card === undefined
        ? undefined
        : new vscode.Hover(new vscode.MarkdownString(hoverMarkdown(card)));
    },
  };

  const documentSymbolProvider: vscode.DocumentSymbolProvider = {
    provideDocumentSymbols(document) {
      const at = modelFileOf(refs, document);
      return at === undefined
        ? undefined
        : outlineOf(refs, document.getText(), at.layer).map(documentSymbolOf);
    },
  };

  const workspaceSymbolProvider: vscode.WorkspaceSymbolProvider<FileSymbol> = {
    provideWorkspaceSymbols(query) {
      const modelRoot = currentModelRoot();
      if (modelRoot === undefined) {
        return [];
      }
      return workspaceSymbols(refs, modelRoot, fsReader, query).map(
        (found) =>
          new FileSymbol(
            found.name,
            symbolKind(found.kind, vscode.SymbolKind.Object),
            found.container,
            found.layer,
            found.file,
          ),
      );
    },
    resolveWorkspaceSymbol(symbol) {
      const facts = refs.layers.get(symbol.layer);
      const line = facts === undefined ? 0 : resolveSymbolLine(facts, symbol.file, fsReader);
      symbol.location = new vscode.Location(
        vscode.Uri.file(symbol.file),
        new vscode.Position(line, 0),
      );
      return symbol;
    },
  };

  const [descriptionLinkProvider, reportLinkProvider] = documentLinkProviders(refs);

  context.subscriptions.push(
    vscode.languages.registerDefinitionProvider(selector, definitionProvider),
    vscode.languages.registerHoverProvider(selector, hoverProvider),
    vscode.languages.registerReferenceProvider(selector, {
      provideReferences: (document, position, referenceContext, token) =>
        provideReferencesAt(refs, document, position, referenceContext.includeDeclaration, token),
    }),
    vscode.languages.registerDocumentSymbolProvider(selector, documentSymbolProvider),
    vscode.languages.registerWorkspaceSymbolProvider(workspaceSymbolProvider),
    vscode.languages.registerCodeLensProvider(selector, codeLensProvider(refs, lensChanges.event)),
    vscode.languages.registerDocumentLinkProvider(selector, descriptionLinkProvider),
    vscode.languages.registerDocumentLinkProvider({ scheme: "file" }, reportLinkProvider),
    vscode.languages.registerCodeActionsProvider(
      selector,
      {
        provideCodeActions: (document, _range, codeActionContext) =>
          quickFixesOf(
            refs,
            document,
            codeActionContext.diagnostics,
            codeActionContext.triggerKind,
          ),
      },
      { providedCodeActionKinds: [vscode.CodeActionKind.QuickFix] },
    ),
  );
}

// --- The Trace Links tree (the trace link search's panel) -----------------------
//
// The trace tree lives in the SWCraft view under *Experiments → Trace Links* (the two mode
// nodes *Labelling* and *Review*) and in the Trace Links view: document → requirement →
// items with checkboxes, read from the proposals file the last search wrote. Every decision
// of its look and every edit of the file is traceCore.ts's; this half reads and writes the
// one file, keeps the context keys and the document sets, and runs the CLI for the search,
// Apply and *Choose Another Element…*. The proposals file lives in the experiment's report
// directory, which the CLI refuses to put inside `model_root`. Does not reach two windows on
// one project: nothing is locked, and the byte comparison before each write stops a window
// that read an older file, not two writes that compare the same bytes before either renames.

/** `workspaceState` key: the proposals file shown, relative to the manifest's directory, POSIX. */
const TRACE_FILE_KEY = "swcraft.trace.proposals";
/** `workspaceState` key: the two document sets, one per mode; a view preference. */
const TRACE_MODES_KEY = "swcraft.trace.modes";
/** The write queue's coalescing interval: a run of ticks is one write. */
const TRACE_WRITE_DELAY_MS = 250;

/** One element of the tree: a descriptor, or the one line shown for a file that cannot be read. */
type TraceElement = TraceNode | TraceLine;

interface TraceState {
  /** The proposals file shown, absolute; `undefined` before any search. */
  file?: string;
  /** The bytes last read from or written to *file*: the write's comparison and the watcher's echo filter. */
  bytes?: Uint8Array;
  /** The file as read, with the edits not yet written applied. */
  proposals?: ProposalsFile;
  /** Why *file* cannot be read, when it cannot. */
  refusal?: string;
  /** A search or an Apply of the panel is running. */
  busy: boolean;
  /** The edited file waiting for the queue's timer. */
  queued?: ProposalsFile;
  timer?: ReturnType<typeof setTimeout>;
  /** The documents chosen per mode. */
  modes: TraceModes;
  /** The mode the Trace Links view shows: the one clicked last. */
  mode: TraceMode;
  /** Raised on each reload from disk, never on the panel's own edits: part of every trace id. */
  generation: number;
}

const traceState: TraceState = {
  busy: false,
  modes: readModes(undefined),
  mode: "review",
  generation: 0,
};
let traceContext: vscode.ExtensionContext | undefined;
/** The Trace Links view (`swcraftTrace`), set by `registerTraceView()`. */
let traceView: vscode.TreeView<TraceElement> | undefined;
const traceTreeChanged = new vscode.EventEmitter<TraceElement | undefined>();
const TRACE_VIEW_ID = "swcraftTrace";
/** `workspaceState` key: the mode the Trace Links view shows. */
const TRACE_MODE_KEY = "swcraft.trace.mode";
let traceWatcher: vscode.Disposable | undefined;

/** The manifest's directory, which every remembered or answered proposals name is relative to. */
function traceProjectDir(): string | undefined {
  const root = getWorkspaceRoot();
  const manifest = root === undefined ? undefined : findManifest(root);
  return manifest === undefined ? undefined : path.dirname(manifest);
}

function readBytesOrNothing(file: string): Uint8Array | undefined {
  try {
    return fs.readFileSync(file);
  } catch {
    return undefined;
  }
}

/**
 * Whether two paths name one file: by their real paths when both exist (a
 * workspace opened through a symlink, the CLI's resolved path), else by their
 * `file:` URIs, which VS Code normalises (a Windows drive letter's case).
 */
function sameFile(left: string, right: string): boolean {
  try {
    return fs.realpathSync.native(left) === fs.realpathSync.native(right);
  } catch {
    return vscode.Uri.file(left).toString() === vscode.Uri.file(right).toString();
  }
}

function sameBytes(left: Uint8Array | undefined, right: Uint8Array | undefined): boolean {
  return left !== undefined && right !== undefined && Buffer.from(left).equals(Buffer.from(right));
}

/** Redraw the tree and set the two context keys the menus read. */
function refreshTraceView(): void {
  const proposals = traceState.proposals;
  void vscode.commands.executeCommand(
    "setContext",
    "swcraft.trace.pending",
    proposals !== undefined && pendingCount(proposals) > 0,
  );
  void vscode.commands.executeCommand(
    "setContext",
    "swcraft.trace.loaded",
    proposals !== undefined,
  );
  void vscode.commands.executeCommand("setContext", "swcraft.trace.mode", traceState.mode);
  if (traceView !== undefined) {
    traceView.description = traceViewDescription(traceState.modes, traceState.mode);
  }
  treeDataProvider?.refresh();
  traceTreeChanged.fire(undefined);
}

/** Show *mode* in the Trace Links view, remembered per workspace, and reveal the view. */
async function showTraceMode(mode: TraceMode): Promise<void> {
  traceState.mode = mode;
  void traceContext?.workspaceState.update(TRACE_MODE_KEY, mode);
  refreshTraceView();
  await vscode.commands.executeCommand(`${TRACE_VIEW_ID}.focus`);
}

/** Keep *modes* as the two document sets, remembered per workspace, and redraw. */
function setTraceModes(modes: TraceModes): void {
  traceState.modes = modes;
  void traceContext?.workspaceState.update(TRACE_MODES_KEY, modes);
  refreshTraceView();
}

function setTraceBusy(busy: boolean): void {
  traceState.busy = busy;
  void vscode.commands.executeCommand("setContext", "swcraft.trace.busy", busy);
}

/** Watch *file* for a search, an Apply or a hand edit; the panel's own write is recognised by its bytes. */
function watchTraceFile(file: string): void {
  traceWatcher?.dispose();
  const watcher = vscode.workspace.createFileSystemWatcher(
    new vscode.RelativePattern(vscode.Uri.file(path.dirname(file)), path.basename(file)),
  );
  const changed = (): void => {
    if (!sameBytes(readBytesOrNothing(file), traceState.bytes)) {
      const lost = traceState.queued !== undefined;
      loadTraceFile(file);
      if (lost) {
        void vscode.window.showWarningMessage(CHANGED_ON_DISK);
      }
    }
  };
  watcher.onDidChange(changed);
  watcher.onDidCreate(changed);
  watcher.onDidDelete(changed);
  traceWatcher = watcher;
}

/**
 * Show *file*: read it, remember it per workspace (relative to the manifest's
 * directory) and watch it. A queued edit is dropped: the file on disk wins.
 */
function loadTraceFile(file: string): void {
  if (traceState.timer !== undefined) {
    clearTimeout(traceState.timer);
    traceState.timer = undefined;
  }
  traceState.queued = undefined;
  if (traceState.file !== file) {
    watchTraceFile(file);
  }
  traceState.file = file;
  // A reload from disk is a new tree: VS Code fetches every node afresh rather than keeping
  // children it holds under an unchanged id.
  traceState.generation += 1;
  const bytes = readBytesOrNothing(file);
  traceState.bytes = bytes;
  traceState.proposals = undefined;
  traceState.refusal = undefined;
  if (bytes !== undefined) {
    const read = readProposals(bytes);
    if (isRefusal(read)) {
      traceState.refusal = read.error;
    } else {
      traceState.proposals = read;
    }
  }
  const projectDir = traceProjectDir();
  if (projectDir !== undefined && traceContext !== undefined) {
    const relative = path.relative(projectDir, file).split(path.sep).join("/");
    void traceContext.workspaceState.update(TRACE_FILE_KEY, relative);
  }
  refreshTraceView();
}

/** Whether the proposals file is open in an editor with unsaved changes. */
function traceFileIsDirty(): boolean {
  const file = traceState.file;
  return (
    file !== undefined &&
    vscode.workspace.textDocuments.some(
      (document) =>
        document.uri.scheme === "file" && sameFile(document.uri.fsPath, file) && document.isDirty,
    )
  );
}

/**
 * Write *proposals* now: only over the bytes the panel last read or wrote —
 * otherwise the view is reloaded from disk and the change is not saved — into
 * a `.partial` sibling created exclusively, renamed into place.
 */
function writeTraceFile(proposals: ProposalsFile): void {
  const file = traceState.file;
  if (file === undefined) {
    return;
  }
  if (traceFileIsDirty()) {
    loadTraceFile(file);
    void vscode.window.showWarningMessage(DIRTY_REFUSAL);
    return;
  }
  if (!sameBytes(readBytesOrNothing(file), traceState.bytes)) {
    loadTraceFile(file);
    void vscode.window.showWarningMessage(CHANGED_ON_DISK);
    return;
  }
  const bytes = Buffer.from(serializeProposals(proposals), "utf8");
  const partial = `${file}.${randomBytes(8).toString("hex")}.partial`;
  try {
    fs.writeFileSync(partial, bytes, { flag: "wx" });
    fs.renameSync(partial, file);
  } catch (error) {
    fs.rmSync(partial, { force: true });
    void vscode.window.showErrorMessage(
      `SWCraft: the proposals file could not be written (${error instanceof Error ? error.message : String(error)}); the view was reloaded.`,
    );
    loadTraceFile(file);
    return;
  }
  traceState.bytes = bytes;
}

/**
 * Write the queued edit now and cancel the timer. Every flow that hands the
 * file to the CLI or to an editor flushes first, and so does `deactivate()`.
 */
export function flushTraceWrites(): Promise<void> {
  if (traceState.timer !== undefined) {
    clearTimeout(traceState.timer);
    traceState.timer = undefined;
  }
  const queued = traceState.queued;
  traceState.queued = undefined;
  if (queued !== undefined) {
    writeTraceFile(queued);
  }
  return Promise.resolve();
}

/**
 * Apply one decision edit: refused while a run is in progress or while the file
 * has unsaved changes in an editor; otherwise shown at once and written by the
 * queue, one write per burst of clicks.
 */
function applyTraceEdit(edit: (proposals: ProposalsFile) => EditResult, reserved = false): void {
  const proposals = traceState.proposals;
  if (proposals === undefined) {
    return;
  }
  // *reserved*: a flow that holds busy itself makes this edit.
  const refusal = traceState.busy && !reserved ? BUSY_REFUSAL : isDirtyRefusal(traceFileIsDirty());
  if (refusal !== undefined) {
    void vscode.window.showWarningMessage(refusal);
    refreshTraceView();
    return;
  }
  const result = edit(proposals);
  if (!result.ok) {
    void vscode.window.showInformationMessage(`SWCraft: ${result.refusal}`);
    refreshTraceView();
    return;
  }
  queueTraceWrite(result.file);
}

function queueTraceWrite(proposals: ProposalsFile): void {
  traceState.proposals = proposals;
  traceState.queued = proposals;
  if (traceState.timer !== undefined) {
    clearTimeout(traceState.timer);
  }
  traceState.timer = setTimeout(() => {
    void flushTraceWrites();
  }, TRACE_WRITE_DELAY_MS);
  refreshTraceView();
}

/** A checkbox flipped: the tick, untick, confirm or choice removal it means. */
function traceCheckboxChanged(
  items: ReadonlyArray<[TraceElement, vscode.TreeItemCheckboxState]>,
): void {
  for (const [element, state] of items) {
    if (element.kind === "message" || element.kind === "apply" || element.item === undefined) {
      continue;
    }
    const checked = state === vscode.TreeItemCheckboxState.Checked;
    const item = element.item;
    applyTraceEdit((proposals) => {
      if (item.slot === "candidates") {
        return (checked ? tick : untick)(proposals, item.requirement, item.target);
      }
      if (item.slot === "chosen") {
        return checked
          ? { ok: true, file: proposals }
          : removeChoice(proposals, item.requirement, item.target);
      }
      return (checked ? confirmLink : unconfirmLink)(
        proposals,
        item.requirement,
        item.index,
        item.target,
      );
    });
  }
}

/** The `TreeItem` of one trace element: every decision of its look is the node's (traceCore.ts). */
function traceTreeItem(element: TraceElement): vscode.TreeItem {
  if (element.kind === "message") {
    const item = new vscode.TreeItem(element.text, vscode.TreeItemCollapsibleState.None);
    item.iconPath = new vscode.ThemeIcon("warning");
    return item;
  }
  if (element.kind === "apply") {
    // Always visible, unlike the title bar's icons.
    const item = new vscode.TreeItem(element.text, vscode.TreeItemCollapsibleState.None);
    item.id = "trace:apply";
    item.iconPath = new vscode.ThemeIcon("check", new vscode.ThemeColor("charts.green"));
    item.tooltip = "Write the decisions into the model: a dry run first, then one question.";
    item.command = { command: "swcraft.traceApply", title: "Apply…" };
    return item;
  }
  const collapsible = {
    none: vscode.TreeItemCollapsibleState.None,
    collapsed: vscode.TreeItemCollapsibleState.Collapsed,
    expanded: vscode.TreeItemCollapsibleState.Expanded,
  }[element.collapsible];
  const item = new vscode.TreeItem(
    element.highlights === undefined
      ? element.label
      : { label: element.label, highlights: element.highlights },
    collapsible,
  );
  item.id = element.id;
  item.description = element.description;
  item.tooltip = new vscode.MarkdownString(element.tooltip);
  item.contextValue = element.contextValue;
  if (element.icon !== undefined) {
    item.iconPath = new vscode.ThemeIcon(
      element.icon.id,
      element.icon.color === undefined ? undefined : new vscode.ThemeColor(element.icon.color),
    );
  }
  if (element.checkbox !== undefined) {
    item.checkboxState =
      element.checkbox === "checked"
        ? vscode.TreeItemCheckboxState.Checked
        : vscode.TreeItemCheckboxState.Unchecked;
  }
  if (element.kind === "mode" && element.mode !== undefined) {
    // A click on Labelling or Review opens its document picker.
    item.command = {
      command: "swcraft.traceChooseMode",
      title: "Choose Documents…",
      arguments: [element],
    };
  } else if (element.kind === "requirement") {
    // A click shows the requirement's text beside the tree; the row still expands by its arrow.
    item.command = {
      command: "swcraft.traceOpenRequirement",
      title: "Open Requirement",
      arguments: [element],
    };
  } else if (element.kind === "other") {
    item.command = {
      command: "swcraft.traceOtherElement",
      title: "Other Element…",
      arguments: [element],
    };
  } else if (element.item !== undefined) {
    // The click opens the element; the checkbox is the decision.
    item.command = {
      command: "swcraft.traceOpenElement",
      title: "Open Element",
      arguments: [element],
    };
  }
  return item;
}

/** The children of the panel's *Trace Links*: Labelling and Review. */
function traceRootChildren(): TraceElement[] {
  return traceModeNodes(traceState.proposals, traceState.modes, traceState.generation);
}

/** The Trace Links view's tree: the documents of the mode clicked last, then down to the items. */
class TraceTreeDataProvider implements vscode.TreeDataProvider<TraceElement> {
  readonly onDidChangeTreeData = traceTreeChanged.event;

  getTreeItem(element: TraceElement): vscode.TreeItem {
    return traceTreeItem(element);
  }

  getChildren(element?: TraceElement): TraceElement[] {
    if (element === undefined) {
      return traceViewRoot(
        traceState.proposals,
        traceState.refusal,
        traceState.modes,
        traceState.mode,
        traceState.generation,
      );
    }
    return traceElementChildren(element);
  }
}

/** The children of one trace element, read from the file as it is now. */
function traceElementChildren(element: TraceElement): TraceElement[] {
  const proposals = traceState.proposals;
  if (element.kind === "message" || element.kind === "apply" || element.mode === undefined) {
    return [];
  }
  const { mode } = element;
  if (element.kind === "mode") {
    return traceDocuments(proposals, traceState.modes, mode, traceState.generation);
  }
  if (proposals === undefined) {
    return [];
  }
  if (element.kind === "document") {
    return traceRequirements(proposals, mode, element.document ?? null, traceState.generation);
  }
  if (element.kind === "requirement" || element.kind === "more") {
    const entry = proposals.requirements.find((candidate) => candidate.id === element.requirement);
    if (entry === undefined) {
      return [];
    }
    return element.kind === "more"
      ? traceMoreChildren(entry, mode, traceState.generation)
      : traceChildren(entry, mode, traceState.generation);
  }
  return [];
}

/**
 * Set up the Trace Links tree's state. Called from `activate()` after `registerTreeView()`.
 * The remembered proposals file and document sets are shown again, and
 * `swcraft.trace.available` follows the project's detection.
 */
export function registerTraceView(context: vscode.ExtensionContext): void {
  traceContext = context;
  traceState.modes = readModes(context.workspaceState.get(TRACE_MODES_KEY));
  traceState.mode = readMode(context.workspaceState.get(TRACE_MODE_KEY));
  traceView = vscode.window.createTreeView(TRACE_VIEW_ID, {
    treeDataProvider: new TraceTreeDataProvider(),
    manageCheckboxStateManually: true,
    showCollapseAll: true,
  });
  const available = (): void => {
    const projectDir = traceProjectDir();
    void vscode.commands.executeCommand(
      "setContext",
      "swcraft.trace.available",
      projectDir !== undefined,
    );
    const remembered = context.workspaceState.get<string>(TRACE_FILE_KEY);
    if (
      projectDir !== undefined &&
      typeof remembered === "string" &&
      traceState.file === undefined
    ) {
      const file = path.resolve(projectDir, remembered);
      if (fs.existsSync(file)) {
        loadTraceFile(file);
      }
    }
  };
  context.subscriptions.push(
    traceView,
    traceTreeChanged,
    traceView.onDidChangeCheckboxState((event) => traceCheckboxChanged(event.items)),
    vscode.workspace.onDidChangeWorkspaceFolders(available),
    { dispose: () => traceWatcher?.dispose() },
  );
  setTraceBusy(false);
  available();
  refreshTraceView();
}

/** The node a view command was invoked on, or `undefined` for anything else. */
function traceNodeOf(node: unknown): TraceNode | undefined {
  const element = traceElementOf(node);
  return element !== undefined && "item" in element ? element : undefined;
}

/**
 * The trace node a command was invoked on: a context menu hands over the panel's
 * own element (`{kind: "trace", element}`), a click the node itself.
 */
function traceElementOf(node: unknown): TraceNode | undefined {
  if (typeof node !== "object" || node === null) {
    return undefined;
  }
  const candidate =
    (node as { kind?: unknown }).kind === "trace" ? (node as { element: unknown }).element : node;
  return typeof candidate === "object" &&
    candidate !== null &&
    (candidate as { kind?: unknown }).kind !== "message" &&
    typeof (candidate as { id?: unknown }).id === "string"
    ? (candidate as TraceNode)
    : undefined;
}

/** `swcraft.traceAccept`: tick a row from the context menu. */
function traceAcceptCommand(node: unknown): void {
  if (resolveManifestOrNotify() === undefined) {
    return;
  }
  const item = traceNodeOf(node)?.item;
  if (item?.slot === "candidates") {
    applyTraceEdit((proposals) => tick(proposals, item.requirement, item.target));
  }
}

/** `swcraft.traceReject`: reject a row (the context menu and the inline ✕). */
function traceRejectCommand(node: unknown): void {
  if (resolveManifestOrNotify() === undefined) {
    return;
  }
  const item = traceNodeOf(node)?.item;
  if (item?.slot === "candidates") {
    applyTraceEdit((proposals) => reject(proposals, item.requirement, item.target));
  }
}

/** `swcraft.traceUndoReject`: undo a rejection (the context menu and the inline ↺). */
function traceUndoRejectCommand(node: unknown): void {
  if (resolveManifestOrNotify() === undefined) {
    return;
  }
  const item = traceNodeOf(node)?.item;
  if (item?.slot === "candidates") {
    applyTraceEdit((proposals) => undoReject(proposals, item.requirement, item.target));
  }
}

/** `swcraft.traceRemoveChoice`: remove a `chosen` entry. */
function traceRemoveChoiceCommand(node: unknown): void {
  if (resolveManifestOrNotify() === undefined) {
    return;
  }
  const item = traceNodeOf(node)?.item;
  if (item?.slot === "chosen") {
    applyTraceEdit((proposals) => removeChoice(proposals, item.requirement, item.target));
  }
}

/** `swcraft.traceMarkStrong`: preselect every strong, undecided row of the Review documents. */
function traceMarkStrongCommand(): void {
  if (resolveManifestOrNotify() === undefined) {
    return;
  }
  applyTraceEdit((proposals) => {
    const { file, count } = markStrong(proposals, traceState.modes.review);
    void vscode.window.showInformationMessage(
      count === 0
        ? "SWCraft: no strong, undecided row to preselect."
        : `SWCraft: ${count} strong row${count === 1 ? "" : "s"} preselected; untick any you do not accept.`,
    );
    return { ok: true, file };
  });
}

/** `swcraft.traceClear`: undo every decision not yet applied, after a modal. */
async function traceClearCommand(): Promise<void> {
  if (resolveManifestOrNotify() === undefined || traceState.proposals === undefined) {
    return;
  }
  const { count } = clearDecisions(traceState.proposals);
  if (count === 0) {
    void vscode.window.showInformationMessage("SWCraft: there is no decision to clear.");
    return;
  }
  const answer = await vscode.window.showWarningMessage(
    clearQuestion(count),
    { modal: true },
    "Clear",
  );
  if (answer === "Clear") {
    applyTraceEdit((proposals) => ({ ok: true, file: clearDecisions(proposals).file }));
  }
}

/** `swcraft.traceOpenProposals`: the proposals file in a normal editor, the queue flushed first. */
async function traceOpenProposalsCommand(): Promise<void> {
  if (resolveManifestOrNotify() === undefined || traceState.file === undefined) {
    return;
  }
  await flushTraceWrites();
  await vscode.window.showTextDocument(vscode.Uri.file(traceState.file), { preview: false });
}

/** `swcraft.traceOpenReport`: the search's `report.md`, the proposals file's sibling, in the preview. */
async function traceOpenReportCommand(): Promise<void> {
  if (resolveManifestOrNotify() === undefined || traceState.file === undefined) {
    return;
  }
  const report = path.join(path.dirname(traceState.file), "report.md");
  if (!isFile(report)) {
    void vscode.window.showWarningMessage(
      "SWCraft: the search's report.md is not on disk any more.",
    );
    return;
  }
  await vscode.commands.executeCommand("markdown.showPreview", vscode.Uri.file(report));
}

/**
 * `swcraft.traceOpenElement` and a row's click: the element's file at its line,
 * nothing else. A lexical reading of the saved files, navigation's own.
 */
async function traceOpenElementCommand(node: unknown): Promise<void> {
  const resolved = resolveManifestOrNotify();
  const item = traceNodeOf(node)?.item;
  const modelRoot = currentModelRoot();
  if (
    resolved === undefined ||
    item === undefined ||
    modelRoot === undefined ||
    traceContext === undefined
  ) {
    return;
  }
  const refs = loadReferences(
    fs.readFileSync(referencesArtifactPath(traceContext), { encoding: "utf8" }),
  );
  const element = traceElementLocation(refs, item.target, modelRoot, fsRequestReader());
  if (element === undefined) {
    void vscode.window.showWarningMessage(notFoundMessage(item.target));
    return;
  }
  await showPreviewTab(element.file, element.line);
}

/**
 * Every file the Trace Links view and the Resources view open: one preview tab in the
 * active editor group, the focus kept in the view, so the next click replaces it. Never a
 * split.
 */
async function showPreviewTab(file: string, line: number): Promise<void> {
  await vscode.window.showTextDocument(vscode.Uri.file(file), {
    preview: true,
    preserveFocus: true,
    selection: new vscode.Range(line, 0, line, 0),
    viewColumn: vscode.ViewColumn.Active,
  });
}

/**
 * The Apply flow, in the imports' dry-run sequence: flush, then `traceApplyFlow()`: the dry
 * run's report in the *Trace Apply Report* tab with **Write** and **Cancel**, the applied run
 * on Write bound to the previewed plan by `--expect-plan`, then the proposals reloaded. Each
 * run writes its Markdown report to a temporary `--report` file. Busy is held from before
 * the flush through the wait for Write, unless *reserved* (the calling search flow already
 * holds it). The CLI re-checks every decision.
 */
async function traceApplyCommandFlow(
  resolved: { workspaceRoot: string; manifestRelPath: string },
  outputChannel: vscode.OutputChannel,
  reserved = false,
): Promise<TraceApplyOutcome> {
  const file = traceState.file;
  if (file === undefined) {
    void vscode.window.showInformationMessage(
      "SWCraft: there is no proposals file yet; run a search first.",
    );
    return "stopped";
  }
  const refusal = traceState.busy && !reserved ? BUSY_REFUSAL : isDirtyRefusal(traceFileIsDirty());
  if (refusal !== undefined) {
    void vscode.window.showWarningMessage(refusal);
    return "stopped";
  }
  const { workspaceRoot, manifestRelPath } = resolved;
  const executable = resolveSwcraftExecutable();
  const commandId = "swcraft.traceApply";
  const label = SPAWNING_COMMAND_LABELS[commandId];
  const log = (line: string) => outputChannel.appendLine(line);
  // Busy is taken before the flush: a second Apply or search started while it is awaited is refused.
  return holdingTraceBusy(setTraceBusy, reserved, async () => {
    await flushTraceWrites();
    return traceApplyFlow(
      {
        tabs: importTabs,
        executable,
        render: importReportRendering,
        record: (run) => {
          recordCliOutcome(commandId, label, run);
        },
        reload: () => loadTraceFile(file),
        log,
        refuse: (message) => {
          void vscode.window.showWarningMessage(message);
        },
        writeRefusal: () => isDirtyRefusal(traceFileIsDirty()),
      },
      file,
      (options) => {
        outputChannel.show(true);
        return withRunIndicator(commandId, describeRunning(commandId), () =>
          withImportReportFile(
            workspaceRoot,
            (report) =>
              spawnCli(
                executable,
                buildTraceApplyArgv(manifestRelPath, file, { ...options, report }),
                workspaceRoot,
                outputChannel,
                { echoStdout: false },
              ),
            fs,
            { log },
          ),
        );
      },
    );
  });
}

/** `swcraft.traceApply`: the view's *Apply…*. */
async function traceApplyCommand(outputChannel: vscode.OutputChannel): Promise<void> {
  const resolved = resolveManifestOrNotify();
  if (resolved === undefined) {
    return;
  }
  await traceApplyCommandFlow(resolved, outputChannel);
}

/**
 * The trace kind of `experimentCommand`, the palette's *Trace Link Search…*: which mode,
 * then that mode's picker.
 */
async function traceSearchCommand(outputChannel: vscode.OutputChannel): Promise<void> {
  const picked = await vscode.window.showQuickPick(
    TRACE_MODES.map((mode) => ({ label: MODE_LABELS[mode], mode })),
    { title: "Trace links: which mode?" },
  );
  if (picked !== undefined) {
    await traceChooseModeCommand(picked.mode, outputChannel);
  }
}

/** Run *flow* holding busy for its whole length, so a second search, an Apply or a decision cannot start in between. */
async function withTraceBusy(flow: () => Promise<void>): Promise<void> {
  if (traceState.busy) {
    void vscode.window.showWarningMessage(BUSY_REFUSAL);
    return;
  }
  setTraceBusy(true);
  try {
    await flow();
  } finally {
    setTraceBusy(false);
  }
}

/** The configuration setting's file, or `null` after showing its refusal. */
function traceConfigFile(
  workspaceRoot: string,
  outputChannel: vscode.OutputChannel,
): string | undefined | null {
  const { setting } = EXPERIMENT_KINDS.trace;
  const config = vscode.workspace
    .getConfiguration(undefined, vscode.Uri.file(workspaceRoot))
    .get<unknown>(setting);
  const settingRefusal = experimentSettingRefusal(setting, config);
  if (settingRefusal !== undefined) {
    outputChannel.appendLine(settingRefusal);
    void vscode.window.showWarningMessage(settingRefusal);
    return null;
  }
  return typeof config === "string" ? config : undefined;
}

/** `--documents`: each document's links and the proposals file a search would replace, which is loaded. */
async function traceDocumentsAnswer(
  resolved: { workspaceRoot: string; manifestRelPath: string },
  projectDir: string,
  configFile: string | undefined,
  outputChannel: vscode.OutputChannel,
): Promise<TraceDocuments | undefined> {
  const { commandId, label } = EXPERIMENT_KINDS.trace;
  await flushTraceWrites();
  const listed = await withRunIndicator(commandId, describeRunning(commandId), () =>
    spawnCli(
      resolveSwcraftExecutable(),
      buildTraceDocumentsArgv(resolved.manifestRelPath, configFile),
      resolved.workspaceRoot,
      outputChannel,
      { echoStdout: false },
    ),
  );
  if (!recordCliOutcome(commandId, label, listed)) {
    return undefined;
  }
  let answer: TraceDocuments;
  try {
    answer = parseTraceDocuments(listed.stdout);
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error);
    outputChannel.appendLine(`SWCraft: ${detail}`);
    void vscode.window.showWarningMessage(`SWCraft: ${detail}`);
    return undefined;
  }
  if (answer.proposals !== null) {
    const previous = path.resolve(projectDir, answer.proposals);
    if (traceState.file === undefined || !sameFile(traceState.file, previous)) {
      loadTraceFile(previous);
    }
  }
  return answer;
}

/**
 * `swcraft.traceChooseMode`, a click on *Labelling* or *Review*: `--documents`, that mode's
 * picker, the question when the choice would hide unapplied decisions, then a search only
 * when the proposals file does not already give every chosen document its mode.
 */
async function traceChooseModeCommand(
  node: unknown,
  outputChannel: vscode.OutputChannel,
): Promise<void> {
  const mode: TraceMode | undefined =
    node === "labelling" || node === "review" ? node : traceElementOf(node)?.mode;
  const resolved = resolveManifestOrNotify();
  const projectDir = traceProjectDir();
  if (mode === undefined || resolved === undefined || projectDir === undefined) {
    return;
  }
  // The click switches the Trace Links view to this mode at once.
  await showTraceMode(mode);
  await withTraceBusy(async () => {
    const configFile = traceConfigFile(resolved.workspaceRoot, outputChannel);
    if (configFile === null) {
      return;
    }
    outputChannel.show(true);
    const answer = await traceDocumentsAnswer(resolved, projectDir, configFile, outputChannel);
    if (answer === undefined) {
      return;
    }
    // The modes as remembered, before the scope filters them: a document that left the
    // scope still counts as dropped when it holds decisions.
    const remembered = traceState.modes;
    const before = modesInScope(remembered, answer);
    const picked = await vscode.window.showQuickPick(modePickItems(answer, before, mode), {
      canPickMany: true,
      title: modePickerTitle(mode),
      placeHolder: "Documents with no link yet are listed first; press Enter to confirm",
    });
    if (picked === undefined) {
      return;
    }
    const { modes, leftOut } = chooseDocuments(
      before,
      mode,
      picked.map((item) => item.document),
      answer,
    );
    if (leftOut) {
      void vscode.window.showInformationMessage(GROUP_LEFT_OUT);
    }
    const dropped = droppedWithDecisions(traceState.proposals, remembered, modes);
    if (dropped.length > 0) {
      const choice = await vscode.window.showWarningMessage(
        droppedQuestion(dropped),
        { modal: true },
        PENDING_APPLY_BUTTON,
        DISCARD_THEM_BUTTON,
      );
      if (choice === PENDING_APPLY_BUTTON) {
        if ((await traceApplyCommandFlow(resolved, outputChannel, true)) !== "applied") {
          return;
        }
      } else if (choice === DISCARD_THEM_BUTTON) {
        const documents = dropped.map((item) => item.document);
        applyTraceEdit(
          (proposals) => ({ ok: true, file: clearDecisions(proposals, documents).file }),
          true,
        );
        await flushTraceWrites();
      } else {
        return;
      }
      // The documents leave the view only once nothing of theirs waits: a refused or failed
      // edit, or a link Apply left to a hand edit, keeps them.
      if (droppedWithDecisions(traceState.proposals, remembered, modes).length > 0) {
        void vscode.window.showWarningMessage(DROPPED_STILL_PENDING);
        return;
      }
    }
    if (!searchNeeded(traceState.proposals, modes, answer)) {
      setTraceModes(modes);
      return;
    }
    if (await traceSearchRun(resolved, projectDir, answer, modes, configFile, outputChannel)) {
      setTraceModes(modes);
    }
  });
}

/** `swcraft.traceNewSearch`: search again for the documents both modes hold. */
async function traceNewSearchCommand(outputChannel: vscode.OutputChannel): Promise<void> {
  const resolved = resolveManifestOrNotify();
  const projectDir = traceProjectDir();
  if (resolved === undefined || projectDir === undefined) {
    return;
  }
  await withTraceBusy(async () => {
    const configFile = traceConfigFile(resolved.workspaceRoot, outputChannel);
    if (configFile === null) {
      return;
    }
    outputChannel.show(true);
    const answer = await traceDocumentsAnswer(resolved, projectDir, configFile, outputChannel);
    if (answer === undefined) {
      return;
    }
    const modes = modesInScope(traceState.modes, answer);
    if (modes.labelling.length === 0 && modes.review.length === 0) {
      void vscode.window.showInformationMessage(
        "SWCraft: choose the documents first: click Labelling or Review under Trace Links.",
      );
      return;
    }
    if (await traceSearchRun(resolved, projectDir, answer, modes, configFile, outputChannel)) {
      setTraceModes(modes);
    }
  });
}

/**
 * One search for *modes*, its labelled documents as `--labelling`: the pending question
 * when the file holds unapplied decisions (*Apply…*, *Discard and Search*, Cancel), then the
 * search, whose proposals file is then shown. Answers whether the search ran and its file
 * was loaded. After *Apply…* the search continues only when the applied run succeeded and
 * nothing is pending, so the question cannot loop.
 */
async function traceSearchRun(
  resolved: { workspaceRoot: string; manifestRelPath: string },
  projectDir: string,
  answer: TraceDocuments,
  modes: TraceModes,
  configFile: string | undefined,
  outputChannel: vscode.OutputChannel,
): Promise<boolean> {
  const { workspaceRoot, manifestRelPath } = resolved;
  const { commandId, label } = EXPERIMENT_KINDS.trace;
  const executable = resolveSwcraftExecutable();
  let discardPending = false;
  if (answer.proposals !== null) {
    const previous = path.resolve(projectDir, answer.proposals);
    if (traceFileIsDirty()) {
      void vscode.window.showWarningMessage(DIRTY_REFUSAL);
      return false;
    }
    const bytes = readBytesOrNothing(previous);
    const read = bytes === undefined ? undefined : readProposals(bytes);
    const count = read === undefined ? 0 : isRefusal(read) ? undefined : pendingCount(read);
    if (count === undefined || count > 0) {
      const buttons =
        count === undefined
          ? [PENDING_DISCARD_BUTTON]
          : [PENDING_APPLY_BUTTON, PENDING_DISCARD_BUTTON];
      const choice = await vscode.window.showWarningMessage(
        pendingQuestion(count),
        { modal: true },
        ...buttons,
      );
      if (choice === PENDING_APPLY_BUTTON) {
        const outcome = await traceApplyCommandFlow(resolved, outputChannel, true);
        const after = traceState.proposals;
        if (outcome !== "applied" || after === undefined || pendingCount(after) > 0) {
          void vscode.window.showWarningMessage(STILL_PENDING);
          return false;
        }
      } else if (choice === PENDING_DISCARD_BUTTON) {
        discardPending = true;
      } else {
        return false;
      }
    }
  }
  const labelling = labellingArgumentOf(modes);
  const search = async (discard: boolean) => {
    await flushTraceWrites();
    // Again here: the buffer may have been edited during the questions and the picker.
    if (traceFileIsDirty()) {
      void vscode.window.showWarningMessage(DIRTY_REFUSAL);
      return undefined;
    }
    const argv = buildTraceSearchArgv(manifestRelPath, {
      config: configFile,
      labelling,
      discardPending: discard,
    });
    return withRunIndicator(commandId, describeRunning(commandId), () =>
      spawnCli(executable, argv, workspaceRoot, outputChannel),
    );
  };
  let ran = await search(discardPending);
  if (ran === undefined) {
    return false;
  }
  // The panel's reader is a subset of the CLI's: a file it read as holding nothing pending can
  // still be refused by the CLI. That refusal is answered here, once.
  if (ran.exitCode !== 0 && !discardPending && searchRefusedPending(ran.stderr)) {
    const choice = await vscode.window.showWarningMessage(
      SEARCH_REFUSED_PENDING,
      { modal: true },
      PENDING_DISCARD_BUTTON,
    );
    if (choice === PENDING_DISCARD_BUTTON) {
      ran = await search(true);
      if (ran === undefined) {
        return false;
      }
    }
  }
  if (!recordCliOutcome(commandId, label, ran)) {
    return false;
  }
  const written = proposalsPathOf(ran.stdout, workspaceRoot);
  if (written === undefined) {
    outputChannel.appendLine(
      "SWCraft: the search printed no proposals file, so there is nothing to show",
    );
    return false;
  }
  loadTraceFile(written);
  await vscode.commands.executeCommand(`${TRACE_VIEW_ID}.focus`);
  return true;
}

/**
 * `swcraft.traceOtherElement`, the *Any element of the model…* row: straight to the model's
 * elements. The search's other proposals are rows under *More candidates (N)*.
 */
async function traceOtherElementCommand(
  node: unknown,
  outputChannel: vscode.OutputChannel,
): Promise<void> {
  if (resolveManifestOrNotify() === undefined) {
    return;
  }
  await traceChooseElementCommand(node, outputChannel);
}

/**
 * `swcraft.traceOpenRequirement`, a requirement row's click: its file in the
 * view's one preview tab (`showPreviewTab`). The file holds the whole text.
 */
async function traceOpenRequirementCommand(node: unknown): Promise<void> {
  const requirement = traceElementOf(node)?.requirement;
  const modelRoot = currentModelRoot();
  if (
    resolveManifestOrNotify() === undefined ||
    requirement === undefined ||
    modelRoot === undefined ||
    traceContext === undefined
  ) {
    return;
  }
  const refs = loadReferences(
    fs.readFileSync(referencesArtifactPath(traceContext), { encoding: "utf8" }),
  );
  const found = requirementLocation(refs, requirement, modelRoot, fsRequestReader());
  if (found === undefined) {
    void vscode.window.showWarningMessage(
      `SWCraft: the file of requirement ${requirement} was not found.`,
    );
    return;
  }
  await showPreviewTab(found.file, found.line);
}

/**
 * `swcraft.traceChooseElement`: an element outside the requirement's list, from `swcraft
 * list` (the five linkable layers) or a component's ports and runnables read lexically from
 * its file, appended as a `chosen` entry.
 */
async function traceChooseElementCommand(
  node: unknown,
  outputChannel: vscode.OutputChannel,
): Promise<void> {
  const resolved = resolveManifestOrNotify();
  const requirement = traceElementOf(node)?.requirement;
  const modelRoot = currentModelRoot();
  if (
    resolved === undefined ||
    requirement === undefined ||
    modelRoot === undefined ||
    traceContext === undefined
  ) {
    return;
  }
  const commandId = "swcraft.traceChooseElement";
  const executable = resolveSwcraftExecutable();
  const listing = await withRunIndicator(commandId, describeRunning(commandId), () =>
    listModel(
      executable,
      resolved.workspaceRoot,
      resolved.manifestRelPath,
      undefined,
      outputChannel,
    ),
  );
  if (listing.kind !== "listed") {
    void vscode.window.showWarningMessage(
      "SWCraft: the model's elements could not be listed; see the SWCraft output channel.",
    );
    return;
  }
  const refs = loadReferences(
    fs.readFileSync(referencesArtifactPath(traceContext), { encoding: "utf8" }),
  );
  const reader = fsRequestReader();
  const picked = await vscode.window.showQuickPick(
    chooseCandidates(listing.listings, refs, modelRoot, reader),
    {
      title: `Choose an element for ${requirement}`,
      matchOnDescription: true,
    },
  );
  if (picked === undefined) {
    return;
  }
  let address = picked.address;
  if (address.kind === "swc") {
    const facts = refs.layers.get("swc");
    const found =
      facts === undefined ? undefined : resolveElement(facts, address.name, modelRoot, reader);
    const text = found === undefined ? undefined : reader.readText(found.file);
    const member = await vscode.window.showQuickPick(
      text === undefined ? [] : componentMembers(refs, address.name, text),
      { title: `${address.name}: the component itself, or one of its ports and runnables` },
    );
    if (member === undefined) {
      return;
    }
    address = member.address;
  }
  applyTraceEdit((proposals) => choose(proposals, requirement, address));
}

// --- The panel review: the review tab and the Pending Change Sets view --------
//
// The thin host of `reviewCore.ts`. A server started with `SWCRAFT_REVIEW=panel` writes each
// question it waits on into `<SWCraft home>/reviews/`; this window shows the live ones of
// its own project in the `swcraftReview` view and opens one review tab per question, without
// taking the focus or notifying. The tab's Approve and Reject buttons are the only way to
// answer: no command approves or rejects, and a click answers the tab's own question, never
// the message's.

/** One open review tab: the question it was opened for and the text the host put into it. */
interface ReviewTab {
  panel: vscode.WebviewPanel;
  record: PendingRecord;
  /** The line its buttons were disabled with, re-sent when a hidden tab is shown again. */
  disabled?: string;
}

const reviewTabs = new Map<string, ReviewTab>();
/**
 * The record this window last opened a tab for at each key, by `reviewIdentity`: a re-read
 * never reopens it, and a record of another identity at the same key is new.
 */
const reviewOpened = new Map<string, string>();
let reviewRecords: PendingRecord[] = [];
let reviewView: vscode.TreeView<ReviewNode> | undefined;
const reviewTreeChanged = new vscode.EventEmitter<ReviewNode | undefined>();

/** `<SWCraft home>/reviews` on this machine. */
function currentReviewsDir(): string {
  const platform = currentPlatform();
  return reviewsDir(toolchainHome(process.env, os.homedir(), platform), platform);
}

/**
 * The real paths of this window's manifests, one per workspace folder whose root holds a
 * `swcraft.yaml` (the folders `mcpProvider.ts` registers a server for). Empty when no
 * project is detected.
 */
function reviewManifestRealPaths(): string[] {
  const manifests: string[] = [];
  for (const folder of vscode.workspace.workspaceFolders ?? []) {
    const manifest = findManifest(folder.uri.fsPath);
    if (manifest === undefined) {
      continue;
    }
    try {
      manifests.push(fs.realpathSync.native(manifest));
    } catch {
      // Gone between the probe and the resolution: the next re-read decides.
    }
  }
  return manifests;
}

/** A record's `manifest` with the letter case on disk, or the value itself when it does not resolve. */
function recordManifestRealPath(manifest: string): string {
  try {
    return fs.realpathSync.native(manifest);
  } catch {
    return manifest;
  }
}

/**
 * The reviews directory's pending records and the keys its listing named; an absent or
 * unreadable directory holds none and lists `undefined`. Only `*.pending.json` names are
 * read: the directory is shared by every project's server.
 */
function readReviewEntries(dir: string): {
  entries: ReviewEntry[];
  listed: Set<string> | undefined;
} {
  let names: string[];
  try {
    names = fs.readdirSync(dir);
  } catch {
    return { entries: [], listed: undefined };
  }
  const entries: ReviewEntry[] = [];
  for (const name of names.filter(isPendingFileName)) {
    const entry = readReviewEntry(fs, dir, name);
    if (entry !== undefined) {
      entries.push(entry);
    }
  }
  return { entries, listed: listedReviewKeys(names) };
}

/** The live records of *manifests*' projects among *entries*, as `windowReviews` decides. */
function liveReviews(
  entries: readonly ReviewEntry[],
  manifests: readonly string[],
): PendingRecord[] {
  if (manifests.length === 0) {
    return [];
  }
  return windowReviews(entries, manifests, currentPlatform(), Date.now(), recordManifestRealPath);
}

/** Disable *tab*'s buttons with *line*. */
function disableReviewTab(tab: ReviewTab, line: string): void {
  tab.disabled = line;
  void tab.panel.webview.postMessage({ type: "disable", line });
}

/** Render *tab*'s page afresh, which enables its buttons, and forget its disabled line. */
function renderReviewTab(tab: ReviewTab): void {
  tab.disabled = undefined;
  tab.panel.webview.html = reviewHtml(tab.record, {
    nonce: newNonce(),
    utcOffsetMinutes: localUtcOffsetMinutes(tab.record.created),
  });
}

/**
 * Re-read the directory: redraw the view and its badge, then apply `reviewTransitions`:
 * dispose a tab whose key now holds another record, open a record new to this window,
 * disable the tab of one that vanished and enable one whose own record is back.
 */
function rereadReviews(manifests: readonly string[] = reviewManifestRealPaths()): void {
  void vscode.commands.executeCommand("setContext", REVIEW_AVAILABLE_KEY, manifests.length > 0);
  // No project: the directory is not listed, so nothing is forgotten either.
  const read = manifests.length === 0 ? undefined : readReviewEntries(currentReviewsDir());
  reviewRecords = read === undefined ? [] : liveReviews(read.entries, manifests);
  if (reviewView !== undefined) {
    const tooltip = reviewBadgeTooltip(reviewRecords.length);
    reviewView.badge = tooltip === undefined ? undefined : { value: reviewRecords.length, tooltip };
  }
  reviewTreeChanged.fire(undefined);
  const byKey = new Map(reviewRecords.map((record) => [reviewKey(record), record]));
  const live = new Map([...byKey].map(([key, record]) => [key, reviewIdentity(record)]));
  const tabs = new Map(
    [...reviewTabs].map(([key, tab]) => [
      key,
      { identity: reviewIdentity(tab.record), disabled: tab.disabled },
    ]),
  );
  const transitions = reviewTransitions(live, reviewOpened, tabs, read?.listed);
  for (const key of transitions.dispose) {
    const tab = reviewTabs.get(key);
    if (tab !== undefined) {
      reviewTabs.delete(key);
      tab.panel.dispose();
    }
  }
  for (const key of transitions.open) {
    const record = byKey.get(key);
    if (record !== undefined) {
      openReviewTab(record);
    }
  }
  for (const key of transitions.disable) {
    const tab = reviewTabs.get(key);
    if (tab !== undefined) {
      disableReviewTab(tab, NO_LONGER_WAITING);
    }
  }
  for (const key of transitions.enable) {
    const tab = reviewTabs.get(key);
    if (tab !== undefined) {
      renderReviewTab(tab);
    }
  }
  for (const key of transitions.forget) {
    reviewOpened.delete(key);
  }
}

/**
 * Open *record*'s tab, or reveal this very record's open tab; a tab at the key that rendered
 * another record is disposed first. The focus stays where it is.
 */
function openReviewTab(record: PendingRecord): void {
  const key = reviewKey(record);
  reviewOpened.set(key, reviewIdentity(record));
  const open = reviewTabs.get(key);
  if (open !== undefined && sameReview(open.record, record)) {
    open.panel.reveal(undefined, true);
    return;
  }
  if (open !== undefined) {
    reviewTabs.delete(key);
    open.panel.dispose();
  }
  const panel = vscode.window.createWebviewPanel(
    REVIEW_TAB_VIEW_TYPE,
    reviewTabTitle(record),
    { viewColumn: vscode.ViewColumn.Active, preserveFocus: true },
    { enableScripts: true, localResourceRoots: [] },
  );
  const tab: ReviewTab = { panel, record };
  reviewTabs.set(key, tab);
  renderReviewTab(tab);
  panel.webview.onDidReceiveMessage((message: unknown) => answerReviewTab(tab, message));
  // A hidden webview is rebuilt from its HTML when shown again, which would enable its
  // buttons once more: the disabled line is sent again.
  panel.onDidChangeViewState(() => {
    if (panel.visible && tab.disabled !== undefined) {
      disableReviewTab(tab, tab.disabled);
    }
  });
  panel.onDidDispose(() => {
    // A tab disposed for a newer record at its key must not remove that record's tab.
    if (reviewTabs.get(key) === tab) {
      reviewTabs.delete(key);
    }
  });
}

/**
 * A click on *tab*: the question is the tab's own, re-read from disk now; the decision is
 * written only as `answerPlan` says, first answer winning. The tab closes on success and
 * says why otherwise.
 */
function answerReviewTab(tab: ReviewTab, message: unknown): void {
  const answer = parseTabAnswer(message);
  if (answer === undefined) {
    return;
  }
  if (tab.disabled !== undefined) {
    // A click on a disabled tab whose page was rebuilt before the re-sent disable line
    // reached it answers nothing.
    disableReviewTab(tab, tab.disabled);
    return;
  }
  const dir = currentReviewsDir();
  const entry = readReviewEntry(fs, dir, pendingFileName(tab.record));
  const fresh =
    entry === undefined ? undefined : liveReviews([entry], reviewManifestRealPaths())[0];
  const plan = answerPlan(tab.record, fresh, answer);
  if (!plan.ok) {
    disableReviewTab(tab, plan.reason);
    return;
  }
  const written = writeDecision(fs, dir, plan.record, plan.decision, plan.digest);
  if (!written.ok && written.live === true) {
    // An answer to an earlier copy of the question was still at the name: this question is
    // unanswered, so the buttons stay live.
    void tab.panel.webview.postMessage({ type: "note", line: written.reason });
    return;
  }
  if (!written.ok) {
    disableReviewTab(tab, written.reason);
    return;
  }
  tab.panel.dispose();
  rereadReviews();
}

class ReviewTreeDataProvider implements vscode.TreeDataProvider<ReviewNode> {
  readonly onDidChangeTreeData = reviewTreeChanged.event;

  getTreeItem(node: ReviewNode): vscode.TreeItem {
    const item = new vscode.TreeItem(node.label, vscode.TreeItemCollapsibleState.None);
    item.id = node.key;
    item.description = node.description;
    item.tooltip = node.tooltip;
    item.iconPath = new vscode.ThemeIcon("git-pull-request");
    item.contextValue = "review.row";
    item.command = {
      command: REVIEW_OPEN_COMMAND,
      title: "Open Review",
      arguments: [{ changeSetId: node.changeSetId, question: node.question }],
    };
    return item;
  }

  getChildren(node?: ReviewNode): ReviewNode[] {
    if (node !== undefined) {
      return [];
    }
    return reviewNodes(reviewRecords, localUtcOffsetMinutes(new Date().toISOString()));
  }
}

/**
 * `swcraft.reviewOpen`: a row's click. Its argument only chooses which tab opens; the record
 * is resolved from disk.
 */
function reviewOpenCommand(node: unknown): void {
  // Spelled out in both commands, not shared: the guard that derives which commands need a
  // project reads `resolveManifestOrNotify()` in the handler's own body.
  const manifests = reviewManifestRealPaths();
  if (manifests.length === 0) {
    // No folder of this window is a project: the usual notification says so.
    resolveManifestOrNotify();
    return;
  }
  rereadReviews(manifests);
  const wanted = node as { changeSetId?: unknown; question?: unknown } | undefined;
  const record = reviewRecords.find(
    (candidate) =>
      candidate.change_set_id === wanted?.changeSetId && candidate.question === wanted?.question,
  );
  if (record !== undefined) {
    openReviewTab(record);
  }
}

/** `swcraft.reviewRefresh`: the view's title-bar Refresh. */
function reviewRefreshCommand(): void {
  const manifests = reviewManifestRealPaths();
  if (manifests.length === 0) {
    resolveManifestOrNotify();
    return;
  }
  rereadReviews(manifests);
}

/**
 * Set up the panel review: the Pending Change Sets view and its badge, the watcher over the
 * reviews directory, the re-reads (when the view becomes visible, when the window regains
 * focus, and every three seconds while a project is detected), and the two view commands.
 * `workspaceContains:swcraft.yaml` activates the extension, so a waiting question's tab
 * opens without the SWCraft view being shown.
 */
export function registerReviewView(context: vscode.ExtensionContext): void {
  reviewView = vscode.window.createTreeView(REVIEW_VIEW_ID, {
    treeDataProvider: new ReviewTreeDataProvider(),
  });
  const watcher = vscode.workspace.createFileSystemWatcher(
    new vscode.RelativePattern(vscode.Uri.file(currentReviewsDir()), `*${PENDING_SUFFIX}`),
  );
  // Each handler is wrapped: an event's argument must never reach `manifests`.
  watcher.onDidCreate(() => rereadReviews());
  watcher.onDidChange(() => rereadReviews());
  watcher.onDidDelete(() => rereadReviews());
  // A watcher that misses an event delays a tab by seconds, never loses a question: the
  // listing is a handful of files.
  const timer = setInterval(() => {
    const manifests = reviewManifestRealPaths();
    if (manifests.length > 0) {
      rereadReviews(manifests);
    }
  }, REVIEW_REREAD_MS);
  context.subscriptions.push(
    reviewView,
    reviewTreeChanged,
    watcher,
    { dispose: () => clearInterval(timer) },
    reviewView.onDidChangeVisibility((event) => {
      if (event.visible) {
        rereadReviews();
      }
    }),
    vscode.window.onDidChangeWindowState((state) => {
      if (state.focused) {
        rereadReviews();
      }
    }),
    vscode.workspace.onDidChangeWorkspaceFolders(() => rereadReviews()),
    // Spelled out, as every registration is, so the manifest tests read them.
    vscode.commands.registerCommand("swcraft.reviewOpen", (node) => reviewOpenCommand(node)),
    vscode.commands.registerCommand("swcraft.reviewRefresh", () => reviewRefreshCommand()),
  );
  rereadReviews();
}

// --- The Resources view ------------------------------------------------------------
//
// `swcraft resource-report --format json` drawn as a tree, in the Trace Links view's manner:
// one preview tab per click, nothing re-run on its own but the one refresh after a green
// Build. Every label, icon, colour and click target is resourceCore.ts's; this half runs the
// report, keeps the last document and maps nodes to `TreeItem`s.

interface ResourceState {
  /** The last document the view could read; kept across a failed run. */
  document?: ResourceDocument;
  /**
   * When the run that read *document* started: a change stamped after it marks the
   * document, even one made during the run.
   */
  readAt?: number;
  /** The newest model change the watch stamped (`noteModelChange()`), whatever is shown. */
  newestChangeAt?: number;
  /**
   * The workspace folder the view's state belongs to, so a folder change drops it: a run
   * answers only in the folder open when it ends.
   */
  root?: string;
  /** The last run failed: a non-zero exit, or a document the view could not read. */
  failed: boolean;
  /** A run of the report is in progress. */
  busy: boolean;
}

const resourceState: ResourceState = { failed: false, busy: false };
/**
 * The refresh a green Build ends with, set for its length; it never rejects. A press of
 * Resource Report and the view's Refresh await it rather than meeting `BUSY_REFUSAL`, and
 * `toolchainCommand()` refuses while it is set, since it runs outside `viewsRootWrites`.
 */
let resourceRefreshRun: Promise<void> | undefined;
let resourceContext: vscode.ExtensionContext | undefined;
/** The Resources view (`swcraftResources`), set by `registerResourceView()`. */
let resourceView: vscode.TreeView<ResourceNode> | undefined;
const resourceTreeChanged = new vscode.EventEmitter<ResourceNode | undefined>();
const RESOURCES_VIEW_ID = "swcraftResources";
/** The tree as last drawn. */
let resourceTree: ResourceTree = { description: "", nodes: [] };

/** Redraw the view from the state. */
function refreshResourceView(): void {
  resourceTree = resourceNodes(
    resourceState.document,
    isOutOfDate(resourceReportState()),
    resourceState.failed,
  );
  if (resourceView !== undefined) {
    resourceView.description = resourceTree.description;
  }
  resourceTreeChanged.fire(undefined);
  // The Model section's Resource Report item and the badge read the same state.
  artifactsChanged();
}

function setResourcesBusy(busy: boolean): void {
  resourceState.busy = busy;
  void vscode.commands.executeCommand("setContext", "swcraft.resources.busy", busy);
}

/**
 * The shown report's state, by the artifacts' rule: the one state the view's description,
 * the Model item and the badge read.
 */
function resourceReportState(): ArtifactState | undefined {
  return decideResourceReportState({
    readAt: resourceState.readAt,
    newestChangeAt: resourceState.newestChangeAt,
  });
}

/**
 * A change under the model root or to the manifest, stamped at *changedAt*. The newest
 * stamp is kept whether or not a report is shown, so a change made during a run marks the
 * document that run shows. Nothing re-runs: a save regenerates nothing.
 */
function markResourcesOutOfDate(changedAt: number): void {
  const before = resourceReportState();
  if (resourceState.newestChangeAt === undefined || changedAt > resourceState.newestChangeAt) {
    resourceState.newestChangeAt = changedAt;
  }
  if (resourceReportState() !== before) {
    refreshResourceView();
  }
}

/** One node as a `TreeItem`: resourceCore decided every part, this only maps it. */
function resourceTreeItem(node: ResourceNode): vscode.TreeItem {
  const collapsible = {
    none: vscode.TreeItemCollapsibleState.None,
    collapsed: vscode.TreeItemCollapsibleState.Collapsed,
    expanded: vscode.TreeItemCollapsibleState.Expanded,
  }[node.collapsible];
  const item = new vscode.TreeItem(node.label, collapsible);
  item.id = node.id;
  item.description = node.description;
  item.tooltip = node.tooltip === "" ? undefined : node.tooltip;
  item.contextValue = node.contextValue;
  if (node.icon !== undefined) {
    item.iconPath = new vscode.ThemeIcon(
      node.icon.id,
      node.icon.color === undefined ? undefined : new vscode.ThemeColor(node.icon.color),
    );
  }
  if (node.open !== undefined) {
    // The click opens the row's file; a row with children still expands by its arrow.
    item.command = { command: "swcraft.resourcesOpen", title: "Open Element", arguments: [node] };
  }
  return item;
}

/** The Resources view's tree: the nodes `resourceNodes()` drew last. */
class ResourceTreeDataProvider implements vscode.TreeDataProvider<ResourceNode> {
  readonly onDidChangeTreeData = resourceTreeChanged.event;

  getTreeItem(node: ResourceNode): vscode.TreeItem {
    return resourceTreeItem(node);
  }

  getChildren(node?: ResourceNode): ResourceNode[] {
    return node === undefined ? resourceTree.nodes : node.children;
  }
}

/**
 * Set up the Resources view. `swcraft.resources.available` follows the project's detection.
 * Nothing is run here: the view shows its welcome until Resource Report or a green Build
 * fills it.
 */
export function registerResourceView(context: vscode.ExtensionContext): void {
  resourceContext = context;
  resourceView = vscode.window.createTreeView(RESOURCES_VIEW_ID, {
    treeDataProvider: new ResourceTreeDataProvider(),
    showCollapseAll: true,
  });
  context.subscriptions.push(
    resourceView,
    resourceTreeChanged,
    vscode.workspace.onDidChangeWorkspaceFolders(resourceFoldersChanged),
  );
  setResourcesBusy(false);
  void updateResourcesAvailable();
  refreshResourceView();
}

/** `swcraft.resources.available`: whether the window holds a project, the view's `when`. */
async function updateResourcesAvailable(): Promise<void> {
  await vscode.commands.executeCommand(
    "setContext",
    "swcraft.resources.available",
    traceProjectDir() !== undefined,
  );
}

/**
 * A workspace-folder change: the view's `when` follows the project, and a report read in a
 * folder no longer open first is dropped with its stamp (it is another project's). A report
 * of the folder still open first stays.
 */
function resourceFoldersChanged(): void {
  void updateResourcesAvailable();
  if (resourceState.root === undefined || resourceState.root === getWorkspaceRoot()) {
    return;
  }
  resourceState.document = undefined;
  resourceState.readAt = undefined;
  resourceState.root = undefined;
  resourceState.failed = false;
  refreshResourceView();
}

/** Run *flow* holding busy for its whole length, as `withTraceBusy()` does. */
async function withResourcesBusy(flow: () => Promise<void>): Promise<void> {
  if (resourceState.busy) {
    void vscode.window.showWarningMessage(BUSY_REFUSAL);
    return;
  }
  setResourcesBusy(true);
  try {
    await flow();
  } finally {
    setResourcesBusy(false);
  }
}

/**
 * One run of `swcraft resource-report --format json`, with `--report` when *options* ask
 * for the file, recorded under `swcraft.resourceReport` whichever command started it. The
 * stdout is the document, never echoed; the channel gets the echo, the stderr and one
 * closing line. A non-zero exit or an unreadable document keeps the last good tree, marked
 * failed. A shown document is stamped with the run's start, so a change made during the run
 * keeps it *out of date*. A run that ends after the first folder changed is shown nowhere.
 * Answers the run's stderr (where a `--report` run's `Generated report:` line is) when the
 * document was shown, else `undefined`, so no failure opens a tab. The caller holds busy.
 */
async function resourceReportRun(
  resolved: { workspaceRoot: string; manifestRelPath: string },
  options: ResourceReportOptions,
  outputChannel: vscode.OutputChannel,
): Promise<string | undefined> {
  const commandId = COMMAND_IDS["resource-report"];
  const label = SPAWNING_COMMAND_LABELS[commandId];
  const argv = resourceReportJsonArgv(
    buildCliArgv("resource-report", resolved.manifestRelPath),
    options,
  );
  const startedAt = Date.now();
  const run = await withRunIndicator(commandId, describeRunning(commandId), () =>
    spawnCli(resolveSwcraftExecutable(), argv, resolved.workspaceRoot, outputChannel, {
      echoStdout: false,
    }),
  );
  const read = run.exitCode === 0 ? parseResourceDocument(run.stdout) : undefined;
  outputChannel.appendLine(resourceRunLine(run.exitCode, read, options));
  if (resolved.workspaceRoot !== getWorkspaceRoot()) {
    // The first folder changed during the run: its answer is the project
    // `resourceFoldersChanged()` dropped, so the view keeps the state that change left.
    recordCliOutcome(commandId, label, run);
    return undefined;
  }
  if (read === undefined || isResourceRefusal(read)) {
    if (read === undefined) {
      recordCliOutcome(commandId, label, run);
    } else {
      recordCommandStatus(commandId, { kind: "failure", message: `SWCraft: ${read.error}` });
      void vscode.window.showWarningMessage(`SWCraft: ${read.error}`);
    }
    resourceState.failed = true;
    // The failure belongs to this folder, as a shown document does: a folder change drops it.
    resourceState.root = resolved.workspaceRoot;
    refreshResourceView();
    return undefined;
  }
  recordCliOutcome(commandId, label, run);
  resourceState.document = read;
  resourceState.readAt = startedAt;
  resourceState.root = resolved.workspaceRoot;
  resourceState.failed = false;
  refreshResourceView();
  return run.stderr;
}

/** Reveal the view (`swcraftResources.focus`), the focus left on it. */
async function revealResources(): Promise<void> {
  // The view's `when` is set, and awaited, before the focus, which a still-hidden view
  // would refuse (a project onboarded after activation).
  await updateResourcesAvailable();
  await vscode.commands.executeCommand(`${RESOURCES_VIEW_ID}.focus`);
}

/**
 * `swcraft.resourceReport` (*report* true) and `swcraft.resourcesRefresh` (the view's ↻,
 * *report* false): run the report and reveal the view; a press then opens the report in a
 * tab, last, so the focus ends on it. A failed run, or one from a folder no longer open
 * first, opens no tab. During the post-Build refresh neither is refused: both wait for it;
 * Refresh then starts no second run, while a press runs its own, since that refresh wrote no
 * report.
 */
async function resourceReportCommand(
  outputChannel: vscode.OutputChannel,
  { report }: ResourceReportOptions,
): Promise<void> {
  const resolved = resolveManifestOrNotify();
  if (resolved === undefined) {
    return;
  }
  if (resourceRefreshRun !== undefined) {
    await resourceRefreshRun;
    if (!report) {
      await revealResources();
      return;
    }
  }
  await withResourcesBusy(async () => {
    const stderr = await resourceReportRun(resolved, { report }, outputChannel);
    await revealResources();
    if (report && stderr !== undefined) {
      await previewGeneratedReport(stderr, resolved.workspaceRoot, outputChannel);
    }
  });
}

/**
 * The refresh a green Build ends with: one report run, writing no file and revealing
 * nothing (Build's ending owns the focus). Skipped silently while a report holds busy, while
 * a refresh is in hand (busy is released a few turns before its `finally`, and a second one
 * would erase its promise), while a toolchain run is in flight (it could report a failure
 * from the installation being replaced), and when the Build's folder is no longer open
 * first. The view's `when` is set first, for a manifest created outside onboarding. Holds
 * `resourceRefreshRun`, which never rejects: a failed refresh ends in the channel, and Build
 * stays green.
 */
async function refreshResourcesAfterBuild(
  resolved: { workspaceRoot: string; manifestRelPath: string },
  outputChannel: vscode.OutputChannel,
): Promise<void> {
  if (
    resourceState.busy ||
    resourceRefreshRun !== undefined ||
    toolchainRunInFlight ||
    resolved.workspaceRoot !== getWorkspaceRoot()
  ) {
    return;
  }
  const run = async (): Promise<void> => {
    try {
      await withResourcesBusy(async () => {
        await updateResourcesAvailable();
        await resourceReportRun(resolved, { report: false }, outputChannel);
      });
    } catch (error: unknown) {
      outputChannel.appendLine(
        `SWCraft: the Resources refresh failed: ${error instanceof Error ? error.message : String(error)}`,
      );
    } finally {
      resourceRefreshRun = undefined;
    }
  };
  // `run()` holds busy and returns at its first await, so the assignment lands in the guard's
  // turn; `finally` follows a real await, so it can never run before it.
  resourceRefreshRun = run();
  await resourceRefreshRun;
}

/**
 * `swcraft.resourcesOpen`, a row's click: the file of the node's subject (a core's or
 * memory's `hardware/` file at its limit's line, a consumer's `tasks/` or `swc/` file at its
 * budget's line, a finding's first subject at its place) in the one preview tab. An element
 * the storage rule does not find is a warning naming it.
 */
async function resourcesOpenCommand(node: unknown): Promise<void> {
  const resource = resourceNodeOf(node);
  const subject = resource?.open;
  const resolved = resolveManifestOrNotify();
  const modelRoot = currentModelRoot();
  if (
    subject === undefined ||
    resolved === undefined ||
    modelRoot === undefined ||
    resourceContext === undefined
  ) {
    return;
  }
  const refs = loadReferences(
    fs.readFileSync(referencesArtifactPath(resourceContext), { encoding: "utf8" }),
  );
  const reader = fsRequestReader();
  const facts = refs.layers.get(subject.layer);
  const element = subject.element;
  const found =
    facts === undefined || element === undefined
      ? undefined
      : resolveElement(facts, element, modelRoot, reader);
  if (element !== undefined && found === undefined) {
    void vscode.window.showWarningMessage(
      resourceNotFoundMessage({ layer: subject.layer, element }),
    );
    return;
  }
  const lineKeys = resource?.lineKeys;
  const text =
    lineKeys === undefined || found === undefined ? undefined : reader.readText(found.file);
  const place =
    lineKeys === undefined || found === undefined
      ? placeSubject(refs, subject, modelRoot, resolved.manifestPath, reader)
      : { file: found.file, line: text === undefined ? 0 : resourceLine(text, lineKeys) };
  await showPreviewTab(place.file, place.line);
}

// --- The Impact view -----------------------------------------------------------------
//
// `swcraft impact --format json` drawn as a tree, in the Resources view's manner: one
// question at a time, asked from the element's file (the context menu, the palette, the
// *impact* CodeLens, a dependent row's action), one preview tab per click, nothing re-run on
// its own. Every label, id and click target is impactCore.ts's; this half asks, runs the
// walk, keeps the last answer and maps nodes to `TreeItem`s.

/**
 * impactCore's `ImpactAnswer` (moved only by `impactAfterRun()` and
 * `impactAfterFolderChange()`) beside what only this half writes.
 */
interface ImpactState extends ImpactAnswer {
  /** The newest model change the watch stamped (`noteModelChange()`), whatever is shown. */
  newestChangeAt?: number;
  /** A question is being answered. */
  busy: boolean;
}

let impactState: ImpactState = { failed: false, busy: false };
let impactContext: vscode.ExtensionContext | undefined;
/** The Impact view (`swcraftImpact`), set by `registerImpactView()`. */
let impactView: vscode.TreeView<ImpactNode> | undefined;
const impactTreeChanged = new vscode.EventEmitter<ImpactNode | undefined>();
const IMPACT_VIEW_ID = "swcraftImpact";
/** The tree as last drawn. */
let impactTree: ImpactTree = { description: "", nodes: [] };

/**
 * The shown answer's state, by the Resources report's rule: none before the first answer,
 * else *out of date* once a change is stamped after the run that read it started.
 */
function impactAnswerState(): ArtifactState | undefined {
  return decideResourceReportState({
    readAt: impactState.readAt,
    newestChangeAt: impactState.newestChangeAt,
  });
}

/**
 * Redraw the view from the state, and set the context keys its menus read. The answer has
 * no Model item and is not counted in the badge, so nothing else is redrawn.
 */
function refreshImpactView(): void {
  impactTree =
    impactState.refs === undefined
      ? { description: "", nodes: [] }
      : impactNodes(impactState.document, impactState.refs, {
          question: impactState.question,
          outOfDate: isOutOfDate(impactAnswerState()),
          failed: impactState.failed,
        });
  void vscode.commands.executeCommand(
    "setContext",
    "swcraft.impact.loaded",
    impactState.question !== undefined,
  );
  if (impactView !== undefined) {
    impactView.description = impactTree.description;
  }
  impactTreeChanged.fire(undefined);
}

function setImpactBusy(busy: boolean): void {
  impactState.busy = busy;
  void vscode.commands.executeCommand("setContext", "swcraft.impact.busy", busy);
}

/**
 * A change under the model root or to the manifest, by `markResourcesOutOfDate()`'s rule;
 * the view is redrawn only when its mark moves. Nothing re-runs.
 */
function markImpactOutOfDate(changedAt: number): void {
  const before = impactAnswerState();
  if (impactState.newestChangeAt === undefined || changedAt > impactState.newestChangeAt) {
    impactState.newestChangeAt = changedAt;
  }
  if (impactAnswerState() !== before) {
    refreshImpactView();
  }
}

/** One node as a `TreeItem`: impactCore decided every part, this only maps it. */
function impactTreeItem(node: ImpactNode): vscode.TreeItem {
  const collapsible = {
    none: vscode.TreeItemCollapsibleState.None,
    collapsed: vscode.TreeItemCollapsibleState.Collapsed,
    expanded: vscode.TreeItemCollapsibleState.Expanded,
  }[node.collapsible];
  const item = new vscode.TreeItem(node.label, collapsible);
  item.id = node.id;
  item.description = node.description;
  item.tooltip = node.tooltip === "" ? undefined : node.tooltip;
  item.contextValue = node.contextValue;
  if (node.element !== undefined) {
    // A root and a dependent row open their file; a layer node and a message row nothing.
    item.command = { command: "swcraft.impactOpen", title: "Open Element", arguments: [node] };
  }
  return item;
}

/** The Impact view's tree: the nodes `impactNodes()` drew last. */
class ImpactTreeDataProvider implements vscode.TreeDataProvider<ImpactNode> {
  readonly onDidChangeTreeData = impactTreeChanged.event;

  getTreeItem(node: ImpactNode): vscode.TreeItem {
    return impactTreeItem(node);
  }

  getChildren(node?: ImpactNode): ImpactNode[] {
    return node === undefined ? impactTree.nodes : node.children;
  }
}

/**
 * Set up the Impact view. `swcraft.impact.available` follows the project's detection.
 * Nothing is run here: the view shows its welcome until a question is asked.
 */
export function registerImpactView(context: vscode.ExtensionContext): void {
  impactContext = context;
  impactView = vscode.window.createTreeView(IMPACT_VIEW_ID, {
    treeDataProvider: new ImpactTreeDataProvider(),
    showCollapseAll: true,
  });
  context.subscriptions.push(
    impactView,
    impactTreeChanged,
    vscode.workspace.onDidChangeWorkspaceFolders(impactFoldersChanged),
  );
  setImpactBusy(false);
  void updateImpactAvailable();
  refreshImpactView();
}

/** `swcraft.impact.available`: whether the window holds a project, the view's `when`. */
async function updateImpactAvailable(): Promise<void> {
  await vscode.commands.executeCommand(
    "setContext",
    "swcraft.impact.available",
    traceProjectDir() !== undefined,
  );
}

/**
 * A workspace-folder change, by `resourceFoldersChanged()`'s rules: an answer or failed
 * first question from a folder no longer open first is dropped with its stamp
 * (`impactAfterFolderChange()`), so the view shows its welcome again.
 */
function impactFoldersChanged(): void {
  void updateImpactAvailable();
  const next = impactAfterFolderChange(impactState, getWorkspaceRoot());
  if (next !== impactState) {
    impactState = next;
    refreshImpactView();
  }
}

/** Run *flow* holding busy for its whole length, as `withResourcesBusy()` does. */
async function withImpactBusy(flow: () => Promise<void>): Promise<void> {
  if (impactState.busy) {
    void vscode.window.showWarningMessage(BUSY_REFUSAL);
    return;
  }
  setImpactBusy(true);
  try {
    await flow();
  } finally {
    setImpactBusy(false);
  }
}

/**
 * The active text editor as `impactQuestion` reads it (path, live text, cursor line, dirty
 * flag), or `undefined` for none (a webview, a diff) or an unsaved buffer.
 */
function activeImpactEditor(): ImpactEditor | undefined {
  const editor = vscode.window.activeTextEditor;
  const document = editor?.document;
  if (editor === undefined || document === undefined || document.uri.scheme !== "file") {
    return undefined;
  }
  return {
    file: document.uri.fsPath,
    text: document.getText(),
    line: editor.selection.active.line,
    isDirty: document.isDirty,
  };
}

/**
 * One run of `swcraft impact --format json -- <name>`, the whole walk, recorded under
 * `swcraft.impact` whichever command started it, in `resourceReportRun()`'s shape: the
 * stdout is never echoed, a failure keeps the last good tree, and a shown answer is stamped
 * with the run's start. A run that ends after the first folder changed is shown nowhere.
 * *resolved* is the project the question was read against. Answers whether the outcome was
 * recorded in the view. The caller holds busy.
 */
async function impactRun(
  resolved: { workspaceRoot: string; manifestRelPath: string },
  name: string,
  refs: References,
  outputChannel: vscode.OutputChannel,
): Promise<boolean> {
  const commandId = COMMAND_IDS.impact;
  const label = SPAWNING_COMMAND_LABELS[commandId];
  const argv = buildImpactTreeArgv(resolved.manifestRelPath, name);
  const startedAt = Date.now();
  const run = await withRunIndicator(commandId, describeRunning(commandId), () =>
    spawnCli(resolveSwcraftExecutable(), argv, resolved.workspaceRoot, outputChannel, {
      echoStdout: false,
    }),
  );
  const read = run.exitCode === 0 ? readImpactDocument(run.stdout) : undefined;
  outputChannel.appendLine(impactRunLine(run.exitCode, read));
  if (resolved.workspaceRoot !== getWorkspaceRoot()) {
    // The first folder changed during the run: its answer is the project
    // `impactFoldersChanged()` dropped, so the view keeps the state that change left.
    recordCliOutcome(commandId, label, run);
    return false;
  }
  if (read !== undefined && "error" in read) {
    recordCommandStatus(commandId, { kind: "failure", message: `SWCraft: ${read.error}` });
    void vscode.window.showWarningMessage(`SWCraft: ${read.error}`);
  } else {
    recordCliOutcome(commandId, label, run);
  }
  impactState = impactAfterRun(impactState, {
    root: resolved.workspaceRoot,
    name,
    refs,
    startedAt,
    read,
  });
  refreshImpactView();
  return true;
}

/** Reveal the view and focus it (`swcraftImpact.focus`), its `when` set first. */
async function revealImpact(): Promise<void> {
  // The view's `when` is set, and awaited, before the focus, which a still-hidden view
  // would refuse (a project onboarded after activation).
  await updateImpactAvailable();
  await vscode.commands.executeCommand(`${IMPACT_VIEW_ID}.focus`);
}

/**
 * `swcraft.impact`, *SWCraft: Impact of This Element*: from the editor's context menu and
 * the palette (the active editor's element, or the one named under its cursor), the
 * *impact* CodeLens or a dependent row's action (their name). `impactQuestion` decides
 * which element, through Go to Definition's resolver, or answers with a message and no run.
 * The project is resolved once, so the question and the run read the same model root.
 */
async function impactCommand(
  outputChannel: vscode.OutputChannel,
  arg?: unknown,
  lensUri?: unknown,
): Promise<void> {
  const resolved = resolveManifestOrNotify();
  if (resolved === undefined) {
    return;
  }
  await askImpact(resolved, arg, outputChannel, lensUri);
}

/** `swcraft.impactRefresh`, the view's ↻: the shown question asked again. */
async function impactRefreshCommand(outputChannel: vscode.OutputChannel): Promise<void> {
  const resolved = resolveManifestOrNotify();
  const question = impactState.question;
  if (resolved === undefined || question === undefined) {
    return;
  }
  await askImpact(resolved, question, outputChannel);
}

/**
 * The question *arg* asks and its run, against *resolved*, the project the command resolved
 * once, holding busy for both.
 */
async function askImpact(
  resolved: { workspaceRoot: string; manifestPath: string; manifestRelPath: string },
  arg: unknown,
  outputChannel: vscode.OutputChannel,
  lensUri?: unknown,
): Promise<void> {
  const context = impactContext;
  if (context === undefined) {
    return;
  }
  await withImpactBusy(async () => {
    const refs = loadReferences(
      fs.readFileSync(referencesArtifactPath(context), { encoding: "utf8" }),
    );
    const question = impactQuestion(
      arg,
      {
        refs,
        modelRoot: resolveModelRoot(resolved.manifestPath),
        reader: fsRequestReader(),
        editor: activeImpactEditor(),
      },
      lensUri,
    );
    if ("message" in question) {
      void vscode.window.showInformationMessage(question.message);
      return;
    }
    // A run whose folder changed under it is shown nowhere, so the view is not focused.
    if (await impactRun(resolved, question.name, refs, outputChannel)) {
      await revealImpact();
    }
  });
}

/**
 * `swcraft.impactOpen`, a root's or a dependent row's click: the node's element file, placed
 * as Find All References places it (its identity line or, under a requirement root, the
 * first member line naming it), in the one preview tab with the focus kept in the view. A
 * file not found is a warning naming the element.
 */
async function impactOpenCommand(node: unknown): Promise<void> {
  const impact = impactNodeOf(node);
  const element = impact?.element;
  const resolved = resolveManifestOrNotify();
  const modelRoot = currentModelRoot();
  if (
    impact === undefined ||
    element === undefined ||
    resolved === undefined ||
    modelRoot === undefined ||
    impactContext === undefined
  ) {
    return;
  }
  // The table the tree was drawn with; the shipped file only if none is held.
  const refs =
    impactState.refs ??
    loadReferences(fs.readFileSync(referencesArtifactPath(impactContext), { encoding: "utf8" }));
  // A root carries no root layer: no relation targets "", so it opens at its identity line.
  // The asked name is the drawn node's own (`impactNodes()` sets it), never re-derived here.
  const [place] = dependentLocations(
    refs,
    { layer: element.token, name: element.name },
    impact.rootLayer ?? "",
    impact.rootName,
    modelRoot,
    fsRequestReader(),
  );
  if (place === undefined) {
    void vscode.window.showWarningMessage(
      impactFileNotFoundMessage(impactLayerLabel(refs, element.token), element.name),
    );
    return;
  }
  await showPreviewTab(place.file, place.line);
}

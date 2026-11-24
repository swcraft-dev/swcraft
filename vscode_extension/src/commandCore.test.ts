import assert from "node:assert/strict";
import { type ChildProcess, spawn } from "node:child_process";
import { getEventListeners, once } from "node:events";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { test } from "node:test";
import * as commandCore from "./commandCore";
import {
  BUILD_WAITING_TEXT,
  type BuildStep,
  buildBuildSteps,
  buildCliArgv,
  buildExportComponentArgv,
  buildGenerateDiagramsArgv,
  buildImpactJsonArgv,
  buildImpactTreeArgv,
  buildImportRequirementsArgv,
  buildInitArgv,
  buildListArgv,
  buildPreflightArgv,
  buildRunEnding,
  buildRunGenerations,
  buildRunMessage,
  buildRunRefreshesResources,
  type CliCommandKind,
  COMMAND_IDS,
  captureChild,
  checkModelJsonArgv,
  currentPlatform,
  DIAGRAMS_PROGRESS_TEXT,
  decideCliUnavailablePrompt,
  decideExportComponentPrompt,
  describeBuildProgress,
  describeRunning,
  diagramsArgument,
  EXPORT_COMPONENT_PROMPT_TEXT,
  elementsInLayer,
  escapeMarkdown,
  IMPORT_APPLY_BUTTON,
  IMPORT_CANCEL_BUTTON,
  IMPORT_REPORT_TITLE,
  IMPORT_RUN_WORDING,
  importDoneMessage,
  type LayerListing,
  LISTING_SCHEMA_VERSION,
  METRICS_SINCE_SETTING,
  mapExitCodeToOutcome,
  mapPreflightResult,
  metricsArgv,
  metricsSinceRefusal,
  outcomeMessage,
  PROBLEMS_PANEL_DETAILS,
  parseListJson,
  planOnboardingSteps,
  preflightNotFoundMessage,
  type RunOutput,
  resolveManifest,
  resourceReportJsonArgv,
  runBuildSteps,
  SerialRunner,
  SPAWNING_COMMAND_LABELS,
  summaryOf,
  toolchainRunningRefusal,
  VIEW_ID_PATTERN,
  validateSwcNameInput,
  viewsRootWriteRunningRefusal,
} from "./commandCore";
import { DIAGRAM_EDITOR_COMMANDS } from "./diagramsTab";
import { IMPACT_NON_SPAWNING_COMMANDS } from "./impactCore";
import { applyButtons, importReportMarkdown, importTabHtml } from "./importTabCore";
import { RESOURCE_NON_SPAWNING_COMMANDS } from "./resourceCore";
import { escapeHtml, REVIEW_VIEW_COMMANDS } from "./reviewCore";
import {
  assertNoProductionModuleNames,
  functionBody,
  importedModules,
  PROJECT_ROOT,
  readCliPackageSource,
  readSource,
  sourceRegion,
  withoutCommentLines,
} from "./testSupport";
import { TRACE_NON_SPAWNING_COMMANDS } from "./traceCore";
import { TREE_COMMAND_DEFINITIONS } from "./treeView";

function mkWorkspace(): string {
  return fs.mkdtempSync(path.join(os.tmpdir(), "swcraft-vscode-commandCore-test-"));
}

function cleanup(dir: string): void {
  fs.rmSync(dir, { recursive: true, force: true });
}

// --- buildCliArgv ------------------------------------------------------
//
// argv is what follows the resolved `swcraft` executable: no `-m`, no module name and no
// interpreter. `toolchain.ts` resolves the executable by absolute path, so nothing here works
// around a PATH lookup.
//
// `validate` and `generate-c4` are not `CliCommandKind`s: `swcraft validate` is Build's last step,
// `swcraft generate diagrams` is Build's fourth and Diagrams' first, and no step records a status
// of its own (see buildBuildSteps below).

test("buildCliArgv: the retired kinds have no command id and no argv shape", () => {
  // The TYPE refuses a retired kind: only a cast reaches `buildCliArgv` with one, and there the
  // TypeError (the spread of an absent subcommand list) is thrown before any argv exists, so
  // nothing can be spawned from it.
  for (const kind of ["validate", "generate-c4"]) {
    assert.equal(kind in COMMAND_IDS, false, `'${kind}' is no CLI passthrough kind any more`);
    assert.throws(() => buildCliArgv(kind as CliCommandKind, "swcraft.yaml"), TypeError);
  }
});

// --- buildCliArgv: the three read-only commands ------
//
// Each argv shape below was run on `examples/basic` before it was written here; these tests compare
// argv arrays only. They add no behaviour beyond the argv: nothing is parsed or re-rendered.

test("buildCliArgv: 'change-report' passes no revision arguments at all", () => {
  const argv = buildCliArgv("change-report", "examples/basic/swcraft.yaml");
  assert.deepEqual(argv, ["change-report", "--project", "examples/basic/swcraft.yaml"]);
  // The CLI's `change-report` subparser has two optional revision positionals defaulting to "HEAD
  // vs. the working tree", the question the panel asks; anything between the subcommand and
  // `--project` would be a revision the panel never asked the user for.
  assert.equal(argv.indexOf("--project"), argv.indexOf("change-report") + 1);
});

test("buildCliArgv: 'diff' is no CLI passthrough kind any more (Change Report replaced it)", () => {
  // The `swcraft diff` CLI command stays; the panel has no Diff item.
  assert.equal("diff" in COMMAND_IDS, false);
  assert.throws(() => buildCliArgv("diff" as CliCommandKind, "swcraft.yaml"), TypeError);
});

// `check-model` is the cheap half of `validate`: the same model checks with no ARXML written and
// therefore no AUTOSAR XSD compiled (the 5-6 s a `validate` costs on a host with a licensed
// schema). One hyphenated subcommand, like `resource-report`, and no positional.
test("buildCliArgv: 'check-model' is one hyphenated subcommand and takes no argument", () => {
  assert.deepEqual(buildCliArgv("check-model", "swcraft.yaml"), [
    "check-model",
    "--project",
    "swcraft.yaml",
  ]);
  assert.throws(
    () => buildCliArgv("check-model", "swcraft.yaml", "BrakeController"),
    /takes no positional argument/,
  );
});

test("buildCliArgv: 'resource-report' is one hyphenated subcommand and takes no argument", () => {
  assert.deepEqual(buildCliArgv("resource-report", "swcraft.yaml"), [
    "resource-report",
    "--project",
    "swcraft.yaml",
  ]);
});

// --- Architecture Metrics: the argv from swcraft.metricsSince ---------------

test("metricsArgv: an empty setting passes --since-head and no --since", () => {
  const argv = metricsArgv(buildCliArgv("metrics", "swcraft.yaml"), []);
  assert.deepEqual(argv, ["metrics", "--project", "swcraft.yaml", "--report", "--since-head"]);
  assert.equal(
    argv.some((word) => word.startsWith("--since=") || word === "--since"),
    false,
  );
});

test("metricsArgv: one revision is one --since=<rev> word, and no --since-head", () => {
  assert.deepEqual(metricsArgv(buildCliArgv("metrics", "swcraft.yaml"), ["v1.0"]), [
    "metrics",
    "--project",
    "swcraft.yaml",
    "--report",
    "--since=v1.0",
  ]);
});

test("metricsArgv: two revisions keep the setting's order, the first the base", () => {
  const argv = metricsArgv(buildCliArgv("metrics", "swcraft.yaml"), ["v1.0", "HEAD~2"]);
  assert.deepEqual(argv.slice(-3), ["--report", "--since=v1.0", "--since=HEAD~2"]);
  assert.equal(argv.includes("--since-head"), false);
});

test("metricsArgv: a revision opening with '-' stays inside its one word", () => {
  // Two words would hand argparse an option it does not know (exit 2); the joined
  // word reaches the command, whose refusal names the revision.
  const argv = metricsArgv(buildCliArgv("metrics", "swcraft.yaml"), ["-x"]);
  assert.equal(argv.at(-1), "--since=-x");
  assert.equal(argv.includes("-x"), false);
});

test("metricsSinceRefusal: a list of strings or unset runs, anything else is refused", () => {
  for (const value of [undefined, null, [], ["v1"], ["v1", "HEAD~1"]]) {
    assert.equal(metricsSinceRefusal(value), undefined, JSON.stringify(value));
  }
  for (const value of ["v1", 3, {}, [1], ["v1", null]]) {
    assert.match(
      metricsSinceRefusal(value) ?? "",
      new RegExp(`${METRICS_SINCE_SETTING.replace(".", "\\.")} must be a list of revisions`),
      JSON.stringify(value),
    );
  }
});

test("buildCliArgv: 'impact' passes the name last, after --project and a bare --", () => {
  assert.deepEqual(buildCliArgv("impact", "examples/basic/swcraft.yaml", "ExampleSWC"), [
    "impact",
    "--project",
    "examples/basic/swcraft.yaml",
    "--",
    "ExampleSWC",
  ]);
});

test("buildCliArgv: a name starting with '-' reaches the CLI as the positional, not as an option", () => {
  // argparse reads any token starting with `-` as an option, so `swcraft impact -foo --project p`
  // exits 2 ("the following arguments are required: NAME") instead of answering "element not
  // found". A leading `-` is reachable from the Impact QuickPick (`Requirement.id` is a free
  // string); `--` ends option parsing.
  const argv = buildCliArgv("impact", "swcraft.yaml", "-foo");
  assert.deepEqual(argv.slice(-2), ["--", "-foo"]);
  assert.ok(
    argv.indexOf("--") > argv.indexOf("--project"),
    "the separator must come after every option this argv carries",
  );
});

test("buildCliArgv: the kinds with no positional carry no -- separator", () => {
  for (const kind of [
    "change-report",
    "check-model",
    "metrics",
    "resource-report",
  ] as CliCommandKind[]) {
    assert.equal(
      buildCliArgv(kind, "swcraft.yaml").includes("--"),
      false,
      `${kind} must not carry an empty argument separator`,
    );
  }
});

test("buildCliArgv: 'impact' trims surrounding whitespace from the name", () => {
  assert.deepEqual(
    buildCliArgv("impact", "swcraft.yaml", "  ISpeed \n"),
    buildCliArgv("impact", "swcraft.yaml", "ISpeed"),
  );
});

test("buildCliArgv: 'impact' without a name throws rather than spawning a nameless run", () => {
  assert.throws(() => buildCliArgv("impact", "swcraft.yaml"), /needs an element name/);
  assert.throws(() => buildCliArgv("impact", "swcraft.yaml", "   "), /needs an element name/);
});

test("buildCliArgv: a positional handed to a kind that takes none throws, never dropped silently", () => {
  for (const kind of [
    "change-report",
    "check-model",
    "metrics",
    "resource-report",
  ] as CliCommandKind[]) {
    assert.throws(
      () => buildCliArgv(kind, "swcraft.yaml", "HEAD~1"),
      /takes no positional argument/,
      `${kind} must refuse a positional argument`,
    );
  }
});

// --- COMMAND_IDS -----------------------------------------------------------
//
// The kinds whose panel item is one spawn, each with its own command id, so no run is recorded
// against another kind's tree item.

test("COMMAND_IDS: every CLI kind maps to its own VS Code command id", () => {
  assert.deepEqual(COMMAND_IDS, {
    "change-report": "swcraft.changeReport",
    "check-model": "swcraft.checkModel",
    impact: "swcraft.impact",
    metrics: "swcraft.metrics",
    "resource-report": "swcraft.resourceReport",
  });
});

test("COMMAND_IDS: the ids are distinct and every kind buildCliArgv accepts has one", () => {
  const ids = Object.values(COMMAND_IDS);
  assert.equal(new Set(ids).size, ids.length, `command ids must be unique: ${ids}`);
  for (const kind of Object.keys(COMMAND_IDS) as CliCommandKind[]) {
    // A kind with no argv shape would throw here rather than produce one.
    const argv = kind === "impact" ? buildCliArgv(kind, "p", "N") : buildCliArgv(kind, "p");
    assert.ok(argv.length > 0, `${kind} must have an argv shape`);
  }
});

// --- buildExportComponentArgv --------------------------
//
// `swcraft generate arxml --component <name>` omits `--output`: the CLI (cli/build_commands.py's
// cmd_generate) computes the default `<dist_root>/components/<name>.arxml` (`<name>~N.arxml` for a
// case-only twin) from the manifest, so the extension never has to know the dist_root.

test("buildExportComponentArgv: produces 'swcraft generate arxml --component=<name> --project <manifest>'", () => {
  assert.deepEqual(buildExportComponentArgv("swcraft.yaml", "SpeedSensor"), [
    "generate",
    "arxml",
    "--component=SpeedSensor",
    "--project",
    "swcraft.yaml",
  ]);
});

test("buildExportComponentArgv: trims surrounding whitespace from the SWC name", () => {
  const argv = buildExportComponentArgv("swcraft.yaml", "  SpeedSensor  ");
  assert.equal(argv[argv.length - 3], "--component=SpeedSensor");
});

// A leading `-` is unreachable from the QuickPick (AUTOSAR identifiers) but reachable from the
// free-text fallback, whose only validator is non-emptiness. As two argv entries argparse reads
// the value as an option and exits 2; the `=` form carries it through to the CLI's own "SWC not
// found".
test("buildExportComponentArgv: a name starting with '-' stays one token", () => {
  const argv = buildExportComponentArgv("swcraft.yaml", "-foo");
  assert.ok(argv.includes("--component=-foo"));
  assert.ok(!argv.includes("--component"));
});

// --- `swcraft import requirements`: argv, approval, summary, preview --------

// An absolute path for the host the test runs on: the builder checks it with
// `path.isAbsolute`, which is the host's rule.
const IMPORT_SOURCE = path.resolve(os.tmpdir(), "needs.json");

test("buildImportRequirementsArgv: the dry run is 'import requirements --source=<file> --project <manifest> --dry-run'", () => {
  assert.deepEqual(buildImportRequirementsArgv("swcraft.yaml", IMPORT_SOURCE, { dryRun: true }), [
    "import",
    "requirements",
    `--source=${IMPORT_SOURCE}`,
    "--project",
    "swcraft.yaml",
    "--dry-run",
  ]);
});

test("buildImportRequirementsArgv: the applied run carries no --dry-run", () => {
  assert.deepEqual(buildImportRequirementsArgv("swcraft.yaml", IMPORT_SOURCE, { dryRun: false }), [
    "import",
    "requirements",
    `--source=${IMPORT_SOURCE}`,
    "--project",
    "swcraft.yaml",
  ]);
});

// The `=` form keeps a file name starting with '-' one token, the
// buildExportComponentArgv hardening.
test("buildImportRequirementsArgv: a file name starting with '-' stays one token", () => {
  const file = path.resolve(os.tmpdir(), "-needs.json");
  const argv = buildImportRequirementsArgv("swcraft.yaml", file, { dryRun: true });
  assert.ok(argv.includes(`--source=${file}`));
  assert.ok(!argv.includes("--source"));
});

test("buildImportRequirementsArgv: a relative path throws", () => {
  assert.throws(
    () => buildImportRequirementsArgv("swcraft.yaml", "needs.json", { dryRun: true }),
    /must be an absolute path/,
  );
  assert.throws(
    () =>
      buildImportRequirementsArgv("swcraft.yaml", path.join("exports", "needs.json"), {
        dryRun: false,
      }),
    /must be an absolute path/,
  );
});

const IMPORT_MAPPING = path.resolve(os.tmpdir(), "team.needs-mapping.yaml");

test("buildImportRequirementsArgv: a mapping and a baseline are one '=' token each, before --project", () => {
  assert.deepEqual(
    buildImportRequirementsArgv("swcraft.yaml", IMPORT_SOURCE, {
      dryRun: true,
      mapping: IMPORT_MAPPING,
      baseline: "v2.3",
    }),
    [
      "import",
      "requirements",
      `--source=${IMPORT_SOURCE}`,
      `--mapping=${IMPORT_MAPPING}`,
      "--baseline=v2.3",
      "--project",
      "swcraft.yaml",
      "--dry-run",
    ],
  );
});

test("buildImportRequirementsArgv: each new value alone, and the applied argv is the dry run's without --dry-run", () => {
  const mappingOnly = buildImportRequirementsArgv("swcraft.yaml", IMPORT_SOURCE, {
    dryRun: false,
    mapping: IMPORT_MAPPING,
  });
  assert.ok(mappingOnly.includes(`--mapping=${IMPORT_MAPPING}`));
  assert.equal(
    mappingOnly.some((token) => token.startsWith("--baseline")),
    false,
  );
  const baselineOnly = buildImportRequirementsArgv("swcraft.yaml", IMPORT_SOURCE, {
    dryRun: false,
    baseline: "abc123",
  });
  assert.ok(baselineOnly.includes("--baseline=abc123"));
  assert.equal(
    baselineOnly.some((token) => token.startsWith("--mapping")),
    false,
  );
  const asked = { mapping: IMPORT_MAPPING, baseline: "v2.3" };
  const dry = buildImportRequirementsArgv("swcraft.yaml", IMPORT_SOURCE, {
    dryRun: true,
    ...asked,
  });
  const applied = buildImportRequirementsArgv("swcraft.yaml", IMPORT_SOURCE, {
    dryRun: false,
    ...asked,
  });
  assert.deepEqual(
    applied,
    dry.filter((token) => token !== "--dry-run"),
  );
});

test("buildImportRequirementsArgv: an empty baseline is no --baseline; one starting with '-' or holding spaces stays one token, as typed", () => {
  for (const baseline of [undefined, ""]) {
    const argv = buildImportRequirementsArgv("swcraft.yaml", IMPORT_SOURCE, {
      dryRun: true,
      baseline,
    });
    assert.equal(
      argv.some((token) => token.startsWith("--baseline")),
      false,
      String(baseline),
    );
  }
  for (const baseline of ["-rc1", " v2 ", "release 2.3"]) {
    const argv = buildImportRequirementsArgv("swcraft.yaml", IMPORT_SOURCE, {
      dryRun: true,
      baseline,
    });
    assert.ok(argv.includes(`--baseline=${baseline}`), baseline);
  }
});

test("buildImportRequirementsArgv: a relative mapping path throws, as the source does", () => {
  for (const mapping of ["team.needs-mapping.yaml", path.join("..", "team.needs-mapping.yaml")]) {
    assert.throws(
      () => buildImportRequirementsArgv("swcraft.yaml", IMPORT_SOURCE, { dryRun: true, mapping }),
      /the mapping file must be an absolute path/,
    );
  }
});

test("mappingPickItems: the remembered mapping first as last used, then Choose and SWCraft's own names", () => {
  assert.deepEqual(commandCore.mappingPickItems("reqs/team.needs-mapping.yaml"), [
    {
      label: "$(file) reqs/team.needs-mapping.yaml",
      description: "last used",
      choice: "remembered",
    },
    { label: "Choose another mapping file…", choice: "choose" },
    { label: "SWCraft's own names (no mapping)", choice: "none" },
  ]);
});

test("mappingPickItems: nothing remembered — unset, empty, blank or not a string — offers the last two only", () => {
  for (const remembered of [undefined, null, "", "   ", 42, ["a.yaml"]]) {
    assert.deepEqual(
      commandCore.mappingPickItems(remembered).map((item) => item.choice),
      ["choose", "none"],
      JSON.stringify(remembered),
    );
  }
});

test("rememberedSettingPath: a stored string trimmed; unset, empty, blank or not a string is no mapping", () => {
  assert.equal(
    commandCore.rememberedSettingPath("  reqs/team.needs-mapping.yaml "),
    "reqs/team.needs-mapping.yaml",
  );
  for (const stored of [undefined, null, "", "   ", 42, ["a.yaml"]]) {
    assert.equal(commandCore.rememberedSettingPath(stored), undefined, JSON.stringify(stored));
  }
});

test("storedSettingPath: inside the project relative in '/' form, outside absolute, never '..'", () => {
  const projectDir = path.resolve(os.tmpdir(), "project");
  assert.equal(
    commandCore.storedSettingPath(path.join(projectDir, "team.needs-mapping.yaml"), projectDir),
    "team.needs-mapping.yaml",
  );
  assert.equal(
    commandCore.storedSettingPath(
      path.join(projectDir, "reqs", "team.needs-mapping.yaml"),
      projectDir,
    ),
    "reqs/team.needs-mapping.yaml",
  );
  for (const outside of [
    path.resolve(os.tmpdir(), "team.needs-mapping.yaml"),
    path.resolve(os.tmpdir(), "project-other", "team.needs-mapping.yaml"),
    path.resolve(os.tmpdir(), "elsewhere", "deep", "team.needs-mapping.yaml"),
  ]) {
    const stored = commandCore.storedSettingPath(outside, projectDir);
    assert.equal(stored, outside);
    assert.equal(stored.split(/[\\/]/).includes(".."), false, stored);
  }
  // A folder inside the project whose name starts with two dots is inside.
  assert.equal(
    commandCore.storedSettingPath(path.join(projectDir, "..mappings", "m.yaml"), projectDir),
    "..mappings/m.yaml",
  );
});

test("storedSettingPath: a relative file is refused, never stored", () => {
  assert.throws(
    () => commandCore.storedSettingPath("team.needs-mapping.yaml", os.tmpdir()),
    /must be absolute/,
  );
});

test("settingPathArgFor: the stored value resolved against the project directory; empty is no mapping", () => {
  const projectDir = path.resolve(os.tmpdir(), "project");
  assert.equal(
    commandCore.settingPathArgFor("reqs/team.needs-mapping.yaml", projectDir),
    path.join(projectDir, "reqs", "team.needs-mapping.yaml"),
  );
  const outside = path.resolve(os.tmpdir(), "team.needs-mapping.yaml");
  assert.equal(commandCore.settingPathArgFor(outside, projectDir), outside);
  for (const stored of [undefined, "", "  ", 7]) {
    assert.equal(commandCore.settingPathArgFor(stored, projectDir), undefined, String(stored));
  }
  // What the pick stores reads back to the file it picked.
  const picked = path.join(projectDir, "reqs", "m.yaml");
  assert.equal(
    commandCore.settingPathArgFor(commandCore.storedSettingPath(picked, projectDir), projectDir),
    picked,
  );
});

test("the delivery's version box and the mapping pick: their prompts, every text non-empty", () => {
  assert.equal(
    commandCore.BASELINE_PROMPT_TEXT.prompt,
    "The delivery's version, as the file names it. Press Enter to record it, or give another: the requirements team's baseline or commit.",
  );
  assert.equal(
    commandCore.BASELINE_PROMPT_TEXT.promptRequired,
    "The file names no version. Enter the delivery's version: the requirements team's baseline or commit of this delivery.",
  );
  assert.doesNotMatch(JSON.stringify(commandCore.BASELINE_PROMPT_TEXT), /Leave empty|--baseline/);
  for (const text of [
    commandCore.BASELINE_PROMPT_TEXT,
    commandCore.MAPPING_PICK_TEXT,
    { title: commandCore.MAPPING_DIALOG_TITLE },
  ]) {
    for (const value of Object.values(text)) {
      assert.ok(value.trim().length > 0);
    }
    assert.match(text.title, /^SWCraft: /);
  }
});

test("the requirements import's buttons: Write, the one apply, and Cancel", () => {
  // Which message runs the apply, failing closed for everything else, is
  // `acceptImportTabMessage()`'s (importTabCore.test.ts); the labels are these.
  assert.equal(IMPORT_APPLY_BUTTON, "Write", "the trace Apply's word");
  assert.equal(IMPORT_CANCEL_BUTTON, "Cancel", "the tab's secondary button");
  assert.deepEqual(applyButtons(false), [
    { action: "apply", label: IMPORT_APPLY_BUTTON, primary: true },
    { action: "cancel", label: IMPORT_CANCEL_BUTTON, primary: false },
  ]);
});

test("the delivery's version box: prefilled with the file's version, else empty and required", () => {
  const filled = commandCore.baselineBoxOptions("SYS-2.1");
  assert.equal(filled.value, "SYS-2.1", "Enter records what the file names");
  assert.equal(filled.prompt, commandCore.BASELINE_PROMPT_TEXT.prompt);
  const empty = commandCore.baselineBoxOptions(undefined);
  assert.equal(empty.value, "");
  assert.equal(empty.prompt, commandCore.BASELINE_PROMPT_TEXT.promptRequired);
  for (const options of [filled, empty]) {
    for (const blank of ["", "  ", "\t"]) {
      assert.equal(options.validateInput(blank), commandCore.BASELINE_PROMPT_TEXT.required, blank);
    }
    assert.equal(options.validateInput("abc123"), undefined);
    assert.equal(options.title, commandCore.BASELINE_PROMPT_TEXT.title);
  }
});

test("fileDeliveryVersion: the file's non-blank current_version, else undefined", () => {
  const needs = (version: unknown) => JSON.stringify({ current_version: version, versions: {} });
  assert.equal(commandCore.fileDeliveryVersion(needs("SYS-2.1")), "SYS-2.1");
  assert.equal(commandCore.fileDeliveryVersion(`\uFEFF${needs("2.3")}`), "2.3", "a BOM is skipped");
  for (const text of [
    needs(""),
    needs("   "),
    needs(7),
    needs(null),
    JSON.stringify({ versions: {} }),
    "[]",
    "null",
    '"SYS-2.1"',
    "{ not json",
    "",
  ]) {
    assert.equal(commandCore.fileDeliveryVersion(text), undefined, text);
  }
});

test("baselineArgFor: the file's version kept is no --baseline, anything else is passed as typed", () => {
  assert.equal(commandCore.baselineArgFor("SYS-2.1", "SYS-2.1"), undefined);
  assert.equal(commandCore.baselineArgFor("abc123", "SYS-2.1"), "abc123");
  assert.equal(commandCore.baselineArgFor(" SYS-2.1", "SYS-2.1"), " SYS-2.1");
  assert.equal(commandCore.baselineArgFor("abc123", undefined), "abc123");
});

test("importDoneMessage: the file's name and the count verbatim, a sentence without one", () => {
  const count = "46 added, 0 changed, 0 removed, 0 stubs created, 0 source updates";
  assert.equal(importDoneMessage("/work/req/srs.json", count), `srs.json was imported: ${count}.`);
  assert.equal(importDoneMessage("/work/req/srs.json", undefined), "srs.json was imported.");
  assert.equal(IMPORT_REPORT_TITLE, "Requirements Import Report");
});

// The header the CLI prints, its summary line last, then a section line
// (indented two spaces).
const IMPORT_REPORT = [
  "Requirements import from 'needs.json'",
  "Dry run: nothing was written.",
  "Summary: 3 added, 2 changed, 1 removed, 1 stubs created, 0 source updates",
  "",
  "Added:",
  "  SWR-1",
  "",
].join("\n");

test("summaryOf: the count after 'Summary: ', verbatim", () => {
  assert.equal(
    summaryOf(IMPORT_REPORT),
    "3 added, 2 changed, 1 removed, 1 stubs created, 0 source updates",
  );
});

// Windows keeps the stream's newline translation: `$` under `/m` matches before the `\r`.
test("summaryOf: a CRLF report holds", () => {
  assert.equal(
    summaryOf(IMPORT_REPORT.replace(/\n/g, "\r\n")),
    "3 added, 2 changed, 1 removed, 1 stubs created, 0 source updates",
  );
});

test("summaryOf: a report without the summary line is undefined", () => {
  assert.equal(summaryOf(""), undefined);
  assert.equal(summaryOf("Requirements import from 'needs.json'\nNo change.\n"), undefined);
});

test("summaryOf: malformed counts are undefined", () => {
  for (const line of [
    "Summary: x added, 2 changed, 1 removed, 1 stubs created, 0 source updates",
    "Summary: 3 added, 2 changed, 1 removed, 1 stubs created",
    "Summary: 3 added, 2 changed, 1 removed, 1 stubs created, 0 source updates.",
    "Summary: -3 added, 2 changed, 1 removed, 1 stubs created, 0 source updates",
    "Summary:  3 added, 2 changed, 1 removed, 1 stubs created, 0 source updates",
  ]) {
    assert.equal(summaryOf(`header\n${line}\n`), undefined, line);
  }
});

test("summaryOf: two column-zero summary lines are undefined", () => {
  const report = [
    "Requirements import from 'needs.json'",
    "Summary: 1 added, 0 changed, 0 removed, 0 stubs created, 0 source updates",
    "Summary: 9 added, 9 changed, 9 removed, 9 stubs created, 9 source updates",
  ].join("\n");
  assert.equal(summaryOf(report), undefined);
});

// A toolchain that printed a `current_version` unescaped would put the file's summary line ahead
// of the real one, and the modal would show zero additions when one was planned.
test("summaryOf: a baseline that injects a summary line ahead of the real one is undefined", () => {
  const report = [
    "Requirements import: baseline SRS-1",
    "Summary: 0 added, 0 changed, 0 removed, 0 stubs created, 0 source updates",
    "Dry run: nothing was written.",
    "Summary: 1 added, 0 changed, 0 removed, 0 stubs created, 0 source updates",
    "",
    "Added:",
    "  SWR-1 (v1)",
    "",
  ].join("\n");
  assert.equal(summaryOf(report), undefined);
  assert.equal(summaryOf(report.replace(/\n/g, "\r\n")), undefined);
});

test("summaryOf: a 'Summary:' text inside a section line is not the summary", () => {
  const report = [
    "Requirements import from 'needs.json'",
    "Changed:",
    "  SWR-1 title: 'Summary: 9 added, 9 changed, 9 removed, 9 stubs created, 9 source updates'",
    "  Summary: 8 added, 8 changed, 8 removed, 8 stubs created, 8 source updates",
    "Summary: 1 added, 0 changed, 0 removed, 1 stubs created, 0 source updates",
  ].join("\n");
  assert.equal(
    summaryOf(report),
    "1 added, 0 changed, 0 removed, 1 stubs created, 0 source updates",
  );
});

test("importDryRunOutcome: a requirements dry run's exit 0 is its report, with no line of the extension's", () => {
  // Write and Cancel stand under it in the tab, so no sentence sends the user to a later run.
  const run = { exitCode: 0, spawnFailed: false, stdout: IMPORT_REPORT, stderr: "" };
  assert.deepEqual(
    commandCore.importDryRunOutcome(run, "/opt/swcraft", commandCore.IMPORT_RUN_WORDING),
    {
      kind: "report",
      report: { cliText: IMPORT_REPORT, notes: [] },
    },
  );
});

test("importDryRunOutcome: a refused requirements dry run keeps its stderr and ends with Nothing was written", () => {
  const stderr = "Error: needs.json: 'SWR-1' is an architecture requirement's id\n";
  const run = { exitCode: 1, spawnFailed: false, stdout: "", stderr };
  assert.deepEqual(
    commandCore.importDryRunOutcome(run, "/opt/swcraft", commandCore.IMPORT_RUN_WORDING),
    {
      kind: "refused",
      report: { cliText: stderr, notes: ["Nothing was written."] },
    },
  );
});

// --- buildListArgv ---------------------------------------------------------

test("buildListArgv: no layer lists every layer as JSON", () => {
  assert.deepEqual(buildListArgv("swcraft.yaml"), [
    "list",
    "--project",
    "swcraft.yaml",
    "--format",
    "json",
  ]);
});

test("buildListArgv: a layer is passed as the positional, before --project", () => {
  assert.deepEqual(buildListArgv("examples/basic/swcraft.yaml", "swc"), [
    "list",
    "swc",
    "--project",
    "examples/basic/swcraft.yaml",
    "--format",
    "json",
  ]);
});

// --- buildImpactJsonArgv (Find All References) -------------------

test("buildImpactJsonArgv: impact as JSON, one level deep, the name after --", () => {
  assert.deepEqual(buildImpactJsonArgv("swcraft.yaml", "IExample"), [
    "impact",
    "--project",
    "swcraft.yaml",
    "--format",
    "json",
    "--depth",
    "1",
    "--",
    "IExample",
  ]);
  // A name that starts with `-` stays a name: it follows the `--`.
  assert.deepEqual(buildImpactJsonArgv("swcraft.yaml", "-x").slice(-2), ["--", "-x"]);
});

// --- buildImpactTreeArgv (the Impact view) -------------------

test("buildImpactTreeArgv: impact as JSON, the whole walk (no --depth), the name after --", () => {
  assert.deepEqual(buildImpactTreeArgv("swcraft.yaml", "IExample"), [
    "impact",
    "--project",
    "swcraft.yaml",
    "--format",
    "json",
    "--",
    "IExample",
  ]);
  assert.ok(!buildImpactTreeArgv("swcraft.yaml", "IExample").includes("--depth"));
  assert.deepEqual(buildImpactTreeArgv("swcraft.yaml", "-x").slice(-2), ["--", "-x"]);
});

test("buildImpactJsonArgv keeps its --depth 1: Find All References still asks one level", () => {
  const argv = buildImpactJsonArgv("swcraft.yaml", "IExample");
  assert.deepEqual(argv.slice(argv.indexOf("--depth"), argv.indexOf("--depth") + 2), [
    "--depth",
    "1",
  ]);
});

// --- No interpreter is left in any argv, and none is left in any spawn -------

test("no argv builder emits an interpreter token: the first word is a swcraft subcommand", () => {
  // The extension spawns the installed `swcraft`, never a Python interpreter, so an interpreter
  // token anywhere in an argv would reach the CLI's argparse as a subcommand and be refused there,
  // with a message naming neither this rule nor this file.
  const argvs: Record<string, string[]> = {
    ...Object.fromEntries(
      buildBuildSteps("swcraft.yaml").map((step) => [`build: ${step.label}`, [...step.argv]]),
    ),
    "check-model": buildCliArgv("check-model", "swcraft.yaml"),
    "generate diagrams (Diagrams)": buildGenerateDiagramsArgv("swcraft.yaml"),
    impact: buildCliArgv("impact", "swcraft.yaml", "ISpeed"),
    "impact --format json (Find All References)": buildImpactJsonArgv("swcraft.yaml", "ISpeed"),
    "impact --format json (the Impact view)": buildImpactTreeArgv("swcraft.yaml", "ISpeed"),
    "generate arxml --component": buildExportComponentArgv("swcraft.yaml", "SpeedSensor"),
    list: buildListArgv("swcraft.yaml"),
    "list swc": buildListArgv("swcraft.yaml", "swc"),
    init: buildInitArgv("/some/target"),
    "--help": buildPreflightArgv(),
  };
  for (const [label, argv] of Object.entries(argvs)) {
    for (const token of ["-m", "-c", "swcraft_tools.cli", "swcraft_mcp"]) {
      assert.ok(
        !argv.includes(token),
        `${label}'s argv still carries the interpreter token ${token}`,
      );
    }
    assert.ok(
      argv[0] !== undefined && !argv[0].startsWith("-") === (label !== "--help"),
      `${label}'s argv must start with what the CLI's parser reads first`,
    );
  }
});

test("commands.ts spawns the resolved executable and echoes the canonical command line", () => {
  // Structural, because `commands.ts` imports `vscode` and cannot be loaded here. Two halves: the
  // spawn takes the executable `toolchain.ts` resolved (no interpreter is resolved anywhere in the
  // adapter), and the echo is the line a reader can retype in a terminal, `$ swcraft validate
  // --project …`, which the Windows exit gate reads out of the output channel.
  const source = readSource("src/commands.ts");
  assert.ok(
    source.includes('`$ ${SWCRAFT_COMMAND_NAME} ${argv.join(" ")}`'),
    "spawnCli must echo `$ swcraft <argv>`, single-sourcing the name from toolchain.ts",
  );
  // The MCP provider needs the whole resolution (`exists` as well as `executable`), so the call
  // sits one function up and this reads both halves of the chain.
  assert.match(
    source,
    /function swcraftResolution\(\) \{\n {2}return resolveSwcraft\(/,
    "the adapter must resolve through toolchain.ts's one resolution",
  );
  assert.match(
    source,
    /function resolveSwcraftExecutable\(\): string \{\n {2}return swcraftResolution\(\)\.executable;\n\}/,
    "and the executable every spawn uses must be that resolution's, not a second one",
  );
  // The *argument*, not only the shape: `swcraft.executable` is the extension's executable
  // override, and replacing `executableSetting(),` with `undefined,` at a call site silently
  // ignores it with neither tsc nor Biome saying a word (the helper stays referenced elsewhere).
  //
  // Two resolutions are setting-free on purpose: both toolchain commands install into
  // `toolchainHome(…)` whatever the override says, so the override would describe a binary the
  // installer never touched. They take the setting as a PARAMETER, so the assertion is at their
  // call sites, where `undefined` is written once each on purpose.
  const resolutions = source.match(/resolveSwcraft\(/g) ?? [];
  assert.ok(
    resolutions.length >= 3,
    `commands.ts calls resolveSwcraft ${resolutions.length} times`,
  );
  const fromTheSetting = source.match(/resolveSwcraft\(\s*executableSetting\(\),/g) ?? [];
  const fromAParameter = source.match(/resolveSwcraft\(\s*setting,/g) ?? [];
  assert.equal(
    fromTheSetting.length + fromAParameter.length,
    resolutions.length,
    `${resolutions.length - fromTheSetting.length - fromAParameter.length} of ${resolutions.length} ` +
      "resolveSwcraft() calls in commands.ts start from neither executableSetting() nor a `setting` " +
      "parameter — that window ignores `swcraft.executable` and nothing above says why",
  );
  assert.ok(fromTheSetting.length >= 1, "the spawn path still starts from the setting");
  for (const [helper, honours, ignores] of [
    [
      "readToolchainHealth",
      // Check Editor Tooling adds its run's one reading as a third argument.
      /readToolchainHealth\(context, executableSetting\(\)(?:, reading)?\)/g,
      /readToolchainHealth\(context, undefined\)/g,
    ],
    [
      "installedToolchainVersion",
      /installedToolchainVersion\(executableSetting\(\)\)/g,
      /installedToolchainVersion\(undefined\)/g,
    ],
  ] as const) {
    // Comment lines are dropped first: a prose mention of `readToolchainHealth()`
    // is not a call, and counting one would make this guard fail on a rewording.
    const code = withoutCommentLines(source);
    const calls = code.match(new RegExp(`${helper}\\(`, "g")) ?? [];
    const named = (code.match(honours) ?? []).length + (code.match(ignores) ?? []).length;
    assert.equal(
      calls.length - 1,
      named,
      `${helper} is called ${calls.length - 1} time(s) but only ${named} pass an argument this guard ` +
        "recognises — every caller must say, in the call, whether it reads `swcraft.executable`",
    );
  }
  // The negative half is stated over shapes that can come *back*: a check for a symbol that exists
  // nowhere is vacuous. These two spellings are what an interpreter resolution would have to
  // reintroduce, and neither is in the production source.
  assertNoProductionModuleNames(
    [".proto/shims", "swcraft_tools.cli", "swcraft_mcp.server"],
    "every spawn is the resolved executable, never an interpreter",
  );
});

// --- parseListJson ---------------------------------------------------------
//
// The well-formed fixture below is the *verbatim* stdout of
//   .venv/bin/python -m swcraft_tools.cli list swc \
//       --project examples/basic/swcraft.yaml --format json
// so the parser is tested against the contract it consumes, not its author's assumptions.

const REAL_LIST_STDOUT = `{
  "schemaVersion": 1,
  "layers": {
    "swc": {
      "identityField": "name",
      "elements": [
        "ExampleConsumerSWC",
        "ExampleSWC"
      ]
    }
  }
}
`;

test("parseListJson: reads the real CLI output for examples/basic's swc layer", () => {
  assert.deepEqual(parseListJson(REAL_LIST_STDOUT), [
    { layer: "swc", elements: ["ExampleConsumerSWC", "ExampleSWC"] },
  ]);
});

test("parseListJson: keeps the document's layer order and each layer's element order", () => {
  const stdout = JSON.stringify({
    schemaVersion: LISTING_SCHEMA_VERSION,
    layers: {
      swc: { identityField: "name", elements: ["Zulu", "Alpha"] },
      interfaces: { identityField: "name", elements: ["ISpeed"] },
    },
  });
  assert.deepEqual(parseListJson(stdout), [
    { layer: "swc", elements: ["Zulu", "Alpha"] },
    { layer: "interfaces", elements: ["ISpeed"] },
  ]);
});

test("parseListJson: an empty element list is a valid answer, not an error", () => {
  const stdout = JSON.stringify({
    schemaVersion: LISTING_SCHEMA_VERSION,
    layers: { swc: { identityField: "name", elements: [] } },
  });
  assert.deepEqual(parseListJson(stdout), [{ layer: "swc", elements: [] }]);
});

test("parseListJson: an unknown schemaVersion is refused, and the message names the version it got", () => {
  const stdout = JSON.stringify({ schemaVersion: 7, layers: {} });
  assert.throws(
    () => parseListJson(stdout),
    (error: Error) => {
      assert.match(error.message, /schemaVersion/);
      assert.match(error.message, /\b7\b/);
      assert.match(error.message, new RegExp(String(LISTING_SCHEMA_VERSION)));
      return true;
    },
  );
});

test("parseListJson: a missing schemaVersion is refused the same way", () => {
  assert.throws(() => parseListJson(JSON.stringify({ layers: {} })), /schemaVersion/);
});

test("parseListJson: malformed JSON throws rather than yielding an empty list", () => {
  assert.throws(() => parseListJson("not json at all"), /as JSON/);
});

test("parseListJson: an empty stdout (a CLI that printed nothing) throws", () => {
  assert.throws(() => parseListJson(""), /as JSON/);
});

test("parseListJson: a JSON document that is not an object is refused", () => {
  assert.throws(() => parseListJson("[1, 2, 3]"), /not a JSON object/);
});

test("parseListJson: a document without a `layers` object is refused", () => {
  const stdout = JSON.stringify({ schemaVersion: LISTING_SCHEMA_VERSION });
  assert.throws(() => parseListJson(stdout), /`layers` object/);
});

test("parseListJson: a layer entry whose elements are not strings is refused, naming the layer", () => {
  const stdout = JSON.stringify({
    schemaVersion: LISTING_SCHEMA_VERSION,
    layers: { swc: { identityField: "name", elements: [{ name: "ExampleSWC" }] } },
  });
  assert.throws(() => parseListJson(stdout), /layer 'swc'/);
});

// --- elementsInLayer -------------------------------------------------------

test("elementsInLayer: an absent layer directory yields an empty list, not undefined", () => {
  // The key is present with an empty list — `listing.py` distinguishes "no
  // SWCs" from "no such layer" and the extension depends on that distinction.
  const listings = parseListJson(
    JSON.stringify({
      schemaVersion: LISTING_SCHEMA_VERSION,
      layers: { swc: { identityField: "name", elements: [] } },
    }),
  );
  assert.deepEqual(elementsInLayer(listings, "swc"), []);
});

test("elementsInLayer: a structurally valid document missing the requested layer yields undefined", () => {
  const listings = parseListJson(
    JSON.stringify({
      schemaVersion: LISTING_SCHEMA_VERSION,
      layers: { interfaces: { identityField: "name", elements: ["ISpeed"] } },
    }),
  );
  assert.equal(elementsInLayer(listings, "swc"), undefined);
});

test("elementsInLayer: the requested layer's names come back in the document's order", () => {
  assert.deepEqual(elementsInLayer(parseListJson(REAL_LIST_STDOUT), "swc"), [
    "ExampleConsumerSWC",
    "ExampleSWC",
  ]);
});

// --- decideExportComponentPrompt ------------------------------------------
//
// The three-way branch the name prompt carries, kept out of `commands.ts` so a wrong branch fails
// here instead of only in a hand-opened picker; the adapter half, `runNamePrompt()`, is only the
// `vscode.window.*` calls each outcome names. `undefined` (the CLI could not answer) and `[]` (the
// CLI answered "nothing") must never take the same branch, or a project with no SWCs gets an
// empty dead-end picker instead of the message that says what to do.

function listing(layers: Record<string, string[]>): LayerListing[] {
  return Object.entries(layers).map(([layer, elements]) => ({ layer, elements }));
}

test("decideExportComponentPrompt: a populated swc layer becomes a picker of its names", () => {
  const decision = decideExportComponentPrompt(listing({ swc: ["ExampleSWC", "SpeedSensor"] }));
  assert.equal(decision.kind, "pick");
  assert.deepEqual(decision.kind === "pick" ? decision.items : [], [
    { label: "ExampleSWC" },
    { label: "SpeedSensor" },
  ]);
});

test("decideExportComponentPrompt: an empty swc layer informs rather than opening an empty picker", () => {
  const decision = decideExportComponentPrompt(listing({ swc: [] }));
  assert.equal(decision.kind, "inform");
  assert.match(
    decision.kind === "inform" ? decision.message : "",
    /no SWCs to export/,
    "the message must say what to do, not just that the list is empty",
  );
});

test("decideExportComponentPrompt: an unobtainable listing falls back to free text, silently", () => {
  const decision = decideExportComponentPrompt(undefined);
  assert.equal(decision.kind, "input");
  // `listModel()` has already written the reason to the output channel; a
  // second note here would say it twice.
  assert.equal(decision.kind === "input" ? decision.note : "unreachable", undefined);
});

test("decideExportComponentPrompt: a document with no swc key is not the same answer as an empty one", () => {
  const decision = decideExportComponentPrompt(listing({ interfaces: ["ISpeed"] }));
  assert.equal(decision.kind, "input", "no `swc` key is not a claim that the project has no SWCs");
  assert.match(decision.kind === "input" ? (decision.note ?? "") : "", /no `swc` layer/);
  assert.notEqual(
    decision.kind,
    decideExportComponentPrompt(listing({ swc: [] })).kind,
    "an absent layer and an empty layer must take different branches",
  );
});

// --- decideCliUnavailablePrompt --------------------------------------------
//
// "The CLI could not run at all" must not take the free-text branch: every answer there would
// lead to a second failure, with the cause left in an output channel nothing had shown.

test("decideCliUnavailablePrompt: a spawn failure informs and cancels rather than prompting", () => {
  const decision = decideCliUnavailablePrompt("/home/u/.proto/shims/python");
  assert.equal(
    decision.kind,
    "inform",
    "a prompt whose every answer ends in a second failure is a dead end",
  );
  assert.notEqual(
    decision.kind,
    decideExportComponentPrompt(undefined).kind,
    "a spawn failure must not take the same branch as a listing that merely failed",
  );
});

test("decideCliUnavailablePrompt: it is a warning, unlike the model-shaped inform", () => {
  // "This project has no SWCs yet" is a statement about a young project;
  // "the interpreter is not there" is something wrong.
  const unavailable = decideCliUnavailablePrompt("/p");
  assert.equal(unavailable.kind === "inform" && unavailable.severity, "warning");
  const empty = decideExportComponentPrompt(listing({ swc: [] }));
  assert.equal(empty.kind === "inform" && empty.severity, undefined);
});

test("decideCliUnavailablePrompt: the message names the executable and the surface that diagnoses it", () => {
  const decision = decideCliUnavailablePrompt("/home/u/.swcraft/current/bin/swcraft");
  const message = decision.kind === "inform" ? decision.message : "";
  assert.match(message, /\/home\/u\/\.swcraft\/current\/bin\/swcraft/);
  assert.match(message, /Check Editor Tooling/);
  // The repair for an executable that will not start is the installer, not
  // onboarding: a project that installs nothing cannot create it.
  assert.match(message, /SWCraft: Install Toolchain/);
});

// --- One resolution per command run (the QuickPick TOCTOU) ------------------

test("impactCommand resolves the project once: the run uses the manifest the question was read against", () => {
  // The question is read against the manifest's model root (the active editor's
  // layer, a reference's target file); re-resolving for the run could ask project
  // B about a name read in project A — and `impact` answers an unknown name with an
  // empty `matched` and exit 0, so the mismatch would read as an answer.
  const source = withoutCommentLines(readSource("src/commands.ts"));
  for (const declaration of [
    "async function impactCommand(",
    "async function impactRefreshCommand(",
  ]) {
    const command = functionBody(source, declaration);
    assert.equal((command.match(/resolveManifestOrNotify\(\)/g) ?? []).length, 1, declaration);
    assert.match(command, /await askImpact\(resolved, /, declaration);
  }
  const impact = functionBody(source, "async function askImpact(");
  assert.equal(/resolveManifest/.test(impact), false, "the question never resolves again");
  assert.match(impact, /modelRoot: resolveModelRoot\(resolved\.manifestPath\)/);
  assert.match(impact, /await impactRun\(resolved, question\.name, refs, outputChannel\)\)/);
  const run = functionBody(source, "async function impactRun(");
  assert.match(run, /buildImpactTreeArgv\(resolved\.manifestRelPath, name\)/);
  assert.equal((run.match(/resolveSwcraftExecutable\(/g) ?? []).length, 1);
  assert.equal(/resolveManifest/.test(run), false, "the run never resolves the project again");
});

test("exportComponentCommand also resolves once and spawns with the same values", () => {
  // The same shape (a prompt between resolution and spawn): it resolves once and passes those
  // values straight to `spawnCli`.
  const body = sourceRegion(
    readSource("src/commands.ts"),
    "async function exportComponentCommand(",
    "async function importRequirementsCommand(",
  );
  assert.equal((body.match(/resolveManifestOrNotify\(\)/g) ?? []).length, 1);
  assert.equal((body.match(/resolveSwcraftExecutable\(/g) ?? []).length, 1);
});

test("importRequirementsCommand resolves once, so the applied run runs where the dry run ran", () => {
  // The dialog, the report and both questions sit between resolution and the
  // applied run; resolving again would let the yes given to one project's
  // report apply the file to another.
  const body = functionBody(
    withoutCommentLines(readSource("src/commands.ts")),
    "async function importRequirementsCommand(",
  );
  assert.equal((body.match(/resolveManifestOrNotify\(\)/g) ?? []).length, 1);
  assert.equal((body.match(/resolveSwcraftExecutable\(/g) ?? []).length, 1);
});

test("the name prompt declares a title, a picker placeholder and both free-text strings", () => {
  for (const text of [EXPORT_COMPONENT_PROMPT_TEXT]) {
    for (const [field, value] of Object.entries(text)) {
      assert.ok(value.trim().length > 0, `${text.title}: ${field} must not be empty`);
    }
    assert.match(text.title, /^SWCraft: /, "every prompt title names the extension");
  }
});

// --- validateSwcNameInput --------------------------------------------------
//
// The Export Component fallback's validator: when `swcraft list` cannot answer, the free-text
// prompt is what the user gets.

test("validateSwcNameInput: a non-empty name is valid (undefined = no error)", () => {
  assert.equal(validateSwcNameInput("SpeedSensor"), undefined);
});

test("validateSwcNameInput: empty input yields a user-facing error message", () => {
  const message = validateSwcNameInput("");
  assert.ok(message !== undefined);
  assert.match(message, /SWC name/);
});

test("validateSwcNameInput: whitespace-only input yields the same error as empty", () => {
  assert.equal(validateSwcNameInput("   "), validateSwcNameInput(""));
});

// --- mapExitCodeToOutcome / outcomeMessage ------------------------------

test("mapExitCodeToOutcome: exit code 0 is success", () => {
  assert.deepEqual(mapExitCodeToOutcome(0), { kind: "success" });
});

test("mapExitCodeToOutcome: non-zero exit code is failure carrying the code", () => {
  assert.deepEqual(mapExitCodeToOutcome(1), { kind: "failure", exitCode: 1 });
});

test("mapExitCodeToOutcome: null exit code (process never launched) is failure", () => {
  assert.deepEqual(mapExitCodeToOutcome(null), { kind: "failure", exitCode: null });
});

test("outcomeMessage: success has no message", () => {
  assert.equal(outcomeMessage("Check Model", { kind: "success" }), undefined);
});

test("outcomeMessage: failure includes the command label and exit code", () => {
  const message = outcomeMessage("Check Model", { kind: "failure", exitCode: 2 });
  assert.match(message ?? "", /Check Model/);
  assert.match(message ?? "", /2/);
});

test("outcomeMessage: the details default to the output channel", () => {
  const message = outcomeMessage("Diagrams", { kind: "failure", exitCode: 1 });
  assert.equal(
    message,
    "SWCraft: Diagrams failed (exit code 1). See the SWCraft output channel for details.",
  );
});

test("outcomeMessage: Check Model's failure names the Problems panel, not the channel", () => {
  // Check Model ends in Problems and reveals no channel, so its warning points
  // at the view it has just opened.
  const message =
    outcomeMessage("Check Model", { kind: "failure", exitCode: 1 }, PROBLEMS_PANEL_DETAILS) ?? "";
  assert.equal(
    message,
    "SWCraft: Check Model failed (exit code 1). See the Problems panel for details.",
  );
  assert.doesNotMatch(message, /output channel/);
});

test("only Check Model's recorded outcome is worded for the Problems panel", () => {
  const source = withoutCommentLines(readSource("src/commands.ts"));
  assert.match(
    functionBody(source, "async function checkModelCommand("),
    /recordCliOutcome\(commandId, "Check Model", run, PROBLEMS_PANEL_DETAILS\);/,
  );
  assert.equal(
    source.match(/PROBLEMS_PANEL_DETAILS\)/g)?.length,
    1,
    "no other command names Problems",
  );
});

test("outcomeMessage: null exit code renders as 'unknown'", () => {
  const message = outcomeMessage("Diagrams", { kind: "failure", exitCode: null });
  assert.match(message ?? "", /unknown/);
});

// --- Build: the six steps of the build graph ---------------------------------
//
// `Build` is a pure step list run by one loop in the adapter, and not six more `CliCommandKind`
// members: a kind is 1:1 with a VS Code command id and with the key its status is recorded under,
// and a step records none of its own.

const MANIFEST = "examples/basic/swcraft.yaml";

test("buildBuildSteps: the six argv in order, each with --project <manifest>, validate's then --report", () => {
  assert.deepEqual(
    buildBuildSteps(MANIFEST).map((step) => step.argv),
    [
      ["clean-outputs", "--project", MANIFEST],
      ["check-model", "--project", MANIFEST],
      ["generate", "arxml", "--project", MANIFEST],
      ["generate", "diagrams", "--project", MANIFEST],
      ["generate", "needs", "--project", MANIFEST],
      ["validate", "--project", MANIFEST, "--report"],
    ],
  );
});

test("buildBuildSteps: only the validate step asks for the build report, after --project", () => {
  const steps = buildBuildSteps(MANIFEST);
  assert.deepEqual(
    steps.filter((step) => step.argv.includes("--report")).map((step) => step.label),
    ["validate"],
  );
  const validate = steps.find((step) => step.label === "validate");
  assert.deepEqual(validate?.argv.slice(-1), ["--report"]);
});

test("buildBuildSteps: only check-model feeds the Problems panel, its argv the moon task's", () => {
  const steps = buildBuildSteps(MANIFEST);
  assert.deepEqual(
    steps.filter((step) => step.feedsDiagnostics === true).map((step) => step.label),
    ["check-model"],
  );
  const check = steps.find((step) => step.label === "check-model");
  assert.deepEqual(check?.argv, ["check-model", "--project", MANIFEST], "no --format in the step");
});

test("checkModelJsonArgv appends --format json to the caller's argv and changes nothing else", () => {
  const base = buildCliArgv("check-model", MANIFEST);
  assert.deepEqual(checkModelJsonArgv(base), [
    "check-model",
    "--project",
    MANIFEST,
    "--format",
    "json",
  ]);
  assert.deepEqual(base, ["check-model", "--project", MANIFEST], "the base argv is not mutated");
  const step = buildBuildSteps(MANIFEST).find((found) => found.feedsDiagnostics === true);
  assert.ok(step);
  assert.deepEqual(checkModelJsonArgv(step.argv), [...step.argv, "--format", "json"]);
});

test("buildBuildSteps: every step carries a distinct label spelled like its subcommand", () => {
  const steps = buildBuildSteps(MANIFEST);
  const labels = steps.map((step) => step.label);
  assert.deepEqual(labels, [
    "clean-outputs",
    "check-model",
    "generate arxml",
    "generate diagrams",
    "generate needs",
    "validate",
  ]);
  for (const step of steps) {
    // The label is what the warning names ("Build (generate needs) failed"), so it
    // is the words a reader can find in the `$ swcraft …` echo above it.
    assert.equal(step.argv.slice(0, step.argv.indexOf("--project")).join(" "), step.label);
  }
});

test("buildBuildSteps: the needs.json step comes after the diagrams and before the gate", () => {
  const labels = buildBuildSteps(MANIFEST).map((step) => step.label);
  const needs = labels.indexOf("generate needs");
  assert.ok(
    needs > labels.indexOf("generate diagrams"),
    "generate needs follows generate diagrams",
  );
  assert.equal(labels[needs + 1], "validate", "validate, the gate, follows generate needs");
});

test("no argv builder emits the retired rst format: generate rst is the CLI's usage error", () => {
  const argvs: Record<string, readonly string[]> = {
    ...Object.fromEntries(
      buildBuildSteps("swcraft.yaml").map((step) => [`build: ${step.label}`, step.argv]),
    ),
    "generate diagrams (Diagrams)": buildGenerateDiagramsArgv("swcraft.yaml"),
    "generate arxml --component": buildExportComponentArgv("swcraft.yaml", "SpeedSensor"),
  };
  for (const [label, argv] of Object.entries(argvs)) {
    assert.ok(!argv.includes("rst"), `${label}'s argv names the retired rst format`);
  }
});

test("buildRunMessage: a failing needs.json step is named generate needs in the warning", async () => {
  const result = await runBuildSteps(
    buildBuildSteps(MANIFEST),
    scriptedRunner(new Map([["generate needs", 1]])).run,
  );
  assert.equal(
    buildRunMessage(result),
    "SWCraft: Build (generate needs) failed (exit code 1). See the SWCraft output channel for details.",
  );
});

test("buildGenerateDiagramsArgv: Diagrams' generation is Build's own generate diagrams step, spelled once", () => {
  const generate = buildBuildSteps(MANIFEST).find((step) => step.label === "generate diagrams");
  assert.ok(generate, "Build carries a generate diagrams step");
  assert.deepEqual(buildGenerateDiagramsArgv(MANIFEST), [...generate.argv]);
});

test("commandCore.ts names no c4 argv symbol: `generate diagrams` is Build's fourth step and Diagrams' first", () => {
  // The CLI's argparse refuses `generate c4`, so a symbol still named for it would describe a
  // command nothing can run.
  const exported = Object.keys(commandCore);
  assert.deepEqual(
    exported.filter((name) => /c4/i.test(name)),
    [],
    `commandCore.ts still exports ${exported.join(", ")}`,
  );
  const steps = buildBuildSteps(MANIFEST);
  assert.equal(steps[3].label, "generate diagrams");
  assert.deepEqual(buildGenerateDiagramsArgv(MANIFEST), [...steps[3].argv]);
  for (const step of steps) {
    assert.ok(!step.argv.includes("c4"), `Build step '${step.label}' still spells c4`);
  }
});

// --- SerialRunner: one writer of views_root at a time ------------------------

function nextTurn(): Promise<void> {
  return new Promise((resolve) => setImmediate(resolve));
}

test("SerialRunner: a task handed in during another waits for it, and tasks run in order", async () => {
  const runner = new SerialRunner();
  const events: string[] = [];
  let releaseFirst: () => void = () => {};
  const first = runner.run(
    () =>
      new Promise<string>((resolve) => {
        events.push("first started");
        releaseFirst = () => {
          events.push("first finished");
          resolve("a");
        };
      }),
  );
  const second = runner.run(async () => {
    events.push("second started");
    return "b";
  });
  const third = runner.run(async () => {
    events.push("third started");
    return "c";
  });

  await nextTurn();
  assert.deepEqual(events, ["first started"], "nothing starts while the first task runs");
  releaseFirst();
  assert.deepEqual(await Promise.all([first, second, third]), ["a", "b", "c"]);
  assert.deepEqual(events, ["first started", "first finished", "second started", "third started"]);
});

test("SerialRunner: a rejected task hands its rejection to its caller and does not wedge the chain", async () => {
  const runner = new SerialRunner();
  const failed = runner.run(() => Promise.reject(new Error("clean-outputs failed")));
  const thrown = runner.run((): Promise<string> => {
    throw new Error("thrown before a promise existed");
  });
  const next = runner.run(async () => "ran");
  await assert.rejects(failed, /clean-outputs failed/);
  await assert.rejects(thrown, /thrown before a promise existed/);
  assert.equal(await next, "ran");
  assert.equal(await runner.run(async () => "after an idle chain"), "after an idle chain");
});

test("SerialRunner.busy: true from the call that queues a task until the last task has settled", async () => {
  // `toolchainCommand` reads it to refuse while a Build or a generation is
  // running, so it must turn true in the same synchronous turn as `run()` — a
  // click between the call and the task's start must already see it — and turn
  // false before the last caller resumes, rejection included.
  const runner = new SerialRunner();
  assert.equal(runner.busy, false, "an idle runner is not busy");
  let releaseFirst = (): void => {};
  const first = runner.run(
    () =>
      new Promise<string>((resolve) => {
        releaseFirst = () => resolve("a");
      }),
  );
  assert.equal(runner.busy, true, "busy in the same turn as run(), before the task has started");
  const second = runner.run(() => Promise.reject(new Error("refused")));
  await nextTurn();
  releaseFirst();
  assert.equal(await first, "a");
  assert.equal(runner.busy, true, "still busy while a queued task has not settled");
  await assert.rejects(second, /refused/);
  assert.equal(
    runner.busy,
    false,
    "idle again before the last caller resumes, after a rejection too",
  );
  const thrown = runner.run((): Promise<string> => {
    throw new Error("thrown before a promise existed");
  });
  await assert.rejects(thrown, /thrown before a promise existed/);
  assert.equal(
    runner.busy,
    false,
    "a task that throws synchronously does not leave the runner busy",
  );
});

test("a toolchain run and a views_root write refuse each other within one window", () => {
  // A Build resolves `<home>/current/bin/swcraft` once and spawns its six steps through it, and an
  // update switches `current` and removes the other version directories, so an overlapping install
  // could run later steps from another version or remove the tree a step runs from.
  // `toolchainCommand`'s half (it refuses while `viewsRootWrites` is busy) is pinned in
  // toolchainInstall.test.ts; this half: every entry into `viewsRootWrites` reads the flag before
  // it queues anything. What this does NOT reach: the CLI commands that write nothing under
  // views_root (Check Model, Validate, the Generate items, Export Component, Impact) are not
  // gated, and two windows, or a terminal, are not seen at all.
  const source = withoutCommentLines(readSource("src/commands.ts"));
  const guard = /if \(toolchainRunInFlight\) \{[\s\S]{0,400}?return;\s*\}/;

  const build = functionBody(source, "async function buildCommand(");
  const buildGuard = build.search(guard);
  assert.notEqual(buildGuard, -1, "Build refuses while a toolchain run is in flight");
  assert.ok(buildGuard < build.indexOf("viewsRootWrites.run("), "before it queues its run");
  assert.match(build.match(guard)?.[0] ?? "", /toolchainRunningRefusal\("Build"\)/);

  const diagrams = functionBody(source, "async function diagramsCommand(");
  const diagramsGuard = diagrams.search(guard);
  assert.notEqual(diagramsGuard, -1, "a Diagrams press refuses while a toolchain run is in flight");
  assert.ok(diagramsGuard < diagrams.indexOf("resolveViewsRoot("), "before it resolves views_root");
  assert.match(
    diagrams.match(guard)?.[0] ?? "",
    /finishDiagramsPress\(\{ kind: "refused", message: toolchainRunningRefusal\("Diagrams"\) \}\);/,
  );
});

test("toolchainRunningRefusal and viewsRootWriteRunningRefusal name the command, the wait and the repair", () => {
  for (const title of ["Build", "Diagrams"]) {
    const sentence = toolchainRunningRefusal(title);
    assert.match(sentence, new RegExp(`^SWCraft: ${title} was not started`));
    assert.match(sentence, /a toolchain install is running in this window/);
    assert.match(sentence, new RegExp(`run ${title} again when it has finished`));
  }
  const sentence = viewsRootWriteRunningRefusal();
  assert.match(sentence, /^SWCraft: the toolchain run was not started/);
  assert.match(sentence, /a Build or a diagram generation is running in this window/);
  assert.match(sentence, /run the toolchain command again when it has finished/);
});

test("commands.ts sends every views_root writer through one SerialRunner, and the open through none", () => {
  // The adapter half, read as text (commands.ts imports "vscode"). What this does
  // NOT reach: two windows on one folder (two extension hosts, two runners) and a
  // `swcraft` run from a terminal are not serialised with these.
  const source = withoutCommentLines(readSource("src/commands.ts"));
  assert.equal((source.match(/new SerialRunner\(\)/g) ?? []).length, 1, "one runner per window");
  assert.equal(
    (source.match(/viewsRootWrites\.run\(/g) ?? []).length,
    2,
    "exactly two serialised tasks: the Build run, and a Diagrams press's generate diagrams run",
  );
  assert.match(
    functionBody(source, "async function buildCommand("),
    /const steps = buildBuildSteps\(manifestRelPath\);[\s\S]*viewsRootWrites\.run\(\(\) =>\s*runBuildSteps\(steps,/,
    "the WHOLE Build run is one task, so nothing slips in between clean-outputs and generate diagrams",
  );
  assert.match(
    functionBody(source, "async function runGenerateDiagrams("),
    /viewsRootWrites\.run\(\(\) =>\s*spawnCli\(\s*resolveSwcraftExecutable\(\),\s*buildGenerateDiagramsArgv\(manifestRelPath\),\s*workspaceRoot,\s*outputChannel,?\s*\),?\s*\);/,
    "the generation is serialised and nothing else is",
  );
  const generateCall = /runGenerateDiagrams\(workspaceRoot, manifestRelPath, outputChannel\)/;
  const diagrams = functionBody(source, "async function diagramsCommand(");
  assert.match(diagrams, generateCall, "the press's generation is the shared serialised run");
  assert.equal(
    /viewsRootWrites\.run\(|spawnCli\(/.test(diagrams),
    false,
    "and the press spawns nothing else",
  );
  const generatedOpen = "finishDiagramsPress(pressOfShow(shown, view))";
  assert.ok(
    diagrams.indexOf("runGenerateDiagrams(") < diagrams.lastIndexOf(generatedOpen),
    "the generated path's open is outside the serialised task",
  );
  const directOpen = diagrams.indexOf(
    'diagramsTab.show(views.path, { view: requested, fragment: "" })',
  );
  assert.notEqual(directOpen, -1, "the direct-open branch's open is read");
  assert.ok(
    directOpen < diagrams.indexOf("runGenerateDiagrams("),
    "the direct open is not inside a viewsRootWrites.run( either: it comes before any run",
  );
  assert.equal(
    (source.match(/runGenerateDiagrams\(/g) ?? []).length,
    2,
    "one declaration and one caller, the press: a save generates nothing",
  );
});

/** One task of the template's moon.yml, as far as the order test reads it. */
interface MoonTask {
  name: string;
  args: string[];
  deps: string[];
}

/**
 * The `tasks:` block of the template's `moon.yml`, read by line.
 *
 * The extension carries no YAML parser and the file's shape is fixed by the template's own tests,
 * so this reads exactly that shape: a task at two spaces, a key at four, a list item at six, each
 * single-quoted. What it does **not** reach: flow sequences (`deps: ['a']`), other indentations,
 * and anything outside `tasks:`; a task spelled another way reads as one with no `args` and no
 * `deps`, which the coverage assertions below report as a step with no task.
 */
function templateMoonTasks(): MoonTask[] {
  const moonYml = path.join(
    PROJECT_ROOT,
    "..",
    "packages",
    "tools",
    "src",
    "swcraft_tools",
    "templates",
    "vehicle-architecture",
    "moon.yml",
  );
  const lines = fs.readFileSync(moonYml, "utf8").split(/\r?\n/);
  const start = lines.indexOf("tasks:");
  assert.notEqual(start, -1, "the template moon.yml has no top-level `tasks:` block");
  const tasks: MoonTask[] = [];
  let task: MoonTask | undefined;
  let list: string[] | undefined;
  for (const line of lines.slice(start + 1)) {
    if (/^\S/.test(line)) {
      break;
    }
    const taskHeader = /^ {2}([a-z][a-z0-9-]*):\s*$/.exec(line);
    if (taskHeader !== null) {
      task = { name: taskHeader[1], args: [], deps: [] };
      tasks.push(task);
      list = undefined;
      continue;
    }
    const key = /^ {4}([a-zA-Z]+):/.exec(line);
    if (key !== null && task !== undefined) {
      list = key[1] === "args" ? task.args : key[1] === "deps" ? task.deps : undefined;
      continue;
    }
    const item = /^ {6}- '([^']*)'\s*$/.exec(line);
    if (item !== null && list !== undefined) {
      list.push(item[1]);
    }
  }
  return tasks;
}

test("buildBuildSteps: the order is a topological order of the template moon.yml's task graph", () => {
  // The three generators are siblings in the graph, so the order is *a* linearisation, not *the*
  // one: every edge is asserted, never the literal list a second time. A step is matched to its
  // moon task by argv (the template's manifest is `swcraft.yaml`), not by name: the gate task is
  // `validate-arxml` and the step is `validate`, and a name mapping here would be a second truth.
  const steps = buildBuildSteps("swcraft.yaml");
  const tasks = templateMoonTasks();
  const byName = new Map(tasks.map((task) => [task.name, task]));
  const stepIndexOf = new Map<string, number>();
  for (const task of tasks.filter((candidate) => candidate.args.length > 0)) {
    const matches = steps
      .map((step, index) => ({ step, index }))
      .filter(({ step }) => JSON.stringify(step.argv) === JSON.stringify(task.args));
    assert.equal(
      matches.length,
      1,
      `moon task '${task.name}' (${task.args.join(" ")}) must be exactly one Build step`,
    );
    stepIndexOf.set(task.name, matches[0].index);
  }
  assert.equal(stepIndexOf.size, steps.length, "every Build step must be one moon task");

  let edges = 0;
  for (const [name, index] of stepIndexOf) {
    for (const dep of byName.get(name)?.deps ?? []) {
      const depIndex = stepIndexOf.get(dep);
      assert.ok(depIndex !== undefined, `'${name}' depends on '${dep}', which Build does not run`);
      assert.ok(
        depIndex < index,
        `Build runs '${steps[index].label}' before its dependency '${dep}'`,
      );
      edges += 1;
    }
  }
  assert.ok(edges >= 5, `only ${edges} dependency edges were read — the parser has gone blind`);
  // The generator the template names `generate-diagrams` is the fourth step; no task runs the
  // retired `generate c4`.
  assert.equal(stepIndexOf.get("generate-diagrams"), 3);
  assert.equal(
    byName.has("generate-c4"),
    false,
    "the template moon.yml still declares generate-c4",
  );
});

test("buildBuildSteps: Build runs exactly what `moon run :build` reaches", () => {
  const tasks = templateMoonTasks();
  const byName = new Map(tasks.map((task) => [task.name, task]));
  const build = byName.get("build");
  assert.ok(build, "the template moon.yml declares a `build` task");
  const reached = new Set<string>();
  const pending = [...build.deps];
  while (pending.length > 0) {
    const name = pending.pop() as string;
    if (reached.has(name)) {
      continue;
    }
    reached.add(name);
    pending.push(...(byName.get(name)?.deps ?? []));
  }
  const reachedArgv = [...reached]
    .map((name) => byName.get(name))
    .filter((task): task is MoonTask => task !== undefined && task.args.length > 0)
    .map((task) => JSON.stringify(task.args))
    .sort();
  const stepArgv = buildBuildSteps("swcraft.yaml")
    .map((step) => JSON.stringify(step.argv))
    .sort();
  assert.deepEqual(stepArgv, reachedArgv);
});

/**
 * A step runner that answers from a table of exit codes and records what it was
 * asked; every step's stderr names the step, so a result shows whose it carries.
 */
function scriptedRunner(exitCodes: ReadonlyMap<string, number | null>) {
  const ran: string[] = [];
  return {
    ran,
    run: async (step: BuildStep): Promise<RunOutput> => {
      ran.push(step.label);
      // `has`, not `?? 0`: a scripted `null` (could not be started) must stay null.
      const exitCode = exitCodes.has(step.label) ? (exitCodes.get(step.label) as number | null) : 0;
      return { exitCode, stderr: `stderr of ${step.label}\n`, spawnFailed: exitCode === null };
    },
  };
}

test("runBuildSteps: every step succeeding runs all six in order and is one success", async () => {
  const runner = scriptedRunner(new Map());
  const steps = buildBuildSteps(MANIFEST);
  const result = await runBuildSteps(steps, runner.run);
  assert.deepEqual(result, { kind: "success" });
  assert.deepEqual(
    runner.ran,
    steps.map((step) => step.label),
  );
  assert.equal(buildRunMessage(result), undefined, "a successful Build shows no warning");
});

test("runBuildSteps: the first failing step ends the run, and it is one failure naming that step", async () => {
  const runner = scriptedRunner(new Map([["generate arxml", 1]]));
  const steps = buildBuildSteps(MANIFEST);
  const result = await runBuildSteps(steps, runner.run);
  assert.deepEqual(runner.ran, ["clean-outputs", "check-model", "generate arxml"]);
  assert.equal(result.kind, "failure");
  assert.equal(result.kind === "failure" ? result.step.label : undefined, "generate arxml");
  assert.equal(result.kind === "failure" ? result.run.exitCode : undefined, 1);
  assert.equal(
    buildRunMessage(result),
    "SWCraft: Build (generate arxml) failed (exit code 1). See the SWCraft output channel for details.",
  );
});

test("runBuildSteps: the failure carries the failing step's run and no earlier step's", async () => {
  const runner = scriptedRunner(new Map([["clean-outputs", 1]]));
  const result = await runBuildSteps(buildBuildSteps(MANIFEST), runner.run);
  assert.deepEqual(result, {
    kind: "failure",
    step: buildBuildSteps(MANIFEST)[0],
    run: { exitCode: 1, stderr: "stderr of clean-outputs\n", spawnFailed: false },
  });
  const later = await runBuildSteps(
    buildBuildSteps(MANIFEST),
    scriptedRunner(new Map([["generate needs", 2]])).run,
  );
  assert.equal(
    later.kind === "failure" ? later.run.stderr : undefined,
    "stderr of generate needs\n",
  );
});

test("runBuildSteps: the failure keeps the step's own spawnFailed — a timed-out step was started", async () => {
  // `spawnCli` answers a timeout as `exitCode: null, spawnFailed: false`: the
  // failure says what the run said, never `exitCode === null` read as "not started".
  const steps = buildBuildSteps(MANIFEST);
  const timedOut = await runBuildSteps(steps, async () => ({
    exitCode: null,
    stderr: "timed out\n",
    spawnFailed: false,
  }));
  assert.deepEqual(timedOut.kind === "failure" ? timedOut.run : undefined, {
    exitCode: null,
    stderr: "timed out\n",
    spawnFailed: false,
  });
  const notStarted = await runBuildSteps(
    steps,
    scriptedRunner(new Map([["clean-outputs", null]])).run,
  );
  assert.equal(notStarted.kind === "failure" ? notStarted.run.spawnFailed : undefined, true);
});

test("runBuildSteps: a success carries no stderr, whatever the steps printed", async () => {
  const result = await runBuildSteps(buildBuildSteps(MANIFEST), scriptedRunner(new Map()).run);
  assert.deepEqual(result, { kind: "success" });
  assert.equal("stderr" in result, false);
});

test("runBuildSteps: a failing gate is the last step and still a failure", async () => {
  const runner = scriptedRunner(new Map([["validate", 1]]));
  const result = await runBuildSteps(buildBuildSteps(MANIFEST), runner.run);
  assert.equal(runner.ran.length, 6);
  assert.match(buildRunMessage(result) ?? "", /Build \(validate\) failed \(exit code 1\)/);
});

test("runBuildSteps: a step that could not be started stops the run like a failing one", async () => {
  // `spawnCli` answers `null` when the executable could not be launched at all;
  // the next step would fail the same way, five more times.
  const runner = scriptedRunner(new Map([["clean-outputs", null]]));
  const result = await runBuildSteps(buildBuildSteps(MANIFEST), runner.run);
  assert.deepEqual(runner.ran, ["clean-outputs"]);
  assert.match(
    buildRunMessage(result) ?? "",
    /Build \(clean-outputs\) failed \(exit code unknown\)/,
  );
});

// --- how a Build ends: Problems, the report, or nothing more -----------------------

function buildStep(label: string) {
  const step = buildBuildSteps(MANIFEST).find((candidate) => candidate.label === label);
  assert.ok(step, label);
  return step;
}

const VALIDATE_STDOUT = "Validation passed.\nGenerated report: dist/build-report.md\n";

test("buildRunEnding: a failure at check-model ends in Problems, whatever else was kept", () => {
  const result = {
    kind: "failure",
    step: buildStep("check-model"),
    run: { exitCode: 1, stderr: "", spawnFailed: false },
  } as const;
  assert.deepEqual(buildRunEnding(result, undefined), { kind: "problems" });
  assert.deepEqual(
    buildRunEnding(
      { ...result, run: { exitCode: null, stderr: "", spawnFailed: true } },
      undefined,
    ),
    { kind: "problems" },
  );
});

test("buildRunEnding: a run whose validate step ran opens the report, passed or failed", () => {
  const report = { kind: "report", stdout: VALIDATE_STDOUT };
  assert.deepEqual(buildRunEnding({ kind: "success" }, VALIDATE_STDOUT), report);
  assert.deepEqual(
    buildRunEnding(
      {
        kind: "failure",
        step: buildStep("validate"),
        run: { exitCode: 1, stderr: "", spawnFailed: false },
      },
      VALIDATE_STDOUT,
    ),
    report,
  );
  // A validate run that printed no report line still reaches the preview tail,
  // which says so in one channel line.
  assert.deepEqual(buildRunEnding({ kind: "success" }, ""), { kind: "report", stdout: "" });
});

test("buildRunEnding: a failure at clean-outputs or a generator ends at the warning", () => {
  for (const label of ["clean-outputs", "generate arxml", "generate diagrams", "generate needs"]) {
    assert.deepEqual(
      buildRunEnding(
        {
          kind: "failure",
          step: buildStep(label),
          run: { exitCode: 1, stderr: "", spawnFailed: false },
        },
        undefined,
      ),
      { kind: "none" },
      label,
    );
  }
});

test("buildRunEnding: the ending follows the kept stdout, not the result: none kept is no report", () => {
  // A success always ran validate, the last step, so its glue always kept a
  // stdout; the ending still opens no preview of a stdout it was never given.
  assert.deepEqual(buildRunEnding({ kind: "success" }, undefined), { kind: "none" });
  const steps = buildBuildSteps(MANIFEST);
  assert.equal(steps.at(-1)?.label, "validate");
  assert.deepEqual(
    steps.filter((step) => step.writesReport === true).map((step) => step.label),
    ["validate"],
  );
  assert.deepEqual(steps.at(-1)?.argv, ["validate", "--project", MANIFEST, "--report"]);
});

test("buildRunRefreshesResources: a green Build refreshes the view, a failure at any step does not", () => {
  assert.equal(buildRunRefreshesResources({ kind: "success" }), true);
  const [step] = buildBuildSteps(MANIFEST);
  assert.equal(
    buildRunRefreshesResources({
      kind: "failure",
      step,
      run: { exitCode: 1, stderr: "", spawnFailed: false },
    }),
    false,
  );
});

test("buildCommand ends by buildRunEnding: Problems on a check-model error, the report's preview after validate", () => {
  // The adapter half, read as text (commands.ts imports "vscode").
  const source = withoutCommentLines(readSource("src/commands.ts"));
  const body = functionBody(source, "async function buildCommand(");
  // The validate step's stdout is kept in the step callback; checkModelRun is not changed.
  assert.match(
    body,
    /const run = await spawnCli\([\s\S]*?\);\s*if \(step\.writesReport === true\) \{\s*validateStdout = run\.stdout;\s*\}\s*return run;/,
  );
  const statuses = [...body.matchAll(/recordCommandStatus\(\s*"swcraft\.build"/g)];
  const recorded = statuses.at(-1)?.index ?? -1;
  const warned = body.indexOf('notifyWithHelp("warning", message, helpTopicForRun(result.run));');
  const ending = body.indexOf("const ending = buildRunEnding(result, validateStdout);");
  assert.notEqual(recorded, -1, "the run's status is recorded");
  assert.notEqual(warned, -1, "the failure is warned");
  assert.notEqual(ending, -1, "buildCommand calls buildRunEnding(");
  assert.ok(ending > recorded && ending > warned, "the ending follows the status and the warning");
  const tail = body.slice(ending);
  assert.match(
    tail,
    /if \(ending\.kind === "problems"\) \{\s*void vscode\.commands\.executeCommand\(PROBLEMS_FOCUS_COMMAND\);\s*\} else if \(ending\.kind === "report"\) \{\s*await previewGeneratedReport\(ending\.stdout, workspaceRoot, outputChannel\);\s*\}\s*if \(buildRunRefreshesResources\(result\)\) \{\s*await refreshResourcesAfterBuild\(resolved, outputChannel\);\s*\}\s*\}?\s*$/,
    "the focus only in the problems branch, the preview only in the report branch, then the gated refresh, and nothing after",
  );
  // Exactly once each, and nowhere before the ending.
  assert.equal(body.match(/PROBLEMS_FOCUS_COMMAND/g)?.length, 1);
  assert.equal(body.match(/previewGeneratedReport\(/g)?.length, 1);
  assert.equal(
    body.slice(0, ending).match(/PROBLEMS_FOCUS_COMMAND|previewGeneratedReport\(/),
    null,
  );
  // Check Model's own focus move is unchanged: the last statement, after its outcome.
  assert.match(
    functionBody(source, "async function checkModelCommand("),
    /\n\s*recordCliOutcome\(commandId, "Check Model", run, PROBLEMS_PANEL_DETAILS\);\n\s*void vscode\.commands\.executeCommand\(PROBLEMS_FOCUS_COMMAND\);\n\}?\s*$/,
  );
});

// --- what a running Build or Diagrams says -----------------------------------------
//
// Only what is known is shown: Build is six steps the extension starts itself,
// so its text names the step and its place; Diagrams is one command that prints
// nothing until it has rendered every view, so it gets words and no fraction.

test("describeBuildProgress: 'Build — <k>/6 <step>' for each step, in the derived order", () => {
  const steps = buildBuildSteps(MANIFEST);
  assert.deepEqual(
    steps.map((_, index) => describeBuildProgress(steps, index)),
    [
      "Build — 1/6 clean-outputs",
      "Build — 2/6 check-model",
      "Build — 3/6 generate arxml",
      "Build — 4/6 generate diagrams",
      "Build — 5/6 generate needs",
      "Build — 6/6 validate",
    ],
  );
});

test("runBuildSteps: the progress advances by step — each step is announced as it starts", async () => {
  const steps = buildBuildSteps(MANIFEST);
  const shown: string[] = [];
  const ran: string[] = [];
  await runBuildSteps(steps, async (step, index) => {
    shown.push(describeBuildProgress(steps, index));
    ran.push(step.label);
    return { exitCode: 0, stderr: "", spawnFailed: false };
  });
  assert.equal(shown.length, 6);
  assert.deepEqual(
    shown,
    ran.map((label, index) => `Build — ${index + 1}/6 ${label}`),
  );
});

test("runBuildSteps: a failed step is the last one announced", async () => {
  const steps = buildBuildSteps(MANIFEST);
  const shown: string[] = [];
  await runBuildSteps(steps, async (step, index) => {
    shown.push(describeBuildProgress(steps, index));
    return { exitCode: step.label === "generate arxml" ? 1 : 0, stderr: "", spawnFailed: false };
  });
  assert.deepEqual(shown, [
    "Build — 1/6 clean-outputs",
    "Build — 2/6 check-model",
    "Build — 3/6 generate arxml",
  ]);
});

test("DIAGRAMS_PROGRESS_TEXT: 'generating diagrams…' and no fraction", () => {
  assert.equal(DIAGRAMS_PROGRESS_TEXT, "generating diagrams…");
  assert.doesNotMatch(DIAGRAMS_PROGRESS_TEXT, /\d|%|\//);
});

test("BUILD_WAITING_TEXT: before its first step a Build names no step", () => {
  assert.match(BUILD_WAITING_TEXT, /^Build — /);
  assert.doesNotMatch(BUILD_WAITING_TEXT, /\d\/\d/);
});

// --- which artifacts a Build run generated -----------------------------------------
//
// The panel marks the ARXML and the views *out of date* rather than regenerating them on a save,
// and a Build is the one run that generates both. Its first step removes both roots, so a run
// answers, per artifact, whether it left a successful generation behind: the views once
// `generate diagrams` exited 0, the ARXML only once the whole run did (`validate`, the gate, is
// its last step).

test("buildRunGenerations: a successful Build generated both artifacts", () => {
  assert.deepEqual(buildRunGenerations({ kind: "success" }), { arxml: true, views: true });
});

test("buildRunGenerations: a failure at or before generate diagrams generated neither", () => {
  for (const label of ["clean-outputs", "check-model", "generate arxml", "generate diagrams"]) {
    const step = buildBuildSteps(MANIFEST).find((candidate) => candidate.label === label);
    assert.ok(step, label);
    assert.deepEqual(
      buildRunGenerations({
        kind: "failure",
        step,
        run: { exitCode: 1, stderr: "", spawnFailed: false },
      }),
      { arxml: false, views: false },
      label,
    );
  }
});

test("buildRunGenerations: a failure after generate diagrams generated the views and not the ARXML", () => {
  // A Build that failed at `validate` leaves the ARXML on disk (the writer's rule; `validate`
  // checks after writing) with no success recorded, so the panel reads it *out of date*.
  for (const label of ["generate needs", "validate"]) {
    const step = buildBuildSteps(MANIFEST).find((candidate) => candidate.label === label);
    assert.ok(step, label);
    assert.deepEqual(
      buildRunGenerations({
        kind: "failure",
        step,
        run: { exitCode: 1, stderr: "", spawnFailed: false },
      }),
      { arxml: false, views: true },
      label,
    );
  }
});

test("buildRunGenerations: the diagrams step is found by its words, not by its index", () => {
  // The order of the three generators is a choice; if it moved, the rule must move with it rather
  // than read a stale index.
  const steps = buildBuildSteps(MANIFEST);
  const diagramsAt = steps.findIndex((step) => step.label === "generate diagrams");
  assert.notEqual(diagramsAt, -1);
  for (const [index, step] of steps.entries()) {
    assert.equal(
      buildRunGenerations({
        kind: "failure",
        step,
        run: { exitCode: 2, stderr: "", spawnFailed: false },
      }).views,
      index > diagramsAt,
      step.label,
    );
  }
});

test("buildCommand records exactly one status, under swcraft.build, from runBuildSteps", () => {
  // The adapter half, read as text (commands.ts imports "vscode"). One status per run: a
  // step-by-step record would overwrite itself six times and leave the item describing whichever
  // step was last. The refusal while a toolchain run is in flight records its own, before any
  // run, and returns; after the run exactly one is recorded.
  const body = withoutCommentLines(
    functionBody(readSource("src/commands.ts"), "async function buildCommand("),
  );
  const ids = [...body.matchAll(/recordCommandStatus\(\s*"([^"]+)"/g)].map((match) => match[1]);
  assert.deepEqual(ids, ["swcraft.build", "swcraft.build"], "the refusal's and the run's");
  const afterRun = body.slice(body.indexOf("viewsRootWrites.run("));
  assert.equal((afterRun.match(/recordCommandStatus\(/g) ?? []).length, 1);
  assert.match(body, /const steps = buildBuildSteps\(manifestRelPath\);/);
  assert.match(body, /runBuildSteps\(steps,/);
  assert.match(body, /spawnCli\(/);
});

test("diagramsCommand ends every path past the manifest in one finishDiagramsPress, under swcraft.diagrams", () => {
  // An item that never records a status renders permanently blank, which reads as broken rather
  // than idle. Which status and notification each ending gets is `reportDiagramsPress()`'s
  // (diagrams.test.ts); this holds that the adapter reaches it on every path. What it does NOT
  // reach: a path that returns through something other than a bare `return;`.
  const source = withoutCommentLines(readSource("src/commands.ts"));
  const body = functionBody(source, "async function diagramsCommand(");
  assert.equal(
    /recordCommandStatus\(|show(Error|Warning|Information)Message\(/.test(body),
    false,
    "the press records and notifies only through finishDiagramsPress",
  );
  const returns = (body.match(/return;/g) ?? []).length;
  assert.ok(returns >= 3, `only ${returns} returns were read — the slice has gone blind`);
  assert.match(
    body,
    /if \(resolved === undefined\) \{\s*return;/,
    "the one bare return is the missing manifest, which resolveManifestOrNotify reports",
  );
  const finished =
    body.match(
      /finishDiagramsPress\((?:\{[^}]*\}(, generated)?|pressOfShow\(shown, view\))\);\s*return;/g,
    ) ?? [];
  assert.equal(finished.length, returns - 1, "every other return is preceded by its report");
  assert.ok(
    finished.some((ending) =>
      /^finishDiagramsPress\(pressOfShow\(shown, view\)\);\s*return;$/.test(ending),
    ),
    "the direct open's return is preceded by its report",
  );
  assert.match(
    body,
    /finishDiagramsPress\(pressOfShow\(shown, view\)\);\s*\}\s*$/,
    "the shown path, whatever the tab answered, ends in a report too",
  );

  const finish = functionBody(source, "function finishDiagramsPress(");
  const ids = [...finish.matchAll(/recordCommandStatus\(\s*"([^"]+)"/g)].map((match) => match[1]);
  assert.deepEqual(ids, ["swcraft.diagrams"]);
  assert.match(finish, /reportDiagramsPress\(press\)/);
  // An information is shown here; every warning and error goes through
  // notifyWithHelp(), the one place a failed run's notification is shown.
  assert.match(
    finish,
    /if \(notification\.severity === "information"\) \{\s*void vscode\.window\.showInformationMessage\(/,
  );
  assert.match(finish, /notifyWithHelp\(\s*notification\.severity,\s*notification\.message,/);
});

// --- the read-only commands are not the gate ---------
//
// `swcraft resource-report` ALWAYS exits 0 on a successful run, even over budget, because
// `swcraft validate` is the gate and the read-only commands are not (`cli/read_commands.py`'s
// `cmd_resource_report`). The extension warns exactly when `outcomeMessage()` returns a message,
// so a zero exit mapping to success, and success having no message, pin "no invented warning".
// Warning on "over budget" in the report's text would make the panel disagree with the CLI about
// whether the command failed.

test("mapExitCodeToOutcome: a zero exit from resource-report is success, over budget or not", () => {
  const outcome = mapExitCodeToOutcome(0);
  assert.deepEqual(outcome, { kind: "success" });
  assert.equal(
    outcomeMessage("Resource Report", outcome),
    undefined,
    "a successful resource-report must produce no warning notification — its exit code is 0 " +
      "even when the report is over budget, and the extension must not invent a failure the " +
      "CLI did not report",
  );
});

test("mapExitCodeToOutcome: only a non-zero exit from a read-only command warns", () => {
  // The CLI still fails non-zero for a project it could not read at all, and that must still warn.
  const message = outcomeMessage("Resource Report", mapExitCodeToOutcome(1));
  assert.match(message ?? "", /Resource Report/);
});

// --- resolveManifest -----------------------------------------------------

test("resolveManifest: manifest present at workspace root (success)", () => {
  const dir = mkWorkspace();
  try {
    fs.writeFileSync(path.join(dir, "swcraft.yaml"), "name: test\n");
    const result = resolveManifest(dir);
    assert.equal(result.ok, true);
    if (result.ok) {
      assert.equal(result.manifestPath, path.join(dir, "swcraft.yaml"));
      assert.equal(result.manifestRelPath, "swcraft.yaml");
    }
  } finally {
    cleanup(dir);
  }
});

test("resolveManifest: no manifest yields an error result naming swcraft.yaml", () => {
  const dir = mkWorkspace();
  try {
    const result = resolveManifest(dir);
    assert.equal(result.ok, false);
    if (!result.ok) {
      assert.match(result.message, /swcraft\.yaml/);
    }
  } finally {
    cleanup(dir);
  }
});
// --- currentPlatform -----------------------------------------------------

test("currentPlatform: 'win32' maps to 'windows'", () => {
  assert.equal(currentPlatform("win32"), "windows");
});

test("currentPlatform: any non-'win32' platform maps to 'posix'", () => {
  assert.equal(currentPlatform("darwin"), "posix");
  assert.equal(currentPlatform("linux"), "posix");
});

// --- Onboarding: buildInitArgv / buildPreflightArgv / mapPreflightResult /
//     preflightNotFoundMessage / planOnboardingSteps -----------------------

test("buildInitArgv: produces 'swcraft init <targetDir>'", () => {
  assert.deepEqual(buildInitArgv("/some/target"), ["init", "/some/target"]);
});

test("buildPreflightArgv: produces 'swcraft --help'", () => {
  assert.deepEqual(buildPreflightArgv(), ["--help"]);
});

test("mapPreflightResult: exit code 0 with no spawn failure is ok", () => {
  assert.deepEqual(mapPreflightResult(0, false), { kind: "ok" });
});

test("mapPreflightResult: non-zero exit code (the launcher ran, its venv did not) is cli-not-found", () => {
  assert.deepEqual(mapPreflightResult(1, false), { kind: "cli-not-found" });
});

test("mapPreflightResult: null exit code with no spawn failure is cli-not-found", () => {
  assert.deepEqual(mapPreflightResult(null, false), { kind: "cli-not-found" });
});

test("mapPreflightResult: spawn failure (the executable is not there) is cli-not-found regardless of exit code", () => {
  assert.deepEqual(mapPreflightResult(0, true), { kind: "cli-not-found" });
});

test("preflightNotFoundMessage: names the executable, the install command and a 'no files written' note", () => {
  const message = preflightNotFoundMessage("/Users/dev/.swcraft/current/bin/swcraft");
  assert.match(message, /SWCraft CLI not found/);
  assert.match(message, /\/Users\/dev\/\.swcraft\/current\/bin\/swcraft/);
  // The repair is the workstation installer, never a `pip install` into an interpreter the
  // extension does not resolve.
  assert.match(message, /SWCraft: Install Toolchain/);
  assert.doesNotMatch(message, /pip install/);
  assert.match(message, /no files were written/);
});

test("planOnboardingSteps: always includes the preflight argv", () => {
  const steps = planOnboardingSteps("/some/target", true);
  assert.deepEqual(steps.preflightArgv, buildPreflightArgv());
});

test("planOnboardingSteps: there is no second, interpreter-shaped preflight any more", () => {
  // The extension resolves no interpreter to ask whether `swcraft_mcp` imports, and the
  // installation delivers all three packages through one declared requirement (`swcraft-agent`),
  // so no preflight step asks it.
  const steps = planOnboardingSteps("/some/target", true) as unknown as Record<string, unknown>;
  assert.equal(steps.mcpPreflightArgv, undefined);
});

test("planOnboardingSteps: hasExistingManifest true is a no-op for init (initArgv undefined)", () => {
  const steps = planOnboardingSteps("/some/target", true);
  assert.equal(steps.initArgv, undefined);
});

test("planOnboardingSteps: hasExistingManifest false runs swcraft init against targetDir", () => {
  const steps = planOnboardingSteps("/some/target", false);
  assert.deepEqual(steps.initArgv, buildInitArgv("/some/target"));
});

// --- The guide server's own output channel -------------------------------------------
//
// `swcraft docs` logs one line per HTTP request for as long as the server is up. In the SHARED
// `SWCraft` channel those lines would land between the lines of every later command's answer, and
// the panel is used by clicking a command and reading its answer.
//
// Both channels exist only in `commands.ts`, which imports "vscode", so the wiring is asserted
// structurally; what `serveGuideTo()` does with its channel (streams into it, reveals it on a
// failure only) is pinned in `guideServer.test.ts`.

test("registerCommands creates a channel for the guide beside the shared one, and disposes it", () => {
  const source = readSource("src/commands.ts");
  assert.match(
    source,
    /createOutputChannel\(GUIDE_OUTPUT_CHANNEL_NAME\)/,
    "the guide channel must be created from guideServer.ts's exported name, not a retyped literal",
  );
  const registration = sourceRegion(
    source,
    "export function registerCommands(",
    'vscode.commands.registerCommand("swcraft.build"',
  );
  // A Diagrams press is one CLI run, so its transcript belongs in the shared channel like every
  // other one.
  assert.equal(
    (registration.match(/createOutputChannel\(/g) ?? []).length,
    2,
    "exactly two output channels: the shared transcript and the guide's request log",
  );
  assert.equal(/DIAGRAMS_OUTPUT_CHANNEL_NAME|diagramsChannel/.test(source), false);
  assert.match(
    registration,
    /context\.subscriptions\.push\(guideChannel\)/,
    "the guide channel must be disposed with the extension, like the shared one and GuideSession",
  );
});

// --- SWCraft: Diagrams — the adapter half --------------------
//
// `diagramsCommand` lives in commands.ts, which imports "vscode", so what it does is asserted
// structurally here; what a press reports and the watch it arms are pinned in diagrams.test.ts.
// What these scans do NOT reach: that a real extension host shows the page in the webview panel.

test("diagramsCommand generates, then shows index.html of views_root in the diagrams' tab", () => {
  const body = withoutCommentLines(
    functionBody(readSource("src/commands.ts"), "async function diagramsCommand("),
  );
  const at = (needle: string): number => {
    const index = body.indexOf(needle);
    assert.notEqual(index, -1, `diagramsCommand no longer contains ${needle}`);
    return index;
  };
  const order = [
    "if (toolchainRunInFlight)",
    "resolveViewsRoot(manifestPath)",
    "const requested = diagramsArgument(arg, listViewsRoot(views.path), { regenerate })",
    "!regenerate &&",
    'diagramsTab.show(views.path, { view: requested, fragment: "" })',
    "outputChannel.show(true)",
    "const startedAt = Date.now()",
    "runGenerateDiagrams(workspaceRoot, manifestRelPath, outputChannel)",
    'outcomeMessage("Diagrams"',
    'recordGeneration("views", workspaceRoot, startedAt)',
    "found: fs.existsSync(diagramsViewPath(views.path, requested))",
    'const shown = diagramsTab.show(\n    views.path,\n    { view: view?.found === true ? view.id : undefined, fragment: "" },\n    { justGenerated: true },\n  );',
    "  );\n  finishDiagramsPress(pressOfShow(shown, view));\n}",
  ].map(at);
  assert.equal(
    /openExternal|Uri\.file\(/.test(body),
    false,
    "the press shows its page in the tab, never in a browser (D8)",
  );
  assert.deepEqual(
    [...order].sort((a, b) => a - b),
    order,
    "the steps must run in this order",
  );
  // Each refusal ends the press before the next step: a refused generation opens nothing and
  // records no success; the last good pages stay on disk and stay out of date. The generation is
  // stamped with the instant before it was queued, so a save made while it waited or ran still
  // marks its output.
  const between = (from: string, to: string) => body.slice(at(from), at(to));
  assert.match(between("resolveViewsRoot(manifestPath)", "runGenerateDiagrams("), /return;/);
  assert.match(between('outcomeMessage("Diagrams"', "recordGeneration("), /return;/);
  assert.equal(/armFor|\.dispose\(\)/.test(body), false, "a press arms no watch: activation did");
  // No runtime of the extension's own: the CLI finds Node.js itself.
  assert.equal(/resolveRuntime|runtimeAbsentMessage|serveDiagramsTo|Uri\.parse/.test(body), false);
  assert.match(
    readSource("src/commands.ts"),
    /registerCommand\("swcraft\.diagrams", \(arg\) =>\s*diagramsCommand\(outputChannel, arg\)/,
    "swcraft.diagrams is registered against the shared channel, a lens's argument passed on",
  );
});

test("a Diagrams generation is spawnCli's own run: no environment variable removed or added", () => {
  // The generator is a CLI command and gets exactly the environment every other one gets, with no
  // rewriting of its own. What this does NOT reach: what `cliEnv()` itself sets, which is pinned
  // beside spawnCli.
  const source = withoutCommentLines(readSource("src/commands.ts"));
  const run = functionBody(source, "async function runGenerateDiagrams(");
  assert.match(
    run,
    /viewsRootWrites\.run\(\(\) =>\s*spawnCli\(\s*resolveSwcraftExecutable\(\),\s*buildGenerateDiagramsArgv\(manifestRelPath\),\s*workspaceRoot,\s*outputChannel,?\s*\),?\s*\);/,
  );
  assert.equal(/\benv\b|process\.env/.test(run), false);
  for (const spelling of [
    "NO_COLOR",
    "BROWSER",
    '"TEST"',
    "TEST: ",
    'mkdtempSync(path.join(os.tmpdir(), "swcraft-diagrams-',
  ]) {
    assert.equal(source.includes(spelling), false, `commands.ts still spells ${spelling}`);
  }
});

test("the model watch is disposed with the window and re-armed on a workspace-folder change", () => {
  const registration = withoutCommentLines(
    sourceRegion(
      readSource("src/commands.ts"),
      "export function registerCommands(",
      'vscode.commands.registerCommand("swcraft.build"',
    ),
  );
  assert.match(registration, /context\.subscriptions\.push\(\s*modelWatch,/);
  assert.match(
    registration,
    /onDidChangeWorkspaceFolders\(\(\) => \{\s*modelWatch\.dispose\(\);\s*armModelWatch\(\);/,
    "a folder change must dispose the watch over the folder that was open",
  );
});

// --- the artifacts' state: the adapter half ---------------------------
//
// The rule (absent, current, out of date) and the badge are treeView.ts's, pinned in
// treeView.test.ts; which artifacts a Build generated is `buildRunGenerations()`'s, pinned above.
// These scans hold the wiring only commands.ts can carry. They do NOT reach whether VS Code
// delivers a watcher event for a save, draws the ThemeIcon's colour, or shows the badge on the
// activity-bar icon; that is checked by hand in a real window.

test("the watch's pattern watcher reports the path of every create, change and delete", () => {
  const source = readSource("src/commands.ts");
  const watcher = withoutCommentLines(functionBody(source, "function createPatternWatcher("));
  assert.match(
    watcher,
    /createFileSystemWatcher\(\s*new vscode\.RelativePattern\(vscode\.Uri\.file\(pattern\.base\), pattern\.glob\),?\s*\)/,
  );
  for (const event of ["onDidCreate", "onDidChange", "onDidDelete"]) {
    assert.match(
      watcher,
      new RegExp(`watcher\\.${event}\\(\\(uri\\) => onEvent\\(uri\\.fsPath\\)\\)`),
      event,
    );
  }
  assert.match(
    watcher,
    /vscode\.Disposable\.from\([\s\S]*watcher,?\s*\)/,
    "disposing the pattern disposes the watcher itself, not only its listeners",
  );
});

test("buildCommand records the artifacts its run generated, stamped before the run was queued", () => {
  const body = withoutCommentLines(
    functionBody(readSource("src/commands.ts"), "async function buildCommand("),
  );
  const stamp = body.indexOf("const startedAt = Date.now()");
  assert.notEqual(stamp, -1);
  assert.ok(stamp < body.indexOf("viewsRootWrites.run("), "stamped before it waits its turn");
  assert.match(
    body,
    /artifactTracker\?\.recordBuild\(workspaceRoot, startedAt, buildRunGenerations\(result\)\)/,
  );
  assert.ok(
    body.indexOf("recordBuild(") > body.indexOf("viewsRootWrites.run("),
    "recorded once the run has an outcome",
  );
});

test("the tree is a createTreeView, and its badge is describeBadge's", () => {
  const source = withoutCommentLines(readSource("src/commands.ts"));
  const register = functionBody(source, "export function registerTreeView(");
  assert.match(
    register,
    /vscode\.window\.createTreeView\(TREE_VIEW_ID, \{ treeDataProvider: provider \}\)/,
  );
  assert.equal(
    /registerTreeDataProvider/.test(source),
    false,
    "no second registration of the view",
  );
  assert.match(source, /\.badge = describeBadge\(/);
});

test("the out-of-date mark is drawn from the pure half's icon, never decided in the adapter", () => {
  const source = withoutCommentLines(readSource("src/commands.ts"));
  assert.match(
    source,
    /new vscode\.ThemeIcon\(\s*descriptor\.icon\.id,\s*new vscode\.ThemeColor\(descriptor\.icon\.color\),?\s*\)/,
  );
  assert.equal(
    /"outOfDate"|"circle-filled"|"charts\.yellow"/.test(source),
    false,
    "which state is marked, and how, is treeView.ts's",
  );
});

test("the last successful generation is kept per artifact in workspaceState", () => {
  const source = withoutCommentLines(readSource("src/commands.ts"));
  assert.match(
    source,
    /new ArtifactRecord\(\s*context\.workspaceState,\s*scanNewestChangeForBoth,\s*artifactsChanged,?\s*\)/,
  );
  assert.match(source, /newestSourceChange\(/, "compared with the newest model file on activation");
  // The transitions themselves are ArtifactRecord's, unit-tested in treeView.test.ts;
  // the adapter asks the disk and stamps each event with a stamp taken on arrival.
  const note = functionBody(source, "function noteModelChange(");
  assert.match(note, /artifactTracker\?\.changeStamp\(Date\.now\(\)\)/);
  assert.match(note, /noted\(stat\.mtimeMs\)/);
  assert.match(note, /noted\(Date\.now\(\)\)/);
  // The Resources report and the Impact answer take the same stamp as the artifacts,
  // whether or not a tracker is armed.
  assert.match(
    note,
    /markResourcesOutOfDate\(changedAt\);\s*markImpactOutOfDate\(changedAt\);\s*stamp\?\.\(changedAt\);/,
  );
  // A stat answered after the watch was armed again (another folder) marks nothing.
  assert.match(note, /const generation = modelWatchGeneration;/);
  assert.match(
    note,
    /if \(generation !== modelWatchGeneration\) \{\s*return;\s*\}\s*markResourcesOutOfDate\(changedAt\);/,
  );
  // An arming drops the stamps still waiting on their stat; the rescan reads the disk in their
  // place, and its answer marks the Resources report too, unless a later arming outran it.
  const both = functionBody(source, "async function scanNewestChangeForBoth(");
  assert.match(
    both,
    /const generation = modelWatchGeneration;\s*const newest = await scanNewestChange\(folder\);/,
  );
  assert.match(
    both,
    /if \(newest !== undefined && generation === modelWatchGeneration\) \{\s*markResourcesOutOfDate\(newest\);\s*\}\s*return newest;/,
  );
  assert.match(
    functionBody(source, "function armModelWatch("),
    /function armModelWatch\(\): void \{\s*modelWatchGeneration \+= 1;/,
    "every arming moves the generation first",
  );
  const states = functionBody(source, "function artifactStates(");
  assert.match(states, /const onDisk = artifactsOnDisk\(folder\);/);
  assert.match(states, /artifactTracker\.states\(folder, onDisk\)/);
  // Nothing is probed in a manifest the loader refuses: both existsSync calls sit
  // behind readManifestPaths()' whole-manifest verdict, in the one helper the
  // tree's states and the lens's direct open share.
  const probe = functionBody(source, "function artifactsOnDisk(");
  assert.match(probe, /const accepted = paths\.refusal === undefined;/);
  assert.match(probe, /arxml: accepted && fs\.existsSync\(paths\.resolved\["outputs\.arxml"\]\)/);
  assert.match(
    probe,
    /views: accepted && fs\.existsSync\(diagramsIndexPath\(paths\.resolved\.views_root\)\)/,
  );
  assert.equal((probe.match(/existsSync\(/g) ?? []).length, 2, "no other probe");
  const diagrams = functionBody(source, "async function diagramsCommand(");
  for (const [name, body] of [
    ["artifactStates", states],
    ["diagramsCommand", diagrams],
  ]) {
    assert.doesNotMatch(body, /existsSync\(diagramsIndexPath/, `${name} probes nothing of its own`);
  }
  assert.match(
    diagrams,
    /const onDisk = requested === undefined \? undefined : artifactsOnDisk\(workspaceRoot\);/,
  );
  assert.match(diagrams, /artifactTracker\?\.opensDirectly\("views", workspaceRoot, onDisk\)/);
});

// --- a running Build or Diagrams: the adapter half ----------------------
//
// The texts are commandCore.ts's and the item's `running…` treeView.ts's, pinned above and in
// treeView.test.ts. These scans hold the wiring: three places and no popup (the view's own bar, a
// status-bar text, the item's description) and no cancel. They do NOT reach how VS Code renders
// the bar or the status-bar entry, or whether it does so in a remote window.

test("withRunIndicator: the view's own bar and a status-bar text, no notification, no cancel", () => {
  const body = withoutCommentLines(
    functionBody(readSource("src/commands.ts"), "async function withRunIndicator"),
  );
  assert.match(body, /withProgress\(\s*\{ location: \{ viewId: TREE_VIEW_ID \} \}/);
  assert.match(body, /location: vscode\.ProgressLocation\.Window/);
  assert.doesNotMatch(body, /ProgressLocation\.Notification/, "no popup");
  assert.doesNotMatch(body, /cancellable/, "cancel is not this slice's");
  assert.match(body, /runningCommands\.start\(commandId\)/);
  assert.match(
    body,
    /finally \{\s*runningCommands\.finish\(commandId\);/,
    "the flag goes whatever the outcome",
  );
});

test("the running set reaches the tree, and no command's popup reports progress", () => {
  const source = withoutCommentLines(readSource("src/commands.ts"));
  assert.match(source, /buildTree\(statuses, new Date\(\), artifacts, runningCommands, setup\)/);
  assert.equal(
    (source.match(/ProgressLocation\.Notification/g) ?? []).length,
    0,
    "a run is shown in the panel and the status bar, never in a notification",
  );
});

test("buildCommand announces each step as it starts, inside the one serialised run", () => {
  const body = withoutCommentLines(
    functionBody(readSource("src/commands.ts"), "async function buildCommand("),
  );
  assert.match(body, /withRunIndicator\("swcraft\.build", BUILD_WAITING_TEXT,/);
  assert.match(body, /report\(describeBuildProgress\(steps, index\)\)/);
  const indicator = body.indexOf("withRunIndicator(");
  assert.ok(indicator < body.indexOf("viewsRootWrites.run("), "the queued wait is shown too");
  assert.ok(
    body.lastIndexOf("recordCommandStatus(") > body.indexOf("viewsRootWrites.run("),
    "the outcome replaces running… once the run has ended",
  );
});

test("diagramsCommand shows 'generating diagrams…' for the generation only, not the open", () => {
  const body = withoutCommentLines(
    functionBody(readSource("src/commands.ts"), "async function diagramsCommand("),
  );
  assert.match(
    body,
    /withRunIndicator\("swcraft\.diagrams", DIAGRAMS_PROGRESS_TEXT, \(\) =>\s*runGenerateDiagrams\(workspaceRoot, manifestRelPath, outputChannel\),?\s*\)/,
  );
  assert.ok(
    body.indexOf("withRunIndicator(") < body.lastIndexOf("diagramsTab.show("),
    "the indicator ends before the tab is asked to show the generated page",
  );
  const branchStart = body.indexOf("if (\n    !regenerate &&\n    requested !== undefined");
  assert.notEqual(branchStart, -1, "the direct-open branch is read");
  const branch = body.slice(branchStart, body.indexOf("return;", branchStart));
  assert.match(branch, /diagramsTab\.show\(views\.path, \{ view: requested, fragment: "" \}\)/);
  for (const absent of ["withRunIndicator(", "outputChannel.show(", "recordGeneration("]) {
    assert.equal(branch.includes(absent), false, `the direct open holds no ${absent}`);
  }
});

/** Every registered command id and the top-level function its handler calls, read off the source. */
function registeredHandlers(source: string): Array<[string, string]> {
  // Two shapes: `() => handler(…)` and, for a command a CodeLens passes an argument to,
  // `(arg) => handler(…, arg)`; `(arg, lensUri)` for the *impact* lens, which passes its document
  // too.
  const pattern = /registerCommand\(\s*"([^"]+)",\s*\((?:\w+(?:,\s*\w+)?)?\)\s*=>\s*(\w+)\(/g;
  const pairs: Array<[string, string]> = [];
  let match = pattern.exec(source);
  while (match !== null) {
    pairs.push([match[1], match[2]]);
    match = pattern.exec(source);
  }
  return pairs;
}

/**
 * The names of the functions in *source* that show a run indicator — directly,
 * or by calling one that does. Derived by walking the file's own function
 * bodies, so nothing about which command is covered is written twice.
 */
function functionsShowingTheIndicator(source: string): Set<string> {
  const bodies = new Map<string, string>();
  const declaration = /\n(?:async )?function (\w+)\(/g;
  let found = declaration.exec(source);
  while (found !== null) {
    bodies.set(found[1], functionBody(source, found[0].slice(1)));
    found = declaration.exec(source);
  }
  assert.ok(bodies.size > 10, "expected the command module's functions to be readable");
  const showing = new Set(
    [...bodies].filter(([, body]) => body.includes("withRunIndicator(")).map(([name]) => name),
  );
  for (let pass = 0; pass < bodies.size; pass += 1) {
    const before = showing.size;
    for (const [name, body] of bodies) {
      if (!showing.has(name) && [...showing].some((called) => body.includes(`${called}(`))) {
        showing.add(name);
      }
    }
    if (showing.size === before) {
      break;
    }
  }
  return showing;
}

test("every command that spawns the toolchain shows that it is running, and only those", () => {
  // Every command that spawns shows the indicator: `Check Model` on a large model takes many
  // seconds. The set is written once, in `SPAWNING_COMMAND_LABELS`; both halves below are derived
  // from `commands.ts`, so a command added to the table and not wrapped (or wrapped and not added)
  // fails, and so does a new registered command classified as neither.
  //
  // What it does not reach: that the indicator *renders* in a real VS Code window, and that a
  // wrapped call covers the spawn rather than a prompt before it (the ordering guards above say
  // that, per command).
  const source = withoutCommentLines(readSource("src/commands.ts"));
  const handlers = registeredHandlers(source);
  assert.ok(handlers.length > 5, "expected registerCommand(...) calls in commands.ts");
  const showing = functionsShowingTheIndicator(source);
  const covered = handlers.filter(([, handler]) => showing.has(handler)).map(([id]) => id);
  assert.deepEqual(
    covered.slice().sort(),
    Object.keys(SPAWNING_COMMAND_LABELS).slice().sort(),
    "SPAWNING_COMMAND_LABELS must name exactly the commands whose handler shows the indicator",
  );
  // The registered commands that show nothing: the guide server, which is not
  // expected to exit, so there is no run to be in flight; the Trace Links
  // view's commands that only edit the proposals file or open a file; the
  // Pending Change Sets view's two, which re-read the reviews directory and open a
  // review tab; the Resources view's Clear and row click; and Show in Diagram, which
  // reads the diagrams' index and shows a page — stated lists, so a
  // new command that spawns without the indicator still fails.
  assert.deepEqual(
    handlers
      .filter(([id]) => !covered.includes(id))
      .map(([id]) => id)
      .sort(),
    [
      "swcraft.openGuide",
      ...TRACE_NON_SPAWNING_COMMANDS,
      ...REVIEW_VIEW_COMMANDS,
      ...RESOURCE_NON_SPAWNING_COMMANDS,
      ...IMPACT_NON_SPAWNING_COMMANDS,
      ...DIAGRAM_EDITOR_COMMANDS,
    ].sort(),
  );
});

test("describeRunning: '<Command> — running…', the grammar the Build text uses", () => {
  assert.equal(describeRunning("swcraft.checkModel"), "Check Model — running…");
  assert.equal(describeRunning("swcraft.updateToolchain"), "Update Toolchain — running…");
  for (const id of Object.keys(SPAWNING_COMMAND_LABELS)) {
    assert.match(describeRunning(id), / — running…$/);
    assert.doesNotMatch(describeRunning(id), /\d|%|\//, "no fraction, no percentage of time");
  }
  assert.match(BUILD_WAITING_TEXT, / — /, "one grammar for both");
});

test("a prompt is not a run: the indicator starts on the answer, never over the dialog", () => {
  const source = withoutCommentLines(readSource("src/commands.ts"));
  for (const [declaration, prompt] of [
    ["async function exportComponentCommand(", "promptForSwcName("],
    ["async function onboardProjectCommand(", "showOpenDialog("],
    ["async function toolchainCommand(", "pickArchiveSource("],
    ["async function testTablesCommand(", "askTestTablesAnnotations("],
  ] as const) {
    const body = functionBody(source, declaration);
    const asked = body.indexOf(prompt);
    const run = body.indexOf("withRunIndicator(");
    assert.notEqual(asked, -1, `${declaration} calls ${prompt}`);
    assert.notEqual(run, -1, `${declaration} shows the indicator`);
    assert.ok(asked < run, `${declaration} must ask before it says a run is in flight`);
  }
  // The imports pick their file in their flow, before any run, and a cancelled
  // pick runs nothing: importTabCore.test.ts drives both flows with fakes.
});

test("onboardProjectCommand: the picker opens on the workspace folder, a nested or enclosing folder is refused before anything runs", () => {
  // What it does not reach: the dialog and the message box themselves (no harness drives them);
  // the walks, their order and the own-manifest condition on the walk down are
  // `onboardingRefusal`'s (onboarding.test.ts). What it pins is the order the source states: pick,
  // the cancelled pick, the one refusal (output channel, failure status, error message, return),
  // then the run.
  const body = withoutCommentLines(
    functionBody(readSource("src/commands.ts"), "async function onboardProjectCommand("),
  );
  const positionOf = (marker: string | RegExp): number => {
    const at = typeof marker === "string" ? body.indexOf(marker) : body.search(marker);
    assert.notEqual(at, -1, `onboardProjectCommand holds ${String(marker)}`);
    return at;
  };
  const dialog = positionOf("showOpenDialog(");
  assert.ok(positionOf("defaultUri:") > dialog, "the picker passes defaultUri");
  assert.ok(positionOf("getWorkspaceRoot()") < dialog, "the default is the workspace folder");
  const cancelled = positionOf(
    /if \(picked === undefined \|\| picked\.length === 0\) \{\s*return;\s*\}/,
  );
  const decided = positionOf("onboardingRefusal(targetDir, workspaceFolders)");
  const refused = positionOf(
    /if \(refusal !== undefined\) \{\s*outputChannel\.appendLine\(refusal\);\s*recordCommandStatus\("swcraft\.onboardProject", \{ kind: "failure", message: refusal \}\);\s*void vscode\.window\.showErrorMessage\(refusal\);\s*return;\s*\}/,
  );
  const run = positionOf("withRunIndicator(");
  assert.ok(
    dialog < cancelled && cancelled < decided && decided < refused,
    "pick, then the refusal",
  );
  assert.ok(refused < run, "the refusal before the run indicator");
  for (const walk of ["enclosingProject(", "containedProject("]) {
    assert.equal(body.indexOf(walk), -1, `${walk} runs inside onboardingRefusal, not here`);
  }
  assert.equal(body.indexOf("spawnCli("), -1, "the command itself spawns nothing");
  assert.ok(body.indexOf("runOnboarding(") > run, "the spawning body runs inside the indicator");
});

test("the Test Tables press: the tab's webview options, one press at a time, the refusal page, the unreadable line, the ready message", () => {
  // The pure half (the reader, the pages, the pick, the argv) runs in testTablesTab.test.ts; this
  // pins how commands.ts drives it. What it does not reach: the webview and the dialogs
  // themselves (no harness drives them).
  const source = withoutCommentLines(readSource("src/commands.ts"));
  const show = functionBody(source, "function showTestTablesTab(");
  assert.match(
    show,
    /vscode\.window\.createWebviewPanel\(\s*TEST_TABLES_VIEW_TYPE,\s*TEST_TABLES_TAB_TITLE,\s*\{ viewColumn: vscode\.ViewColumn\.Active, preserveFocus: false \},\s*\{ enableScripts: true, localResourceRoots: \[\] \},\s*\);/,
  );
  assert.doesNotMatch(show, /retainContextWhenHidden/);
  assert.match(
    show,
    /const accepted = acceptTestTablesMessage\(message\);\s*if \(accepted === undefined\) \{\s*return;\s*\}\s*void testTablesCommand\(outputChannel, \{ change: accepted === "changeAnnotations" \}\);/,
  );
  assert.equal((source.match(/createWebviewPanel\(\s*TEST_TABLES_VIEW_TYPE/g) ?? []).length, 1);

  const press = functionBody(source, "async function testTablesCommand(");
  // The busy guard comes first, before the manifest, the pick or a run, from every entry point.
  const guard = press.search(
    /if \(testTablesPressBusy\) \{\s*return;\s*\}\s*testTablesPressBusy = true;\s*let rendered = false;\s*try \{/,
  );
  assert.notEqual(guard, -1, "the press returns at once while another runs");
  for (const later of [
    "resolveManifestOrNotify(",
    "askTestTablesAnnotations(",
    "withRunIndicator(",
  ]) {
    assert.ok(press.indexOf(later) > guard, `${later} after the guard`);
  }
  // A failed run renders the refusal page and nothing else.
  assert.match(
    press,
    /if \(!generated\) \{\s*showTestTablesTab\(testTablesRefusalPage\(nonce\), outputChannel\);\s*rendered = true;\s*return;\s*\}/,
  );
  // An unreadable bundle: its reason in the channel, the fixed page in the tab.
  assert.match(
    press,
    /if \(read\.kind === "unreadable"\) \{\s*outputChannel\.appendLine\(`SWCraft: the test tables cannot be read: \$\{read\.reason\}`\);\s*\}/,
  );
  // The flag is cleared on every way out, and a press that rendered nothing re-enables the tab.
  assert.match(
    press,
    /\} finally \{\s*testTablesPressBusy = false;\s*if \(!rendered && testTablesPanel !== undefined\) \{\s*void testTablesPanel\.webview\.postMessage\(TEST_TABLES_READY_MESSAGE\);\s*\}\s*\}/,
  );
  assert.equal((source.match(/testTablesPressBusy = true/g) ?? []).length, 1);
});

test("the imports' host wiring: one ImportTabs over the review tab's webview, each command one flow with its own run", () => {
  // The flows and the tab controller are importTabCore.ts' and run under scripted fakes in
  // importTabCore.test.ts; this pins how commands.ts hands them the webview, the dialog, the runs
  // and Check Model. What it does not reach: the webview and the dialogs themselves (no harness
  // drives them).
  const source = withoutCommentLines(readSource("src/commands.ts"));
  const tabs = functionBody(source, "const importTabs = new ImportTabs(");
  assert.match(
    tabs,
    /vscode\.window\.createWebviewPanel\(\s*IMPORT_TAB_VIEW_TYPE,\s*title,\s*\{ viewColumn: vscode\.ViewColumn\.Active, preserveFocus: false \},\s*\{ enableScripts: true, localResourceRoots: \[\] \},\s*\);\s*panel\.webview\.onDidReceiveMessage\(events\.message\);\s*panel\.onDidDispose\(events\.disposed\);/,
  );
  assert.match(tabs, /reveal: \(\) => panel\.reveal\(undefined, false\),/);
  assert.match(tabs, /nonce: newNonce,/);
  assert.equal((source.match(/createWebviewPanel\(\s*IMPORT_TAB_VIEW_TYPE/g) ?? []).length, 1);
  assert.equal(
    (source.match(/new ImportTabs\(/g) ?? []).length,
    1,
    "one controller for both imports",
  );
  // The report is Markdown rendered by VS Code's built-in Markdown extension,
  // through the core's renderImportReport(), which falls back to plain text.
  assert.match(
    functionBody(source, "function importReportRendering("),
    /return renderImportReport\(report, \(markdown\) =>\s*vscode\.commands\.executeCommand<unknown>\("markdown\.api\.render", markdown\),\s*\);/,
  );
  assert.equal((source.match(/"markdown\.api\.render"/g) ?? []).length, 1);
  // The flow host: the dialog, the outcome recorded on the command's item, Check
  // Model only through its command, a refusal as a notification and no record.
  const host = functionBody(source, "function importFlowHost(");
  assert.match(host, /tabs: importTabs,/);
  assert.match(
    host,
    /return picked === undefined \|\| picked\.length === 0 \? undefined : picked\[0\]\.fsPath;/,
  );
  assert.match(host, /recordCliOutcome\(commandId, label, run\);/);
  assert.match(host, /await vscode\.commands\.executeCommand\("swcraft\.checkModel"\);/);
  assert.match(
    host,
    /refuse: \(message\) => \{\s*void vscode\.window\.showErrorMessage\(message\);\s*\}/,
  );
  assert.equal(/recordCommandStatus\(/.test(host), false, "a refusal records no outcome");
  // One run: alone in its indicator, stdout kept out of the channel, its
  // Markdown report in a temporary file of its own.
  assert.match(
    functionBody(source, "function importRun("),
    /return withRunIndicator\(commandId, describeRunning\(commandId\), \(\) =>\s*withImportReportFile\(\s*workspaceRoot,\s*\(report\) =>\s*spawnCli\(executable, argv\(report\), workspaceRoot, outputChannel, \{ echoStdout: false \}\),\s*fs,\s*\{ log: \(line\) => outputChannel\.appendLine\(line\) \},\s*\),\s*\);/,
  );
  // Each command resolves once and hands its flow its argv; no argv carries
  // --replace-access-lists, and neither command runs a check itself.
  for (const [declaration, flow, argv, filter] of [
    [
      "async function importRequirementsCommand(",
      "importRequirementsFlow(",
      /\(file, options\) =>\s*importRun\(commandId, executable, workspaceRoot, outputChannel, \(report\) =>\s*buildImportRequirementsArgv\(manifestRelPath, file, \{ \.\.\.options, report \}\),\s*\),/,
      'filters: { JSON: ["json"] },',
    ],
    [
      "async function importComponentCommand(",
      "importComponentFlow(",
      /\(file, options\) =>\s*importRun\(commandId, executable, workspaceRoot, outputChannel, \(report\) =>\s*buildImportComponentArgv\(manifestRelPath, file, \{ \.\.\.options, report \}\),\s*\),/,
      'filters: { ARXML: ["arxml"] },',
    ],
  ] as const) {
    const body = functionBody(source, declaration);
    assert.equal((body.match(/resolveManifestOrNotify\(\)/g) ?? []).length, 1, declaration);
    assert.equal((body.match(/resolveSwcraftExecutable\(/g) ?? []).length, 1, declaration);
    assert.ok(body.includes(`await ${flow}`), `${declaration} runs ${flow}`);
    assert.match(body, argv);
    assert.ok(body.includes(filter), `${declaration} filters its dialog`);
    assert.equal(/replace-access-lists|executeCommand\(|showOpenDialog\(/.test(body), false);
  }
});

test("requirementsImportHost: the mapping setting is the workspace folder's alone, read and written there, and the one setting writer", () => {
  // The questions themselves run under scripted fakes in importTabCore.test.ts, and the test
  // tables' pick in testTablesTab.test.ts. No fake reaches VS Code's settings API, so the
  // adapter's calls into it are pinned here by shape. The writer is one helper,
  // rememberWorkspaceSetting(), with two callers.
  const source = withoutCommentLines(readSource("src/commands.ts"));
  const host = functionBody(source, "function requirementsImportHost(");
  assert.match(
    host,
    /rememberedMapping: \(\) =>\s*configuration\.inspect<unknown>\(REQUIREMENTS_MAPPING_SETTING\)\?\.workspaceFolderValue,/,
  );
  assert.match(
    host,
    /rememberMapping: \(value\) =>\s*rememberWorkspaceSetting\(workspaceRoot, REQUIREMENTS_MAPPING_SETTING, value, outputChannel\),/,
  );
  assert.equal(host.indexOf("configuration.get"), -1, "the effective value is never read");
  assert.match(host, /filters: \{ YAML: \["yaml", "yml"\] \}/);
  const tables = functionBody(source, "function testTablesAnnotationsHost(");
  assert.match(
    tables,
    /\.inspect<unknown>\(TEST_TABLES_ANNOTATIONS_SETTING\)\?\.workspaceFolderValue,/,
  );
  assert.match(
    tables,
    /rememberAnnotations: \(value\) =>\s*rememberWorkspaceSetting\(\s*workspaceRoot,\s*TEST_TABLES_ANNOTATIONS_SETTING,\s*value,\s*outputChannel,?\s*\),/,
  );
  assert.match(tables, /canSelectFiles: false,\s*canSelectFolders: true,/);
  for (const body of [host, tables]) {
    assert.equal(body.indexOf(".get<"), -1, "the effective value is never read");
    assert.equal(body.indexOf(".get("), -1, "the effective value is never read");
    for (const run of ["spawnCli(", "withRunIndicator(", "captureChild(", ".update("]) {
      assert.equal(body.indexOf(run), -1, `a pick's host calls ${run}`);
    }
  }
  // The extension's one setting writer: one configuration update, in one
  // helper, into the workspace folder; both hosts its callers.
  const writer = functionBody(source, "async function rememberWorkspaceSetting(");
  assert.match(
    writer,
    /\.getConfiguration\(undefined, vscode\.Uri\.file\(workspaceRoot\)\)\s*\.update\(setting, value, vscode\.ConfigurationTarget\.WorkspaceFolder\);/,
  );
  assert.equal((source.match(/ConfigurationTarget\./g) ?? []).length, 1);
  assert.equal((source.match(/\.update\(setting,/g) ?? []).length, 1);
  assert.equal(
    (source.match(/\.update\(\s*[A-Z_]+_SETTING/g) ?? []).length,
    0,
    "no setting is written outside rememberWorkspaceSetting()",
  );
  assert.equal(
    (source.match(/rememberWorkspaceSetting\(/g) ?? []).length,
    3,
    "the helper and its two calls",
  );
});

test("the import tab is not the review channel: its host and core write no record, no digest, no audit", () => {
  // The imports reuse the review tab's page and CSS, never its channel.
  const source = withoutCommentLines(readSource("src/commands.ts"));
  const host = [
    "function importReportRendering(",
    "const importTabs = new ImportTabs(",
    "function importFlowHost(",
    "function importRun(",
    "async function importRequirementsCommand(",
    "async function importComponentCommand(",
  ]
    .map((declaration) => functionBody(source, declaration))
    .join("\n");
  const core = readSource("src/importTabCore.ts");
  const channel =
    /writeDecision|decisionRecord|decisionFileName|pendingFileName|PENDING_SUFFIX|DECISION_SUFFIX|reviewsDir|currentReviewsDir|REVIEWS_DIRNAME|textDigest|checkApproval|answerPlan|readReviewEntry|rereadReviews|reviewTabs|audit|writeFile|\bfs\./;
  assert.doesNotMatch(host, channel);
  assert.doesNotMatch(withoutCommentLines(core), channel);
  // Of reviewCore the core takes the page helper and the escape, nothing else.
  const imported = withoutCommentLines(core).match(/import \{([^}]*)\} from "\.\/reviewCore";/);
  assert.ok(imported !== null, "importTabCore imports reviewCore once");
  assert.deepEqual(
    imported[1]
      .split(",")
      .map((name) => name.trim())
      .filter((name) => name !== ""),
    ["escapeHtml", "reviewTabPage"],
  );
  // No file system module (not reached: a file system function the host passes in).
  const modules = importedModules(core);
  assert.ok(modules.includes("./reviewCore"), "the import declarations are read");
  assert.deepEqual(
    modules.filter((name) => /^(node:)?fs(\/|$)/.test(name)),
    [],
    "importTabCore imports no file system module",
  );
});

test("the Trace Links view never splits the editor: every open is the active group's preview tab", () => {
  // The editor is never split.
  const source = withoutCommentLines(readSource("src/commands.ts"));
  for (const declaration of [
    "async function traceOpenElementCommand(",
    "async function traceOpenRequirementCommand(",
  ]) {
    const body = functionBody(source, declaration);
    assert.equal(
      /showTextDocument|ViewColumn/.test(body),
      false,
      `${declaration} opens its own way`,
    );
    assert.equal(body.split("showPreviewTab(").length - 1, 1, `${declaration} opens one file`);
  }
  const preview = functionBody(source, "async function showPreviewTab(");
  assert.match(preview, /preview: true,\s*preserveFocus: true,/);
  assert.match(preview, /viewColumn: vscode\.ViewColumn\.Active/);
  // The one exception: the diagrams' tab opens a model file beside the panel, so the diagram and
  // the file are seen together, in the column besideColumn() decides at each open from the
  // panel's current column.
  const tab = functionBody(source, "class DiagramsTab {");
  assert.equal(
    tab.split("ViewColumn.Beside").length - 1,
    1,
    "Beside is spelled once in the tab: openColumn()",
  );
  assert.match(
    functionBody(tab, "  private openColumn(): vscode.ViewColumn {"),
    /const column = besideColumn\(this\.state\.editorColumn, this\.panel\?\.viewColumn\);\s*return column === "beside" \? vscode\.ViewColumn\.Beside : column;/,
  );
  assert.equal(tab.split("this.openColumn()").length - 1, 2, "both opens ask openColumn()");
  const outsideTheTab = source.replace(functionBody(source, "class DiagramsTab {"), "");
  assert.equal(
    /ViewColumn\.Beside/.test(outsideTheTab),
    false,
    "no command of the extension splits, the diagrams' tab aside",
  );
});

test("the read-only report document is gone: no content provider, no swcraft-report scheme", () => {
  const extension = withoutCommentLines(readSource("src/extension.ts"));
  const commands = withoutCommentLines(readSource("src/commands.ts"));
  for (const source of [extension, commands]) {
    assert.equal(
      /registerTextDocumentContentProvider|reportDocument|swcraft-report/.test(source),
      false,
    );
  }
  assert.equal(/function showReportTab\(/.test(commands), false);
});

test("openGuideCommand is handed the guide channel and never the shared one", () => {
  const source = readSource("src/commands.ts");
  assert.match(
    source,
    /openGuideCommand\(guideSession, guideChannel\b/,
    "swcraft.openGuide must be registered against the guide channel",
  );
  const code = withoutCommentLines(
    sourceRegion(source, "async function openGuideCommand(", "function checkEditorToolingCommand("),
  );
  assert.equal(
    /\boutputChannel\b/.test(code),
    false,
    "openGuideCommand must not reach the shared `SWCraft` channel at all",
  );
  // Open Guide reaches the server through `openGuidePage()`, the one opening a
  // rule link and a *Learn more* share; it opens the section its argument names,
  // or the site's root.
  assert.match(
    functionBody(code, "async function openGuidePage("),
    /serveGuideTo\(session, \{ executable, cwd \}, guideChannel\)/,
  );
  const command = functionBody(code, "async function openGuideCommand(");
  assert.match(command, /guideCommandArg\(/, "the argument is checked by guideCommandArg()");
  assert.match(command, /openGuidePage\(session, guideChannel\b/);
});

test("openGuidePage shows the guide in the window's one guide tab and falls back to openExternal", () => {
  // The served URL is mapped through `asExternalUri` (the port forward a remote window needs),
  // handed to the browser inside VS Code, and only when that throws does it go to the user's own
  // browser. `openGuidePage()` holds this for Open Guide, a rule link and a *Learn more* alike, on
  // the served page's address at its anchor. The command and its arguments (one guide tab per
  // window) are `guideTabOpen()`'s decision (helpLinks.test.ts), taken here on the commands this
  // VS Code has and the MAPPED address, and run as decided.
  const body = functionBody(
    withoutCommentLines(readSource("src/commands.ts")),
    "async function openGuidePage(",
  );
  assert.match(body, /const address = guidePageUrl\(outcome\.url, page, anchor\);/);
  const mapped = body.indexOf("asExternalUri(");
  const shown = body.indexOf("guideTabOpen(");
  const caught = body.indexOf("catch", shown);
  const fallback = body.indexOf("openExternal(");
  assert.ok(mapped >= 0, "the URL is mapped through asExternalUri");
  const opened = body.lastIndexOf("try {", mapped);
  assert.ok(
    opened >= 0 && !/\}/.test(body.slice(opened, mapped)),
    "asExternalUri is inside the try, so a resolver that throws takes the fallback",
  );
  assert.ok(shown > mapped, "guideTabOpen comes after asExternalUri");
  assert.match(
    body,
    /const external = await vscode\.env\.asExternalUri\(vscode\.Uri\.parse\(address\)\);\s*const open = guideTabOpen\(\s*await vscode\.commands\.getCommands\(true\),\s*external\.toString\(true\),\s*page,\s*\);\s*await vscode\.commands\.executeCommand\(open\.command, \.\.\.open\.args\);/,
    "the decision is taken on the MAPPED address and run as decided",
  );
  assert.equal(
    /simpleBrowser|workbench\.action\.browser/.test(body),
    false,
    "no browser command is named here: guideTabOpen() is the one place that picks it",
  );
  assert.equal(
    (body.match(/executeCommand\(/g) ?? []).length,
    1,
    "the guide is shown by one command run",
  );
  assert.ok(caught > shown, "a catch follows the open");
  assert.ok(fallback > caught, "openExternal is reached only in the fallback, after the catch");
  assert.equal(body.indexOf("openExternal(", fallback + 1), -1, "openExternal is called once");
  assert.match(body.slice(shown, caught), /return;/, "a shown guide returns before the fallback");
  assert.match(body.slice(fallback), /showInformationMessage\(/, "the advisory message stays");
});

test("Open Guide, a rule link and Learn more all reach the one openGuidePage", () => {
  // One guide tab per window is decided once, in `openGuidePage()`; it holds for the three entries
  // only while each still goes through it.
  const source = withoutCommentLines(readSource("src/commands.ts"));
  assert.match(
    functionBody(source, "async function openGuideCommand("),
    /openGuidePage\(session, guideChannel\b/,
  );
  assert.match(
    functionBody(source, "async function handleHelpUri("),
    /openGuidePage\(guide\.session, guide\.channel, request\.page, request\.anchor\)/,
  );
  assert.match(
    functionBody(source, "function notifyWithHelp("),
    /void openGuidePage\(guide\.session, guide\.channel, page, anchor\);/,
  );
  assert.match(
    functionBody(source, "async function handleHelpUri("),
    /const guide = helpGuide;/,
    "a rule link and Learn more reach the guide the same way",
  );
  assert.equal(
    (source.match(/executeCommand\(open\.command/g) ?? []).length,
    1,
    "the guide tab is opened in one place",
  );
});

test("the panel's menu commands record no tree status: neither is a tree item", () => {
  // Open Guide and Check Editor Tooling are entries of the panel's `…` menu, and a recorded status
  // is drawn only on a tree item: one recorded here would arm the 30-second clearing timer for
  // nothing.
  const source = withoutCommentLines(readSource("src/commands.ts"));
  for (const declaration of [
    "async function openGuideCommand(",
    "async function openGuidePage(",
    "async function checkEditorToolingCommand(",
  ]) {
    const body = functionBody(source, declaration);
    assert.equal(body.includes("recordCommandStatus("), false, `${declaration} records a status`);
  }
  // Their answers stay: the guide's failure message, the health report's
  // notification and its run indicator.
  assert.match(
    functionBody(source, "async function openGuidePage("),
    /showErrorMessage\(outcome\.message\)/,
  );
  const health = functionBody(source, "async function checkEditorToolingCommand(");
  assert.match(health, /withRunIndicator\(\s*"swcraft\.checkEditorTooling"/);
  assert.match(health, /showWarningMessage\(/);
  assert.match(health, /showInformationMessage\(/);
});

test("a section tree item takes its state and its id from the descriptor, never a literal", () => {
  // VS Code applies a changed default only under a new id, so the pure side decides both
  // `collapsedByDefault` and the generation-bearing id, and the adapter only draws them. A literal
  // `Expanded` left on the section branch would make Setup's collapse a no-op.
  const source = withoutCommentLines(readSource("src/commands.ts"));
  const branch = sourceRegion(
    sourceRegion(source, "class SwcraftTreeDataProvider", "getChildren(element?"),
    'if (element.kind === "section") {',
    'if (element.kind === "trace") {',
  );
  assert.match(
    branch,
    /element\.descriptor\.collapsedByDefault\s*\?\s*vscode\.TreeItemCollapsibleState\.Collapsed\s*:\s*vscode\.TreeItemCollapsibleState\.Expanded/,
  );
  assert.equal(
    branch.match(/TreeItemCollapsibleState\.Expanded/g)?.length,
    1,
    "the section branch names `Expanded` only as collapsedByDefault's other arm",
  );
  assert.match(branch, /item\.id = element\.descriptor\.id;/);
  // The facts are read once per refresh, in getChildren() of the project node.
  const children = sourceRegion(source, "getChildren(element?", "private hasManifest(");
  assert.ok(
    children.indexOf("this.setupSection.advance(") < children.indexOf("buildTree("),
    "getChildren must advance the Setup state before it builds the tree",
  );
  // The facts are read lazily, so the record can skip them while a toolchain
  // install runs, and the running set is the one the tree's items read.
  assert.match(
    children,
    /this\.setupSection\.advance\(\s*\(\) => setupSectionCollapsed\(this\.setupFacts\(\)\),\s*runningCommands,\s*\)/,
  );
  assert.match(children, /buildTree\([^;]*, setup\)/);
  // The record is bound to the workspace's memento, so a reload keeps the id.
  assert.match(
    source,
    /new SwcraftTreeDataProvider\(new SetupSectionRecord\(context\.workspaceState\)\)/,
  );
  assert.match(source, /constructor\(private readonly setupSection: SetupSectionRecord\) \{\}/);
  // What the three facts are read from: a file at the resolved executable, the
  // project node's own manifest fact, and a file at the agent profile's path
  // under an open folder — `isFile()` both times, never `existsSync`, which a
  // directory at either path would satisfy.
  const facts = sourceRegion(source, "private setupFacts() {", "\nfunction extensionVersion(");
  assert.match(facts, /const root = getWorkspaceRoot\(\);/);
  assert.match(facts, /toolchainInstalled: isFile\(swcraftResolution\(\)\.executable\),/);
  assert.match(facts, /hasManifest: this\.hasManifest\(\),/);
  assert.match(
    facts,
    /hasAgentProfile: root !== undefined && isFile\(path\.join\(root, AGENT_PROFILE_RELATIVE_PATH\)\),/,
  );
  assert.doesNotMatch(facts, /existsSync|: true\b/);
});

// --- No transitive `vscode` import (commandCore.ts + its dependencies) ---

const VSCODE_IMPORT_PATTERN = /(?:from\s+["']vscode["']|require\(\s*["']vscode["']\s*\))/;

test("the pure modules (commandCore, project, mcpProvider, onboarding, toolchain, diagrams, navigationCore, symbolCore, linkCore, diagnosticsCore, importTabCore) never import 'vscode'", () => {
  // These modules hold the decisions that are tested under plain `node --test`: the MCP decision
  // (which folders get a server, the argv, the one env key), every decision of Go to Definition,
  // hover and Find All References, the outline and workspace symbol scan, the document links, and
  // the reading of `check-model --format json` with its placements and quick fixes.
  for (const file of [
    "commandCore.ts",
    "project.ts",
    "mcpProvider.ts",
    "onboarding.ts",
    "toolchain.ts",
    "diagrams.ts",
    "navigationCore.ts",
    "symbolCore.ts",
    "linkCore.ts",
    "diagnosticsCore.ts",
    // The imports' tab: its page and its message check.
    "importTabCore.ts",
  ]) {
    const content = readSource(path.posix.join("src", file));
    assert.equal(VSCODE_IMPORT_PATTERN.test(content), false, `${file} must not import "vscode"`);
  }
});

test("a successful Onboard Project re-arms the model watch of the open folder", () => {
  // A folder with no manifest watches nothing, and writing its swcraft.yaml in
  // place is no workspace-folder change: without the re-arm, the walkthrough's own
  // flow (onboard the open folder, then Build) leaves every later edit unmarked.
  // The body that spawns is `runOnboarding`: `onboardProjectCommand` asks for the
  // folder and hands it over inside the run indicator.
  const body = withoutCommentLines(
    functionBody(readSource("src/commands.ts"), "async function runOnboarding("),
  );
  const success = body.indexOf(
    'recordCommandStatus("swcraft.onboardProject", { kind: "success" })',
  );
  assert.notEqual(success, -1);
  assert.ok(body.indexOf("armModelWatch();") > success, "re-armed once it has succeeded");
  assert.ok(
    body.indexOf("void updateResourcesAvailable();") > success,
    "the Resources view's `when` follows the new manifest",
  );
  const arm = withoutCommentLines(
    functionBody(readSource("src/commands.ts"), "function armModelWatch("),
  );
  assert.match(arm, /modelWatchSession\.armFor\(folder\);/);
});

test("arming the model watch rescans the change record beside it, and the watch factory does not", () => {
  // Re-reading the newest change is the arming's, stated where it happens: the
  // factory ModelWatchSession calls only builds the watch.
  const source = withoutCommentLines(readSource("src/commands.ts"));
  const arm = functionBody(source, "function armModelWatch(");
  const armFor = arm.indexOf("modelWatchSession.armFor(folder);");
  assert.notEqual(armFor, -1);
  assert.ok(
    arm.indexOf("artifactTracker?.rescan(folder)") > armFor,
    "rescanned after the watch is armed",
  );
  assert.doesNotMatch(functionBody(source, "function watchModelFolder("), /rescan/);
});

// --- captureChild: the settle logic behind spawnCliCapture --

/** A real child that never exits on its own. */
function idleChild(): ChildProcess {
  return spawn(process.execPath, ["-e", "setInterval(() => {}, 1000)"]);
}

test("captureChild: a child that exits is captured once, its stdout and exit code", async () => {
  const child = spawn(process.execPath, ["-e", "process.stdout.write('out'); process.exit(3)"]);
  const controller = new AbortController();
  const result = await captureChild(child, "node", 30_000, controller.signal);
  assert.deepEqual(result, { exitCode: 3, spawnFailed: false, stdout: "out", stderr: "" });
  // Settling removed the abort listener: a later abort reaches nothing.
  assert.equal(getEventListeners(controller.signal, "abort").length, 0);
  controller.abort();
});

test("captureChild: an abort mid-run kills the child and settles exitCode null, nothing added", async () => {
  const child = idleChild();
  const controller = new AbortController();
  const pending = captureChild(child, "node", 30_000, controller.signal);
  assert.equal(getEventListeners(controller.signal, "abort").length, 1);
  controller.abort();
  const result = await pending;
  assert.deepEqual(result, { exitCode: null, spawnFailed: false, stdout: "", stderr: "" });
  assert.equal(child.killed, true, "the child was killed, not left running");
  assert.equal(getEventListeners(controller.signal, "abort").length, 0);
  if (child.exitCode === null && child.signalCode === null) {
    await once(child, "close");
  }
});

test("captureChild: a signal aborted before the call kills the child at once", async () => {
  const child = idleChild();
  const controller = new AbortController();
  controller.abort();
  const result = await captureChild(child, "node", 30_000, controller.signal);
  assert.equal(result.exitCode, null);
  assert.equal(result.spawnFailed, false);
  assert.equal(child.killed, true);
  if (child.exitCode === null && child.signalCode === null) {
    await once(child, "close");
  }
});

test("captureChild: the timeout kills the child and appends its did-not-answer line", async () => {
  const child = idleChild();
  const result = await captureChild(child, "/opt/swcraft", 50);
  assert.equal(result.exitCode, null);
  assert.equal(result.spawnFailed, false);
  assert.equal(
    result.stderr,
    "SWCraft: /opt/swcraft did not answer within 50 ms and was stopped.\n",
  );
  assert.equal(child.killed, true);
  if (child.exitCode === null && child.signalCode === null) {
    await once(child, "close");
  }
});

test("captureChild: an executable that cannot be started is spawnFailed", async () => {
  const missing = path.join(os.tmpdir(), "swcraft-no-such-executable-xyz");
  const result = await captureChild(spawn(missing, []), missing, 30_000);
  assert.equal(result.spawnFailed, true);
  assert.equal(result.exitCode, null);
  assert.match(result.stderr, /ENOENT/);
});

// --- The two commands a CodeLens runs with an argument ------

test("diagramsArgument: only a listed view id of the generator's shape is accepted", () => {
  const listing = ["index.html", "ibd-Chassis.html", "ibd-Chassis.svg", "sequence-Brake~1.html"];
  assert.equal(diagramsArgument({ view: "ibd-Chassis" }, listing), "ibd-Chassis");
  assert.equal(diagramsArgument({ view: "sequence-Brake~1" }, listing), "sequence-Brake~1");
  const refused: unknown[] = [
    undefined, // the palette
    { label: "Diagrams", command: { command: "swcraft.diagrams" } }, // the panel's TreeItem
    { view: "../x" },
    { view: "/abs/ibd-Chassis" },
    { view: "C:\\views\\ibd-Chassis" },
    { view: "ibd-Other" }, // the shape is right, the page is not listed
    { view: "ibd-chassis" }, // a case variant of a listed page
    { view: "ibd-Chassis.html" },
    { view: "ibd-Chassis~0" },
    { view: 3 },
    "ibd-Chassis",
    null,
  ];
  for (const arg of refused) {
    assert.equal(diagramsArgument(arg, listing), undefined, `${JSON.stringify(arg)} is refused`);
  }
  assert.equal(diagramsArgument({ view: "ibd-Chassis" }, undefined), undefined, "no listing");
});

test("diagramsArgument: the tab's Generate asks for its page by its shape alone", () => {
  // Generate on the missing-page refusal must regenerate that page, not show the index because the
  // page is no longer listed.
  const listing = ["index.html", "ibd-Vehicle.html"];
  assert.equal(
    diagramsArgument({ view: "ibd-Chassis" }, listing),
    undefined,
    "a lens: listed only",
  );
  assert.equal(
    diagramsArgument({ view: "ibd-Chassis" }, listing, { regenerate: true }),
    "ibd-Chassis",
  );
  assert.equal(
    diagramsArgument({ view: "ibd-Chassis" }, undefined, { regenerate: true }),
    "ibd-Chassis",
  );
  for (const arg of [undefined, { view: "../x" }, { view: "ibd-Chassis.html" }, { view: 3 }, "x"]) {
    assert.equal(
      diagramsArgument(arg, listing, { regenerate: true }),
      undefined,
      `${JSON.stringify(arg)} is refused`,
    );
  }
});

test("VIEW_ID_PATTERN transcribes the generator's VIEW_ID_RE", () => {
  // Duplicated across the language boundary (a stated limit); this keeps the two spellings one.
  const writer = readSource("../packages/tools/src/swcraft_tools/writer/diagrams/__init__.py");
  const python = /^VIEW_ID_RE = re\.compile\(r"([^"]+)"\)$/m.exec(writer)?.[1];
  assert.equal(VIEW_ID_PATTERN.source, `^${python}$`);
});

test("impactCommand and diagramsCommand take the lens's argument through its decoder", () => {
  const source = withoutCommentLines(readSource("src/commands.ts"));
  // The lens's name, the row's node, the editor menu's Uri and the palette's
  // nothing all go through impactCore's impactQuestion (tested there); the picker is gone.
  const impact = functionBody(source, "async function askImpact(");
  assert.match(impact, /const question = impactQuestion\(\s*arg,\s*\{/);
  assert.match(impact, /editor: activeImpactEditor\(\)/);
  // The lens's document beside its name, for the save-first check.
  assert.match(impact, /\},\s*lensUri,\s*\);/);
  for (const gone of ["promptForImpactName(", "impactArgument(", "runCliCommandIn("]) {
    assert.equal(source.includes(gone), false, `${gone} is gone with the picker and the text run`);
  }
  assert.match(
    source,
    /registerCommand\("swcraft\.impact", \(arg, lensUri\) =>\s*impactCommand\(outputChannel, arg, lensUri\),?\s*\)/,
  );
  assert.match(source, /command: "swcraft\.impact",[^}]*arguments: \[item\.name, document\.uri\]/);
  const diagrams = functionBody(source, "async function diagramsCommand(");
  // One open per path: the lens's direct open of a current page, or the open
  // after the generation; the argument picks the page and, with the in-window
  // record, whether the press generates first.
  assert.equal((diagrams.match(/diagramsTab\.show\(/g) ?? []).length, 2);
  assert.equal(/openExternal\(/.test(diagrams), false);
  assert.ok(diagrams.indexOf("diagramsArgument(") > diagrams.indexOf("if (toolchainRunInFlight)"));
  assert.ok(diagrams.indexOf("diagramsArgument(") < diagrams.indexOf("opensDirectly("));
  assert.ok(diagrams.indexOf("diagramsArgument(") < diagrams.indexOf("runGenerateDiagrams("));
  assert.match(
    diagrams,
    /!regenerate &&\s*requested !== undefined &&\s*onDisk !== undefined &&\s*!viewsRootWrites\.busy &&\s*artifactTracker\?\.opensDirectly\(/,
    "the direct open needs no Generate from the tab, the lens's page, no run in flight and the tracker's yes",
  );
  // A refused generation forgets the in-window record before it reports.
  assert.match(
    diagrams,
    /artifactTracker\?\.forgetWindowGeneration\("views", workspaceRoot\);\s*finishDiagramsPress\(\{ kind: "generation-refused", message \}, generated\);/,
  );
});

// --- Experiments: the kinds table, the argv, the report's path, the handler ---

const { EXPERIMENT_KINDS, buildExperimentArgv, experimentReportPath } = commandCore;

test("buildExperimentArgv: the kind and the project, and no --config without a file", () => {
  assert.deepEqual(buildExperimentArgv("allocation", "swcraft.yaml"), [
    "experiment",
    "allocation",
    "--project",
    "swcraft.yaml",
  ]);
  for (const blank of ["", "   ", "\t"]) {
    assert.deepEqual(
      buildExperimentArgv("allocation", "swcraft.yaml", blank),
      ["experiment", "allocation", "--project", "swcraft.yaml"],
      "an empty or whitespace-only setting is no configuration file",
    );
  }
});

test("buildExperimentArgv: a configuration file is one trimmed --config= token", () => {
  assert.deepEqual(buildExperimentArgv("allocation", "swcraft.yaml", " experiments/try.yaml "), [
    "experiment",
    "allocation",
    "--project",
    "swcraft.yaml",
    "--config=experiments/try.yaml",
  ]);
  // A value that starts with `-` stays a value: split across two tokens argparse
  // would read it as an option.
  const argv = buildExperimentArgv("allocation", "swcraft.yaml", "-odd.yaml");
  assert.equal(argv.at(-1), "--config=-odd.yaml");
  assert.equal(argv.includes("-odd.yaml"), false);
});

test("experimentReportPath: the report line of a real transcript", () => {
  const cwd = path.resolve("/work/project");
  const report = path.join(cwd, "dist", "experiments", "allocation", "report.md");
  const result = path.join(cwd, "dist", "experiments", "allocation", "result.json");
  const stdout =
    "Experiment 'allocation': no component is free to move; nothing to search.\n" +
    `Generated report: ${report}\n` +
    `Generated result: ${result}\n`;
  assert.equal(experimentReportPath(stdout, cwd), report);
});

test("experimentReportPath: CRLF line ends are trimmed", () => {
  const cwd = path.resolve("/work/project");
  const report = path.join(cwd, "dist", "experiments", "try", "report.md");
  const stdout = `summary\r\nGenerated report: ${report}\r\nGenerated result: x\r\n`;
  assert.equal(experimentReportPath(stdout, cwd), report);
});

test("experimentReportPath: with two runs' output, the last report line wins", () => {
  const cwd = path.resolve("/work/project");
  const first = path.join(cwd, "dist", "experiments", "allocation", "report.md");
  const second = path.join(cwd, "dist", "experiments", "try", "report.md");
  const stdout = `Generated report: ${first}\nGenerated report: ${second}\n`;
  assert.equal(experimentReportPath(stdout, cwd), second);
});

test("experimentReportPath: no report line is no path", () => {
  const cwd = path.resolve("/work/project");
  assert.equal(experimentReportPath("", cwd), undefined);
  assert.equal(experimentReportPath("Error: the configuration file is refused\n", cwd), undefined);
  assert.equal(experimentReportPath("Generated result: x\n", cwd), undefined);
});

test("experimentReportPath: change-report's resolved absolute line is kept as printed", () => {
  // `swcraft change-report` prints the resolved absolute path of change-report.md, because the
  // extension's spawn cwd is the workspace root and not necessarily the project directory: the cwd
  // must not move it.
  const cwd = path.resolve("/work/workspace");
  const report = path.resolve("/work/workspace/projects/brake/dist/change-report/change-report.md");
  assert.equal(experimentReportPath(`Generated report: ${report}\n`, cwd), report);
  assert.equal(
    experimentReportPath(`Generated report: ${report}\n`, path.resolve("/elsewhere")),
    report,
  );
});

test("experimentReportPath: a captured stderr holding other lines names the report", () => {
  const cwd = path.resolve("/workspace");
  const report = path.join(cwd, "dist", "resource-report", "resource-report.md");
  const stderr = [
    "Warning: this project pins another toolchain release",
    `Generated report: ${report}`,
    "Warning: something after the line",
    "",
  ].join("\n");
  assert.equal(experimentReportPath(stderr, cwd), report);
  // A run that wrote no file: the warnings alone name no report, so no tab opens.
  assert.equal(
    experimentReportPath("Warning: the resource report was not written: refused\n", cwd),
    undefined,
  );
});

test("experimentReportPath: a relative path is resolved against the working directory", () => {
  const cwd = path.resolve("/work/project");
  assert.equal(
    experimentReportPath("Generated report: reports/allocation/report.md\n", cwd),
    path.join(cwd, "reports", "allocation", "report.md"),
  );
});

test("experimentReportPath: a value holding a control character is no report line", () => {
  const cwd = path.resolve("/work/project");
  const report = path.join(cwd, "dist", "build-report.md");
  for (const character of ["\r", "\t", "\u001b", "\u0000", "\u007f", "\u0085"]) {
    assert.equal(
      experimentReportPath(`Generated report: ${report}${character}x\n`, cwd),
      undefined,
      `a ${JSON.stringify(character)} inside the value`,
    );
  }
  // A carriage return inside the line hides a second report line from a terminal.
  assert.equal(
    experimentReportPath(`Generated report: ${report}\rGenerated report: /elsewhere/x.md\n`, cwd),
    undefined,
  );
  // Non-ASCII text is no control character.
  const accented = path.join(cwd, "Bericht-\u00e9t\u00e9", "report.md");
  assert.equal(experimentReportPath(`Generated report: ${accented}\n`, cwd), accented);
});

const { reportInsideWorkspace } = commandCore;

/** A realpath over a fixed map: a listed path resolves to its target, any other to itself. */
function fakeRealpath(links: Record<string, string>): (candidate: string) => string {
  return (candidate) => {
    for (const [link, target] of Object.entries(links)) {
      if (candidate === link || candidate.startsWith(link + path.sep)) {
        return target + candidate.slice(link.length);
      }
    }
    return candidate;
  };
}

test("reportInsideWorkspace: a report inside the workspace folder is kept, resolved", () => {
  const root = path.resolve("/work/ws");
  const report = path.join(root, "projects", "brake", "dist", "build-report.md");
  assert.equal(reportInsideWorkspace(report, root, fakeRealpath({})), report);
});

test("reportInsideWorkspace: a path outside the workspace, the folder itself or a sibling sharing its prefix is refused", () => {
  const root = path.resolve("/work/ws");
  const realpath = fakeRealpath({});
  assert.equal(
    reportInsideWorkspace(path.resolve("/elsewhere/build-report.md"), root, realpath),
    undefined,
  );
  assert.equal(reportInsideWorkspace(path.join(root, "..", "x.md"), root, realpath), undefined);
  assert.equal(reportInsideWorkspace(root, root, realpath), undefined);
  assert.equal(
    reportInsideWorkspace(path.resolve("/work/ws2/build-report.md"), root, realpath),
    undefined,
  );
});

test("reportInsideWorkspace: a folder inside the workspace whose name starts with two dots is kept", () => {
  const root = path.resolve("/work/ws");
  const report = path.join(root, "..dist", "build-report.md");
  assert.equal(reportInsideWorkspace(report, root, fakeRealpath({})), report);
  const nested = path.join(root, "..", "ws", "..a", "b.md");
  assert.equal(
    reportInsideWorkspace(nested, root, fakeRealpath({})),
    path.join(root, "..a", "b.md"),
  );
});

test("reportInsideWorkspace: both sides are compared resolved, so a symlinked folder still holds its report", () => {
  // The CLI prints the resolved path; the workspace may be opened through a link (`/tmp` on macOS).
  const linked = path.resolve("/tmp/ws");
  const real = path.resolve("/private/tmp/ws");
  const printed = path.join(real, "dist", "build-report.md");
  assert.equal(reportInsideWorkspace(printed, linked, fakeRealpath({ [linked]: real })), printed);
  // A link inside the workspace pointing out of it is outside.
  const outward = path.join(real, "dist", "out");
  assert.equal(
    reportInsideWorkspace(
      path.join(outward, "build-report.md"),
      linked,
      fakeRealpath({ [linked]: real, [outward]: path.resolve("/etc") }),
    ),
    undefined,
  );
});

test("reportInsideWorkspace: a path that cannot be resolved is refused", () => {
  const root = path.resolve("/work/ws");
  const throwing = (): string => {
    throw new Error("ENOENT");
  };
  assert.equal(reportInsideWorkspace(path.join(root, "r.md"), root, throwing), undefined);
});

test("a dist_root holding a newline cannot make the preview open a file outside the workspace", () => {
  // What a CLI printing the path raw would have printed: the newline forges a second report line.
  const root = path.resolve("/work/ws");
  const stdout = `Generated report: ${path.join(root, "x")}\nGenerated report: /elsewhere/build-report.md\nValidation passed.\n`;
  const report = experimentReportPath(stdout, root);
  assert.equal(report, path.resolve("/elsewhere/build-report.md"));
  assert.equal(reportInsideWorkspace(report ?? "", root, fakeRealpath({})), undefined);
});

// --- Export Component: the exported file's path, and the open ---

const { EXPORT_OUTPUT_LINE_PREFIX, exportedArxmlPath, lastPrefixedLine } = commandCore;

test("EXPORT_OUTPUT_LINE_PREFIX is the OUTPUT line ComponentExportReport.render() prints", () => {
  assert.equal(EXPORT_OUTPUT_LINE_PREFIX, "OUTPUT ");
});

test("exportedArxmlPath: an absolute OUTPUT path of a real transcript is kept as printed", () => {
  const cwd = path.resolve("/work/workspace");
  const output = path.resolve("/work/workspace/project/dist/components/TargetSwc.arxml");
  const stdout =
    "Component export report: TargetSwc\n" +
    "STATUS ok\n" +
    `OUTPUT ${output}\n` +
    "EXPORTED 2 interface(s)\n" +
    "  - ISpeed\n";
  assert.equal(exportedArxmlPath(stdout, cwd), output);
  assert.equal(exportedArxmlPath(stdout, path.resolve("/elsewhere")), output);
});

test("exportedArxmlPath: a relative OUTPUT path is resolved against the working directory", () => {
  const cwd = path.resolve("/work/project");
  assert.equal(
    exportedArxmlPath("STATUS ok\nOUTPUT dist/components/TargetSwc.arxml\n", cwd),
    path.join(cwd, "dist", "components", "TargetSwc.arxml"),
  );
});

test("exportedArxmlPath: CRLF line ends are trimmed", () => {
  const cwd = path.resolve("/work/project");
  const output = path.join(cwd, "dist", "components", "TargetSwc.arxml");
  assert.equal(
    exportedArxmlPath(`STATUS ok\r\nOUTPUT ${output}\r\nEXPORTED 0 interface(s)\r\n`, cwd),
    output,
  );
});

test("exportedArxmlPath: no OUTPUT line, or an empty value, is no path", () => {
  const cwd = path.resolve("/work/project");
  assert.equal(exportedArxmlPath("", cwd), undefined);
  // A failed export renders no OUTPUT line (TestExportReportRenderer).
  assert.equal(exportedArxmlPath("STATUS failed\nERROR IUndeclared\n", cwd), undefined);
  assert.equal(exportedArxmlPath("OUTPUT \n", cwd), undefined);
  assert.equal(exportedArxmlPath("OUTPUT    \r\n", cwd), undefined);
  // The prefix carries its space: a bare word is not the line.
  assert.equal(exportedArxmlPath("OUTPUTS x.arxml\n", cwd), undefined);
});

test("exportedArxmlPath: with two OUTPUT lines the last wins, and an empty one does not erase it", () => {
  const cwd = path.resolve("/work/project");
  const first = path.join(cwd, "dist", "components", "A.arxml");
  const second = path.join(cwd, "dist", "components", "B.arxml");
  assert.equal(exportedArxmlPath(`OUTPUT ${first}\nOUTPUT ${second}\n`, cwd), second);
  assert.equal(exportedArxmlPath(`OUTPUT ${second}\nOUTPUT \n`, cwd), second);
});

test("exportedArxmlPath: a path with spaces and non-ASCII characters survives whole", () => {
  const cwd = path.resolve("/work/my project");
  const output = path.join(cwd, "dist", "components", "Brémse Steuerung~1.arxml");
  assert.equal(exportedArxmlPath(`STATUS ok\nOUTPUT ${output}\n`, cwd), output);
  assert.equal(
    exportedArxmlPath("OUTPUT dist/components/Brémse Steuerung.arxml\n", cwd),
    path.join(cwd, "dist", "components", "Brémse Steuerung.arxml"),
  );
});

test("lastPrefixedLine: the shared loop returns the raw value, unresolved", () => {
  assert.equal(lastPrefixedLine("x: a\r\nx:  b \nx: \n", "x: "), "b");
  assert.equal(lastPrefixedLine("y: a\n", "x: "), undefined);
});

test("exportComponentCommand opens the exported file, and falls back to a notification naming it", () => {
  const source = withoutCommentLines(readSource("src/commands.ts"));
  const handler = functionBody(source, "async function exportComponentCommand(");
  const recorded = handler.indexOf(
    'if (!recordCliOutcome("swcraft.exportComponent", "Export Component", run)) {',
  );
  const read = handler.indexOf("exportedArxmlPath(run.stdout, workspaceRoot)");
  const opened = handler.indexOf("vscode.workspace.openTextDocument(");
  const shown = handler.indexOf("vscode.window.showTextDocument(document, { preview: false })");
  const caught = handler.indexOf("} catch (error) {");
  assert.notEqual(recorded, -1);
  assert.notEqual(read, -1, "the path is read off the run's stdout");
  assert.notEqual(opened, -1);
  assert.notEqual(shown, -1, "a non-preview tab");
  assert.notEqual(caught, -1);
  assert.ok(recorded < read && read < opened && opened < shown && shown < caught);
  // One fallback, named once and reached from both failures: the file no longer
  // on disk (checked before the open, and returning) and an open that throws.
  const fallback = handler.indexOf("const notOpened = (reason: string): void => {");
  assert.ok(read < fallback && fallback < opened, "the fallback is declared before the open");
  assert.match(
    handler.slice(fallback, opened),
    /vscode\.window\.showInformationMessage\(\s*`SWCraft: exported component '\$\{name\}' to \$\{exported\}\.`,?\s*\)/,
  );
  assert.match(
    handler,
    /if \(!isFile\(exported\)\) \{\s*notOpened\("the file is not on disk any more"\);\s*return;\s*\}\s*try \{\s*const document = await vscode\.workspace\.openTextDocument\(/,
    "the on-disk check precedes the open and returns through the fallback",
  );
  assert.match(
    handler.slice(caught),
    /^\} catch \(error\) \{\s*notOpened\(error instanceof Error \? error\.message : String\(error\)\);\s*\}/,
    "an open that throws reaches the same fallback",
  );
  const noPath = handler.slice(read, opened);
  assert.match(noPath, /if \(exported === undefined\) \{/);
  assert.match(noPath, /showInformationMessage\(/);
  assert.match(noPath, /the run printed no output path \(see the SWCraft output channel\)/);
  assert.doesNotMatch(noPath, /for the output path/);
  // The open replaced the success notification: the only notifications are the two fallbacks.
  assert.equal(handler.match(/showInformationMessage\(/g)?.length, 2);
});

/** The package.json the experiment tests read, typed as far as they read it. */
function experimentManifest(): {
  contributes: {
    commands: Array<{ command: string; title: string }>;
    configuration: { properties: Record<string, { scope?: string }> };
  };
} {
  return JSON.parse(readSource("package.json"));
}

test("every experiment kind's command and setting are declared, labelled and in the panel", () => {
  const manifest = experimentManifest();
  const declared = new Set(manifest.contributes.commands.map((entry) => entry.command));
  for (const [kind, entry] of Object.entries(EXPERIMENT_KINDS)) {
    assert.match(entry.commandId, /^swcraft\.[A-Za-z0-9]+$/, `${kind}: a dotless command id`);
    assert.ok(declared.has(entry.commandId), `${kind}: package.json declares ${entry.commandId}`);
    assert.equal(
      manifest.contributes.configuration.properties[entry.setting]?.scope,
      "resource",
      `${kind}: package.json declares ${entry.setting} with resource scope`,
    );
    assert.equal(SPAWNING_COMMAND_LABELS[entry.commandId], entry.label, `${kind}: run label`);
    const item = TREE_COMMAND_DEFINITIONS.find((def) => def.commandId === entry.commandId);
    assert.ok(item, `${kind}: a panel item invokes ${entry.commandId}`);
    assert.equal(item.section, "experiments", `${kind}: filed under Experiments`);
    assert.equal(item.label, entry.label, `${kind}: the panel's label is the table's`);
  }
});

test("the experiment kinds are exactly the KIND choices cli.py accepts", () => {
  // A text read of the parser, as the other transcription tests read the CLI:
  // a kind the CLI gains without a panel item, or the reverse, fails here.
  const cli = readCliPackageSource();
  const match = /add_argument\(\s*"kind",\s*choices=\[([^\]]*)\]/.exec(cli);
  assert.ok(match, "the cli package's experiment parser declares no 'kind' argument with choices");
  const choices = [...match[1].matchAll(/"([^"]+)"/g)].map((found) => found[1]);
  assert.deepEqual(choices.sort(), Object.keys(EXPERIMENT_KINDS).sort());
});

test("experimentCommand resolves the project, shows the run, then opens the preview", () => {
  const body = withoutCommentLines(
    functionBody(readSource("src/commands.ts"), "async function experimentCommand("),
  );
  const resolve = body.indexOf("resolveManifestOrNotify()");
  const indicator = body.indexOf("withRunIndicator(");
  const recorded = body.indexOf("recordCliOutcome(commandId, label, run)");
  const preview = body.indexOf("previewGeneratedReport(run.stdout, workspaceRoot, outputChannel)");
  assert.ok(resolve !== -1 && indicator !== -1 && recorded !== -1 && preview !== -1, body);
  assert.ok(resolve < indicator, "the project is resolved before the run starts");
  assert.ok(indicator < recorded, "the outcome is recorded once the run has ended");
  assert.ok(recorded < preview, "the preview opens after the outcome is recorded");
  assert.match(body, /spawnCli\(executable, argv, workspaceRoot, outputChannel\)/);
});

test("changeReportCommand: one click, no question — spawn change-report, record, then preview", () => {
  // Change Report asks nothing and passes no revisions; it shares the experiment's success tail.
  const body = withoutCommentLines(
    functionBody(readSource("src/commands.ts"), "async function changeReportCommand("),
  );
  const resolve = body.indexOf("resolveManifestOrNotify()");
  const argv = body.indexOf('buildCliArgv("change-report", manifestRelPath)');
  const indicator = body.indexOf("withRunIndicator(commandId, describeRunning(commandId)");
  const recorded = body.indexOf('recordCliOutcome(commandId, "Change Report", run)');
  const preview = body.indexOf("previewGeneratedReport(run.stdout, workspaceRoot, outputChannel)");
  assert.ok(
    [resolve, argv, indicator, recorded, preview].every((at) => at !== -1),
    `changeReportCommand's steps: ${body}`,
  );
  assert.ok(resolve < argv && argv < indicator, "the project is resolved before the run starts");
  assert.ok(indicator < recorded, "the outcome is recorded once the run has ended");
  assert.ok(recorded < preview, "the preview opens after the outcome is recorded");
  assert.match(body, /const commandId = COMMAND_IDS\["change-report"\];/);
  assert.match(body, /spawnCli\(executable, argv, workspaceRoot, outputChannel\)/);
  assert.match(
    body,
    /if \(!recordCliOutcome\(commandId, "Change Report", run\)\) \{\s*return;\s*\}\s*await previewGeneratedReport\(/,
  );
  // No question before the run.
  for (const prompt of ["showQuickPick(", "showInputBox(", "showOpenDialog("]) {
    assert.equal(body.includes(prompt), false, `changeReportCommand calls ${prompt}`);
  }
  // Registered under the id the table names, and labelled for the indicator.
  assert.match(
    readSource("src/commands.ts"),
    /registerCommand\("swcraft\.changeReport", \(\) =>\s*changeReportCommand\(outputChannel\),?\s*\)/,
  );
  assert.equal(SPAWNING_COMMAND_LABELS["swcraft.changeReport"], "Change Report");
  assert.equal(SPAWNING_COMMAND_LABELS["swcraft.diff"], undefined);
});

test("metricsCommand: one click — read the setting, refuse a bad one, spawn, record, then preview", () => {
  const body = withoutCommentLines(
    functionBody(readSource("src/commands.ts"), "async function metricsCommand("),
  );
  const resolve = body.indexOf("resolveManifestOrNotify()");
  const setting = body.indexOf("getConfiguration(undefined, vscode.Uri.file(workspaceRoot))");
  const refusal = body.indexOf("metricsSinceRefusal(since)");
  const argv = body.indexOf('buildCliArgv("metrics", manifestRelPath)');
  const indicator = body.indexOf("withRunIndicator(commandId, describeRunning(commandId)");
  const recorded = body.indexOf('recordCliOutcome(commandId, "Architecture Metrics", run)');
  const preview = body.indexOf("previewGeneratedReport(run.stdout, workspaceRoot, outputChannel)");
  assert.ok(
    [resolve, setting, refusal, argv, indicator, recorded, preview].every((at) => at !== -1),
    `metricsCommand's steps: ${body}`,
  );
  assert.ok(resolve < setting && setting < refusal, "the setting is read for the project folder");
  assert.ok(refusal < argv && argv < indicator, "a bad setting is refused before the run");
  assert.ok(indicator < recorded && recorded < preview, "recorded, then previewed");
  assert.match(body, /\.get<unknown>\(METRICS_SINCE_SETTING\)/);
  assert.match(body, /const commandId = COMMAND_IDS\.metrics;/);
  assert.match(
    body,
    /if \(!recordCliOutcome\(commandId, "Architecture Metrics", run\)\) \{\s*return;\s*\}\s*await previewGeneratedReport\(/,
  );
  for (const prompt of ["showQuickPick(", "showInputBox(", "showOpenDialog("]) {
    assert.equal(body.includes(prompt), false, `metricsCommand calls ${prompt}`);
  }
  assert.match(
    readSource("src/commands.ts"),
    /registerCommand\("swcraft\.metrics", \(\) => metricsCommand\(outputChannel\)\)/,
  );
  assert.equal(SPAWNING_COMMAND_LABELS["swcraft.metrics"], "Architecture Metrics");
});

test("previewGeneratedReport reads the report line, checks the file, refreshes the preview and catches a refused one", () => {
  const body = withoutCommentLines(
    functionBody(readSource("src/commands.ts"), "async function previewGeneratedReport("),
  );
  assert.match(body, /const report = experimentReportPath\(output, workspaceRoot\);/);
  for (const line of [
    "the run printed no report path, so there is nothing to preview",
    "is not on disk any more, so there is nothing to preview",
  ]) {
    assert.ok(body.includes(line), `the helper says '${line}'`);
  }
  const isFileCall = body.indexOf("isFile(report)");
  const preview = body.indexOf("openMarkdownPreview(");
  assert.ok(
    isFileCall !== -1 && isFileCall < preview,
    "the report is checked on disk before the preview",
  );
  const inside = body.indexOf(
    "const inside = reportInsideWorkspace(report, workspaceRoot, fs.realpathSync.native);",
  );
  assert.ok(
    inside > isFileCall && inside < preview,
    "the report is checked inside the workspace folder after it is found on disk and before the preview",
  );
  assert.ok(body.includes("is outside the workspace folder, so nothing is previewed"));
  assert.match(
    body,
    /await openMarkdownPreview\(vscode\.Uri\.file\(inside\), report, outputChannel\);/,
  );
});

test("openMarkdownPreview: the one preview call, refreshed, a refused one a channel line", () => {
  // The reports the CLI wrote (Build, Change Report, the experiments, metrics,
  // resources) keep it unchanged.
  const source = withoutCommentLines(readSource("src/commands.ts"));
  const body = functionBody(source, "async function openMarkdownPreview(");
  const preview = body.indexOf('"markdown.showPreview", uri');
  assert.notEqual(preview, -1);
  const tryAt = body.lastIndexOf("try {", preview);
  const catchAt = body.indexOf("} catch (error) {", preview);
  assert.ok(
    tryAt !== -1 && tryAt < preview && catchAt > preview,
    "the preview call is inside a try",
  );
  const refresh = body.indexOf('"markdown.preview.refresh"');
  assert.ok(
    refresh > preview && refresh < catchAt,
    "the preview is refreshed after it opens, inside the same try (a rewritten report kept its first rendering)",
  );
  assert.ok(
    body.slice(catchAt).includes("could not be opened in the Markdown preview"),
    "a refused preview is one channel line",
  );
  assert.equal(
    (source.match(/"markdown\.showPreview"/g) ?? []).length,
    2,
    "this one and the trace report's",
  );
  // Its definition and the generated reports' helper: the imports' reports are the import tab's,
  // never a preview.
  assert.equal((source.match(/openMarkdownPreview\(/g) ?? []).length, 2);
});

test("experimentCommand returns on a failed run and refuses a bad setting before the run", () => {
  const body = withoutCommentLines(
    functionBody(readSource("src/commands.ts"), "async function experimentCommand("),
  );
  // A failed run warns and returns before the report path is read at all.
  assert.match(
    body,
    /if \(!recordCliOutcome\(commandId, label, run\)\) \{\s*return;\s*\}\s*await previewGeneratedReport\(/,
  );
  // The setting is read for the project folder, not the window's first folder.
  assert.match(
    body,
    /getConfiguration\(undefined, vscode\.Uri\.file\(workspaceRoot\)\)\s*\.get<unknown>\(setting\)/,
  );
  const refused = body.indexOf("experimentSettingRefusal(setting, config)");
  assert.ok(
    refused !== -1 && refused < body.indexOf("withRunIndicator("),
    "a setting that is not a path is refused before the run",
  );
});

test("recordCliOutcome records the exit code's outcome, warns on a failure and answers success", () => {
  const source = withoutCommentLines(readSource("src/commands.ts"));
  const body = functionBody(source, "function recordCliOutcome(");
  assert.match(body, /const outcome = mapExitCodeToOutcome\(run\.exitCode\);/);
  assert.match(body, /recordCommandStatus\(\s*commandId,/);
  assert.match(
    body,
    /notifyWithHelp\("warning", message, helpTopicForRun\(run\)\);\s*return false;\s*\}\s*return true;/,
  );
  // The CLI-spawning handlers that share it reach it after their run: `checkModelCommand` (its run
  // fills the Problems panel), the imports' flow host (`importFlowHost()`'s `record`, its applied
  // run always and its dry run on a failure) and `changeReportCommand` among them. The views' runs
  // record a refused document themselves, and their guards are their own.
  for (const [declaration, call] of [
    [
      "async function checkModelCommand(",
      'recordCliOutcome(commandId, "Check Model", run, PROBLEMS_PANEL_DETAILS);',
    ],
    ["async function experimentCommand(", "if (!recordCliOutcome(commandId, label, run)) {"],
    [
      "async function changeReportCommand(",
      'if (!recordCliOutcome(commandId, "Change Report", run)) {',
    ],
    [
      "async function exportComponentCommand(",
      'if (!recordCliOutcome("swcraft.exportComponent", "Export Component", run)) {',
    ],
  ] as const) {
    const handler = functionBody(source, declaration);
    const at = handler.indexOf(call);
    const run = handler.indexOf("withRunIndicator(");
    assert.notEqual(at, -1, `${declaration} calls ${call}`);
    assert.notEqual(run, -1, `${declaration} shows the indicator`);
    assert.ok(at > run, `${declaration} records after its run`);
    assert.equal(/recordCommandStatus\(/.test(handler), false, declaration);
  }
  // The imports record through their flow host's `record`, after the run it
  // passes: importTabCore.test.ts drives the order with fakes.
});

test("every recordCliOutcome call passes its run, from which the exit code is read", () => {
  // The failure notification is told from what the CLI said (its stderr), so no
  // caller may hand on the exit code alone: the run is the one required
  // argument, and recordCliOutcome reads the exit code from it. What this does
  // not reach: which run a caller passes — the argument is read as one name.
  // A call ends at `)`, `;` or `,`.
  const source = withoutCommentLines(readSource("src/commands.ts"));
  const calls = [...source.matchAll(/recordCliOutcome\(([^;{]*?)\)(?=\)| *;|,)/g)].map(
    (match) => match[1],
  );
  assert.equal(calls.length, 15, `fifteen callers, read: ${calls.join(" | ")}`);
  for (const args of calls) {
    assert.match(
      args,
      /, \w+(?:, PROBLEMS_PANEL_DETAILS)?$/,
      `recordCliOutcome(${args}) passes one run`,
    );
  }
  const body = functionBody(source, "function recordCliOutcome(");
  assert.match(body, /label: string,\s*run: RunOutput,\s*detailsAt\?: DetailsAt,/);
  assert.doesNotMatch(body, /exitCode: number \| null/);
});

test("buildCommand hands the failing step's run on, beside buildRunMessage", () => {
  const source = withoutCommentLines(readSource("src/commands.ts"));
  const body = functionBody(source, "async function buildCommand(");
  // Every step's runner answers the whole run, so its stderr reaches runBuildSteps.
  assert.match(
    body,
    /const run = await spawnCli\(executable, \[\.\.\.step\.argv\], workspaceRoot, outputChannel\);[\s\S]*?return run;/,
  );
  assert.doesNotMatch(body, /return exitCode;/);
  assert.match(body, /const message = buildRunMessage\(result\);/);
  assert.match(
    body,
    /if \(message !== undefined && result\.kind === "failure"\) \{[\s\S]*notifyWithHelp\("warning", message, helpTopicForRun\(result\.run\)\);/,
  );
  assert.doesNotMatch(body, /showWarningMessage\(message\)/);
  // The step's own spawnFailed travels with it: a timed-out step (exit code
  // null, started) is not read as one that could not be launched.
  assert.doesNotMatch(body, /exitCode === null/);
});

test("a refused Diagrams generation carries its run's output to the notification", () => {
  const source = withoutCommentLines(readSource("src/commands.ts"));
  const run = functionBody(source, "async function runGenerateDiagrams(");
  assert.match(
    run,
    /\): Promise<SpawnCliResult> \{\s*return viewsRootWrites\.run\(\(\) =>\s*spawnCli\(/,
  );
  const diagrams = functionBody(source, "async function diagramsCommand(");
  assert.match(
    diagrams,
    /const generated = await withRunIndicator\("swcraft\.diagrams", DIAGRAMS_PROGRESS_TEXT, \(\) =>\s*runGenerateDiagrams\(/,
  );
  assert.match(diagrams, /mapExitCodeToOutcome\(generated\.exitCode\)/);
  assert.equal(
    (diagrams.match(/, generated\);/g) ?? []).length,
    1,
    "only the refused generation passes the run",
  );
  const finish = functionBody(source, "function finishDiagramsPress(");
  assert.match(
    finish,
    /function finishDiagramsPress\(press: DiagramsPress, generation\?: RunOutput\)/,
  );
  assert.match(
    finish,
    /if \(notification\.severity === "information"\) \{\s*void vscode\.window\.showInformationMessage\(notification\.message\);\s*return;\s*\}\s*notifyWithHelp\(\s*notification\.severity,\s*notification\.message,\s*generation === undefined \? undefined : helpTopicForRun\(generation\),?\s*\);/,
  );
  assert.equal(
    /show(?:Error|Warning)Message\(/.test(finish),
    false,
    "every warning and error goes through notifyWithHelp",
  );
});

test("notifyWithHelp adds Learn more only for a help topic, and shows the notification as it was otherwise", () => {
  const source = withoutCommentLines(readSource("src/commands.ts"));
  assert.equal(/function showRunFailure\(/.test(source), false, "one notification layer, not two");
  const body = functionBody(source, "function notifyWithHelp(");
  // The topic is the caller's: `project` for the two refusals, the pure
  // classification `helpTopicForRun()` for a failed run; no topic is the
  // notification exactly as it was.
  assert.match(body, /topic: HelpTopic \| undefined,/);
  assert.match(
    body,
    /if \(topic === undefined \|\| guide === undefined\) \{\s*if \(severity === "error"\) \{\s*void vscode\.window\.showErrorMessage\(message\);\s*\} else \{\s*void vscode\.window\.showWarningMessage\(message\);\s*\}\s*return;\s*\}/,
  );
  assert.match(body, /showErrorMessage\(message, LEARN_MORE\)/);
  assert.match(body, /showWarningMessage\(message, LEARN_MORE\)/);
  // A press opens the topic's section in the served guide, the way Open Guide opens.
  assert.match(
    body,
    /if \(choice === LEARN_MORE\) \{\s*const \{ page, anchor \} = HELP_TOPICS\[topic\];\s*void openGuidePage\(guide\.session, guide\.channel, page, anchor\);/,
  );
});

test("Learn more is offered at the help sites alone: no folder, no project, a failed run", () => {
  // The button's text is one constant, read by one function, and that function is called by
  // exactly the notifications that may carry it: the two refusals without a project, and the three
  // places a failed run's notification is shown, each with helpTopicForRun's answer for its run.
  const source = withoutCommentLines(readSource("src/commands.ts"));
  assert.equal(source.split('"Learn more"').length - 1, 1, "the button's text is written once");
  assert.match(source, /const LEARN_MORE = "Learn more";/);
  const declarations = [...source.matchAll(/^(?:export )?(?:async )?function (\w+)\(/gm)].map(
    (match) => ({ name: match[1] as string, body: functionBody(source, match[0]) }),
  );
  // A slice runs to the next declaration, so the constant's own line, written
  // above its reader, ends the slice before it and is not a read.
  const readers = declarations.filter(({ body }) =>
    /\bLEARN_MORE\b/.test(body.replace(/^const LEARN_MORE = .*$/m, "")),
  );
  assert.deepEqual(
    readers.map(({ name }) => name),
    ["notifyWithHelp"],
  );
  const callers = declarations
    .filter(({ name, body }) => name !== "notifyWithHelp" && /\bnotifyWithHelp\(/.test(body))
    .map(({ name }) => name)
    .sort();
  assert.deepEqual(callers, [
    "buildCommand",
    "finishDiagramsPress",
    "recordCliOutcome",
    "resolveManifestOrNotify",
    "showNoWorkspaceError",
  ]);
  assert.match(
    functionBody(source, "function showNoWorkspaceError("),
    /notifyWithHelp\(\s*"error",[\s\S]*"project",?\s*\);/,
  );
  assert.match(
    functionBody(source, "function resolveManifestOrNotify("),
    /if \(!resolution\.ok\) \{\s*notifyWithHelp\("error", resolution\.message, "project"\);/,
  );
  // A failed run's topic is always the classification's, never a literal.
  for (const name of ["buildCommand", "finishDiagramsPress", "recordCliOutcome"]) {
    const calls = [
      ...functionBody(source, `function ${name}(`).matchAll(/notifyWithHelp\(([^;]*)\);/g),
    ];
    assert.ok(calls.length > 0, name);
    for (const [, args] of calls) {
      assert.match(
        args,
        /,\s*(?:\w+ === undefined \? undefined : )?helpTopicForRun\([\w.]+\),?\s*$/,
        `${name}: notifyWithHelp(${args})`,
      );
    }
  }
});

test("publishPlacements links a finding's rule with its whole link as asExternalUri returned it", () => {
  const source = withoutCommentLines(readSource("src/commands.ts"));
  const publish = functionBody(source, "function publishPlacements(");
  assert.match(
    publish,
    /if \(place\.code !== undefined\) \{\s*const link = ruleLinks\.get\(place\.code\);\s*diagnostic\.code = link === undefined \? place\.code : \{ value: place\.code, target: link \};/,
  );
  // Each rule's WHOLE link — page and anchor in it — goes through asExternalUri,
  // on the editor's own product scheme, never a literal, and the result is kept
  // unaltered: nothing is added to a mapped URI (a web workbench's mapping
  // carries the link inside its own query, and a parameter added after it
  // would be lost).
  // The resolver's map, set in registerCommands, carries the link to the API.
  const register = functionBody(source, "export function registerCommands(");
  assert.match(
    register,
    /const extensionId = context\.extension\.id;\s*ruleLinkResolver = new RuleLinkResolver<vscode\.Uri>\(vscode\.env\.uriScheme, \(rule\) => \{\s*const link = helpUri\(vscode\.env\.uriScheme, extensionId, RULES_PAGE, rule\);\s*return link === undefined \? undefined : vscode\.env\.asExternalUri\(vscode\.Uri\.from\(link\)\);\s*\}\);/,
  );
  // The run's wait is bounded, so a slow mapping never holds the Problems
  // entries; a call past its own limit logs to the run's channel.
  const resolve = functionBody(source, "async function resolveRuleLinks(");
  assert.match(
    resolve,
    /return ruleLinkResolver\.resolve\(rules, RULE_LINK_WAIT_MS, \(line\) => channel\.appendLine\(line\)\);/,
  );
  assert.match(source, /const RULE_LINK_WAIT_MS = 1_000;/);
  assert.equal(/"vscode:\/\/|'vscode:\/\//.test(source), false, "no hard-coded vscode:// scheme");
  // The rules are resolved before the run publishes, and only the latest run publishes.
  const run = functionBody(source, "async function checkModelRun(");
  const resolved = run.indexOf("const ruleLinks = await resolveRuleLinks(");
  const published = run.indexOf("if (generation === checkModelRunsStarted)");
  assert.ok(
    resolved >= 0 && resolved < published,
    "the links are resolved before the latest-run check",
  );
  assert.match(run, /publishPlacements\(collection, placements, ruleLinks\);/);
});

test("the URI handler validates a link before it serves the guide", () => {
  const source = withoutCommentLines(readSource("src/commands.ts"));
  const handler = functionBody(source, "async function handleHelpUri(");
  const parsed = handler.indexOf("parseHelpUri(uri, extensionId)");
  const refused = handler.search(
    /if \(request\.kind === "refused"\) \{\s*guide\.channel\.appendLine\([^;]*\);\s*return;\s*\}/,
  );
  const served = handler.indexOf(
    "openGuidePage(guide.session, guide.channel, request.page, request.anchor)",
  );
  assert.ok(parsed >= 0 && refused > parsed && served > refused, "parse, refuse, then serve");
  assert.equal(/serveGuideTo\(|simpleBrowser|openExternal\(/.test(handler), false);
  const register = functionBody(source, "export function registerCommands(");
  assert.match(
    register,
    /vscode\.window\.registerUriHandler\(\{\s*handleUri: \(uri\) =>\s*uriRoute\(uri\) === "model"\s*\? handleModelUri\(uri, extensionId, outputChannel\)\s*: handleHelpUri\(uri, extensionId\),?\s*\}\)/,
  );
  assert.equal(source.split("registerUriHandler(").length - 1, 1, "one handler per extension");
});

test("package.json activates the extension on a URI, so a rule link wakes it", () => {
  const manifest = JSON.parse(readSource("package.json")) as { activationEvents: string[] };
  assert.ok(manifest.activationEvents.includes("onUri"));
});

test("experimentSettingRefusal: unset and a string pass, anything else is refused naming the setting", () => {
  const setting = "swcraft.experiments.allocation.config";
  for (const value of [undefined, null, "", "experiments/try.yaml"]) {
    assert.equal(commandCore.experimentSettingRefusal(setting, value), undefined, String(value));
  }
  for (const value of [123, ["a.yaml"], { path: "a.yaml" }, true]) {
    assert.equal(
      commandCore.experimentSettingRefusal(setting, value),
      `SWCraft: the setting ${setting} must be the path of a configuration file, so the experiment did not run`,
    );
  }
});

test("decodeChunks: a character split across two chunks is decoded whole", () => {
  const bytes = Buffer.from("Generated report: /p/Bericht-\u00e9t\u00e9/report.md\n", "utf8");
  const split = bytes.indexOf(0xc3) + 1;
  const chunks = [bytes.subarray(0, split), bytes.subarray(split)];
  assert.equal(chunks[0].toString("utf8").endsWith("\ufffd"), true, "a per-chunk decode breaks it");
  assert.equal(commandCore.decodeChunks(chunks), bytes.toString("utf8"));
  assert.equal(commandCore.decodeChunks([]), "");
});

test("spawnCli keeps stdout as raw chunks and decodes them once", () => {
  const body = withoutCommentLines(
    functionBody(readSource("src/commands.ts"), "function spawnCli("),
  );
  assert.match(body, /stdoutChunks\.push\(chunk\)/);
  assert.match(body, /decodeChunks\(stdoutChunks\)/);
  // The channel is fed through one streaming decoder per stream, flushed on close.
  assert.match(body, /outputChannel\.append\(stdoutDecoder\.write\(chunk\)\)/);
  assert.match(body, /outputChannel\.append\(stderrDecoder\.write\(chunk\)\)/);
  assert.match(body, /outputChannel\.append\(stdoutDecoder\.end\(\) \+ stderrDecoder\.end\(\)\)/);
  assert.doesNotMatch(body, /chunk\.toString\(\)/);
  assert.doesNotMatch(body, /stdout \+= /);
  // Both the launch failure and the exit resolve with what was captured.
  assert.equal((body.match(/stdout: stdout\(\)/g) ?? []).length, 2);
  // Stderr is captured the same way (a refused check's reason), and `echoStdout: false` only stops
  // the stdout decoder from being fed.
  assert.match(body, /stderrChunks\.push\(chunk\)/);
  assert.match(body, /decodeChunks\(stderrChunks\)/);
  assert.equal((body.match(/stderr: stderr\(\)/g) ?? []).length, 2);
  assert.match(
    body,
    /if \(echoStdout\) \{\s*outputChannel\.append\(stdoutDecoder\.write\(chunk\)\);\s*\}/,
  );
  assert.match(body, /const echoStdout = options\.echoStdout \?\? true;/);
});

// --- the Problems panel's adapter -------------------------------------

test("registerCommands creates the one diagnostic collection and loads no reference table", () => {
  const source = withoutCommentLines(readSource("src/commands.ts"));
  const body = functionBody(source, "export function registerCommands(");
  assert.equal(
    (body.match(/createDiagnosticCollection\(/g) ?? []).length,
    1,
    "one collection, created in registerCommands",
  );
  assert.equal((source.match(/createDiagnosticCollection\(/g) ?? []).length, 1, "and nowhere else");
  assert.match(body, /createDiagnosticCollection\(DIAGNOSTIC_SOURCE\)/);
  // A damaged references.json must take no command with it: it is read per run.
  assert.doesNotMatch(body, /loadReferences\(/);
  assert.match(body, /const artifactPath = referencesArtifactPath\(context\);/);
  assert.match(
    body,
    /registerCommand\("swcraft\.checkModel", \(\) =>\s*checkModelCommand\(outputChannel, diagnostics, artifactPath\),?\s*\)/,
  );
  assert.match(
    body,
    /registerCommand\("swcraft\.build", \(\) =>\s*buildCommand\(outputChannel, diagnostics, artifactPath\),?\s*\)/,
  );
});

test("the reference table's path is spelled once and both readers call it", () => {
  const source = withoutCommentLines(readSource("src/commands.ts"));
  assert.equal(
    (source.match(/path\.join\(context\.extensionPath, "schemas", REFERENCES_ARTIFACT\)/g) ?? [])
      .length,
    1,
  );
  for (const declaration of [
    "export function registerNavigation(",
    "export function registerCommands(",
  ]) {
    assert.match(
      functionBody(source, declaration),
      /referencesArtifactPath\(context\)/,
      `${declaration} reads the path through referencesArtifactPath`,
    );
  }
});

test("Check Model and Build's check-model step both run through checkModelRun", () => {
  const source = withoutCommentLines(readSource("src/commands.ts"));
  const command = functionBody(source, "async function checkModelCommand(");
  assert.match(
    command,
    /withRunIndicator\(commandId, describeRunning\(commandId\), \(\) =>\s*checkModelRun\(/,
  );
  assert.match(command, /buildCliArgv\("check-model", manifestRelPath\)/);
  const build = functionBody(source, "async function buildCommand(");
  assert.match(
    build,
    /if \(step\.feedsDiagnostics === true\) \{\s*return checkModelRun\(\s*executable,\s*workspaceRoot,\s*step\.argv,/,
  );
  const run = functionBody(source, "async function checkModelRun(");
  assert.match(
    run,
    /spawnCli\(\s*executable,\s*checkModelJsonArgv\(baseArgv\),\s*workspaceRoot,\s*outputChannel,\s*\{ echoStdout: false \},?\s*\)/,
  );
  // The artifact is read per run, inside a guard; the verdict is the exit code, untouched.
  assert.match(run, /try \{\s*refs = loadReferences\(fs\.readFileSync\(artifactPath,/);
  assert.match(run, /return run;\s*\}\n/, "the run's exit code and stderr, untouched");
  // One per-run snapshot reader places every entry: one text of each file per run.
  assert.match(run, /const reader = fsRequestReader\(\);/);
  assert.match(
    run,
    /placements = placeRefusals\(manifestFile, reading\.reason, reader, \(file\) =>\s*rehomeRefusedFile\(file, modelRoot, fs\.realpathSync\),?\s*\);/,
  );
  assert.match(
    run,
    /placeFinding\(refs, finding, modelRoot \?\? workspaceRoot, manifestFile, reader\)/,
  );
  assert.doesNotMatch(run, /fsReader/);
  // Only the latest-started of two overlapping runs publishes.
  assert.match(run, /checkModelRunsStarted \+= 1;\s*const generation = checkModelRunsStarted;/);
  assert.match(
    run,
    /if \(generation === checkModelRunsStarted\) \{\s*publishPlacements\(collection, placements, ruleLinks\);\s*\}/,
  );
  // The previous run's entries are replaced whole, the targets with them.
  const publish = functionBody(source, "function publishPlacements(");
  assert.ok(publish.indexOf("collection.clear()") < publish.indexOf("collection.set("));
  assert.ok(publish.indexOf("findingTargets.clear()") < publish.indexOf("findingTargets.set("));
  assert.match(publish, /diagnostic\.source = DIAGNOSTIC_SOURCE;/);
  // Red for an error, yellow for a warning; the rule as the code; the further places as related.
  assert.match(
    publish,
    /place\.severity === "error"\s*\?\s*vscode\.DiagnosticSeverity\.Error\s*:\s*vscode\.DiagnosticSeverity\.Warning/,
  );
  // The rule as the code: the plain name, or the name with its guide link
  // (the link's own guard is "publishPlacements links a finding's rule …").
  assert.match(
    publish,
    /diagnostic\.code =\s*link === undefined \? place\.code : \{ value: place\.code,/,
  );
  assert.match(
    publish,
    /diagnostic\.relatedInformation = place\.related\.map\([\s\S]*new vscode\.DiagnosticRelatedInformation\(/,
  );
});

test("Check Model ends in Problems: no channel reveal, the focus moves after the outcome", () => {
  const source = withoutCommentLines(readSource("src/commands.ts"));
  const command = functionBody(source, "async function checkModelCommand(");
  assert.doesNotMatch(command, /outputChannel\.show\(/);
  const recorded = command.indexOf(
    'recordCliOutcome(commandId, "Check Model", run, PROBLEMS_PANEL_DETAILS);',
  );
  const focus = command.indexOf("vscode.commands.executeCommand(PROBLEMS_FOCUS_COMMAND)");
  assert.notEqual(recorded, -1, "checkModelCommand records its outcome");
  assert.notEqual(focus, -1, "checkModelCommand focuses Problems");
  assert.ok(focus > recorded, "the focus moves after the outcome is recorded");
  // On every finished run: the focus is the very next statement, behind no
  // condition and no early return, and the outcome's answer gates nothing.
  assert.doesNotMatch(command, /if \(!?recordCliOutcome\(/);
  assert.match(
    command,
    /\n\s*recordCliOutcome\(commandId, "Check Model", run, PROBLEMS_PANEL_DETAILS\);\n\s*void vscode\.commands\.executeCommand\(PROBLEMS_FOCUS_COMMAND\);\n\}?\s*$/,
    "the focus call is the statement right after the recorded outcome, and the last one",
  );
  // Build's check-model step moves no focus itself; Build's own ending does
  // (the buildRunEnding tests below).
  assert.doesNotMatch(
    functionBody(source, "async function checkModelRun("),
    /PROBLEMS_FOCUS_COMMAND|markers\.view/,
  );
});

test("the findings' status-bar item is set from describeFindingCounts and opens Problems", () => {
  const source = withoutCommentLines(readSource("src/commands.ts"));
  const publish = functionBody(source, "function publishPlacements(");
  assert.match(publish, /findingsStatusItem\.text = describeFindingCounts\(placements\);/);
  assert.match(publish, /findingsStatusItem\.tooltip = FINDING_COUNTS_TOOLTIP;/);
  assert.ok(
    publish.indexOf("findingsStatusItem.show()") > publish.indexOf("collection.set("),
    "the item shows once the entries are published",
  );
  // Set only there, behind the latest-started-run rule: no run touches it itself.
  assert.doesNotMatch(
    functionBody(source, "async function checkModelRun("),
    /findingsStatusItem\./,
  );
  assert.equal(
    source.match(/findingsStatusItem\.(text|tooltip|show)\b/g)?.length,
    3,
    "the item is written in publishPlacements alone",
  );
  const register = functionBody(source, "export function registerCommands(");
  assert.match(
    register,
    /const findingsItem = vscode\.window\.createStatusBarItem\(\s*"swcraft\.findings",\s*vscode\.StatusBarAlignment\.Left,?\s*\);/,
  );
  assert.match(register, /findingsItem\.name = "SWCraft Findings";/);
  assert.match(register, /findingsItem\.command = PROBLEMS_FOCUS_COMMAND;/);
  assert.match(register, /findingsStatusItem = findingsItem;/);
  assert.match(register, /context\.subscriptions\.push\(findingsItem\);/);
  // Hidden until the first publish: registration never shows it.
  assert.doesNotMatch(register, /findingsItem\.show\(/);
  // The same focus command as Check Model's.
  assert.match(
    readSource("src/diagnosticsCore.ts"),
    /export const PROBLEMS_FOCUS_COMMAND = "workbench\.panel\.markers\.view\.focus";/,
  );
});

test("the quick fixes are a code-action provider of the QuickFix kind, an edit and nothing more", () => {
  const source = withoutCommentLines(readSource("src/commands.ts"));
  const navigation = functionBody(source, "export function registerNavigation(");
  assert.match(
    navigation,
    /registerCodeActionsProvider\(\s*selector,[\s\S]*quickFixesOf\(\s*refs,\s*document,\s*codeActionContext\.diagnostics,\s*codeActionContext\.triggerKind,?\s*\)[\s\S]*\{ providedCodeActionKinds: \[vscode\.CodeActionKind\.QuickFix\] \}/,
  );
  const fixes = functionBody(source, "function quickFixesOf(");
  // Only an explicit request gets an edit: the proactive light bulb and the save participant
  // (`editor.codeActionsOnSave`) ask with Automatic, and a save participant applies what it gets.
  const invoke = fixes.search(
    /if \(triggerKind !== vscode\.CodeActionTriggerKind\.Invoke\) \{\s*return \[\];\s*\}/,
  );
  assert.ok(invoke >= 0, "quickFixesOf refuses a request that is not Invoke");
  for (const later of [
    "for (const diagnostic of diagnostics)",
    "modelFileOf(",
    "quickFixFor(",
    "new vscode.CodeAction(",
    "new vscode.WorkspaceEdit(",
  ]) {
    assert.ok(invoke < fixes.indexOf(later), `the Invoke check precedes ${later}`);
  }
  // Only SWCraft entries of the two link rules, filtered before any file is read.
  // The rule is read through `diagnosticRule()`, so a linked code (an object)
  // keeps its fix.
  assert.match(
    fixes,
    /const code = diagnosticRule\(diagnostic\.code\);\s*if \(\s*diagnostic\.source === DIAGNOSTIC_SOURCE &&\s*code !== undefined &&\s*QUICK_FIX_RULES\.has\(code\)\s*\)/,
  );
  assert.ok(fixes.indexOf("QUICK_FIX_RULES.has(") < fixes.indexOf("modelFileOf("));
  // The line's link must name the finding's requirement; the version is the saved file's.
  assert.match(fixes, /findingTargets\.get\(/);
  assert.match(
    fixes,
    /quickFixFor\(code, target, linkAt\(text, line\), \(id\) =>\s*requirementVersion\(refs, id, at\.modelRoot, reader\),?\s*\)/,
  );
  assert.match(fixes, /const line = diagnostic\.range\.start\.line;/);
  assert.match(
    fixes,
    /action\.edit\.replace\(\s*document\.uri,\s*new vscode\.Range\(line, fix\.start, line, fix\.end\),\s*fix\.replacement,?\s*\)/,
  );
  // No command on the action, no save, no apply: the person picks it and saves.
  for (const absent of [".save(", "applyEdit(", "writeFile"]) {
    assert.equal(fixes.includes(absent), false, `quickFixesOf holds no ${absent}`);
  }
  assert.doesNotMatch(fixes, /\bcommand\b/, "quickFixesOf sets no command");
  // Not preferred: the Auto Fix keystroke does not apply it unpicked.
  assert.equal(source.includes("isPreferred"), false, "no code action is marked preferred");
  // Nothing in the adapter listens to a save.
  assert.doesNotMatch(source, /onDidSaveTextDocument/);
});

test("SPAWNING_COMMAND_LABELS keeps Check Model's entry: no command was added for the panel", () => {
  assert.equal(SPAWNING_COMMAND_LABELS["swcraft.checkModel"], "Check Model");
});

test("each experiment kind is registered by a literal call to experimentCommand", () => {
  const source = withoutCommentLines(readSource("src/commands.ts"));
  for (const [kind, entry] of Object.entries(EXPERIMENT_KINDS)) {
    const id = entry.commandId.replace(".", "\\.");
    assert.match(
      source,
      new RegExp(
        `registerCommand\\("${id}", \\(\\) =>\\s*experimentCommand\\("${kind}", outputChannel\\)`,
      ),
    );
  }
});

// --- The Trace Links view's flows ---------------------------------

test("traceApplyCommandFlow: flush, then the imports' dry-run sequence in the Trace Apply Report tab — no modal, no Show Report", () => {
  const body = withoutCommentLines(
    functionBody(readSource("src/commands.ts"), "async function traceApplyCommandFlow("),
  );
  const order = [
    "traceFileIsDirty()",
    // Busy is held before the flush, so a second start during it is refused.
    "return holdingTraceBusy(setTraceBusy, reserved, async () => {",
    "await flushTraceWrites()",
    "return traceApplyFlow(",
    "tabs: importTabs,",
    "render: importReportRendering,",
    "recordCliOutcome(commandId, label, run)",
    "reload: () => loadTraceFile(file)",
    "withRunIndicator(commandId, describeRunning(commandId)",
    "withImportReportFile(",
    // The core's options, one per run: { dryRun: true } or { dryRun: false, expectPlan }.
    "buildTraceApplyArgv(manifestRelPath, file, { ...options, report })",
  ];
  let at = -1;
  for (const step of order) {
    const found = body.indexOf(step, at + 1);
    assert.ok(found > at, `'${step}' comes after the step before it`);
    at = found;
  }
  // The preview tab's Write and Cancel replace the modal, and the same tab shows the applied
  // report: no notification carries Show Report.
  assert.doesNotMatch(body, /modal: true|Show Report|SHOW_REPORT_BUTTON|applyDoneMessage/);
  assert.doesNotMatch(readSource("src/traceCore.ts"), /SHOW_REPORT_BUTTON|"Show Report"/);
  // Busy set before the flow and cleared after it unless reserved: holdingTraceBusy, driven in traceCore.test.ts.
  assert.equal(body.includes("setTraceBusy(true)") || body.includes("finally"), false);
  // The flow is the core's, the imports' sequence: one withImportReportFile per run, from one site.
  assert.equal(body.split("withImportReportFile(").length - 1, 1);
  const core = withoutCommentLines(
    functionBody(readSource("src/importTabCore.ts"), "export async function traceApplyFlow("),
  );
  assert.match(core, /await dryRunSequence\(host, \{/);
  assert.match(core, /buttons: applyButtons\(false\)/);
  assert.match(core, /return run\(\{ dryRun: false, expectPlan \}\)/);
});

test("traceChooseModeCommand: the documents, the mode's picker, the dropped question, then a search only when needed", () => {
  const body = withoutCommentLines(
    functionBody(readSource("src/commands.ts"), "async function traceChooseModeCommand("),
  );
  const order = [
    "withTraceBusy(",
    "traceConfigFile(resolved.workspaceRoot, outputChannel)",
    "traceDocumentsAnswer(resolved, projectDir, configFile, outputChannel)",
    "const remembered = traceState.modes;",
    "modesInScope(remembered, answer)",
    "modePickItems(answer, before, mode)",
    "chooseDocuments(",
    "droppedWithDecisions(traceState.proposals, remembered, modes)",
    "traceApplyCommandFlow(resolved, outputChannel, true)",
    "clearDecisions(proposals, documents)",
    // Re-checked after either answer: nothing of a dropped document may still wait.
    "droppedWithDecisions(traceState.proposals, remembered, modes)",
    "DROPPED_STILL_PENDING",
    "searchNeeded(traceState.proposals, modes, answer)",
    "setTraceModes(modes)",
    "traceSearchRun(resolved, projectDir, answer, modes, configFile, outputChannel)",
    "setTraceModes(modes)",
  ];
  let at = -1;
  for (const step of order) {
    const found = body.indexOf(step, at + 1);
    assert.ok(found > at, `'${step}' comes after the step before it`);
    at = found;
  }
});

test("traceSearchRun: the pending question, then the search; the question cannot loop", () => {
  const body = withoutCommentLines(
    functionBody(readSource("src/commands.ts"), "async function traceSearchRun("),
  );
  const order = [
    "traceFileIsDirty()",
    "pendingQuestion(count)",
    "traceApplyCommandFlow(resolved, outputChannel, true)",
    "pendingCount(after) > 0",
    "STILL_PENDING",
    "discardPending = true",
    "labellingArgumentOf(modes)",
    "await flushTraceWrites()",
    "traceFileIsDirty()",
    "buildTraceSearchArgv(manifestRelPath, {",
    "labelling,",
    "searchRefusedPending(ran.stderr)",
    "ran = await search(true)",
    "recordCliOutcome(commandId, label, ran)",
    "proposalsPathOf(ran.stdout, workspaceRoot)",
    "loadTraceFile(written)",
    ".focus`",
  ];
  let at = -1;
  for (const step of order) {
    const found = body.indexOf(step, at + 1);
    assert.ok(found > at, `'${step}' comes after the step before it`);
    at = found;
  }
});

test("withTraceBusy holds busy for the whole flow and drops it in finally", () => {
  const body = withoutCommentLines(
    functionBody(readSource("src/commands.ts"), "async function withTraceBusy("),
  );
  const order = [
    "traceState.busy",
    "setTraceBusy(true)",
    "await flow()",
    "finally",
    "setTraceBusy(false)",
  ];
  let at = -1;
  for (const step of order) {
    const found = body.indexOf(step, at + 1);
    assert.ok(found > at, `'${step}' comes after the step before it`);
    at = found;
  }
  for (const name of ["async function traceSearchRun(", "async function traceChooseModeCommand("]) {
    const flow = withoutCommentLines(functionBody(readSource("src/commands.ts"), name));
    assert.equal(flow.includes("setTraceBusy("), false, `${name} never drops busy itself`);
  }
});

test("experimentCommand hands the trace kind to traceSearchCommand before anything else", () => {
  const body = withoutCommentLines(
    functionBody(readSource("src/commands.ts"), "async function experimentCommand("),
  );
  const branch = body.indexOf('if (kind === "trace") {');
  assert.ok(branch !== -1 && branch < body.indexOf("resolveManifestOrNotify()"));
  assert.match(
    body,
    /if \(kind === "trace"\) \{\s*return traceSearchCommand\(outputChannel\);\s*\}/,
  );
});

test("deactivate() returns the Trace Links view's flush, which cancels the timer and writes", () => {
  const extension = withoutCommentLines(readSource("src/extension.ts"));
  assert.match(
    extension,
    /export function deactivate\(\): Promise<void> \{\s*return flushTraceWrites\(\);\s*\}/,
  );
  const flush = withoutCommentLines(
    functionBody(readSource("src/commands.ts"), "export function flushTraceWrites("),
  );
  assert.ok(
    flush.indexOf("clearTimeout(traceState.timer)") < flush.indexOf("writeTraceFile(queued)"),
  );
});

test("a panel write compares the bytes on disk first and renames an exclusive .partial", () => {
  const body = withoutCommentLines(
    functionBody(readSource("src/commands.ts"), "function writeTraceFile("),
  );
  const compare = body.indexOf("sameBytes(readBytesOrNothing(file), traceState.bytes)");
  const exclusive = body.indexOf('{ flag: "wx" }');
  const rename = body.indexOf("fs.renameSync(partial, file)");
  assert.ok(compare !== -1 && compare < exclusive && exclusive < rename, body);
  assert.ok(body.includes("CHANGED_ON_DISK"));
  // The panel's one write site, never a model file.
  const source = withoutCommentLines(readSource("src/commands.ts"));
  assert.equal(source.split("fs.renameSync(").length - 1, 1);
});

// --- The Resources view ---------------------------------------------

test("resourceReportJsonArgv appends --format json, and --report for a press, and never inserts", () => {
  const base = buildCliArgv("resource-report", "swcraft.yaml");
  const plain = ["resource-report", "--project", "swcraft.yaml", "--format", "json"];
  assert.deepEqual(resourceReportJsonArgv(base, { report: false }), plain);
  assert.deepEqual(resourceReportJsonArgv(base, { report: true }), [...plain, "--report"]);
  assert.deepEqual(base, ["resource-report", "--project", "swcraft.yaml"], "the base is untouched");
  assert.equal(SPAWNING_COMMAND_LABELS["swcraft.resourcesRefresh"], "Resource Report");
  assert.equal(SPAWNING_COMMAND_LABELS["swcraft.resourcesFit"], undefined);
});

test("only a press asks for the file: Refresh and the refresh after a Build do not", () => {
  const source = withoutCommentLines(readSource("src/commands.ts"));
  assert.match(
    source,
    /registerCommand\("swcraft\.resourceReport", \(\) =>\s*resourceReportCommand\(outputChannel, \{ report: true \}\),?\s*\)/,
  );
  assert.match(
    source,
    /registerCommand\("swcraft\.resourcesRefresh", \(\) =>\s*resourceReportCommand\(outputChannel, \{ report: false \}\),?\s*\)/,
  );
  assert.match(
    functionBody(source, "async function refreshResourcesAfterBuild("),
    /resourceReportRun\(resolved, \{ report: false \}, outputChannel\)/,
  );
  assert.match(
    functionBody(source, "async function resourceReportCommand("),
    /resourceReportRun\(resolved, \{ report \}, outputChannel\)/,
  );
  // These are the only runs, so the three argvs are the three above.
  assert.equal(source.match(/await resourceReportRun\(/g)?.length, 2);
});

test("the Resources view's run: --format json, its stdout captured, the channel gets the log", () => {
  const source = withoutCommentLines(readSource("src/commands.ts"));
  const run = functionBody(source, "async function resourceReportRun(");
  assert.match(
    run,
    /resourceReportJsonArgv\(\s*buildCliArgv\("resource-report", resolved\.manifestRelPath\),\s*options,?\s*\)/,
  );
  assert.match(
    run,
    /withRunIndicator\(commandId, describeRunning\(commandId\), \(\) =>\s*spawnCli\(resolveSwcraftExecutable\(\), argv, [\s\S]*?\{\s*echoStdout: false,?\s*\}/,
  );
  assert.match(run, /const commandId = COMMAND_IDS\["resource-report"\];/);
  // The log, never the report: one closing line, and the stdout written nowhere.
  assert.equal(run.split("outputChannel.appendLine(").length - 1, 1);
  assert.match(
    run,
    /outputChannel\.appendLine\(resourceRunLine\(run\.exitCode, read, options\)\);/,
  );
  assert.doesNotMatch(run, /append(Line)?\(run\.stdout/);
  assert.doesNotMatch(
    run,
    /outputChannel\.show\(/,
    "the answer is the view, as Check Model's is Problems",
  );
  // A failed exit or an unreadable document keeps the last good tree.
  assert.match(
    run,
    /resourceState\.failed = true;\s*resourceState\.root = resolved\.workspaceRoot;\s*refreshResourceView\(\);\s*return undefined;/,
  );
  // The stderr is answered only with a shown document: no failure and no other folder opens a tab.
  assert.equal(run.match(/return run\.stderr;/g)?.length, 1);
  assert.ok(
    run.indexOf("resourceState.document = read;") < run.indexOf("return run.stderr;"),
    "the stderr is answered after the document is shown",
  );
  assert.equal(run.match(/return undefined;/g)?.length, 2);
  const failure = run.indexOf("resourceState.failed = true;");
  assert.ok(
    failure < run.indexOf("resourceState.document = read;"),
    "the document is kept on a failure",
  );
  assert.match(run, /showWarningMessage\(`SWCraft: \$\{read\.error\}`\)/);
  // The run is stamped with its start, before the CLI reads a file, as Build and
  // Diagrams stamp a generation; only a shown document takes the stamp, so a
  // failed run keeps the mark it had, and a change during the run survives it.
  const stamp = run.indexOf("const startedAt = Date.now();");
  assert.ok(stamp !== -1 && stamp < run.indexOf("withRunIndicator("), "stamped before the run");
  assert.equal(run.split("resourceState.readAt = ").length - 1, 1);
  assert.ok(
    failure < run.indexOf("resourceState.readAt = startedAt;"),
    "a failed run never takes the stamp",
  );
  assert.doesNotMatch(run, /newestChangeAt/, "a run never clears a change it did not read");
  // A change is kept whether or not a report is shown, and redraws only when the mark moves.
  const mark = functionBody(source, "function markResourcesOutOfDate(");
  assert.match(mark, /changedAt > resourceState\.newestChangeAt/);
  assert.doesNotMatch(mark, /resourceState\.document/);
  assert.match(mark, /if \(resourceReportState\(\) !== before\) \{\s*refreshResourceView\(\);/);
  // The command that runs the report goes through this one function.
  const command = functionBody(source, "async function resourceReportCommand(");
  assert.match(command, /resourceReportRun\(/);
  assert.doesNotMatch(command, /spawnCli\(/);
});

test("the Resources view's runs are refused while one is in progress, the busy refusal of Trace Links", () => {
  const source = withoutCommentLines(readSource("src/commands.ts"));
  const busy = functionBody(source, "async function withResourcesBusy(");
  assert.match(
    busy,
    /if \(resourceState\.busy\) \{\s*void vscode\.window\.showWarningMessage\(BUSY_REFUSAL\);\s*return;\s*\}/,
  );
  assert.match(busy, /finally \{\s*setResourcesBusy\(false\);\s*\}/);
  assert.match(
    functionBody(source, "async function resourceReportCommand("),
    /await withResourcesBusy\(async \(\) =>/,
  );
});

test("a green Build refreshes the Resources view after its report's preview, outside the views' queue", () => {
  const body = functionBody(
    withoutCommentLines(readSource("src/commands.ts")),
    "async function buildCommand(",
  );
  // The tail's shape, the preview first and the gate around one call, is the ending test's regex.
  const refresh = body.indexOf("if (buildRunRefreshesResources(result)) {");
  const statuses = [...body.matchAll(/recordCommandStatus\(\s*"swcraft\.build"/g)];
  const recorded = statuses.at(-1)?.index ?? -1;
  const tracked = body.indexOf("artifactTracker?.recordBuild(");
  assert.notEqual(refresh, -1, "the refresh is gated by the pure decision");
  assert.notEqual(recorded, -1, "the run's status is recorded");
  assert.notEqual(tracked, -1, "the run's generations are recorded");
  assert.ok(
    recorded < refresh,
    "Build's status is recorded first, so a failed refresh leaves Build green",
  );
  assert.ok(
    tracked < refresh,
    "after the queued run has ended, so a Diagrams press does not wait for the report",
  );
  assert.doesNotMatch(
    body,
    /resourceReportRun\(|revealResources\(|resourceReportCommand\(|executeCommand\("swcraft\.resourceReport"\)/,
  );
});

test("the refresh after a Build skips silently and moves no focus", () => {
  const body = functionBody(
    withoutCommentLines(readSource("src/commands.ts")),
    "async function refreshResourcesAfterBuild(",
  );
  // No await between the check and the hold, so withResourcesBusy()'s refusal is never shown.
  // The four skip conditions: a report, a refresh in hand, a toolchain run, a folder no longer
  // first.
  assert.match(
    body,
    /if \(\s*resourceState\.busy \|\|\s*resourceRefreshRun !== undefined \|\|\s*toolchainRunInFlight \|\|\s*resolved\.workspaceRoot !== getWorkspaceRoot\(\)\s*\) \{\s*return;\s*\}\s*const run = async \(\): Promise<void> => \{\s*try \{\s*await withResourcesBusy\(async \(\) => \{\s*await updateResourcesAvailable\(\);\s*await resourceReportRun\(resolved, \{ report: false \}, outputChannel\);\s*\}\);/,
  );
  assert.doesNotMatch(
    body,
    /BUSY_REFUSAL|showWarningMessage|revealResources\(|\.focus|previewGeneratedReport\(/,
  );
});

test("the refresh after a Build never rejects: a failure ends in the channel and the hold is released", () => {
  const body = functionBody(
    withoutCommentLines(readSource("src/commands.ts")),
    "async function refreshResourcesAfterBuild(",
  );
  assert.match(
    body,
    /\} catch \(error: unknown\) \{\s*outputChannel\.appendLine\([\s\S]*?\} finally \{\s*resourceRefreshRun = undefined;\s*\}\s*\};\s*resourceRefreshRun = run\(\);\s*await resourceRefreshRun;\s*\}\s*$/,
  );
});

test("during the refresh after a Build a press waits and runs its own report; Refresh waits, reveals and opens no tab", () => {
  const source = withoutCommentLines(readSource("src/commands.ts"));
  const report = functionBody(source, "async function resourceReportCommand(");
  const waited = report.search(
    /if \(resourceRefreshRun !== undefined\) \{\s*await resourceRefreshRun;\s*if \(!report\) \{\s*await revealResources\(\);\s*return;\s*\}\s*\}/,
  );
  const held = report.indexOf("await withResourcesBusy(");
  assert.notEqual(waited, -1, "only Refresh returns after the wait, with the view revealed");
  assert.notEqual(held, -1, "a press runs the report, after the wait or with none");
  assert.ok(waited < held, "before withResourcesBusy() could refuse either");
  assert.ok(
    report.indexOf("return;", waited) < report.indexOf("previewGeneratedReport("),
    "Refresh's return during a Build's refresh stands before the one preview",
  );
  assert.equal(
    source.match(/resourceRefreshRun = /g)?.length,
    2,
    "set by the refresh and cleared by it, nowhere else",
  );
});

test("the command reveals, then previews the run's stderr, and only a press with a shown document does", () => {
  const body = functionBody(
    withoutCommentLines(readSource("src/commands.ts")),
    "async function resourceReportCommand(",
  );
  assert.match(
    body,
    /const stderr = await resourceReportRun\(resolved, \{ report \}, outputChannel\);\s*await revealResources\(\);\s*if \(report && stderr !== undefined\) \{\s*await previewGeneratedReport\(stderr, resolved\.workspaceRoot, outputChannel\);\s*\}\s*\}\);\s*\}?\s*$/,
  );
  assert.equal(body.match(/previewGeneratedReport\(/g)?.length, 1);
});

test("a press with no report line says why in the channel: the run's stderr is echoed, then no path is one line", () => {
  // A pin mismatch: the document fills the view, the CLI's two Warning lines are the stderr
  // (test_cli.py, test_a_pin_mismatch_answers_and_writes_no_file), and no tab opens.
  const source = withoutCommentLines(readSource("src/commands.ts"));
  const run = functionBody(source, "async function resourceReportRun(");
  assert.match(
    run,
    /spawnCli\(resolveSwcraftExecutable\(\), argv, resolved\.workspaceRoot, outputChannel, \{\s*echoStdout: false,\s*\}\)/,
  );
  assert.match(
    functionBody(source, "function spawnCli("),
    /child\.stderr\?\.on\("data", \(chunk: Buffer\) => \{\s*stderrChunks\.push\(chunk\);\s*outputChannel\.append\(stderrDecoder\.write\(chunk\)\);\s*\}\);/,
  );
  const preview = functionBody(source, "async function previewGeneratedReport(");
  assert.match(
    preview,
    /if \(report === undefined\) \{\s*outputChannel\.appendLine\(\s*"SWCraft: the run printed no report path, so there is nothing to preview",\s*\);\s*return;\s*\}/,
  );
});

test("Check Model runs no report, by the command or by Build's check-model step", () => {
  const source = withoutCommentLines(readSource("src/commands.ts"));
  for (const declaration of [
    "async function checkModelCommand(",
    "async function checkModelRun(",
  ]) {
    assert.doesNotMatch(
      functionBody(source, declaration),
      /resourceReportRun\(|refreshResourcesAfterBuild\(|withResourcesBusy\(|resourceReportCommand\(|executeCommand\("swcraft\.resourceReport"\)/,
      declaration,
    );
  }
});

test("the reveal awaits the view's when before its focus, and selects no row", () => {
  const reveal = functionBody(
    withoutCommentLines(readSource("src/commands.ts")),
    "async function revealResources(",
  );
  assert.match(
    reveal,
    /await updateResourcesAvailable\(\);\s*await vscode\.commands\.executeCommand\(`\$\{RESOURCES_VIEW_ID\}\.focus`\);/,
  );
  assert.doesNotMatch(reveal, /\.reveal\(/);
});

test("a workspace-folder change drops a report read in another folder, never one of the folder open", () => {
  const source = withoutCommentLines(readSource("src/commands.ts"));
  assert.match(source, /onDidChangeWorkspaceFolders\(resourceFoldersChanged\)/);
  const changed = functionBody(source, "function resourceFoldersChanged(");
  assert.match(changed, /void updateResourcesAvailable\(\);/);
  assert.match(
    changed,
    /if \(resourceState\.root === undefined \|\| resourceState\.root === getWorkspaceRoot\(\)\) \{\s*return;\s*\}/,
  );
  for (const key of ["document", "readAt", "root"]) {
    assert.match(changed, new RegExp(`resourceState\\.${key} = undefined;`), key);
  }
  assert.match(changed, /refreshResourceView\(\);/);
  // A shown document and a failed run both record their folder, so a change drops either: a
  // first run that failed, with no document, would otherwise show the next folder its failure.
  const run = functionBody(source, "async function resourceReportRun(");
  assert.equal(run.split("resourceState.root = resolved.workspaceRoot;").length - 1, 2);
  assert.match(
    run,
    /resourceState\.failed = true;\s*resourceState\.root = resolved\.workspaceRoot;/,
  );
  // A run in flight across the change brings no answer back to the new folder:
  // neither its document nor its failure reaches the state.
  const moved = run.indexOf("if (resolved.workspaceRoot !== getWorkspaceRoot()) {");
  assert.ok(moved > run.indexOf("spawnCli("), "the folder is compared after the run");
  assert.ok(moved < run.indexOf("resourceState.failed = true;"), "before the failure is kept");
  assert.ok(moved < run.indexOf("resourceState.document = read;"), "before the document is kept");
  assert.match(
    run,
    /if \(resolved\.workspaceRoot !== getWorkspaceRoot\(\)\) \{\s*recordCliOutcome\(commandId, label, run\);\s*return undefined;\s*\}/,
  );
});

test("a Resources row's click opens one preview tab, Trace Links' showPreviewTab, never a split", () => {
  const source = withoutCommentLines(readSource("src/commands.ts"));
  const body = functionBody(source, "async function resourcesOpenCommand(");
  assert.equal(/showTextDocument|ViewColumn/.test(body), false, "it opens the Trace Links way");
  assert.equal(body.split("showPreviewTab(").length - 1, 1);
  assert.match(body, /resolveElement\(facts, element, modelRoot, reader\)/);
  assert.match(body, /placeSubject\(refs, subject, modelRoot, resolved\.manifestPath, reader\)/);
  // Only a named element that is not found warns; a layer-wide finding opens where
  // placeSubject puts it (the manifest's first line), as the Problems panel does.
  assert.match(
    body,
    /if \(element !== undefined && found === undefined\) \{\s*void vscode\.window\.showWarningMessage\(\s*resourceNotFoundMessage\(\{ layer: subject\.layer, element \}\),?\s*\);\s*return;/,
  );
  // A row with key paths opens at their line (line 0 when absent); a finding at its place.
  assert.match(body, /resourceLine\(text, lineKeys\)/);
  assert.match(body, /lineKeys === undefined \|\| found === undefined\s*\?\s*placeSubject\(/);
  assert.match(body, /showPreviewTab\(place\.file, place\.line\)/);
  // The Trace Links positioning: the selection on the line, the preview tab, the focus kept.
  const preview = functionBody(source, "async function showPreviewTab(");
  assert.match(preview, /selection: new vscode\.Range\(line, 0, line, 0\)/);
  assert.match(preview, /preview: true/);
  assert.match(preview, /preserveFocus: true/);
  assert.match(preview, /viewColumn: vscode\.ViewColumn\.Active/);
  // Only a node that carries `open` gets the click; a topology row carries none.
  const item = functionBody(source, "function resourceTreeItem(");
  assert.match(item, /if \(node\.open !== undefined\) \{[^}]*command: "swcraft\.resourcesOpen"/);
});

test("the Resource Report item, the badge and the Resources view read one out-of-date state", () => {
  const source = withoutCommentLines(readSource("src/commands.ts"));
  const states = functionBody(source, "function artifactStates(");
  assert.match(states, /const resources = resourceReportState\(\);/);
  const state = functionBody(source, "function resourceReportState(");
  assert.match(
    state,
    /decideResourceReportState\(\{\s*readAt: resourceState\.readAt,\s*newestChangeAt: resourceState\.newestChangeAt,?\s*\}\)/,
  );
  // The view's own description reads the same state.
  const refreshView = functionBody(source, "function refreshResourceView(");
  assert.match(refreshView, /isOutOfDate\(resourceReportState\(\)\)/);
  assert.doesNotMatch(source, /resourceState\.outOfDate/, "no second, boolean state");
  // Every change of the view's state redraws the Model tree and the badge.
  const refresh = functionBody(source, "function refreshResourceView(");
  assert.match(refresh, /artifactsChanged\(\);/);
  const changed = functionBody(source, "function artifactsChanged(");
  assert.match(changed, /describeBadge\(artifactStates\(getWorkspaceRoot\(\)\)\)/);
});

// --- The Impact view ----------------------------------------------------
//
// commands.ts imports `vscode`, so its half of the view is held by source guards, as the Resources
// view's is; the tree, the question and the run line are impactCore's (impactCore.test.ts).

test("the Impact view's run: the whole walk as JSON, its stdout captured, the channel gets the log", () => {
  const source = withoutCommentLines(readSource("src/commands.ts"));
  const run = functionBody(source, "async function impactRun(");
  assert.match(run, /const argv = buildImpactTreeArgv\(resolved\.manifestRelPath, name\);/);
  assert.match(
    run,
    /withRunIndicator\(commandId, describeRunning\(commandId\), \(\) =>\s*spawnCli\(resolveSwcraftExecutable\(\), argv, [\s\S]*?\{\s*echoStdout: false,?\s*\}/,
  );
  assert.match(run, /const commandId = COMMAND_IDS\.impact;/);
  // The log, never the answer: one closing line, and the stdout written nowhere.
  assert.equal(run.split("outputChannel.appendLine(").length - 1, 1);
  assert.match(run, /outputChannel\.appendLine\(\s*impactRunLine\(run\.exitCode,/);
  assert.doesNotMatch(run, /append(Line)?\(run\.stdout/);
  assert.doesNotMatch(run, /outputChannel\.show\(/, "the answer is the view");
  // One parser: impactCore's readImpactDocument over navigationCore's parseImpactDocument.
  assert.match(run, /readImpactDocument\(run\.stdout\)/);
  // What the view keeps after a run is impactCore's impactAfterRun, tested there
  // behaviourally (a kept tree, a held failed name, the stamp); the run writes no
  // field of the state itself.
  assert.match(run, /outputChannel\.appendLine\(impactRunLine\(run\.exitCode, read\)\);/);
  assert.match(run, /showWarningMessage\(`SWCraft: \$\{read\.error\}`\)/);
  assert.equal(run.split("impactAfterRun(").length - 1, 1);
  assert.match(
    run,
    /impactState = impactAfterRun\(impactState, \{\s*root: resolved\.workspaceRoot,\s*name,\s*refs,\s*startedAt,\s*read,?\s*\}\);\s*refreshImpactView\(\);\s*return true;/,
  );
  assert.doesNotMatch(run, /impactState\.\w+ = /, "no field written beside the reducer");
  // Stamped with the run's start, taken before the run.
  const stamp = run.indexOf("const startedAt = Date.now();");
  assert.ok(stamp !== -1 && stamp < run.indexOf("withRunIndicator("), "stamped before the run");
  assert.doesNotMatch(run, /newestChangeAt/, "a run never clears a change it did not read");
  // Only the reducers move the kept answer.
  for (const field of ["document", "question", "readAt", "root", "failed", "refs"]) {
    assert.doesNotMatch(source, new RegExp(`impactState\\.${field} = `), field);
  }
  // Every question goes through this one run; Refresh asks the shown question.
  for (const declaration of [
    "async function impactCommand(",
    "async function impactRefreshCommand(",
    "async function askImpact(",
  ]) {
    assert.doesNotMatch(functionBody(source, declaration), /spawnCli\(/, declaration);
  }
  assert.match(functionBody(source, "async function askImpact("), /await impactRun\(/);
  assert.match(
    functionBody(source, "async function impactRefreshCommand("),
    /const question = impactState\.question;[\s\S]*await askImpact\(resolved, question, outputChannel\);/,
  );
});

test("the Impact view's questions are refused while one runs, the busy refusal of Trace Links", () => {
  const source = withoutCommentLines(readSource("src/commands.ts"));
  const busy = functionBody(source, "async function withImpactBusy(");
  assert.match(
    busy,
    /if \(impactState\.busy\) \{\s*void vscode\.window\.showWarningMessage\(BUSY_REFUSAL\);\s*return;\s*\}/,
  );
  assert.match(busy, /finally \{\s*setImpactBusy\(false\);\s*\}/);
  assert.match(
    functionBody(source, "async function askImpact("),
    /await withImpactBusy\(async \(\) =>/,
  );
  assert.match(
    functionBody(source, "function setImpactBusy("),
    /setContext", "swcraft\.impact\.busy", busy\)/,
  );
  assert.match(
    source,
    /registerCommand\("swcraft\.impact", \(arg, lensUri\) =>\s*impactCommand\(outputChannel, arg, lensUri\),?\s*\)/,
  );
  assert.match(
    source,
    /registerCommand\("swcraft\.impactRefresh", \(\) =>\s*impactRefreshCommand\(outputChannel\),?\s*\)/,
  );
  assert.match(
    source,
    /registerCommand\("swcraft\.impactOpen", \(node\) => impactOpenCommand\(node\)\)/,
  );
  assert.equal(SPAWNING_COMMAND_LABELS["swcraft.impact"], "Impact");
  assert.equal(SPAWNING_COMMAND_LABELS["swcraft.impactRefresh"], "Impact");
});

test("the Impact question is the active editor's, through impactQuestion, or a message and no run", () => {
  const source = withoutCommentLines(readSource("src/commands.ts"));
  const command = functionBody(source, "async function askImpact(");
  assert.match(
    command,
    /if \("message" in question\) \{\s*void vscode\.window\.showInformationMessage\(question\.message\);\s*return;\s*\}/,
  );
  assert.ok(command.indexOf("impactQuestion(") < command.indexOf("impactRun("));
  // Revealed only when the run's outcome was recorded in the folder still open first.
  assert.match(
    command,
    /if \(await impactRun\(resolved, question\.name, refs, outputChannel\)\) \{\s*await revealImpact\(\);\s*\}/,
  );
  assert.equal(command.split("revealImpact(").length - 1, 1);
  // The editor: the primary cursor's line, the live text, the dirty flag; a non-file document is none.
  const editor = functionBody(source, "function activeImpactEditor(");
  assert.match(editor, /line: editor\.selection\.active\.line/);
  assert.match(editor, /text: document\.getText\(\)/);
  assert.match(editor, /isDirty: document\.isDirty/);
  assert.match(editor, /document\.uri\.scheme !== "file"/);
  // The resolution is Go to Definition's own: impactCore holds the call, guarded in impactCore.test.ts.
});

test("an Impact row's click opens one preview tab at dependentLocations' first place, never a split", () => {
  const source = withoutCommentLines(readSource("src/commands.ts"));
  const body = functionBody(source, "async function impactOpenCommand(");
  assert.equal(/showTextDocument|ViewColumn/.test(body), false, "it opens the Trace Links way");
  assert.equal(body.split("showPreviewTab(").length - 1, 1);
  assert.match(body, /showPreviewTab\(place\.file, place\.line\)/);
  assert.match(
    body,
    /const \[place\] = dependentLocations\(\s*refs,\s*\{ layer: element\.token, name: element\.name \},\s*impact\.rootLayer \?\? "",\s*impact\.rootName,/,
    "the asked name is the drawn node's, set by impactNodes, never re-derived from the state",
  );
  assert.match(body, /const refs =\s*impactState\.refs \?\?\s*loadReferences\(/);
  assert.match(
    body,
    /if \(place === undefined\) \{\s*void vscode\.window\.showWarningMessage\(\s*impactFileNotFoundMessage\(impactLayerLabel\(refs, element\.token\), element\.name\),?\s*\);\s*return;/,
  );
  for (const absent of ["spawnCli(", "withRunIndicator(", "resolveSwcraftExecutable("]) {
    assert.equal(body.includes(absent), false, absent);
  }
});

test("the Impact view: its out of date is the model watch's stamp, read through isOutOfDate", () => {
  const source = withoutCommentLines(readSource("src/commands.ts"));
  assert.match(
    functionBody(source, "function noteModelChange("),
    /markResourcesOutOfDate\(changedAt\);\s*markImpactOutOfDate\(changedAt\);/,
  );
  const mark = functionBody(source, "function markImpactOutOfDate(");
  assert.match(mark, /changedAt > impactState\.newestChangeAt/);
  assert.doesNotMatch(mark, /impactState\.document/);
  assert.match(mark, /if \(impactAnswerState\(\) !== before\) \{\s*refreshImpactView\(\);/);
  assert.match(
    functionBody(source, "function impactAnswerState("),
    /decideResourceReportState\(\{\s*readAt: impactState\.readAt,\s*newestChangeAt: impactState\.newestChangeAt,?\s*\}\)/,
  );
  const refresh = functionBody(source, "function refreshImpactView(");
  assert.match(refresh, /outOfDate: isOutOfDate\(impactAnswerState\(\)\)/);
  assert.match(
    refresh,
    /setContext",\s*"swcraft\.impact\.loaded",\s*impactState\.question !== undefined/,
  );
  assert.match(refresh, /impactView\.description = impactTree\.description;/);
  // No Model item and no badge count: the view redraws itself alone.
  assert.equal(refresh.includes("artifactsChanged("), false);
  assert.doesNotMatch(source, /impactState\.outOfDate/, "no second, boolean state");
  assert.equal(functionBody(source, "function artifactStates(").includes("impact"), false);
});

test("the Impact view: registered after Resources, available with a project, the folder rules", () => {
  const extension = readSource("src/extension.ts");
  assert.ok(
    extension.indexOf("registerResourceView(context);") <
      extension.indexOf("registerImpactView(context);"),
  );
  const source = withoutCommentLines(readSource("src/commands.ts"));
  assert.match(
    functionBody(source, "async function updateImpactAvailable("),
    /"swcraft\.impact\.available",\s*traceProjectDir\(\) !== undefined/,
  );
  assert.match(source, /onDidChangeWorkspaceFolders\(impactFoldersChanged\)/);
  // What a folder change drops is impactCore's impactAfterFolderChange, tested there
  // (a failed first run of another folder included); this half applies it and redraws.
  const changed = functionBody(source, "function impactFoldersChanged(");
  assert.match(
    changed,
    /const next = impactAfterFolderChange\(impactState, getWorkspaceRoot\(\)\);\s*if \(next !== impactState\) \{\s*impactState = next;\s*refreshImpactView\(\);\s*\}/,
  );
  const run = functionBody(source, "async function impactRun(");
  const moved = run.indexOf("if (resolved.workspaceRoot !== getWorkspaceRoot()) {");
  assert.ok(moved > run.indexOf("spawnCli("), "the folder is compared after the run");
  assert.ok(moved < run.indexOf("impactAfterRun("), "before the outcome is kept");
  assert.match(
    run.slice(moved),
    /^if \(resolved\.workspaceRoot !== getWorkspaceRoot\(\)\) \{\s*recordCliOutcome\(commandId, label, run\);\s*return false;\s*\}/,
    "a run whose folder changed is shown nowhere and not revealed",
  );
  // The reveal awaits the view's `when` before its focus.
  assert.match(
    functionBody(source, "async function revealImpact("),
    /await updateImpactAvailable\(\);\s*await vscode\.commands\.executeCommand\(`\$\{IMPACT_VIEW_ID\}\.focus`\);/,
  );
});

// --- `swcraft import component`: argv, question, tab texts, the flow's shape -

const {
  buildImportComponentArgv,
  COMPONENT_IMPORT_REPORT_TITLE,
  COMPONENT_UPDATE_OFFER_LINE,
  componentDryRunOutcome,
  importAppliedOutcome,
  IMPORT_INCOMPLETE_LINE,
  importRefusalReport,
  IMPORT_UPDATE_BUTTON,
  NOTHING_WRITTEN_LINE,
  SHOW_UPDATE_BUTTON,
  UPDATE_RESOLVES_EXIT,
} = commandCore;

const COMPONENT_SOURCE = path.resolve(os.tmpdir(), "SpeedSensor.arxml");
const COMPONENT_EXECUTABLE = "/opt/swcraft/bin/swcraft";

/** A dry run's report as the CLI prints it (`component_exchange/report.py`). */
const componentReport = (action: string): string =>
  [
    `Component import report: ${COMPONENT_SOURCE}`,
    "(DRY RUN -- no files were written)",
    `SWC SpeedSensor: ${action}`,
    "",
    "CREATED interfaces:",
    "  ISpeed",
    "",
  ].join("\n");

const DIVERGES_STDERR =
  "Error: component import aborted with nothing written: SWC 'SpeedSensor' already exists in the target project and diverges (pass --update to replace the component's own definition; ACCESS LISTS regime: carried)\n";

const captured = (
  exitCode: number | null,
  stdout: string,
  stderr: string,
  spawnFailed = false,
): commandCore.CapturedRun => ({ exitCode, stdout, stderr, spawnFailed });

test("buildImportComponentArgv: the first dry run, its applied run, the update dry run and its applied run", () => {
  const base = ["import", "component", `--source=${COMPONENT_SOURCE}`, "--project", "swcraft.yaml"];
  const firstDry = buildImportComponentArgv("swcraft.yaml", COMPONENT_SOURCE, {
    dryRun: true,
    update: false,
  });
  const firstApplied = buildImportComponentArgv("swcraft.yaml", COMPONENT_SOURCE, {
    dryRun: false,
    update: false,
  });
  const updateDry = buildImportComponentArgv("swcraft.yaml", COMPONENT_SOURCE, {
    dryRun: true,
    update: true,
  });
  const updateApplied = buildImportComponentArgv("swcraft.yaml", COMPONENT_SOURCE, {
    dryRun: false,
    update: true,
  });
  assert.deepEqual(firstDry, [...base, "--dry-run"]);
  assert.deepEqual(firstApplied, base);
  assert.deepEqual(updateDry, [...base, "--dry-run", "--update"]);
  assert.deepEqual(updateApplied, [...base, "--update"]);
  // Each applied argv is its dry run's minus `--dry-run`, nothing else.
  for (const [dry, applied] of [
    [firstDry, firstApplied],
    [updateDry, updateApplied],
  ]) {
    assert.deepEqual(
      dry.filter((token) => token !== "--dry-run"),
      applied,
    );
  }
  // `--update` only in the update pair; `--replace-access-lists` in none.
  assert.ok(!firstDry.includes("--update") && !firstApplied.includes("--update"));
  for (const argv of [firstDry, firstApplied, updateDry, updateApplied]) {
    assert.ok(!argv.includes("--replace-access-lists"));
    assert.ok(!argv.includes("--source"), "the file is one `=` token");
  }
});

test("buildImportComponentArgv: a relative path throws", () => {
  for (const file of ["SpeedSensor.arxml", path.join("exports", "SpeedSensor.arxml")]) {
    for (const update of [false, true]) {
      assert.throws(
        () => buildImportComponentArgv("swcraft.yaml", file, { dryRun: true, update }),
        /must be an absolute path/,
      );
    }
  }
});

test("the component import's constants", () => {
  assert.equal(COMPONENT_IMPORT_REPORT_TITLE, "Component Import Report");
  assert.equal(SHOW_UPDATE_BUTTON, "Show Update");
  assert.equal(IMPORT_UPDATE_BUTTON, "Update");
  assert.equal(IMPORT_APPLY_BUTTON, "Write");
  assert.equal(IMPORT_CANCEL_BUTTON, "Cancel");
  // The tab's button sets carry these labels; that a click runs only its own
  // action, failing closed, is `acceptImportTabMessage()`'s (importTabCore.test.ts).
  assert.deepEqual(
    applyButtons(true).map((button) => button.label),
    [IMPORT_UPDATE_BUTTON, IMPORT_CANCEL_BUTTON],
  );
});

test("UPDATE_RESOLVES_EXIT equals the CLI's constant", () => {
  const cli = readCliPackageSource();
  const declared = cli.match(/^UPDATE_RESOLVES_EXIT = (\d+)$/m);
  assert.ok(declared !== null, "the cli package declares UPDATE_RESOLVES_EXIT");
  assert.equal(UPDATE_RESOLVES_EXIT, Number(declared[1]));
  assert.match(cli, /sys\.exit\(UPDATE_RESOLVES_EXIT if resolves else 1\)/);
});

test("componentDryRunOutcome: exit 0 is the report, with no line of the extension's", () => {
  const outcome = componentDryRunOutcome(
    captured(0, componentReport("created"), ""),
    COMPONENT_EXECUTABLE,
    false,
  );
  assert.equal(outcome.kind, "report");
  assert.deepEqual(outcome.report, {
    cliText: componentReport("created"),
    notes: [],
  });
  // The update dry run's report reads the same way.
  const update = componentDryRunOutcome(
    captured(0, componentReport("updated"), ""),
    COMPONENT_EXECUTABLE,
    true,
  );
  assert.equal(update.kind, "report");
  assert.deepEqual(update.report.notes, []);
});

test("componentDryRunOutcome: exit 3 without --update offers the update, with its stderr kept", () => {
  const outcome = componentDryRunOutcome(
    captured(UPDATE_RESOLVES_EXIT, "", DIVERGES_STDERR),
    COMPONENT_EXECUTABLE,
    false,
  );
  assert.deepEqual(outcome, {
    kind: "updateResolves",
    report: { cliText: DIVERGES_STDERR, notes: [COMPONENT_UPDATE_OFFER_LINE] },
  });
  assert.doesNotMatch(outcome.report.notes.join("\n"), /Nothing was written/);
});

test("componentDryRunOutcome: exit 1 offers no update; an exit 3 of the update dry run is a refusal", () => {
  const stderr = [
    "Error: component import aborted with nothing written: SWC 'SpeedSensor' already exists in the target project and diverges (…); interface 'ISpeed' diverges",
    "  a second line the CLI grouped under it",
    "",
  ].join("\n");
  for (const [exitCode, update] of [
    [1, false],
    [1, true],
    [UPDATE_RESOLVES_EXIT, true],
    [2, false],
  ] as const) {
    const outcome = componentDryRunOutcome(
      captured(exitCode, "", stderr),
      COMPONENT_EXECUTABLE,
      update,
    );
    assert.equal(outcome.kind, "refused", `exit ${exitCode}, update ${update}`);
    assert.deepEqual(outcome.report, importRefusalReport(stderr));
  }
  // Every stderr line kept, `Error: ` included, then the last line.
  assert.deepEqual(importRefusalReport(stderr), { cliText: stderr, notes: [NOTHING_WRITTEN_LINE] });
  // The tab: every line kept, then *Nothing was written.* as its last element, and no button.
  const page = importTabHtml(
    {
      title: "Component Import Report",
      run: "Dry run",
      file: COMPONENT_SOURCE,
      report: importRefusalReport(stderr),
      buttons: [],
    },
    { nonce: "abc123", rendering: { kind: "plain" } },
  );
  for (const line of stderr.trimEnd().split("\n")) {
    assert.ok(page.includes(escapeHtml(line)), `the report keeps '${line}'`);
  }
  assert.ok(page.includes("<p>Nothing was written.</p>\n</body>"), page);
  assert.doesNotMatch(page, /<button/);
  // The Markdown the tab renders: the stderr fenced, every line kept, then the line, last.
  const markdown = importReportMarkdown(importRefusalReport(stderr));
  assert.ok(markdown.startsWith(`\`\`\`\n${stderr.trimEnd()}\n\`\`\`\n\n`), markdown);
  assert.ok(markdown.endsWith("\n\nNothing was written\\.\n"), markdown);
  assert.equal(NOTHING_WRITTEN_LINE, "Nothing was written.");
});

test("componentDryRunOutcome: a spawn failure and a silent signal give the extension's own sentence", () => {
  const spawn = componentDryRunOutcome(captured(null, "", "", true), COMPONENT_EXECUTABLE, false);
  assert.deepEqual(spawn, {
    kind: "refused",
    report: {
      cliText: "",
      notes: [
        `The SWCraft CLI could not be started (${COMPONENT_EXECUTABLE}).`,
        "Nothing was written.",
      ],
    },
  });
  const signal = componentDryRunOutcome(captured(null, "", ""), COMPONENT_EXECUTABLE, true);
  assert.deepEqual(signal, {
    kind: "refused",
    report: {
      cliText: "",
      notes: ["The import stopped without a word (stopped by a signal).", "Nothing was written."],
    },
  });
  // A spawn failure that reports exit 3 is still the spawn failure, never the offer.
  assert.equal(
    componentDryRunOutcome(
      captured(UPDATE_RESOLVES_EXIT, "", "", true),
      COMPONENT_EXECUTABLE,
      false,
    ).kind,
    "refused",
  );
});

test("importAppliedOutcome: exit 0 is stdout alone; a failure never says nothing was written", () => {
  const report = componentReport("created").replace("(DRY RUN -- no files were written)\n", "");
  assert.deepEqual(
    importAppliedOutcome(captured(0, report, ""), COMPONENT_EXECUTABLE, IMPORT_RUN_WORDING),
    {
      kind: "imported",
      report: { cliText: report, notes: [] },
    },
  );
  for (const run of [
    captured(1, "", "Error: component import aborted with nothing written: …\n"),
    captured(UPDATE_RESOLVES_EXIT, "", DIVERGES_STDERR),
    captured(null, "", "", true),
    captured(null, "", ""),
    captured(null, "", "Traceback (most recent call last):\n  …\n"),
  ]) {
    const outcome = importAppliedOutcome(run, COMPONENT_EXECUTABLE, IMPORT_RUN_WORDING);
    assert.equal(outcome.kind, "failed");
    assert.equal(outcome.report.notes.at(-1), IMPORT_INCOMPLETE_LINE);
    const page = importTabHtml(
      {
        title: "Component Import Report",
        run: "Import",
        file: COMPONENT_SOURCE,
        report: outcome.report,
        buttons: [],
      },
      { nonce: "abc123", rendering: { kind: "plain" } },
    );
    assert.doesNotMatch(page, /Nothing was written/);
    assert.ok(page.includes(`<p>${IMPORT_INCOMPLETE_LINE}</p>\n</body>`), page);
    assert.doesNotMatch(page, /<button/, "a failed applied run offers nothing");
    const markdown = importReportMarkdown(outcome.report);
    assert.doesNotMatch(markdown, /Nothing was written/);
    assert.ok(markdown.endsWith(`${escapeMarkdown(IMPORT_INCOMPLETE_LINE)}\n`), markdown);
  }
  assert.deepEqual(
    importAppliedOutcome(captured(null, "", "", true), COMPONENT_EXECUTABLE, IMPORT_RUN_WORDING)
      .report.notes,
    ["The SWCraft CLI could not be started (/opt/swcraft/bin/swcraft).", IMPORT_INCOMPLETE_LINE],
  );
  assert.deepEqual(
    importAppliedOutcome(captured(null, "", ""), COMPONENT_EXECUTABLE, IMPORT_RUN_WORDING).report
      .notes,
    ["The import stopped without a word (stopped by a signal).", IMPORT_INCOMPLETE_LINE],
  );
  assert.equal(
    IMPORT_INCOMPLETE_LINE,
    "The import did not complete. Check what it changed with Change Report or git status.",
  );
});

test("importAppliedOutcome: an import applied before its report failed says so, for either import", () => {
  // cli/import_commands.py's two sentences after an applied run, the same for both imports
  // (`_import_done()`): the report could not be rendered, or its --report file
  // could not be written. The tab's closing line agrees with the CLI's.
  const cli = readCliPackageSource();
  assert.match(
    cli,
    /return "the dry run completed" if args\.dry_run else "the import was applied"/,
  );
  assert.match(cli, /f"Error: \{_import_done\(args\)\}, but its report could not be rendered/);
  // `write_report_file()`'s sentence, its run named by `done` (Trace Apply's) or `_import_done()`.
  assert.match(cli, /f"Error: \{done\} and its report printed, but --report/);
  // Both imports name their run by `_import_done()`.
  assert.equal(
    cli.match(/write_report_file\(args, text, markdown, _import_done\(args\)\)/g)?.length,
    2,
  );
  assert.equal(
    commandCore.IMPORT_REPORT_FAILED_LINE,
    "The import was applied, but its report could not be shown. Check what it changed with Change Report or git status.",
  );
  for (const stderr of [
    "Error: the import was applied, but its report could not be rendered (boom).\n",
    "Error: the import was applied and its report printed, but --report /t/r.md could not be written (denied).\r\n",
    "Warning: a line first\nError: the import was applied, but its report could not be rendered (x).\n",
  ]) {
    const outcome = importAppliedOutcome(
      captured(1, "", stderr),
      COMPONENT_EXECUTABLE,
      IMPORT_RUN_WORDING,
    );
    assert.equal(outcome.kind, "failed", "no Check Model button");
    assert.deepEqual(outcome.report.notes, [commandCore.IMPORT_REPORT_FAILED_LINE]);
    assert.equal(outcome.report.cliText, stderr);
  }
  // Any other failure, a dry run's sentence and the sentence not at a line's start keep the incomplete line.
  for (const stderr of [
    "Error: the dry run completed, but its report could not be rendered (x).\n",
    "Error: requirement R1: the import was applied? no\n",
    "Error: component import aborted with nothing written: …\n",
  ]) {
    assert.deepEqual(
      importAppliedOutcome(captured(1, "", stderr), COMPONENT_EXECUTABLE, IMPORT_RUN_WORDING).report
        .notes,
      [IMPORT_INCOMPLETE_LINE],
    );
  }
});

test("importComponentCommand is registered, labelled and placed under Export Component", () => {
  const source = withoutCommentLines(readSource("src/commands.ts"));
  assert.match(
    source,
    /vscode\.commands\.registerCommand\("swcraft\.exportComponent", \(\) =>\s*exportComponentCommand\(outputChannel\),\s*\),\s*vscode\.commands\.registerCommand\("swcraft\.importComponent", \(\) =>\s*importComponentCommand\(outputChannel\),\s*\),/,
  );
  assert.equal(SPAWNING_COMMAND_LABELS["swcraft.importComponent"], "Import Component");
});

// --- The import report's Markdown file (the CLI writes it, the tab renders it) -
//
// Both imports pass `--report=<file>.md` and the CLI writes the report's
// Markdown form there; the extension reads it, renders it and deletes it. It
// never turns the CLI's text into Markdown itself.

const REPORT_FILE = path.join(os.tmpdir(), "swcraft-import-x", "import-report.md");

test("the import argvs carry --report=<file.md> as one token on the dry run and on the applied run", () => {
  const requirements = (dryRun: boolean) =>
    commandCore.buildImportRequirementsArgv("swcraft.yaml", IMPORT_SOURCE_FOR_REPORT, {
      dryRun,
      report: REPORT_FILE,
    });
  assert.deepEqual(requirements(true).slice(-2), ["--dry-run", `--report=${REPORT_FILE}`]);
  assert.deepEqual(requirements(false).at(-1), `--report=${REPORT_FILE}`);
  assert.ok(!requirements(false).includes("--dry-run"));
  for (const dryRun of [true, false]) {
    for (const update of [true, false]) {
      const argv = commandCore.buildImportComponentArgv(
        "swcraft.yaml",
        COMPONENT_SOURCE_FOR_REPORT,
        {
          dryRun,
          update,
          report: REPORT_FILE,
        },
      );
      assert.equal(argv.at(-1), `--report=${REPORT_FILE}`);
      assert.equal(argv.filter((token) => token.startsWith("--report")).length, 1);
      assert.ok(!argv.includes("--replace-access-lists"));
    }
  }
  // Without a report file, the argv is what it was.
  assert.ok(
    !commandCore
      .buildImportComponentArgv("swcraft.yaml", COMPONENT_SOURCE_FOR_REPORT, {
        dryRun: true,
        update: false,
      })
      .some((token) => token.startsWith("--report")),
  );
});

test("importReportOption: a relative or a non-.md report path throws", () => {
  assert.equal(commandCore.importReportOption("/t/R.MD"), "--report=/t/R.MD");
  for (const bad of [
    "report.md",
    path.join("t", "report.md"),
    path.join(os.tmpdir(), "report.txt"),
  ]) {
    assert.throws(() => commandCore.importReportOption(bad), /absolute \.md path/);
    assert.throws(
      () =>
        commandCore.buildImportRequirementsArgv("swcraft.yaml", IMPORT_SOURCE_FOR_REPORT, {
          dryRun: true,
          report: bad,
        }),
      /absolute \.md path/,
    );
  }
});

const IMPORT_SOURCE_FOR_REPORT = path.resolve("exports", "needs.json");
const COMPONENT_SOURCE_FOR_REPORT = path.resolve("exports", "SpeedSensor.arxml");

/** A scratch workspace folder and a temporary root outside it, removed by the caller. */
function reportFileScratch(): { workspace: string; tmpRoot: string; cleanup: () => void } {
  const base = fs.mkdtempSync(path.join(os.tmpdir(), "swcraft-report-test-"));
  const workspace = path.join(base, "workspace");
  const tmpRoot = path.join(base, "tmp");
  fs.mkdirSync(workspace);
  fs.mkdirSync(tmpRoot);
  return { workspace, tmpRoot, cleanup: () => fs.rmSync(base, { recursive: true, force: true }) };
}

const MARKDOWN_REPORT = "# Component import report\\: S\\.arxml\n\n**SWC S\\: created**\n";

test("withImportReportFile: the run gets an absolute .md path outside the workspace, its file is read and then deleted", async () => {
  const { workspace, tmpRoot, cleanup } = reportFileScratch();
  try {
    let seen: string | undefined;
    const result = await commandCore.withImportReportFile(
      workspace,
      async (report) => {
        seen = report;
        assert.ok(report !== undefined);
        fs.writeFileSync(report, MARKDOWN_REPORT, { encoding: "utf8" });
        return captured(0, "Component import report: S.arxml\n", "");
      },
      fs,
      { tmpRoot },
    );
    assert.ok(seen !== undefined && path.isAbsolute(seen) && seen.endsWith(".md"));
    assert.equal(path.basename(seen), commandCore.IMPORT_REPORT_FILE_NAME);
    assert.ok(
      commandCore.liesOutside(seen, workspace, fs.realpathSync),
      "never under the workspace folder",
    );
    assert.ok(seen.startsWith(tmpRoot + path.sep));
    assert.equal(result.reportMarkdown, MARKDOWN_REPORT);
    assert.equal(fs.existsSync(path.dirname(seen)), false, "the run's directory is removed");
    assert.deepEqual(fs.readdirSync(tmpRoot), []);
    assert.deepEqual(fs.readdirSync(workspace), [], "nothing goes into the project");
  } finally {
    cleanup();
  }
});

test("withImportReportFile: a fresh directory per run, with the OS temporary directory by default", async () => {
  const { workspace, cleanup } = reportFileScratch();
  try {
    const seen: string[] = [];
    for (let index = 0; index < 2; index += 1) {
      await commandCore.withImportReportFile(
        workspace,
        async (report) => {
          assert.ok(report !== undefined);
          seen.push(report);
          return captured(0, "", "");
        },
        fs,
      );
    }
    assert.notEqual(path.dirname(seen[0]), path.dirname(seen[1]));
    for (const report of seen) {
      assert.ok(
        commandCore.liesOutside(report, os.tmpdir(), fs.realpathSync) === false,
        "under the OS temporary directory",
      );
      assert.equal(fs.existsSync(path.dirname(report)), false);
    }
  } finally {
    cleanup();
  }
});

test("withImportReportFile: the file is deleted after a failed run and after a run that throws", async () => {
  const { workspace, tmpRoot, cleanup } = reportFileScratch();
  try {
    const failed = await commandCore.withImportReportFile(
      workspace,
      async (report) => {
        assert.ok(report !== undefined);
        fs.writeFileSync(report, MARKDOWN_REPORT, { encoding: "utf8" });
        return captured(1, "", "Error: refused\n");
      },
      fs,
      { tmpRoot },
    );
    assert.deepEqual(fs.readdirSync(tmpRoot), []);
    // A failed run's file, had one been written, is not the report: the outcome ignores it.
    const outcome = commandCore.importAppliedOutcome(failed, "/s", commandCore.IMPORT_RUN_WORDING);
    assert.equal(outcome.kind, "failed");
    assert.equal("markdown" in outcome.report, false);

    await assert.rejects(
      commandCore.withImportReportFile(
        workspace,
        async (report) => {
          assert.ok(report !== undefined);
          fs.writeFileSync(report, MARKDOWN_REPORT, { encoding: "utf8" });
          throw new Error("boom");
        },
        fs,
        { tmpRoot },
      ),
      /boom/,
    );
    assert.deepEqual(fs.readdirSync(tmpRoot), [], "removed in the finally");
  } finally {
    cleanup();
  }
});

test("withImportReportFile: a run that wrote no file has no reportMarkdown, and its report is the fenced stdout", async () => {
  const { workspace, tmpRoot, cleanup } = reportFileScratch();
  try {
    const stdout = "Component import report: S.arxml\nSWC S: created\n";
    const result = await commandCore.withImportReportFile(
      workspace,
      async () => captured(0, stdout, ""),
      fs,
      { tmpRoot },
    );
    assert.equal("reportMarkdown" in result, false);
    const outcome = commandCore.importDryRunOutcome(result, "/s", commandCore.IMPORT_RUN_WORDING);
    assert.equal(outcome.kind, "report");
    assert.equal("markdown" in outcome.report, false);
    assert.ok(importReportMarkdown(outcome.report).startsWith(`\`\`\`\n${stdout}\`\`\``));
    assert.deepEqual(fs.readdirSync(tmpRoot), []);
  } finally {
    cleanup();
  }
});

test("withImportReportFile: a temporary directory inside the workspace folder is never used", async () => {
  const { workspace, cleanup } = reportFileScratch();
  try {
    const lines: string[] = [];
    let seen: string | undefined = "unset";
    await commandCore.withImportReportFile(
      workspace,
      async (report) => {
        seen = report;
        return captured(0, "", "");
      },
      fs,
      { tmpRoot: workspace, log: (line) => lines.push(line) },
    );
    assert.equal(seen, undefined, "the run goes ahead without --report");
    assert.deepEqual(fs.readdirSync(workspace), [], "its directory is removed again");
    assert.match(lines.join("\n"), /lies in the workspace folder/);
  } finally {
    cleanup();
  }
});

test("withImportReportFile: no temporary directory, the run goes ahead without --report and says so", async () => {
  const { workspace, tmpRoot, cleanup } = reportFileScratch();
  try {
    const lines: string[] = [];
    let seen: string | undefined = "unset";
    await commandCore.withImportReportFile(
      workspace,
      async (report) => {
        seen = report;
        return captured(0, "", "");
      },
      fs,
      { tmpRoot: path.join(tmpRoot, "absent"), log: (line) => lines.push(line) },
    );
    assert.equal(seen, undefined);
    assert.match(lines.join("\n"), /no temporary directory for the import report/);
  } finally {
    cleanup();
  }
});

test("withImportReportFile: a file that does not open with a Markdown report's heading is no Markdown", async () => {
  // An older toolchain writes its TEXT report to any --report path, .md included:
  // the run behaves as one that wrote no file, so the tab shows the fenced stdout.
  const { workspace, tmpRoot, cleanup } = reportFileScratch();
  try {
    const run = (content: string) =>
      commandCore.withImportReportFile(
        workspace,
        async (report) => {
          assert.ok(report !== undefined);
          fs.writeFileSync(report, content, { encoding: "utf8" });
          return captured(0, content, "");
        },
        fs,
        { tmpRoot },
      );
    const text = "Component import report: S.arxml\nSWC S: created\n";
    assert.equal("reportMarkdown" in (await run(text)), false);
    assert.equal("reportMarkdown" in (await run("Requirements import: baseline v1\n")), false);
    assert.equal("reportMarkdown" in (await run("")), false);
    const requirements = "# Requirements import\\: baseline v1\n";
    assert.equal((await run(requirements)).reportMarkdown, requirements);
    assert.equal((await run(MARKDOWN_REPORT)).reportMarkdown, MARKDOWN_REPORT);
    assert.deepEqual(fs.readdirSync(tmpRoot), []);
  } finally {
    cleanup();
  }
});

test("withImportReportFile: a temporary root that is a link into the workspace folder is never used", async () => {
  // The realpath leg of liesOutside(): lexically the directory is outside the
  // workspace, but its real path is inside it.
  const { workspace, tmpRoot, cleanup } = reportFileScratch();
  try {
    const link = path.join(tmpRoot, "into-workspace");
    fs.symlinkSync(workspace, link, "dir");
    assert.equal(commandCore.liesOutside(link, workspace, fs.realpathSync), false);
    const lines: string[] = [];
    let seen: string | undefined = "unset";
    await commandCore.withImportReportFile(
      workspace,
      async (report) => {
        seen = report;
        return captured(0, "", "");
      },
      fs,
      { tmpRoot: link, log: (line) => lines.push(line) },
    );
    assert.equal(seen, undefined, "the run goes ahead without --report");
    assert.deepEqual(fs.readdirSync(workspace), [], "its directory is removed again");
    assert.match(lines.join("\n"), /lies in the workspace folder/);
  } finally {
    cleanup();
  }
});

test("withImportReportFile: a directory that cannot be removed is a log line, and the run's result stands", {
  skip: process.platform === "win32" || process.getuid?.() === 0,
}, async () => {
  // POSIX only: a read-only parent refuses the removal (Windows ignores the mode
  // bits on a directory, and root ignores them everywhere).
  const { workspace, tmpRoot, cleanup } = reportFileScratch();
  try {
    const lines: string[] = [];
    const result = await commandCore.withImportReportFile(
      workspace,
      async (report) => {
        assert.ok(report !== undefined);
        fs.writeFileSync(report, MARKDOWN_REPORT, { encoding: "utf8" });
        fs.chmodSync(tmpRoot, 0o500);
        return captured(0, "", "");
      },
      fs,
      { tmpRoot, log: (line) => lines.push(line) },
    );
    assert.equal(result.reportMarkdown, MARKDOWN_REPORT);
    assert.match(
      lines.join("\n"),
      /the import report's temporary directory could not be removed \(/,
    );
  } finally {
    fs.chmodSync(tmpRoot, 0o700);
    cleanup();
  }
});

test("the Markdown headings are the CLI's, and the component and Trace Apply ones are the tab's", () => {
  assert.deepEqual(commandCore.IMPORT_MARKDOWN_HEADINGS, [
    "# Component import report\\: ",
    "# Requirements import\\: ",
    "# Trace Apply Report — ",
  ]);
  assert.ok(commandCore.IMPORT_MARKDOWN_HEADINGS.includes(commandCore.COMPONENT_MARKDOWN_HEADING));
  assert.ok(
    commandCore.IMPORT_MARKDOWN_HEADINGS.includes(commandCore.TRACE_APPLY_MARKDOWN_HEADING),
  );
});

test("escapeMarkdown escapes an ampersand, so an entity-like run is not decoded", () => {
  assert.equal(commandCore.escapeMarkdown("A &amp; B"), "A \\&amp; B");
  assert.equal(commandCore.escapeMarkdown("a_b"), "a\\_b");
});

test("liesOutside: the folder itself and anything below it are inside; a sibling is outside", () => {
  const folder = path.resolve("ws");
  assert.equal(commandCore.liesOutside(folder, folder, fs.realpathSync), false);
  assert.equal(
    commandCore.liesOutside(path.join(folder, "a", "b.md"), folder, fs.realpathSync),
    false,
  );
  assert.equal(commandCore.liesOutside(path.resolve("ws2", "b.md"), folder, fs.realpathSync), true);
  assert.equal(commandCore.liesOutside(path.resolve("b.md"), folder, fs.realpathSync), true);
});

test("liesOutside: a child whose name starts with `..` is inside; the parent itself is outside", () => {
  const folder = path.resolve("project");
  for (const child of ["..tmp", path.join("..tmp", "r.md"), path.join("...", "r.md"), "..r.md"]) {
    assert.equal(
      commandCore.liesOutside(path.join(folder, child), folder, fs.realpathSync),
      false,
      child,
    );
  }
  assert.equal(commandCore.liesOutside(path.dirname(folder), folder, fs.realpathSync), true);
  assert.equal(
    commandCore.liesOutside(path.resolve("..tmp", "r.md"), folder, fs.realpathSync),
    true,
  );
});

test("the outcomes carry the CLI's Markdown report only for a run that exited 0", () => {
  const withFile = (exitCode: number | null, stdout: string, stderr: string) => ({
    ...captured(exitCode, stdout, stderr),
    reportMarkdown: MARKDOWN_REPORT,
  });
  const dry = commandCore.componentDryRunOutcome(withFile(0, "SWC S: created\n", ""), "/s", false);
  assert.equal(dry.kind, "report");
  assert.equal(dry.report.markdown, MARKDOWN_REPORT);
  assert.equal(
    commandCore.importDryRunOutcome(
      withFile(0, "Summary: x\n", ""),
      "/s",
      commandCore.IMPORT_RUN_WORDING,
    ).report.markdown,
    MARKDOWN_REPORT,
  );
  assert.equal(
    commandCore.importAppliedOutcome(withFile(0, "", ""), "/s", commandCore.IMPORT_RUN_WORDING)
      .report.markdown,
    MARKDOWN_REPORT,
  );
  for (const outcome of [
    commandCore.importDryRunOutcome(
      withFile(1, "", "Error: x\n"),
      "/s",
      commandCore.IMPORT_RUN_WORDING,
    ),
    commandCore.componentDryRunOutcome(withFile(3, "", DIVERGES_STDERR), "/s", false),
    commandCore.importAppliedOutcome(withFile(null, "", ""), "/s", commandCore.IMPORT_RUN_WORDING),
  ]) {
    assert.equal("markdown" in outcome.report, false);
  }
});

// --- the diagrams' tab — the adapter half --------------------
//
// `DiagramsTab` and the model route live in commands.ts, which imports "vscode"; every decision
// they carry out is diagramsTab.ts's or helpLinks.ts's, unit-tested there. These scans hold the
// wiring. What they do NOT reach: the panel as VS Code draws it, and the order in which a real
// webview receives what is posted.

test("the diagrams' tab is one webview panel per window with the panel's options", () => {
  const source = withoutCommentLines(readSource("src/commands.ts"));
  assert.equal(
    (source.match(/createWebviewPanel\(\s*DIAGRAMS_TAB_VIEW_TYPE/g) ?? []).length,
    1,
    "one place creates the diagrams' panel",
  );
  const ensure = functionBody(source, "private ensurePanel(): vscode.WebviewPanel {");
  assert.match(
    ensure,
    /if \(this\.panel !== undefined\) \{\s*return this\.panel;/,
    "reused while open",
  );
  for (const option of [
    "enableScripts: true",
    "localResourceRoots: []",
    "enableCommandUris: false",
    "enableFindWidget: false",
    "retainContextWhenHidden: true",
  ]) {
    assert.ok(ensure.includes(option), option);
  }
  assert.match(
    ensure,
    /onDidDispose\(\(\) => \{\s*this\.panel = undefined;\s*this\.state\.disposed\(\);/,
  );
  // A panel moved into the files' column forgets it.
  assert.match(
    ensure,
    /panel\.onDidChangeViewState\(\(\) => this\.state\.panelMovedTo\(panel\.viewColumn\)\);/,
  );
  assert.match(source, /^const diagramsTab = new DiagramsTab\(\);$/m, "one tab for the window");
});

test("the diagrams' tab carries out planShow's steps in its order: a reveal posts after the panel is revealed", () => {
  const source = withoutCommentLines(readSource("src/commands.ts"));
  const show = functionBody(source, "  show(\n    viewsRoot: string,");
  assert.match(
    show,
    /options: \{ reveal\?: RevealRequest; justGenerated\?: boolean \} = \{\},\s*\): TabShow \{\s*const \{ reveal, justGenerated = false \} = options;/,
  );
  // A refusal shown in the view's place is not a page: planShow renders there.
  assert.match(
    show,
    /const shown = this\.panel === undefined \? undefined : this\.state\.shownIn\(viewsRoot\);/,
  );
  assert.match(show, /for \(const step of planShow\(shown, \{ entry, reveal \}\)\)/);
  assert.match(
    show,
    /case "render":\s*this\.state\.visit\(viewsRoot, step\.entry\);\s*answer = this\.render\(step\.reveal, justGenerated\);/,
  );
  assert.match(show, /case "reveal-panel":\s*this\.panel\?\.reveal\(undefined, false\);/);
  assert.match(
    show,
    /case "post-reveal":\s*void this\.panel\?\.webview\.postMessage\(\{ type: "reveal", \.\.\.step\.reveal \}\);/,
  );
  assert.equal(
    /setTimeout/.test(show),
    false,
    "no timer (the prototype's 150 ms is not carried over)",
  );
});

test("Show in Diagram never returns silently past the manifest: every early return says why", () => {
  const source = withoutCommentLines(readSource("src/commands.ts"));
  const body = functionBody(
    source,
    "async function showInDiagramCommand(arg?: unknown): Promise<void> {",
  );
  assert.match(
    body,
    /if \(modelRoot === undefined\) \{\s*void vscode\.window\.showWarningMessage\(SHOW_IN_DIAGRAM_NO_MODEL_ROOT_LINE\);\s*return;/,
    "a manifest without a resolvable model_root is said",
  );
  const bare = [...body.matchAll(/\{\s*return;/g)].length;
  // The one bare return is the missing manifest (resolveManifestOrNotify reports it);
  // the other is a quick pick the user dismissed.
  assert.equal(bare, 2, "only the missing manifest and a dismissed pick return without a line");
  assert.match(body, /if \(resolved === undefined\) \{\s*return;/);
  assert.match(body, /if \(view === undefined\) \{\s*return;/);
});

test("the diagrams' tab reads a page only through resolveViewsFile, and sets only webviewDocument's or refusalDocument's text", () => {
  const source = withoutCommentLines(readSource("src/commands.ts"));
  const render = functionBody(
    source,
    "  private render(reveal: RevealRequest | null, justGenerated = false): TabShow {",
  );
  assert.match(render, /const file = resolveViewsFile\(viewsRoot, name\);/);
  assert.match(render, /fs\.readFileSync\(file\.path, \{ encoding: "utf8" \}\)/);
  assert.match(render, /panel\.webview\.html = document\.html;/);
  assert.match(
    render,
    /this\.state\.showedPage\(modelAllowlist\(parseModelBlock\(html\)\), outOfDate\);/,
  );
  // A page the press itself just generated and still another release's names the mismatch.
  assert.match(
    render,
    /const line = refusalLine\(document, justGenerated, this\.effects\?\.extensionVersion \?\? ""\);/,
  );
  const refuse = functionBody(
    source,
    "  private refuse(panel: vscode.WebviewPanel, nonce: string, line: string, detail: string): TabShow {",
  );
  assert.match(refuse, /this\.state\.showedRefusal\(\);/, "a refusal opens nothing");
  // An other-release refusal reads the hash file afresh once (a same-version .vsix over a running
  // copy), and the cache keeps it.
  assert.match(render, /webviewDocumentRereading\(/);
  assert.match(render, /\(\) => this\.readPageScriptHash\(\),/);
  assert.match(render, /this\.scriptHash = rendered\.scriptHash;/);
  assert.match(render, /if \(document === undefined\) \{\s*return noScriptHash\(\);/);
  assert.equal(/\bwebviewDocument\(/.test(source), false, "no rewrite without the re-read");
  const assignments = source.match(/\.webview\.html = /g) ?? [];
  assert.equal(
    source.match(/panel\.webview\.html = refusalDocument\(nonce, line\);/g)?.length,
    1,
    "the refusal's one line",
  );
  assert.ok(assignments.length >= 2);
  const open = functionBody(
    source,
    "  private async open(file: string, member: string | null): Promise<void> {",
  );
  assert.match(open, /acceptOpen\(file, member, this\.state\.allowlist, modelRoot\)/);
  const navigate = functionBody(source, "  private async navigate(href: string): Promise<void> {");
  // Both link checks before anything is shown or opened: a listed link out of views_root is ignored
  // with one line, never the missing-page refusal.
  assert.match(navigate, /followLink\(href, viewsRoot, listViewsRoot\(viewsRoot\)\)/);
  assert.equal(/acceptNavigate\(|resolveViewsFile\(/.test(navigate), false);
  assert.match(navigate, /executeCommand\("vscode\.open", vscode\.Uri\.file\(target\.path\)/);
});

test("the tab's Generate is the press regenerating always; the tracker's changes reach the shown page", () => {
  const source = withoutCommentLines(readSource("src/commands.ts"));
  assert.match(
    source,
    /generate: \(arg\) => void diagramsCommand\(outputChannel, arg, \{ regenerate: true \}\),/,
  );
  assert.match(source, /extensionVersion: extensionVersion\(context\),\s*\}\);/);
  assert.match(
    source,
    /outOfDate: \(\) => isOutOfDate\(artifactStates\(getWorkspaceRoot\(\)\)\.views\),/,
  );
  assert.match(
    functionBody(source, "function artifactsChanged(): void {"),
    /diagramsTab\.syncOutOfDate\(\);/,
  );
  const build = functionBody(source, "async function buildCommand(");
  assert.match(
    build,
    /if \(buildRunGenerations\(result\)\.views\) \{\s*diagramsTab\.reloadShown\(\);/,
    "a Build that generated the views re-reads the page shown",
  );
});

test("the URI handler dispatches on the link's path first: the model route before the guide's early return", () => {
  const source = withoutCommentLines(readSource("src/commands.ts"));
  assert.match(
    source,
    /handleUri: \(uri\) =>\s*uriRoute\(uri\) === "model"\s*\? handleModelUri\(uri, extensionId, outputChannel\)\s*: handleHelpUri\(uri, extensionId\),/,
  );
  const handler = functionBody(source, "async function handleModelUri(");
  assert.match(handler, /modelLinkAction\(uri, extensionId, modelRoot\)/);
  assert.equal(
    (handler.match(/show(Error|Warning|Information)Message\(/g) ?? []).length,
    1,
    "one notification, the action's own text",
  );
  assert.match(
    handler,
    /case "notify":\s*void vscode\.window\.showWarningMessage\(action\.message\);/,
  );
  assert.match(handler, /case "log":\s*channel\.appendLine\(action\.line\);/);
});

test("clientTableUnreadableMessage: says the table could not be read and that nothing was written", () => {
  const message = commandCore.clientTableUnreadableMessage(new Error("it is not JSON"));
  assert.match(message, /the bundled client table could not be read \(it is not JSON\)/);
  assert.match(message, /nothing was written/);
  assert.doesNotMatch(message, /while writing project files/);
  assert.equal(message.includes("\n"), false);
});

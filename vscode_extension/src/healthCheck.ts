// Pure logic: the `SWCraft: Check Editor Tooling` report.
//
// No `vscode` import, so it runs under plain `node --test`; the adapter
// (`commands.ts`) does all the reading and printing, every decision is here.
//
// Why it exists: with the gallery unreachable, `code --install-extension` on
// the .vsix exits 0, installs SWCraft alone and says nothing about the pack.
// This report is what tells the user.

import {
  compareReleases,
  type Platform,
  parseSwcraftVersion,
  type SwcraftResolution,
  samePath,
} from "./toolchain";

// --- The one sentence this report exists to keep true ------------------------

/**
 * Printed verbatim at the foot of every report, so "all present" is not read as
 * "the project is fine": `swcraft validate` is the sole authoritative model and
 * ARXML/XSD gate.
 */
export const NOT_VALIDATION_EVIDENCE =
  "A healthy pack is not validation evidence: no editor diagnostic from any pack member says " +
  "anything about the model or the generated ARXML. `swcraft validate` is the gate.";

// --- Version ranges ----------------------------------------------------------

/** Whether a host version satisfies a member's declared `engines.vscode`. */
export type FloorVerdict = "satisfied" | "below-floor" | "not-comparable";

interface ParsedVersion {
  major: number;
  minor: number;
  patch: number;
}

interface ParsedRange {
  major: number;
  majorMustEqual: boolean;
  minor: number;
  minorMustEqual: boolean;
  patch: number;
  patchMustEqual: boolean;
  isMinimum: boolean;
}

/**
 * VS Code's own `engines.vscode` grammar, read from the shipped 1.134.0 bundle
 * (`workbench.desktop.main.js`, `isValidVersion`/`normalizeVersion`/
 * `parseVersion`): `/^(\^|>=)?((\d+)|x)\.((\d+)|x)\.((\d+)|x)(\-.*)?$/`, plus `*`.
 * The `(-…)` tail, a `notBefore` build date this checker is not given, is not
 * accepted: such a range is `not-comparable`.
 */
const RANGE_PATTERN = /^(\^|>=)?((\d+)|x)\.((\d+)|x)\.((\d+)|x)$/;

/** `1.134.0`, or `1.134.0-insider` — the pre-release tail is dropped. */
const VERSION_PATTERN = /^(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$/;

function parseVersion(version: string): ParsedVersion | undefined {
  const match = VERSION_PATTERN.exec(version.trim());
  if (match === null) {
    return undefined;
  }
  return {
    major: Number(match[1]),
    minor: Number(match[2]),
    patch: Number(match[3]),
  };
}

/**
 * Parse and normalize a range exactly as VS Code does: a caret on a non-zero
 * major clears `minorMustEqual` and `patchMustEqual` (`^1.110.0` is "1.110.0 or
 * later, major 1"), while a bare `1.63.0` is an exact requirement, not a floor.
 */
function parseRange(range: string): ParsedRange | undefined {
  const trimmed = range.trim();
  if (trimmed === "*") {
    return {
      major: 0,
      majorMustEqual: false,
      minor: 0,
      minorMustEqual: false,
      patch: 0,
      patchMustEqual: false,
      isMinimum: false,
    };
  }
  const match = RANGE_PATTERN.exec(trimmed);
  if (match === null) {
    return undefined;
  }
  const hasCaret = match[1] === "^";
  const major = match[2] === "x" ? 0 : Number(match[2]);
  const parsed: ParsedRange = {
    major,
    majorMustEqual: match[2] !== "x",
    minor: match[4] === "x" ? 0 : Number(match[4]),
    minorMustEqual: match[4] !== "x",
    patch: match[6] === "x" ? 0 : Number(match[6]),
    patchMustEqual: match[6] !== "x",
    isMinimum: match[1] === ">=",
  };
  if (hasCaret) {
    if (major !== 0) {
      parsed.minorMustEqual = false;
    }
    parsed.patchMustEqual = false;
  }
  return parsed;
}

/**
 * Whether *hostVersion* satisfies *range*, following VS Code's `isValidVersion`
 * comparison (`RANGE_PATTERN`). `not-comparable`, never a guessed failure, for
 * an unparseable host version, an absent or unparseable range, or a `notBefore`
 * date form.
 *
 * What this does not reach: VS Code's build-date check and its `0.x` shim for
 * pre-1.0 versions; both lie outside the accepted grammar, so they answer
 * `not-comparable`.
 */
export function satisfiesEngineRange(hostVersion: string, range: string | undefined): FloorVerdict {
  if (range === undefined) {
    return "not-comparable";
  }
  const host = parseVersion(hostVersion);
  const wanted = parseRange(range);
  if (host === undefined || wanted === undefined) {
    return "not-comparable";
  }
  return compare(host, wanted) ? "satisfied" : "below-floor";
}

function compare(host: ParsedVersion, wanted: ParsedRange): boolean {
  if (wanted.isMinimum) {
    if (host.major !== wanted.major) {
      return host.major > wanted.major;
    }
    if (host.minor !== wanted.minor) {
      return host.minor > wanted.minor;
    }
    return host.patch >= wanted.patch;
  }
  if (host.major !== wanted.major) {
    return host.major > wanted.major ? !wanted.majorMustEqual : false;
  }
  if (host.minor !== wanted.minor) {
    return host.minor > wanted.minor ? !wanted.minorMustEqual : false;
  }
  if (host.patch !== wanted.patch) {
    return host.patch > wanted.patch ? !wanted.patchMustEqual : false;
  }
  return true;
}

// --- Pack members ------------------------------------------------------------

/** One extension the host reports as installed and enabled (`vscode.extensions.all`). */
export interface InstalledExtension {
  /** `publisher.name`, as VS Code reports it. Compared case-insensitively. */
  id: string;
  /** The member's own `packageJSON.version`, when the host reported one. */
  version?: string;
  /** The member's own `packageJSON.engines.vscode`, when it declares one. */
  engineRange?: string;
}

/**
 * The verdict for one declared pack member. `absent` is "not installed" or
 * "installed but disabled": VS Code's API (`@types/vscode` 1.125.0) drops a
 * disabled extension from `all` and cannot tell them apart. `version-incompatible`
 * is rare (VS Code disables an extension the host is too old for, which then
 * reads `absent`); `UNDETECTABLE` states the limit in the report.
 */
export type PackMemberStatus = "present" | "absent" | "version-incompatible";

export interface PackMemberHealth {
  /** The member's id exactly as `package.json`'s `extensionPack` declares it. */
  id: string;
  status: PackMemberStatus;
  /** One line, always naming *id* — the id is what an administrator mirrors or installs. */
  message: string;
}

/**
 * The health of every declared pack member, in the manifest's order.
 *
 * *declaredPack* is this extension's `packageJSON.extensionPack`: the pack is
 * declared in `package.json` alone, never as a second list here, which would go
 * stale silently. *installed* is enabled extensions only (`PackMemberStatus`).
 */
export function checkPackHealth(
  installed: readonly InstalledExtension[],
  declaredPack: readonly string[],
  vscodeVersion: string,
): PackMemberHealth[] {
  // Extension ids are case-insensitive in VS Code.
  const byId = new Map(installed.map((extension) => [extension.id.toLowerCase(), extension]));
  return declaredPack.map((id) => {
    const found = byId.get(id.toLowerCase());
    if (found === undefined) {
      return {
        id,
        status: "absent" as const,
        message:
          `${id} — not installed, or installed and disabled (VS Code's API cannot tell those ` +
          "apart). Install it from the Extensions view, or re-enable it there.",
      };
    }
    const version = found.version === undefined ? "" : ` ${found.version}`;
    const verdict = satisfiesEngineRange(vscodeVersion, found.engineRange);
    if (verdict === "below-floor") {
      return {
        id,
        status: "version-incompatible" as const,
        message:
          `${id}${version} requires VS Code ${found.engineRange}, and this host is ` +
          `${vscodeVersion}. It is installed but the host does not satisfy its floor.`,
      };
    }
    if (verdict === "not-comparable") {
      return {
        id,
        status: "present" as const,
        message:
          `${id}${version} — installed and enabled. Its engines.vscode ` +
          `(${found.engineRange ?? "not declared"}) could not be compared with this host ` +
          `(${vscodeVersion}), so the floor was not checked.`,
      };
    }
    return {
      id,
      status: "present" as const,
      message: `${id}${version} — installed and enabled.`,
    };
  });
}

// --- Runtime and schema resources -------------------------------------------

/** The health of one runtime or schema resource. */
export interface ResourceHealth {
  /** Short name of the resource, e.g. `SWCraft toolchain`. */
  label: string;
  /**
   * `missing` is the counted verdict, marked `[problem]`: absent, or unusable as
   * it stands. `warning` works with something worth saying beside it; it is
   * printed but not counted, so "no problems found" can still carry the line.
   */
  status: "ok" | "warning" | "missing";
  /** One line naming the concrete path or count that produced the verdict. */
  message: string;
}

// --- The workstation toolchain ----------------------------------------------

/** What running `<swcraft> --version` produced. */
export interface VersionProbe {
  /** Whether the process started and exited 0. */
  ok: boolean;
  /** Its stdout, whatever there was of it — quoted back when it cannot be read. */
  output: string;
}

/** Everything `checkToolchain()` decides from; the adapter does all the reading. */
export interface ToolchainInput {
  /** `toolchain.ts`'s answer: which executable, from where, and whether it is on disk. */
  resolution: SwcraftResolution;
  /** This extension's own `packageJSON.version` — the version the CLI must agree with. */
  extensionVersion: string;
  /**
   * The `--version` probe, or `undefined` when it was not run because the
   * executable is not there. `undefined` with an executable that *does* exist
   * is read as a probe that did not run — the same finding, never a pass.
   */
  version: VersionProbe | undefined;
  /** Where a bare `swcraft` resolves on this host, or `undefined` when it resolves nowhere. */
  pathResolution: string | undefined;
  platform: Platform;
}

/**
 * The health of the workstation installation every SWCraft command spawns.
 *
 * Six findings, the earliest wins (a shadowed `PATH` is not what to fix when the
 * executable is missing):
 *
 * 1. Not there. The repair is `SWCraft: Install Toolchain`, or the
 *    `swcraft.executable` setting when the path came from it.
 * 2. There but will not start: the venv's `pyvenv.cfg` names an absolute base
 *    interpreter that was upgraded or removed.
 * 3. Runs but prints no readable version: the first line is quoted; the repair
 *    is *Update Toolchain*.
 * 4. A different version: both are named.
 * 5. A bare `swcraft` resolves nowhere (warning). On macOS and Linux the
 *    installer only prints an `export PATH=…` line for a profile, so a healthy
 *    install shows this until it is added; on Windows the application predates
 *    the install.
 * 6. A bare `swcraft` resolves elsewhere (warning): the extension is unaffected,
 *    but a terminal or `moon` build in this window runs the other one. It reads
 *    the `PATH` VS Code was started with, never a venv activated in a terminal.
 *
 * Neither warning suggests a new window: `process.env` is fixed when the
 * application launches, so only restarting VS Code from a shell that has the
 * entry changes it.
 *
 * What it does not reach: whether the CLI works beyond `--version`
 * (`swcraft validate`'s question); two spellings of one file (a link, an 8.3
 * short name) read as a shadow, which is why case 6 is a warning naming both
 * paths; and on POSIX a profile line never added from one added after this
 * window opened.
 */
export function checkToolchain(input: ToolchainInput): ResourceHealth {
  const label = "SWCraft toolchain";
  const { executable, source } = input.resolution;
  if (!input.resolution.exists) {
    const repair =
      source === "setting"
        ? "The `swcraft.executable` setting names it — correct the setting, or clear it to use " +
          "the installation under your user profile."
        : "Run `SWCraft: Install Toolchain` and point it at the workstation archive.";
    return {
      label,
      status: "missing",
      message: `${label}: ${executable} does not exist — every SWCraft command spawns it, so all of them would fail. ${repair}`,
    };
  }
  if (input.version === undefined || !input.version.ok) {
    return {
      label,
      status: "missing",
      message:
        `${label}: ${executable} is there but \`swcraft --version\` did not run. The installation's ` +
        "venv names an absolute base interpreter in its pyvenv.cfg, so a Python that was upgraded, " +
        "moved or removed leaves exactly this. Run `SWCraft: Update Toolchain` with an archive to " +
        "rebuild it over the Python on this machine.",
    };
  }
  const reported = parseSwcraftVersion(input.version.output);
  if (reported === undefined) {
    const firstLine = input.version.output.split(/\r?\n/)[0]?.trim() ?? "";
    return {
      label,
      status: "missing",
      message:
        `${label}: ${executable} ran but printed no version this could read ` +
        `(${firstLine === "" ? "no output at all" : firstLine}), so it could not be compared with ` +
        `this extension's ${input.extensionVersion}. Run \`SWCraft: Update Toolchain\`.`,
    };
  }
  if (reported !== input.extensionVersion) {
    return {
      label,
      status: "missing",
      message:
        `${label}: ${executable} is version ${reported} and this extension is ` +
        `${input.extensionVersion} — they ship together and are meant to agree. Run ` +
        "`SWCraft: Update Toolchain` with the archive of the version you want, or install the " +
        "matching .vsix from it.",
    };
  }
  if (input.pathResolution === undefined) {
    const advice =
      input.platform === "windows"
        ? "A terminal opened before the install was done has the old environment; open a new one. " +
          "If this window itself predates the install, quit VS Code completely and start it again — " +
          "an integrated terminal inherits the environment the application was launched with, which " +
          "neither a new window nor Reload Window changes."
        : "On macOS and Linux the installer edits no shell file: it prints one `export PATH=" +
          '"<home>/current/bin:$PATH"` line instead. Add it to your shell profile and open a new ' +
          "terminal — or, if it is already there, quit VS Code completely and start it again from a " +
          "shell that has it, since the environment this window reads was fixed when the " +
          "application launched.";
    return {
      label,
      status: "warning",
      message:
        `${label}: ${executable} (${reported}) — but a bare \`swcraft\` resolves nowhere on the ` +
        "PATH this window was launched with. The extension does not need PATH (it spawns the " +
        "path above), while a `moon` build started from this window will not find swcraft at " +
        "all. An integrated terminal is an interactive shell that re-reads your profile, so it " +
        `may already find it and this reading cannot tell. ${advice}`,
    };
  }
  if (!samePath(input.pathResolution, executable, input.platform)) {
    return {
      label,
      status: "warning",
      message:
        `${label}: ${executable} (${reported}) — but a bare \`swcraft\` resolves to ` +
        `${input.pathResolution} in this window. The extension is unaffected (it spawns the path ` +
        "above), while a terminal or a moon build here runs the other one. This reads the PATH " +
        "VS Code was started with, so the cause is another SWCraft or another environment listed " +
        "ahead of the installation there; a venv activated inside the integrated terminal is not " +
        "something this can see either way.",
    };
  }
  return {
    label,
    status: "ok",
    message: `${label}: ${executable} (${reported}), and a bare \`swcraft\` resolves to it`,
  };
}

/**
 * The schema files `contributes.yamlValidation` requires this install to hold,
 * as basenames, deduplicated and sorted; the manifest is the only inventory.
 * *contributions* is the raw value: a missing block or malformed entries yield
 * an empty list rather than a throw. Only `./schemas/…` urls count.
 */
export function declaredSchemaFiles(contributions: unknown): string[] {
  if (!Array.isArray(contributions)) {
    return [];
  }
  const names = new Set<string>();
  for (const entry of contributions) {
    const url = (entry as { url?: unknown } | null)?.url;
    if (typeof url !== "string" || !url.startsWith("./schemas/")) {
      continue;
    }
    const basename = url.slice("./schemas/".length);
    if (basename !== "" && !basename.includes("/")) {
      names.add(basename);
    }
  }
  return [...names].sort();
}

/**
 * Whether each schema `contributes.yamlValidation` declares is in the installed
 * `schemas/` directory, by name (a count would pass a partial install). A
 * missing one silently removes completion, hover and diagnostics for its layer.
 *
 * *schemaFiles* is the directory's `*.schema.json` list, `undefined` when it
 * could not be read; *declared* is `declaredSchemaFiles()`'s answer.
 *
 * What it does not check: a present file's content (a truncated, stale or
 * hand-edited schema counts as there); `test_editor_schema_drift.py` checks
 * that at build time.
 */
export function checkBundledSchemas(
  schemaFiles: readonly string[] | undefined,
  directory: string,
  declared: readonly string[],
): ResourceHealth {
  const label = "Bundled JSON Schemas";
  if (schemaFiles === undefined) {
    return {
      label,
      status: "missing",
      message:
        `${label}: ${directory} could not be read — the schemas contributes.yamlValidation ` +
        "points at are not in this install, so YAML completion, hover and per-file diagnostics " +
        "have nothing to bind. Reinstall the extension.",
    };
  }
  if (schemaFiles.length === 0) {
    return {
      label,
      status: "missing",
      message:
        `${label}: ${directory} is empty — the schemas contributes.yamlValidation points at are ` +
        "not in this install, so YAML completion, hover and per-file diagnostics have nothing " +
        "to bind. Reinstall the extension.",
    };
  }
  if (declared.length === 0) {
    // A manifest failure, and it fails the user the same way: files nothing
    // points at bind to nothing.
    return {
      label,
      status: "missing",
      message:
        `${label}: ${schemaFiles.length} file(s) in ${directory}, but this extension's ` +
        "contributes.yamlValidation declares no ./schemas/ url — nothing binds them, so YAML " +
        "completion, hover and per-file diagnostics never appear. Reinstall the extension.",
    };
  }
  const present = new Set(schemaFiles);
  const absent = declared.filter((name) => !present.has(name));
  if (absent.length > 0) {
    return {
      label,
      status: "missing",
      message:
        `${label}: ${absent.length} of ${declared.length} schema file(s) contributes.yamlValidation ` +
        `declares are not in ${directory} (${absent.join(", ")}) — YAML completion, hover and ` +
        "per-file diagnostics are silently dead for the layers those urls match. Reinstall the " +
        "extension.",
    };
  }
  return {
    label,
    status: "ok",
    message:
      `${label}: all ${declared.length} schema file(s) declared by contributes.yamlValidation ` +
      `are in ${directory}`,
  };
}

// --- The Copilot agent profile -------------------------------------------------

/**
 * Where a SWCraft project keeps the SWCraft Architect agent, relative to its
 * manifest's folder; `onboarding.ts` builds its paths from it.
 */
export const AGENT_PROFILE_RELATIVE_PATH = ".github/agents/swcraft-architect.agent.md";

/**
 * Whether the window's SWCraft project has the SWCraft Architect agent, and
 * whether it is this release's. Its `tools:` list (the SWCraft MCP tools and a
 * read-only `view`, no terminal or file edit) is what keeps Copilot from
 * answering model questions by scripting over the YAML.
 *
 * *profile* is the project's file, *bundled* this install's copy; each
 * `undefined` when unreadable. Compared byte for byte; a difference is a
 * `warning`, since a user may have edited it on purpose.
 *
 * What it does not check: whether the profile works, whether Copilot loads it,
 * or what `.github/copilot-instructions.md` says.
 */
export function checkAgentProfile(
  profile: Uint8Array | undefined,
  bundled: Uint8Array | undefined,
): ResourceHealth {
  const label = "Copilot agent profile";
  if (bundled === undefined) {
    return {
      label,
      status: "missing",
      message:
        `${label}: this install's bundled profile could not be read, so the project's ` +
        `${AGENT_PROFILE_RELATIVE_PATH} cannot be compared with it. Reinstall the extension.`,
    };
  }
  if (profile === undefined) {
    return {
      label,
      status: "missing",
      message:
        `${label}: ${AGENT_PROFILE_RELATIVE_PATH} is missing, so this project has no SWCraft ` +
        "Architect agent — the Copilot agent with the SWCraft MCP tools, read-only `view` for a " +
        "saved SWCraft answer, and no file edit or terminal. Run SWCraft: Onboard Project to add it.",
    };
  }
  if (Buffer.compare(profile, bundled) !== 0) {
    return {
      label,
      status: "warning",
      message: `${label}: ${AGENT_PROFILE_RELATIVE_PATH} differs from this release's; Onboard Project replaces it.`,
    };
  }
  return {
    label,
    status: "ok",
    message: `${label}: ${AGENT_PROFILE_RELATIVE_PATH} is this release's.`,
  };
}

/**
 * The row *label* for a project that does not use Copilot (`agentClients.ts`'s
 * `clientInUse()`): `ok`, since the project's choice is no finding. *client* is
 * the Copilot row's label in the client table.
 */
export function copilotNotUsed(label: string, client: string): ResourceHealth {
  return {
    label,
    status: "ok",
    message:
      `${label}: not checked — this project holds another chat client's files and none of ` +
      `${client}'s, so it is not set up for it. SWCraft: Onboard Project with ${client} chosen adds them.`,
  };
}

// --- The Copilot model guard ---------------------------------------------------

/**
 * Where a SWCraft project keeps the Copilot `PreToolUse` hook that runs
 * `swcraft guard`, relative to its manifest's folder: the one spelling, which
 * `onboarding.ts` builds its destination from.
 */
export const HOOK_RELATIVE_PATH = ".github/hooks/swcraft.json";

/** Everything `checkModelGuard()` decides from; the adapter does all the reading. */
export interface ModelGuardInput {
  /** The project's hook file, `undefined` when it is absent. */
  hook: Uint8Array | undefined;
  /** This install's bundled hook asset, `undefined` when it could not be read. */
  bundled: Uint8Array | undefined;
  /** Where a bare `swcraft` resolves on this host, or `undefined` when it resolves nowhere. */
  pathResolution: string | undefined;
  /**
   * `<pathResolution> --version`, or `undefined` when it was not run because
   * nothing resolved. A probe that did not run or printed no readable version is
   * read as "not this release's", never as a pass.
   */
  pathVersion: VersionProbe | undefined;
  /** This extension's own `packageJSON.version`, the release the hook's `swcraft` must be. */
  extensionVersion: string;
}

/**
 * Whether the window's SWCraft project has the Copilot model guard (the
 * `PreToolUse` hook keeping Copilot's file tools off the model directory), and
 * whether the hook can run.
 *
 * - The file missing or differing from this release's: a `warning` (a project
 *   works without it; a user may have edited it). This release's: ok.
 * - The hook runs the bare `swcraft` on `PATH`; one that resolves nowhere or is
 *   another release adds a `warning`. A hook that cannot run (no `swcraft`, or
 *   an older one without `guard`, exit 2) makes Copilot deny every tool call,
 *   SWCraft's included (observed with VS Code 1.141.1); the message says so for
 *   this release's hook, conditionally for one that differs, and not without a
 *   hook. A release from `GUARD_FIRST_RELEASE` on runs `guard` and decides.
 * - The row is never counted for a `PATH` finding: it lies outside the project.
 * - An unreadable bundled asset is `missing`, with the `PATH` finding kept.
 *
 * What it does not check: whether Copilot loads or honours the hook, whether
 * hooks are off (`chat.useHooks`), or terminal commands (the guard judges file
 * tools only). `PATH` is read as in `checkToolchain()`'s cases 5 and 6.
 */
export function checkModelGuard(input: ModelGuardInput): ResourceHealth {
  const label = "Copilot model guard";
  if (input.bundled === undefined) {
    // A hook that cannot be compared is not shown to be this release's: its cost is conditional.
    const pathProblem = guardPathProblem(input, input.hook === undefined ? "absent" : "differs");
    // `missing` decides the status; the PATH finding is kept beside it, never dropped.
    const broken =
      `${label}: this install's bundled hook could not be read, so the project's ` +
      `${HOOK_RELATIVE_PATH} cannot be compared with it. Reinstall the extension.`;
    return {
      label,
      status: "missing",
      message: pathProblem === undefined ? broken : `${broken} ${pathProblem}`,
    };
  }
  let status: ResourceHealth["status"];
  let finding: string;
  let hook: HookState;
  if (input.hook === undefined) {
    hook = "absent";
    status = "warning";
    finding =
      `${HOOK_RELATIVE_PATH} is missing, so the model is not protected from Copilot's file ` +
      "tools. Run SWCraft: Onboard Project to add it.";
  } else if (Buffer.compare(input.hook, input.bundled) !== 0) {
    hook = "differs";
    status = "warning";
    finding = `${HOOK_RELATIVE_PATH} differs from this release's; Onboard Project replaces it.`;
  } else {
    hook = "this-release";
    status = "ok";
    finding = `${HOOK_RELATIVE_PATH} is this release's.`;
  }
  const pathProblem = guardPathProblem(input, hook);
  if (pathProblem !== undefined) {
    return { label, status: "warning", message: `${label}: ${finding} ${pathProblem}` };
  }
  return { label, status, message: `${label}: ${finding}` };
}

/**
 * The first release whose `swcraft` has the `guard` subcommand the hook runs; a
 * `swcraft` older than this exits 2 on the hook's command line.
 */
export const GUARD_FIRST_RELEASE = "0.16.0";

/** The state of the project's hook file, which decides whether a PATH finding costs Copilot anything. */
type HookState = "this-release" | "differs" | "absent";

/**
 * The `swcraft`-on-`PATH` half of `checkModelGuard()`, or `undefined` when it is
 * this release's. *hook* is the project's hook file's state.
 */
function guardPathProblem(input: ModelGuardInput, hook: HookState): string | undefined {
  const cost =
    "Copilot can call no tool in this project — SWCraft's included — until this release's " +
    "`swcraft` is on PATH.";
  // A hook that differs may no longer run `swcraft guard`, so its cost is conditional.
  const stillRuns = "the hook still runs `swcraft guard`";
  const ifStillRuns = hook === "differs" ? `if ${stillRuns}, ` : "";
  if (input.pathResolution === undefined) {
    const lead =
      "But a bare `swcraft` resolves nowhere on the PATH this window was launched with, so the " +
      "hook cannot run";
    return hook === "absent" ? `${lead}.` : `${lead}, and ${ifStillRuns}${cost}`;
  }
  const reported =
    input.pathVersion?.ok === true ? parseSwcraftVersion(input.pathVersion.output) : undefined;
  if (reported === undefined) {
    const lead = `But the \`swcraft\` on PATH (${input.pathResolution}) reported no version this could read`;
    if (hook === "absent") {
      return `${lead}, so it may have no \`guard\`.`;
    }
    if (hook === "differs") {
      return `${lead}; if it has no \`guard\` and ${stillRuns}, ${cost}`;
    }
    return `${lead}; if it has no \`guard\`, ${cost}`;
  }
  if (reported === input.extensionVersion) {
    return undefined;
  }
  const lead =
    `But the \`swcraft\` on PATH (${input.pathResolution}) is version ${reported} and this ` +
    `extension is ${input.extensionVersion}`;
  if (compareReleases(reported, GUARD_FIRST_RELEASE) === -1) {
    return hook === "absent"
      ? `${lead}; ${reported} has no \`guard\`.`
      : `${lead}; ${reported} has no \`guard\`, so ${ifStillRuns}${cost}`;
  }
  // A release that has `guard` runs it and decides: the answer is that release's, not "no tool".
  if (hook === "absent") {
    return `${lead}; ${reported} has \`guard\`, but no hook runs it in this project.`;
  }
  if (hook === "differs") {
    return `${lead}; if ${stillRuns}, its answer is that release's \`swcraft guard\`'s.`;
  }
  return `${lead}; the hook's answer is that release's \`swcraft guard\`'s.`;
}

// --- The report --------------------------------------------------------------

export interface HealthInput {
  /** `vscode.version` — the running host's product version. */
  vscodeVersion: string;
  /** This extension's own `packageJSON.extensionPack`, in manifest order. */
  declaredPack: readonly string[];
  /** `vscode.extensions.all`, reduced — enabled extensions only. */
  installed: readonly InstalledExtension[];
  toolchain: ResourceHealth;
  schemas: ResourceHealth;
  /**
   * `checkAgentProfile()`'s verdict, or `undefined` when the window holds no
   * SWCraft project — the row then does not apply and is not printed.
   */
  agentProfile?: ResourceHealth;
  /** `checkModelGuard()`'s verdict, or `undefined` when the window holds no SWCraft project. */
  modelGuard?: ResourceHealth;
}

/**
 * The limits of this report, printed inside it, so "SWCraft did not mention it"
 * is not read as "SWCraft checked it".
 *
 * Every verdict is read from a declaration that travels with its subject; a
 * member's runtime is declared nowhere, so it is not probed. Members are named
 * by display name, never by id (the pack is declared in `package.json` alone).
 */
export const UNDETECTABLE: readonly string[] = [
  "Disabled or not installed — VS Code's extension API lists only enabled extensions, so a " +
    "member reported absent may be installed and switched off. The Extensions view is where " +
    "that is visible.",
  "Whether a present member works — this checks that it is installed, enabled and that this " +
    "host satisfies its declared engines.vscode. It does not activate it or exercise it.",
  "A member this host is too old for — VS Code does not enable an extension whose " +
    "engines.vscode the host fails, and a disabled extension is not listed, so such a member " +
    "is reported above as absent rather than as version-incompatible. No members reported " +
    "version-incompatible is therefore not evidence that every declared floor was met.",
  "What a member does on the network — SWCraft observes no pack member's network activity at " +
    "all. Two known paths, both Red Hat XML's: referenced DTDs and XSDs are fetched over the " +
    "network by default (xml.downloadExternalResources.enabled), and a platform-mismatched " +
    "install downloads its LemMinX binary. Neither is observable from here.",
  "Members not declared in this extension's package.json — the pack checked is the one the " +
    "manifest declares, so an extension a user installed alongside it is neither required nor " +
    "reported.",
  "Whether the installed CLI works — the toolchain check runs `swcraft --version` and compares " +
    "it with this extension's version, and that is all. Whether a model, a project or an ARXML " +
    "is sound is `swcraft validate`'s question and no report of this kind answers it.",
  "Which `swcraft` a build actually runs — the PATH line asks where a bare `swcraft` resolves " +
    "in this window's environment, which is not necessarily a terminal's (a shell profile, an " +
    "activated environment or a `moon` toolchain can change it), and it compares paths as " +
    "strings, so a link or a short name pointing at the same file can read as a shadow.",
  "Whether a bundled JSON Schema is the right one — the schema check compares the file names " +
    "contributes.yamlValidation declares against the names in the schemas/ directory, and reads " +
    "no schema's content. A truncated, stale or hand-edited schema file counts as present. " +
    "Content is checked by the project's own tests before release, never here.",
];

/**
 * Render the whole report as output-channel lines: a function of *input* alone,
 * reading no clock.
 */
export function buildHealthReport(input: HealthInput): string[] {
  const members = checkPackHealth(input.installed, input.declaredPack, input.vscodeVersion);
  const resources = resourcesOf(input);
  const problems = problemCount(members, resources);

  const lines: string[] = [];
  lines.push("SWCraft: Editor Tooling Health");
  lines.push("");
  lines.push(`VS Code ${input.vscodeVersion}`);
  lines.push("");
  lines.push(`Extension pack — ${input.declaredPack.length} member(s) declared in package.json:`);
  if (members.length === 0) {
    lines.push("  (none — this extension declares no extensionPack)");
  }
  for (const member of members) {
    lines.push(`  ${marker(member.status === "present")} ${member.message}`);
  }
  lines.push("");
  lines.push("Runtime and schema resources:");
  for (const resource of resources) {
    lines.push(`  ${resourceMarker(resource)} ${resource.message}`);
  }
  lines.push("");
  lines.push("What this report cannot see:");
  for (const limit of UNDETECTABLE) {
    lines.push(`  - ${limit}`);
  }
  lines.push("");
  lines.push(summaryLine(problems));
  lines.push(NOT_VALIDATION_EVIDENCE);
  return lines;
}

function marker(ok: boolean): string {
  return ok ? "[ok]     " : "[problem]";
}

/**
 * A resource's marker, all three the same width so the findings stay in one
 * column; `[warning]` is not counted by `problemCount()`.
 */
function resourceMarker(resource: ResourceHealth): string {
  if (resource.status === "ok") {
    return "[ok]     ";
  }
  return resource.status === "warning" ? "[warning]" : "[problem]";
}

/** The one-line verdict, also the adapter's notification. */
export function summaryLine(problems: number): string {
  return problems === 0
    ? "No problems found."
    : `${problems} problem(s) found — see the lines marked [problem] above.`;
}

function problemCount(
  members: readonly PackMemberHealth[],
  resources: readonly ResourceHealth[],
): number {
  return (
    members.filter((member) => member.status !== "present").length +
    // `warning` is rendered, not counted (`ResourceHealth.status`).
    resources.filter((resource) => resource.status === "missing").length
  );
}

/**
 * The report's problem count, for the adapter's notification; the same
 * `problemCount()` the report renders, so the two cannot disagree.
 */
export function countProblems(input: HealthInput): number {
  return problemCount(
    checkPackHealth(input.installed, input.declaredPack, input.vscodeVersion),
    resourcesOf(input),
  );
}

/** The resource rows of *input*, in report order; the two project rows only where they apply. */
function resourcesOf(input: HealthInput): ResourceHealth[] {
  const rows = [input.toolchain, input.schemas];
  for (const row of [input.agentProfile, input.modelGuard]) {
    if (row !== undefined) {
      rows.push(row);
    }
  }
  return rows;
}

/**
 * *probe* wrapped so each executable (by `samePath()`) is probed once: the
 * toolchain row and the model guard's row usually probe the same file, and must
 * not disagree about it. What it does not reach: two spellings of one file (a
 * link, an 8.3 short name) are still two probes.
 */
export function probeOncePerPath(
  probe: (executable: string) => Promise<VersionProbe>,
  platform: Platform,
): (executable: string) => Promise<VersionProbe> {
  const answered: { executable: string; result: Promise<VersionProbe> }[] = [];
  return (executable) => {
    const earlier = answered.find((entry) => samePath(entry.executable, executable, platform));
    if (earlier !== undefined) {
      return earlier.result;
    }
    const result = probe(executable);
    answered.push({ executable, result });
    return result;
  };
}

// Pure logic: SWCraft project onboarding, plan and apply for a target directory.
// It copies the files of the chat clients the user chose, as the client table
// (`agentClients.ts`, `clients.json`) names them, plus the companion
// `.vscode/settings.json` for Copilot together with Codex or Claude Code
// (`applyCompanionSettings()`).
//
// No `vscode` import, so it runs under plain `node --test`. `planOnboarding()`
// touches no filesystem; `applyOnboardingPlan()` copies from the assets
// directory the caller passes (`dist/onboarding-assets/` at runtime,
// `fixtures/agents/` in tests). Whether and how `swcraft init` runs is
// `commandCore.ts`'s `planOnboardingSteps()` and commands.ts.
//
// It writes no `.vscode/mcp.json`: the extension publishes the MCP server itself
// (`mcpProvider.ts`). Every copy is the template's file byte for byte, written
// link-safe (`writeKeepingForeign()`): a symbolic link at the file or at a folder
// on the way is kept and named, never written through. Copilot's three files
// replace any regular file (`always-own`); the others are written where absent
// or recognisably SWCraft's and otherwise kept (`OwnFileRecognition`), with no
// JSON or TOML merge. A file of a client the user did not choose is neither
// written nor removed.

import * as crypto from "node:crypto";
import * as fs from "node:fs";
import * as path from "node:path";

import { type ClientTable, type CompanionSettings, clientNamed } from "./agentClients";
import { writeFileExclusive } from "./exclusiveCreate";
import { AGENT_PROFILE_RELATIVE_PATH, HOOK_RELATIVE_PATH } from "./healthCheck";
import { compareCodeUnits } from "./navigationCore";
import { MANIFEST_FILENAME } from "./project";

/** File name of the custom-agent asset within the onboarding assets directory. */
export const AGENT_ASSET_NAME = path.posix.basename(AGENT_PROFILE_RELATIVE_PATH);
/** File name of the Copilot instructions asset within the onboarding assets directory. */
export const INSTRUCTIONS_ASSET_NAME = "copilot-instructions.md";
/**
 * File name of the Copilot model guard's hook asset within the onboarding assets
 * directory, named apart from its destination's basename (`swcraft.json`) to say
 * what it is beside the agent assets.
 */
export const HOOK_ASSET_NAME = "swcraft.hooks.json";
/**
 * File name of the Codex model guard's hook asset, named apart from its
 * destination's basename (`hooks.json`). No Health row checks it: whether Codex
 * trusted it is not visible from the extension, and an older `swcraft` without
 * `--host` makes Codex block every shell command and edit.
 */
export const CODEX_HOOK_ASSET_NAME = "swcraft.codex-hooks.json";
/** Where the Codex hook lives in a project, relative to the manifest's folder. */
export const CODEX_HOOK_RELATIVE_PATH = ".codex/hooks.json";
/** The folder of the Codex hook, `CODEX_HOOK_RELATIVE_PATH`'s first component. */
const CODEX_FOLDER_NAME = ".codex";

// --- The five client files: Codex and Claude Code -----------------------------
//
// Each asset is a byte copy of the template's file (pinned by
// `packageVsix.test.ts` and `packages/mcp/tests/test_vscode_agent_profile.py`),
// named apart from its destination where that name would mean something here
// (a `CLAUDE.md` would be loaded by a Claude Code session in this repository).
// `AGENTS.md` keeps its name: `dev.py schemas` renders it under that name.

/** The asset of the project's `.mcp.json`, Claude Code's server line. */
export const MCP_JSON_ASSET_NAME = "swcraft.mcp.json";
/** The asset of the project's `.claude/settings.json`, Claude Code's settings and guard hook. */
export const CLAUDE_SETTINGS_ASSET_NAME = "swcraft.claude-settings.json";
/** The asset of the project's `.codex/config.toml`, Codex's server line. */
export const CODEX_CONFIG_ASSET_NAME = "swcraft.codex-config.toml";
/** The asset of the project's `AGENTS.md`, the profile Codex reads (and `CLAUDE.md` imports). */
export const AGENTS_MD_ASSET_NAME = "AGENTS.md";
/** The asset of the project's `CLAUDE.md`, one marker line and `@AGENTS.md`. */
export const CLAUDE_MD_ASSET_NAME = "swcraft.claude.md";

// --- Kiro's three files -------------------------------------------------------
//
// Byte copies of the template's `.kiro/` files, pinned by the same two tests.

/** The asset of the project's `.kiro/settings/mcp.json`, Kiro's server line. */
export const KIRO_MCP_JSON_ASSET_NAME = "swcraft.kiro-mcp.json";
/** The asset of the project's `.kiro/steering/swcraft.md`, the profile Kiro reads whole. */
export const KIRO_STEERING_ASSET_NAME = "swcraft.kiro-steering.md";
/** The asset of the project's `.kiro/hooks/swcraft-guard.json`, Kiro's model guard hook. */
export const KIRO_HOOK_ASSET_NAME = "swcraft.kiro-hook.json";

/** Kiro's three files' places in a project, relative to the manifest's folder. */
export const KIRO_MCP_JSON_RELATIVE_PATH = ".kiro/settings/mcp.json";
export const KIRO_STEERING_RELATIVE_PATH = ".kiro/steering/swcraft.md";
export const KIRO_HOOK_RELATIVE_PATH = ".kiro/hooks/swcraft-guard.json";

/** The five client files' places in a project, relative to the manifest's folder. */
export const MCP_JSON_RELATIVE_PATH = ".mcp.json";
export const CLAUDE_SETTINGS_RELATIVE_PATH = ".claude/settings.json";
export const CODEX_CONFIG_RELATIVE_PATH = ".codex/config.toml";
export const AGENTS_MD_RELATIVE_PATH = "AGENTS.md";
export const CLAUDE_MD_RELATIVE_PATH = "CLAUDE.md";
/**
 * The titles of the guide sections the client table's `guideSection` column
 * names, keyed by `<page>.md#<anchor>`, with the page's title: the report names a
 * section by the words the guide shows. A title equal to its page's is the page
 * itself (`guidePointer()`). `onboarding.test.ts` holds them equal to the guide.
 */
export const GUIDE_SECTION_TITLES: ReadonlyMap<string, { page: string; title: string }> = new Map([
  ["vscode.md#talking-to-the-model", { page: "In VS Code", title: "Talking to the model" }],
  [
    "vscode.md#codex-and-claude-code-in-vs-code",
    { page: "In VS Code", title: "Codex and Claude Code in VS Code" },
  ],
  ["kiro.md#kiro", { page: "In Kiro", title: "In Kiro" }],
  [
    "kiro.md#kiro-with-codex-or-claude-code",
    { page: "In Kiro", title: "Kiro with Codex or Claude Code" },
  ],
]);

/** The section the Codex and Claude Code rows name: what to add by hand to a client file kept. */
const CLIENT_FILES_SECTION = "vscode.md#codex-and-claude-code-in-vs-code";

/**
 * The title of the guide section that says what to add by hand to a client file
 * *Onboard Project* kept, on the guide's *In VS Code* page (`docs/guide/vscode.md`).
 */
export const CLIENT_FILES_GUIDE_SECTION =
  GUIDE_SECTION_TITLES.get(CLIENT_FILES_SECTION)?.title ?? "";

/**
 * The sentence that marks a Markdown or TOML client file as SWCraft's: its first
 * line, as an HTML comment or a `#` comment. The same sentence is
 * `swcraft_tools.agents_md`'s.
 */
export const ONBOARD_MARKER_SENTENCE =
  "Written by SWCraft's Onboard Project. Edit freely: once this line is gone, Onboard Project keeps the file.";
export const MARKDOWN_MARKER_LINE = `<!-- ${ONBOARD_MARKER_SENTENCE} -->`;
export const TOML_MARKER_LINE = `# ${ONBOARD_MARKER_SENTENCE}`;

/**
 * How *Onboard Project* recognises its own file at a destination (`isOwnFile()`):
 * `always-own` takes every regular file for SWCraft's (only a link or a non-file
 * is kept); the JSON kinds by structure; the Markdown and TOML ones by their
 * first line (`hasOnboardMarker()`); `none` recognises nothing, so the file is
 * written only when absent.
 */
export type OwnFileRecognition =
  | "always-own"
  | "codex-hook"
  | "mcp-json"
  | "claude-settings"
  | "kiro-mcp-json"
  | "kiro-hook"
  | "markdown-marker"
  | "toml-marker"
  | "none";

export interface OnboardingFileCopy {
  /** File name within the onboarding assets directory (dist/onboarding-assets or fixtures/agents). */
  assetName: string;
  /** Absolute destination path under the target directory. */
  destPath: string;
  /**
   * How SWCraft's own file at the destination is recognised and replaced; any
   * other entry there is kept. Absent means `always-own`.
   */
  keepsForeign?: OwnFileRecognition;
}

export interface OnboardingPlan {
  /** Static asset copies: the chosen clients' files (`CLIENT_FILE_ASSETS`). */
  fileCopies: OnboardingFileCopy[];
  /** The directory being onboarded. */
  targetDir: string;
}

/** The SWCraft Architect agent's path in the project whose manifest is in *projectDir*. */
export function agentProfilePath(projectDir: string): string {
  return path.join(projectDir, ...AGENT_PROFILE_RELATIVE_PATH.split("/"));
}

/** The Copilot model guard's hook file in the project whose manifest is in *projectDir*. */
export function hookPath(projectDir: string): string {
  return path.join(projectDir, ...HOOK_RELATIVE_PATH.split("/"));
}

/** The Codex model guard's hook file in the project whose manifest is in *projectDir*. */
export function codexHookPath(projectDir: string): string {
  return path.join(projectDir, ...CODEX_HOOK_RELATIVE_PATH.split("/"));
}

/** A path in *projectDir* from its POSIX spelling relative to the manifest's folder. */
function inProject(projectDir: string, relative: string): string {
  return path.join(projectDir, ...relative.split("/"));
}

/** Where the Copilot instructions live in a project, relative to the manifest's folder. */
export const INSTRUCTIONS_RELATIVE_PATH = `.github/${INSTRUCTIONS_ASSET_NAME}`;

/** One client file's onboarding asset and how *Onboard Project* recognises its own copy. */
export interface ClientFileAsset {
  /** File name within the onboarding assets directory. */
  assetName: string;
  /** How SWCraft's own file is recognised at the destination (`OnboardingFileCopy.keepsForeign`). */
  keepsForeign: OwnFileRecognition;
}

/**
 * Every file a row of the client table can name, keyed by its POSIX path relative
 * to the project: the asset it is copied from and how SWCraft's own copy is
 * recognised. The one place a destination meets its asset (the table says only
 * which client writes which file). The entry order is the plan's order; a table
 * file with no entry fails `planOnboarding()`.
 */
export const CLIENT_FILE_ASSETS: ReadonlyMap<string, ClientFileAsset> = new Map([
  [AGENT_PROFILE_RELATIVE_PATH, { assetName: AGENT_ASSET_NAME, keepsForeign: "always-own" }],
  [INSTRUCTIONS_RELATIVE_PATH, { assetName: INSTRUCTIONS_ASSET_NAME, keepsForeign: "always-own" }],
  [HOOK_RELATIVE_PATH, { assetName: HOOK_ASSET_NAME, keepsForeign: "always-own" }],
  [CODEX_HOOK_RELATIVE_PATH, { assetName: CODEX_HOOK_ASSET_NAME, keepsForeign: "codex-hook" }],
  [MCP_JSON_RELATIVE_PATH, { assetName: MCP_JSON_ASSET_NAME, keepsForeign: "mcp-json" }],
  [
    CLAUDE_SETTINGS_RELATIVE_PATH,
    { assetName: CLAUDE_SETTINGS_ASSET_NAME, keepsForeign: "claude-settings" },
  ],
  [CODEX_CONFIG_RELATIVE_PATH, { assetName: CODEX_CONFIG_ASSET_NAME, keepsForeign: "toml-marker" }],
  [AGENTS_MD_RELATIVE_PATH, { assetName: AGENTS_MD_ASSET_NAME, keepsForeign: "markdown-marker" }],
  [CLAUDE_MD_RELATIVE_PATH, { assetName: CLAUDE_MD_ASSET_NAME, keepsForeign: "markdown-marker" }],
  [
    KIRO_MCP_JSON_RELATIVE_PATH,
    { assetName: KIRO_MCP_JSON_ASSET_NAME, keepsForeign: "kiro-mcp-json" },
  ],
  [
    KIRO_STEERING_RELATIVE_PATH,
    { assetName: KIRO_STEERING_ASSET_NAME, keepsForeign: "markdown-marker" },
  ],
  [KIRO_HOOK_RELATIVE_PATH, { assetName: KIRO_HOOK_ASSET_NAME, keepsForeign: "kiro-hook" }],
]);

/** The client whose files the notice offered to a project without the agent profile writes. */
export const AGENT_PROFILE_NOTICE_CLIENT = "copilot";

/**
 * The copies the notice offered to a project without the agent profile makes
 * (`applyAgentProfilePlan()`): the files of the table's Copilot row alone. Pure.
 */
export function planAgentProfileNotice(targetDir: string, table: ClientTable): OnboardingPlan {
  return planOnboarding(targetDir, clientNamed(table, AGENT_PROFILE_NOTICE_CLIENT).files);
}

/**
 * Plan onboarding for *targetDir*: one copy per file of *files* (the chosen
 * clients' files, `clientFilesFor()`), in `CLIENT_FILE_ASSETS`' order. A file
 * with no asset throws, naming it. Pure.
 */
export function planOnboarding(targetDir: string, files: readonly string[]): OnboardingPlan {
  for (const file of files) {
    if (!CLIENT_FILE_ASSETS.has(file)) {
      throw new Error(`the client table names ${file}, which has no onboarding asset`);
    }
  }
  const fileCopies: OnboardingFileCopy[] = [];
  for (const [relative, asset] of CLIENT_FILE_ASSETS) {
    if (!files.includes(relative)) {
      continue;
    }
    fileCopies.push({
      assetName: asset.assetName,
      destPath: inProject(targetDir, relative),
      keepsForeign: asset.keepsForeign,
    });
  }
  return { targetDir, fileCopies };
}

/**
 * The destinations of *plan* that `applyOnboardingPlan()` wrote, POSIX paths
 * relative to the project, in plan order: every copy but a destination it kept
 * and one inside a folder it kept.
 */
export function writtenFiles(plan: OnboardingPlan, kept: readonly string[]): string[] {
  return plan.fileCopies
    .filter(
      (copy) =>
        !kept.some(
          (entry) => copy.destPath === entry || copy.destPath.startsWith(entry + path.sep),
        ),
    )
    .map((copy) => projectRelative(plan.targetDir, copy.destPath));
}

/** *entry* in *targetDir* as a POSIX path relative to it. */
function projectRelative(targetDir: string, entry: string): string {
  return path.relative(targetDir, entry).split(path.sep).join("/");
}

/**
 * Execute *plan*: copy the assets from *sourceAssetsDir* to their destinations,
 * creating parent directories. Does not run `swcraft init`.
 *
 * A copy is written where there is none or where the file is SWCraft's own
 * (`isOwnFile()`); re-running on an onboarded folder is how a project gets this
 * release's copies. Any other entry (a file that is the user's or does not
 * parse, a directory, a symbolic link) is kept, and so is a folder on the way
 * that is a link or no folder (returned once, however many copies it holds).
 * Returns the destinations kept, in plan order, for `keptMessage()`.
 * *afterInspection* is a test seam (`writeKeepingForeign()`).
 */
export function applyOnboardingPlan(
  plan: OnboardingPlan,
  sourceAssetsDir: string,
  afterInspection?: (destPath: string) => void,
): string[] {
  const kept: string[] = [];
  for (const copy of plan.fileCopies) {
    // Read first, so a missing asset throws even where the destination is kept.
    const keptHere = writeKeepingForeign(
      fs.readFileSync(path.join(sourceAssetsDir, copy.assetName)),
      copy.destPath,
      plan.targetDir,
      copy.keepsForeign ?? "always-own",
      afterInspection,
    );
    if (keptHere !== undefined && !kept.includes(keptHere)) {
      kept.push(keptHere);
    }
  }
  return kept;
}

/**
 * Write SWCraft's *content* to *destPath* in *projectDir* without following a
 * link and without replacing a file that appeared after it was inspected; returns
 * the destination kept (the outermost linked folder inside the project, else the
 * file), or `undefined` when written. *recognition* tells SWCraft's own file
 * from the user's.
 *
 * The decision rests on one observation (`inspectEntry()`). An absent file is
 * created exclusively (`writeFileExclusive()`, which fails on any entry, a
 * dangling link included); an `EEXIST` repeats the inspection once. SWCraft's
 * own file is replaced by an exclusive temporary file renamed over it, which
 * replaces a link rather than writing through it. The folders on the way are
 * re-read with `lstat` before each write. *afterInspection* is a test seam,
 * called after each inspection.
 *
 * What this does not reach: a folder swapped for a link, or SWCraft's file for a
 * foreign one, in the few system calls before the write or rename (no portable
 * Node.js `openat`/`renameat`); on Windows, a link planted at a temporary file's
 * random name, which a `wx` create (`CreateFile` with `CREATE_NEW`) follows;
 * `writeFileExclusive()`'s fallback without hard links; and *projectDir* itself,
 * which the user chose.
 */
function writeKeepingForeign(
  content: Buffer,
  destPath: string,
  projectDir: string,
  recognition: OwnFileRecognition,
  afterInspection?: (destPath: string) => void,
): string | undefined {
  const linked = linkedFolderInside(projectDir, destPath);
  if (linked !== undefined) {
    return linked;
  }
  fs.mkdirSync(path.dirname(destPath), { recursive: true });
  // Two rounds: an `EEXIST` inspects again once; a second keeps what is there.
  for (let round = 0; round < 2; round++) {
    const linkedBefore = linkedFolderInside(projectDir, destPath);
    if (linkedBefore !== undefined) {
      return linkedBefore;
    }
    const state = inspectEntry(destPath, recognition);
    if (state === "foreign") {
      return destPath;
    }
    afterInspection?.(destPath);
    const linkedAfter = linkedFolderInside(projectDir, destPath);
    if (linkedAfter !== undefined) {
      return linkedAfter;
    }
    if (state === "own") {
      replaceByRename(content, destPath);
      return undefined;
    }
    try {
      writeFileExclusive(destPath, content);
      return undefined;
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "EEXIST") {
        throw error;
      }
    }
  }
  return destPath;
}

/** Whether *destPath* names any entry, a dangling link included (`lstat`). */
export function entryExists(destPath: string): boolean {
  try {
    fs.lstatSync(destPath);
    return true;
  } catch (error) {
    if (isMissingEntry(error)) {
      return false;
    }
    throw error;
  }
}

/**
 * Replace *destPath* with *content* by an exclusive temporary file beside it and
 * a rename over it, the temporary file removed when the rename fails.
 */
function replaceByRename(content: Buffer, destPath: string): void {
  const temporary = path.join(
    path.dirname(destPath),
    `.${path.basename(destPath)}.swcraft-${process.pid}-${crypto.randomBytes(6).toString("hex")}.tmp`,
  );
  fs.writeFileSync(temporary, content, { flag: "wx" });
  try {
    fs.renameSync(temporary, destPath);
  } catch (error) {
    fs.rmSync(temporary, { force: true });
    throw error;
  }
}

/**
 * Whether *error* says an entry is not there: `ENOENT`, or `ENOTDIR` (a component
 * on the way is a file, so nothing below it exists).
 */
function isMissingEntry(error: unknown): boolean {
  const code = (error as NodeJS.ErrnoException).code;
  return code === "ENOENT" || code === "ENOTDIR";
}

/**
 * The outermost entry strictly inside *projectDir* on the way to *destPath* that
 * is a symbolic link (dangling or not) or no folder, by `lstat`; `undefined` when
 * there is none. A write would follow a linked parent out of the project, and a
 * file on the way would make `mkdirSync` throw partway. *projectDir* itself is
 * not judged.
 */
function linkedFolderInside(projectDir: string, destPath: string): string | undefined {
  const relative = path.relative(projectDir, path.dirname(destPath));
  if (relative === "") {
    return undefined;
  }
  let folder = projectDir;
  for (const part of relative.split(path.sep)) {
    folder = path.join(folder, part);
    try {
      const entry = fs.lstatSync(folder);
      if (entry.isSymbolicLink() || !entry.isDirectory()) {
        return folder;
      }
    } catch (error) {
      if (isMissingEntry(error)) {
        return undefined;
      }
      throw error;
    }
  }
  return undefined;
}

/** What one observation of a destination found (`inspectEntry()`). */
type EntryState = "absent" | "own" | "foreign";

/**
 * One observation of *destPath*: `absent`, `own` (a regular file SWCraft's by
 * *recognition*), or `foreign` for anything else, a symbolic link always (read
 * with `lstat`, since a copy onto it would write the link's target).
 */
function inspectEntry(destPath: string, recognition: OwnFileRecognition): EntryState {
  let entry: fs.Stats;
  try {
    entry = fs.lstatSync(destPath);
  } catch (error) {
    if (isMissingEntry(error)) {
      return "absent";
    }
    return "foreign";
  }
  if (!entry.isFile()) {
    return "foreign";
  }
  if (recognition === "always-own") {
    return "own";
  }
  let existing: string;
  try {
    existing = fs.readFileSync(destPath, "utf8");
  } catch {
    return "foreign";
  }
  return isOwnFile(existing, recognition) ? "own" : "foreign";
}

/** Whether *text* is SWCraft's own file as *recognition* tells it (`OwnFileRecognition`). */
export function isOwnFile(text: string, recognition: OwnFileRecognition): boolean {
  switch (recognition) {
    case "always-own":
      return true;
    case "codex-hook":
      return isSwcraftCodexHook(text);
    case "mcp-json":
      return isSwcraftMcpJson(text);
    case "claude-settings":
      return isSwcraftClaudeSettings(text);
    case "kiro-mcp-json":
      return isSwcraftKiroMcpJson(text);
    case "kiro-hook":
      return isSwcraftKiroHook(text);
    case "markdown-marker":
      return hasOnboardMarker(text, MARKDOWN_MARKER_LINE);
    case "toml-marker":
      return hasOnboardMarker(text, TOML_MARKER_LINE);
    case "none":
      return false;
  }
}

/**
 * Whether *text*'s first line is *markerLine* exactly (a trailing `\r` ignored);
 * removing that line makes the file the user's.
 */
export function hasOnboardMarker(text: string, markerLine: string): boolean {
  const end = text.indexOf("\n");
  const first = end === -1 ? text : text.slice(0, end);
  return (first.endsWith("\r") ? first.slice(0, -1) : first) === markerLine;
}

/** *text* parsed as JSON, or `undefined` when it does not parse. */
function parsedJson(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return undefined;
  }
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function hasOnlyKeys(value: Record<string, unknown>, allowed: readonly string[]): boolean {
  return Object.keys(value).every((key) => allowed.includes(key));
}

/** Whether *value* is an object holding only *allowed* keys, each with a string value. */
function isStringMap(value: unknown, allowed: readonly string[]): boolean {
  return (
    isPlainObject(value) &&
    hasOnlyKeys(value, allowed) &&
    Object.values(value).every((member) => typeof member === "string")
  );
}

/**
 * Whether *hooks* is a hook table SWCraft writes, of any release: a non-empty
 * `PreToolUse` and nothing else, each entry a matcher group (an optional string
 * `matcher`, a non-empty `hooks`) whose every handler is a `type: "command"`
 * running *command*, with any `timeout` or `statusMessage`. Matcher and timeout
 * are not compared, so another release's file is still recognised.
 */
function isSwcraftHookTable(hooks: unknown, command: string): boolean {
  if (!isPlainObject(hooks) || !hasOnlyKeys(hooks, ["PreToolUse"])) {
    return false;
  }
  const groups = hooks.PreToolUse;
  // An empty list holds no SWCraft handler; `every()` alone would pass it.
  return (
    Array.isArray(groups) &&
    groups.length > 0 &&
    groups.every(
      (group: unknown) =>
        isPlainObject(group) &&
        hasOnlyKeys(group, ["matcher", "hooks"]) &&
        (group.matcher === undefined || typeof group.matcher === "string") &&
        Array.isArray(group.hooks) &&
        group.hooks.length > 0 &&
        group.hooks.every(
          (handler: unknown) =>
            isPlainObject(handler) &&
            hasOnlyKeys(handler, ["type", "command", "timeout", "statusMessage"]) &&
            handler.type === "command" &&
            handler.command === command,
        ),
    )
  );
}

/**
 * Whether *text* is an `mcpServers` file SWCraft wrote, the core of
 * `isSwcraftMcpJson()` and `isSwcraftKiroMcpJson()`: JSON holding `mcpServers`
 * and nothing else, its one server `swcraft`, holding no key but `command`,
 * `args`, `env` and, with *numericTimeout*, `timeout`; `command` is `swcraft`,
 * `args` a list of strings opening with `mcp-serve`, `env`, when present, holds
 * `SWCRAFT_REVIEW` alone, a string, and `timeout`, when present, is a number.
 */
function isSwcraftServerJson(text: string, numericTimeout: boolean): boolean {
  const parsed = parsedJson(text);
  if (!isPlainObject(parsed) || !hasOnlyKeys(parsed, ["mcpServers"])) {
    return false;
  }
  const servers = parsed.mcpServers;
  if (!isPlainObject(servers) || !hasOnlyKeys(servers, ["swcraft"])) {
    return false;
  }
  const server = servers.swcraft;
  return (
    isPlainObject(server) &&
    hasOnlyKeys(
      server,
      numericTimeout ? ["command", "args", "env", "timeout"] : ["command", "args", "env"],
    ) &&
    server.command === "swcraft" &&
    Array.isArray(server.args) &&
    server.args.every((argument: unknown) => typeof argument === "string") &&
    server.args[0] === "mcp-serve" &&
    (server.env === undefined || isStringMap(server.env, ["SWCRAFT_REVIEW"])) &&
    (server.timeout === undefined || typeof server.timeout === "number")
  );
}

/**
 * Whether *text* is a `.mcp.json` SWCraft wrote, of any release, by structure
 * (`isSwcraftServerJson()`); the remaining arguments and the review mode's value
 * are not compared. A second server, another key or unparseable text makes it the
 * user's. What this does not reach: a user who changed only SWCraft's own values
 * inside that shape (another `--project`, review mode) has the file replaced.
 */
export function isSwcraftMcpJson(text: string): boolean {
  return isSwcraftServerJson(text, false);
}

/**
 * Whether *text* is a `.kiro/settings/mcp.json` SWCraft wrote, of any release:
 * `isSwcraftMcpJson()`'s shape, plus an optional numeric `timeout`. Kiro's own
 * server keys (`disabled`, `autoApprove`, `disabledTools`) make it the user's.
 * What this does not reach: as `isSwcraftMcpJson()`, a user's change to
 * SWCraft's own values inside that shape is replaced.
 */
export function isSwcraftKiroMcpJson(text: string): boolean {
  return isSwcraftServerJson(text, true);
}

/** The one command a SWCraft Kiro hook entry runs. */
const KIRO_GUARD_COMMAND = "swcraft guard --host kiro";

/**
 * Whether *text* is a `.kiro/hooks/swcraft-guard.json` SWCraft wrote, of any
 * release: a string `version` and a non-empty `hooks` list and nothing else; each
 * entry holds only optional string `name`, `trigger`, `matcher`, an optional
 * numeric `timeout` (none compared) and an `action` running
 * `swcraft guard --host kiro`. An `enabled` or `description` key is Kiro's, so a
 * hook the user switched off stays off. What this does not reach: a user's
 * change to SWCraft's own values inside that shape is replaced.
 */
export function isSwcraftKiroHook(text: string): boolean {
  const parsed = parsedJson(text);
  if (
    !isPlainObject(parsed) ||
    !hasOnlyKeys(parsed, ["version", "hooks"]) ||
    typeof parsed.version !== "string"
  ) {
    return false;
  }
  const entries = parsed.hooks;
  const optionalString = (value: unknown) => value === undefined || typeof value === "string";
  return (
    Array.isArray(entries) &&
    entries.length > 0 &&
    entries.every(
      (entry: unknown) =>
        isPlainObject(entry) &&
        hasOnlyKeys(entry, ["name", "trigger", "matcher", "action", "timeout"]) &&
        optionalString(entry.name) &&
        optionalString(entry.trigger) &&
        optionalString(entry.matcher) &&
        (entry.timeout === undefined || typeof entry.timeout === "number") &&
        isPlainObject(entry.action) &&
        hasOnlyKeys(entry.action, ["type", "command"]) &&
        entry.action.type === "command" &&
        entry.action.command === KIRO_GUARD_COMMAND,
    )
  );
}

/** The one command a SWCraft Claude Code hook handler runs. */
const CLAUDE_GUARD_COMMAND = "swcraft guard --host claude";

/**
 * Whether *text* is a `.claude/settings.json` SWCraft wrote, of any release:
 * only an optional `env` (`CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS` alone, a string),
 * an optional `enabledMcpjsonServers` of `["swcraft"]`, and a required `hooks`
 * table running `swcraft guard --host claude` (`isSwcraftHookTable()`). Anything
 * else makes it the user's. What this does not reach: a user's change to
 * SWCraft's own values inside that shape is replaced.
 */
export function isSwcraftClaudeSettings(text: string): boolean {
  const parsed = parsedJson(text);
  return (
    isPlainObject(parsed) &&
    hasOnlyKeys(parsed, ["env", "enabledMcpjsonServers", "hooks"]) &&
    (parsed.env === undefined || isStringMap(parsed.env, ["CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS"])) &&
    (parsed.enabledMcpjsonServers === undefined ||
      (Array.isArray(parsed.enabledMcpjsonServers) &&
        parsed.enabledMcpjsonServers.length === 1 &&
        parsed.enabledMcpjsonServers[0] === "swcraft")) &&
    isSwcraftHookTable(parsed.hooks, CLAUDE_GUARD_COMMAND)
  );
}

/** The one command a SWCraft Codex hook handler runs. */
const CODEX_GUARD_COMMAND = "swcraft guard --host codex";

/**
 * Whether *text* is a Codex hook file SWCraft wrote, of any release: `hooks`
 * alone, a table running `swcraft guard --host codex` (`isSwcraftHookTable()`).
 * Anything more, or unparseable text, makes it the user's.
 */
export function isSwcraftCodexHook(text: string): boolean {
  const parsed = parsedJson(text);
  return (
    isPlainObject(parsed) &&
    hasOnlyKeys(parsed, ["hooks"]) &&
    isSwcraftHookTable(parsed.hooks, CODEX_GUARD_COMMAND)
  );
}

/** Where the report sends a reader for a kept client file: the guide section, by page and title. */
const CLIENT_FILES_GUIDE_POINTER = `as the guide's "${CLIENT_FILES_GUIDE_SECTION}" section (In VS Code) says`;

/**
 * The one line for a kept Codex hook file. When *kept* is the `.codex` folder
 * itself (a link or no folder), `config.toml` was not written either, so the
 * line names both and the guide section.
 */
export function keptCodexHookMessage(kept?: string): string {
  if (kept !== undefined && path.basename(kept) === CODEX_FOLDER_NAME) {
    return (
      `SWCraft: kept the existing ${CODEX_FOLDER_NAME}, which is a symbolic link or not a folder, and wrote ` +
      "nothing into it; add the SWCraft model guard's entry (`swcraft guard --host codex`) to its " +
      `hooks.json and SWCraft's server to its config.toml by hand, ${CLIENT_FILES_GUIDE_POINTER}.`
    );
  }
  return (
    `SWCraft: kept the existing ${CODEX_HOOK_RELATIVE_PATH}, which is not SWCraft's; ` +
    "add the SWCraft model guard's entry (`swcraft guard --host codex`) to it by hand."
  );
}

/**
 * Where the report sends a reader for *files*: the guide section of the first
 * table row owning one of them, else the Codex and Claude Code section.
 */
function guidePointer(table: ClientTable, files: readonly string[]): string {
  const section =
    table.clients.find((client) => client.files.some((file) => files.includes(file)))
      ?.guideSection ?? CLIENT_FILES_SECTION;
  const known = GUIDE_SECTION_TITLES.get(section);
  if (known === undefined) {
    return `as the guide's section ${section} says`;
  }
  return known.title === known.page
    ? `as the guide's "${known.page}" page says`
    : `as the guide's "${known.title}" section (${known.page}) says`;
}

/**
 * The one line for an entry `applyOnboardingPlan()` kept in *targetDir*, by its
 * project-relative path. An entry of Copilot's files only (`always-own`) can only
 * be a link or no file/folder, so the line says to move it aside and run again;
 * any other says what to add by hand, with its guide section (`guidePointer()`).
 */
export function keptMessage(targetDir: string, kept: string, table: ClientTable): string {
  const relative = projectRelative(targetDir, kept);
  if (relative === CODEX_HOOK_RELATIVE_PATH || relative === CODEX_FOLDER_NAME) {
    return keptCodexHookMessage(kept);
  }
  const inside = [...CLIENT_FILE_ASSETS.keys()].filter(
    (file) => file === relative || file.startsWith(`${relative}/`),
  );
  const isFile = inside.includes(relative);
  if (
    inside.length > 0 &&
    inside.every((file) => CLIENT_FILE_ASSETS.get(file)?.keepsForeign === "always-own")
  ) {
    return isFile
      ? `SWCraft: kept the existing ${relative}, which is a symbolic link or not a file, and wrote nothing ` +
          "through it; move it aside and run Onboard Project again to get SWCraft's file."
      : `SWCraft: kept the existing ${relative}, which is a symbolic link or not a folder, and wrote nothing ` +
          "into it; move it aside and run Onboard Project again to get SWCraft's files.";
  }
  const pointer = guidePointer(table, inside);
  if (!isFile) {
    return (
      `SWCraft: kept the existing ${relative}, which is a symbolic link or not a folder, and wrote nothing ` +
      `into it; add SWCraft's file to it by hand, ${pointer}.`
    );
  }
  return (
    `SWCraft: kept the existing ${relative}, which is not SWCraft's; add SWCraft's part to it by ` +
    `hand, ${pointer}.`
  );
}

/**
 * The sentence added when *projectDir* is in no git repository (no `.git` entry
 * in it or above it), else `undefined`. Codex finds a project's `.codex/` layer
 * from the repository root (`project_root_markers`, `.git` by default), so
 * outside one it runs no project hook. *exists* is a test seam. What this does not reach: other root markers.
 */
export function codexHookOutsideGitMessage(
  projectDir: string,
  exists: (candidate: string) => boolean = fs.existsSync,
): string | undefined {
  let dir = path.resolve(projectDir);
  for (;;) {
    if (exists(path.join(dir, ".git"))) {
      return undefined;
    }
    const parent = path.dirname(dir);
    if (parent === dir) {
      break;
    }
    dir = parent;
  }
  return (
    `SWCraft: Codex runs the model guard in ${CODEX_HOOK_RELATIVE_PATH} only in a git repository, ` +
    "and this folder is none; `git init` makes it one."
  );
}

// --- The companion `.vscode/settings.json` -------------------------------------
//
// Copilot chosen together with Codex or Claude Code also writes the table's
// `copilotCompanionSettings` (keys that stop VS Code attaching a root
// `AGENTS.md`/`CLAUDE.md` to every Copilot request), only where the file is
// absent; an existing one is the user's and is reported as kept. The one
// `swcraft init` wrote in this same run holds nothing of the user's, so it is
// completed instead.

/** What `applyCompanionSettings()` did. */
export type CompanionOutcome =
  /** Written (absent before), or `swcraft init`'s file of this run completed with the keys. */
  | { kind: "written"; destPath: string }
  /** An existing file already holding every key with the rule's value: nothing to do. */
  | { kind: "present"; destPath: string }
  /** An existing file, or a folder on the way that is a link or no folder, left as it is. */
  | { kind: "kept"; destPath: string };

/** *settings* rendered as `swcraft init` renders `.vscode/settings.json`: `json.dumps(…, indent=2)`. */
function renderSettings(settings: Record<string, unknown>): string {
  return JSON.stringify(settings, null, 2);
}

/** *destPath*'s JSON object, or `undefined` when it is no regular file or holds no JSON object. */
function readJsonObject(destPath: string): Record<string, unknown> | undefined {
  try {
    if (!fs.lstatSync(destPath).isFile()) {
      return undefined;
    }
    const parsed = parsedJson(fs.readFileSync(destPath, "utf8"));
    return isPlainObject(parsed) ? parsed : undefined;
  } catch {
    return undefined;
  }
}

/**
 * Write the companion rule's settings into *targetDir*'s `rule.file`, link-safe
 * (`writeKeepingForeign()`, `none`): written when absent; `present` when every
 * key already holds the rule's value; otherwise `kept`. With
 * *completesInitOutput* (this run's `swcraft init` wrote the file) a JSON object
 * there is completed with the keys by a rename. What this does not reach:
 * `writeKeepingForeign()`'s window, and a user who wrote the file between
 * `swcraft init` and this step.
 */
export function applyCompanionSettings(
  targetDir: string,
  rule: CompanionSettings,
  completesInitOutput: boolean,
): CompanionOutcome {
  const destPath = inProject(targetDir, rule.file);
  const linked = linkedFolderInside(targetDir, destPath);
  if (linked !== undefined) {
    return { kind: "kept", destPath: linked };
  }
  const existing = readJsonObject(destPath);
  if (
    existing !== undefined &&
    Object.entries(rule.settings).every(([key, value]) => existing[key] === value)
  ) {
    return { kind: "present", destPath };
  }
  if (completesInitOutput && existing !== undefined) {
    replaceByRename(
      Buffer.from(renderSettings({ ...existing, ...rule.settings }), "utf8"),
      destPath,
    );
    return { kind: "written", destPath };
  }
  const kept = writeKeepingForeign(
    Buffer.from(renderSettings(rule.settings), "utf8"),
    destPath,
    targetDir,
    "none",
  );
  return kept === undefined ? { kind: "written", destPath } : { kind: "kept", destPath: kept };
}

/**
 * The one line for a companion settings file *Onboard Project* kept: the file, or
 * the folder on the way that is a link or no folder, and the keys to add by hand
 * with the guide section.
 */
export function keptCompanionMessage(
  targetDir: string,
  rule: CompanionSettings,
  kept: string,
): string {
  const relative = projectRelative(targetDir, kept);
  const keys = Object.entries(rule.settings)
    .map(([key, value]) => `"${key}": ${JSON.stringify(value)}`)
    .join(" and ");
  const what =
    relative === rule.file
      ? `kept the existing ${relative}; add ${keys} to it`
      : `kept the existing ${relative}, which is a symbolic link or not a folder, and wrote nothing ` +
        `into it; add ${keys} to ${rule.file}`;
  return `SWCraft: ${what} by hand, so Copilot does not read the Codex and Claude Code files, ${CLIENT_FILES_GUIDE_POINTER}.`;
}

/** What *Onboard Project*'s closing lines report. */
export interface OnboardingReport {
  /** The chosen clients' labels, in the table's order (`clientLabelsFor()`). */
  clientLabels: readonly string[];
  /** What was written, POSIX paths relative to the project (`writtenFiles()`, then the companion file). */
  written: readonly string[];
  /** The entries `applyOnboardingPlan()` kept, in its order. */
  kept: readonly string[];
  /** The companion settings file's line when it was kept (`keptCompanionMessage()`). */
  companionKept?: string;
  /** The table's warning for each conflict the chosen set meets (`conflictWarnings()`). */
  warnings: readonly string[];
  /** The no-repository sentence (`codexHookOutsideGitMessage()`), only when the Codex hook was planned. */
  outsideGit: string | undefined;
}

/**
 * *Onboard Project*'s closing lines, the first the notification's opening: the
 * folder and clients, what was written, each kept entry, the kept companion file,
 * each conflict's warning (no question: the choice is the confirmation) and the
 * no-repository sentence.
 */
export function onboardedMessages(
  targetDir: string,
  report: OnboardingReport,
  table: ClientTable,
): string[] {
  const lines = [`SWCraft: onboarded ${targetDir} for ${report.clientLabels.join(", ")}.`];
  if (report.written.length > 0) {
    lines.push(`SWCraft: wrote ${report.written.join(", ")}.`);
  }
  for (const entry of report.kept) {
    lines.push(keptMessage(targetDir, entry, table));
  }
  if (report.companionKept !== undefined) {
    lines.push(report.companionKept);
  }
  for (const warning of report.warnings) {
    lines.push(`SWCraft: Warning: ${warning}`);
  }
  if (report.outsideGit !== undefined) {
    lines.push(report.outsideGit);
  }
  return lines;
}

// --- Onboarding never runs inside a project, or around one -------------------
//
// Scaffolding inside a folder a parent's manifest covers, or above a folder
// holding one, would nest two projects, so the command refuses and names the
// project's folder. The walk up stops at the outermost workspace folder (or the
// filesystem root); the walk down runs only when `swcraft init` would scaffold,
// and is bounded in depth and entries read. Paths are compared by
// `path.relative`, never by string prefix.
//
// What neither walk reaches: a symbolic link is not resolved (the walk down
// does not enter a linked directory), and a manifest written after the check
// is not seen.

/** True when *dir* is *ancestor* itself (`relative` is empty) or lies under it. */
function isWithin(ancestor: string, dir: string): boolean {
  const relative = path.relative(path.resolve(ancestor), path.resolve(dir));
  return !path.isAbsolute(relative) && relative !== ".." && !relative.startsWith(`..${path.sep}`);
}

/** True when *a* and *b* name the same folder (`path.relative` is empty). */
function samePath(a: string, b: string): boolean {
  return path.relative(path.resolve(a), path.resolve(b)) === "";
}

/**
 * The nearest strict ancestor of *targetDir* that holds `swcraft.yaml`, walking
 * up to and including *boundary*; `undefined` when there is none. A *targetDir*
 * that is the boundary walks nothing.
 */
export function enclosingProject(targetDir: string, boundary: string): string | undefined {
  let dir = path.resolve(targetDir);
  const stop = path.resolve(boundary);
  while (!samePath(dir, stop) && path.dirname(dir) !== dir) {
    dir = path.dirname(dir);
    if (fs.existsSync(path.join(dir, MANIFEST_FILENAME))) {
      return dir;
    }
  }
  return undefined;
}

/**
 * Where `enclosingProject` stops for *targetDir*: the outermost of
 * *workspaceFolders* containing or equal to it (so a project in an outer
 * workspace folder is still found), else the filesystem root.
 */
export function onboardingBoundary(targetDir: string, workspaceFolders: readonly string[]): string {
  let outermost: string | undefined;
  for (const folder of workspaceFolders) {
    if (!isWithin(folder, targetDir)) {
      continue;
    }
    if (outermost === undefined || isWithin(folder, outermost)) {
      outermost = path.resolve(folder);
    }
  }
  return outermost ?? path.parse(path.resolve(targetDir)).root;
}

/** The refusal's text: the chosen folder is inside the project at *parent*. */
export function nestedProjectMessage(parent: string): string {
  return `SWCraft: this folder is inside the project at ${parent}; onboard that folder.`;
}

/** How many folder levels below the chosen folder `containedProject` looks by default. */
export const CONTAINED_PROJECT_MAX_DEPTH = 3;

/** The directory names `containedProject` never enters. */
export const CONTAINED_PROJECT_SKIPPED_DIRS: readonly string[] = [
  ".git",
  "node_modules",
  ".venv",
  "dist",
];

/** How many directory entries, files included, `containedProject` reads by default. */
export const CONTAINED_PROJECT_MAX_ENTRIES = 2000;

/**
 * The first folder strictly below *targetDir*, at most *maxDepth* levels down,
 * that holds `swcraft.yaml`, as a POSIX path relative to *targetDir*; `undefined`
 * when there is none, or when more than *maxEntries* entries (files included)
 * would have to be read: a walk that stops names nothing, so the answer never
 * depends on read order. Breadth-first, skipping `CONTAINED_PROJECT_SKIPPED_DIRS`
 * and unreadable folders; each folder's entries in code-unit order, parent by
 * parent (`a/z` before `a-x/b`), so the answer is the same on every machine.
 */
export function containedProject(
  targetDir: string,
  maxDepth: number = CONTAINED_PROJECT_MAX_DEPTH,
  maxEntries: number = CONTAINED_PROJECT_MAX_ENTRIES,
): string | undefined {
  const root = path.resolve(targetDir);
  let level: string[][] = [[]];
  let read = 0;
  for (let depth = 1; depth <= maxDepth && level.length > 0; depth++) {
    const next: string[][] = [];
    for (const parts of level) {
      const listed = readSubfolders(path.join(root, ...parts), maxEntries - read);
      if (listed === "over-budget") {
        return undefined;
      }
      if (listed === undefined) {
        continue;
      }
      read += listed.count;
      const names = listed.names.sort(compareCodeUnits);
      for (const name of names) {
        const child = [...parts, name];
        if (fs.existsSync(path.join(root, ...child, MANIFEST_FILENAME))) {
          return child.join("/");
        }
        next.push(child);
      }
    }
    level = next;
  }
  return undefined;
}

/**
 * The subfolders of *dir* that `containedProject` may enter, unsorted, with how
 * many entries were read; `"over-budget"` past *budget* entries, without reading
 * the rest (at most *budget* + 1 are held); `undefined` when *dir* cannot be read.
 */
function readSubfolders(
  dir: string,
  budget: number,
): { names: string[]; count: number } | "over-budget" | undefined {
  let handle: fs.Dir;
  try {
    handle = fs.opendirSync(dir);
  } catch {
    return undefined;
  }
  const names: string[] = [];
  let count = 0;
  try {
    for (let entry = handle.readSync(); entry !== null; entry = handle.readSync()) {
      count += 1;
      if (count > budget) {
        return "over-budget";
      }
      if (entry.isDirectory() && !CONTAINED_PROJECT_SKIPPED_DIRS.includes(entry.name)) {
        names.push(entry.name);
      }
    }
  } catch {
    return undefined;
  } finally {
    handle.closeSync();
  }
  return { names, count };
}

/** The refusal's text: the chosen folder holds the project at *relative* below it. */
export function containedProjectMessage(relative: string): string {
  return `SWCraft: this folder contains the project at ${relative}; onboard that folder or choose an empty one.`;
}

/**
 * Why *targetDir* may not be onboarded, as the refusal's text; `undefined` when it
 * may. The walk up always; the walk down only when *targetDir* has no
 * `swcraft.yaml`, i.e. when `swcraft init` would scaffold there.
 */
export function onboardingRefusal(
  targetDir: string,
  workspaceFolders: readonly string[],
  maxDepth: number = CONTAINED_PROJECT_MAX_DEPTH,
  maxEntries: number = CONTAINED_PROJECT_MAX_ENTRIES,
): string | undefined {
  const parent = enclosingProject(targetDir, onboardingBoundary(targetDir, workspaceFolders));
  if (parent !== undefined) {
    return nestedProjectMessage(parent);
  }
  if (fs.existsSync(path.join(targetDir, MANIFEST_FILENAME))) {
    return undefined;
  }
  const contained = containedProject(targetDir, maxDepth, maxEntries);
  return contained === undefined ? undefined : containedProjectMessage(contained);
}

// --- The missing-profile notice -----------------------------------------------
//
// On activation, a project without the SWCraft Architect agent is offered it,
// unless dismissed for the workspace or not using Copilot (`clientInUse()`).
// Unlike *Onboard Project*, the notice never overwrites a file and never runs
// `swcraft init`: it writes the table's Copilot row only where absent.

/** The `workspaceState` key under which "Not for this workspace" is remembered. */
export const AGENT_PROFILE_NOTICE_DISMISSED_KEY = "swcraft.agentProfileNotice.dismissed";

/** The notice's text and its two choices. */
export const AGENT_PROFILE_NOTICE =
  `SWCraft: this project has no SWCraft Architect agent (${AGENT_PROFILE_RELATIVE_PATH}), ` +
  "the Copilot agent that answers and changes the model through the SWCraft tools, " +
  "with read-only file access for a SWCraft answer saved to a file.";
export const ADD_AGENT_PROFILE_CHOICE = "Add the SWCraft Architect agent";
export const DISMISS_AGENT_PROFILE_CHOICE = "Not for this workspace";

/** What the activation-time decision reads; every field is a fact the adapter gathered. */
export interface AgentProfileNoticeInput {
  /** The folder whose `swcraft.yaml` `findManifest()` found, or `undefined` for no project. */
  projectDir: string | undefined;
  /** Whether `agentProfilePath(projectDir)` exists. */
  profileExists: boolean;
  /** The `workspaceState` value under `AGENT_PROFILE_NOTICE_DISMISSED_KEY`. */
  dismissed: boolean;
  /**
   * Whether the project uses Copilot, `agentClients.ts`'s `clientInUse()` for the
   * notice's client: a file of its row is present, or no other client's file is.
   */
  copilotInUse: boolean;
}

/** Whether to show the notice: a SWCraft project that uses Copilot, no profile, not dismissed. */
export function shouldOfferAgentProfile(input: AgentProfileNoticeInput): boolean {
  return (
    input.projectDir !== undefined && input.copilotInUse && !input.profileExists && !input.dismissed
  );
}

/** What the adapter does with the notice's answer; closing it without a choice is `nothing`. */
export type AgentProfileNoticeAction = "apply" | "remember" | "nothing";

/**
 * The notice's answer, decided: *Add* applies the plan, *Not for this workspace*
 * is remembered, and closing it does nothing, so it is offered again next time.
 */
export function agentProfileNoticeAction(choice: string | undefined): AgentProfileNoticeAction {
  if (choice === ADD_AGENT_PROFILE_CHOICE) {
    return "apply";
  }
  return choice === DISMISS_AGENT_PROFILE_CHOICE ? "remember" : "nothing";
}

/**
 * Execute the notice's *plan* without overwriting anything or writing through a
 * link: a destination holding any entry, a dangling link included, is skipped
 * (`entryExists()`); the write is exclusive (`writeFileExclusive()`, since a
 * plain `wx` create on Windows follows a dangling link); a copy under a folder
 * that is a link or no folder is skipped. Returns the destinations written; any
 * other failure throws. What this does not reach: `writeKeepingForeign()`'s
 * window and `writeFileExclusive()`'s own limits.
 */
export function applyAgentProfilePlan(plan: OnboardingPlan, sourceAssetsDir: string): string[] {
  const written: string[] = [];
  for (const copy of plan.fileCopies) {
    const content = fs.readFileSync(path.join(sourceAssetsDir, copy.assetName));
    if (linkedFolderInside(plan.targetDir, copy.destPath) !== undefined) {
      continue;
    }
    if (entryExists(copy.destPath)) {
      continue;
    }
    fs.mkdirSync(path.dirname(copy.destPath), { recursive: true });
    try {
      writeFileExclusive(copy.destPath, content);
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === "EEXIST") {
        continue;
      }
      throw error;
    }
    written.push(copy.destPath);
  }
  return written;
}

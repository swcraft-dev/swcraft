// Whether the `swcraft` that Codex's, Claude Code's and Kiro's chats will start is the one
// SWCraft uses.
//
// The project's client server files start the SWCraft server as a bare `swcraft` (a portable
// line), which the client resolves through the `PATH` VS Code was started with (inferred,
// not observed, for Claude Code and Kiro). An older `swcraft` first on that `PATH` refuses
// the project's `toolchain_version` pin, and the chat says only that the tools are
// unavailable. This module decides whether to say so, in ONE warning. Pure: every reading is
// handed in by `commands.ts` (`checkClientSwcraftOnPath()`).
//
// **Which files trigger it.** The client table's `server` files naming a bare `swcraft`
// (`namesBareSwcraft()`; the TOML check is textual, so a `command` spelled another way draws
// no warning). Hook files are left out: Codex's and Claude Code's hooks run on the login
// shell's or the hook's `PATH`, not VS Code's; Copilot's is *Check Editor Tooling*'s row.
//
// **When it warns.** SWCraft's own executable (`resolveSwcraft()`) is compared, after
// resolving links, with where a bare `swcraft` resolves (`resolveBareCommand()`): nothing
// resolves, or another file resolves, **even with the same version** (this check cannot show
// it is the same build). Both versions are probed and named. The same file, or an absent own
// executable, draws no warning (*Check Editor Tooling*'s toolchain row reports those).
//
// What it does **not** reach:
// - the **hooks' `PATH`**: an older `swcraft` there answers the guard with exit 2 and
//   blocks every tool, and this check does not see it;
// - a VS Code whose `PATH` changes **after it started**;
// - a client that **sets its own `PATH`** (or `env.PATH` in its entry), or a user-level
//   server that shadows the project's;
// - the **build**: versions are compared as `--version` strings;
// - whether the client is installed, trusted, or starts the server at all;
// - **Claude Code on Windows**: a lookup through a shell or the current folder is unverified.

import * as path from "node:path";

import { type ClientServerFile, type ClientTable, clientServerFiles } from "./agentClients";
import type { VersionProbe } from "./healthCheck";
import {
  CLIENT_FILES_GUIDE_SECTION,
  hasOnboardMarker,
  KIRO_MCP_JSON_RELATIVE_PATH,
  MCP_JSON_RELATIVE_PATH,
  TOML_MARKER_LINE,
} from "./onboarding";
import { type Platform, parseSwcraftVersion, type SwcraftResolution, samePath } from "./toolchain";

export type { ClientServerFile } from "./agentClients";

/** The client server files that are JSON with an `mcpServers` object (`namesBareSwcraft()`). */
const JSON_SERVER_FILES: readonly string[] = [MCP_JSON_RELATIVE_PATH, KIRO_MCP_JSON_RELATIVE_PATH];

/** A line of a TOML file setting `command` to a bare `swcraft` (`namesBareSwcraft()`). */
const TOML_BARE_SWCRAFT_COMMAND = /^\s*command\s*=\s*["']swcraft["']\s*$/m;

/**
 * Whether *text*, the content of the client server file *relativePath*, names a bare
 * `swcraft` as its server's command: the JSON files by any server whose `command` is
 * `swcraft`, `.codex/config.toml` by SWCraft's marker line or a `command = "swcraft"` line.
 */
export function namesBareSwcraft(relativePath: string, text: string): boolean {
  if (JSON_SERVER_FILES.includes(relativePath)) {
    let parsed: unknown;
    try {
      parsed = JSON.parse(text);
    } catch {
      return false;
    }
    const servers =
      typeof parsed === "object" && parsed !== null
        ? (parsed as { mcpServers?: unknown }).mcpServers
        : undefined;
    return (
      typeof servers === "object" &&
      servers !== null &&
      Object.values(servers).some(
        (server: unknown) =>
          typeof server === "object" &&
          server !== null &&
          (server as { command?: unknown }).command === "swcraft",
      )
    );
  }
  return hasOnboardMarker(text, TOML_MARKER_LINE) || TOML_BARE_SWCRAFT_COMMAND.test(text);
}

/**
 * The client server files *projectDir* holds that name a bare `swcraft`, in the client
 * table's order; *readText* answers `undefined` for an absent or unreadable file. An empty
 * list means no check.
 */
export function presentClientServerFiles(
  projectDir: string,
  table: ClientTable,
  readText: (candidate: string) => string | undefined,
): ClientServerFile[] {
  return clientServerFiles(table).filter((file) => {
    const text = readText(path.join(projectDir, ...file.relativePath.split("/")));
    return text !== undefined && namesBareSwcraft(file.relativePath, text);
  });
}

/** A version reading as the warning renders it. */
export type VersionReading = { version: string } | { failed: string };

/** What the comparison found. */
export type ClientSwcraftFinding =
  | { kind: "not-applicable" }
  | { kind: "same-file" }
  | { kind: "missing"; own: string; ownVersion: VersionReading }
  | {
      kind: "different";
      own: string;
      ownVersion: VersionReading;
      onPath: string;
      pathVersion: VersionReading;
    };

/** Everything `checkClientSwcraft()` decides from; the adapter does all the reading. */
export interface ClientSwcraftInput {
  /** `presentClientServerFiles()`'s answer. */
  clientFiles: readonly ClientServerFile[];
  /** The executable SWCraft spawns, `resolveSwcraft()`'s answer. */
  own: SwcraftResolution;
  /** Where a bare `swcraft` resolves, `resolveBareCommand()`'s answer. */
  onPath: string | undefined;
  platform: Platform;
  /** Resolves links (`fs.realpathSync`); may throw, and then the path is compared as given. */
  realpath: (candidate: string) => string;
  /** `<executable> --version`. */
  probe: (executable: string) => Promise<VersionProbe>;
}

/**
 * Whether *a* and *b* are one file: each resolved through *realpath* (a link to
 * the file is the file), then compared as `samePath()` compares — by case on
 * POSIX, not on Windows. A path *realpath* cannot resolve is compared as given.
 */
export function sameFile(
  a: string,
  b: string,
  platform: Platform,
  realpath: (candidate: string) => string,
): boolean {
  const resolve = (candidate: string) => {
    try {
      return realpath(candidate);
    } catch {
      return candidate;
    }
  };
  return samePath(resolve(a), resolve(b), platform);
}

/** A probe's result as a reading: the version, or the first line it printed. */
export function versionReading(probe: VersionProbe): VersionReading {
  const version = probe.ok ? parseSwcraftVersion(probe.output) : undefined;
  if (version !== undefined) {
    return { version };
  }
  const firstLine = probe.output
    .split(/\r?\n/)
    .map((line) => line.trim())
    .find((line) => line !== "");
  return { failed: firstLine ?? "no output" };
}

/** The comparison; probes run only where the warning will name a version. */
export async function checkClientSwcraft(input: ClientSwcraftInput): Promise<ClientSwcraftFinding> {
  if (input.clientFiles.length === 0 || !input.own.exists) {
    return { kind: "not-applicable" };
  }
  const own = input.own.executable;
  if (input.onPath === undefined) {
    return { kind: "missing", own, ownVersion: versionReading(await input.probe(own)) };
  }
  if (sameFile(input.onPath, own, input.platform, input.realpath)) {
    return { kind: "same-file" };
  }
  const [ownProbe, pathProbe] = await Promise.all([input.probe(own), input.probe(input.onPath)]);
  return {
    kind: "different",
    own,
    ownVersion: versionReading(ownProbe),
    onPath: input.onPath,
    pathVersion: versionReading(pathProbe),
  };
}

function joinAnd(items: readonly string[]): string {
  return items.length <= 1
    ? (items[0] ?? "")
    : `${items.slice(0, -1).join(", ")} and ${items[items.length - 1]}`;
}

function describe(executable: string, reading: VersionReading): string {
  return "version" in reading
    ? `${executable} (version ${reading.version})`
    : `${executable}, which did not answer \`swcraft --version\` (${reading.failed})`;
}

/**
 * The remedy every warning ends with: the reliable check is this warning not coming back
 * after a restart; `swcraft --version` in VS Code's terminal is only a hint.
 */
export function clientSwcraftRemedy(ownVersion: VersionReading): string {
  const release =
    "version" in ownVersion ? `version ${ownVersion.version}` : "the release SWCraft uses";
  return (
    "Quit VS Code and start it again so that its PATH puts SWCraft's `swcraft` first — on " +
    "macOS from the Dock or Finder, which reads your login shell's PATH. The check is that " +
    "this warning does not come back; on Windows and Linux it is the same check, once VS Code " +
    "is started so that its PATH has SWCraft's `swcraft` first (tried on macOS only). " +
    "`swcraft --version` in VS Code's own terminal is only " +
    `a hint: that terminal's shell re-reads your profile, so it can show ${release} while ` +
    "the PATH the chats inherit still has the old one. The guide's " +
    `"${CLIENT_FILES_GUIDE_SECTION}" section (In VS Code) says more.`
  );
}

/**
 * The one warning for *finding*, or `undefined` when there is nothing to say.
 * It names the clients and files that triggered it, what a bare `swcraft`
 * resolved to and its version, and what SWCraft uses and its version.
 */
export function clientSwcraftWarning(
  finding: ClientSwcraftFinding,
  clientFiles: readonly ClientServerFile[],
): string | undefined {
  if (finding.kind === "not-applicable" || finding.kind === "same-file") {
    return undefined;
  }
  const clients = joinAnd(clientFiles.map((file) => file.client));
  const files = joinAnd(clientFiles.map((file) => file.relativePath));
  const verb = clientFiles.length === 1 ? "starts" : "start";
  const lines =
    `This project's ${files} ${verb} the SWCraft server as a bare \`swcraft\`, looked up on ` +
    "the PATH VS Code was started with";
  const own = describe(finding.own, finding.ownVersion);
  if (finding.kind === "missing") {
    return (
      `SWCraft: ${clients} in this window will find no \`swcraft\`. ${lines}, and no ` +
      `\`swcraft\` is on it; SWCraft uses ${own}. ${clientSwcraftRemedy(finding.ownVersion)}`
    );
  }
  const sameVersion =
    "version" in finding.ownVersion &&
    "version" in finding.pathVersion &&
    finding.ownVersion.version === finding.pathVersion.version;
  const consequence = sameVersion
    ? "The versions agree, but it is another installation, so the chat would not run what " +
      "SWCraft runs."
    : "A `swcraft` of another release refuses the project's toolchain pin, and the chat then " +
      "says only that the tools are unavailable.";
  return (
    `SWCraft: ${clients} in this window would start a different \`swcraft\` from SWCraft's. ` +
    `${lines}, which resolves it to ${describe(finding.onPath, finding.pathVersion)}; SWCraft ` +
    `uses ${own}. ${consequence} ${clientSwcraftRemedy(finding.ownVersion)}`
  );
}

/**
 * The trigger the adapter calls on activation and after *Onboard Project*: *read* answers
 * the warning for a project folder (or `undefined`), *show* raises it. **At most one
 * warning per window.** A trigger arriving while a check runs is run once more after it,
 * with the latest folder, so a check started before *Onboard Project* wrote the client
 * files cannot swallow the check of what it wrote.
 */
export function clientSwcraftTrigger(
  read: (projectDir: string) => Promise<string | undefined>,
  show: (message: string) => void,
): (projectDir: string) => Promise<void> {
  let shown = false;
  let running: Promise<void> | undefined;
  let pending: string | undefined;
  const runFrom = async (projectDir: string): Promise<void> => {
    let next: string | undefined = projectDir;
    while (next !== undefined && !shown) {
      const current: string = next;
      pending = undefined;
      const message = await read(current);
      if (message !== undefined && !shown) {
        shown = true;
        show(message);
      }
      next = pending;
    }
    pending = undefined;
  };
  return (projectDir) => {
    if (shown) {
      return Promise.resolve();
    }
    if (running !== undefined) {
      pending = projectDir;
      return running;
    }
    running = runFrom(projectDir).finally(() => {
      running = undefined;
    });
    return running;
  };
}

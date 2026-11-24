// Where the SWCraft executable is.
//
// There is **one** SWCraft on a machine: the installation the workstation archive's
// `install.py` wrote under the user's profile, with `current` linked at the installed
// version. This module is the only one that answers where that executable is.
//
// Pure: no `vscode` import; `commands.ts` passes `process.env`, `os.homedir()` and
// `fs.existsSync`.
//
// What it does **not** do: it never runs the executable, so "exists" is a filesystem
// answer only; whether it starts and what version it is, is `healthCheck.ts`'s
// `checkToolchain()`.

import * as path from "node:path";

/** Which platform's path layout and environment a resolution is for. */
export type Platform = "windows" | "posix";

/**
 * The command as a person types it, and as the output channel echoes it.
 *
 * The spawn uses the absolute path this module resolves; the echo prints this name, the
 * line a reader can retype in a terminal.
 */
export const SWCRAFT_COMMAND_NAME = "swcraft";

/**
 * The executable override, the one machine-scoped setting this extension
 * contributes; the other settings are the experiment kinds' configuration files
 * (`EXPERIMENT_KINDS` in `commandCore.ts`).
 */
export const EXECUTABLE_SETTING = "swcraft.executable";

/** The environment block a resolution reads, as `process.env` shapes it. */
export type Environment = Readonly<Record<string, string | undefined>>;

/** `%LOCALAPPDATA%\SWCraft`'s last segment. */
const WINDOWS_HOME_DIRNAME = "SWCraft";
/** `~/.swcraft`. */
const POSIX_HOME_DIRNAME = ".swcraft";
/** The link the installer switches on every update; the extension follows it. */
const CURRENT_LINK_NAME = "current";

/**
 * The `path` flavour *platform* spells paths with.
 *
 * The cast is needed because TypeScript types `path.posix` and `path.win32` as unrelated
 * objects although every caller uses `join` alone. *platform* is the **target** platform,
 * so a Windows resolution can be built and asserted from macOS.
 */
export function platformPath(platform: Platform): typeof path.win32 {
  return platform === "windows" ? path.win32 : (path.posix as unknown as typeof path.win32);
}

/**
 * *env*'s `PATH`, split into directories the way *platform*'s shell reads it.
 *
 * Empty entries are dropped and a quoted entry is unquoted; an absent or blank `PATH` is
 * an empty list. Shared by `resolveOnPath()`, `resolveBareCommand()` and
 * `toolchainInstall.ts`'s `pathLookup()`; the lookups themselves differ in the names they
 * try and stay separate.
 *
 * What it does **not** reach: an entry is not expanded (a literal `~` stays), and the list
 * is the **extension host's** environment, so a venv activated in an integrated terminal
 * never reaches it.
 */
export function pathDirectories(env: Environment, platform: Platform): string[] {
  const raw = env.PATH ?? env.Path;
  const separator = platform === "windows" ? ";" : ":";
  return (raw ?? "")
    .split(separator)
    .map((entry) => entry.trim().replace(/^"|"$/g, ""))
    .filter((entry) => entry !== "");
}

/**
 * The installation home: `%LOCALAPPDATA%\SWCraft` on Windows, `~/.swcraft` elsewhere,
 * the layout `scripts/workstation/install.py`'s `default_home()` writes (the two must not
 * drift). *env* is passed in so a resolution for the other platform can be built.
 *
 * **The one divergence from the installer.** `install.py` refuses when neither
 * `LOCALAPPDATA` nor `USERPROFILE` is set; this falls back to *homeDir*, because every
 * command and the health check call it, and a throwing resolver would turn one diagnosable
 * finding into an exception on every surface.
 */
export function toolchainHome(env: Environment, homeDir: string, platform: Platform): string {
  const p = platformPath(platform);
  if (platform !== "windows") {
    return p.join(homeDir, POSIX_HOME_DIRNAME);
  }
  const local = env.LOCALAPPDATA;
  if (local !== undefined && local.trim() !== "") {
    return p.join(local, WINDOWS_HOME_DIRNAME);
  }
  const profile = env.USERPROFILE;
  const base = profile !== undefined && profile.trim() !== "" ? profile : homeDir;
  return p.join(base, "AppData", "Local", WINDOWS_HOME_DIRNAME);
}

/**
 * The executable inside an installation home: `<home>\current\Scripts\swcraft.exe` on
 * Windows, `<home>/current/bin/swcraft` elsewhere.
 *
 * Through `current`, never `versions/<v>`: the installer installs a new version beside the
 * old one and then switches the link.
 */
export function installedSwcraft(home: string, platform: Platform): string {
  const p = platformPath(platform);
  return platform === "windows"
    ? p.join(home, CURRENT_LINK_NAME, "Scripts", "swcraft.exe")
    : p.join(home, CURRENT_LINK_NAME, "bin", SWCRAFT_COMMAND_NAME);
}

/** Where the resolved executable came from — what a health message must name. */
export type ExecutableSource = "setting" | "installation";

export interface SwcraftResolution {
  /** Absolute path to the executable every SWCraft command spawns. */
  executable: string;
  source: ExecutableSource;
  /** Whether that path is on disk. A filesystem answer only — see the file header. */
  exists: boolean;
}

/**
 * The executable every CLI-spawning command runs, and where it came from.
 *
 * The `swcraft.executable` setting when it is non-blank, the installation otherwise; the
 * two are never both probed. That makes it an *override*: a fallback probe would let a
 * mistyped setting silently resolve to the installation, and the health check could name
 * neither the path given nor why it was ignored.
 */
export function resolveSwcraft(
  setting: string | undefined,
  env: Environment,
  homeDir: string,
  platform: Platform,
  exists: (candidate: string) => boolean,
): SwcraftResolution {
  const override = setting?.trim();
  if (override !== undefined && override !== "") {
    return { executable: override, source: "setting", exists: exists(override) };
  }
  const executable = installedSwcraft(toolchainHome(env, homeDir, platform), platform);
  return { executable, source: "installation", exists: exists(executable) };
}

/**
 * The executable names a bare `swcraft` can resolve to, per platform.
 *
 * Windows tries `swcraft.exe` (the installation's venv) and `swcraft.cmd` (a wrapper-style
 * install), not the full `%PATHEXT%`; see `resolveOnPath()`.
 */
const PATH_EXECUTABLE_NAMES: Record<Platform, readonly string[]> = {
  windows: ["swcraft.exe", "swcraft.cmd"],
  posix: [SWCRAFT_COMMAND_NAME],
};

/**
 * Where a bare `swcraft` resolves in *env*'s `PATH`, or `undefined` when it resolves
 * nowhere.
 *
 * It asks "where does it resolve", not "is the installation's directory on `PATH`": a
 * `PATH` listing another environment's `bin` first resolves `swcraft` there although the
 * installation's directory is listed further along.
 *
 * What it does **not** reach: the **integrated terminal's** `PATH` (a venv activated there
 * never reaches the extension host's environment, fixed when the window opened);
 * `%PATHEXT%` is not read; an entry is not expanded (see `pathDirectories()`); and it asks
 * only whether the file exists, not whether it is executable (a mode bit `fs.existsSync`
 * does not read).
 */
export function resolveOnPath(
  env: Environment,
  platform: Platform,
  exists: (candidate: string) => boolean,
): string | undefined {
  const p = platformPath(platform);
  for (const directory of pathDirectories(env, platform)) {
    for (const name of PATH_EXECUTABLE_NAMES[platform]) {
      const candidate = p.join(directory, name);
      if (exists(candidate)) {
        return candidate;
      }
    }
  }
  return undefined;
}

/** `%PATHEXT%`'s value when the environment carries none — Windows' own default list. */
const DEFAULT_PATHEXT = ".COM;.EXE;.BAT;.CMD";

/**
 * Where a bare *name* resolves in *env*'s `PATH` by the target platform's own lookup rules,
 * or `undefined` when it resolves nowhere (`clientSwcraft.ts`'s check).
 *
 * Unlike `resolveOnPath()`, this models what *another program* (Codex's or Claude Code's
 * extension, inheriting the host's environment) resolves: on Windows every `%PATHEXT%`
 * extension in order (the variable read in any case; Windows' default list when absent or
 * blank), each directory before the next; on POSIX the bare name. *isExecutable* is the
 * adapter's `fs.accessSync` with `X_OK` (existence on Windows), so a non-executable file
 * on POSIX is passed over, as a shell passes it over.
 *
 * What it does **not** reach: a Windows lookup that searches the current directory first
 * (`cmd.exe`'s) is not modelled; an entry is not expanded; and a client that sets its own
 * `PATH`, or runs the command through a login shell, resolves something else.
 */
export function resolveBareCommand(
  name: string,
  env: Environment,
  platform: Platform,
  isExecutable: (candidate: string) => boolean,
): string | undefined {
  const p = platformPath(platform);
  // Windows variable names are case-insensitive (Node's `process.env` reads them so there);
  // a plain object handed in is searched by any spelling, `PATHEXT` first.
  const declared =
    env.PATHEXT ?? Object.entries(env).find(([key]) => key.toUpperCase() === "PATHEXT")?.[1];
  const pathext = declared === undefined || declared.trim() === "" ? DEFAULT_PATHEXT : declared;
  const names =
    platform === "windows"
      ? pathext
          .split(";")
          .map((extension) => extension.trim())
          .filter((extension) => extension !== "")
          .map((extension) => `${name}${extension}`)
      : [name];
  for (const directory of pathDirectories(env, platform)) {
    for (const candidateName of names) {
      const candidate = p.join(directory, candidateName);
      if (isExecutable(candidate)) {
        return candidate;
      }
    }
  }
  return undefined;
}

/**
 * Whether two paths name the same file, as far as a string comparison can say.
 *
 * Windows is compared case-insensitively with separators normalized (both differ freely
 * between an environment block and a composed path); POSIX as given. Links, `..` segments
 * and 8.3 short names are not resolved, so two spellings of one file can compare unequal,
 * which is why the caller warns rather than fails.
 */
export function samePath(a: string, b: string, platform: Platform): boolean {
  if (platform !== "windows") {
    return a === b;
  }
  const normalize = (value: string) => value.replace(/\//g, "\\").toLowerCase();
  return normalize(a) === normalize(b);
}

/**
 * The version in `swcraft --version`'s output, or `undefined` when there is none to read.
 *
 * The CLI prints argparse's `%(prog)s %(version)s`, so the version is the last token of the
 * first non-empty line (a bare version reads the same way). A token that does not look like
 * a version (a traceback, an empty stdout, a banner) gives `undefined`, not a guess.
 */
export function parseSwcraftVersion(output: string): string | undefined {
  const line = output
    .split(/\r?\n/)
    .map((candidate) => candidate.trim())
    .find((candidate) => candidate !== "");
  if (line === undefined) {
    return undefined;
  }
  const token = line.split(/\s+/).at(-1);
  if (token === undefined || !/^\d+(\.\d+)*([.a-zA-Z0-9+-]*)$/.test(token)) {
    return undefined;
  }
  return token;
}

/**
 * The PEP 440 **release segment** of *value* as numbers, or `undefined` when *value* does
 * not start with one.
 *
 * The epoch, a pre/post/dev suffix and a local version are discarded, so
 * `compareReleases()` reads `0.7.0rc1` and `0.7.0` as the same version: an rc archive over
 * its own final release is a repair rather than a refusal.
 */
function releaseSegment(value: string): number[] | undefined {
  const match = /^\s*v?(?:\d+!)?(\d+(?:\.\d+)*)/.exec(value);
  return match === null ? undefined : match[1].split(".").map((part) => Number(part));
}

/**
 * `1` when *a* is a newer release than *b*, `-1` when older, `0` when the same, and
 * `undefined` when either is not a version.
 *
 * Numeric per segment (`0.10.0` is newer than `0.9.0`); missing segments read as zero, as
 * PEP 440 compares `1.0` with `1.0.0`.
 */
export function compareReleases(a: string, b: string): number | undefined {
  const left = releaseSegment(a);
  const right = releaseSegment(b);
  if (left === undefined || right === undefined) {
    return undefined;
  }
  for (let index = 0; index < Math.max(left.length, right.length); index += 1) {
    const difference = (left[index] ?? 0) - (right[index] ?? 0);
    if (difference !== 0) {
      return difference > 0 ? 1 : -1;
    }
  }
  return 0;
}

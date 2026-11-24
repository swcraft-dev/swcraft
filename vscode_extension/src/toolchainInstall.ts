// The two toolchain commands' decisions.
//
// `SWCraft: Install Toolchain` and `SWCraft: Update Toolchain` run the **archive's own**
// installer: the extension embeds neither `install.py` nor a wheel nor an interpreter; it
// unpacks what the user points it at and starts the archive's own wrapper. A second copy of
// the installer in the extension would have nothing keeping the two in step.
//
// Pure: no `vscode` import. `commands.ts` is the adapter: picker, temporary directory,
// spawn, streamed output, notifications.
//
// **What this module does not reach.** It never runs anything and never touches a disk
// (`which` and the directory listing are handed in), so "this is an archive" is a filename
// answer; the installer's `SHA256SUMS` check decides whether the contents are intact. The
// Windows half (`tar.exe` unpack, `cmd.exe` start, the wrapper, the installer) is not
// measured by this suite.

import {
  compareReleases,
  type Environment,
  type Platform,
  pathDirectories,
  platformPath,
} from "./toolchain";

/** The command ids, single-sourced so `package.json` and the tests can agree. */
export const INSTALL_TOOLCHAIN_COMMAND_ID = "swcraft.installToolchain";
export const UPDATE_TOOLCHAIN_COMMAND_ID = "swcraft.updateToolchain";

/**
 * `package.json`'s title for `UPDATE_TOOLCHAIN_COMMAND_ID`, as the sentences that send a
 * reader to the archive flow quote it. `healthCheck.ts` spells it as a literal (it may
 * import `toolchain.ts` alone), and `healthCheck.test.ts` holds it to this constant.
 */
export const UPDATE_TOOLCHAIN_TITLE = "SWCraft: Update Toolchain";

/**
 * The archive's own wrappers, at the root of the unpacked archive
 * (`scripts/workstation/swcraft_install.sh` and `swcraft_install.cmd`).
 *
 * The commands start **these**, never `install.py`: each wrapper checks the interpreter the
 * archive carries against `SHA256SUMS`, unpacks it into `.bootstrap/python` and runs
 * `install.py` on it with every argument passed through, so the machine needs no Python.
 */
export const POSIX_WRAPPER_FILENAME = "swcraft_install.sh";
export const WINDOWS_WRAPPER_FILENAME = "swcraft_install.cmd";

/** The archive members whose names carry the version — `install.py`'s two shapes. */
const VSIX_PREFIX = "swcraft-vscode-";
const VSIX_SUFFIX = ".vsix";
const WHEEL_SUFFIX = ".whl";
const WHEEL_DISTRIBUTIONS = ["swcraft_tools", "swcraft_mcp", "swcraft_agent"];

/**
 * `%SystemRoot%\System32`, the directory both Windows programs this module names are taken
 * from by full path. Without `SystemRoot` it falls back to `C:\Windows` rather than a
 * `PATH` lookup, which is what the full path avoids.
 */
function windowsSystemDirectory(env: Environment): string {
  const root = env.SystemRoot ?? env.SYSTEMROOT;
  const base = root !== undefined && root.trim() !== "" ? root : "C:\\Windows";
  return platformPath("windows").join(base, "System32");
}

/** The program that opens a picked `.zip`, and which of the two command lines it takes. */
export interface ZipUnpacker {
  program: string;
  flavour: "tar" | "unzip";
}

export type UnpackerLookup = { unpacker: ZipUnpacker } | { refusal: string };

/**
 * The program that unpacks a picked `.zip` into a temporary directory (Node ships no zip
 * reader, and the machine is not asked to have a Python):
 *
 * - **Windows**: `%SystemRoot%\System32\tar.exe` (bsdtar, which reads a zip), by full path
 *   and never looked up: a GNU tar earlier on `PATH` (as Git for Windows puts there) reads
 *   `C:` as a remote host. The wrapper needs this program anyway.
 * - **elsewhere**: `unzip` on the `PATH` VS Code was started with (macOS ships it in
 *   `/usr/bin`); a Linux host may lack it, and then this refuses and names the folder
 *   route, which needs no unpacker. The refusal says **whose** `PATH` was searched.
 *
 * What it does **not** reach: Windows is unmeasured; how either program treats an absolute
 * or `..` member name is that program's (both strip rather than refuse); a doctored archive
 * is refused by the `SHA256SUMS` check that follows.
 */
export function findZipUnpacker(
  env: Environment,
  platform: Platform,
  which: (name: string) => string | undefined,
): UnpackerLookup {
  if (platform === "windows") {
    return {
      unpacker: {
        program: platformPath("windows").join(windowsSystemDirectory(env), "tar.exe"),
        flavour: "tar",
      },
    };
  }
  const unzip = which("unzip");
  if (unzip !== undefined && unzip !== "") {
    return { unpacker: { program: unzip, flavour: "unzip" } };
  }
  return {
    refusal:
      "SWCraft: no `unzip` was found on the PATH VS Code was started with, so the archive zip " +
      "cannot be unpacked here — nothing was installed. Unpack it yourself, run this command " +
      `again and choose *${UNPACKED_ARCHIVE_FOLDER_LABEL}*.`,
  };
}

/**
 * The executable extensions a Windows lookup appends; not `%PATHEXT%`.
 *
 * No `.cmd` or `.bat`: the resolved path goes into `spawn(argv[0], …)` with no shell, and
 * Node >= 18.20.2 / 20.12.2 refuses a batch file without `shell: true` (the CVE-2024-27980
 * fix) with `spawn … EINVAL`. Offering a name that cannot start is worse than the refusal
 * for an absent program; the same holds for `.ps1` or `.py`.
 */
const WINDOWS_EXECUTABLE_SUFFIXES = ["", ".exe"];

/**
 * A `which` over *env*'s `PATH`, the lookup `findZipUnpacker()` is handed, so the "no
 * `unzip`" refusal fires before the user picks an archive rather than as an ENOENT after.
 *
 * What it does **not** reach, as with `toolchain.ts`'s `resolveOnPath()`: a `PATH` entry is
 * not expanded, and existence is not executability (a directory named `unzip` would answer
 * here and fail at spawn).
 */
export function pathLookup(
  env: Readonly<Record<string, string | undefined>>,
  platform: Platform,
  exists: (candidate: string) => boolean,
): (name: string) => string | undefined {
  const suffixes = platform === "windows" ? WINDOWS_EXECUTABLE_SUFFIXES : [""];
  const directories = pathDirectories(env, platform);
  const p = platformPath(platform);
  return (name: string) => {
    for (const directory of directories) {
      for (const suffix of suffixes) {
        const candidate = p.join(directory, name + suffix);
        if (exists(candidate)) {
          return candidate;
        }
      }
    }
    return undefined;
  };
}

/**
 * The characters `cmd.exe` re-reads on the line after `/c`: the same set as
 * `scripts/workstation/install.py`'s `CMD_REPARSED_CHARACTERS`, held equal by a test.
 */
export const CMD_REPARSED_CHARACTERS = '&|<>^%"';

export type InstallerArgv = { argv: string[] } | { refusal: string };

/**
 * The argv that runs the archive's own wrapper with `--home <default>` (first element: the
 * executable to spawn), or a refusal.
 *
 * - **POSIX**: `/bin/sh <archive>/swcraft_install.sh --home <home>`, so lost mode bits do
 *   not matter.
 * - **Windows**: `%SystemRoot%\System32\cmd.exe /d /c call <archive>\swcraft_install.cmd
 *   --home <home>` (`spawn` needs a shell for a batch file; `install.py`'s `code_argv()`
 *   rule). `call` keeps `cmd.exe` from stripping the quotes of a path with a space; `/d`
 *   skips the registry's `AutoRun` commands.
 *
 * `--home` is passed although it is the default: the echoed line can be retyped, the
 * extension's and `install.py`'s homes cannot silently diverge, and the Windows wrapper
 * pauses for a key only when handed no argument. **A path `cmd.exe` would re-read is
 * refused** rather than escaped (`install.py`'s `run_argv()` reason): Node quotes for the C
 * runtime and `cmd.exe` re-parses, so `C:\a&b` would become two commands.
 *
 * What it does **not** reach: whether `cmd.exe` reads the line as documented (unmeasured on
 * Windows), and the wrapper's own handling of its arguments.
 */
export function buildInstallerArgv(
  archiveRoot: string,
  home: string,
  platform: Platform,
  env: Environment,
): InstallerArgv {
  const p = platformPath(platform);
  if (platform !== "windows") {
    return { argv: ["/bin/sh", p.join(archiveRoot, POSIX_WRAPPER_FILENAME), "--home", home] };
  }
  const wrapper = p.join(archiveRoot, WINDOWS_WRAPPER_FILENAME);
  const reparsed = (part: string) => [...CMD_REPARSED_CHARACTERS].find((c) => part.includes(c));
  const inWrapper = reparsed(wrapper);
  if (inWrapper !== undefined) {
    return {
      refusal:
        `SWCraft: the installer was not started, because ${wrapper} carries \`${inWrapper}\`, which ` +
        "cmd.exe reads as part of a command rather than of a path. Unpack the archive into a " +
        "folder whose path does not carry it, and point this command at that folder.",
    };
  }
  const inHome = reparsed(home);
  if (inHome !== undefined) {
    return {
      refusal:
        `SWCraft: the installer was not started, because the installation home ${home} carries ` +
        `\`${inHome}\`, which cmd.exe reads as part of a command rather than of a path — and ` +
        "the extension looks for the installation there and nowhere else, so this command cannot " +
        "install on this account.",
    };
  }
  return {
    argv: [
      p.join(windowsSystemDirectory(env), "cmd.exe"),
      "/d",
      "/c",
      "call",
      wrapper,
      "--home",
      home,
    ],
  };
}

/**
 * The argv that unpacks *zipPath* into *destination* with *unpacker*.
 *
 * `unzip` takes `-o` because it asks on stdin when it meets an existing file, and the
 * spawn's stdin is a pipe nobody writes to, so the run would hang.
 */
export function buildUnpackArgv(
  unpacker: ZipUnpacker,
  zipPath: string,
  destination: string,
): string[] {
  return unpacker.flavour === "tar"
    ? [unpacker.program, "-xf", zipPath, "-C", destination]
    : [unpacker.program, "-q", "-o", zipPath, "-d", destination];
}

export type VersionAnswer = { version: string } | { refusal: string };

/**
 * The version an unpacked archive holds, read out of its member names: the two filename
 * shapes `install.py`'s `archive_version()` reads (the archive carries no version file).
 * Two versions are a refusal.
 *
 * The installer stays the authority on what gets installed; this answer decides only the
 * *Update* comparison and the reload offer. *where* is the path the user picked, not the
 * temporary directory a zip was unpacked into.
 */
export function archiveVersion(names: readonly string[], where: string): VersionAnswer {
  const found = new Set<string>();
  for (const name of names) {
    const base = name.split(/[\\/]/).at(-1) ?? "";
    if (base.startsWith(VSIX_PREFIX) && base.endsWith(VSIX_SUFFIX)) {
      found.add(base.slice(VSIX_PREFIX.length, -VSIX_SUFFIX.length));
      continue;
    }
    if (!base.endsWith(WHEEL_SUFFIX)) {
      continue;
    }
    for (const distribution of WHEEL_DISTRIBUTIONS) {
      if (base.startsWith(`${distribution}-`)) {
        found.add(base.slice(distribution.length + 1).split("-")[0]);
      }
    }
  }
  if (found.size === 0) {
    return {
      refusal:
        `SWCraft: ${where} does not look like a workstation archive — nothing in it is a ` +
        `${VSIX_PREFIX}<version>${VSIX_SUFFIX} or a SWCraft wheel, so there is no version to ` +
        "install. Point this command at the archive zip, or at the directory it was unpacked into.",
    };
  }
  if (found.size > 1) {
    return {
      refusal:
        `SWCraft: ${where} holds more than one version (${[...found].sort().join(", ")}), so it is ` +
        "not an archive this command can read. Unpack a fresh one.",
    };
  }
  return { version: [...found][0] };
}

export type UpdateDecision = { proceed: true } | { refusal: string };

/**
 * Whether *Update Toolchain* may run this archive over the installed version.
 *
 * An older archive is refused in the command, before anything is unpacked, naming both
 * versions; rolling back is running the older archive's installer from a terminal, a
 * deliberate decision rather than a button beside *Update*.
 *
 * Three cases proceed: the **same** version (`plan_install()` treats it as a repair); an
 * installed version that is **`undefined`** (the executable will not start, and
 * `checkToolchain()` names this command as the repair); and an installed string that is
 * **not a version** (nothing to compare).
 */
export function decideUpdate(archive: string, installed: string | undefined): UpdateDecision {
  if (installed === undefined) {
    return { proceed: true };
  }
  const order = compareReleases(archive, installed);
  if (order === undefined || order >= 0) {
    return { proceed: true };
  }
  return {
    refusal:
      `SWCraft: this archive is ${archive} and the installed toolchain is ${installed}, so ` +
      "*Update Toolchain* would move it backwards. To go back to an older release deliberately, " +
      "run that archive's installer from a terminal.",
  };
}

/**
 * The line a run appends when `swcraft.executable` is set, or `undefined`.
 *
 * The override is what commands spawn (`toolchain.ts`'s `resolveSwcraft`), while both
 * toolchain commands install into `toolchainHome(...)` and read their versions there, so
 * the post-run health line does not answer "what will my commands spawn"; this note does.
 * A blank setting is not set, as `resolveSwcraft` reads it.
 *
 * What it does **not** reach: whether the override is on disk, or whether it is a
 * *different* SWCraft from the one just installed; both are `checkToolchain`'s.
 */
export function settingShadowsInstallationNote(
  setting: string | undefined,
  home: string,
): string | undefined {
  const override = setting?.trim();
  if (override === undefined || override === "") {
    return undefined;
  }
  return (
    `SWCraft: \`swcraft.executable\` is set to ${override}, so SWCraft commands in this window still ` +
    `spawn that executable and not the toolchain in ${home} this run installed. Clear the setting to ` +
    "use the installation."
  );
}

/**
 * Whether a successful run changed the version: the **first** of the two questions a
 * *Reload Window* offer asks. A run that changed it left the in-memory extension one version
 * behind the CLI; a repair leaves nothing to reload for.
 *
 * The **second** question, whether the newer extension is there to reload into, is
 * `extensionWasNotInstalled(run.output)`, read by `versionChangedNotice` and
 * `pendingReloadNote`.
 */
export function offersReload(archiveVersion: string, extensionVersion: string): boolean {
  return archiveVersion !== extensionVersion;
}

/**
 * The line to print beneath the post-run health report when a reload is pending, or
 * `undefined` when it is not.
 *
 * The health re-read compares the CLI's version with the **still-in-memory** extension
 * version, so an update reports a mismatch of its own making; this says which line to
 * ignore. When the installer **skipped** the extension (`code` not on the extension host's
 * `PATH`, the default on a GUI-launched macOS VS Code), a reload loads the same extension
 * again, so the remedy is installing the `.vsix` instead.
 *
 * What this does not reach is `extensionWasNotInstalled`'s limitation: a drift between
 * `install.py`'s sentences and `EXTENSION_NOT_INSTALLED_MARKERS` returns the *optimistic*
 * branch.
 */
export function pendingReloadNote(
  archiveVersion: string,
  extensionVersion: string,
  output: string,
): string | undefined {
  if (!offersReload(archiveVersion, extensionVersion)) {
    return undefined;
  }
  const mismatch =
    `SWCraft: the version mismatch above is this run's own — the CLI is now ${archiveVersion} and ` +
    `this extension is still the ${extensionVersion} that was loaded when the window opened. `;
  if (extensionWasNotInstalled(output)) {
    return (
      mismatch +
      `The archive's extension was not installed, so reloading the window would load this same ` +
      `${extensionVersion} again: install swcraft-vscode-${archiveVersion}.vsix from the archive ` +
      "through the Extensions view (Install from VSIX), then reload."
    );
  }
  return `${mismatch}Reloading the window is what makes both sides agree.`;
}

/**
 * The notification a run that **changed the version** puts up, and whether it carries a
 * *Reload Window* button: only when the archive's extension was installed, so there is a
 * newer one to reload into (the installer skips it, still exiting 0, when `code` is not on
 * the extension host's `PATH`).
 *
 * What it does not reach: it inherits `extensionWasNotInstalled`'s text matching; it does
 * not know whether the `.vsix` it names still exists (`sideloadNote` covers a zip source);
 * and an unchanged version is `offersReload`'s and `pathAdviceAfterRun`'s.
 */
export function versionChangedNotice(
  archiveVersion: string,
  home: string,
  output: string,
): { message: string; offersReloadButton: boolean } {
  if (extensionWasNotInstalled(output)) {
    return {
      offersReloadButton: false,
      message:
        `SWCraft: installed ${archiveVersion} into ${home}, but the archive's extension was not ` +
        `installed — the transcript above says why. This window still runs the older extension, ` +
        `so install swcraft-vscode-${archiveVersion}.vsix from the archive through the Extensions ` +
        "view (Install from VSIX) and reload the window after that.",
    };
  }
  return {
    offersReloadButton: true,
    message:
      `SWCraft: installed ${archiveVersion} into ${home}. The archive's extension was installed ` +
      `too, so reload the window to run ${archiveVersion} on both sides.`,
  };
}

/**
 * *argv* as a line a reader can retype: a part holding whitespace or a quote is quoted
 * (`C:\Program Files\...` is the common case).
 *
 * What it does not reach: `JSON.stringify` is neither `cmd.exe`'s nor POSIX `sh`'s quoting,
 * and the spawn itself takes an argv array and no shell.
 */
export function quoteArgvForEcho(argv: readonly string[]): string {
  return argv.map((part) => (/[\s"]/.test(part) ? JSON.stringify(part) : part)).join(" ");
}

/**
 * What a failed installer run says in a notification: its **last sentence**.
 *
 * `install.py` writes its one-sentence refusals last, to **stderr**, so the last non-empty
 * line of *errors* (stderr alone) is the diagnosis; the full console is in the output
 * channel as *output*, and the subprocesses' output (pip, `venv`, `code`) is in
 * `install.log`, whose path the installer prints above the refusal. Stderr is read rather
 * than the combined *output* because two pipes have no order between them: the stdout chunk
 * may arrive after the stderr one.
 *
 * What this does not reach: a run that wrote nothing to stderr (killed by a signal, or a
 * refusal printed to stdout) falls back to *output*'s last line, with that cross-pipe
 * exposure.
 *
 * *exitCode* is `null` both when the process never started and when a *signal* ended it;
 * *signal* tells them apart, so a killed installer is not reported as "could not be
 * started".
 */
export function installerFailureMessage(
  output: string,
  exitCode: number | null,
  errors: string,
  signal: string | null = null,
): string {
  const lastLine = (text: string): string | undefined =>
    text
      .split(/\r?\n/)
      .map((line) => line.trim())
      .filter((line) => line !== "")
      .at(-1);
  const last = lastLine(errors) ?? lastLine(output);
  const what =
    signal !== null
      ? `was terminated by ${signal}`
      : exitCode === null
        ? "could not be started"
        : `exited ${exitCode}`;
  if (last === undefined) {
    return `SWCraft: the toolchain installer ${what} and printed nothing — see the SWCraft output channel.`;
  }
  return `SWCraft: the toolchain installer ${what}: ${last}`;
}

/**
 * The markers in `install.py`'s report that the extension was **not** installed.
 * `_install_extension` has three such outcomes: the **skip** (`code` never resolved; the
 * run exits 0), the first fragment; and a `code` that could not be **started** or that
 * **failed**, both raising and sharing the second fragment.
 *
 * Read as text: there is no structured channel between the installer and this extension,
 * and one for two sentences would be a second contract. The fragments are
 * `_install_extension`'s in `scripts/workstation/install.py`, its Python suite asserts
 * them, and a drift makes a note *missing* rather than wrong. They are this long because
 * `_uninstall_extension` reads "which is also what it answers when the extension was not
 * installed", which a shorter fragment would match.
 */
const EXTENSION_NOT_INSTALLED_MARKERS = [
  "so the extension was not installed",
  "the toolchain is installed and the extension is not",
];

/**
 * Whether the installer's transcript says the extension did **not** go in: the one reading
 * `sideloadNote` and `versionChangedNotice` share, so the two cannot contradict each other.
 *
 * What it does not reach is `EXTENSION_NOT_INSTALLED_MARKERS`' limitation: a drift from
 * `install.py`'s sentences makes the note *missing* rather than wrong.
 */
export function extensionWasNotInstalled(output: string): boolean {
  return EXTENSION_NOT_INSTALLED_MARKERS.some((marker) => output.includes(marker));
}

/**
 * The line to add when the installer told the reader to sideload a `.vsix` from a directory
 * this command is about to delete.
 *
 * `buildInstallerArgv` passes no `--no-vsix`, so when `code` is missing or will not run,
 * `install.py` reports the `.vsix` inside the archive root it was given; for a zip that is
 * the temporary directory `toolchainCommand` removes in its `finally`. The note names the
 * zip the reader picked instead. Its caller emits it **before** the failure return, because
 * two of the three branches it answers are refusals. A directory source, or an install that
 * placed the extension, needs nothing.
 *
 * What this does not reach: it does not install the extension, and it does not know *why*
 * `code` was missing.
 */
export function sideloadNote(
  output: string,
  source: { source: "zip" | "directory"; picked: string },
  version: string,
): string | undefined {
  if (source.source !== "zip" || !extensionWasNotInstalled(output)) {
    return undefined;
  }
  return (
    `SWCraft: the .vsix path above is inside a temporary copy that this command removes when it ` +
    `finishes. Unpack ${source.picked} yourself and install swcraft-vscode-${version}.vsix from ` +
    "it through the Extensions view (Install from VSIX)."
  );
}

/**
 * The installer's own sentence when it hands the `PATH` entry to the reader rather than
 * editing a shell file (`_add_path_entry` in `scripts/workstation/install.py`, macOS and
 * Linux). Read as text like `EXTENSION_NOT_INSTALLED_MARKERS`; the Python suite pins it.
 */
const PATH_LINE_PRINTED_MARKER = "Add this to your shell profile";

/**
 * What to tell the reader about `PATH` after a run that changed no version.
 *
 * `install.py` plans its `path` step only when the entry is absent from the environment it
 * was spawned with (`cliEnv()`), so a repair often prints nothing; the transcript is
 * therefore read for the installer's sentence rather than assumed.
 *
 * What this does not reach: Windows, where the entry goes into `HKCU\Environment` and the
 * installer prints nothing either way; and neither half reads a shell profile.
 */
export function pathAdviceAfterRun(platform: Platform, output: string): string {
  if (platform === "windows") {
    return "Open a new terminal for the PATH entry to take effect there.";
  }
  return output.includes(PATH_LINE_PRINTED_MARKER)
    ? "On macOS and Linux nothing was added to your shell profile — the installer printed the " +
        "`export PATH=…` line for it in the output channel above."
    : "On macOS and Linux the installer edits no shell profile, and this time it printed no " +
        "`export PATH=…` line: the entry is already on the PATH this window was launched with.";
}

/**
 * One entry of the picker that asks what the user is pointing the command at.
 *
 * The discriminator is `source`, not `kind`, because `QuickPickItem` declares a `kind` of
 * its own (`QuickPickItemKind.Separator`).
 */
export interface ArchiveSourceItem {
  source: "zip" | "directory";
  label: string;
  detail: string;
}

/**
 * The archive's own platform token for a Node.js `process.platform` / `process.arch` pair,
 * or `undefined` for a host no archive is built for.
 *
 * The tokens are `scripts/build_artifacts.py`'s `workstation_platform_token()`, held equal
 * by `toolchainInstall.test.ts`. Not `commandCore.ts`'s `currentPlatform()`, which answers
 * a path layout without the architecture.
 */
export function archivePlatformToken(nodePlatform: string, arch: string): string | undefined {
  if (nodePlatform === "win32" && arch === "x64") {
    return "windows-x64";
  }
  if (nodePlatform === "linux" && arch === "x64") {
    return "linux-x64";
  }
  if (nodePlatform === "darwin" && arch === "arm64") {
    return "macos-arm64";
  }
  return undefined;
}

/**
 * `swcraft-<version>-<platform>.zip`, with a placeholder standing for a part
 * that cannot be known: an extension manifest without a version, or a host no
 * archive is built for.
 */
export function archiveFileName(
  extensionVersion: string,
  platformToken: string | undefined,
): string {
  const version = extensionVersion === "" ? "<version>" : extensionVersion;
  return `swcraft-${version}-${platformToken ?? "<platform>"}.zip`;
}

/** The picker's label for an already-unpacked archive — also named by `findZipUnpacker()`'s refusal. */
export const UNPACKED_ARCHIVE_FOLDER_LABEL = "Unpacked archive folder";

/**
 * The two things a person can point either command at, asked as a QuickPick **before** the
 * open dialog: a dialog with both `canSelectFiles` and `canSelectFolders` degrades to a
 * directory selector on Windows and Linux, which would leave the zip unpickable.
 */
export const ARCHIVE_SOURCE_ITEMS: readonly ArchiveSourceItem[] = [
  {
    source: "zip",
    label: "Archive file (.zip)",
    detail: `The ${archiveFileName("", undefined)} you were handed; it is unpacked to a temporary directory.`,
  },
  {
    source: "directory",
    label: UNPACKED_ARCHIVE_FOLDER_LABEL,
    detail: "A directory holding install.py, wheels/ and the .vsix — an archive already extracted.",
  },
];

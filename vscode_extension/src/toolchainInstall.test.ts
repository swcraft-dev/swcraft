// Unit tests for the two toolchain commands' decisions.
//
// Everything the commands decide is in `toolchainInstall.ts` and is driven here directly:
// which program opens a picked zip, what argv starts the archive's own wrapper, which version
// the archive holds, whether *Update* may proceed, whether a reload has to be offered, and what
// a failed run says. The adapter half — the picker, the temporary directory, the spawn, the
// notification — is `commands.ts`, witnessed only by the source guards at the end of this file
// and by the seated gate: `commands.ts` imports `vscode` and cannot be loaded under plain
// `node --test`.

import assert from "node:assert/strict";
import * as path from "node:path";
import { test } from "node:test";

import {
  assertNoProductionModuleNames,
  functionBody,
  productionModules,
  readManifest,
  readSource,
  withoutCommentLines,
} from "./testSupport";

import {
  ARCHIVE_SOURCE_ITEMS,
  archiveFileName,
  archivePlatformToken,
  archiveVersion,
  buildInstallerArgv,
  buildUnpackArgv,
  CMD_REPARSED_CHARACTERS,
  decideUpdate,
  extensionWasNotInstalled,
  findZipUnpacker,
  INSTALL_TOOLCHAIN_COMMAND_ID,
  installerFailureMessage,
  offersReload,
  POSIX_WRAPPER_FILENAME,
  pathAdviceAfterRun,
  pathLookup,
  pendingReloadNote,
  quoteArgvForEcho,
  settingShadowsInstallationNote,
  sideloadNote,
  UNPACKED_ARCHIVE_FOLDER_LABEL,
  UPDATE_TOOLCHAIN_COMMAND_ID,
  UPDATE_TOOLCHAIN_TITLE,
  versionChangedNotice,
  WINDOWS_WRAPPER_FILENAME,
} from "./toolchainInstall";

/** A `which` stub that answers from a table and records what it was asked. */
function whichSpy(table: Readonly<Record<string, string>>): {
  which: (name: string) => string | undefined;
  asked: string[];
} {
  const asked: string[] = [];
  return {
    which: (name: string) => {
      asked.push(name);
      return table[name];
    },
    asked,
  };
}

// --- findZipUnpacker ---------------------------------------------------------
//
// Which program opens a picked zip: one the platform already carries, since the archive's own
// interpreter is inside the very zip that has to be opened first.

test("findZipUnpacker: Windows names the tar.exe under SystemRoot and looks nothing up", () => {
  // A GNU tar earlier on PATH — Git for Windows puts one there — reads `C:` as a
  // remote host name, which is why the archive's own `swcraft_install.cmd` names
  // this program by its full path too; the spy is what says PATH was not asked.
  const spy = whichSpy({
    tar: "C:\\Git\\usr\\bin\\tar.exe",
    unzip: "C:\\Git\\usr\\bin\\unzip.exe",
  });
  // `D:\WinNT` and not `C:\Windows`: the fallback is `C:\Windows`, so a root
  // equal to it would pass even if the environment were never read.
  const found = findZipUnpacker({ SystemRoot: "D:\\WinNT" }, "windows", spy.which);
  assert.ok("unpacker" in found, JSON.stringify(found));
  assert.deepEqual(found.unpacker, { program: "D:\\WinNT\\System32\\tar.exe", flavour: "tar" });
  assert.deepEqual(spy.asked, []);
});

test("findZipUnpacker: the SYSTEMROOT spelling is read too", () => {
  const found = findZipUnpacker({ SYSTEMROOT: "E:\\Win" }, "windows", whichSpy({}).which);
  assert.ok("unpacker" in found, JSON.stringify(found));
  assert.equal(found.unpacker.program, "E:\\Win\\System32\\tar.exe");
});

test("findZipUnpacker: a Windows block with no SystemRoot still names C:\\Windows's tar.exe", () => {
  const found = findZipUnpacker({}, "windows", whichSpy({}).which);
  assert.ok("unpacker" in found);
  assert.equal(found.unpacker.program, "C:\\Windows\\System32\\tar.exe");
});

test("findZipUnpacker: a blank SystemRoot falls back to C:\\Windows rather than to a relative System32", () => {
  const found = findZipUnpacker({ SystemRoot: "  " }, "windows", whichSpy({}).which);
  assert.ok("unpacker" in found);
  assert.equal(found.unpacker.program, "C:\\Windows\\System32\\tar.exe");
});

test("findZipUnpacker: POSIX looks unzip up on the PATH VS Code was started with", () => {
  const spy = whichSpy({ unzip: "/usr/bin/unzip" });
  const found = findZipUnpacker({ PATH: "/usr/bin:/bin" }, "posix", spy.which);
  assert.ok("unpacker" in found, JSON.stringify(found));
  assert.deepEqual(found.unpacker, { program: "/usr/bin/unzip", flavour: "unzip" });
  assert.deepEqual(spy.asked, ["unzip"], "nothing else is looked for, a Python least of all");
});

test("findZipUnpacker: POSIX without unzip refuses, names the folder route, and asks for no Python", () => {
  // The lookup scans the extension host's `process.env`, so the sentence says whose PATH it
  // searched rather than claiming the machine has no such program. The folder route needs no
  // unpacker, so it is the repair the sentence names.
  const found = findZipUnpacker(
    {},
    "posix",
    whichSpy({ "python3.11": "/usr/bin/python3.11" }).which,
  );
  assert.ok("refusal" in found);
  assert.match(found.refusal, /`unzip`/);
  assert.match(found.refusal, /PATH VS Code was started with/);
  assert.doesNotMatch(found.refusal, /on this machine/);
  const folderItem = ARCHIVE_SOURCE_ITEMS.find((item) => item.source === "directory");
  assert.equal(folderItem?.label, UNPACKED_ARCHIVE_FOLDER_LABEL);
  assert.ok(
    found.refusal.includes(UNPACKED_ARCHIVE_FOLDER_LABEL),
    "the picker item a reader chooses instead is named in the picker's own words",
  );
  assert.doesNotMatch(found.refusal, /python/i);
});

// --- pathLookup --------------------------------------------------------------
//
// The `which` `findZipUnpacker()` is handed. It exists so the "no `unzip`"
// refusal fires only when the program really is absent: a bare `spawn("unzip")`
// would answer that question with an ENOENT after the user has already picked an
// archive, and with a message about a file handle.

test("pathLookup: POSIX answers the first PATH entry that holds the name", () => {
  const present = new Set(["/opt/homebrew/bin/unzip"]);
  const lookup = pathLookup({ PATH: "/usr/bin:/opt/homebrew/bin" }, "posix", (c) => present.has(c));
  assert.equal(lookup("unzip"), "/opt/homebrew/bin/unzip");
  assert.equal(lookup("bsdtar"), undefined);
});

test("pathLookup: Windows adds the executable extensions, and reads Path as well as PATH", () => {
  // A Windows environment block spells the variable either way — `process.env` on
  // Windows is case-insensitive, but the object handed to a resolution for the
  // other platform is not.
  const present = new Set(["C:\\Tools\\unzip.exe"]);
  const lookup = pathLookup({ Path: "C:\\Tools" }, "windows", (c) => present.has(c));
  assert.equal(lookup("unzip"), "C:\\Tools\\unzip.exe");
});

test("pathLookup: no PATH at all answers undefined rather than throwing", () => {
  assert.equal(pathLookup({}, "posix", () => true)("unzip"), undefined);
});

test("pathLookup: a .cmd or .bat is not offered, because spawn() cannot start one", () => {
  // The resolved path goes straight into `spawn(argv[0], …)` with no `shell`, and Node >=
  // 18.20.2 / 20.12.2 refuses a batch file without `shell: true` (the CVE-2024-27980 fix) —
  // `spawn … EINVAL`, not a sentence. A host whose only candidate is a wrapper script gets the
  // refusal written for an absent program, not a launch error.
  const present = new Set(["C:\\shims\\unzip.cmd", "C:\\shims\\unzip.bat"]);
  const lookup = pathLookup({ Path: "C:\\shims" }, "windows", (c) => present.has(c));
  assert.equal(lookup("unzip"), undefined);
});

test("pathLookup: a quoted Windows PATH entry still resolves", () => {
  // A `PATH` entry may legitimately be quoted on Windows; an unstripped quote makes
  // the lookup miss on a machine that does have the program.
  const present = new Set(["C:\\Program Files\\Tools\\unzip.exe"]);
  const lookup = pathLookup({ Path: '"C:\\Program Files\\Tools"' }, "windows", (c) =>
    present.has(c),
  );
  assert.equal(lookup("unzip"), "C:\\Program Files\\Tools\\unzip.exe");
});

test("pathLookup: a PATH entry padded with spaces still resolves", () => {
  // The other half of the same normalisation: a `PATH` assembled by a batch file or an
  // installer routinely carries a space after the separator, and an unstripped one makes every
  // lookup in that directory miss.
  const present = new Set(["C:\\Tools\\unzip.exe"]);
  const lookup = pathLookup({ Path: "C:\\Other; C:\\Tools " }, "windows", (c) => present.has(c));
  assert.equal(lookup("unzip"), "C:\\Tools\\unzip.exe");
});

// --- the two argv builders ---------------------------------------------------
//
// The installer is started through the archive's OWN wrapper, never as
// `<python> install.py`: the wrapper is the archive's one bootstrap — it checks the
// carried interpreter against `SHA256SUMS`, unpacks it into `.bootstrap/python` and
// runs `install.py` on it — and a copy of those acts here would be a second
// bootstrap with nothing keeping the two in step.

test("buildInstallerArgv: POSIX runs swcraft_install.sh through /bin/sh with --home, and no --python", () => {
  assert.deepEqual(buildInstallerArgv("/tmp/archive", "/Users/dev/.swcraft", "posix", {}), {
    argv: [
      "/bin/sh",
      path.posix.join("/tmp/archive", POSIX_WRAPPER_FILENAME),
      "--home",
      "/Users/dev/.swcraft",
    ],
  });
});

test("buildInstallerArgv: Windows runs swcraft_install.cmd through SystemRoot's cmd.exe /d /c call", () => {
  assert.deepEqual(
    buildInstallerArgv("C:\\Temp\\archive", "C:\\Users\\dev\\AppData\\Local\\SWCraft", "windows", {
      SystemRoot: "D:\\WinNT",
    }),
    {
      argv: [
        "D:\\WinNT\\System32\\cmd.exe",
        "/d",
        "/c",
        "call",
        "C:\\Temp\\archive\\swcraft_install.cmd",
        "--home",
        "C:\\Users\\dev\\AppData\\Local\\SWCraft",
      ],
    },
  );
});

test("buildInstallerArgv: Windows reads SYSTEMROOT, and falls back to C:\\Windows for a blank root", () => {
  const cmd = (env: Record<string, string>): string | undefined => {
    const answer = buildInstallerArgv("C:\\a", "C:\\h", "windows", env);
    return "argv" in answer ? answer.argv[0] : undefined;
  };
  assert.equal(cmd({ SYSTEMROOT: "E:\\Win" }), "E:\\Win\\System32\\cmd.exe");
  assert.equal(cmd({ SystemRoot: "" }), "C:\\Windows\\System32\\cmd.exe");
  assert.equal(cmd({}), "C:\\Windows\\System32\\cmd.exe");
});

test("buildInstallerArgv: no argv names a Python or install.py, on either platform", () => {
  // The whole point of starting the wrapper: an interpreter is the archive's to
  // provide, and `--python` would now mean "build on this one and unpack none".
  for (const [platform, root] of [
    ["posix", "/tmp/a"],
    ["windows", "C:\\a"],
  ] as const) {
    const answer = buildInstallerArgv(root, platform === "posix" ? "/h" : "C:\\h", platform, {});
    assert.ok("argv" in answer, JSON.stringify(answer));
    assert.equal(answer.argv.includes("--python"), false);
    assert.equal(
      answer.argv.some((part) => /python|install\.py/i.test(part)),
      false,
      answer.argv.join(" "),
    );
  }
});

test("buildInstallerArgv: Windows refuses a path cmd.exe would re-read, naming the path and the character", () => {
  // `cmd.exe` re-reads the line after `/c` with its own parser, so a folder under
  // `C:\a&b` would split into two commands, the second a program nobody named.
  for (const character of CMD_REPARSED_CHARACTERS) {
    const root = `C:\\Temp\\a${character}b`;
    const inRoot = buildInstallerArgv(root, "C:\\h", "windows", {});
    assert.ok("refusal" in inRoot, `${character} in the archive folder was not refused`);
    assert.ok(inRoot.refusal.includes(`\`${character}\``), inRoot.refusal);
    assert.ok(inRoot.refusal.includes(root), inRoot.refusal);

    const home = `C:\\Users\\a${character}b\\AppData\\Local\\SWCraft`;
    const inHome = buildInstallerArgv("C:\\Temp\\archive", home, "windows", {});
    assert.ok("refusal" in inHome, `${character} in the home was not refused`);
    assert.ok(inHome.refusal.includes(`\`${character}\``), inHome.refusal);
    assert.ok(inHome.refusal.includes(home), inHome.refusal);
  }
});

test("buildInstallerArgv: POSIX has no command interpreter in the way, so the same characters start", () => {
  const answer = buildInstallerArgv("/tmp/a&b%c", "/Users/a^b/.swcraft", "posix", {});
  assert.ok("argv" in answer, JSON.stringify(answer));
});

test("the characters refused are install.py's own CMD_REPARSED_CHARACTERS", () => {
  // One list, in two languages, for one parser: the installer refuses the same
  // characters in the `cmd.exe` lines it builds.
  const installer = readSource("../scripts/workstation/install.py");
  const match = /^CMD_REPARSED_CHARACTERS = '([^']+)'$/m.exec(installer);
  assert.ok(match !== null, "install.py no longer declares CMD_REPARSED_CHARACTERS as one literal");
  assert.deepEqual([...CMD_REPARSED_CHARACTERS].sort(), [...(match[1] as string)].sort());
});

test("the wrappers the argv names exist, pass every argument through, and do not pause on --home", () => {
  // `--home` is what keeps a spawned `.cmd` from waiting for a key: its double-click
  // pause fires only when no argument was passed, and a `pause` on the pipe this
  // spawn gives it would never be answered.
  const sh = readSource(`../scripts/workstation/${POSIX_WRAPPER_FILENAME}`);
  const cmd = readSource(`../scripts/workstation/${WINDOWS_WRAPPER_FILENAME}`);
  assert.match(sh, /install\.py" "\$@"/);
  assert.match(cmd, /install\.py" %\*/);
  // What `if "%~1"==""` guards, not only that it is there: the flag that makes
  // the wrapper pause is set on the one line that also requires NO first
  // argument, and `pause` runs only behind that flag.
  const code = cmd
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line !== "" && !/^rem\b/i.test(line));
  const setsPause = code.filter((line) => /set "SWCRAFT_PAUSE=1"/.test(line));
  assert.deepEqual(
    setsPause,
    ['if not errorlevel 1 if "%~1"=="" if not defined SWCRAFT_NO_PAUSE set "SWCRAFT_PAUSE=1"'],
    "SWCRAFT_PAUSE is set only when no argument was passed",
  );
  assert.deepEqual(
    code.filter((line) => /\bpause\b/i.test(line) && !/SWCRAFT_(NO_)?PAUSE/.test(line)),
    [],
    "no bare pause",
  );
  assert.deepEqual(
    code.filter((line) => /\bpause$/i.test(line)),
    ["if defined SWCRAFT_PAUSE pause"],
    "the one pause runs behind the flag that --home leaves unset",
  );
});

test("buildUnpackArgv: tar -xf <zip> -C <destination> for Windows' tar.exe", () => {
  assert.deepEqual(
    buildUnpackArgv(
      { program: "C:\\Windows\\System32\\tar.exe", flavour: "tar" },
      "C:\\Downloads\\swcraft-0.7.0-windows-x64.zip",
      "C:\\Temp\\unpacked",
    ),
    [
      "C:\\Windows\\System32\\tar.exe",
      "-xf",
      "C:\\Downloads\\swcraft-0.7.0-windows-x64.zip",
      "-C",
      "C:\\Temp\\unpacked",
    ],
  );
});

test("buildUnpackArgv: unzip -q -o <zip> -d <destination> elsewhere, never prompting", () => {
  // `-o` because an `unzip` that meets an existing file asks on stdin, and the
  // spawn's stdin is a pipe nobody writes to: the run would hang, not refuse.
  assert.deepEqual(
    buildUnpackArgv(
      { program: "/usr/bin/unzip", flavour: "unzip" },
      "/Downloads/swcraft-0.7.0-macos-arm64.zip",
      "/tmp/unpacked",
    ),
    [
      "/usr/bin/unzip",
      "-q",
      "-o",
      "/Downloads/swcraft-0.7.0-macos-arm64.zip",
      "-d",
      "/tmp/unpacked",
    ],
  );
});

// --- archiveVersion ----------------------------------------------------------
//
// The same two filename shapes `install.py`'s `archive_version()` reads, and
// for its reason: the archive carries no version file. The installer is the
// authority — this answer only decides the *Update* comparison and the reload
// offer, and a disagreement would be caught by the installer refusing.

test("archiveVersion: read from the .vsix beside install.py", () => {
  const answer = archiveVersion(
    ["install.py", "swcraft_install.sh", "swcraft-vscode-0.7.0.vsix", "SHA256SUMS", "wheels"],
    "/tmp/unpacked",
  );
  assert.deepEqual(answer, { version: "0.7.0" });
});

test("archiveVersion: read from a wheel name when the listing reaches wheels/", () => {
  const answer = archiveVersion(
    ["install.py", "wheels/swcraft_tools-0.7.0-py3-none-any.whl", "wheels/lxml-6.0.2-cp311.whl"],
    "/tmp/unpacked",
  );
  assert.deepEqual(answer, { version: "0.7.0" });
});

test("archiveVersion: nothing that names a version is a refusal naming the directory", () => {
  const answer = archiveVersion(["Documents", "notes.txt"], "/Users/dev/Downloads");
  assert.ok("refusal" in answer);
  assert.match(answer.refusal, /\/Users\/dev\/Downloads/);
  assert.match(answer.refusal, /\.vsix/);
});

test("archiveVersion: a companion file beside the .vsix is not a second version", () => {
  // The conjunction of `startsWith(VSIX_PREFIX) && endsWith(VSIX_SUFFIX)`: with `||`, a
  // checksum or a signature written beside the extension (the shapes a mirror or a signing
  // step adds) slices a bogus version out of the name, and the run refuses a working archive
  // as "more than one version".
  const answer = archiveVersion(
    ["install.py", "swcraft-vscode-0.7.0.vsix", "swcraft-vscode-0.7.0.vsix.sha256"],
    "/tmp/unpacked",
  );
  assert.deepEqual(answer, { version: "0.7.0" });
});

test("archiveVersion: two versions in one directory is a refusal naming both", () => {
  const answer = archiveVersion(
    ["swcraft-vscode-0.7.0.vsix", "wheels/swcraft_tools-0.6.0-py3-none-any.whl"],
    "/tmp/unpacked",
  );
  assert.ok("refusal" in answer);
  assert.match(answer.refusal, /0\.6\.0/);
  assert.match(answer.refusal, /0\.7\.0/);
});

// --- the sideload note ------------------------------------------------------

test("sideloadNote: a zip source gets a line naming the archive the user still has", () => {
  // `buildInstallerArgv` passes no `--no-vsix`, so an extension host without `code` on its
  // PATH — the default on macOS — makes install.py print "install
  // <archiveRoot>/swcraft-vscode-<v>.vsix from the Extensions view yourself". For a zip source
  // that `archiveRoot` is the temporary directory the command removes in its own `finally`,
  // before the reader can act on the transcript.
  const note = sideloadNote(
    "VS Code's `code` command was not found, so the extension was not installed: install " +
      "/tmp/swcraft-archive-ab12/swcraft-vscode-0.7.0.vsix from the Extensions view (Install from VSIX) yourself\n",
    { source: "zip", picked: "/home/u/Downloads/swcraft-0.7.0-linux-x64.zip" },
    "0.7.0",
  );
  assert.ok(note !== undefined);
  assert.match(note, /swcraft-0\.7\.0-linux-x64\.zip/);
  assert.match(note, /swcraft-vscode-0\.7\.0\.vsix/);
});

test("sideloadNote: a directory source needs no note — the printed path outlives the command", () => {
  assert.equal(
    sideloadNote(
      "install /home/u/archive/swcraft-vscode-0.7.0.vsix from the Extensions view yourself\n",
      { source: "directory", picked: "/home/u/archive" },
      "0.7.0",
    ),
    undefined,
  );
});

test("sideloadNote: the branch that could not START code gets the note too", () => {
  // `_install_extension` has *three* outcomes, not only the skip: a `code` that resolved at
  // plan time and will not start at run time raises, and its sentence names the same
  // temporary `.vsix`.
  const note = sideloadNote(
    "`code` could not be started ([Errno 8] Exec format error), so the toolchain is installed and " +
      "the extension is not: install /tmp/swcraft-archive-ab12/swcraft-vscode-0.7.0.vsix from the " +
      "Extensions view yourself\n",
    { source: "zip", picked: "/home/u/Downloads/swcraft-0.7.0-linux-x64.zip" },
    "0.7.0",
  );
  assert.ok(note !== undefined);
  assert.match(note, /swcraft-0\.7\.0-linux-x64\.zip/);
});

test("sideloadNote: the branch where code RAN and failed gets the note too", () => {
  const note = sideloadNote(
    "`code` could not install the extension (exit code 1), so the toolchain is installed and the " +
      "extension is not: install /tmp/swcraft-archive-ab12/swcraft-vscode-0.7.0.vsix from the " +
      "Extensions view yourself\n",
    { source: "zip", picked: "/home/u/Downloads/swcraft-0.7.0-linux-x64.zip" },
    "0.7.0",
  );
  assert.ok(note !== undefined);
  assert.match(note, /swcraft-0\.7\.0-linux-x64\.zip/);
});

test("sideloadNote: an UNINSTALL that could not reach code says nothing", () => {
  // The opposite case for the widened marker: `_uninstall_extension`'s three sentences also
  // end in the Extensions view, so matching on that fragment alone would tell a reader who
  // removed SWCraft to go and install it.
  for (const output of [
    "VS Code's `code` command was not found, so the extension was not removed: remove SWCraft from " +
      "the Extensions view yourself\n",
    "`code` did not remove the extension (exit code 1), which is also what it answers when the " +
      "extension was not installed: check the Extensions view if it is still there\n",
  ]) {
    assert.equal(
      sideloadNote(
        output,
        { source: "zip", picked: "/home/u/Downloads/swcraft-0.7.0-linux-x64.zip" },
        "0.7.0",
      ),
      undefined,
      output,
    );
  }
});

test("sideloadNote: an install that did place the extension says nothing", () => {
  assert.equal(
    sideloadNote(
      "Installing SWCraft 0.7.0\nExtension 'swcraft-local.swcraft-vscode' was successfully installed.\n",
      { source: "zip", picked: "/home/u/Downloads/swcraft-0.7.0-linux-x64.zip" },
      "0.7.0",
    ),
    undefined,
  );
});

// --- decideUpdate ------------------------------------------------------------

test("decideUpdate: an older archive is refused in the command, naming both versions", () => {
  const decision = decideUpdate("0.6.0", "0.7.0");
  assert.ok("refusal" in decision);
  assert.match(decision.refusal, /0\.6\.0/);
  assert.match(decision.refusal, /0\.7\.0/);
  // Rollback is the installer run from a terminal, deliberately not a button.
  assert.match(decision.refusal, /terminal/i);
});

test("decideUpdate: 0.10.0 over 0.9.0 proceeds — the compare is numeric", () => {
  assert.deepEqual(decideUpdate("0.10.0", "0.9.0"), { proceed: true });
  const backwards = decideUpdate("0.9.0", "0.10.0");
  assert.ok("refusal" in backwards);
});

test("decideUpdate: the same version proceeds, because the installer treats it as a repair", () => {
  // `plan_install` installs no venv and runs `pip install --force-reinstall`
  // into the existing directory when the version is already present. Refusing
  // it here would take away the one action that repairs an installation whose
  // files are damaged but whose version still reads.
  assert.deepEqual(decideUpdate("0.7.0", "0.7.0"), { proceed: true });
});

test("decideUpdate: an unreadable installed version proceeds — that is what Update repairs", () => {
  // `checkToolchain()`'s case 2 names *Update Toolchain* as the repair for an
  // installation that exists and will not start; there is no version to read
  // in that state, and refusing here would leave that sentence pointing at a
  // command that refuses.
  assert.deepEqual(decideUpdate("0.7.0", undefined), { proceed: true });
});

test("decideUpdate: an installed version that is not a version proceeds too", () => {
  // A wrapper printing a banner reaches `parseSwcraftVersion()` as a string
  // that is not a version. Nothing can be compared, so nothing is refused.
  assert.deepEqual(decideUpdate("0.7.0", "not-a-version"), { proceed: true });
});

// --- offersReload ------------------------------------------------------------

test("offersReload: only when the archive's version differs from the running extension's", () => {
  // The archive installs its own `.vsix`, so after a version-changing run the
  // extension in memory is the old one — and `checkToolchain()` would report
  // the mismatch the update itself just caused until the window reloads.
  assert.equal(offersReload("0.7.0", "0.6.0"), true);
  assert.equal(offersReload("0.7.0", "0.7.0"), false);
});

// --- versionChangedNotice ----------------------------------------------------

// An installer that skipped the extension (`code` not on the extension host's PATH, the
// default on a GUI-launched macOS VS Code) still exits 0; a notice saying the archive's
// extension was installed would then contradict the sideload note in the channel, and
// reloading would leave the reader one version behind with no sign of why.

test("versionChangedNotice: an install that placed the extension offers the reload", () => {
  const notice = versionChangedNotice(
    "0.7.0",
    "/home/u/.swcraft",
    "Installing SWCraft 0.7.0\nExtension 'swcraft-local.swcraft-vscode' was successfully installed.\n",
  );
  assert.equal(notice.offersReloadButton, true);
  assert.match(notice.message, /extension was installed/);
  assert.match(notice.message, /0\.7\.0/);
  assert.match(notice.message, /\/home\/u\/\.swcraft/);
});

test("versionChangedNotice: a SKIPPED extension is not asserted, and no reload is offered", () => {
  const notice = versionChangedNotice(
    "0.7.0",
    "/home/u/.swcraft",
    "VS Code's `code` command was not found, so the extension was not installed: install " +
      "/tmp/swcraft-archive-ab12/swcraft-vscode-0.7.0.vsix from the Extensions view (Install from VSIX) yourself\n",
  );
  assert.equal(
    notice.offersReloadButton,
    false,
    "reloading changes nothing when the .vsix never went in — it just hides the mismatch",
  );
  assert.match(notice.message, /was not installed/);
  assert.match(notice.message, /swcraft-vscode-0\.7\.0\.vsix/, "the file the reader must install");
  assert.doesNotMatch(
    notice.message,
    /extension was installed too/,
    "the sentence that contradicted the sideload note two lines above it",
  );
});

test("versionChangedNotice: the two raising branches read the same as the skip", () => {
  // `_install_extension`'s other two outcomes — a `code` that will not start and a `code`
  // that ran and failed — share the second marker, and the run then fails, so this notice is
  // not reached on them. Asserted anyway: the reading is the transcript's, whichever of the
  // three produced it.
  for (const output of [
    "`code` could not be started ([Errno 8] Exec format error), so the toolchain is installed and " +
      "the extension is not: install /tmp/a/swcraft-vscode-0.7.0.vsix from the Extensions view yourself\n",
    "`code` could not install the extension (exit code 1), so the toolchain is installed and the " +
      "extension is not: install /tmp/a/swcraft-vscode-0.7.0.vsix from the Extensions view yourself\n",
  ]) {
    assert.equal(
      versionChangedNotice("0.7.0", "/home/u/.swcraft", output).offersReloadButton,
      false,
      output,
    );
  }
});

test("versionChangedNotice: an UNINSTALL transcript is not read as a skipped install", () => {
  // The marker's own trap, pinned here as well as on `sideloadNote`: all three of
  // `_uninstall_extension`'s sentences end in the Extensions view too.
  const notice = versionChangedNotice(
    "0.7.0",
    "/home/u/.swcraft",
    "`code` did not remove the extension (exit code 1), which is also what it answers when the " +
      "extension was not installed: check the Extensions view if it is still there\n",
  );
  assert.equal(notice.offersReloadButton, true);
});

// --- extensionWasNotInstalled ------------------------------------------------

test("extensionWasNotInstalled: the one reading both the note and the notice use", () => {
  // Two callers must not be able to disagree about one transcript.
  assert.equal(extensionWasNotInstalled("so the extension was not installed: ..."), true);
  assert.equal(
    extensionWasNotInstalled("the toolchain is installed and the extension is not"),
    true,
  );
  assert.equal(extensionWasNotInstalled("was successfully installed."), false);
});

// --- installerFailureMessage -------------------------------------------------

test("installerFailureMessage: the installer's last sentence, with the exit code", () => {
  const output =
    "checking the platform\n" +
    "installing swcraft-agent==0.7.0\n" +
    "runtime/python.tar.gz does not match its line in SHA256SUMS, so nothing was installed\n\n";
  const message = installerFailureMessage(output, 1, "");
  assert.match(message, /does not match its line in SHA256SUMS/);
  // `/1/` alone is satisfied by a fixture sentence carrying a digit; the exit code has to be
  // asserted where it is rendered.
  assert.match(message, /exited 1\b/);
});

test("installerFailureMessage: CRLF output still yields one line, not a line with a CR", () => {
  const message = installerFailureMessage("first\r\nthe last sentence\r\n", 2, "");
  assert.match(message, /the last sentence/);
  assert.doesNotMatch(message, /\r/);
});

test("installerFailureMessage: an installer that printed nothing says so", () => {
  const message = installerFailureMessage("   \n\n", 3, "  \n");
  assert.match(message, /exited 3\b/);
  assert.match(message, /printed nothing/);
  assert.match(message, /output channel/i);
});

test("installerFailureMessage: a process that never started is not reported as an exit code", () => {
  // `spawnStreaming()` resolves `exitCode: null` on `child.on("error")`, the *install.py could
  // not be launched* case. The message renders `exited ${exitCode}`, never "exit code", so the
  // branch owes the sentence and the absence of the word `null`.
  const message = installerFailureMessage("spawn ENOENT\n", null, "spawn ENOENT\n");
  assert.match(message, /spawn ENOENT/);
  assert.match(message, /could not be started/);
  assert.doesNotMatch(message, /\bnull\b/);
});

// --- the installer's quiet console (progress lines and install.log) ----------
//
// The installer prints one progress line per step and writes every subprocess's output into
// `install.log`, so a failed run's transcript carries the wrapper's own line, the numbered
// lines, the log's last lines (pip's own errors among them) and the log's path before the
// refusal. These cases witness that the readers read that shape right.

const LOG = "/Users/x/.swcraft/install.log";

// The pip step's argv as `plan_install` builds it for a wheelhouse install on the
// archive's carried Python: the environment is the version directory itself.
const PIP_ARGV =
  "/Users/x/.swcraft/versions/0.16.0/bin/python -m pip install --no-index --find-links " +
  "/tmp/swcraft-x/wheels swcraft-agent==0.16.0";

const PIP_REFUSAL =
  `\`${PIP_ARGV}\` failed with ` +
  "exit code 1, so nothing further was changed and the installation that was there is still the " +
  "current one";

// The shape `install.py` prints for a pip failure on the fourth of the ten steps such a
// plan has: the whole log is shorter than the tail's twenty lines, so the heading counts
// all of it -- the five header lines, each step's `==` lines, the `$` argv lines and pip's
// own output -- and the refusal follows the path.
const NEW_SHAPE_FAILED_TRANSCRIPT =
  "preparing the installer ... done\n" +
  "Installing SWCraft 0.16.0 into /Users/x/.swcraft\n" +
  "1/10 checking the wheels against this machine ... done, 0 s\n" +
  "2/10 unpacking Python ... done, 3 s\n" +
  "3/10 creating the Python environment ... done, 4 s\n" +
  "4/10 installing swcraft-agent and its dependencies ... failed, 9 s\n" +
  `the last 17 lines of ${LOG}:\n` +
  "  SWCraft 0.16.0 install\n" +
  "  date: 2026-10-12T10:15:02+02:00\n" +
  "  platform: posix (macosx-11.0-arm64)\n" +
  "  archive: /tmp/swcraft-x\n" +
  "  argv: --home /Users/x/.swcraft\n" +
  "  == 1/10 checking the wheels against this machine\n" +
  "  == done, 0 s\n" +
  "  == 2/10 unpacking Python\n" +
  "  == done, 3 s\n" +
  "  == 3/10 creating the Python environment\n" +
  "  $ /Users/x/.swcraft/versions/0.16.0/runtime/python/bin/python3 -m venv /Users/x/.swcraft/versions/0.16.0\n" +
  "  == done, 4 s\n" +
  "  == 4/10 installing swcraft-agent and its dependencies\n" +
  `  $ ${PIP_ARGV}\n` +
  "  ERROR: Could not find a version that satisfies the requirement swcraft-mcp==0.16.0\n" +
  "  ERROR: No matching distribution found for swcraft-mcp==0.16.0\n" +
  "  == failed, 9 s\n" +
  `the full log is ${LOG}\n` +
  `${PIP_REFUSAL}\n`;

test("installerFailureMessage: a new-shape transcript still quotes the refusal, not the log's tail", () => {
  const message = installerFailureMessage(NEW_SHAPE_FAILED_TRANSCRIPT, 1, `${PIP_REFUSAL}\n`);
  assert.equal(message, `SWCraft: the toolchain installer exited 1: ${PIP_REFUSAL}`);
  // Neither pip's own error nor the log's path is what the notification shows:
  // both sit above the refusal, which the installer flushes and prints last.
  assert.doesNotMatch(message, /ERROR:/);
  assert.doesNotMatch(message, /the full log is/);
});

test("installerFailureMessage: the refusal is read from stderr whatever order the two pipes arrived in", () => {
  // The installer flushes stdout (the log's tail and its path) before it writes the refusal to
  // stderr, but the parent reads two pipes and may receive the stdout chunk last. This is that
  // arrival order: the notification must still quote the refusal and nothing of the tail.
  const stdout = NEW_SHAPE_FAILED_TRANSCRIPT.replace(`${PIP_REFUSAL}\n`, "");
  const stderr = `${PIP_REFUSAL}\n`;
  const arrivedStderrFirst = `${stdout.slice(0, stdout.indexOf("the last 17"))}${stderr}${stdout.slice(stdout.indexOf("the last 17"))}`;
  assert.match(arrivedStderrFirst.trimEnd(), /the full log is \S+$/);
  for (const output of [`${stdout}${stderr}`, arrivedStderrFirst]) {
    assert.equal(
      installerFailureMessage(output, 1, stderr),
      `SWCraft: the toolchain installer exited 1: ${PIP_REFUSAL}`,
    );
  }
});

test("installerFailureMessage: a run that wrote nothing to stderr falls back to its output's last line", () => {
  // A process killed by a signal, or a sentence some path prints to stdout: the
  // combined output is all there is, so its last line is what is shown.
  assert.equal(
    installerFailureMessage("1/10 checking the wheels ... \nKilled\n", 137, ""),
    "SWCraft: the toolchain installer exited 137: Killed",
  );
});

test("the adapter keeps stderr apart for the failure message and still streams both pipes", () => {
  const body = functionBody(
    withoutCommentLines(readSource("src/commands.ts")),
    "function spawnStreaming(",
  );
  assert.match(body, /child\.stdout\?\.on\("data", collect\)/);
  assert.match(
    body,
    /child\.stderr\?\.on\("data", \(chunk: Buffer\) => \{\s*errors \+= collect\(chunk\);/,
  );
  assert.match(body, /resolve\(\{ exitCode: code, signal, spawnFailed: false, output, errors \}\)/);
});

test("the adapter passes the signal that ended the child on to the failure message", () => {
  // Node's `close` gives `(null, "SIGTERM")` for a killed child; without the signal a killed
  // installer reads as one that "could not be started".
  const body = functionBody(
    withoutCommentLines(readSource("src/commands.ts")),
    "function spawnStreaming(",
  );
  assert.match(body, /child\.on\("close", \(code, signal\) =>/);
});

test("installerFailureMessage: a process ended by a signal is reported as terminated, not as never started", () => {
  const message = installerFailureMessage(
    "4/10 installing swcraft-agent ... \n",
    null,
    "",
    "SIGTERM",
  );
  assert.equal(
    message,
    "SWCraft: the toolchain installer was terminated by SIGTERM: 4/10 installing swcraft-agent ...",
  );
  assert.doesNotMatch(message, /could not be started/);
});

// The same ten-step plan, run to the end on a machine without VS Code's `code`.
const NEW_SHAPE_SUCCESS_WITH_SKIP =
  "preparing the installer ... done\n" +
  "Installing SWCraft 0.16.0 into /Users/x/.swcraft\n" +
  "1/10 checking the wheels against this machine ... done, 0 s\n" +
  "2/10 unpacking Python ... done, 3 s\n" +
  "3/10 creating the Python environment ... done, 4 s\n" +
  "4/10 installing swcraft-agent and its dependencies ... done, 38 s\n" +
  "5/10 unpacking Node.js ... done, 2 s\n" +
  "6/10 placing node ... done, 0 s\n" +
  "7/10 pointing current at 0.16.0 ... done, 0 s\n" +
  "8/10 PATH entry ... done, 0 s\n" +
  'Add this to your shell profile and open a new terminal: export PATH="/Users/x/.swcraft/current/bin:$PATH"\n' +
  "9/10 installing the SWCraft editor extension ... done, 0 s\n" +
  "VS Code's `code` command was not found, so the extension was not installed: install " +
  "/tmp/swcraft-x/swcraft-vscode-0.16.0.vsix from the Extensions view (Install from VSIX) yourself\n" +
  "10/10 removing the installer's temporary Python ... done, 1 s\n" +
  "SWCraft 0.16.0 is installed in /Users/x/.swcraft\n";

// A run whose companion `code` refused: the sentence names the companion, carries neither
// "not installed" fragment, and the run's last line points at the log holding VS Code's reason.
const NEW_SHAPE_SUCCESS_WITH_A_REFUSED_COMPANION =
  "preparing the installer ... done\n" +
  "Installing SWCraft 0.16.0 into /Users/x/.swcraft\n" +
  "7/10 installing the editor extension redhat.vscode-yaml ... done, 2 s\n" +
  "`redhat.vscode-yaml` was not installed (`code` exited with code 1) -- install it by name from the " +
  "Extensions view, or install /tmp/swcraft-x/extensions/redhat.vscode-yaml.vsix there with Install from VSIX\n" +
  "8/10 installing the SWCraft editor extension ... done, 3 s\n" +
  "SWCraft 0.16.0 is installed in /Users/x/.swcraft\n" +
  `the full log is ${LOG}\n`;

test("a successful transcript that points at the log is not read as SWCraft's extension missing", () => {
  assert.equal(extensionWasNotInstalled(NEW_SHAPE_SUCCESS_WITH_A_REFUSED_COMPANION), false);
  assert.match(
    pathAdviceAfterRun("posix", NEW_SHAPE_SUCCESS_WITH_A_REFUSED_COMPANION),
    /already on the PATH/,
  );
});

test("extensionWasNotInstalled and the PATH marker read a new-shape successful transcript", () => {
  // The skip is `done` followed by its unchanged sentence, so the marker is still
  // a substring of the transcript, and so is the POSIX PATH line.
  assert.equal(extensionWasNotInstalled(NEW_SHAPE_SUCCESS_WITH_SKIP), true);
  assert.match(pathAdviceAfterRun("posix", NEW_SHAPE_SUCCESS_WITH_SKIP), /printed the/);
});

test("extensionWasNotInstalled and the PATH marker: progress lines alone carry neither", () => {
  // A step's label must never be mistaken for one of the sentences: the vsix
  // step's line says "installing the SWCraft editor extension ... done", and a
  // run whose PATH entry is already there plans no path step and prints no line.
  const placed =
    "preparing the installer ... done\n" +
    "Installing SWCraft 0.16.0 into /Users/x/.swcraft\n" +
    "1/6 checking the wheels against this machine ... done, 0 s\n" +
    "2/6 creating the Python environment ... done, 4 s\n" +
    "3/6 installing swcraft-agent and its dependencies ... done, 38 s\n" +
    "4/6 pointing current at 0.16.0 ... done, 0 s\n" +
    "5/6 installing the SWCraft editor extension ... done, 6 s\n" +
    "6/6 removing the installer's temporary Python ... done, 1 s\n" +
    "SWCraft 0.16.0 is installed in /Users/x/.swcraft\n";
  assert.equal(extensionWasNotInstalled(placed), false);
  assert.match(pathAdviceAfterRun("posix", placed), /printed no/);
});

// --- the picker's two sources ------------------------------------------------

test("ARCHIVE_SOURCE_ITEMS: the zip and the unpacked directory are picked separately", () => {
  // A single `showOpenDialog` with both `canSelectFiles` and `canSelectFolders` degrades to a
  // *directory* selector on Windows and Linux, which would make the zip — the file a colleague
  // is actually handed — unpickable. So the kind is asked first, and each dialog selects one
  // thing.
  assert.equal(ARCHIVE_SOURCE_ITEMS.length, 2);
  assert.deepEqual(
    ARCHIVE_SOURCE_ITEMS.map((item) => item.source),
    ["zip", "directory"],
  );
  for (const item of ARCHIVE_SOURCE_ITEMS) {
    assert.match(item.label, /\S/);
    assert.match(item.detail, /\S/);
  }
});

// --- the archive's own names ---------------------------------------------------

test("archivePlatformToken: the three hosts an archive is built for, and nothing else", () => {
  assert.equal(archivePlatformToken("win32", "x64"), "windows-x64");
  assert.equal(archivePlatformToken("linux", "x64"), "linux-x64");
  assert.equal(archivePlatformToken("darwin", "arm64"), "macos-arm64");
  assert.equal(archivePlatformToken("darwin", "x64"), undefined);
  assert.equal(archivePlatformToken("win32", "arm64"), undefined);
  assert.equal(archivePlatformToken("freebsd", "x64"), undefined);
});

test("archivePlatformToken: every token is one build_artifacts.py's workstation_platform_token returns", () => {
  const builder = readSource("../scripts/build_artifacts.py");
  const start = builder.indexOf("def workstation_platform_token(");
  assert.notEqual(start, -1, "build_artifacts.py must still define workstation_platform_token");
  const body = builder.slice(start, builder.indexOf("\ndef ", start + 1));
  const returned = [...body.matchAll(/return "([a-z0-9-]+)"/g)].map((match) => match[1]).sort();
  assert.deepEqual(returned, ["linux-x64", "macos-arm64", "windows-x64"]);
  const ours = [
    archivePlatformToken("win32", "x64"),
    archivePlatformToken("linux", "x64"),
    archivePlatformToken("darwin", "arm64"),
  ].sort();
  assert.deepEqual(ours, returned);
});

test("archiveFileName: a placeholder for each part that cannot be known", () => {
  assert.equal(archiveFileName("0.11.0", "linux-x64"), "swcraft-0.11.0-linux-x64.zip");
  assert.equal(archiveFileName("", "macos-arm64"), "swcraft-<version>-macos-arm64.zip");
  assert.equal(archiveFileName("", undefined), "swcraft-<version>-<platform>.zip");
  assert.ok(ARCHIVE_SOURCE_ITEMS[0].detail.includes("swcraft-<version>-<platform>.zip"));
});

test("UPDATE_TOOLCHAIN_TITLE is the manifest's own title for the command", () => {
  const command = readManifest().contributes?.commands?.find(
    (entry) => entry.command === UPDATE_TOOLCHAIN_COMMAND_ID,
  );
  assert.equal(command?.title, UPDATE_TOOLCHAIN_TITLE);
});

// A transcript in which `install.py` did place the archive's extension, and one
// in which it skipped it — the sentence `EXTENSION_NOT_INSTALLED_MARKERS` reads.
const INSTALLED_TRANSCRIPT = "Installed swcraft-vscode-0.7.0.vsix through code.";
const SKIPPED_TRANSCRIPT =
  "`code` was not found on PATH, so the extension was not installed. Install " +
  "swcraft-vscode-0.7.0.vsix from the archive yourself.";

// --- the manifest and the adapter --------------------------------------------
//
// Two kinds of case. Most read the manifest, or `commands.ts` as **text** (it imports
// `vscode`), so they prove what the adapter *says*, never what it does; the seated gate
// exercises that wiring for real. The rest (`pathAdviceAfterRun`, `pendingReloadNote`,
// `quoteArgvForEcho`) call this module's pure helpers and assert the strings the adapter
// renders, which is behaviour witnessed here.

test("package.json declares the two toolchain commands with their titles", () => {
  const byId = new Map(
    (readManifest().contributes?.commands ?? []).map((c) => [c.command, c.title]),
  );
  assert.equal(byId.get(INSTALL_TOOLCHAIN_COMMAND_ID), "SWCraft: Install Toolchain");
  assert.equal(byId.get(UPDATE_TOOLCHAIN_COMMAND_ID), "SWCraft: Update Toolchain");
});

test("both commands activate the extension on their own, with no project open", () => {
  // Installing a toolchain is not a project action: the reader most likely to
  // run it has nothing open yet, and `workspaceContains:swcraft.yaml` would
  // never fire for them.
  const events = (readManifest() as { activationEvents?: string[] }).activationEvents ?? [];
  assert.ok(events.includes(`onCommand:${INSTALL_TOOLCHAIN_COMMAND_ID}`));
  assert.ok(events.includes(`onCommand:${UPDATE_TOOLCHAIN_COMMAND_ID}`));
});

test("the adapter fires the MCP provider's change event after a successful run", () => {
  // After an install the executable that was absent is there, and VS Code has to be told to
  // ask for the definitions again; nothing else in the window will.
  const source = readSource("src/commands.ts");
  assert.match(source, /mcpProvider\.refresh\(\)/);
  const extension = readSource("src/extension.ts");
  assert.match(extension, /registerMcpServerProvider\(context\)/);
  assert.match(
    extension,
    /registerCommands\(context, mcpProvider\)/,
    "the provider is created first and handed to the commands that fire it",
  );
});

test("the adapter's post-run steps happen after the failure return, in their order", () => {
  // An ordering, not presence: firing `refresh()` before checking the exit code would
  // republish a definition set for an executable that is not there after a failed install, so
  // this reads the one function's body and asserts positions.
  const body = functionBody(
    withoutCommentLines(readSource("src/commands.ts")),
    "async function runToolchainCommand(",
  );

  const failed = body.indexOf(
    "installerFailureMessage(run.output, run.exitCode, run.errors, run.signal)",
  );
  const refresh = body.indexOf("mcpProvider.refresh()");
  const note = body.indexOf("pendingReloadNote(");
  const reload = body.indexOf("offersReload(");
  for (const [name, index] of [
    ["installerFailureMessage", failed],
    ["mcpProvider.refresh()", refresh],
    ["pendingReloadNote(", note],
    ["offersReload(", reload],
  ] as const) {
    assert.notEqual(index, -1, `runToolchainCommand's body does not mention ${name}`);
  }
  assert.ok(failed < refresh, "the exit-code failure must return before anything is refreshed");
  assert.ok(refresh < note, "the provider is refreshed before the health line the run reports");
  assert.ok(
    note < reload,
    "the pending-reload note is printed beside the health line, before the offer",
  );
});

test("pathAdviceAfterRun: the printed line is named only when it was actually printed", () => {
  // This notification fires only when the version did not change (a repair or a reinstall),
  // and `install.py` plans its `path` step only when the entry is absent from the environment
  // it was spawned with (`cliEnv()`), so in the common case nothing was printed and the
  // sentence must not say the line is in the output channel above.
  const printed = pathAdviceAfterRun(
    "posix",
    'Installing SWCraft 0.7.0\nAdd this to your shell profile and open a new terminal: export PATH="/h/.swcraft/current/bin:$PATH"\n',
  );
  assert.match(printed, /printed the `export PATH=…` line/);

  const silent = pathAdviceAfterRun(
    "posix",
    "Installing SWCraft 0.7.0\nSWCraft 0.7.0 is installed in /h/.swcraft\n",
  );
  assert.match(silent, /already on the PATH this window was launched with/);
  assert.doesNotMatch(silent, /printed the `export PATH=…` line/);
});

test("pathAdviceAfterRun: Windows says the same thing either way, because nothing is printed there", () => {
  // The entry goes into `HKCU\Environment` and the installer prints no line on
  // either path, so there is nothing to read and the advice does not split.
  for (const output of ["", 'Add this to your shell profile: export PATH="x:$PATH"']) {
    const advice = pathAdviceAfterRun("windows", output);
    assert.match(advice, /Open a new terminal/);
    assert.doesNotMatch(advice, /shell profile/);
  }
});

test("the adapter emits the sideload note BEFORE the failure return", () => {
  // The note answers three installer outcomes, two of them refusals, so a call placed after
  // the exit-code check is never reached on the paths that need it most, and the `finally`
  // then deletes the directory the quoted `.vsix` path names. The mirror of the ordering
  // above: everything else is post-run, this one is not.
  const body = functionBody(
    withoutCommentLines(readSource("src/commands.ts")),
    "async function runToolchainCommand(",
  );

  const sideload = body.indexOf("sideloadNote(run.output");
  const failed = body.indexOf(
    "installerFailureMessage(run.output, run.exitCode, run.errors, run.signal)",
  );
  assert.notEqual(sideload, -1, "runToolchainCommand's body does not call sideloadNote");
  assert.notEqual(failed, -1, "runToolchainCommand's body does not call installerFailureMessage");
  assert.ok(
    sideload < failed,
    "the sideload note must be emitted before the failure return, or the two branches that raise " +
      "quote a .vsix path in a directory this command is about to delete and say nothing about it",
  );
});

test("the adapter refuses a second toolchain run while one is in flight", () => {
  // A run is minutes of pip output with no progress UI and no cancellation, so a second click
  // is expected, and two runs against the same `--home` can have one delete the version
  // directory the other has just linked `current` at. The assertions are on
  // `toolchainCommand`'s own body and the refusal's shape: identifier matches are satisfied
  // by the try/finally alone.
  //
  // What this does NOT reach: whether the refusal's message is the right one, and whether the
  // flag is released on every path — the `finally` is asserted as text, not exercised.
  const guarded = functionBody(
    withoutCommentLines(readSource("src/commands.ts")),
    "async function toolchainCommand(",
  );
  assert.match(
    guarded,
    /if \(toolchainRunInFlight\) \{[\s\S]{0,400}?return;\s*\}/,
    "`toolchainCommand` reads the flag and returns without starting a second run",
  );
  const takenAt = guarded.indexOf("toolchainRunInFlight = true");
  assert.notEqual(takenAt, -1, "the flag is taken");
  assert.ok(
    takenAt < guarded.indexOf("await "),
    "the flag is taken BEFORE the first await, or a second click between the check and the await passes it",
  );
  assert.match(
    guarded,
    /finally \{\s*toolchainRunInFlight = false;/,
    "and released in a `finally`",
  );
});

test("the adapter refuses a toolchain run while a Build or a diagram generation is running", () => {
  // A Build spawns its six steps through `<home>/current/bin/swcraft`, and an update switches
  // `current` and removes the other version directories, so an install overlapping it could
  // mix versions across the steps or remove the tree a step runs from. The check comes before
  // the flag is taken (a refused click leaves the flag alone) and before the first await (so
  // nothing queues between the check and the flag). The other direction is pinned in
  // commandCore.test.ts. What this does NOT reach: the commands outside `viewsRootWrites`, two
  // windows, and a terminal.
  const guarded = functionBody(
    withoutCommentLines(readSource("src/commands.ts")),
    "async function toolchainCommand(",
  );
  // The refresh a green Build ends with runs outside that queue, from the same installation.
  const busyGuard =
    /if \(viewsRootWrites\.busy \|\| resourceRefreshRun !== undefined\) \{[\s\S]{0,400}?return;\s*\}/;
  const at = guarded.search(busyGuard);
  assert.notEqual(at, -1, "`toolchainCommand` reads the runner and returns without starting a run");
  assert.match(guarded.match(busyGuard)?.[0] ?? "", /fail\(viewsRootWriteRunningRefusal\(\)\);/);
  const takenAt = guarded.indexOf("toolchainRunInFlight = true");
  assert.ok(at < takenAt, "checked before the flag is taken");
  assert.ok(at < guarded.indexOf("await "), "and before the first await");
});

test("the adapter never lets the temp-directory cleanup throw out of the command", () => {
  // `force: true` suppresses ENOENT only, not the EPERM/EBUSY a Windows handle produces; an
  // escaping throw would show the success notification *and* a command-failure error.
  const source = withoutCommentLines(readSource("src/commands.ts"));
  assert.match(source, /maxRetries/);
  assert.match(source, /try \{\s*fs\.rmSync\(/);
});

test("the version probe is given a timeout", () => {
  // `spawnCliCapture()` resolves only on close/error, so a wedged executable would hang *Check
  // Editor Tooling* with no cancellation, and *Update Toolchain* after the unpack, so its
  // `finally` never runs and the wheels stay in the temp directory.
  const source = withoutCommentLines(readSource("src/commands.ts"));
  assert.match(source, /VERSION_PROBE_TIMEOUT_MS/);
  assert.match(source, /probeToolchainVersion[\s\S]{0,400}VERSION_PROBE_TIMEOUT_MS/);
});

test("the MCP definition set is refreshed by every event that changes it", () => {
  // Three inputs change while a window is open and each must fire `refresh()`: onboarding turns
  // "no server" into "one server", and a multi-root window's label depends on the project
  // count. Each handler's own body is read, since an identifier match survives an empty
  // handler.
  //
  // What this does NOT reach: the fourth changer is the toolchain commands, whose
  // `mcpProvider.refresh()` is asserted where those commands are; nor whether `refresh()`
  // reaches VS Code — that is mcpProvider.test.ts's.
  const source = withoutCommentLines(readSource("src/commands.ts"));
  assert.match(
    source,
    /onDidChangeWorkspaceFolders\(\(\) => provider\.refresh\(\)\)/,
    "a folder added to or removed from the window changes the set and every label",
  );
  assert.match(
    source,
    /onDidChangeConfiguration\(\(event\) => \{[\s\S]{0,300}?affectsConfiguration\(EXECUTABLE_SETTING\)[\s\S]{0,120}?provider\.refresh\(\)/,
    "`swcraft.executable` names the command every published definition spawns",
  );
  assert.match(
    // `runOnboarding` is the part of the command that spawns; the command itself
    // asks for the folder and hands it over inside the run indicator.
    functionBody(source, "async function runOnboarding("),
    /mcpProvider\.refresh\(\)/,
    'onboarding a project turns "no server" into "one server" in the walkthrough\'s own flow',
  );
});

test("the setting-shadowed note names the setting and the home it hides", () => {
  // `swcraft.executable` is an override with nothing else probed, while both toolchain
  // commands install into `toolchainHome(...)` regardless; the *Update* decision and the
  // post-run health line are bound to the installation, so this note says, once, when the
  // setting shadows it.
  assert.equal(settingShadowsInstallationNote(undefined, "/Users/dev/.swcraft"), undefined);
  assert.equal(settingShadowsInstallationNote("   ", "/Users/dev/.swcraft"), undefined);
  const note = settingShadowsInstallationNote("/w/.venv/bin/swcraft", "/Users/dev/.swcraft");
  assert.ok(note !== undefined);
  assert.match(note, /swcraft\.executable/, "the setting is named, so the reader can find it");
  assert.match(note, /\/w\/\.venv\/bin\/swcraft/, "and so is what it points at");
  assert.match(note, /\/Users\/dev\/\.swcraft/, "and the installation this run actually changed");
});

test("the panel's commands that need no project are the three items and two menu entries the guide names", () => {
  // The three are Setup's tree items (Install Toolchain, Update Toolchain, Onboard Project);
  // the two are the entries of the view's `…` menu (Open Guide, Check Editor Tooling), which
  // need no project either. No command carries an `enablement` clause, so nothing greys them
  // out.
  //
  // The set is derived from the handlers: a command that reaches `resolveManifestOrNotify()`
  // or `showNoWorkspaceError()` needs a project, one that reaches neither does not. The page
  // must name each of the rest and nothing else.
  //
  // What this does NOT reach: what a folder-less run then *does* — `onboardProject` opens a
  // picker, the toolchain commands install into the home, *Open Guide* serves from the
  // installation — which the page says in prose this cannot check.
  const source = withoutCommentLines(readSource("src/commands.ts"));
  const handlers = new Map<string, string>();
  for (const match of source.matchAll(
    /vscode\.commands\.registerCommand\("(swcraft\.[A-Za-z0-9]+)",\s*\((?:\w+(?:,\s*\w+)?)?\)\s*=>\s*([A-Za-z0-9]+)/g,
  )) {
    handlers.set(match[1] as string, match[2] as string);
  }
  // Every declared command, not a floor: the manifest count is what the regex has to reach, so
  // a handler spelled in a shape it cannot read fails here instead of silently dropping out.
  const declared = (
    JSON.parse(readSource("package.json")) as { contributes: { commands: unknown[] } }
  ).contributes.commands.length;
  assert.equal(
    handlers.size,
    declared,
    `only ${handlers.size} command handlers were read out of commands.ts, for ${declared} declared commands`,
  );

  const needsNoProject: string[] = [];
  for (const [id, handler] of handlers) {
    const body = source.includes(`async function ${handler}(`)
      ? functionBody(source, `async function ${handler}(`)
      : (source.split(`function ${handler}(`)[1] ?? "");
    if (!/resolveManifestOrNotify\(\)|showNoWorkspaceError\(\)/.test(body)) {
      needsNoProject.push(id);
    }
  }

  const page = readSource("../docs/guide/vscode.md");
  const claimed = needsNoProject.filter((id) => page.includes(id));
  assert.deepEqual(
    claimed.sort(),
    needsNoProject.sort(),
    "docs/guide/vscode.md must name every panel item that works with no folder open: " +
      needsNoProject.join(", "),
  );
  assert.match(page, /needs? no project/, "and it must still say so in words the reader can find");
  assert.doesNotMatch(
    page,
    /the only item in the panel that needs no project/,
    "the singular claim was false four ways since D12",
  );
});

test("functionBody stops at the next top-level declaration, whatever its keyword", () => {
  // A slice ending only at the next `\nasync function ` reads past every other declaration kind;
  // a guard wider than it reads passes for reasons nobody chose.
  //
  // What this does NOT reach: a declaration at column zero inside a template literal, and an
  // arrow function assigned to a `const`, which no terminator matches — the docstring says
  // both.
  const source = [
    "async function subject() {",
    "  return 1;",
    "}",
    "",
    "export function afterIt() {",
    "  return 2;",
    "}",
    "",
  ].join("\n");
  const body = functionBody(source, "async function subject(");
  assert.match(body, /return 1;/);
  assert.doesNotMatch(body, /afterIt/, "the slice must stop at the next top-level declaration");

  const trailingClass = [
    "async function subject() {",
    "  return 1;",
    "}",
    "",
    "class After {}",
    "",
  ].join("\n");
  assert.doesNotMatch(functionBody(trailingClass, "async function subject("), /After/);

  const nested = [
    "async function subject() {",
    "  const inner = {",
    "    function: 1,",
    "  };",
    "  return inner;",
    "}",
    "",
  ].join("\n");
  assert.match(
    functionBody(nested, "async function subject("),
    /return inner;/,
    "an indented `function` is not an end",
  );

  const last = ["async function subject() {", "  return 1;", "}", ""].join("\n");
  assert.match(
    functionBody(last, "async function subject("),
    /return 1;/,
    "the last declaration reads to EOF",
  );
});

test("the in-flight flag is released before the reload offer is awaited", () => {
  // `await showInformationMessage(msg, "Reload Window")` resolves only when the user answers
  // or dismisses, and a notification with a button does not auto-dismiss. Inside the body the
  // in-flight flag guards, it would keep both toolchain commands refusing until the window was
  // reloaded. So the run returns the offer and `toolchainCommand` makes it after the
  // `finally`: the guarded body awaits no notification, and the offer is textually after the
  // release.
  //
  // What this does NOT reach: whether VS Code really keeps that promise pending, documented
  // behaviour no source guard can observe.
  const source = withoutCommentLines(readSource("src/commands.ts"));
  const guarded = functionBody(source, "async function runToolchainCommand(");
  assert.doesNotMatch(
    guarded,
    /await vscode\.window\.show(Information|Warning|Error)Message\(/,
    "the guarded body awaits no notification — every one of them is `void`-ed or returned",
  );
  const outer = functionBody(source, "async function toolchainCommand(");
  const released = outer.indexOf("toolchainRunInFlight = false");
  const offered = outer.indexOf('"Reload Window"');
  assert.notEqual(released, -1, "the flag is released in `toolchainCommand`");
  assert.notEqual(offered, -1, "and the reload offer is made there");
  assert.ok(released < offered, "the flag is released BEFORE the offer the user may never answer");
});

test("the toolchain commands read the installation and never the executable override", () => {
  // A source guard, because the two readings are adapter functions that spawn a process:
  // `runToolchainCommand` never reaches `executableSetting()`, directly or through a helper
  // that defaults to it, so the decision and the report are about `toolchainHome(...)`, where
  // the installer writes.
  //
  // What this does NOT reach: whether `checkEditorTooling`'s health report still honours the
  // setting — the one caller that should; healthCheck.test.ts's `checkToolchain` cases assert
  // the "setting" source.
  const source = withoutCommentLines(readSource("src/commands.ts"));
  const body = functionBody(source, "async function runToolchainCommand(");
  assert.doesNotMatch(
    body,
    /(installedToolchainVersion|readToolchainHealth)\([^)]*executableSetting\(\)/,
    "neither reading resolves through the override",
  );
  assert.match(
    body,
    /installedToolchainVersion\(undefined\)/,
    "the update decision reads the installation",
  );
  assert.match(
    body,
    /readToolchainHealth\(context, undefined\)/,
    "and so does the post-run report",
  );
  assert.match(
    body,
    /settingShadowsInstallationNote\(executableSetting\(\), home\)/,
    "and a set override is named once, beside the health line it contradicts",
  );
});

test("a pending reload is explained beside the health line it contradicts", () => {
  // The health re-read compares against the still-in-memory extension version, so a
  // successful *update* prints a `[problem]`-class mismatch line right above the *Reload
  // Window* offer that fixes it; this note explains it.
  assert.equal(pendingReloadNote("0.7.0", "0.7.0", INSTALLED_TRANSCRIPT), undefined);
  const note = pendingReloadNote("0.7.0", "0.6.0", INSTALLED_TRANSCRIPT);
  assert.ok(note !== undefined);
  assert.match(note, /0\.6\.0/);
  assert.match(note, /reload/i);
  const source = withoutCommentLines(readSource("src/commands.ts"));
  assert.match(source, /pendingReloadNote\(/);
});

test("the pending-reload note stops promising what the skipped .vsix cannot deliver", () => {
  // When the installer skipped the extension (a GUI-launched macOS VS Code), reloading makes
  // nothing agree: the line must not promise it while the notification says the extension
  // was not installed.
  const contradicted = pendingReloadNote("0.7.0", "0.6.0", SKIPPED_TRANSCRIPT);
  assert.ok(contradicted !== undefined, "the mismatch above still needs explaining");
  assert.doesNotMatch(
    contradicted,
    /makes both sides agree/,
    "a reload cannot make the sides agree when the newer extension never went in",
  );
  assert.match(contradicted, /not installed/i);
  assert.match(contradicted, /\.vsix/i, "…and the note says what would fix it instead");
  assert.match(contradicted, /0\.7\.0/);

  // One reading of `run.output` answers the channel line, the sideload note and the
  // notification, so the three cannot disagree.
  const source = withoutCommentLines(readSource("src/commands.ts"));
  assert.match(
    source,
    /pendingReloadNote\(version\.version, extensionVersion\(context\), run\.output\)/,
    "the note is answered from the same transcript as versionChangedNotice",
  );
});

test("no doc comment sits on another doc comment instead of the function it describes", () => {
  // A `*/` followed by a `/**` with nothing but blank lines between them is a doc block
  // documenting another doc block: the function beneath loses its JSDoc and hover shows the
  // wrong one. What it does not reach: a doc comment that is simply *wrong* about the
  // function directly beneath it.
  const offences: string[] = [];
  for (const module of productionModules()) {
    const lines = readSource(path.posix.join("src", module)).split("\n");
    for (let i = 0; i < lines.length; i += 1) {
      if (lines[i].trim() !== "*/") continue;
      let next = i + 1;
      while (next < lines.length && lines[next].trim() === "") next += 1;
      if (next < lines.length && lines[next].trim().startsWith("/**")) {
        offences.push(
          `${module}:${i + 1} — a doc block ends and another begins, with no code between`,
        );
      }
    }
  }
  assert.deepEqual(offences, [], offences.join("\n"));
});

test("the echoed installer command line is retypable when a path holds a space", () => {
  // `spawnStreaming`'s doc comment says the echo is a line a reader may retype, which needs
  // quoting for `C:\\Program Files\\...`.
  assert.equal(
    quoteArgvForEcho(["/usr/bin/python3.11", "/tmp/a b/install.py"]),
    '/usr/bin/python3.11 "/tmp/a b/install.py"',
  );
  assert.equal(quoteArgvForEcho(["swcraft", "--version"]), "swcraft --version");
});

test("the adapter offers a window reload rather than reloading by itself", () => {
  const source = withoutCommentLines(readSource("src/commands.ts"));
  assert.match(source, /workbench\.action\.reloadWindow/);
  assert.match(
    source,
    /offersReload\(/,
    "the decision is toolchainInstall.ts's; the adapter only asks",
  );
});

test("no production module embeds the installer or the wheels", () => {
  // The extension *drives* the archive: it unpacks what the user points it at and runs the
  // `install.py` inside it. Installer steps of its own would be a second installer with
  // nothing keeping the two in step.
  assertNoProductionModuleNames(
    ["--find-links", "--no-index", "--force-reinstall"],
    "installing is install.py's, and the extension only runs it",
  );
});

test("the toolchain commands look for no Python and name none", () => {
  // The archive carries its interpreter and its own wrapper starts `install.py` on it, so a
  // Python lookup here would put back the prerequisite the archive exists to remove, and
  // `--python <that Python>` would build on the machine's interpreter. Read with comment
  // lines removed: a comment may name what it replaced.
  const module = withoutCommentLines(readSource("src/toolchainInstall.ts"));
  for (const spelling of [
    "python3.11",
    "py -3",
    '"py"',
    "--python",
    "zipfile",
    "REQUIRED_PYTHON",
  ]) {
    assert.equal(module.includes(spelling), false, `toolchainInstall.ts names ${spelling}`);
  }
  const body = functionBody(
    withoutCommentLines(readSource("src/commands.ts")),
    "async function runToolchainCommand(",
  );
  assert.doesNotMatch(body, /python/i, "runToolchainCommand resolves no interpreter");
});

test("the adapter asks for a zip unpacker only for a zip, after the pick and before the unpack", () => {
  // A picked folder needs no program to open it, so it must not be refused for the lack of
  // one; and the lookup comes after the picker, or cancelling the picker would follow a
  // refusal about a zip nobody chose. The picker is `toolchainCommand`'s (a dialog is the
  // user's turn and carries no run indicator), so the two halves are read in the two
  // functions and their order holds across them.
  const source = withoutCommentLines(readSource("src/commands.ts"));
  const caller = functionBody(source, "async function toolchainCommand(");
  const picked = caller.indexOf("pickArchiveSource(");
  assert.notEqual(picked, -1, "toolchainCommand's body does not mention pickArchiveSource(");
  assert.ok(
    picked < caller.indexOf("runToolchainCommand("),
    "the archive is chosen before the run it belongs to starts",
  );
  const body = functionBody(source, "async function runToolchainCommand(");
  assert.equal(body.indexOf("pickArchiveSource("), -1, "the run does not ask again");
  const zipBranch = body.indexOf('if (source.source === "zip")');
  const lookup = body.indexOf("findZipUnpacker(");
  const unpack = body.indexOf("buildUnpackArgv(");
  for (const [name, index] of [
    ['if (source.source === "zip")', zipBranch],
    ["findZipUnpacker(", lookup],
    ["buildUnpackArgv(", unpack],
  ] as const) {
    assert.notEqual(index, -1, `runToolchainCommand's body does not mention ${name}`);
  }
  assert.ok(zipBranch < lookup && lookup < unpack, body);
});

test("the adapter refuses before spawning when the installer argv is a refusal", () => {
  const body = functionBody(
    withoutCommentLines(readSource("src/commands.ts")),
    "async function runToolchainCommand(",
  );
  assert.match(
    body,
    /const installer = buildInstallerArgv\(archiveRoot, home, platform, process\.env\);\s*if \("refusal" in installer\) \{\s*fail\(installer\.refusal\);\s*return;\s*\}\s*const run = await spawnStreaming\(\s*installer\.argv,/,
  );
});

test("a toolchain run stops no diagram viewer: there is none, and the watch is left armed", () => {
  // Nothing of the installation runs between two diagram generations, and a save during the
  // run only marks the artifacts out of date; the run still refuses while a generation is
  // queued or running (`viewsRootWrites.busy`, pinned above). What this does NOT reach: the
  // MCP server's Python, which is not this extension's to stop.
  const source = withoutCommentLines(readSource("src/commands.ts"));
  for (const name of ["async function runToolchainCommand(", "async function toolchainCommand("]) {
    const body = functionBody(source, name);
    assert.equal(
      /stopDiagrams|diagramsSession|diagramsWatch|\.dispose\(\)/.test(body),
      false,
      name,
    );
  }
  for (const mode of ["install", "update"]) {
    assert.match(
      source,
      new RegExp(`toolchainCommand\\("${mode}", context, outputChannel, mcpProvider\\)`),
      `${mode} is handed no diagram state`,
    );
  }
});

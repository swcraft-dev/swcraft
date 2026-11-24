// Unit tests for the workstation-installation resolution.
//
// The property the module exists to make true: a SWCraft executable comes from **one** place,
// the workstation installation the archive's installer wrote, plus **one** explicit override,
// the `swcraft.executable` setting. No project `.venv`, no proto shim, no PATH lookup: the
// candidate list is a single entry either way, and the `exists` spy below is what says so.

import assert from "node:assert/strict";
import * as path from "node:path";
import { test } from "node:test";
import {
  EXPERIMENT_KINDS,
  METRICS_SINCE_SETTING,
  REQUIREMENTS_MAPPING_SETTING,
} from "./commandCore";
import {
  productionModules,
  readManifest,
  readSource,
  spy,
  withoutCommentLines,
} from "./testSupport";
import { TEST_TABLES_ANNOTATIONS_SETTING } from "./testTablesTab";
import {
  compareReleases,
  EXECUTABLE_SETTING,
  installedSwcraft,
  parseSwcraftVersion,
  resolveOnPath,
  resolveSwcraft,
  SWCRAFT_COMMAND_NAME,
  samePath,
  toolchainHome,
} from "./toolchain";

// --- toolchainHome -----------------------------------------------------------

test("toolchainHome: %LOCALAPPDATA%\\SWCraft on Windows", () => {
  assert.equal(
    toolchainHome({ LOCALAPPDATA: "C:\\Users\\dev\\AppData\\Local" }, "C:\\Users\\dev", "windows"),
    "C:\\Users\\dev\\AppData\\Local\\SWCraft",
  );
});

test("toolchainHome: ~/.swcraft elsewhere, and LOCALAPPDATA is ignored there", () => {
  // A macOS or Linux host with a stray LOCALAPPDATA (a Wine or CI environment
  // sets one) must still install into the POSIX home: the platform decides the
  // layout, never the environment.
  assert.equal(
    toolchainHome({ LOCALAPPDATA: "/somewhere/else" }, "/Users/dev", "posix"),
    "/Users/dev/.swcraft",
  );
});

test("toolchainHome: Windows without LOCALAPPDATA falls back to USERPROFILE's Local", () => {
  // The same two-step `install.py`'s `default_home()` takes, so the extension
  // and the installer name the same directory on a host where the variable is
  // missing (a service account, a stripped-down environment block).
  assert.equal(
    toolchainHome({ USERPROFILE: "C:\\Users\\dev" }, "C:\\Users\\other", "windows"),
    "C:\\Users\\dev\\AppData\\Local\\SWCraft",
  );
});

test("toolchainHome: with neither variable it answers from homeDir rather than throwing", () => {
  // Where the extension deliberately diverges from `install.py`, which refuses:
  // this function is called by every command and by the health check, and a
  // resolver that throws would replace one diagnosable finding ("that path is
  // not there") with an exception on every surface at once. `os.homedir()`
  // always answers.
  assert.equal(
    toolchainHome({}, "C:\\Users\\dev", "windows"),
    "C:\\Users\\dev\\AppData\\Local\\SWCraft",
  );
});

// --- installedSwcraft --------------------------------------------------------

test("installedSwcraft: <home>\\current\\Scripts\\swcraft.exe on Windows", () => {
  assert.equal(
    installedSwcraft("C:\\Users\\dev\\AppData\\Local\\SWCraft", "windows"),
    "C:\\Users\\dev\\AppData\\Local\\SWCraft\\current\\Scripts\\swcraft.exe",
  );
});

test("installedSwcraft: <home>/current/bin/swcraft elsewhere", () => {
  assert.equal(
    installedSwcraft("/Users/dev/.swcraft", "posix"),
    "/Users/dev/.swcraft/current/bin/swcraft",
  );
});

test("installedSwcraft: it is the `current` link, never a version directory", () => {
  // The installer switches `current` and the extension follows it; an
  // extension that resolved `versions/<v>` would keep spawning the version it
  // was installed beside after an update.
  assert.ok(installedSwcraft("/h", "posix").includes("/current/"));
  assert.ok(!installedSwcraft("/h", "posix").includes("versions"));
});

// --- resolveSwcraft ----------------------------------------------------------

test("resolveSwcraft: with no setting the installation is the answer, and the only candidate", () => {
  const { exists, probed } = spy(() => true);
  const resolution = resolveSwcraft(undefined, {}, "/Users/dev", "posix", exists);
  assert.equal(resolution.executable, "/Users/dev/.swcraft/current/bin/swcraft");
  assert.equal(resolution.source, "installation");
  assert.equal(resolution.exists, true);
  assert.deepEqual(probed, ["/Users/dev/.swcraft/current/bin/swcraft"]);
});

test("resolveSwcraft: the swcraft.executable setting wins and nothing else is probed", () => {
  // The developer case the setting exists for: the extension run against a
  // checkout, pointed at that checkout's own `.venv/bin/swcraft`. The spy is
  // the assertion — a fallback probe of the installation would show up here as
  // a second candidate, and the setting would then be a preference rather than
  // an override.
  const { exists, probed } = spy(() => true);
  const resolution = resolveSwcraft(
    "/src/swcraft/.venv/bin/swcraft",
    { LOCALAPPDATA: "C:\\l" },
    "/Users/dev",
    "posix",
    exists,
  );
  assert.equal(resolution.executable, "/src/swcraft/.venv/bin/swcraft");
  assert.equal(resolution.source, "setting");
  assert.deepEqual(probed, ["/src/swcraft/.venv/bin/swcraft"]);
});

test("resolveSwcraft: a setting naming a path that is not there still wins, and is reported absent", () => {
  // Falling back to the installation would be the silent repair that makes a
  // typo in a setting undiagnosable: the health check names the path it was
  // given, and it can only do that if the resolution kept it.
  const resolution = resolveSwcraft("/typo/swcraft", {}, "/Users/dev", "posix", () => false);
  assert.equal(resolution.executable, "/typo/swcraft");
  assert.equal(resolution.source, "setting");
  assert.equal(resolution.exists, false);
});

test("resolveSwcraft: an empty or whitespace-only setting is not a setting", () => {
  // What an unset setting looks like once a user has opened and cleared the
  // field: `""` from the settings UI, `"   "` from a hand-edited settings.json.
  for (const setting of ["", "   ", undefined]) {
    const resolution = resolveSwcraft(setting, {}, "/Users/dev", "posix", () => true);
    assert.equal(resolution.source, "installation");
    assert.equal(resolution.executable, "/Users/dev/.swcraft/current/bin/swcraft");
  }
});

test("resolveSwcraft: a setting is trimmed, so a pasted path with a trailing space still resolves", () => {
  const resolution = resolveSwcraft("  /src/.venv/bin/swcraft  ", {}, "/h", "posix", () => true);
  assert.equal(resolution.executable, "/src/.venv/bin/swcraft");
});

test("resolveSwcraft: the Windows installation is resolved with Windows separators", () => {
  const resolution = resolveSwcraft(
    undefined,
    { LOCALAPPDATA: "C:\\Users\\dev\\AppData\\Local" },
    "C:\\Users\\dev",
    "windows",
    () => true,
  );
  assert.equal(
    resolution.executable,
    "C:\\Users\\dev\\AppData\\Local\\SWCraft\\current\\Scripts\\swcraft.exe",
  );
});

// --- no runtime of the extension's own ----------------------------------------
//
// Diagrams are files written by `swcraft generate diagrams`, which finds its Node.js itself
// (the installation's first, `PATH` second — the CLI's rule, pinned on the Python side), so
// the extension resolves no runtime and starts no `node`.

test("toolchain.ts answers where swcraft is and nothing else: no Node.js or LikeC4 resolution", async () => {
  // What this does NOT reach: a resolution under a name none of these spell.
  const exported = Object.keys(await import("./toolchain"));
  for (const retired of ["installedNode", "installedLikec4", "resolveRuntime"]) {
    assert.equal(exported.includes(retired), false, `toolchain.ts still exports ${retired}`);
  }
  const offences: string[] = [];
  for (const module of productionModules()) {
    const source = withoutCommentLines(readSource(path.posix.join("src", module)));
    for (const spelling of ["likec4.mjs", "node.exe", '"runtime"', "'runtime'"]) {
      if (source.includes(spelling)) {
        offences.push(`${module} spells ${spelling}`);
      }
    }
    for (const pattern of [
      /["']bin["']\s*,\s*["']node["']/,
      /\/bin\/node\b/,
      /\b(spawn|execFile|which|lookup)\(\s*["'](node|likec4)(\.exe|\.cmd)?["']/,
    ]) {
      if (pattern.test(source)) {
        offences.push(`${module} matches ${pattern}`);
      }
    }
  }
  assert.deepEqual(offences, [], "the extension locates and starts no Node.js of its own");
});

// --- resolveOnPath -----------------------------------------------------------

test("resolveOnPath: answers where a bare `swcraft` resolves, in PATH order", () => {
  // The question the health check asks, and it is deliberately not "is the
  // installation's directory listed on PATH": an activated project `.venv`
  // earlier in the list shadows the installation, and a listing check cannot
  // see that at all.
  const resolved = resolveOnPath(
    { PATH: "/work/proj/.venv/bin:/Users/dev/.swcraft/current/bin" },
    "posix",
    (candidate) =>
      candidate === "/work/proj/.venv/bin/swcraft" ||
      candidate === "/Users/dev/.swcraft/current/bin/swcraft",
  );
  assert.equal(resolved, "/work/proj/.venv/bin/swcraft");
});

test("resolveOnPath: a directory that holds no swcraft is skipped, not answered", () => {
  const resolved = resolveOnPath(
    { PATH: "/usr/bin:/Users/dev/.swcraft/current/bin" },
    "posix",
    (candidate) => candidate === "/Users/dev/.swcraft/current/bin/swcraft",
  );
  assert.equal(resolved, "/Users/dev/.swcraft/current/bin/swcraft");
});

test("resolveOnPath: nothing on PATH yields undefined, which is a finding and not an error", () => {
  assert.equal(
    resolveOnPath({ PATH: "/usr/bin:/bin" }, "posix", () => false),
    undefined,
  );
  assert.equal(
    resolveOnPath({}, "posix", () => true),
    undefined,
  );
  assert.equal(
    resolveOnPath({ PATH: "" }, "posix", () => true),
    undefined,
  );
});

test("resolveOnPath: Windows splits on ';', strips quotes and tries .exe before .cmd", () => {
  const probed: string[] = [];
  const resolved = resolveOnPath(
    { PATH: '"C:\\tools";C:\\Users\\dev\\AppData\\Local\\SWCraft\\current\\Scripts' },
    "windows",
    (candidate) => {
      probed.push(candidate);
      return candidate.endsWith("Scripts\\swcraft.exe");
    },
  );
  assert.equal(resolved, "C:\\Users\\dev\\AppData\\Local\\SWCraft\\current\\Scripts\\swcraft.exe");
  assert.deepEqual(probed.slice(0, 2), ["C:\\tools\\swcraft.exe", "C:\\tools\\swcraft.cmd"]);
});

// --- samePath ----------------------------------------------------------------

test("samePath: Windows compares case-insensitively and across separators", () => {
  // Otherwise the PATH warning fires on every Windows host that spells the
  // same directory differently from the environment block — a warning nobody
  // can act on is worse than no warning.
  assert.equal(
    samePath(
      "C:\\SWCraft\\current\\Scripts\\swcraft.exe",
      "c:/swcraft/current/scripts/swcraft.exe",
      "windows",
    ),
    true,
  );
});

test("samePath: POSIX is case-sensitive, because the filesystem question is", () => {
  assert.equal(
    samePath("/h/.swcraft/current/bin/swcraft", "/h/.SWCraft/current/bin/swcraft", "posix"),
    false,
  );
  assert.equal(
    samePath("/h/.swcraft/current/bin/swcraft", "/h/.swcraft/current/bin/swcraft", "posix"),
    true,
  );
});

// --- parseSwcraftVersion -----------------------------------------------------

test("parseSwcraftVersion: reads the CLI's own `--version` line", () => {
  // `swcraft --version` prints `swcraft <version>` (argparse's `%(prog)s %(version)s`), and
  // `packages/tools/tests/test_version_policy.py` runs the real CLI against that shape. The
  // version is **read** from `package.json`, a lockstep site: a literal would be a bump site
  // nothing lists, red on the next release.
  const version = readManifest().version;
  assert.ok(typeof version === "string" && version.length > 0, "package.json declares a version");
  assert.equal(parseSwcraftVersion(`swcraft ${version}\n`), version);
});

test("parseSwcraftVersion: no fixture here spells a release literally", () => {
  // The rule the case above states, asserted where the file it is about can see it;
  // `packages/tools/tests/test_version_policy.py` keeps the half only the CLI can answer: that
  // `swcraft --version` really prints a line `parseSwcraftVersion` reads.
  //
  // What this does not reach: anything but `parseSwcraftVersion(` call sites in this file. A
  // literal release written into some other helper's fixture is invisible here.
  const source = readSource("src/toolchain.test.ts");
  const literals = source.match(/parseSwcraftVersion\("swcraft \d/g) ?? [];
  assert.deepEqual(
    literals,
    [],
    "a literal `swcraft <version>` fixture is a bump site that appears on no version-bump list, " +
      "and it goes red on the next release. Derive it from readManifest().version instead",
  );
});

test("parseSwcraftVersion: a bare version, and trailing whitespace, both read", () => {
  // Deliberately not this product's version: these two are about the *shape* the
  // reader accepts, and a fixture that happened to equal the current release would
  // read as a lockstep claim it is not making.
  assert.equal(parseSwcraftVersion("9.9.9"), "9.9.9");
  assert.equal(parseSwcraftVersion("  swcraft 9.9.9  \r\n"), "9.9.9");
});

test("parseSwcraftVersion: output that carries no version is undefined, never a guess", () => {
  assert.equal(parseSwcraftVersion(""), undefined);
  assert.equal(parseSwcraftVersion("   \n"), undefined);
  assert.equal(parseSwcraftVersion("Traceback (most recent call last):\n"), undefined);
});

// --- The names the rest of the extension spells --------------------------------

test("the command name and the setting id are single-sourced here", () => {
  assert.equal(SWCRAFT_COMMAND_NAME, "swcraft");
  assert.equal(EXECUTABLE_SETTING, "swcraft.executable");
});

// --- The settings this extension contributes ---------------------------------

interface ConfigurationContribution {
  title?: string;
  properties?: Record<
    string,
    { type?: string; default?: unknown; scope?: string; description?: string }
  >;
}

function configuration(): ConfigurationContribution {
  const declared = readManifest().contributes?.configuration;
  assert.ok(
    declared !== undefined && typeof declared === "object" && !Array.isArray(declared),
    "package.json declares no contributes.configuration",
  );
  return declared as ConfigurationContribution;
}

test("package.json contributes the executable override, one configuration path per experiment kind, the metrics revisions, the requirements mapping and the test tables' annotations folder", () => {
  // The extension resolves the installation and needs no configuration to do it; the other
  // settings are the configuration file each experiment kind runs with (derived here from
  // `EXPERIMENT_KINDS`), the mapping file the requirements import remembers and the
  // annotations folder the test tables remember. Every setting beyond those is a way for a
  // machine to end up in a state no test covers, so the set is asserted.
  assert.deepEqual(
    Object.keys(configuration().properties ?? {}).sort(),
    [
      EXECUTABLE_SETTING,
      METRICS_SINCE_SETTING,
      REQUIREMENTS_MAPPING_SETTING,
      TEST_TABLES_ANNOTATIONS_SETTING,
      ...Object.values(EXPERIMENT_KINDS).map((entry) => entry.setting),
    ].sort(),
  );
});

test("the metrics revisions are a resource-scoped list of strings, empty by default", () => {
  // Empty means HEAD (the panel passes --since-head); each entry is one --since.
  const property = configuration().properties?.[METRICS_SINCE_SETTING] as
    | {
        type?: string;
        items?: { type?: string };
        scope?: string;
        default?: unknown;
        markdownDescription?: string;
      }
    | undefined;
  assert.equal(property?.type, "array");
  assert.deepEqual(property?.items, { type: "string" });
  assert.equal(property?.scope, "resource");
  assert.deepEqual(property?.default, []);
  assert.match(property?.markdownDescription ?? "", /empty/i);
  assert.match(property?.markdownDescription ?? "", /HEAD/);
});

test("an experiment's setting is a resource-scoped string, empty by default", () => {
  // `resource` scope lets a workspace's `.vscode/settings.json` name the file:
  // it is an input the CLI reads as data, never a program, and the extension
  // runs only in a trusted workspace. Empty means the kind's defaults.
  for (const entry of Object.values(EXPERIMENT_KINDS)) {
    const property = configuration().properties?.[entry.setting] as
      | { type?: string; scope?: string; default?: unknown; markdownDescription?: string }
      | undefined;
    assert.equal(property?.type, "string", entry.setting);
    assert.equal(property?.scope, "resource", entry.setting);
    assert.equal(property?.default, "", entry.setting);
    assert.match(property?.markdownDescription ?? "", /relative to the project directory/);
    assert.match(property?.markdownDescription ?? "", /empty/i);
  }
});

test("the requirements mapping is a resource-scoped string, empty by default", () => {
  // `resource` scope, the experiment configs' reason: the mapping is a file the
  // CLI reads as data, a workspace may commit it, and the import writes it into
  // the workspace folder's settings. Empty means SWCraft's own names.
  const property = configuration().properties?.[REQUIREMENTS_MAPPING_SETTING] as
    | { type?: string; scope?: string; default?: unknown; markdownDescription?: string }
    | undefined;
  assert.equal(property?.type, "string");
  assert.equal(property?.scope, "resource");
  assert.equal(property?.default, "");
  assert.match(property?.markdownDescription ?? "", /relative to the project directory/);
  assert.match(property?.markdownDescription ?? "", /--mapping/);
  assert.match(property?.markdownDescription ?? "", /empty/i);
});

test("the test tables' annotations folder is a resource-scoped string with no default", () => {
  // The requirements mapping's kind of setting (a path the CLI reads as data,
  // written into the workspace folder's settings by the press), with one
  // difference: no default, so a folder never answered (no value: the press
  // asks) is told from *none yet* (`""`: the press passes no --annotations).
  const property = configuration().properties?.[TEST_TABLES_ANNOTATIONS_SETTING] as
    | { type?: string; scope?: string; default?: unknown; markdownDescription?: string }
    | undefined;
  assert.equal(property?.type, "string");
  assert.equal(property?.scope, "resource");
  assert.equal(property !== undefined && "default" in property, false, "no default");
  assert.match(property?.markdownDescription ?? "", /relative to the project directory/);
  assert.match(property?.markdownDescription ?? "", /--annotations/);
  assert.match(property?.markdownDescription ?? "", /workspace folder's own value/);
});

test("the setting is a machine-scoped string, so a workspace cannot point it at a binary", () => {
  // `machine` scope is what stops a `.vscode/settings.json` in a cloned
  // repository from naming the executable this extension spawns. The
  // Workspace Trust declaration (`untrustedWorkspaces.supported: false`) is the
  // other half of that, and neither replaces the other: trust gates the whole
  // extension, scope gates this one value.
  const property = configuration().properties?.[EXECUTABLE_SETTING];
  assert.equal(property?.type, "string");
  assert.equal(property?.scope, "machine");
  assert.equal(property?.default, "");
});

test("the setting's description says what it overrides and who it is for", () => {
  const description = configuration().properties?.[EXECUTABLE_SETTING]?.description ?? "";
  assert.match(description, /installation/);
  assert.match(description, /checkout|development/i);
});

// --- compareReleases ---------------------------------------------------------

test("compareReleases: 0.9.0 < 0.10.0 — the case a string comparison gets wrong", () => {
  // `"0.10.0" > "0.9.0"` is `false` as strings, so a lexicographic compare would refuse the
  // newer archive as older.
  assert.equal(compareReleases("0.10.0", "0.9.0"), 1);
  assert.equal(compareReleases("0.9.0", "0.10.0"), -1);
});

test("compareReleases: releases of unequal length compare by segment", () => {
  assert.equal(compareReleases("0.7", "0.7.0"), 0);
  assert.equal(compareReleases("1.0.0", "0.9.9"), 1);
  assert.equal(compareReleases("0.7.1", "0.7.0.1"), 1);
});

test("compareReleases: the release segment alone — a pre-release does not order", () => {
  // What this does **not** reach, stated as a test rather than as a comment:
  // PEP 440 orders `0.7.0rc1` *before* `0.7.0`, and nothing here implements
  // that. Both spellings answer with the same release segment, so an rc of the
  // installed version reads as the same version and installs as a repair.
  assert.equal(compareReleases("0.7.0rc1", "0.7.0"), 0);
  assert.equal(compareReleases("v0.7.0+local.1", "0.7.0"), 0);
});

test("compareReleases: a string that is not a version at all answers undefined", () => {
  assert.equal(compareReleases("latest", "0.7.0"), undefined);
  assert.equal(compareReleases("0.7.0", ""), undefined);
});

// Unit tests for the `SWCraft: Check Editor Tooling` surface.
//
// Two things here are load-bearing beyond the usual branch coverage:
//
//  1. **The version comparison is tested, not eyeballed.** `^1.110.0` against a 1.134.0 host
//     is the case that ships, and a caret range is the shape a hand-written `>=` comparison
//     gets wrong in both directions (a caret clears `minorMustEqual`, so `^1.110.0` accepts
//     1.134.0; it does not clear `majorMustEqual`, so it rejects 2.0.0; and a **bare**
//     `1.63.0` is an exact requirement, not a floor). Each is asserted against VS Code's own
//     semantics, read from the shipped bundle — see `healthCheck.ts`'s `RANGE_PATTERN`.
//  2. **The pack membership comes from `package.json` and nowhere else.** From two
//     directions: `checkPackHealth()` reports whatever pack it is handed, and no production
//     module contains a member id as a literal.

import assert from "node:assert/strict";
import * as path from "node:path";
import { test } from "node:test";

import {
  AGENT_PROFILE_RELATIVE_PATH,
  buildHealthReport,
  checkAgentProfile,
  checkBundledSchemas,
  checkModelGuard,
  checkPackHealth,
  checkToolchain,
  copilotNotUsed,
  countProblems,
  declaredSchemaFiles,
  type HealthInput,
  HOOK_RELATIVE_PATH,
  type InstalledExtension,
  type ModelGuardInput,
  NOT_VALIDATION_EVIDENCE,
  type PackMemberHealth,
  probeOncePerPath,
  satisfiesEngineRange,
  summaryLine,
  type ToolchainInput,
  UNDETECTABLE,
} from "./healthCheck";
import { functionBody, productionModules, readManifest, readSource } from "./testSupport";
import { resolveSwcraft } from "./toolchain";
import { UPDATE_TOOLCHAIN_TITLE } from "./toolchainInstall";

const HOST = "1.134.0";

/** A three-member pack with nothing SWCraft ships in it — see the membership tests. */
const PACK = ["alpha.one", "beta.two", "gamma.three"];

function installed(...entries: InstalledExtension[]): InstalledExtension[] {
  return entries;
}

function statusesById(members: PackMemberHealth[]): Record<string, string> {
  return Object.fromEntries(members.map((member) => [member.id, member.status]));
}

/**
 * A rendered finding, as opposed to the summary line that *mentions* the marker. Anchored on
 * the report's two-space indent so counting findings never counts the sentence pointing at
 * them.
 */
function isProblemLine(line: string): boolean {
  return line.startsWith("  [problem]");
}

/** The installed executable on a POSIX host, as `toolchain.ts` composes it. */
const INSTALLED = "/home/u/.swcraft/current/bin/swcraft";
const EXTENSION_VERSION = "0.7.0";
/** `EXTENSION_VERSION` as a pattern, its dots escaped (`0.7.0` unescaped also matches `0x7y0`). */
const EXTENSION_VERSION_PATTERN = new RegExp(EXTENSION_VERSION.replace(/\./g, "\\."));

/** A `checkToolchain` input for a healthy installation, with per-case overrides. */
function toolchainInput(overrides: Partial<ToolchainInput> = {}): ToolchainInput {
  return {
    resolution: resolveSwcraft(undefined, {}, "/home/u", "posix", () => true),
    extensionVersion: EXTENSION_VERSION,
    version: { ok: true, output: `swcraft ${EXTENSION_VERSION}\n` },
    pathResolution: INSTALLED,
    platform: "posix",
    ...overrides,
  };
}

const OK_TOOLCHAIN = checkToolchain(toolchainInput());
const DECLARED_SCHEMAS = ["swc.schema.json", "tasks.schema.json"];
//: The healthy fixture is derived from the declaration rather than re-spelled beside it, so
//: editing `DECLARED_SCHEMAS` alone cannot turn it into a silent "file missing" baseline.
const OK_SCHEMAS = checkBundledSchemas(DECLARED_SCHEMAS, "/ext/schemas", DECLARED_SCHEMAS);

function input(overrides: Partial<HealthInput> = {}): HealthInput {
  return {
    vscodeVersion: HOST,
    declaredPack: PACK,
    installed: installed(
      { id: "alpha.one", version: "1.0.0", engineRange: "^1.110.0" },
      { id: "beta.two", version: "2.0.0", engineRange: "^1.63.0" },
      { id: "gamma.three", version: "3.0.0", engineRange: ">=1.67.0" },
    ),
    toolchain: OK_TOOLCHAIN,
    schemas: OK_SCHEMAS,
    ...overrides,
  };
}

// --- Version comparison ------------------------------------------------------

test("satisfiesEngineRange: the shipped case — ^1.110.0 against a 1.134.0 host", () => {
  // This extension's own declared floor against this repo's measured host. A comparison that
  // treated the caret as "same minor" would report that floor as unsatisfied on the very host
  // it was raised for.
  assert.equal(satisfiesEngineRange("1.134.0", "^1.110.0"), "satisfied");
});

test("satisfiesEngineRange: a host below the floor is below-floor, by minor and by patch", () => {
  assert.equal(satisfiesEngineRange("1.100.0", "^1.110.0"), "below-floor");
  assert.equal(satisfiesEngineRange("1.109.9", "^1.110.0"), "below-floor");
  assert.equal(satisfiesEngineRange("1.110.0", "^1.110.1"), "below-floor");
  assert.equal(satisfiesEngineRange("0.9.0", "^1.110.0"), "below-floor");
});

test("satisfiesEngineRange: the floor itself is satisfied, and a later patch too", () => {
  assert.equal(satisfiesEngineRange("1.110.0", "^1.110.0"), "satisfied");
  assert.equal(satisfiesEngineRange("1.110.4", "^1.110.0"), "satisfied");
});

test("satisfiesEngineRange: a caret still bounds the major — 2.0.0 does not satisfy ^1.110.0", () => {
  // The half a naive `>=` comparison drops. VS Code's normalization clears
  // `minorMustEqual`/`patchMustEqual` under a caret and leaves
  // `majorMustEqual` set.
  assert.equal(satisfiesEngineRange("2.0.0", "^1.110.0"), "below-floor");
});

test("satisfiesEngineRange: >= is a plain minimum with no major bound", () => {
  assert.equal(satisfiesEngineRange("1.134.0", ">=1.67.0"), "satisfied");
  assert.equal(satisfiesEngineRange("1.66.9", ">=1.67.0"), "below-floor");
  assert.equal(satisfiesEngineRange("2.0.0", ">=1.67.0"), "satisfied");
  // The boundary itself, on the `>=` branch: the host that is exactly the declared floor is the
  // common one, and `>` for `>=` would report a member declaring `>=1.67.0` on a 1.67.0 host as
  // below its own floor.
  assert.equal(satisfiesEngineRange("1.67.0", ">=1.67.0"), "satisfied");
  assert.equal(satisfiesEngineRange("1.67.1", ">=1.67.0"), "satisfied");
  assert.equal(satisfiesEngineRange("1.67.0", ">=1.67.1"), "below-floor");
});

test("satisfiesEngineRange: a bare version is an exact requirement, not a floor", () => {
  // VS Code's own semantics: with no `^` and no `>=`, all three `mustEqual` flags stay set, so
  // 1.134.0 does *not* satisfy `1.63.0`; the report says what VS Code would do.
  assert.equal(satisfiesEngineRange("1.63.0", "1.63.0"), "satisfied");
  assert.equal(satisfiesEngineRange("1.134.0", "1.63.0"), "below-floor");
});

test("satisfiesEngineRange: `*` and `x` wildcards accept anything at their level", () => {
  assert.equal(satisfiesEngineRange("1.134.0", "*"), "satisfied");
  assert.equal(satisfiesEngineRange("1.134.0", "1.x.x"), "satisfied");
  assert.equal(satisfiesEngineRange("2.0.0", "1.x.x"), "below-floor");
  assert.equal(satisfiesEngineRange("2.0.0", "x.x.x"), "satisfied");
});

test("satisfiesEngineRange: an insider host version compares on its release triple", () => {
  assert.equal(satisfiesEngineRange("1.134.0-insider", "^1.110.0"), "satisfied");
});

test("satisfiesEngineRange: what it refuses to answer is not-comparable, never below-floor", () => {
  // An undeclared, unparseable, or date-qualified range gets a third answer.
  // Reporting `below-floor` for a string the comparison did not understand
  // would be a fabricated verdict, and the one a user cannot act on.
  assert.equal(satisfiesEngineRange(HOST, undefined), "not-comparable");
  assert.equal(satisfiesEngineRange(HOST, ">=1.67.0 <2.0.0"), "not-comparable");
  assert.equal(satisfiesEngineRange(HOST, "^1.110.0-20240101"), "not-comparable");
  assert.equal(satisfiesEngineRange(HOST, "latest"), "not-comparable");
  assert.equal(satisfiesEngineRange("not-a-version", "^1.110.0"), "not-comparable");
});

// --- Pack members ------------------------------------------------------------

test("checkPackHealth: every member installed and above its floor is present", () => {
  const members = checkPackHealth(input().installed, PACK, HOST);
  assert.deepEqual(statusesById(members), {
    "alpha.one": "present",
    "beta.two": "present",
    "gamma.three": "present",
  });
  assert.equal(countProblems(input()), 0);
});

test("checkPackHealth: one member missing is reported absent, with its id in the message", () => {
  const members = checkPackHealth(
    installed(
      { id: "alpha.one", version: "1.0.0", engineRange: "^1.110.0" },
      { id: "gamma.three", version: "3.0.0", engineRange: ">=1.67.0" },
    ),
    PACK,
    HOST,
  );
  assert.deepEqual(statusesById(members), {
    "alpha.one": "present",
    "beta.two": "absent",
    "gamma.three": "present",
  });
  const absent = members.find((member) => member.id === "beta.two");
  assert.ok(absent);
  // The id is what an administrator mirrors or a user searches for; a message
  // saying "a pack member is missing" would not be actionable.
  assert.match(absent.message, /beta\.two/);
});

test("checkPackHealth: an absent member's message names BOTH possibilities, because the API cannot separate them", () => {
  // The honest three-state answer. `vscode.extensions.all` lists enabled
  // extensions only (`@types/vscode`: `onDidChange` fires when extensions are
  // "installed, uninstalled, enabled or disabled"), and the namespace has no
  // member that enumerates disabled ones — so "not installed" and "installed
  // but disabled" arrive here as the same observation, and the report must not
  // claim to have distinguished them.
  const [member] = checkPackHealth([], ["alpha.one"], HOST);
  assert.equal(member.status, "absent");
  assert.match(member.message, /not installed/);
  assert.match(member.message, /disabled/);
});

test("checkPackHealth: the status vocabulary is exactly three values — there is no 'disabled' verdict", () => {
  const cases: InstalledExtension[][] = [
    [],
    installed({ id: "alpha.one", version: "1.0.0", engineRange: "^1.110.0" }),
    installed({ id: "alpha.one", version: "1.0.0", engineRange: "^1.200.0" }),
    installed({ id: "alpha.one", version: "1.0.0" }),
  ];
  const seen = new Set<string>();
  for (const extensions of cases) {
    for (const member of checkPackHealth(extensions, ["alpha.one"], HOST)) {
      seen.add(member.status);
    }
  }
  assert.deepEqual([...seen].sort(), ["absent", "present", "version-incompatible"]);
});

test("checkPackHealth: a member present but below the floor is version-incompatible, not absent", () => {
  const members = checkPackHealth(
    installed({ id: "alpha.one", version: "9.9.9", engineRange: "^1.200.0" }),
    ["alpha.one"],
    HOST,
  );
  assert.equal(members[0].status, "version-incompatible");
  assert.match(members[0].message, /alpha\.one/);
  assert.match(members[0].message, /\^1\.200\.0/);
  assert.match(members[0].message, /1\.134\.0/);
});

test("checkPackHealth: a member whose engines.vscode cannot be compared stays present, and says so", () => {
  const members = checkPackHealth(
    installed({ id: "alpha.one", version: "1.0.0", engineRange: "whatever" }),
    ["alpha.one"],
    HOST,
  );
  assert.equal(members[0].status, "present");
  assert.match(members[0].message, /could not be compared/);
});

test("checkPackHealth: an empty declared pack yields an empty report and no problems", () => {
  assert.deepEqual(checkPackHealth(input().installed, [], HOST), []);
  assert.equal(countProblems(input({ declaredPack: [] })), 0);
});

test("checkPackHealth: extension ids are matched case-insensitively", () => {
  // VS Code folds case on extension identifiers; a manifest that spelled a
  // member `Alpha.One` must not be reported missing against an installed
  // `alpha.one`.
  const members = checkPackHealth(
    installed({ id: "alpha.one", version: "1.0.0", engineRange: "^1.110.0" }),
    ["Alpha.One"],
    HOST,
  );
  assert.equal(members[0].status, "present");
  // …and the id is echoed as the *manifest* spells it, since that is the
  // string the reader will go looking for.
  assert.equal(members[0].id, "Alpha.One");
});

test("checkPackHealth: members are reported in the declared order", () => {
  const members = checkPackHealth([], [...PACK].reverse(), HOST);
  assert.deepEqual(
    members.map((member) => member.id),
    [...PACK].reverse(),
  );
});

// --- The runtime/schema half -------------------------------------------------

test("checkToolchain: a matching installation that runs is ok and names the executable", () => {
  const health = checkToolchain(toolchainInput());
  assert.equal(health.status, "ok");
  assert.match(health.message, /\/home\/u\/\.swcraft\/current\/bin\/swcraft/);
  assert.match(health.message, EXTENSION_VERSION_PATTERN);
});

test("checkToolchain: case 1 — no installation names SWCraft: Install Toolchain", () => {
  const health = checkToolchain(
    toolchainInput({
      resolution: resolveSwcraft(undefined, {}, "/home/u", "posix", () => false),
      version: undefined,
      pathResolution: undefined,
    }),
  );
  assert.equal(health.status, "missing");
  assert.match(health.message, /\/home\/u\/\.swcraft\/current\/bin\/swcraft/);
  assert.match(health.message, /every SWCraft command/);
  assert.match(health.message, /SWCraft: Install Toolchain/);
});

test("checkToolchain: case 1 through the setting names the setting, not the installer", () => {
  // A mistyped `swcraft.executable` must not be reported as "install the
  // toolchain": the toolchain may be installed and perfectly healthy, and the
  // repair is the setting the user wrote.
  const health = checkToolchain(
    toolchainInput({
      resolution: resolveSwcraft("/typo/swcraft", {}, "/home/u", "posix", () => false),
      version: undefined,
      pathResolution: undefined,
    }),
  );
  assert.equal(health.status, "missing");
  assert.match(health.message, /\/typo\/swcraft/);
  assert.match(health.message, /swcraft\.executable/);
});

test("checkToolchain: case 2 — present but `swcraft --version` will not start names Update Toolchain", () => {
  // The failure a relocated or removed base Python leaves: the venv's
  // `pyvenv.cfg` names an absolute interpreter, and the pilot's machines are
  // allowed to upgrade or remove Python 3.11. The file is right there, so
  // "missing" would send the reader looking for a path that exists.
  const health = checkToolchain(toolchainInput({ version: { ok: false, output: "" } }));
  assert.equal(health.status, "missing");
  assert.match(health.message, /\/home\/u\/\.swcraft\/current\/bin\/swcraft/);
  assert.match(health.message, /did not run|could not be started/);
  assert.match(health.message, /pyvenv\.cfg|base interpreter/);
  assert.ok(health.message.includes(`\`${UPDATE_TOOLCHAIN_TITLE}\``), health.message);
  assert.doesNotMatch(health.message, /SWCraft: Install Toolchain/);
});

test("checkToolchain: case 2 — an executable that is there and was never probed is case 2, not a throw", () => {
  // The other half of `input.version === undefined || !input.version.ok`, which no other
  // fixture reaches (each pairs `version: undefined` with a resolution that does not exist).
  // Without it the next line dereferences `input.version.output`, and Check Editor Tooling
  // throws instead of reporting a broken installation.
  const health = checkToolchain(toolchainInput({ version: undefined }));
  assert.equal(health.status, "missing");
  assert.match(health.message, /did not run|could not be started/);
  assert.ok(health.message.includes(`\`${UPDATE_TOOLCHAIN_TITLE}\``), health.message);
});

test("checkToolchain: case 3 — a version mismatch names both versions", () => {
  const health = checkToolchain(
    toolchainInput({ version: { ok: true, output: "swcraft 0.6.0\n" } }),
  );
  assert.equal(health.status, "missing");
  assert.match(health.message, /0\.6\.0/);
  assert.match(health.message, EXTENSION_VERSION_PATTERN);
  assert.ok(health.message.includes(`\`${UPDATE_TOOLCHAIN_TITLE}\``), health.message);
});

test("checkToolchain: case 3 — output with no readable version is reported, never compared", () => {
  const health = checkToolchain(
    toolchainInput({ version: { ok: true, output: "usage: swcraft [-h]\n" } }),
  );
  assert.equal(health.status, "missing");
  assert.match(health.message, /usage: swcraft/);
});

test("checkToolchain: case 4 — a shadowed `swcraft` on PATH is a warning, not a failure", () => {
  // The activated project `.venv` in the integrated terminal. The extension
  // spawns the installation by absolute path and is unaffected, so this is not
  // a problem to count — but a terminal and a `moon` build in this window run
  // the other one, which is worth a sentence.
  const health = checkToolchain(toolchainInput({ pathResolution: "/work/proj/.venv/bin/swcraft" }));
  assert.equal(health.status, "warning");
  assert.match(health.message, /\/work\/proj\/\.venv\/bin\/swcraft/);
  assert.match(health.message, /\/home\/u\/\.swcraft\/current\/bin\/swcraft/);
});

test("checkToolchain: case 4 — nothing on PATH at all is the same warning, not silence", () => {
  const health = checkToolchain(toolchainInput({ pathResolution: undefined }));
  assert.equal(health.status, "warning");
  assert.match(health.message, /PATH/);
});

// On macOS and Linux the installer edits no shell file: it prints one `export PATH=…` line for
// the reader to add, so this finding fires on every healthy POSIX installation until that line
// is in a profile, and "open a new one" cannot fix it there.
test("checkToolchain: case 4 — the POSIX advice names the line the installer printed", () => {
  const health = checkToolchain(toolchainInput({ pathResolution: undefined, platform: "posix" }));
  assert.equal(health.status, "warning");
  assert.match(health.message, /shell profile/);
  assert.match(health.message, /export PATH/);
  // A new window does not help: the extension host's `process.env` is fixed when the
  // **application** launches, and Reload Window inherits the same block. Only starting VS Code
  // again from a shell that has the entry works.
  assert.doesNotMatch(health.message, /new window/);
  assert.match(health.message, /quit VS Code/);
});

test("checkToolchain: case 4 — the Windows advice is still the new terminal", () => {
  // There the installer really does edit the user PATH, through `HKCU\Environment`,
  // so a terminal that predates the install is the whole of the cause.
  const health = checkToolchain(toolchainInput({ pathResolution: undefined, platform: "windows" }));
  assert.equal(health.status, "warning");
  assert.match(health.message, /open a new one/);
  assert.doesNotMatch(health.message, /shell profile/);
  // ...and the same application-launch fact holds there: an integrated terminal
  // inherits the environment VS Code was started with, so a window that predates
  // the installer's registry edit needs VS Code restarted, not a new terminal tab.
  assert.match(health.message, /quit VS Code/);
});

// `resolveOnPath` is handed the extension host's `process.env`, fixed when the window opened,
// so a venv activated in the integrated terminal is exactly what it cannot observe; the
// message must not name that case as the usual cause.
test("checkToolchain: case 4 — the shadow sentence does not name what this cannot observe", () => {
  const health = checkToolchain(toolchainInput({ pathResolution: "/work/proj/.venv/bin/swcraft" }));
  assert.doesNotMatch(health.message, /activated project environment/);
  assert.match(health.message, /VS Code was started with/);
});

// The counting case: the non-ok verdicts asserted as a set of distinct sentences, so a branch
// nobody counted cannot go unseen (`resolves nowhere` is what every healthy macOS or Linux
// installation produces until the printed `export PATH=` line is in a profile), and a seventh
// branch reusing an existing sentence fails here.
//
// What it does not reach: the order of the six (the "earlier findings win" case below), a
// branch whose sentence is distinct but wrong, and a seventh branch with a sentence of its own.
test("checkToolchain: six non-ok verdicts, six distinct sentences", () => {
  const nonOk = [
    // 1 — not there
    toolchainInput({
      resolution: resolveSwcraft(undefined, {}, "/home/u", "posix", () => false),
      version: undefined,
    }),
    // 2 — there and will not start
    toolchainInput({ version: { ok: false, output: "" } }),
    // 3 — ran, exited 0, printed nothing this can read
    toolchainInput({ version: { ok: true, output: "usage: swcraft [-h]\n" } }),
    // 4 — ran and is a different version
    toolchainInput({ version: { ok: true, output: "swcraft 0.6.0\n" } }),
    // 5 — a bare `swcraft` resolves nowhere
    toolchainInput({ pathResolution: undefined }),
    // 6 — a bare `swcraft` resolves somewhere else
    toolchainInput({ pathResolution: "/work/proj/.venv/bin/swcraft" }),
  ].map((probe) => checkToolchain(probe));

  assert.equal(
    new Set(nonOk.map((health) => health.message)).size,
    6,
    "each of the six findings must be its own sentence — a repeated one is a case the reader " +
      "cannot tell apart, and a case the documents cannot describe",
  );
  assert.ok(
    nonOk.every((health) => health.status !== "ok"),
    "every input above is a finding; an `ok` among them means a branch stopped firing",
  );
  assert.deepEqual(
    nonOk.map((health) => health.status),
    ["missing", "missing", "missing", "missing", "warning", "warning"],
    "four counted problems and two warnings, in the order the function decides them",
  );
  // The two easiest to miss, named rather than merely counted.
  assert.match(nonOk[2].message, /printed no version this could read/);
  assert.match(nonOk[4].message, /resolves nowhere/);
  assert.match(nonOk[5].message, /resolves to \/work\/proj/);
});

test("checkToolchain: the earlier findings win — a broken install is not reported as a PATH warning", () => {
  // One record, one verdict: a shadowed PATH is not what to fix when the
  // executable itself is not there.
  const health = checkToolchain(
    toolchainInput({
      resolution: resolveSwcraft(undefined, {}, "/home/u", "posix", () => false),
      version: undefined,
      pathResolution: "/work/proj/.venv/bin/swcraft",
    }),
  );
  assert.equal(health.status, "missing");
  assert.match(health.message, /SWCraft: Install Toolchain/);
});

test("checkToolchain: a Windows host reports the Windows executable", () => {
  const health = checkToolchain(
    toolchainInput({
      resolution: resolveSwcraft(
        undefined,
        { LOCALAPPDATA: "C:\\Users\\u\\AppData\\Local" },
        "C:\\Users\\u",
        "windows",
        () => false,
      ),
      version: undefined,
      pathResolution: undefined,
      platform: "windows",
    }),
  );
  assert.equal(health.status, "missing");
  assert.match(health.message, /swcraft\.exe/);
});

test("checkToolchain: the PATH comparison is case-insensitive on Windows", () => {
  // Two spellings of one directory must not raise a warning nobody can act on.
  const resolution = resolveSwcraft(
    undefined,
    { LOCALAPPDATA: "C:\\Users\\u\\AppData\\Local" },
    "C:\\Users\\u",
    "windows",
    () => true,
  );
  const health = checkToolchain(
    toolchainInput({
      resolution,
      platform: "windows",
      pathResolution: resolution.executable.toLowerCase().replace(/\\/g, "/"),
    }),
  );
  assert.equal(health.status, "ok");
});

test("checkBundledSchemas: every declared schema present is ok, and says how many were declared", () => {
  const health = checkBundledSchemas(["a.schema.json", "b.schema.json"], "/ext/schemas", [
    "a.schema.json",
    "b.schema.json",
  ]);
  assert.equal(health.status, "ok");
  assert.match(health.message, /all 2 schema file/);
});

test("checkBundledSchemas: ONE declared schema absent from a non-empty directory is a problem", () => {
  // The finding this check exists for, and one a file count cannot see: the directory has
  // files in it, while the layer whose url resolves to nothing has silently dead completion,
  // hover and diagnostics. The absent name must be in the message: it says which layer stopped
  // working.
  const health = checkBundledSchemas(["a.schema.json", "b.schema.json"], "/ext/schemas", [
    "a.schema.json",
    "b.schema.json",
    "tasks.schema.json",
  ]);
  assert.equal(health.status, "missing");
  assert.match(health.message, /tasks\.schema\.json/);
  assert.match(health.message, /1 of 3/);
});

test("checkBundledSchemas: the real manifest's declared inventory against a directory missing one of it", () => {
  // Anchored on the shipped `contributes.yamlValidation` rather than invented names, so the
  // check runs against the inventory it is handed at runtime, and a new layer needs no edit here.
  const declared = declaredSchemaFiles(readManifest().contributes?.yamlValidation);
  assert.ok(declared.length > 1, "the shipped manifest must declare more than one schema url");
  assert.equal(checkBundledSchemas(declared, "/ext/schemas", declared).status, "ok");
  const partial = declared.slice(1);
  const health = checkBundledSchemas(partial, "/ext/schemas", declared);
  assert.equal(
    health.status,
    "missing",
    "a directory missing one declared schema must not be reported healthy",
  );
  assert.match(health.message, new RegExp(declared[0].replace(/\./g, "\\.")));
});

test("checkBundledSchemas: extra undeclared files in the directory are not a problem", () => {
  // The check is one-directional on purpose: an orphan file binds to nothing
  // and costs the user nothing. `yamlValidation.test.ts` is where the one-to-one
  // rule is enforced, at packaging time, where an orphan is actionable.
  const health = checkBundledSchemas(
    ["a.schema.json", "b.schema.json", "stray.schema.json"],
    "/ext/schemas",
    ["a.schema.json", "b.schema.json"],
  );
  assert.equal(health.status, "ok");
});

test("checkBundledSchemas: an empty schemas directory is a problem", () => {
  const health = checkBundledSchemas([], "/ext/schemas", DECLARED_SCHEMAS);
  assert.equal(health.status, "missing");
  assert.match(health.message, /is empty/);
  assert.match(health.message, /\/ext\/schemas/);
});

test("checkBundledSchemas: an unreadable schemas directory is a different message from an empty one", () => {
  const missing = checkBundledSchemas(undefined, "/ext/schemas", DECLARED_SCHEMAS);
  assert.equal(missing.status, "missing");
  assert.match(missing.message, /could not be read/);
  assert.notEqual(
    missing.message,
    checkBundledSchemas([], "/ext/schemas", DECLARED_SCHEMAS).message,
  );
});

test("checkBundledSchemas: files present but nothing declared is a problem, not silence", () => {
  // A manifest that lost its `contributes.yamlValidation` block fails the user
  // exactly as a missing file does — nothing binds — and it is the shape a
  // count-only check read as healthiest of all.
  const health = checkBundledSchemas(["a.schema.json"], "/ext/schemas", []);
  assert.equal(health.status, "missing");
  assert.match(health.message, /declares no \.\/schemas\/ url/);
});

// --- What `contributes.yamlValidation` requires -------------------------------

test("declaredSchemaFiles: the shipped manifest's urls, as basenames", () => {
  const declared = declaredSchemaFiles(readManifest().contributes?.yamlValidation);
  assert.deepEqual(declared, [...new Set(declared)].sort(), "deduplicated and sorted");
  assert.ok(declared.includes("swc.schema.json"));
  for (const name of declared) {
    assert.match(name, /\.schema\.json$/);
  }
});

test("declaredSchemaFiles: a missing or malformed block yields no requirement rather than throwing", () => {
  // It runs inside a health check: reading someone else's JSON must not be the
  // thing that breaks the surface whose job is reporting breakage.
  assert.deepEqual(declaredSchemaFiles(undefined), []);
  assert.deepEqual(declaredSchemaFiles({}), []);
  assert.deepEqual(declaredSchemaFiles([null, 7, "x", {}]), []);
});

test("declaredSchemaFiles: only extension-relative ./schemas/ urls are a required file", () => {
  assert.deepEqual(
    declaredSchemaFiles([
      { url: "./schemas/b.schema.json" },
      { url: "./schemas/a.schema.json" },
      { url: "./schemas/a.schema.json" },
      { url: "https://example.invalid/remote.schema.json" },
      { url: "./schemas/nested/deep.schema.json" },
      { url: "./schemas/" },
    ]),
    ["a.schema.json", "b.schema.json"],
  );
});

// --- The report --------------------------------------------------------------

test("buildHealthReport: a healthy host reports no problems", () => {
  const lines = buildHealthReport(input());
  assert.ok(lines.includes(summaryLine(0)));
  assert.ok(lines.includes("No problems found."));
  assert.equal(lines.some(isProblemLine), false);
});

test("buildHealthReport: the host version is printed, and comes from the input", () => {
  assert.ok(buildHealthReport(input({ vscodeVersion: "1.140.1" })).includes("VS Code 1.140.1"));
});

test("buildHealthReport: every problem is marked and counted", () => {
  const lines = buildHealthReport(
    input({
      installed: installed({ id: "alpha.one", version: "1.0.0", engineRange: "^1.200.0" }),
      toolchain: checkToolchain(
        toolchainInput({
          resolution: resolveSwcraft(undefined, {}, "/home/u", "posix", () => false),
          version: undefined,
        }),
      ),
      schemas: checkBundledSchemas([], "/ext/schemas", DECLARED_SCHEMAS),
    }),
  );
  // alpha.one below its floor, beta.two and gamma.three absent, the toolchain
  // missing, the schemas empty.
  assert.equal(lines.filter(isProblemLine).length, 5);
  assert.ok(lines.includes(summaryLine(5)));
});

test("buildHealthReport: an empty declared pack says so rather than printing an empty list", () => {
  const lines = buildHealthReport(input({ declaredPack: [] }));
  assert.ok(lines.some((line) => line.includes("Extension pack — 0 member(s)")));
  assert.ok(lines.some((line) => line.includes("this extension declares no extensionPack")));
});

test("buildHealthReport: the report states verbatim that a healthy pack is not validation evidence", () => {
  // `swcraft validate` is the gate, and no editor diagnostic is evidence. Printed on every
  // report, healthy or not: a green one is the most likely to be misread.
  for (const report of [input(), input({ declaredPack: [], installed: [] })]) {
    const lines = buildHealthReport(report);
    assert.ok(
      lines.includes(NOT_VALIDATION_EVIDENCE),
      "the report must carry NOT_VALIDATION_EVIDENCE verbatim",
    );
  }
  assert.match(NOT_VALIDATION_EVIDENCE, /A healthy pack is not validation evidence/);
  // The CLI's spelling: the rule is about the command, and a report naming a panel button that
  // does not exist would send the reader looking for it.
  assert.match(NOT_VALIDATION_EVIDENCE, /`swcraft validate` is the gate\./);
  assert.doesNotMatch(NOT_VALIDATION_EVIDENCE, /SWCraft: Validate/);
});

test("buildHealthReport: the report names what it cannot see", () => {
  const lines = buildHealthReport(input());
  assert.ok(lines.includes("What this report cannot see:"));
  for (const limit of UNDETECTABLE) {
    assert.ok(lines.includes(`  - ${limit}`), `the report must carry the limit: ${limit}`);
  }
  const text = lines.join("\n");
  // The three limits the report states: the disabled/absent conflation, Red Hat XML's two
  // network paths, and that a present member is not an exercised one.
  assert.match(text, /only enabled extensions/);
  assert.match(text, /xml\.downloadExternalResources\.enabled/);
  assert.match(text, /LemMinX binary/);
  assert.match(text, /does not activate it/);
  // `version-incompatible` is near-unreachable: VS Code disables a member whose floor this host
  // fails, and a disabled member is reported absent. Stated in the report rather than fixed by
  // deleting a correct verdict.
  assert.match(text, /reported above as absent rather than as version-incompatible/);
});

test("the report has no paragraph on a member's Node.js: the LikeC4 member left the pack", () => {
  // No limit on the retired LikeC4 member's external Node.js: nothing this pack installs needs
  // it, and a report naming a runtime nothing needs sends the reader looking for one.
  // `swcraft generate diagrams` finds its own Node.js; a missing one is that command's refusal.
  const text = buildHealthReport(input()).join("\n");
  for (const retired of [/likec4/i, /Node\.js/, /\bnode\b/, /\.c4\b/, /preview fails/]) {
    assert.doesNotMatch(text, retired);
  }
  for (const limit of UNDETECTABLE) {
    assert.doesNotMatch(limit, /external runtime/, "the member-runtime limit is back");
  }
});

test("the report still names every member the shipped pack declares — both of them", () => {
  // The other half: removing a paragraph must not remove a member line. The ids
  // are the manifest's, never a list here (the report's own rule), and the
  // count is the pack's.
  const declared = readManifest().extensionPack ?? [];
  assert.equal(declared.length, 2, "the shipped pack is Red Hat YAML and Red Hat XML");
  const lines = buildHealthReport(
    input({
      declaredPack: declared,
      installed: installed(
        ...declared.map((id) => ({ id, version: "1.0.0", engineRange: "^1.67.0" })),
      ),
    }),
  );
  assert.ok(lines.includes("Extension pack — 2 member(s) declared in package.json:"));
  for (const id of declared) {
    assert.equal(
      lines.filter((line) => line.startsWith("  [ok]") && line.includes(id)).length,
      1,
      `the report must carry one [ok] line for ${id}`,
    );
  }
  assert.ok(lines.includes(summaryLine(0)));
});

// --- The Copilot agent profile ------------------------------------------------

const BUNDLED_PROFILE = new TextEncoder().encode("---\nname: SWCraft Architect\n---\n");

test("checkAgentProfile: no profile is missing, and names Onboard Project", () => {
  const health = checkAgentProfile(undefined, BUNDLED_PROFILE);
  assert.equal(health.status, "missing");
  assert.equal(health.label, "Copilot agent profile");
  assert.match(health.message, /SWCraft: Onboard Project/);
  assert.ok(health.message.includes(AGENT_PROFILE_RELATIVE_PATH));
  // The template's profile grants `view` beside the SWCraft tools, so the
  // message never says the agent's only tools are SWCraft's.
  assert.match(health.message, /read-only `view` for a saved SWCraft answer/);
  assert.doesNotMatch(health.message, /only tools/);
});

test("checkAgentProfile: a profile different from the bundled one is a warning naming Onboard Project", () => {
  const edited = new TextEncoder().encode("---\nname: SWCraft Architect\n---\nmy own rule\n");
  const health = checkAgentProfile(edited, BUNDLED_PROFILE);
  assert.equal(health.status, "warning");
  assert.match(health.message, /differs from this release's; Onboard Project replaces it/);
});

test("checkAgentProfile: a profile equal to the bundled one is ok", () => {
  const health = checkAgentProfile(Uint8Array.from(BUNDLED_PROFILE), BUNDLED_PROFILE);
  assert.equal(health.status, "ok");
});

test("checkAgentProfile: a bundled profile that cannot be read is missing, not ok", () => {
  assert.equal(checkAgentProfile(BUNDLED_PROFILE, undefined).status, "missing");
  assert.match(checkAgentProfile(BUNDLED_PROFILE, undefined).message, /Reinstall the extension/);
});

test("buildHealthReport: the agent profile row is printed; a warning is marked and not counted", () => {
  const edited = new TextEncoder().encode("edited\n");
  const warned = input({ agentProfile: checkAgentProfile(edited, BUNDLED_PROFILE) });
  const lines = buildHealthReport(warned);
  assert.equal(
    lines.filter((line) => line.startsWith("  [warning] Copilot agent profile")).length,
    1,
  );
  assert.equal(countProblems(warned), 0);
  assert.ok(lines.includes(summaryLine(0)));

  const missing = input({ agentProfile: checkAgentProfile(undefined, BUNDLED_PROFILE) });
  assert.equal(countProblems(missing), 1);
  assert.equal(
    buildHealthReport(missing).filter((line) =>
      line.startsWith("  [problem] Copilot agent profile"),
    ).length,
    1,
  );

  const ok = input({ agentProfile: checkAgentProfile(BUNDLED_PROFILE, BUNDLED_PROFILE) });
  assert.equal(countProblems(ok), 0);
  assert.ok(
    buildHealthReport(ok).some((line) => line.startsWith("  [ok]      Copilot agent profile")),
  );
});

test("buildHealthReport: no SWCraft project, no agent profile row", () => {
  assert.equal(
    buildHealthReport(input()).some((line) => line.includes("Copilot agent profile")),
    false,
  );
});

// --- The Copilot model guard ---------------------------------------------------

const BUNDLED_HOOK = new TextEncoder().encode(
  '{ "hooks": { "PreToolUse": [ { "type": "command", "command": "swcraft guard" } ] } }\n',
);
const ON_PATH = "/home/u/.swcraft/current/bin/swcraft";

/** A `checkModelGuard` input for a project with this release's hook and `swcraft`. */
function guardInput(overrides: Partial<ModelGuardInput> = {}): ModelGuardInput {
  return {
    hook: Uint8Array.from(BUNDLED_HOOK),
    bundled: BUNDLED_HOOK,
    pathResolution: ON_PATH,
    pathVersion: { ok: true, output: `swcraft ${EXTENSION_VERSION}\n` },
    extensionVersion: EXTENSION_VERSION,
    ...overrides,
  };
}

test("checkModelGuard: the hook path is the one the template ships", () => {
  assert.equal(HOOK_RELATIVE_PATH, ".github/hooks/swcraft.json");
});

test("checkModelGuard: this release's hook and swcraft is ok", () => {
  const health = checkModelGuard(guardInput());
  assert.equal(health.label, "Copilot model guard");
  assert.equal(health.status, "ok");
  assert.equal(health.message, `Copilot model guard: ${HOOK_RELATIVE_PATH} is this release's.`);
});

test("checkModelGuard: no hook is a warning saying the model is unprotected, naming Onboard Project", () => {
  const health = checkModelGuard(guardInput({ hook: undefined }));
  assert.equal(health.status, "warning");
  assert.ok(health.message.includes(HOOK_RELATIVE_PATH));
  assert.match(health.message, /the model is not protected from Copilot's file tools/);
  assert.match(health.message, /SWCraft: Onboard Project/);
});

test("checkModelGuard: a hook different from the bundled one is a warning naming Onboard Project", () => {
  const edited = new TextEncoder().encode('{ "hooks": {} }\n');
  const health = checkModelGuard(guardInput({ hook: edited }));
  assert.equal(health.status, "warning");
  assert.match(health.message, /differs from this release's; Onboard Project replaces it/);
});

test("checkModelGuard: a bundled hook that cannot be read is missing, not ok", () => {
  const health = checkModelGuard(guardInput({ bundled: undefined }));
  assert.equal(health.status, "missing");
  assert.match(health.message, /Reinstall the extension/);
});

/** The cost a hook that cannot run carries, stated. */
const NO_TOOL =
  "Copilot can call no tool in this project — SWCraft's included — until this release's " +
  "`swcraft` is on PATH.";
const EDITED_HOOK = new TextEncoder().encode('{ "hooks": {} }\n');
const NOWHERE = { pathResolution: undefined, pathVersion: undefined };

test("checkModelGuard: a swcraft that resolves nowhere, under this release's hook, says Copilot can call no tool", () => {
  const health = checkModelGuard(guardInput(NOWHERE));
  assert.equal(health.status, "warning");
  assert.equal(
    health.message,
    `Copilot model guard: ${HOOK_RELATIVE_PATH} is this release's. But a bare \`swcraft\` ` +
      "resolves nowhere on the PATH this window was launched with, so the hook cannot run, and " +
      NO_TOOL,
  );
  assert.doesNotMatch(health.message, /not measured/);
});

test("checkModelGuard: a swcraft from before guard, under this release's hook, says Copilot can call no tool", () => {
  const health = checkModelGuard(
    guardInput({ pathVersion: { ok: true, output: "swcraft 0.15.0\n" } }),
  );
  assert.equal(health.status, "warning");
  assert.ok(
    health.message.endsWith(
      `But the \`swcraft\` on PATH (${ON_PATH}) is version 0.15.0 and this extension is ` +
        `${EXTENSION_VERSION}; 0.15.0 has no \`guard\`, so ${NO_TOOL}`,
    ),
    health.message,
  );
});

test("checkModelGuard: an older swcraft names its version, the extension's, the PATH, the cost and the hook finding", () => {
  const health = checkModelGuard(
    guardInput({ pathVersion: { ok: true, output: "swcraft 0.6.9\n" } }),
  );
  assert.equal(health.status, "warning");
  assert.ok(health.message.includes(ON_PATH));
  assert.match(health.message, /version 0\.6\.9/);
  assert.match(health.message, EXTENSION_VERSION_PATTERN);
  assert.match(health.message, /0\.6\.9 has no `guard`/);
  assert.ok(health.message.includes(NO_TOOL), health.message);
  assert.match(health.message, /no tool in this project — SWCraft's included/);
  // The file's own finding is kept beside the PATH one.
  assert.match(health.message, /is this release's\./);
});

test("checkModelGuard: a swcraft whose version cannot be read gives the conditional cost", () => {
  for (const pathVersion of [
    undefined,
    { ok: false, output: "" },
    { ok: true, output: "usage: swcraft [-h]\n" },
  ]) {
    const health = checkModelGuard(guardInput({ pathVersion }));
    assert.equal(health.status, "warning", JSON.stringify(pathVersion));
    assert.ok(
      health.message.endsWith(
        `But the \`swcraft\` on PATH (${ON_PATH}) reported no version this could read; if it ` +
          `has no \`guard\`, ${NO_TOOL}`,
      ),
      health.message,
    );
  }
});

test("checkModelGuard: another release that has guard is a warning without 'no tool'", () => {
  for (const [reported, extension] of [
    ["0.16.0", "0.16.1"],
    ["99.0.0", EXTENSION_VERSION],
  ] as const) {
    const health = checkModelGuard(
      guardInput({
        pathVersion: { ok: true, output: `swcraft ${reported}\n` },
        extensionVersion: extension,
      }),
    );
    assert.equal(health.status, "warning", reported);
    assert.ok(health.message.includes(`version ${reported}`), health.message);
    assert.ok(health.message.includes(`this extension is ${extension}`), health.message);
    assert.match(health.message, /the hook's answer is that release's `swcraft guard`'s\./);
    assert.doesNotMatch(health.message, /no tool/);
    assert.doesNotMatch(health.message, /older/);
  }
});

test("checkModelGuard: this release's swcraft on PATH adds no finding", () => {
  const health = checkModelGuard(
    guardInput({
      extensionVersion: "0.16.1",
      pathVersion: { ok: true, output: "swcraft 0.16.1\n" },
    }),
  );
  assert.equal(health.status, "ok");
  assert.doesNotMatch(health.message, /But /);
});

test("checkModelGuard: a hook that differs makes the cost conditional on it still running guard", () => {
  const nowhere = checkModelGuard(guardInput({ hook: EDITED_HOOK, ...NOWHERE }));
  assert.equal(nowhere.status, "warning");
  assert.ok(
    nowhere.message.endsWith(
      `so the hook cannot run, and if the hook still runs \`swcraft guard\`, ${NO_TOOL}`,
    ),
    nowhere.message,
  );
  const older = checkModelGuard(
    guardInput({ hook: EDITED_HOOK, pathVersion: { ok: true, output: "swcraft 0.15.0\n" } }),
  );
  assert.ok(
    older.message.endsWith(
      `0.15.0 has no \`guard\`, so if the hook still runs \`swcraft guard\`, ${NO_TOOL}`,
    ),
    older.message,
  );
  const unreadable = checkModelGuard(guardInput({ hook: EDITED_HOOK, pathVersion: undefined }));
  assert.ok(
    unreadable.message.endsWith(
      `if it has no \`guard\` and the hook still runs \`swcraft guard\`, ${NO_TOOL}`,
    ),
    unreadable.message,
  );
});

test("checkModelGuard: with no hook the PATH finding is kept with no cost — nothing runs", () => {
  const nowhere = checkModelGuard(guardInput({ hook: undefined, ...NOWHERE }));
  assert.equal(nowhere.status, "warning");
  assert.match(nowhere.message, /the model is not protected from Copilot's file tools/);
  assert.ok(
    nowhere.message.endsWith(
      "resolves nowhere on the PATH this window was launched with, so the hook cannot run.",
    ),
    nowhere.message,
  );
  assert.doesNotMatch(nowhere.message, /no tool/);
  const older = checkModelGuard(
    guardInput({ hook: undefined, pathVersion: { ok: true, output: "swcraft 0.15.0\n" } }),
  );
  assert.ok(older.message.endsWith("0.15.0 has no `guard`."), older.message);
  assert.doesNotMatch(older.message, /no tool/);
  const unreadable = checkModelGuard(guardInput({ hook: undefined, pathVersion: undefined }));
  assert.ok(unreadable.message.endsWith("so it may have no `guard`."), unreadable.message);
  assert.doesNotMatch(unreadable.message, /no tool/);
});

test("checkModelGuard: another release that has guard, with no hook, names no hook's answer", () => {
  const health = checkModelGuard(
    guardInput({
      hook: undefined,
      pathVersion: { ok: true, output: "swcraft 0.16.0\n" },
      extensionVersion: "0.16.1",
    }),
  );
  assert.equal(health.status, "warning");
  assert.match(health.message, /the model is not protected from Copilot's file tools/);
  assert.ok(
    health.message.endsWith(
      `But the \`swcraft\` on PATH (${ON_PATH}) is version 0.16.0 and this extension is ` +
        "0.16.1; 0.16.0 has `guard`, but no hook runs it in this project.",
    ),
    health.message,
  );
  assert.doesNotMatch(health.message, /the hook's answer/);
  assert.doesNotMatch(health.message, /no tool/);
});

test("checkModelGuard: another release that has guard, with a hook that differs, answers conditionally", () => {
  const health = checkModelGuard(
    guardInput({
      hook: EDITED_HOOK,
      pathVersion: { ok: true, output: "swcraft 0.16.0\n" },
      extensionVersion: "0.16.1",
    }),
  );
  assert.equal(health.status, "warning");
  assert.ok(
    health.message.endsWith(
      "this extension is 0.16.1; if the hook still runs `swcraft guard`, its answer is that " +
        "release's `swcraft guard`'s.",
    ),
    health.message,
  );
  assert.doesNotMatch(health.message, /no tool/);
});

test("checkModelGuard: an unreadable bundled hook keeps the PATH finding beside missing", () => {
  const health = checkModelGuard(guardInput({ bundled: undefined, ...NOWHERE }));
  assert.equal(health.status, "missing");
  assert.match(health.message, /Reinstall the extension/);
  assert.match(health.message, /a bare `swcraft` resolves nowhere/);
  // The project's hook cannot be shown to be this release's: the cost is conditional.
  assert.match(health.message, /if the hook still runs `swcraft guard`, Copilot can call no tool/);
  const noHook = checkModelGuard(guardInput({ bundled: undefined, hook: undefined, ...NOWHERE }));
  assert.equal(noHook.status, "missing");
  assert.match(noHook.message, /a bare `swcraft` resolves nowhere/);
  assert.doesNotMatch(noHook.message, /no tool/);
});

test("checkModelGuard: the hook is named by its relative path; the only absolute path is the PATH swcraft", () => {
  // The input carries no project directory at all, so the one absolute path a
  // message can name is `pathResolution`, and only where the PATH finding is
  // printed: the user needs to know which `swcraft` the hook would run.
  const edited = new TextEncoder().encode('{ "hooks": {} }\n');
  const older = { ok: true, output: "swcraft 0.6.9\n" };
  for (const [overrides, namesOnPath] of [
    [{}, false],
    [{ hook: undefined }, false],
    [{ hook: edited }, false],
    [{ bundled: undefined }, false],
    [{ pathVersion: older }, true],
    [{ hook: undefined, pathVersion: older }, true],
  ] as const) {
    const health = checkModelGuard(guardInput(overrides));
    assert.ok(health.message.includes(HOOK_RELATIVE_PATH), health.message);
    assert.equal(health.message.includes(ON_PATH), namesOnPath, health.message);
  }
});

test("buildHealthReport: the model guard row is printed after the profile; its warnings are not counted", () => {
  const profile = checkAgentProfile(BUNDLED_PROFILE, BUNDLED_PROFILE);
  const warned = input({
    agentProfile: profile,
    modelGuard: checkModelGuard(guardInput({ hook: undefined })),
  });
  const lines = buildHealthReport(warned);
  const guardLine = lines.findIndex((line) => line.startsWith("  [warning] Copilot model guard"));
  const profileLine = lines.findIndex((line) =>
    line.startsWith("  [ok]      Copilot agent profile"),
  );
  assert.ok(guardLine > profileLine && profileLine > 0, lines.join("\n"));
  assert.equal(countProblems(warned), 0);

  const broken = input({ modelGuard: checkModelGuard(guardInput({ bundled: undefined })) });
  assert.equal(countProblems(broken), 1);

  assert.equal(
    buildHealthReport(input()).some((line) => line.includes("Copilot model guard")),
    false,
    "no SWCraft project, no model guard row",
  );
});

test("the Health adapter passes the hook reading and the PATH probe into HealthInput", () => {
  // The adapter is VS Code glue and cannot run here; its body is read as text.
  const source = readSource("src/commands.ts");
  const reader = functionBody(source, "async function readModelGuardHealth(");
  assert.match(reader, /checkModelGuard\(/);
  assert.match(reader, /hook: readBytesOrUndefined\(hookPath\(projectDir\)\)/);
  assert.match(
    reader,
    /bundled: readBytesOrUndefined\(path\.join\(onboardingAssetsDir\(context\), HOOK_ASSET_NAME\)\)/,
  );
  // PATH and the probe come from the run's one reading, never a second resolution.
  assert.match(reader, /const \{ pathResolution \} = reading;/);
  assert.match(reader, /await reading\.probe\(pathResolution\)/);
  assert.doesNotMatch(reader, /resolveOnPath\(|probeToolchainVersion\(/);
  assert.match(reader, /extensionVersion: extensionVersion\(context\)/);
  const once = functionBody(source, "function readToolchainOnce(");
  assert.match(once, /resolveOnPath\(process\.env, platform, fs\.existsSync\)/);
  assert.match(
    once,
    /probeOncePerPath\(\(executable\) => probeToolchainVersion\(executable\), platform\)/,
  );
  const toolchain = functionBody(source, "async function readToolchainHealth(");
  assert.match(toolchain, /pathResolution: reading\.pathResolution/);
  assert.match(toolchain, /await reading\.probe\(resolution\.executable\)/);
  assert.doesNotMatch(toolchain, /resolveOnPath\(|probeToolchainVersion\(/);
  const command = functionBody(source, "async function checkEditorToolingCommand(");
  assert.match(command, /const reading = readToolchainOnce\(\);/);
  assert.match(command, /await readToolchainHealth\(context, executableSetting\(\), reading\)/);
  assert.match(command, /await readModelGuardHealth\(context, reading\)/);
  assert.match(command, /\bmodelGuard,/);
});

test("probeOncePerPath: one executable is probed once and both callers get that answer", async () => {
  const probed: string[] = [];
  const probe = probeOncePerPath(async (executable) => {
    probed.push(executable);
    return { ok: true, output: `swcraft ${probed.length}.0.0` };
  }, "posix");
  const first = await probe("/opt/swcraft/bin/swcraft");
  const again = await probe("/opt/swcraft/bin/swcraft");
  assert.deepEqual(again, first);
  assert.deepEqual(probed, ["/opt/swcraft/bin/swcraft"]);
  const other = await probe("/usr/local/bin/swcraft");
  assert.equal(other.output, "swcraft 2.0.0");
  assert.deepEqual(probed, ["/opt/swcraft/bin/swcraft", "/usr/local/bin/swcraft"]);
});

test("probeOncePerPath: the same file is compared as samePath does — by case on POSIX, not on Windows", async () => {
  for (const [platform, expected] of [
    ["posix", 2],
    ["windows", 1],
  ] as const) {
    let calls = 0;
    const probe = probeOncePerPath(async () => {
      calls += 1;
      return { ok: true, output: "swcraft 1.0.0" };
    }, platform);
    await probe("C:/Tools/SWCraft/swcraft.exe");
    await probe("c:\\tools\\swcraft\\SWCRAFT.EXE");
    assert.equal(calls, expected, platform);
  }
});

test("no runtime probe exists — this module still imports one thing", () => {
  // Asserted rather than trusted: the report's limits must not quietly grow into
  // a probe of a member's runtime (the per-member runtime table
  // `checkPackHealth`'s reasoning forbids). A probe cannot be written without
  // reaching outside this module: resolving a program needs child_process, and
  // reading another extension's setting needs `vscode`. Neither may appear.
  const source = readSource("src/healthCheck.ts");
  const imported = [...source.matchAll(/^import[^;]*?from "([^"]+)";/gm)].map((match) => match[1]);
  assert.deepEqual(
    imported,
    ["./toolchain"],
    "healthCheck.ts is pure logic over one sibling module",
  );
  assert.equal(/\brequire\(|\bimport\(/.test(source), false, "no dynamic import either");
});

// The report is read on a colleague's machine, where the source repository is not.
// What this does not reach: a message of a fault state neither input below builds,
// and every other extension surface.
const REPOSITORY_PATH =
  /(?:^|[^\w.-])(?:docs|packages|tools|scripts|mcp_server|agent|vscode_extension)\/|[\w-]\.py\b|\bCLAUDE\.md\b/;

test("the repository-path check matches every shape it is there to refuse", () => {
  for (const line of [
    "see docs/guide/vscode.md.",
    "held by packages/tools/tests/test_editor_schema_drift.py, never here.",
    "run scripts/dev.py",
    "packages/mcp/src/server.ts",
    "(agent/README)",
    "vscode_extension/src/commands.ts",
    "the check in generate.py",
    "see CLAUDE.md",
  ]) {
    assert.match(line, REPOSITORY_PATH);
  }
  // The left boundary: a name that only ends like a top-level directory is no such path.
  for (const line of [".github/agents/x", "sub-docs/"]) {
    assert.doesNotMatch(line, REPOSITORY_PATH);
  }
});

test("buildHealthReport: no line names a path of the source repository, healthy or faulted", () => {
  const faulted = input({
    installed: installed({ id: "alpha.one", version: "1.0.0", engineRange: "^1.200.0" }),
    toolchain: checkToolchain(
      toolchainInput({
        resolution: resolveSwcraft(undefined, {}, "/home/u", "posix", () => false),
        version: undefined,
      }),
    ),
    schemas: checkBundledSchemas([], "/ext/schemas", DECLARED_SCHEMAS),
    agentProfile: checkAgentProfile(undefined, BUNDLED_PROFILE),
    modelGuard: checkModelGuard(guardInput({ bundled: undefined })),
  });
  // The faulted input is faulted: every row prints its fault message, the member its below-floor one.
  for (const row of [
    faulted.toolchain,
    faulted.schemas,
    faulted.agentProfile,
    faulted.modelGuard,
  ]) {
    assert.equal(row?.status, "missing");
  }
  assert.ok(
    checkPackHealth(faulted.installed, faulted.declaredPack, faulted.vscodeVersion).some(
      (member) => member.status !== "present",
    ),
  );
  for (const report of [input(), faulted]) {
    for (const line of buildHealthReport(report)) {
      assert.doesNotMatch(line, REPOSITORY_PATH);
    }
  }
});

test("countProblems agrees with the markers buildHealthReport renders", () => {
  const cases = [
    input(),
    input({ installed: [] }),
    input({ declaredPack: [] }),
    input({
      toolchain: checkToolchain(
        toolchainInput({
          resolution: resolveSwcraft(undefined, {}, "/home/u", "posix", () => false),
          version: undefined,
        }),
      ),
    }),
    input({ toolchain: checkToolchain(toolchainInput({ pathResolution: undefined })) }),
    input({ schemas: checkBundledSchemas(undefined, "/ext/schemas", DECLARED_SCHEMAS) }),
  ];
  for (const candidate of cases) {
    const lines = buildHealthReport(candidate);
    assert.equal(countProblems(candidate), lines.filter(isProblemLine).length);
  }
});

// --- The pack is declared in package.json, and only there --------------------

test("the declared pack is the manifest's — checkPackHealth reports whatever it is handed", () => {
  // Purity from the outside: given a pack of names that appear nowhere in this
  // repository, the report is about exactly those names and mentions no
  // shipped member. A checker consulting a list of its own could not do this.
  const members = checkPackHealth([], ["some.invented-extension"], HOST);
  assert.deepEqual(
    members.map((member) => member.id),
    ["some.invented-extension"],
  );
  const text = members.map((member) => member.message).join("\n");
  const shipped = readManifest().extensionPack ?? [];
  assert.ok(shipped.length > 0, "package.json must declare an extensionPack for this loop to bite");
  for (const declared of shipped) {
    assert.equal(text.includes(declared), false, `${declared} must not leak into the report`);
  }
});

test("the adapter reads the declared pack from the extension's own packageJSON", () => {
  const source = readSource("src/commands.ts");
  assert.match(
    source,
    /context\.extension\.packageJSON\??\.extensionPack/,
    "commands.ts must read the pack from its own manifest, not from a list in TypeScript",
  );
});

test("the adapter reads the required schema inventory from the extension's own packageJSON", () => {
  // The same rule as the pack above, for the same reason: the required
  // inventory is declared in `contributes.yamlValidation`, and a list of schema
  // basenames retyped in TypeScript would go stale silently on the one surface
  // whose job is noticing something missing.
  const source = readSource("src/commands.ts");
  assert.match(
    source,
    /declaredSchemaFiles\(\s*context\.extension\.packageJSON\??\.contributes\??\.yamlValidation/,
    "commands.ts must derive the required schema files from its own contributes.yamlValidation",
  );
});

test("no production module names a schema basename — contributes.yamlValidation is the declaration", () => {
  const declared = declaredSchemaFiles(readManifest().contributes?.yamlValidation);
  assert.ok(declared.length > 0, "the manifest must declare schema urls for this to bite");
  for (const module of productionModules()) {
    const source = readSource(path.posix.join("src", module));
    for (const name of declared) {
      assert.equal(
        source.includes(name),
        false,
        `${module} names ${name}; the required inventory is contributes.yamlValidation's alone`,
      );
    }
  }
});

test("no production module names a pack member id — package.json is the only declaration", () => {
  // The strongest form of the guard: a second literal list is what goes stale, so no member id
  // may appear in production TypeScript at all. `healthCheck.ts` names a member by its display
  // name ("Red Hat XML") in prose.
  const declared = readManifest().extensionPack ?? [];
  assert.ok(declared.length > 0, "package.json must declare an extensionPack for this to bite");
  // The module list is derived, not written, so no production `.ts` under `src/` escapes the
  // guard; tests are excluded, since a test naming a member id is how the shipped pack is
  // asserted at all. `productionModules()` carries the non-emptiness floor.
  for (const module of productionModules()) {
    const source = readSource(path.posix.join("src", module));
    for (const id of declared) {
      assert.equal(
        source.includes(id),
        false,
        `${module} names the pack member '${id}' — the pack is declared in package.json alone`,
      );
    }
  }
});

test("the manifest's own pack, fully installed on this floor, reports all present", () => {
  // The end-to-end shape with the real membership: the ids from package.json, the measured
  // versions and floors, and the verdict a healthy online install should produce.
  const declared = readManifest().extensionPack ?? [];
  assert.ok(declared.length > 0, "an empty pack would make the comparison below vacuous");
  const members = checkPackHealth(
    declared.map((id) => ({ id, version: "1.0.0", engineRange: "^1.110.0" })),
    declared,
    HOST,
  );
  assert.deepEqual(
    members.map((member) => member.status),
    declared.map(() => "present"),
  );
});

test("copilotNotUsed: a project set up for another client alone gets an ok row naming why nothing was checked", () => {
  for (const label of ["Copilot agent profile", "Copilot model guard"]) {
    const row = copilotNotUsed(label, "the client");
    assert.equal(row.status, "ok");
    assert.equal(row.label, label);
    assert.match(row.message, new RegExp(`^${label}: not checked`));
    assert.match(row.message, /none of the client's/);
    assert.equal(row.message.includes("\n"), false);
  }
  // The adapter asks the client table before either Copilot row compares anything.
  const commands = readSource("src/commands.ts");
  for (const [reader, label] of [
    ["function readAgentProfileHealth(", "Copilot agent profile"],
    ["async function readModelGuardHealth(", "Copilot model guard"],
  ]) {
    const body = functionBody(commands, reader);
    assert.match(body, /const copilot = copilotUnused\(context, projectDir\);/);
    assert.ok(body.includes(`return copilotNotUsed("${label}", copilot);`), reader);
    assert.ok(body.indexOf("copilotUnused(") < body.indexOf("return check"), reader);
  }
});

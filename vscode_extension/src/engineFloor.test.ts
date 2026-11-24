// The VS Code compatibility floor.
//
// `engines.vscode` is the VS Code version the extension declares it installs into;
// `@types/vscode` is the API surface the compiler type-checks against. They state one number
// in two places, and nothing else notices when only one moves:
//
//   - raising `engines.vscode` alone compiles against the older API declarations, so a new
//     API is a compile error nobody understands;
//   - raising `@types/vscode` alone compiles against an API the floor's host lacks: a runtime
//     `undefined is not a function` on the oldest supported VS Code.
//
// The floor `^1.110.0` was the highest `engines.vscode` of a pack member that has since left
// the pack. It stays although the remaining members need less (`^1.67.0`): lowering it would
// declare hosts the extension's code was never compiled or run against. This test does not
// re-measure (a CI host has no pack member to read); it pins that the two declarations agree
// and that the floor stays at or above the members'.
import assert from "node:assert/strict";
import { test } from "node:test";

import { readManifest } from "./testSupport";

/** `engines.vscode`, asserted present rather than defaulted. */
function engineFloor(): string {
  const declared = readManifest().engines?.vscode;
  assert.ok(
    typeof declared === "string" && declared.length > 0,
    "package.json must declare engines.vscode",
  );
  return declared;
}

/** The `@types/vscode` devDependency range, asserted present rather than defaulted. */
function typesPin(): string {
  const declared = readManifest().devDependencies?.["@types/vscode"];
  assert.ok(
    typeof declared === "string" && declared.length > 0,
    "package.json must declare a @types/vscode devDependency",
  );
  return declared;
}

test("engines.vscode and the @types/vscode pin declare the same floor", () => {
  assert.equal(
    typesPin(),
    engineFloor(),
    "engines.vscode and devDependencies['@types/vscode'] must be the same range — " +
      "a floor raised in one place only compiles against one API version and installs against another",
  );
});

test("the declared floor is a caret range over a concrete 1.x version", () => {
  // VS Code's own convention, and what `vsce` expects: `^1.<minor>.<patch>`.
  // A bare version, a `>=` range or a `*` would still satisfy the equality
  // test above while saying something quite different about compatibility.
  assert.match(
    engineFloor(),
    /^\^1\.\d+\.\d+$/,
    `engines.vscode must be a caret range over a concrete 1.x version, got "${engineFloor()}"`,
  );
});

test("the floor is at least the highest measured extensionPack member floor", () => {
  // The members' floors as measured: redhat.vscode-xml ^1.67.0, redhat.vscode-yaml ^1.63.0.
  // A lower bound, not an equality: re-measuring a member higher raises this manifest without
  // failing here; dropping below a member's floor would declare members the host cannot install.
  const MEASURED_MEMBER_FLOOR = [1, 67, 0];
  const match = /^\^(\d+)\.(\d+)\.(\d+)$/.exec(engineFloor());
  assert.ok(match, `engines.vscode must be a caret range, got "${engineFloor()}"`);
  const declared = [Number(match[1]), Number(match[2]), Number(match[3])];
  const isAtLeast =
    declared[0] > MEASURED_MEMBER_FLOOR[0] ||
    (declared[0] === MEASURED_MEMBER_FLOOR[0] &&
      (declared[1] > MEASURED_MEMBER_FLOOR[1] ||
        (declared[1] === MEASURED_MEMBER_FLOOR[1] && declared[2] >= MEASURED_MEMBER_FLOOR[2])));
  assert.ok(
    isAtLeast,
    `engines.vscode "${engineFloor()}" is below the highest measured extensionPack member floor ` +
      `^${MEASURED_MEMBER_FLOOR.join(".")} (redhat.vscode-xml)`,
  );
});

test("@vscode/test-electron is pinned at or above 3.1.0", () => {
  // The harness that proves the floor: VS Code 1.110+ renamed the macOS bundle executable from
  // `Contents/MacOS/Electron` to the product name (`Code`); @vscode/test-electron 3.0.0
  // resolves the old name and fails `spawn … ENOENT`, 3.1.0 the new one. Raising the floor
  // without this bump leaves the host-level suite unable to start.
  const declared = readManifest().devDependencies?.["@vscode/test-electron"];
  assert.ok(
    typeof declared === "string",
    "package.json must declare a @vscode/test-electron devDependency",
  );
  const match = /^\^(\d+)\.(\d+)\.(\d+)$/.exec(declared);
  assert.ok(match, `@vscode/test-electron must be a caret range, got "${declared}"`);
  const [major, minor] = [Number(match[1]), Number(match[2])];
  assert.ok(
    major > 3 || (major === 3 && minor >= 1),
    `@vscode/test-electron "${declared}" is below ^3.1.0, which is the first release that resolves ` +
      "the renamed macOS bundle executable of VS Code 1.110+",
  );
});

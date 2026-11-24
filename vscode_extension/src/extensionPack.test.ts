// Pack membership and Marketplace categories.
//
// Two manifest facts, neither a preference:
//
//   - **`extensionPack`, never `extensionDependencies`.** SWCraft calls no other extension's
//     API. It contributes `yamlValidation` (data Red Hat YAML reads if installed) and
//     associates `.arxml` with the built-in `xml` language (Red Hat XML validates it if
//     installed). Each degrades to plain-text editing with every SWCraft command unaffected,
//     which is what a pack member is and a dependency VS Code refuses to activate without is
//     not (`docs/guide/vscode.md` § *Install the extension*).
//   - **the categories are the functional ones**, not the placeholder `Other`.
//
// The workstation archive carries the members as `.vsix` files, so the pack list is also a
// payload list: `scripts/workstation/pins/runtime-pins.json` pins one build of each, and the
// pin-agreement test below keeps the two lists one list.
//
// No `vscode` import: this runs under plain `node --test`. Whether a host resolves the
// members at install time is a clean-host acceptance question, not this file's.
import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as path from "node:path";
import { test } from "node:test";

import { PROJECT_ROOT, readManifest } from "./testSupport";

/**
 * The committed pins the workstation archive's payloads are fetched by. Its
 * `extensions` list is what `python scripts/dev.py update-pins` minted from this
 * manifest's `extensionPack`, and what `dev.py runtime` fetches.
 */
const RUNTIME_PINS = path.join(
  PROJECT_ROOT,
  "..",
  "scripts",
  "workstation",
  "pins",
  "runtime-pins.json",
);

/**
 * The pack, in the order a reader meets its members: the model YAML, then the ARXML it
 * generates into. `likec4.likec4-vscode` left when the diagrams became SVG pages no editor
 * extension reads (`RETIRED_MEMBERS`).
 *
 * Order is asserted, not only membership: it is the order VS Code lists the pack in, and an
 * unordered assertion would let a member be swapped for another without notice.
 */
const PACK_MEMBERS = ["redhat.vscode-yaml", "redhat.vscode-xml"];

/** Members the pack once declared and must not declare again without an argument here. */
const RETIRED_MEMBERS = ["likec4.likec4-vscode"];

/**
 * Every category is one VS Code 1.134.0 admits, and each earns its place:
 *
 *   - `Programming Languages` — `contributes.yamlValidation` binds a generated JSON Schema to
 *     each model layer, and the manifest associates `.arxml` files;
 *   - `Visualization` — `SWCraft: Diagrams` generates the model's diagrams as SVG pages;
 *   - `AI` — the extension registers the SWCraft MCP server
 *     (`contributes.mcpServerDefinitionProviders`) and onboarding installs the
 *     `swcraft-architect` Copilot agent profile;
 *   - `Extension Packs` — the manifest declares an `extensionPack`.
 *
 * Declined: `Linters` and `Formatters` (the extension lints and formats nothing; its Problems
 * entries are one `swcraft check-model` run's findings, rendered), `Snippets` (the completion
 * snippets are the YAML schemas', served by Red Hat YAML), `Testing`, `Chat` (no chat
 * participant; the agent profile is a file Copilot reads) and `Other` (the placeholder).
 */
const CATEGORIES = ["Programming Languages", "Visualization", "AI", "Extension Packs"];

/**
 * The `categories` enum of VS Code 1.134.0's own extension manifest schema,
 * read from the app bundle rather than from a documentation page:
 * `out/vs/workbench/workbench.desktop.main.js`, the array the
 * `categories.items.oneOf[0].enum` of the extension-point schema points at.
 *
 * A category outside this list is not an error VS Code reports at install time;
 * it is simply ignored, so a typo would silently cost the extension its
 * Marketplace placement.
 */
const VS_CODE_CATEGORIES = [
  "AI",
  "Azure",
  "Chat",
  "Data Science",
  "Debuggers",
  "Extension Packs",
  "Education",
  "Formatters",
  "Keymaps",
  "Language Packs",
  "Linters",
  "Machine Learning",
  "Notebooks",
  "Programming Languages",
  "SCM Providers",
  "Snippets",
  "Testing",
  "Themes",
  "Visualization",
  "Other",
];

test("the extension pack declares exactly the two measured members, in order", () => {
  assert.deepEqual(readManifest().extensionPack, PACK_MEMBERS);
});

test("the LikeC4 member is gone from the pack and from the archive's pins", () => {
  // The equality above would already fail on its return; this names the member
  // so the failure says which decision is being reversed. The pins are checked
  // by id too, because the archive carrying a `.vsix` the pack no longer
  // declares would install it for nothing.
  const pins = JSON.parse(fs.readFileSync(RUNTIME_PINS, "utf8")) as {
    extensions?: { id: string }[];
  };
  const pinned = (pins.extensions ?? []).map((entry) => entry.id);
  for (const retired of RETIRED_MEMBERS) {
    assert.equal((readManifest().extensionPack ?? []).includes(retired), false, retired);
    assert.equal(pinned.includes(retired), false, `runtime-pins.json still pins ${retired}`);
  }
});

test("the archive's pinned extensions are exactly the pack's members, in the pack's order", () => {
  // The one assertion that keeps payload and pack from drifting apart: a member
  // added to the pack and not pinned would be declared and never carried, and a
  // pinned one the pack dropped would be carried and installed for nothing.
  const pins = JSON.parse(fs.readFileSync(RUNTIME_PINS, "utf8")) as {
    extensions?: { id: string }[];
  };
  assert.deepEqual(
    (pins.extensions ?? []).map((entry) => entry.id),
    readManifest().extensionPack,
  );
  assert.deepEqual(
    (pins.extensions ?? []).map((entry) => entry.id),
    PACK_MEMBERS,
  );
});

test("no extensionDependencies key is declared at all", () => {
  // `assert.equal(..., undefined)` would also pass for a declared `null` or an
  // explicit `undefined`; the claim is that the key is absent, so the key is
  // what is checked. A hard dependency here would make VS Code refuse to
  // activate SWCraft when a member is missing or disabled — the opposite of the
  // graceful degradation the pack is chosen for.
  assert.ok(
    !Object.hasOwn(readManifest(), "extensionDependencies"),
    "package.json must declare no extensionDependencies key: a member missing or disabled must not stop SWCraft",
  );
});

test("every pack member id is a publisher.name identifier", () => {
  // A pack member is resolved from the gallery by this exact string. A typo
  // resolves nothing, and VS Code reports it only in the install log.
  for (const member of readManifest().extensionPack ?? []) {
    assert.match(
      member,
      /^[a-z0-9][a-z0-9-]*\.[a-z0-9][a-z0-9-]*$/,
      `"${member}" is not a lowercase publisher.name extension identifier`,
    );
  }
});

test("the pack member ids are unique", () => {
  const members = readManifest().extensionPack ?? [];
  assert.deepEqual([...new Set(members)], members, "a pack member is declared twice");
});

test("categories are exactly the functional set", () => {
  assert.deepEqual(readManifest().categories, CATEGORIES);
});

test("every declared category is one VS Code 1.134.0 admits", () => {
  // The guard the equality above cannot give: it pins *this* set, while this
  // one pins that a future edit of that set stays inside the vocabulary. What
  // neither reaches: whether a category is the *apt* one — that is the
  // reasoning beside CATEGORIES, and nothing tests reasoning.
  for (const category of readManifest().categories ?? []) {
    assert.ok(
      VS_CODE_CATEGORIES.includes(category),
      `"${category}" is not one of VS Code 1.134.0's extension categories`,
    );
  }
});

test("the placeholder category is gone", () => {
  // `Other` is admissible, so the test above would keep passing if it came back beside the
  // functional ones.
  assert.ok(
    !(readManifest().categories ?? []).includes("Other"),
    "`Other` is the placeholder the functional categories replaced; it must not return beside them",
  );
});

// The `.arxml` file association: SWCraft maps `.arxml` onto VS Code's built-in `xml` language id.
// Each consequence below is asserted because a later edit could reverse it unnoticed:
//
//   1. **`.arxml` is xml.** The generated file is an XML document, and all XML tooling (the
//      built-in grammar, Red Hat XML's LemMinX server, other editors' XML modes) keys off that id;
//      an id of our own would take the file away from all of them for nothing.
//   2. **The entry extends a language, it does not declare one.** VS Code's language registry
//      merges a contribution into an existing entry with the same `id` (`_registerLanguage` calls
//      `_mergeLanguage` on the hit; measured in the 1.134.0 bundle), so `{id: "xml",
//      extensions: [".arxml"]}` appends one extension to the built-in `xml` language and nothing
//      else.
//   3. **No XSD binding is contributed.** Red Hat XML's schema binding is a *user* setting. The
//      AUTOSAR schemas are licensed files this repository cannot ship, and `swcraft validate` is
//      the authoritative ARXML gate: an editor diagnostic is never validation evidence, and a
//      contributed binding would make a squiggle *look* like the gate.
//
// No `vscode` import: this runs under plain `node --test`. What a host does with the association
// is `src/test/integration/smoke.test.ts`'s, which opens a real `.arxml` in an Extension
// Development Host and reads back its `languageId`.
import assert from "node:assert/strict";
import { test } from "node:test";

import { type LanguageContribution, readManifest } from "./testSupport";

/** The extension SWCraft associates, and the language id it associates it with. */
const ARXML_EXTENSION = ".arxml";
const XML_LANGUAGE_ID = "xml";

/**
 * Language ids that already exist without SWCraft: the allowlist behind "SWCraft claims no
 * language id of its own".
 *
 * Where each entry comes from (macOS, VS Code 1.134.0):
 *
 *   - `xml`, `xsl`: the built-in `vscode.xml` extension (`contributes.languages[].id` of
 *     `/Applications/Visual Studio Code.app/Contents/Resources/app/extensions/xml/package.json`).
 *     It declares no `main` and contributes only `languages` and `grammars`, which is why its
 *     highlighting survives every pack member's absence.
 *   - `yaml`: the built-in `vscode.yaml` extension, same shape.
 *   - `dtd`, `rnc`: `redhat.vscode-xml` 0.29.3's only languages. `xml` is the built-in id, which is
 *     why the association above is what makes Red Hat XML pick up an `.arxml` file at all.
 *
 * **What this list does not reach.** It is the set SWCraft may *name*, not every id a host has
 * registered (a host with other extensions has many more). SWCraft only ever extends something
 * the pack accounts for, so a new id has to be argued for here before it can ship.
 */
const IDS_OWNED_BY_VSCODE_OR_A_PACK_MEMBER = new Set(["xml", "xsl", "yaml", "dtd", "rnc"]);

/**
 * The Red Hat XML settings that bind a schema to a document, which a `configurationDefaults`
 * block would have to name to make SWCraft's build imply an editor-side XSD check. Spelled as
 * `redhat.vscode-xml` 0.29.3's `contributes.configuration.properties` declares them.
 */
const XSD_BINDING_SETTINGS = [
  "xml.fileAssociations",
  "xml.catalogs",
  "xml.validation.enabled",
  "xml.validation.schema.enabled",
  "xml.validation.noGrammar",
  "xml.validation.filters",
];

function languageContributions(): LanguageContribution[] {
  return readManifest().contributes?.languages ?? [];
}

test("the .arxml extension is associated with the built-in xml language id", () => {
  const entries = languageContributions();
  assert.ok(entries.length > 0, "expected contributes.languages to declare at least one entry");

  const owners = entries.filter((entry) => (entry.extensions ?? []).includes(ARXML_EXTENSION));
  assert.deepEqual(
    owners.map((entry) => entry.id),
    [XML_LANGUAGE_ID],
    `expected exactly one contributes.languages entry to claim ${ARXML_EXTENSION}, and its id to be '${XML_LANGUAGE_ID}'`,
  );
});

test("no contributes.languages entry declares a language id of SWCraft's own", () => {
  for (const entry of languageContributions()) {
    assert.ok(
      IDS_OWNED_BY_VSCODE_OR_A_PACK_MEMBER.has(entry.id),
      `contributes.languages declares '${entry.id}', which no built-in and no pack member owns. ` +
        "SWCraft extends existing languages; a new id would take the file away from every tool " +
        "that keys off the standard one. See this file's header before adding to the allowlist.",
    );
  }
});

test("no .c4 association: the manifest names no .c4 file and no LikeC4 language or extension", () => {
  // The diagrams are SVG pages a browser opens, so SWCraft has no generated `.c4` file to
  // associate. The whole manifest text is scanned, because a `.c4` could return under a
  // contribution point this file does not model. Not reached: the walkthrough's Markdown pages.
  for (const entry of languageContributions()) {
    assert.equal((entry.extensions ?? []).includes(".c4"), false, `'${entry.id}' claims .c4`);
    assert.equal(/likec4/i.test(entry.id), false, `'${entry.id}' is a LikeC4 language id`);
  }
  const serialised = JSON.stringify(readManifest());
  assert.equal(/\.c4\b/.test(serialised), false, "package.json names a .c4 file");
  assert.equal(/likec4/i.test(serialised), false, "package.json names LikeC4");
});

test("the xml entry extends the language rather than redefining it", () => {
  const entry = languageContributions().find((candidate) => candidate.id === XML_LANGUAGE_ID);
  assert.ok(entry, "expected an entry for the xml language id");

  // VS Code's `_mergeLanguage` (1.134.0) appends our extensions to the built-in list without a
  // `configuration` and prepends them with one, and rewrites the display name from `aliases[0]`;
  // an entry with both `id` and `configuration` also generates `onLanguage:<id>`, which would
  // activate SWCraft on every XML file in the workspace.
  assert.equal(entry.aliases, undefined, "declaring aliases would rename VS Code's XML language");
  assert.equal(
    entry.configuration,
    undefined,
    "declaring configuration would replace the built-in XML language configuration and add an onLanguage:xml activation event",
  );
  assert.equal(entry.firstLine, undefined, "the association is by path; no first-line heuristic");
  assert.equal(entry.filenames, undefined, "the association is by extension, not by filename");
  assert.deepEqual(
    entry.extensions,
    [ARXML_EXTENSION],
    "the entry exists to add .arxml and nothing else; the built-in list already carries .xml, .xsd and 60 more",
  );
});

test("no XSD binding is contributed: schema validation stays a user setting and swcraft validate stays the gate", () => {
  const manifest = readManifest();
  const contributes = manifest.contributes ?? {};

  assert.equal(
    contributes.configurationDefaults,
    undefined,
    "contributes.configurationDefaults would push editor settings onto every user of the extension",
  );
  // SWCraft contributes its own settings (`swcraft.executable`, the workstation-installation
  // override, one configuration path per experiment kind), so the question is "does the block
  // declare anything but SWCraft's own". A `configuration` block cannot set another extension's
  // value anyway; the manifest-wide scan below is the real guard.
  const properties = Object.keys(
    ((contributes.configuration ?? {}) as { properties?: Record<string, unknown> }).properties ??
      {},
  );
  for (const property of properties) {
    assert.ok(
      property.startsWith("swcraft."),
      `contributes.configuration declares '${property}', which is not SWCraft's own setting`,
    );
  }

  // A future Red Hat XML could add another binding setting, so the whole manifest text is
  // scanned for any `xml.` key, including under contribution points this test does not model.
  const serialised = JSON.stringify(manifest);
  for (const setting of XSD_BINDING_SETTINGS) {
    assert.ok(
      !serialised.includes(setting),
      `package.json names '${setting}'. An XSD binding is a user configuration: SWCraft never invokes ` +
        "an editor-side schema check, and swcraft validate is the authoritative ARXML gate.",
    );
  }
  assert.ok(!/"xml\.[a-zA-Z]/.test(serialised), "package.json names an `xml.*` setting");
});

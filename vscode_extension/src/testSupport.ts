// Helpers shared by the unit tests that read the extension's own manifest,
// schemas and sources. A plain module rather than an export from a *.test.ts
// file: `node --test` registers a file's tests at import time, so importing a
// test file would run its tests a second time.
import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as path from "node:path";
import * as ts from "typescript";

import type { Reader } from "./navigationCore";

/** The extension root: tsc emits src/*.ts to out/ (rootDir: src), one level below it. */
export const PROJECT_ROOT = path.join(__dirname, "..");

/**
 * The smallest number of production modules `productionModules()` accepts: a
 * little below the current count, so merging two modules needs no edit here
 * while a `src/` that stopped being read still fails.
 */
export const PRODUCTION_MODULE_FLOOR = 24;
export const SCHEMAS_DIR = path.join(PROJECT_ROOT, "schemas");

/** One `contributes.commands` entry, typed only as far as the tests read it. */
export interface CommandContribution {
  command: string;
  title: string;
}

/**
 * One step of a `contributes.walkthroughs` entry.
 *
 * `media` is typed as the markdown variant alone, the only one SWCraft uses; a
 * step switched to `image` or `svg` fails `walkthrough.test.ts`'s media
 * assertions.
 */
export interface WalkthroughStep {
  id: string;
  title: string;
  description?: string;
  media: { markdown?: string };
  completionEvents?: string[];
}

/** One `contributes.walkthroughs` entry. */
export interface Walkthrough {
  id: string;
  title: string;
  description: string;
  steps: WalkthroughStep[];
}

/**
 * One `contributes.languages` entry.
 *
 * Typed with the keys `languageAssociation.test.ts` reads and the keys it asserts
 * absent (`aliases`, `configuration`). VS Code 1.134.0 requires only `id`.
 */
export interface LanguageContribution {
  id: string;
  extensions?: string[];
  filenames?: string[];
  firstLine?: string;
  aliases?: string[];
  configuration?: string;
}

/**
 * One `capabilities.*` entry.
 *
 * VS Code 1.134.0 admits a bare boolean or this object for `virtualWorkspaces`,
 * and only this object for `untrustedWorkspaces` (`supported` required).
 * `restrictedConfigurations` is read only when `supported` is `"limited"`.
 */
export interface CapabilityDeclaration {
  supported?: boolean | "limited";
  description?: string;
  restrictedConfigurations?: string[];
}

/** One `contributes.mcpServerDefinitionProviders` entry. */
export interface McpServerDefinitionProviderContribution {
  id: string;
  label: string;
}

/** The parsed `package.json` of the extension, typed only as far as the tests read it. */
export interface Manifest {
  activationEvents?: string[];
  categories?: string[];
  version?: string;
  engines?: Record<string, string>;
  icon?: string;
  main?: string;
  capabilities?: {
    untrustedWorkspaces?: CapabilityDeclaration;
    virtualWorkspaces?: boolean | CapabilityDeclaration;
  };
  devDependencies?: Record<string, string>;
  contributes?: {
    yamlValidation?: unknown;
    commands?: CommandContribution[];
    walkthroughs?: Walkthrough[];
    languages?: LanguageContribution[];
    configurationDefaults?: Record<string, unknown>;
    configuration?: unknown;
    mcpServerDefinitionProviders?: McpServerDefinitionProviderContribution[];
    views?: Record<string, Array<{ id: string; name: string }>>;
    viewsContainers?: Record<string, Array<{ id: string; title: string; icon: string }>>;
  };
  extensionPack?: string[];
  extensionDependencies?: string[];
}

export function readManifest(): Manifest {
  return JSON.parse(fs.readFileSync(path.join(PROJECT_ROOT, "package.json"), "utf8")) as Manifest;
}

/** The `*.schema.json` basenames under `schemas/`, the files `contributes.yamlValidation` points at. */
export function schemaFiles(): string[] {
  return fs.readdirSync(SCHEMAS_DIR).filter((name) => name.endsWith(".schema.json"));
}

/** The walkthroughs `package.json` declares; empty when it declares none. */
export function walkthroughs(): Walkthrough[] {
  return readManifest().contributes?.walkthroughs ?? [];
}

/**
 * Every walkthrough step's `media.markdown`, as extension-relative POSIX paths
 * (`media/walkthrough/01-panel.md`). A step without markdown media contributes
 * nothing, so `walkthrough.test.ts` asserts the list's length against the step
 * count.
 */
export function walkthroughMediaPaths(): string[] {
  return walkthroughs().flatMap((walkthrough) =>
    walkthrough.steps
      .map((step) => step.media?.markdown)
      .filter((markdown): markdown is string => typeof markdown === "string"),
  );
}

/**
 * Every file under `media/` the manifest points at: the extension icon, the
 * Activity Bar container's icon and every walkthrough step's Markdown, as
 * extension-relative POSIX paths. Derived from the manifest, not the directory,
 * so a declared file that stops shipping fails and a stray one does not. The
 * extension icon is listed because nothing at runtime would notice it missing.
 */
export function declaredMediaPaths(): string[] {
  const manifest = readManifest();
  const containers = manifest.contributes?.viewsContainers ?? {};
  const containerIcons = Object.values(containers).flatMap((container) =>
    container.map((entry) => entry.icon),
  );
  const extensionIcon = typeof manifest.icon === "string" ? [manifest.icon] : [];
  return [...extensionIcon, ...containerIcons, ...walkthroughMediaPaths()].filter((mediaPath) =>
    mediaPath.startsWith("media/"),
  );
}

/**
 * The text of a source file, addressed by its POSIX path relative to the
 * extension root (`src/commands.ts`). A guard reading text proves what the
 * source says, never what it does (the `vscode` adapters cannot load under
 * `node --test`).
 */
export function readSource(relativePosixPath: string): string {
  return fs.readFileSync(path.join(PROJECT_ROOT, ...relativePosixPath.split("/")), "utf8");
}

/**
 * The text of the `swcraft_tools.cli` package: every module, in POSIX path order, joined, so a
 * text that moves between modules stays found. Asserts more than one module is read.
 */
export function readCliPackageSource(): string {
  const directory = path.join(
    PROJECT_ROOT,
    "..",
    "packages",
    "tools",
    "src",
    "swcraft_tools",
    "cli",
  );
  const modules = fs
    .readdirSync(directory, { encoding: "utf8", recursive: true })
    .filter((name) => name.endsWith(".py"))
    .map((name) => name.split(path.sep).join("/"))
    .sort();
  assert.ok(
    modules.length > 1,
    `the cli package has ${modules.length} module(s); the read has gone blind`,
  );
  return modules
    .map((name) => fs.readFileSync(path.join(directory, ...name.split("/")), "utf8"))
    .join("\n");
}

/**
 * Every production module under `src/`, as basenames, `*.test.ts` excluded.
 * Does not reach `src/test/` or `scripts/`.
 *
 * Asserts `PRODUCTION_MODULE_FLOOR`: every caller asserts an absence, which a
 * short list would satisfy vacuously. Never lower the floor to make a run pass.
 */
export function productionModules(): string[] {
  const modules = fs
    .readdirSync(path.join(PROJECT_ROOT, "src"))
    .filter((name) => name.endsWith(".ts") && !name.endsWith(".test.ts"))
    .sort();
  assert.ok(
    modules.length >= PRODUCTION_MODULE_FLOOR,
    `only ${modules.length} production modules found under src/ (${modules.join(", ")}); the guards ` +
      "that scan every module assert an absence and would pass vacuously on a short list",
  );
  return modules;
}

/**
 * *source* with its comment lines removed: every line whose first non-space
 * character starts `//`, `/*` or `*`, so a scan for a forbidden spelling reads
 * code, not explanation.
 *
 * What it does not reach: a block comment opened after code, a trailing `//`
 * after code, a line inside a block comment that does not start with `*` (kept
 * as code), a code line whose first character is `*` (a continued
 * multiplication, `yield*`; dropped), and a line inside a string literal that
 * starts like a comment. It is a line filter, not a lexer.
 */
export function withoutCommentLines(source: string): string {
  return source
    .split("\n")
    .filter((line) => !/^\s*(\/\/|\/\*|\*)/.test(line))
    .join("\n");
}

/**
 * The module specifiers *source* names, read from its syntax tree: every
 * `import` and `export ... from` declaration, `import x = require()`, and every
 * `import()` or `require()` call whose first argument is a string literal.
 * Comments and string literals are never read as imports.
 *
 * What it does not reach: a computed specifier (a non-literal argument), and a
 * `require` or `import()` called through another name.
 */
export function importedModules(source: string): string[] {
  const file = ts.createSourceFile("source.ts", source, ts.ScriptTarget.Latest, false);
  const modules: string[] = [];
  const visit = (node: ts.Node): void => {
    if (
      (ts.isImportDeclaration(node) || ts.isExportDeclaration(node)) &&
      node.moduleSpecifier !== undefined &&
      ts.isStringLiteral(node.moduleSpecifier)
    ) {
      modules.push(node.moduleSpecifier.text);
    } else if (ts.isExternalModuleReference(node) && ts.isStringLiteral(node.expression)) {
      modules.push(node.expression.text);
    } else if (
      ts.isCallExpression(node) &&
      (node.expression.kind === ts.SyntaxKind.ImportKeyword ||
        (ts.isIdentifier(node.expression) && node.expression.text === "require")) &&
      node.arguments.length > 0 &&
      ts.isStringLiteralLike(node.arguments[0])
    ) {
      modules.push(node.arguments[0].text);
    }
    ts.forEachChild(node, visit);
  };
  visit(file);
  return modules;
}

/**
 * One function declaration's body, sliced out of *source* by text.
 *
 * Asserts *declaration* is found: a missed `indexOf` would slice the file's last
 * character, on which every absence assertion passes. *declaration* is matched
 * verbatim (`async function runToolchainCommand(`); the body ends at the next
 * `function `, `async function `, `export ` or `class ` at column zero, or at
 * the end of the file.
 *
 * What it does not reach: it is a text slice, not a parser. A column-zero
 * declaration inside a template literal, an arrow function assigned to a
 * `const` (no terminator matches it), and a declaration named inside a string
 * read as what they look like.
 */
export function functionBody(source: string, declaration: string): string {
  const at = source.indexOf(declaration);
  assert.notEqual(
    at,
    -1,
    `the source declares no \`${declaration}\` — this guard reads a function body and would ` +
      "pass vacuously on an empty slice. Rename it here too, or say why it is gone.",
  );
  const rest = source.slice(at);
  const ends = ["\nasync function ", "\nfunction ", "\nexport ", "\nclass "]
    .map((terminator) => rest.indexOf(terminator, 1))
    .filter((index) => index !== -1);
  return ends.length === 0 ? rest : rest.slice(0, Math.min(...ends));
}

/**
 * The text of *source* between two verbatim markers. Both markers and their
 * order are asserted, since an empty slice satisfies every absence assertion.
 *
 * What it does not reach: it takes the first occurrence of each marker, so a
 * marker that also appears earlier bounds the wrong region.
 */
export function sourceRegion(source: string, from: string, to: string): string {
  const start = source.indexOf(from);
  assert.notEqual(start, -1, `the source does not contain \`${from}\``);
  const end = source.indexOf(to);
  assert.notEqual(end, -1, `the source does not contain \`${to}\``);
  assert.ok(start < end, `\`${from}\` must precede \`${to}\`; the slice between them is empty`);
  return source.slice(start, end);
}

/** An `exists` predicate that records every candidate it was asked about. */
export function spy(answer: (candidate: string) => boolean): {
  exists: (candidate: string) => boolean;
  probed: string[];
} {
  const probed: string[] = [];
  return {
    exists: (candidate: string) => {
      probed.push(candidate);
      return answer(candidate);
    },
    probed,
  };
}

/**
 * Assert that no production module under `src/` names any of *spellings* in its
 * code (comment lines removed). *why* is appended to the failure.
 *
 * What it does not reach: what a running extension host does, and the limits
 * of {@link withoutCommentLines}' line filter.
 */
export function assertNoProductionModuleNames(spellings: readonly string[], why: string): void {
  for (const module of productionModules()) {
    const source = withoutCommentLines(readSource(path.posix.join("src", module)));
    for (const spelling of spellings) {
      assert.equal(source.includes(spelling), false, `${module} names ${spelling} — ${why}`);
    }
  }
}

/**
 * The text of a document of `src/fixtures/impact/`: the stdout of `swcraft impact
 * --format json -- <name>` on `examples/basic` with no `--depth`, or shaped from
 * one. Re-recorded by `packages/tools/tests/test_impact_panel_fixtures.py`, whose
 * tests hold every file equal to a fresh run.
 */
export function impactFixture(name: string): string {
  return fs.readFileSync(path.join(PROJECT_ROOT, "src", "fixtures", "impact", name), "utf8");
}

/** `examples/basic`'s model root, read from disk: the files the recorded documents name. */
export const BASIC_MODEL = path.join(PROJECT_ROOT, "..", "examples", "basic", "model");

/** A reader of the disk as it is: a file or directory that cannot be read is `undefined`. */
export const diskReader: Reader = {
  readText: (filePath) => {
    try {
      return fs.readFileSync(filePath, "utf8");
    } catch {
      return undefined;
    }
  },
  listDir: (directory) => {
    try {
      return fs.readdirSync(directory);
    } catch {
      return undefined;
    }
  },
};

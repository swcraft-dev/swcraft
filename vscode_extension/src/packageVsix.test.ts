import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { test } from "node:test";
import { pathToFileURL } from "node:url";

import { listFiles, PackageManager } from "@vscode/vsce";

import { declaredMediaPaths, schemaFiles } from "./testSupport";

function mkWorkspace(): string {
  return fs.mkdtempSync(path.join(os.tmpdir(), "swcraft-vscode-packageVsix-test-"));
}

function cleanup(dir: string): void {
  fs.rmSync(dir, { recursive: true, force: true });
}

// tsc emits src/**/*.ts -> out/ (rootDir: src), so this compiled test lives at
// out/packageVsix.test.js; PROJECT_ROOT (vscode_extension/) is one level up.
const PROJECT_ROOT = path.join(__dirname, "..");
const REPO_ROOT = path.join(PROJECT_ROOT, "..");

// package-vsix.mjs is an ESM module, so it cannot be `require()`d from this CommonJS-compiled
// test, and with "module": "commonjs" tsc downlevels a plain `import()` to `require()` too.
// Routing the call through `new Function` hides it from tsc, so the emitted JS keeps a genuine
// dynamic `import()`.
interface PackageVsixScript {
  checkPackagePreconditions: (root: string) => void;
  buildBundle: (root: string) => void;
  stageLicenceFiles: (root: string, repoRoot?: string) => void;
  LICENCE_FILES: string[];
}

const dynamicImport = new Function("specifier", "return import(specifier)") as (
  specifier: string,
) => Promise<PackageVsixScript>;

async function loadPackageVsixScript(): Promise<PackageVsixScript> {
  const url = pathToFileURL(path.join(PROJECT_ROOT, "scripts", "package-vsix.mjs")).href;
  return dynamicImport(url);
}

// --- checkPackagePreconditions --------------------------------------------

test("checkPackagePreconditions: throws when dist/extension.js is missing", async () => {
  const dir = mkWorkspace();
  try {
    const { checkPackagePreconditions } = await loadPackageVsixScript();
    assert.throws(() => checkPackagePreconditions(dir), /dist\/extension\.js/);
  } finally {
    cleanup(dir);
  }
});

test("checkPackagePreconditions: proceeds (does not throw) when dist/extension.js exists", async () => {
  const dir = mkWorkspace();
  try {
    fs.mkdirSync(path.join(dir, "dist"), { recursive: true });
    fs.writeFileSync(path.join(dir, "dist", "extension.js"), "// stub bundle\n");
    const { checkPackagePreconditions } = await loadPackageVsixScript();
    assert.doesNotThrow(() => checkPackagePreconditions(dir));
  } finally {
    cleanup(dir);
  }
});

// --- stageLicenceFiles: every .vsix carries the root licence and notice -----
//
// The extension declares Apache-2.0 and vsce takes the project's LICENSE as its licence; the root
// files are the one source, copied in before vsce runs.

test("stageLicenceFiles: copies LICENSE and NOTICE from the repository root, byte for byte", async () => {
  const dir = mkWorkspace();
  try {
    const project = path.join(dir, "vscode_extension");
    fs.mkdirSync(project);
    fs.writeFileSync(path.join(dir, "LICENSE"), "licence text\n");
    fs.writeFileSync(path.join(dir, "NOTICE"), "notice text\n");
    fs.writeFileSync(path.join(project, "LICENSE"), "stale\n");
    const { stageLicenceFiles, LICENCE_FILES } = await loadPackageVsixScript();
    assert.deepEqual(LICENCE_FILES, ["LICENSE", "NOTICE"]);
    stageLicenceFiles(project);
    for (const name of LICENCE_FILES) {
      assert.ok(
        fs.readFileSync(path.join(project, name)).equals(fs.readFileSync(path.join(dir, name))),
        `${name} is not the root file's bytes`,
      );
    }
  } finally {
    cleanup(dir);
  }
});

test("stageLicenceFiles: a missing root file throws by name and copies nothing", async () => {
  const dir = mkWorkspace();
  try {
    const project = path.join(dir, "vscode_extension");
    fs.mkdirSync(project);
    fs.writeFileSync(path.join(dir, "LICENSE"), "licence text\n");
    const { stageLicenceFiles } = await loadPackageVsixScript();
    assert.throws(() => stageLicenceFiles(project), /NOTICE not found at the repository root/);
    assert.equal(fs.existsSync(path.join(project, "LICENSE")), false);
  } finally {
    cleanup(dir);
  }
});

test("the extension declares the Apache-2.0 licence", () => {
  const manifest = JSON.parse(fs.readFileSync(path.join(PROJECT_ROOT, "package.json"), "utf8"));
  assert.equal(manifest.license, "Apache-2.0");
});

// --- buildBundle: packaging builds first -----------------------------------
//
// Without the build step `npm run package` fails on a clean checkout, and after a source edit
// fails with the staleness error instead of rebuilding. Pinned against a synthetic `esbuild.mjs`
// rather than the real (slow) bundle.

test("buildBundle: runs the project's esbuild.mjs in the given root", async () => {
  const dir = mkWorkspace();
  try {
    fs.writeFileSync(
      path.join(dir, "esbuild.mjs"),
      "import { mkdirSync, writeFileSync } from 'node:fs';\n" +
        "mkdirSync('dist', { recursive: true });\n" +
        "writeFileSync('dist/extension.js', '// built\\n');\n",
      "utf8",
    );
    const { buildBundle, checkPackagePreconditions } = await loadPackageVsixScript();

    buildBundle(dir);

    assert.ok(fs.existsSync(path.join(dir, "dist", "extension.js")));
    // ...and the freshness precondition is satisfied by that build: packaging cannot fail on a
    // bundle it was able to make.
    assert.doesNotThrow(() => checkPackagePreconditions(dir));
  } finally {
    cleanup(dir);
  }
});

test("buildBundle: a failing build aborts packaging with an actionable error", async () => {
  const dir = mkWorkspace();
  try {
    fs.writeFileSync(path.join(dir, "esbuild.mjs"), "process.exit(3);\n", "utf8");
    const { buildBundle } = await loadPackageVsixScript();

    assert.throws(() => buildBundle(dir), /esbuild\.mjs failed with exit code 3/);
  } finally {
    cleanup(dir);
  }
});

// --- fixtures/agents/* sync guard ------------------------------------------
//
// vscode_extension/fixtures/agents/ holds a bundled copy of the static onboarding files (the agent
// profile, the Copilot instructions, the model guard's two hooks, the files Codex and Claude Code
// read, and `clients.json`), so the extension ships them without depending on the repository
// layout. Their source is the `vehicle-architecture` template's -- what `swcraft init` writes --
// never the repository's own `.github/` pair, which is written for developing SWCraft. The same
// byte-equality is pinned from Python (packages/mcp/tests/test_vscode_agent_profile.py,
// TestBundledExtensionCopies) so `dev.py test` catches drift without `npm`; both guards move
// together.

const FIXTURES_AGENTS_DIR = path.join(PROJECT_ROOT, "fixtures", "agents");
const TEMPLATE_GITHUB_DIR = path.join(
  REPO_ROOT,
  "packages",
  "tools",
  "src",
  "swcraft_tools",
  "templates",
  "vehicle-architecture",
  ".github",
);
const TEMPLATE_AGENTS_DIR = path.join(TEMPLATE_GITHUB_DIR, "agents");
const TEMPLATE_ROOT = path.dirname(TEMPLATE_GITHUB_DIR);
const TEMPLATE_CODEX_HOOK = path.join(TEMPLATE_ROOT, ".codex", "hooks.json");

/**
 * The five client files and Kiro's three: each bundled asset and the template file it copies byte
 * for byte, the template's path POSIX-spelled from its root. `AGENTS.md` and Kiro's steering file
 * are not copied by hand: `dev.py schemas` and `dev.py docs-reference` render both copies of each.
 */
const CLIENT_FILE_ASSETS: readonly (readonly [string, string])[] = [
  ["swcraft.mcp.json", ".mcp.json"],
  ["swcraft.claude-settings.json", ".claude/settings.json"],
  ["swcraft.codex-config.toml", ".codex/config.toml"],
  ["AGENTS.md", "AGENTS.md"],
  ["swcraft.claude.md", "CLAUDE.md"],
  ["swcraft.kiro-mcp.json", ".kiro/settings/mcp.json"],
  ["swcraft.kiro-steering.md", ".kiro/steering/swcraft.md"],
  ["swcraft.kiro-hook.json", ".kiro/hooks/swcraft-guard.json"],
];

/** The assets the two `dev.py` commands render rather than a hand copy. */
const RENDERED_ASSETS: ReadonlySet<string> = new Set(["AGENTS.md", "swcraft.kiro-steering.md"]);

/**
 * The client table: `swcraft init --client` reads
 * `packages/tools/src/swcraft_tools/templates/clients.json`, *Onboard Project* reads this byte copy
 * among its assets — one table, never a second one in code.
 */
const CLIENT_TABLE_ASSET = "clients.json";
const TEMPLATE_CLIENT_TABLE = path.join(path.dirname(TEMPLATE_ROOT), "clients.json");

function templateFile(relative: string): string {
  return path.join(TEMPLATE_ROOT, ...relative.split("/"));
}

test("fixtures/agents/swcraft-architect.agent.md stays byte-identical to the template's .github/agents/swcraft-architect.agent.md", () => {
  const bundled = fs.readFileSync(path.join(FIXTURES_AGENTS_DIR, "swcraft-architect.agent.md"));
  const source = fs.readFileSync(path.join(TEMPLATE_AGENTS_DIR, "swcraft-architect.agent.md"));
  assert.ok(
    bundled.equals(source),
    "vscode_extension/fixtures/agents/swcraft-architect.agent.md has drifted from " +
      "the template's .github/agents/swcraft-architect.agent.md -- re-copy the file",
  );
});

test("fixtures/agents/copilot-instructions.md stays byte-identical to the template's .github/copilot-instructions.md", () => {
  const bundled = fs.readFileSync(path.join(FIXTURES_AGENTS_DIR, "copilot-instructions.md"));
  const source = fs.readFileSync(path.join(TEMPLATE_GITHUB_DIR, "copilot-instructions.md"));
  assert.ok(
    bundled.equals(source),
    "vscode_extension/fixtures/agents/copilot-instructions.md has drifted from " +
      "the template's .github/copilot-instructions.md -- re-copy the file",
  );
});

test("fixtures/agents/swcraft.hooks.json stays byte-identical to the template's .github/hooks/swcraft.json", () => {
  const bundled = fs.readFileSync(path.join(FIXTURES_AGENTS_DIR, "swcraft.hooks.json"));
  const source = fs.readFileSync(path.join(TEMPLATE_GITHUB_DIR, "hooks", "swcraft.json"));
  assert.ok(
    bundled.equals(source),
    "vscode_extension/fixtures/agents/swcraft.hooks.json has drifted from " +
      "the template's .github/hooks/swcraft.json -- re-copy the file",
  );
});

test("fixtures/agents/swcraft.codex-hooks.json stays byte-identical to the template's .codex/hooks.json", () => {
  const bundled = fs.readFileSync(path.join(FIXTURES_AGENTS_DIR, "swcraft.codex-hooks.json"));
  const source = fs.readFileSync(TEMPLATE_CODEX_HOOK);
  assert.ok(
    bundled.equals(source),
    "vscode_extension/fixtures/agents/swcraft.codex-hooks.json has drifted from " +
      "the template's .codex/hooks.json -- re-copy the file",
  );
});

for (const [assetName, relative] of CLIENT_FILE_ASSETS) {
  test(`fixtures/agents/${assetName} stays byte-identical to the template's ${relative}`, () => {
    const bundled = fs.readFileSync(path.join(FIXTURES_AGENTS_DIR, assetName));
    const source = fs.readFileSync(templateFile(relative));
    const remedy = RENDERED_ASSETS.has(assetName)
      ? "run `python scripts/dev.py schemas` or `python scripts/dev.py docs-reference`"
      : "re-copy the file";
    assert.ok(
      bundled.equals(source),
      `vscode_extension/fixtures/agents/${assetName} has drifted from the template's ${relative} -- ${remedy}`,
    );
  });
}

test("fixtures/agents/clients.json stays byte-identical to the template package's clients.json", () => {
  const bundled = fs.readFileSync(path.join(FIXTURES_AGENTS_DIR, CLIENT_TABLE_ASSET));
  assert.ok(
    bundled.equals(fs.readFileSync(TEMPLATE_CLIENT_TABLE)),
    "vscode_extension/fixtures/agents/clients.json has drifted from " +
      "packages/tools/src/swcraft_tools/templates/clients.json -- re-copy the file",
  );
});

test("fixtures/agents/ holds exactly the thirteen onboarding assets", () => {
  // esbuild.mjs copies every file in the directory, so a stray file would ship.
  assert.deepEqual(fs.readdirSync(FIXTURES_AGENTS_DIR).sort(), [
    "AGENTS.md",
    "clients.json",
    "copilot-instructions.md",
    "swcraft-architect.agent.md",
    "swcraft.claude-settings.json",
    "swcraft.claude.md",
    "swcraft.codex-config.toml",
    "swcraft.codex-hooks.json",
    "swcraft.hooks.json",
    "swcraft.kiro-hook.json",
    "swcraft.kiro-mcp.json",
    "swcraft.kiro-steering.md",
    "swcraft.mcp.json",
  ]);
});

test("no bundled asset is named CLAUDE.md, which a Claude Code session in this repository would load", () => {
  // A subfolder's CLAUDE.md is loaded when a session reads files there, and the
  // project's imports the architect profile (`@AGENTS.md`); the repository's
  // `claudeMdExcludes` names the template's copy alone.
  for (const name of fs.readdirSync(FIXTURES_AGENTS_DIR)) {
    assert.notEqual(name.toLowerCase(), "claude.md", `fixtures/agents/${name}`);
  }
});

// --- the bundled profile teaches the surface the server actually has -------
//
// The `.vsix` writes these files into a user's workspace as the instructions an agent follows.
// Byte-equality alone would ship a profile naming a removed tool as long as both copies said it.
// The removed names are the `replaced` set of `tool-projections.json`, so this cannot fall behind
// the contract; matching is on identifier boundaries, so the surviving generic `read` does not make
// a removed `read_*` name look present.

interface ToolProjections {
  projections: { replaced: string[] };
}

function removedToolNames(): string[] {
  const projections = JSON.parse(
    fs.readFileSync(
      path.join(
        REPO_ROOT,
        "packages",
        "tools",
        "tests",
        "fixtures",
        "contracts",
        "editing-protocol",
        "tool-projections.json",
      ),
      "utf8",
    ),
  ) as ToolProjections;
  return projections.projections.replaced;
}

test("the bundled onboarding assets name no removed tool", () => {
  const removed = removedToolNames();
  assert.equal(
    removed.length,
    29,
    "the decision table stopped listing the removals this guard needs",
  );
  for (const name of ["swcraft-architect.agent.md", "copilot-instructions.md"]) {
    const text = fs.readFileSync(path.join(FIXTURES_AGENTS_DIR, name), "utf8");
    const named = removed.filter((tool) => new RegExp(`(?<!\\w)${tool}(?!\\w)`).test(text));
    assert.deepEqual(
      named,
      [],
      `fixtures/agents/${name} still names removed tools: ${named.join(", ")}`,
    );
  }
});

// --- packaged content includes the onboarding assets -----------------------

test("packaged .vsix content includes dist/onboarding-assets/* (regression guard for the assets' copy step)", async (t) => {
  // A real build, not a faked dist/: the point is to catch a regression in esbuild.mjs's copy
  // step, or a .vscodeignore edit that excludes dist/**, before it ships.
  execFileSync(process.execPath, [path.join(PROJECT_ROOT, "esbuild.mjs")], {
    stdio: "inherit",
    cwd: PROJECT_ROOT,
  });

  // The package script stages the root licence and notice before vsce runs; so does this case, so
  // the listing shows what a real `npm run package` ships. The copies are removed afterwards, so a
  // tree that ran the tests cannot hide a dropped staging step from a later check.
  const { stageLicenceFiles, LICENCE_FILES } = await loadPackageVsixScript();
  const made = LICENCE_FILES.filter((name) => !fs.existsSync(path.join(PROJECT_ROOT, name)));
  t.after(() => {
    for (const name of made) fs.rmSync(path.join(PROJECT_ROOT, name), { force: true });
  });
  stageLicenceFiles(PROJECT_ROOT);

  // packageManager: None skips dependency resolution: the extension has no runtime dependencies
  // to bundle, and the check stays fast and offline.
  const files = await listFiles({
    cwd: PROJECT_ROOT,
    packageManager: PackageManager.None,
  });

  const expected = [
    "dist/onboarding-assets/swcraft-architect.agent.md",
    "dist/onboarding-assets/copilot-instructions.md",
    "dist/onboarding-assets/swcraft.hooks.json",
    "dist/onboarding-assets/swcraft.codex-hooks.json",
    ...CLIENT_FILE_ASSETS.map(([assetName]) => `dist/onboarding-assets/${assetName}`),
    `dist/onboarding-assets/${CLIENT_TABLE_ASSET}`,
    // vsce packages LICENSE as `extension/LICENSE.txt` and NOTICE as is.
    "LICENSE",
    "NOTICE",
  ];
  for (const expectedFile of expected) {
    assert.ok(
      files.includes(expectedFile),
      `expected "${expectedFile}" in packaged .vsix content, got: ${files.join(", ")}`,
    );
  }
  // The hook that ships is the template's, byte for byte: what Onboard Project
  // writes into a project is what `swcraft init` writes.
  assert.ok(
    fs
      .readFileSync(path.join(PROJECT_ROOT, "dist", "onboarding-assets", "swcraft.hooks.json"))
      .equals(fs.readFileSync(path.join(TEMPLATE_GITHUB_DIR, "hooks", "swcraft.json"))),
    "dist/onboarding-assets/swcraft.hooks.json is not the template's .github/hooks/swcraft.json",
  );
  assert.ok(
    fs
      .readFileSync(
        path.join(PROJECT_ROOT, "dist", "onboarding-assets", "swcraft.codex-hooks.json"),
      )
      .equals(fs.readFileSync(TEMPLATE_CODEX_HOOK)),
    "dist/onboarding-assets/swcraft.codex-hooks.json is not the template's .codex/hooks.json",
  );
  assert.ok(
    fs
      .readFileSync(path.join(PROJECT_ROOT, "dist", "onboarding-assets", CLIENT_TABLE_ASSET))
      .equals(fs.readFileSync(TEMPLATE_CLIENT_TABLE)),
    "dist/onboarding-assets/clients.json is not packages/tools/src/swcraft_tools/templates/clients.json",
  );
  for (const [assetName, relative] of CLIENT_FILE_ASSETS) {
    assert.ok(
      fs
        .readFileSync(path.join(PROJECT_ROOT, "dist", "onboarding-assets", assetName))
        .equals(fs.readFileSync(templateFile(relative))),
      `dist/onboarding-assets/${assetName} is not the template's ${relative}`,
    );
  }

  // The editor schemas ship verbatim (not bundled), so a .vscodeignore edit excluding schemas/**
  // would silently break every contributes.yamlValidation url.
  const shippedSchemas = schemaFiles();
  assert.ok(shippedSchemas.length > 0, "schemas/ holds no *.schema.json");
  for (const schemaFile of shippedSchemas) {
    const expectedPath = `schemas/${schemaFile}`;
    assert.ok(
      files.includes(expectedPath),
      `expected "${expectedPath}" in packaged .vsix content, got: ${files.join(", ")}`,
    );
  }
  // The reference table navigation reads at activation ships beside the schemas; `schemaFiles()`
  // sees `*.schema.json` only, so it is named here. Without it the providers are off.
  assert.ok(
    files.includes("schemas/references.json"),
    `expected "schemas/references.json" in packaged .vsix content, got: ${files.join(", ")}`,
  );

  // The same guard for media/**: a .vscodeignore edit excluding it would blank every walkthrough
  // step, the Activity Bar icon and the gallery icon with nothing failing anywhere. The paths come
  // from the manifest (`declaredMediaPaths()`), so this asserts what the extension declares it
  // needs rather than what sits in the directory.
  const declaredMedia = declaredMediaPaths();
  assert.ok(
    declaredMedia.length > 0,
    "package.json declares no media/ paths -- the walkthrough and the Activity Bar icon both live there",
  );
  for (const mediaPath of declaredMedia) {
    assert.ok(
      files.includes(mediaPath),
      `expected "${mediaPath}" in packaged .vsix content, got: ${files.join(", ")}`,
    );
  }
});

// --- direct-execution guard --------------------------------------------------
//
// A raw `file://${process.argv[1]}` template does not URL-encode a Windows path the way
// `import.meta.url` is encoded, so a direct-execution check built on it is silently false on
// Windows and `node scripts/package-vsix.mjs` would exit 0 without packaging; the script compares
// against `pathToFileURL(process.argv[1]).href`. This test cannot reproduce a Windows path, but it
// catches a guard that is permanently false on the platform running it.

test("node scripts/package-vsix.mjs (run directly) packages the extension via the self-execution guard", async (t) => {
  // The real package step stages the root licence files; the copies it makes are removed after.
  const { LICENCE_FILES } = await loadPackageVsixScript();
  const made = LICENCE_FILES.filter((name) => !fs.existsSync(path.join(PROJECT_ROOT, name)));
  t.after(() => {
    for (const name of made) fs.rmSync(path.join(PROJECT_ROOT, name), { force: true });
  });
  // Build first so dist/extension.js exists (checkPackagePreconditions() requires it).
  execFileSync(process.execPath, [path.join(PROJECT_ROOT, "esbuild.mjs")], {
    stdio: "inherit",
    cwd: PROJECT_ROOT,
  });

  const staleVsixFiles = fs.readdirSync(PROJECT_ROOT).filter((name) => name.endsWith(".vsix"));
  for (const stale of staleVsixFiles) {
    fs.rmSync(path.join(PROJECT_ROOT, stale));
  }

  execFileSync(process.execPath, [path.join(PROJECT_ROOT, "scripts", "package-vsix.mjs")], {
    stdio: "inherit",
    cwd: PROJECT_ROOT,
  });

  const produced = fs.readdirSync(PROJECT_ROOT).filter((name) => name.endsWith(".vsix"));
  try {
    assert.ok(
      produced.length > 0,
      "expected `node scripts/package-vsix.mjs` (executed directly) to produce a .vsix file " +
        "-- the self-execution guard did not fire",
    );
  } finally {
    for (const name of produced) {
      fs.rmSync(path.join(PROJECT_ROOT, name));
    }
  }
});

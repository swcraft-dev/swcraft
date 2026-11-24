// Unit tests for what `SWCraft: Onboard Project` writes into a target folder: asset copies and
// nothing else (the profile, the instructions, the Copilot `PreToolUse` hook
// `.github/hooks/swcraft.json`, Codex's `.codex/hooks.json` and the chosen clients' files). No
// `.vscode/mcp.json` is generated: the extension publishes the MCP server itself
// (`mcpProvider.ts`, tested by `mcpProvider.test.ts`).

import assert from "node:assert/strict";
import nodeFs, * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { type TestContext, test } from "node:test";
import { clientFilesFor, loadClientTable } from "./agentClients";
import { AGENT_PROFILE_RELATIVE_PATH } from "./healthCheck";
import {
  ADD_AGENT_PROFILE_CHOICE,
  AGENT_ASSET_NAME,
  AGENT_PROFILE_NOTICE,
  AGENT_PROFILE_NOTICE_DISMISSED_KEY,
  AGENTS_MD_ASSET_NAME,
  agentProfileNoticeAction,
  applyAgentProfilePlan,
  applyCompanionSettings,
  applyOnboardingPlan,
  CLAUDE_MD_ASSET_NAME,
  CLAUDE_SETTINGS_ASSET_NAME,
  CLIENT_FILES_GUIDE_SECTION,
  CODEX_CONFIG_ASSET_NAME,
  CODEX_HOOK_ASSET_NAME,
  CONTAINED_PROJECT_MAX_DEPTH,
  CONTAINED_PROJECT_MAX_ENTRIES,
  CONTAINED_PROJECT_SKIPPED_DIRS,
  codexHookOutsideGitMessage,
  codexHookPath,
  containedProject,
  containedProjectMessage,
  DISMISS_AGENT_PROFILE_CHOICE,
  enclosingProject,
  GUIDE_SECTION_TITLES,
  HOOK_ASSET_NAME,
  hasOnboardMarker,
  hookPath,
  INSTRUCTIONS_ASSET_NAME,
  isOwnFile,
  isSwcraftClaudeSettings,
  isSwcraftCodexHook,
  isSwcraftKiroHook,
  isSwcraftKiroMcpJson,
  isSwcraftMcpJson,
  KIRO_HOOK_ASSET_NAME,
  KIRO_HOOK_RELATIVE_PATH,
  KIRO_MCP_JSON_ASSET_NAME,
  KIRO_MCP_JSON_RELATIVE_PATH,
  KIRO_STEERING_ASSET_NAME,
  KIRO_STEERING_RELATIVE_PATH,
  keptCodexHookMessage,
  keptCompanionMessage,
  keptMessage,
  MARKDOWN_MARKER_LINE,
  MCP_JSON_ASSET_NAME,
  nestedProjectMessage,
  type OnboardingPlan,
  type OnboardingReport,
  onboardedMessages,
  onboardingBoundary,
  onboardingRefusal,
  planAgentProfileNotice,
  planOnboarding,
  shouldOfferAgentProfile,
  TOML_MARKER_LINE,
  writtenFiles,
} from "./onboarding";
import { MANIFEST_FILENAME } from "./project";
import { functionBody, PROJECT_ROOT, readSource } from "./testSupport";

function mkWorkspace(): string {
  return fs.mkdtempSync(path.join(os.tmpdir(), "swcraft-vscode-onboarding-test-"));
}

function cleanup(dir: string): void {
  fs.rmSync(dir, { recursive: true, force: true });
}

// tsc emits src/**/*.ts -> out/ (rootDir: src), so this compiled test lives at
// out/onboarding.test.js; fixtures/ is a sibling of out/, hence a single "..".
// fixtures/agents/ is the bundled source the extension ships under dist/onboarding-assets/
// (esbuild.mjs's copy step), read directly: there is no `vscode` context to resolve an install
// path from.
const FIXTURES_AGENTS_DIR = path.join(__dirname, "..", "fixtures", "agents");

/** The bundled client table, as the extension reads it at run time. */
const TABLE = loadClientTable(FIXTURES_AGENTS_DIR);
/** Every client's files: the plan with every client chosen. */
const EVERY_CLIENT_FILE = clientFilesFor(
  TABLE,
  TABLE.clients.map((client) => client.name),
);

// --- planOnboarding: pure, no filesystem access ----------------------------

test("planOnboarding: does not touch the filesystem (targetDir need not exist)", () => {
  const targetDir = path.join(
    os.tmpdir(),
    `swcraft-vscode-onboarding-does-not-exist-${Date.now()}`,
  );
  assert.equal(fs.existsSync(targetDir), false);

  planOnboarding(targetDir, EVERY_CLIENT_FILE);

  assert.equal(
    fs.existsSync(targetDir),
    false,
    "planOnboarding must not create targetDir or any files in it",
  );
});

test("planOnboarding: fileCopies lists the profile, the instructions, both hooks, the five client files and Kiro's three with correct destinations", () => {
  const plan = planOnboarding("/some/target", EVERY_CLIENT_FILE);

  assert.deepEqual(plan.fileCopies, [
    {
      assetName: AGENT_ASSET_NAME,
      destPath: path.join("/some/target", ".github", "agents", AGENT_ASSET_NAME),
      keepsForeign: "always-own",
    },
    {
      assetName: INSTRUCTIONS_ASSET_NAME,
      destPath: path.join("/some/target", ".github", INSTRUCTIONS_ASSET_NAME),
      keepsForeign: "always-own",
    },
    {
      assetName: HOOK_ASSET_NAME,
      destPath: path.join("/some/target", ".github", "hooks", "swcraft.json"),
      keepsForeign: "always-own",
    },
    {
      assetName: CODEX_HOOK_ASSET_NAME,
      destPath: path.join("/some/target", ".codex", "hooks.json"),
      keepsForeign: "codex-hook",
    },
    {
      assetName: MCP_JSON_ASSET_NAME,
      destPath: path.join("/some/target", ".mcp.json"),
      keepsForeign: "mcp-json",
    },
    {
      assetName: CLAUDE_SETTINGS_ASSET_NAME,
      destPath: path.join("/some/target", ".claude", "settings.json"),
      keepsForeign: "claude-settings",
    },
    {
      assetName: CODEX_CONFIG_ASSET_NAME,
      destPath: path.join("/some/target", ".codex", "config.toml"),
      keepsForeign: "toml-marker",
    },
    {
      assetName: AGENTS_MD_ASSET_NAME,
      destPath: path.join("/some/target", "AGENTS.md"),
      keepsForeign: "markdown-marker",
    },
    {
      assetName: CLAUDE_MD_ASSET_NAME,
      destPath: path.join("/some/target", "CLAUDE.md"),
      keepsForeign: "markdown-marker",
    },
    {
      assetName: KIRO_MCP_JSON_ASSET_NAME,
      destPath: path.join("/some/target", ".kiro", "settings", "mcp.json"),
      keepsForeign: "kiro-mcp-json",
    },
    {
      assetName: KIRO_STEERING_ASSET_NAME,
      destPath: path.join("/some/target", ".kiro", "steering", "swcraft.md"),
      keepsForeign: "markdown-marker",
    },
    {
      assetName: KIRO_HOOK_ASSET_NAME,
      destPath: path.join("/some/target", ".kiro", "hooks", "swcraft-guard.json"),
      keepsForeign: "kiro-hook",
    },
  ]);
});

test("planOnboarding: Kiro chosen alone plans its three files and nothing else", () => {
  const plan = planOnboarding("/some/target", clientFilesFor(TABLE, ["kiro"]));
  assert.deepEqual(
    plan.fileCopies.map((copy) => [copy.assetName, copy.keepsForeign]),
    [
      [KIRO_MCP_JSON_ASSET_NAME, "kiro-mcp-json"],
      [KIRO_STEERING_ASSET_NAME, "markdown-marker"],
      [KIRO_HOOK_ASSET_NAME, "kiro-hook"],
    ],
  );
});

test("the hook asset is the bundled swcraft.hooks.json and lands at .github/hooks/swcraft.json", () => {
  assert.equal(HOOK_ASSET_NAME, "swcraft.hooks.json");
  assert.equal(hookPath("/p"), path.join("/p", ".github", "hooks", "swcraft.json"));
  assert.ok(fs.statSync(path.join(FIXTURES_AGENTS_DIR, HOOK_ASSET_NAME)).isFile());
});

test("the Codex hook asset is the bundled swcraft.codex-hooks.json and lands at .codex/hooks.json", () => {
  assert.equal(CODEX_HOOK_ASSET_NAME, "swcraft.codex-hooks.json");
  assert.equal(codexHookPath("/p"), path.join("/p", ".codex", "hooks.json"));
  const hook = JSON.parse(
    fs.readFileSync(path.join(FIXTURES_AGENTS_DIR, CODEX_HOOK_ASSET_NAME), "utf8"),
  );
  assert.equal(hook.hooks.PreToolUse[0].hooks[0].command, "swcraft guard --host codex");
});

test("planOnboarding: the plan carries the copies and the target, and nothing about MCP", () => {
  // `planOnboarding` takes no `Platform` or `hasProjectVenv` flag: they only ever picked a
  // `.vscode/mcp.json` shape, so a plan that grew either back would mean the generated config had
  // returned.
  const plan = planOnboarding("/some/target", EVERY_CLIENT_FILE);
  assert.deepEqual(Object.keys(plan).sort(), ["fileCopies", "targetDir"]);
  assert.equal(plan.targetDir, "/some/target");
  assert.equal(
    planOnboarding.length,
    2,
    "planOnboarding takes the target directory and the chosen clients' files",
  );
});

// --- applyOnboardingPlan: actually copies files ----------------------------

test("applyOnboardingPlan: copies the agent profile and Copilot instructions from sourceAssetsDir", () => {
  const dir = mkWorkspace();
  try {
    const plan = planOnboarding(dir, EVERY_CLIENT_FILE);

    applyOnboardingPlan(plan, FIXTURES_AGENTS_DIR);

    for (const copy of plan.fileCopies) {
      const expected = fs.readFileSync(path.join(FIXTURES_AGENTS_DIR, copy.assetName));
      const actual = fs.readFileSync(copy.destPath);
      assert.ok(
        actual.equals(expected),
        `${copy.destPath} does not match the source asset ${copy.assetName}`,
      );
    }
  } finally {
    cleanup(dir);
  }
});

test("applyOnboardingPlan: creates .github/agents/ even when .github/ does not exist yet", () => {
  const dir = mkWorkspace();
  try {
    const plan: OnboardingPlan = planOnboarding(dir, EVERY_CLIENT_FILE);
    assert.equal(fs.existsSync(path.join(dir, ".github")), false);

    applyOnboardingPlan(plan, FIXTURES_AGENTS_DIR);

    assert.equal(fs.statSync(path.join(dir, ".github", "agents", AGENT_ASSET_NAME)).isFile(), true);
    assert.equal(fs.statSync(path.join(dir, ".github", INSTRUCTIONS_ASSET_NAME)).isFile(), true);
    assert.equal(fs.statSync(hookPath(dir)).isFile(), true);
    assert.equal(fs.statSync(codexHookPath(dir)).isFile(), true);
  } finally {
    cleanup(dir);
  }
});

test("applyOnboardingPlan: re-running overwrites a stale asset with the shipped one", () => {
  // The command is re-runnable against an onboarded folder, and re-running only picks up the
  // current copies of SWCraft's own assets; there is no merge to get wrong.
  const dir = mkWorkspace();
  try {
    const plan = planOnboarding(dir, EVERY_CLIENT_FILE);
    applyOnboardingPlan(plan, FIXTURES_AGENTS_DIR);
    fs.writeFileSync(
      plan.fileCopies[0].destPath,
      "a stale profile from an older release\n",
      "utf8",
    );

    applyOnboardingPlan(plan, FIXTURES_AGENTS_DIR);

    const expected = fs.readFileSync(path.join(FIXTURES_AGENTS_DIR, AGENT_ASSET_NAME));
    assert.ok(fs.readFileSync(plan.fileCopies[0].destPath).equals(expected));
  } finally {
    cleanup(dir);
  }
});

test("applyOnboardingPlan: a Codex hook file that is not SWCraft's is kept byte-equal and returned", () => {
  // Codex reads one hooks.json per layer, so a different file is the user's own.
  const dir = mkWorkspace();
  try {
    const plan = planOnboarding(dir, EVERY_CLIENT_FILE);
    assert.deepEqual(applyOnboardingPlan(plan, FIXTURES_AGENTS_DIR), []);
    const users = '{ "hooks": { "Stop": [] } }\n';
    fs.writeFileSync(codexHookPath(dir), users, "utf8");

    assert.deepEqual(applyOnboardingPlan(plan, FIXTURES_AGENTS_DIR), [codexHookPath(dir)]);

    assert.equal(fs.readFileSync(codexHookPath(dir), "utf8"), users);
  } finally {
    cleanup(dir);
  }
});

test("applyOnboardingPlan: a Codex hook file byte-equal to the asset is rewritten, an absent one written", () => {
  const dir = mkWorkspace();
  try {
    const plan = planOnboarding(dir, EVERY_CLIENT_FILE);
    const expected = fs.readFileSync(path.join(FIXTURES_AGENTS_DIR, CODEX_HOOK_ASSET_NAME));
    assert.equal(fs.existsSync(codexHookPath(dir)), false);
    assert.deepEqual(applyOnboardingPlan(plan, FIXTURES_AGENTS_DIR), []);
    assert.ok(fs.readFileSync(codexHookPath(dir)).equals(expected));
    assert.deepEqual(applyOnboardingPlan(plan, FIXTURES_AGENTS_DIR), []);
    assert.ok(fs.readFileSync(codexHookPath(dir)).equals(expected));
  } finally {
    cleanup(dir);
  }
});

test("applyOnboardingPlan: an older SWCraft Codex hook (another timeout, matcher, message) is overwritten", () => {
  // Recognisably SWCraft's: only hooks.PreToolUse, every handler `swcraft guard --host codex`.
  const dir = mkWorkspace();
  try {
    const older = {
      hooks: {
        PreToolUse: [
          {
            matcher: "^(Bash|apply_patch)$",
            hooks: [
              {
                type: "command",
                command: "swcraft guard --host codex",
                timeout: 30,
                statusMessage: "old message",
              },
            ],
          },
          { hooks: [{ type: "command", command: "swcraft guard --host codex" }] },
        ],
      },
    };
    fs.mkdirSync(path.dirname(codexHookPath(dir)), { recursive: true });
    fs.writeFileSync(codexHookPath(dir), JSON.stringify(older), "utf8");

    assert.deepEqual(
      applyOnboardingPlan(planOnboarding(dir, EVERY_CLIENT_FILE), FIXTURES_AGENTS_DIR),
      [],
    );

    const expected = fs.readFileSync(path.join(FIXTURES_AGENTS_DIR, CODEX_HOOK_ASSET_NAME));
    assert.ok(fs.readFileSync(codexHookPath(dir)).equals(expected));
  } finally {
    cleanup(dir);
  }
});

for (const [name, text] of [
  [
    "an extra hook beside SWCraft's",
    JSON.stringify({
      hooks: {
        PreToolUse: [
          {
            matcher: "^Bash$",
            hooks: [
              { type: "command", command: "swcraft guard --host codex", timeout: 10 },
              { type: "command", command: "my-own-check" },
            ],
          },
        ],
      },
    }),
  ],
  [
    "another event",
    JSON.stringify({
      hooks: {
        PreToolUse: [{ hooks: [{ type: "command", command: "swcraft guard --host codex" }] }],
        Stop: [],
      },
    }),
  ],
  [
    "another top-level key",
    JSON.stringify({
      hooks: {
        PreToolUse: [{ hooks: [{ type: "command", command: "swcraft guard --host codex" }] }],
      },
      note: "mine",
    }),
  ],
  [
    "another handler key",
    JSON.stringify({
      hooks: {
        PreToolUse: [
          { hooks: [{ type: "command", command: "swcraft guard --host codex", env: { A: "1" } }] },
        ],
      },
    }),
  ],
  ["text that does not parse", '{ "hooks": { "PreToolUse": [ '],
  ["an empty PreToolUse", JSON.stringify({ hooks: { PreToolUse: [] } })],
  [
    "a matcher group with no handler",
    JSON.stringify({ hooks: { PreToolUse: [{ matcher: "^Bash$", hooks: [] }] } }),
  ],
] as const) {
  test(`applyOnboardingPlan: a Codex hook file holding ${name} is kept byte-equal and returned`, () => {
    const dir = mkWorkspace();
    try {
      fs.mkdirSync(path.dirname(codexHookPath(dir)), { recursive: true });
      fs.writeFileSync(codexHookPath(dir), text, "utf8");

      assert.deepEqual(
        applyOnboardingPlan(planOnboarding(dir, EVERY_CLIENT_FILE), FIXTURES_AGENTS_DIR),
        [codexHookPath(dir)],
      );

      assert.equal(fs.readFileSync(codexHookPath(dir), "utf8"), text);
    } finally {
      cleanup(dir);
    }
  });
}

test("applyOnboardingPlan: a Codex hook path that is a directory is kept, not replaced", () => {
  const dir = mkWorkspace();
  try {
    fs.mkdirSync(codexHookPath(dir), { recursive: true });
    assert.deepEqual(
      applyOnboardingPlan(planOnboarding(dir, EVERY_CLIENT_FILE), FIXTURES_AGENTS_DIR),
      [codexHookPath(dir)],
    );
    assert.ok(fs.statSync(codexHookPath(dir)).isDirectory());
  } finally {
    cleanup(dir);
  }
});

test("isSwcraftCodexHook: a file with no SWCraft handler is not SWCraft's, an empty one included", () => {
  assert.equal(isSwcraftCodexHook(JSON.stringify({ hooks: { PreToolUse: [] } })), false);
  assert.equal(
    isSwcraftCodexHook(JSON.stringify({ hooks: { PreToolUse: [{ hooks: [] }] } })),
    false,
  );
  assert.equal(
    isSwcraftCodexHook(
      JSON.stringify({
        hooks: {
          PreToolUse: [
            { hooks: [{ type: "command", command: "swcraft guard --host codex" }] },
            { hooks: [] },
          ],
        },
      }),
    ),
    false,
  );
  assert.equal(
    isSwcraftCodexHook(
      JSON.stringify({
        hooks: {
          PreToolUse: [{ hooks: [{ type: "command", command: "swcraft guard --host codex" }] }],
        },
      }),
    ),
    true,
  );
});

/**
 * A file symbolic link at *link*, else the test skipped (Windows without the privilege to make one).
 * What the link cases do not reach: on a Windows runner without that privilege every one of them
 * skips, so there the link safety is not exercised at all; the runner's skip count shows it.
 */
function fileLinkOrSkip(t: TestContext, target: string, link: string): boolean {
  try {
    fs.symlinkSync(target, link, "file");
    return true;
  } catch (error) {
    t.skip(`symlinks are not available here: ${String(error)}`);
    return false;
  }
}

test("applyOnboardingPlan: a dangling Codex hook link is kept and nothing is created where it points", (t) => {
  // A copy onto the link would follow it and create its target outside the project.
  const dir = mkWorkspace();
  const outside = mkWorkspace();
  try {
    const target = path.join(outside, "elsewhere.json");
    fs.mkdirSync(path.dirname(codexHookPath(dir)), { recursive: true });
    if (!fileLinkOrSkip(t, target, codexHookPath(dir))) {
      return;
    }

    assert.deepEqual(
      applyOnboardingPlan(planOnboarding(dir, EVERY_CLIENT_FILE), FIXTURES_AGENTS_DIR),
      [codexHookPath(dir)],
    );

    assert.ok(fs.lstatSync(codexHookPath(dir)).isSymbolicLink());
    assert.equal(fs.readlinkSync(codexHookPath(dir)), target);
    assert.equal(fs.existsSync(target), false);
  } finally {
    cleanup(dir);
    cleanup(outside);
  }
});

test("applyOnboardingPlan: a Codex hook link to an SWCraft-shaped file is kept, its target unchanged", (t) => {
  const dir = mkWorkspace();
  const outside = mkWorkspace();
  try {
    const target = path.join(outside, "hooks.json");
    const older = JSON.stringify({
      hooks: {
        PreToolUse: [
          { hooks: [{ type: "command", command: "swcraft guard --host codex", timeout: 30 }] },
        ],
      },
    });
    fs.writeFileSync(target, older, "utf8");
    fs.mkdirSync(path.dirname(codexHookPath(dir)), { recursive: true });
    if (!fileLinkOrSkip(t, target, codexHookPath(dir))) {
      return;
    }

    assert.deepEqual(
      applyOnboardingPlan(planOnboarding(dir, EVERY_CLIENT_FILE), FIXTURES_AGENTS_DIR),
      [codexHookPath(dir)],
    );

    assert.ok(fs.lstatSync(codexHookPath(dir)).isSymbolicLink());
    assert.equal(fs.readFileSync(target, "utf8"), older);
  } finally {
    cleanup(dir);
    cleanup(outside);
  }
});

/**
 * A directory symbolic link at *link*, else the test skipped (Windows without the privilege to make one).
 * What the link cases do not reach: as `fileLinkOrSkip()` — on a Windows runner without that privilege
 * every folder-link case skips.
 */
function folderLinkOrSkip(t: TestContext, target: string, link: string): boolean {
  try {
    fs.symlinkSync(target, link, "dir");
    return true;
  } catch (error) {
    t.skip(`symlinks are not available here: ${String(error)}`);
    return false;
  }
}

test("applyOnboardingPlan: a .codex folder that is a link is kept and nothing is written through it", (t) => {
  // `lstat` of `.codex/hooks.json` follows a linked `.codex`, and a copy would write into its target.
  const dir = mkWorkspace();
  const outside = mkWorkspace();
  try {
    const codexDir = path.dirname(codexHookPath(dir));
    if (!folderLinkOrSkip(t, outside, codexDir)) {
      return;
    }

    assert.deepEqual(
      applyOnboardingPlan(planOnboarding(dir, EVERY_CLIENT_FILE), FIXTURES_AGENTS_DIR),
      [codexDir],
    );

    assert.ok(fs.lstatSync(codexDir).isSymbolicLink());
    assert.deepEqual(fs.readdirSync(outside), []);
    assert.ok(fs.existsSync(hookPath(dir)), "the other copies are still written");
  } finally {
    cleanup(dir);
    cleanup(outside);
  }
});

test("applyOnboardingPlan: a linked .codex holding an SWCraft-shaped hook is kept, the hook unchanged", (t) => {
  const dir = mkWorkspace();
  const outside = mkWorkspace();
  try {
    const older = JSON.stringify({
      hooks: {
        PreToolUse: [{ hooks: [{ type: "command", command: "swcraft guard --host codex" }] }],
      },
    });
    fs.writeFileSync(path.join(outside, "hooks.json"), older, "utf8");
    const codexDir = path.dirname(codexHookPath(dir));
    if (!folderLinkOrSkip(t, outside, codexDir)) {
      return;
    }

    assert.deepEqual(
      applyOnboardingPlan(planOnboarding(dir, EVERY_CLIENT_FILE), FIXTURES_AGENTS_DIR),
      [codexDir],
    );

    assert.equal(fs.readFileSync(path.join(outside, "hooks.json"), "utf8"), older);
    assert.deepEqual(fs.readdirSync(outside), ["hooks.json"]);
  } finally {
    cleanup(dir);
    cleanup(outside);
  }
});

test("applyOnboardingPlan: a dangling .codex link is kept and nothing is created where it points", (t) => {
  const dir = mkWorkspace();
  const outside = mkWorkspace();
  try {
    const target = path.join(outside, "gone");
    const codexDir = path.dirname(codexHookPath(dir));
    if (!folderLinkOrSkip(t, target, codexDir)) {
      return;
    }

    assert.deepEqual(
      applyOnboardingPlan(planOnboarding(dir, EVERY_CLIENT_FILE), FIXTURES_AGENTS_DIR),
      [codexDir],
    );

    assert.ok(fs.lstatSync(codexDir).isSymbolicLink());
    assert.equal(fs.existsSync(target), false);
  } finally {
    cleanup(dir);
    cleanup(outside);
  }
});

// --- The Codex hook is written race-safely: checked again at the write -------

const OLDER_SWCRAFT_CODEX_HOOK = JSON.stringify({
  hooks: {
    PreToolUse: [
      {
        hooks: [
          {
            type: "command",
            command: "swcraft guard --host codex",
            timeout: 30,
          },
        ],
      },
    ],
  },
});

/** The bundled Codex hook asset's bytes. */
function codexHookAsset(): Buffer {
  return fs.readFileSync(path.join(FIXTURES_AGENTS_DIR, CODEX_HOOK_ASSET_NAME));
}

/**
 * *action* the first time the seam is called for *destPath*, nothing for any
 * other destination or after: Copilot's copies, first in the plan, pass the same
 * seam since every copy is written link-safe.
 */
function onceAt(destPath: string, action: (destPath: string) => void): (destPath: string) => void {
  const inner = once(action);
  return (candidate) => {
    if (candidate === destPath) {
      inner(candidate);
    }
  };
}

/** *action* the first time the seam is called, nothing after. */
function once(action: (destPath: string) => void): (destPath: string) => void {
  let done = false;
  return (destPath) => {
    if (!done) {
      done = true;
      action(destPath);
    }
  };
}

test("applyOnboardingPlan: a foreign Codex hook created after the inspection is kept, never overwritten", () => {
  // The exclusive create fails with EEXIST; the hook is inspected again and found foreign.
  const dir = mkWorkspace();
  try {
    const foreign = '{"hooks":{"Stop":[]}}';
    const kept = applyOnboardingPlan(
      planOnboarding(dir, EVERY_CLIENT_FILE),
      FIXTURES_AGENTS_DIR,
      onceAt(codexHookPath(dir), (destPath) => fs.writeFileSync(destPath, foreign, "utf8")),
    );

    assert.deepEqual(kept, [codexHookPath(dir)]);
    assert.equal(fs.readFileSync(codexHookPath(dir), "utf8"), foreign);
  } finally {
    cleanup(dir);
  }
});

test("applyOnboardingPlan: an SWCraft Codex hook created after the inspection is replaced by the asset", () => {
  const dir = mkWorkspace();
  try {
    const kept = applyOnboardingPlan(
      planOnboarding(dir, EVERY_CLIENT_FILE),
      FIXTURES_AGENTS_DIR,
      onceAt(codexHookPath(dir), (destPath) =>
        fs.writeFileSync(destPath, OLDER_SWCRAFT_CODEX_HOOK, "utf8"),
      ),
    );

    assert.deepEqual(kept, []);
    assert.deepEqual(fs.readFileSync(codexHookPath(dir)), codexHookAsset());
    // No temporary file is left beside the two files `.codex` holds.
    assert.deepEqual(fs.readdirSync(path.dirname(codexHookPath(dir))).sort(), [
      "config.toml",
      "hooks.json",
    ]);
  } finally {
    cleanup(dir);
  }
});

test("applyOnboardingPlan: a foreign hook created right after the ownership check is kept, never replaced", (t) => {
  // A filesystem seam on the `lstat` that observes the hook: it finds nothing, and a
  // foreign hook appears at once. The decision is taken on that one observation
  // (absent), so the exclusive create meets the file and the hook is inspected again
  // and kept; a second look at whether it exists would have replaced it by rename.
  const dir = mkWorkspace();
  try {
    const destination = codexHookPath(dir);
    const foreign = '{"hooks":{"Stop":[]}}';
    const realLstat = nodeFs.lstatSync;
    let appeared = false;
    t.mock.method(nodeFs, "lstatSync", (...args: Parameters<typeof nodeFs.lstatSync>) => {
      if (!appeared && args[0] === destination) {
        try {
          return realLstat(...args);
        } catch (error) {
          appeared = true;
          fs.writeFileSync(destination, foreign, "utf8");
          throw error;
        }
      }
      return realLstat(...args);
    });
    const kept = applyOnboardingPlan(planOnboarding(dir, EVERY_CLIENT_FILE), FIXTURES_AGENTS_DIR);
    t.mock.restoreAll();

    assert.equal(appeared, true);
    assert.deepEqual(kept, [destination]);
    assert.equal(fs.readFileSync(destination, "utf8"), foreign);
  } finally {
    cleanup(dir);
  }
});

test("applyOnboardingPlan: SWCraft's own hook found by the one observation is replaced by the asset", () => {
  const dir = mkWorkspace();
  try {
    fs.mkdirSync(path.dirname(codexHookPath(dir)), { recursive: true });
    fs.writeFileSync(codexHookPath(dir), OLDER_SWCRAFT_CODEX_HOOK, "utf8");
    const kept = applyOnboardingPlan(planOnboarding(dir, EVERY_CLIENT_FILE), FIXTURES_AGENTS_DIR);

    assert.deepEqual(kept, []);
    assert.deepEqual(fs.readFileSync(codexHookPath(dir)), codexHookAsset());
  } finally {
    cleanup(dir);
  }
});

test("applyOnboardingPlan: an SWCraft hook swapped for a link after the inspection is replaced, never written through", (t) => {
  // The rename replaces the link itself; the file it pointed to is left as it was.
  const dir = mkWorkspace();
  const outside = mkWorkspace();
  try {
    const target = path.join(outside, "hooks.json");
    fs.writeFileSync(target, OLDER_SWCRAFT_CODEX_HOOK, "utf8");
    fs.mkdirSync(path.dirname(codexHookPath(dir)), { recursive: true });
    fs.writeFileSync(codexHookPath(dir), OLDER_SWCRAFT_CODEX_HOOK, "utf8");
    let linked = true;
    const kept = applyOnboardingPlan(
      planOnboarding(dir, EVERY_CLIENT_FILE),
      FIXTURES_AGENTS_DIR,
      onceAt(codexHookPath(dir), (destPath) => {
        fs.rmSync(destPath);
        linked = fileLinkOrSkip(t, target, destPath);
      }),
    );
    if (!linked) {
      return;
    }

    assert.deepEqual(kept, []);
    assert.equal(fs.lstatSync(codexHookPath(dir)).isSymbolicLink(), false);
    assert.deepEqual(fs.readFileSync(codexHookPath(dir)), codexHookAsset());
    assert.equal(fs.readFileSync(target, "utf8"), OLDER_SWCRAFT_CODEX_HOOK);
  } finally {
    cleanup(dir);
    cleanup(outside);
  }
});

test("applyOnboardingPlan: a .codex folder swapped for a link after the inspection gets nothing written into it", (t) => {
  const dir = mkWorkspace();
  const outside = mkWorkspace();
  try {
    const codexDir = path.dirname(codexHookPath(dir));
    let linked = true;
    const kept = applyOnboardingPlan(
      planOnboarding(dir, EVERY_CLIENT_FILE),
      FIXTURES_AGENTS_DIR,
      onceAt(codexHookPath(dir), () => {
        fs.rmSync(codexDir, { recursive: true });
        linked = folderLinkOrSkip(t, outside, codexDir);
      }),
    );
    if (!linked) {
      return;
    }

    assert.deepEqual(kept, [codexDir]);
    assert.deepEqual(fs.readdirSync(outside), []);
  } finally {
    cleanup(dir);
    cleanup(outside);
  }
});

test("applyAgentProfilePlan: the notice writes nothing into .codex, linked or not", (t) => {
  // The Codex hook belongs to the client table's codex row, so the Copilot notice never reaches
  // `.codex` at all.
  const dir = mkWorkspace();
  const outside = mkWorkspace();
  try {
    const codexDir = path.dirname(codexHookPath(dir));
    if (!folderLinkOrSkip(t, outside, codexDir)) {
      return;
    }

    const written = applyAgentProfilePlan(planAgentProfileNotice(dir, TABLE), FIXTURES_AGENTS_DIR);

    assert.equal(written.includes(codexHookPath(dir)), false);
    assert.ok(written.includes(hookPath(dir)));
    assert.deepEqual(fs.readdirSync(outside), []);
  } finally {
    cleanup(dir);
    cleanup(outside);
  }
});

test("keptCodexHookMessage: a kept .codex folder is named as a link nothing was written into", () => {
  const message = keptCodexHookMessage(path.join("/some/target", ".codex"));
  assert.equal(message.includes("\n"), false);
  assert.match(message, /kept the existing \.codex, which is a symbolic link or not a folder/);
  assert.match(message, /wrote nothing into it/);
  assert.match(message, /swcraft guard --host codex/);
  assert.match(message, /by hand/);
  assert.equal(keptCodexHookMessage(codexHookPath("/some/target")), keptCodexHookMessage());
});

test("keptCodexHookMessage: one line naming the file and the entry to add by hand", () => {
  const message = keptCodexHookMessage();
  assert.equal(message.includes("\n"), false);
  assert.match(message, /\.codex\/hooks\.json/);
  assert.match(message, /swcraft guard --host codex/);
  assert.match(message, /by hand/);
});

test("Onboard Project names every kept file and a folder outside git in the output channel and the notification", () => {
  const body = functionBody(readSource("src/commands.ts"), "async function runOnboarding(");
  // The chosen clients' files are computed once and planned from.
  assert.equal(body.match(/clientFilesFor\(/g)?.length, 1);
  assert.match(body, /const files = clientFilesFor\(table, chosen\);/);
  assert.match(body, /plan = planOnboarding\(targetDir, files\)/);
  assert.match(body, /kept = applyOnboardingPlan\(plan, /);
  assert.match(body, /onboardedMessages\(\s*targetDir,\s*\{/);
  assert.match(body, /kept,\n/);
  assert.match(body, /warnings: conflictWarnings\(table, chosen\)/);
  // The no-repository line follows the table's Codex row's `hook`, not a path spelled here.
  assert.match(body, /client\.name === CODEX_CLIENT\)\?\.hook/);
  assert.match(
    body,
    /codexHook !== null && files\.includes\(codexHook\)\s*\?\s*codexHookOutsideGitMessage\(targetDir\)/,
  );
  assert.doesNotMatch(body, /CODEX_HOOK_RELATIVE_PATH/);
  assert.match(body, /for \(const line of closing\) \{\s*outputChannel\.appendLine\(line\);/);
  assert.match(body, /showInformationMessage\(closing\.join\(" "\)\)/);
});

test("codexHookOutsideGitMessage: a folder with .git, or below one, gets no sentence", () => {
  const dir = mkWorkspace();
  try {
    const project = path.join(dir, "project");
    fs.mkdirSync(path.join(project, ".git"), { recursive: true });
    assert.equal(codexHookOutsideGitMessage(project), undefined);
    const nested = path.join(dir, "repo", "sub", "project");
    fs.mkdirSync(nested, { recursive: true });
    // A worktree's `.git` is a file; it marks a repository all the same.
    fs.writeFileSync(path.join(dir, "repo", ".git"), "gitdir: elsewhere\n", { encoding: "utf8" });
    assert.equal(codexHookOutsideGitMessage(nested), undefined);
  } finally {
    cleanup(dir);
  }
});

test("codexHookOutsideGitMessage: a folder in no git repository gets one sentence naming git init", () => {
  const asked: string[] = [];
  const target = path.join(path.sep, "some", "target");
  const message = codexHookOutsideGitMessage(target, (candidate) => {
    asked.push(candidate);
    return false;
  });
  assert.ok(message !== undefined);
  assert.equal(message.includes("\n"), false);
  assert.match(message, /\.codex\/hooks\.json only in a git repository/);
  assert.match(message, /`git init` makes it one/);
  // Every folder up to the filesystem root is asked, the target's own first. The walk starts from
  // the resolved folder, so the expected candidates are resolved the same way: on Windows a
  // rootless `\some\target` gains the current drive.
  const resolved = path.resolve(target);
  assert.equal(asked[0], path.join(resolved, ".git"));
  assert.equal(asked.at(-1), path.join(path.parse(resolved).root, ".git"));
});

/** An `OnboardingReport` with nothing in it but *fields*. */
function report(fields: Partial<OnboardingReport>): OnboardingReport {
  return {
    clientLabels: ["GitHub Copilot"],
    written: [],
    kept: [],
    warnings: [],
    outsideGit: undefined,
    ...fields,
  };
}

test("onboardedMessages: the folder and clients, what was written, then the kept lines, the warnings and the no-repository sentence", () => {
  const target = path.join(path.sep, "some", "target");
  assert.deepEqual(onboardedMessages(target, report({}), TABLE), [
    `SWCraft: onboarded ${target} for GitHub Copilot.`,
  ]);
  const outside = codexHookOutsideGitMessage(target, () => false);
  assert.deepEqual(
    onboardedMessages(
      target,
      report({
        clientLabels: ["GitHub Copilot", "Codex (OpenAI)"],
        written: ["AGENTS.md", ".vscode/settings.json"],
        kept: [codexHookPath(target)],
        outsideGit: outside,
      }),
      TABLE,
    ),
    [
      `SWCraft: onboarded ${target} for GitHub Copilot, Codex (OpenAI).`,
      "SWCraft: wrote AGENTS.md, .vscode/settings.json.",
      keptCodexHookMessage(codexHookPath(target)),
      outside,
    ],
  );
  const mcpJson = path.join(target, ".mcp.json");
  const companionKept = keptCompanionMessage(
    target,
    TABLE.copilotCompanionSettings,
    path.join(target, ".vscode", "settings.json"),
  );
  assert.deepEqual(
    onboardedMessages(
      target,
      report({
        kept: [codexHookPath(target), mcpJson],
        companionKept,
        warnings: ["first", "second"],
      }),
      TABLE,
    ),
    [
      `SWCraft: onboarded ${target} for GitHub Copilot.`,
      keptCodexHookMessage(codexHookPath(target)),
      keptMessage(target, mcpJson, TABLE),
      companionKept,
      "SWCraft: Warning: first",
      "SWCraft: Warning: second",
    ],
  );
});

test("applyOnboardingPlan: a missing source asset throws, so the caller can report it", () => {
  // `commandCore.ts`'s `onboardingApplyFailedMessage()` exists for a broken or absent
  // `dist/onboarding-assets/` in a packaged install, and is only reachable if this function
  // propagates the error instead of swallowing it.
  const dir = mkWorkspace();
  try {
    assert.throws(() =>
      applyOnboardingPlan(planOnboarding(dir, EVERY_CLIENT_FILE), path.join(dir, "no-such-dir")),
    );
  } finally {
    cleanup(dir);
  }
});

// --- The eight client files: Codex's and Claude Code's five, Kiro's three ---------------
//
// `.mcp.json`, `.claude/settings.json`, `.codex/config.toml`, `AGENTS.md` and `CLAUDE.md`: written
// where absent, overwritten where recognisably SWCraft's (the two JSON files by their structure,
// the other three by their first-line marker), kept and reported otherwise; never written through
// a link.

/** The eight client files' destinations in *dir*, keyed by their POSIX path in the project. */
const CLIENT_FILES: readonly (readonly [string, string])[] = [
  [".mcp.json", MCP_JSON_ASSET_NAME],
  [".claude/settings.json", CLAUDE_SETTINGS_ASSET_NAME],
  [".codex/config.toml", CODEX_CONFIG_ASSET_NAME],
  ["AGENTS.md", AGENTS_MD_ASSET_NAME],
  ["CLAUDE.md", CLAUDE_MD_ASSET_NAME],
  [KIRO_MCP_JSON_RELATIVE_PATH, KIRO_MCP_JSON_ASSET_NAME],
  [KIRO_STEERING_RELATIVE_PATH, KIRO_STEERING_ASSET_NAME],
  [KIRO_HOOK_RELATIVE_PATH, KIRO_HOOK_ASSET_NAME],
];

function inDir(dir: string, relative: string): string {
  return path.join(dir, ...relative.split("/"));
}

function assetText(assetName: string): string {
  return fs.readFileSync(path.join(FIXTURES_AGENTS_DIR, assetName), "utf8");
}

/** *text* written at *relative* in *dir*, its folder created. */
function writeIn(dir: string, relative: string, text: string): string {
  const destination = inDir(dir, relative);
  fs.mkdirSync(path.dirname(destination), { recursive: true });
  fs.writeFileSync(destination, text, "utf8");
  return destination;
}

test("the eight client files: absent, each is written byte-equal to its asset and nothing is kept", () => {
  const dir = mkWorkspace();
  try {
    assert.deepEqual(
      applyOnboardingPlan(planOnboarding(dir, EVERY_CLIENT_FILE), FIXTURES_AGENTS_DIR),
      [],
    );
    for (const [relative, assetName] of CLIENT_FILES) {
      assert.equal(fs.readFileSync(inDir(dir, relative), "utf8"), assetText(assetName), relative);
    }
  } finally {
    cleanup(dir);
  }
});

test("the eight client files: SWCraft's, in another spelling or an older body, are overwritten", () => {
  // The JSON files reformatted (same structure), the marked files with the marker
  // line and another body: each is recognisably SWCraft's and is replaced.
  const dir = mkWorkspace();
  try {
    const older: Record<string, string> = {
      ".mcp.json": JSON.stringify(JSON.parse(assetText(MCP_JSON_ASSET_NAME))),
      ".claude/settings.json": JSON.stringify(JSON.parse(assetText(CLAUDE_SETTINGS_ASSET_NAME))),
      ".codex/config.toml": `${TOML_MARKER_LINE}\r\n[mcp_servers.swcraft]\ncommand = "old"\n`,
      "AGENTS.md": `${MARKDOWN_MARKER_LINE}\n\nAn older profile.\n`,
      "CLAUDE.md": `${MARKDOWN_MARKER_LINE}\n`,
      [KIRO_MCP_JSON_RELATIVE_PATH]: JSON.stringify(
        JSON.parse(assetText(KIRO_MCP_JSON_ASSET_NAME)),
      ),
      [KIRO_STEERING_RELATIVE_PATH]: `${MARKDOWN_MARKER_LINE}\r\n\nAn older steering file.\n`,
      [KIRO_HOOK_RELATIVE_PATH]: JSON.stringify(JSON.parse(assetText(KIRO_HOOK_ASSET_NAME))),
    };
    for (const [relative] of CLIENT_FILES) {
      writeIn(dir, relative, older[relative]);
    }

    assert.deepEqual(
      applyOnboardingPlan(planOnboarding(dir, EVERY_CLIENT_FILE), FIXTURES_AGENTS_DIR),
      [],
    );

    for (const [relative, assetName] of CLIENT_FILES) {
      assert.equal(fs.readFileSync(inDir(dir, relative), "utf8"), assetText(assetName), relative);
    }
    assert.deepEqual(fs.readdirSync(path.join(dir, ".claude")), ["settings.json"]);
  } finally {
    cleanup(dir);
  }
});

/** Kiro's `.kiro/settings/mcp.json` shape SWCraft writes, its `swcraft` server's fields overridden by *server*. */
function kiroMcpJson(server: Record<string, unknown> = {}): string {
  return JSON.stringify({
    mcpServers: {
      swcraft: {
        command: "swcraft",
        args: ["mcp-serve", "--project", "swcraft.yaml"],
        env: { SWCRAFT_REVIEW: "panel" },
        ...server,
      },
    },
  });
}

/**
 * Kiro's `.kiro/hooks/swcraft-guard.json` shape SWCraft writes, its one entry's
 * fields overridden by *entry*, followed by *more* entries.
 */
function kiroHook(entry: Record<string, unknown> = {}, more: unknown[] = []): string {
  return JSON.stringify({
    version: "v1",
    hooks: [
      {
        name: "SWCraft model guard",
        trigger: "PreToolUse",
        matcher: "*",
        action: { type: "command", command: "swcraft guard --host kiro" },
        timeout: 10,
        ...entry,
      },
      ...more,
    ],
  });
}

const FOREIGN_CLIENT_FILES: readonly (readonly [string, string, string])[] = [
  [
    ".mcp.json",
    "a second server beside SWCraft's",
    JSON.stringify({
      mcpServers: {
        ...JSON.parse(assetText(MCP_JSON_ASSET_NAME)).mcpServers,
        mine: { command: "my-server" },
      },
    }),
  ],
  [
    ".mcp.json",
    "SWCraft's server under another command",
    JSON.stringify({
      mcpServers: {
        swcraft: {
          command: "/opt/swcraft/bin/swcraft",
          args: ["mcp-serve", "--project", "/abs/swcraft.yaml"],
          env: { SWCRAFT_REVIEW: "panel" },
        },
      },
    }),
  ],
  [".mcp.json", "text that does not parse", '{ "mcpServers": { '],
  [
    ".claude/settings.json",
    "a fourth key",
    JSON.stringify({
      ...JSON.parse(assetText(CLAUDE_SETTINGS_ASSET_NAME)),
      permissions: { allow: ["Bash(ls)"] },
    }),
  ],
  [
    ".claude/settings.json",
    "a second handler beside the guard",
    assetText(CLAUDE_SETTINGS_ASSET_NAME).replace(
      '"timeout": 10',
      '"timeout": 10 }, { "type": "command", "command": "my-hook"',
    ),
  ],
  [".claude/settings.json", "text that does not parse", "{ env: "],
  [
    ".codex/config.toml",
    "the marker line removed",
    assetText(CODEX_CONFIG_ASSET_NAME).split("\n").slice(1).join("\n"),
  ],
  [
    ".codex/config.toml",
    "the Markdown marker, not the TOML one",
    `${MARKDOWN_MARKER_LINE}\n[mcp_servers.swcraft]\n`,
  ],
  ["AGENTS.md", "the marker line removed", "# My own agents file\n"],
  ["AGENTS.md", "the marker on the second line", `\n${MARKDOWN_MARKER_LINE}\n`],
  ["CLAUDE.md", "the marker line edited", `${MARKDOWN_MARKER_LINE.replace("SWCraft", "Mine")}\n`],
  ["CLAUDE.md", "an empty file", ""],
  [
    KIRO_MCP_JSON_RELATIVE_PATH,
    "a second server beside SWCraft's",
    JSON.stringify({
      mcpServers: {
        ...JSON.parse(assetText(KIRO_MCP_JSON_ASSET_NAME)).mcpServers,
        mine: { command: "my-server" },
      },
    }),
  ],
  [
    KIRO_MCP_JSON_RELATIVE_PATH,
    "SWCraft's server with Kiro's autoApprove added",
    kiroMcpJson({ autoApprove: ["validate"] }),
  ],
  [KIRO_MCP_JSON_RELATIVE_PATH, "text that does not parse", '{ "mcpServers": '],
  [
    KIRO_STEERING_RELATIVE_PATH,
    "the marker line removed",
    assetText(KIRO_STEERING_ASSET_NAME).split("\n").slice(1).join("\n"),
  ],
  [
    KIRO_STEERING_RELATIVE_PATH,
    "front matter before the marker",
    `---\ninclusion: always\n---\n${MARKDOWN_MARKER_LINE}\n`,
  ],
  [
    KIRO_HOOK_RELATIVE_PATH,
    "a second entry running another command",
    kiroHook({}, [
      { name: "mine", trigger: "PreToolUse", action: { type: "command", command: "my-hook" } },
    ]),
  ],
  [KIRO_HOOK_RELATIVE_PATH, "SWCraft's entry switched off", kiroHook({ enabled: false })],
  [KIRO_HOOK_RELATIVE_PATH, "text that does not parse", '{ "version": '],
];

for (const [relative, name, text] of FOREIGN_CLIENT_FILES) {
  test(`the eight client files: ${relative} holding ${name} is kept byte-equal and returned`, () => {
    const dir = mkWorkspace();
    try {
      const destination = writeIn(dir, relative, text);

      assert.deepEqual(
        applyOnboardingPlan(planOnboarding(dir, EVERY_CLIENT_FILE), FIXTURES_AGENTS_DIR),
        [destination],
      );

      assert.equal(fs.readFileSync(destination, "utf8"), text);
      for (const [other, assetName] of CLIENT_FILES) {
        if (other !== relative) {
          assert.equal(fs.readFileSync(inDir(dir, other), "utf8"), assetText(assetName), other);
        }
      }
    } finally {
      cleanup(dir);
    }
  });
}

test("the eight client files: an earlier release's .mcp.json and .claude/settings.json are replaced", () => {
  // Another argument, and the matcher and timeout an earlier SWCraft wrote (without Claude Code's
  // `Monitor`): the guard's fix reaches a project onboarded by an earlier SWCraft.
  const dir = mkWorkspace();
  try {
    writeIn(dir, ".mcp.json", mcpJson({ args: ["mcp-serve", "--project", "/abs/swcraft.yaml"] }));
    writeIn(
      dir,
      ".claude/settings.json",
      claudeSettings("^(Read|Grep|Glob|Edit|Write|MultiEdit|NotebookEdit|Bash|PowerShell)$", 30),
    );
    assert.deepEqual(
      applyOnboardingPlan(planOnboarding(dir, EVERY_CLIENT_FILE), FIXTURES_AGENTS_DIR),
      [],
    );
    for (const [relative, assetName] of CLIENT_FILES) {
      assert.equal(fs.readFileSync(inDir(dir, relative), "utf8"), assetText(assetName), relative);
    }
  } finally {
    cleanup(dir);
  }
});

test("the eight client files: a destination that is a directory is kept, not replaced", () => {
  const dir = mkWorkspace();
  try {
    fs.mkdirSync(path.join(dir, "AGENTS.md"));
    assert.deepEqual(
      applyOnboardingPlan(planOnboarding(dir, EVERY_CLIENT_FILE), FIXTURES_AGENTS_DIR),
      [path.join(dir, "AGENTS.md")],
    );
    assert.ok(fs.statSync(path.join(dir, "AGENTS.md")).isDirectory());
  } finally {
    cleanup(dir);
  }
});

for (const [relative, assetName] of CLIENT_FILES) {
  test(`the eight client files: ${relative} as a link to a SWCraft file elsewhere is kept, its target unchanged`, (t) => {
    const dir = mkWorkspace();
    const outside = mkWorkspace();
    try {
      const target = path.join(outside, "target");
      // The target holds SWCraft's own file, so only the link makes the entry foreign.
      fs.writeFileSync(target, `${assetText(assetName)}\n`, "utf8");
      const destination = inDir(dir, relative);
      fs.mkdirSync(path.dirname(destination), { recursive: true });
      if (!fileLinkOrSkip(t, target, destination)) {
        return;
      }

      assert.deepEqual(
        applyOnboardingPlan(planOnboarding(dir, EVERY_CLIENT_FILE), FIXTURES_AGENTS_DIR),
        [destination],
      );

      assert.ok(fs.lstatSync(destination).isSymbolicLink());
      assert.equal(fs.readFileSync(target, "utf8"), `${assetText(assetName)}\n`);
    } finally {
      cleanup(dir);
      cleanup(outside);
    }
  });

  test(`the eight client files: ${relative} as a dangling link is kept and nothing is created where it points`, (t) => {
    const dir = mkWorkspace();
    const outside = mkWorkspace();
    try {
      const target = path.join(outside, "gone");
      const destination = inDir(dir, relative);
      fs.mkdirSync(path.dirname(destination), { recursive: true });
      if (!fileLinkOrSkip(t, target, destination)) {
        return;
      }

      assert.deepEqual(
        applyOnboardingPlan(planOnboarding(dir, EVERY_CLIENT_FILE), FIXTURES_AGENTS_DIR),
        [destination],
      );

      assert.equal(fs.existsSync(target), false);
    } finally {
      cleanup(dir);
      cleanup(outside);
    }
  });
}

test("the eight client files: a .claude folder that is a link is kept and nothing is written through it", (t) => {
  const dir = mkWorkspace();
  const outside = mkWorkspace();
  try {
    const claudeDir = path.join(dir, ".claude");
    if (!folderLinkOrSkip(t, outside, claudeDir)) {
      return;
    }

    assert.deepEqual(
      applyOnboardingPlan(planOnboarding(dir, EVERY_CLIENT_FILE), FIXTURES_AGENTS_DIR),
      [claudeDir],
    );

    assert.ok(fs.lstatSync(claudeDir).isSymbolicLink());
    assert.deepEqual(fs.readdirSync(outside), []);
    assert.ok(fs.existsSync(path.join(dir, ".mcp.json")), "the other client files are written");
  } finally {
    cleanup(dir);
    cleanup(outside);
  }
});

test("the eight client files: a dangling .claude link is kept and nothing is created where it points", (t) => {
  const dir = mkWorkspace();
  const outside = mkWorkspace();
  try {
    const target = path.join(outside, "gone");
    const claudeDir = path.join(dir, ".claude");
    if (!folderLinkOrSkip(t, target, claudeDir)) {
      return;
    }

    assert.deepEqual(
      applyOnboardingPlan(planOnboarding(dir, EVERY_CLIENT_FILE), FIXTURES_AGENTS_DIR),
      [claudeDir],
    );

    assert.equal(fs.existsSync(target), false);
  } finally {
    cleanup(dir);
    cleanup(outside);
  }
});

test("the eight client files: a linked .codex is returned once, and neither hooks.json nor config.toml goes through it", (t) => {
  const dir = mkWorkspace();
  const outside = mkWorkspace();
  try {
    const codexDir = path.join(dir, ".codex");
    if (!folderLinkOrSkip(t, outside, codexDir)) {
      return;
    }

    assert.deepEqual(
      applyOnboardingPlan(planOnboarding(dir, EVERY_CLIENT_FILE), FIXTURES_AGENTS_DIR),
      [codexDir],
    );

    assert.deepEqual(fs.readdirSync(outside), []);
  } finally {
    cleanup(dir);
    cleanup(outside);
  }
});

test("the eight client files: a foreign file created after the inspection is kept, never overwritten", () => {
  const dir = mkWorkspace();
  try {
    const destination = path.join(dir, ".mcp.json");
    const foreign = '{"mcpServers":{"mine":{"command":"x"}}}';
    // The seam runs after each guarded copy's inspection; it acts at `.mcp.json` once.
    const kept = applyOnboardingPlan(
      planOnboarding(dir, EVERY_CLIENT_FILE),
      FIXTURES_AGENTS_DIR,
      (destPath) => {
        if (destPath === destination && !fs.existsSync(destPath)) {
          fs.writeFileSync(destPath, foreign, "utf8");
        }
      },
    );
    assert.deepEqual(kept, [destination]);
    assert.equal(fs.readFileSync(destination, "utf8"), foreign);
  } finally {
    cleanup(dir);
  }
});

test("the eight client files: SWCraft's file swapped for a link after the inspection is replaced, never written through", (t) => {
  const dir = mkWorkspace();
  const outside = mkWorkspace();
  try {
    const destination = writeIn(dir, "AGENTS.md", `${MARKDOWN_MARKER_LINE}\nolder\n`);
    const target = path.join(outside, "AGENTS.md");
    fs.writeFileSync(target, `${MARKDOWN_MARKER_LINE}\nolder\n`, "utf8");
    let linked = true;
    let swapped = false;
    const kept = applyOnboardingPlan(
      planOnboarding(dir, EVERY_CLIENT_FILE),
      FIXTURES_AGENTS_DIR,
      (destPath) => {
        if (destPath === destination && !swapped) {
          swapped = true;
          fs.rmSync(destPath);
          linked = fileLinkOrSkip(t, target, destPath);
        }
      },
    );
    if (!linked) {
      return;
    }

    assert.deepEqual(kept, []);
    assert.equal(fs.lstatSync(destination).isSymbolicLink(), false);
    assert.equal(fs.readFileSync(destination, "utf8"), assetText(AGENTS_MD_ASSET_NAME));
    assert.equal(fs.readFileSync(target, "utf8"), `${MARKDOWN_MARKER_LINE}\nolder\n`);
  } finally {
    cleanup(dir);
    cleanup(outside);
  }
});

test("the eight client files: a .claude folder swapped for a link after the inspection gets nothing written into it", (t) => {
  const dir = mkWorkspace();
  const outside = mkWorkspace();
  try {
    const claudeDir = path.join(dir, ".claude");
    const settings = path.join(claudeDir, "settings.json");
    let linked = true;
    let swapped = false;
    const kept = applyOnboardingPlan(
      planOnboarding(dir, EVERY_CLIENT_FILE),
      FIXTURES_AGENTS_DIR,
      (destPath) => {
        if (destPath === settings && !swapped) {
          swapped = true;
          fs.rmSync(claudeDir, { recursive: true });
          linked = folderLinkOrSkip(t, outside, claudeDir);
        }
      },
    );
    if (!linked) {
      return;
    }

    assert.deepEqual(kept, [claudeDir]);
    assert.deepEqual(fs.readdirSync(outside), []);
  } finally {
    cleanup(dir);
    cleanup(outside);
  }
});

test("the recognisers: each bundled client asset is SWCraft's own file by its plan's recognition", () => {
  for (const copy of planOnboarding("/p", EVERY_CLIENT_FILE).fileCopies) {
    if (copy.keepsForeign !== undefined) {
      assert.ok(isOwnFile(assetText(copy.assetName), copy.keepsForeign), copy.assetName);
    }
  }
  assert.ok(isSwcraftMcpJson(assetText(MCP_JSON_ASSET_NAME)));
  assert.ok(isSwcraftClaudeSettings(assetText(CLAUDE_SETTINGS_ASSET_NAME)));
  assert.ok(isSwcraftKiroMcpJson(assetText(KIRO_MCP_JSON_ASSET_NAME)));
  assert.ok(isSwcraftKiroHook(assetText(KIRO_HOOK_ASSET_NAME)));
  // Each recogniser refuses the other JSON files; Kiro's server line shares the core,
  // so `.mcp.json`'s shape is Kiro's too, and Kiro's (no `timeout` today) is `.mcp.json`'s.
  assert.equal(isSwcraftMcpJson(assetText(CLAUDE_SETTINGS_ASSET_NAME)), false);
  assert.equal(isSwcraftClaudeSettings(assetText(MCP_JSON_ASSET_NAME)), false);
  assert.ok(isSwcraftKiroMcpJson(assetText(MCP_JSON_ASSET_NAME)));
  for (const other of [CLAUDE_SETTINGS_ASSET_NAME, CODEX_HOOK_ASSET_NAME, HOOK_ASSET_NAME]) {
    assert.equal(isSwcraftKiroMcpJson(assetText(other)), false, other);
    assert.equal(isSwcraftKiroHook(assetText(other)), false, other);
  }
  assert.equal(isSwcraftKiroHook(assetText(KIRO_MCP_JSON_ASSET_NAME)), false);
  assert.equal(isSwcraftKiroMcpJson(assetText(KIRO_HOOK_ASSET_NAME)), false);
  assert.equal(isSwcraftCodexHook(assetText(KIRO_HOOK_ASSET_NAME)), false);
});

test("the Kiro recognisers: an earlier release's shape is SWCraft's, so its file is replaced", () => {
  assert.ok(isSwcraftKiroMcpJson(kiroMcpJson()));
  // The optional `timeout` key, present or not by release, its value not compared.
  assert.ok(isSwcraftKiroMcpJson(kiroMcpJson({ timeout: 86400000 })));
  assert.ok(isSwcraftKiroMcpJson(kiroMcpJson({ timeout: 600000 })));
  assert.ok(isSwcraftKiroMcpJson(kiroMcpJson({ env: undefined })));
  assert.ok(isSwcraftKiroMcpJson(kiroMcpJson({ env: { SWCRAFT_REVIEW: "elicit" } })));
  // `.mcp.json` takes no `timeout`: the key is Kiro's file's alone.
  assert.equal(isSwcraftMcpJson(kiroMcpJson({ timeout: 86400000 })), false);
  // Another timeout, matcher or name, no matcher, a second SWCraft entry.
  assert.ok(isSwcraftKiroHook(kiroHook()));
  assert.ok(isSwcraftKiroHook(kiroHook({ timeout: 60 })));
  assert.ok(isSwcraftKiroHook(kiroHook({ matcher: "@builtin" })));
  assert.ok(
    isSwcraftKiroHook(kiroHook({ name: "Model guard", matcher: undefined, timeout: undefined })),
  );
  assert.ok(isSwcraftKiroHook(JSON.stringify({ ...JSON.parse(kiroHook()), version: "v2" })));
  assert.ok(
    isSwcraftKiroHook(
      kiroHook({}, [{ action: { type: "command", command: "swcraft guard --host kiro" } }]),
    ),
  );
});

test("the Kiro recognisers: another key, server, command or entry makes the file the user's", () => {
  for (const text of [
    kiroMcpJson({ timeout: "86400000" }),
    kiroMcpJson({ disabled: false }),
    kiroMcpJson({ autoApprove: ["*"] }),
    kiroMcpJson({ disabledTools: ["build"] }),
    kiroMcpJson({ command: "/opt/swcraft/bin/swcraft" }),
    kiroMcpJson({ env: { SWCRAFT_REVIEW: "panel", PATH: "/x" } }),
    JSON.stringify({ ...JSON.parse(kiroMcpJson()), powers: {} }),
    "not json",
  ]) {
    assert.equal(isSwcraftKiroMcpJson(text), false, text);
  }
  for (const text of [
    kiroHook({ action: { type: "command", command: "swcraft guard --host codex" } }),
    kiroHook({ action: { type: "command", command: "swcraft guard" } }),
    kiroHook({ action: { type: "askAgent", prompt: "check" } }),
    kiroHook({ action: { type: "command", command: "swcraft guard --host kiro", cwd: "." } }),
    kiroHook({ enabled: true }),
    kiroHook({ description: "mine" }),
    kiroHook({ timeout: "10" }),
    kiroHook({ matcher: 1 }),
    kiroHook({}, [{ name: "mine", action: { type: "command", command: "my-hook" } }]),
    JSON.stringify({ version: "v1", hooks: [] }),
    JSON.stringify({ hooks: JSON.parse(kiroHook()).hooks }),
    JSON.stringify({ ...JSON.parse(kiroHook()), extra: 1 }),
    "not json",
  ]) {
    assert.equal(isSwcraftKiroHook(text), false, text);
  }
});

test("the eight client files: an earlier release's Kiro server line and hook are replaced", () => {
  // A `timeout` key in mcp.json (the optional key), another hook timeout, matcher and name.
  const dir = mkWorkspace();
  try {
    writeIn(dir, KIRO_MCP_JSON_RELATIVE_PATH, kiroMcpJson({ timeout: 86400000 }));
    writeIn(
      dir,
      KIRO_HOOK_RELATIVE_PATH,
      kiroHook({ name: "Guard", matcher: "@builtin", timeout: 30 }),
    );
    assert.deepEqual(
      applyOnboardingPlan(planOnboarding(dir, EVERY_CLIENT_FILE), FIXTURES_AGENTS_DIR),
      [],
    );
    for (const [relative, assetName] of CLIENT_FILES) {
      assert.equal(fs.readFileSync(inDir(dir, relative), "utf8"), assetText(assetName), relative);
    }
  } finally {
    cleanup(dir);
  }
});

/** The `.claude/settings.json` shape SWCraft writes, with *matcher* and *timeout* as given. */
function claudeSettings(
  matcher: string,
  timeout: number,
  extra: Record<string, unknown> = {},
): string {
  return JSON.stringify({
    env: { CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS: "0" },
    enabledMcpjsonServers: ["swcraft"],
    hooks: {
      PreToolUse: [
        { matcher, hooks: [{ type: "command", command: "swcraft guard --host claude", timeout }] },
      ],
    },
    ...extra,
  });
}

/** The `.mcp.json` shape SWCraft writes, its `swcraft` server's fields overridden by *server*. */
function mcpJson(server: Record<string, unknown> = {}, more: Record<string, unknown> = {}): string {
  return JSON.stringify({
    mcpServers: {
      swcraft: {
        command: "swcraft",
        args: ["mcp-serve", "--project", "${CLAUDE_PROJECT_DIR:-.}/swcraft.yaml"],
        env: { SWCRAFT_REVIEW: "panel" },
        ...server,
      },
      ...more,
    },
  });
}

test("the JSON recognisers: an earlier release's shape is SWCraft's, so its file is replaced", () => {
  // The matcher an earlier SWCraft wrote (without Claude Code's `Monitor`), and another timeout.
  const preMonitor = "^(Read|Grep|Glob|Edit|Write|MultiEdit|NotebookEdit|Bash|PowerShell)$";
  assert.ok(isSwcraftClaudeSettings(claudeSettings(preMonitor, 10)));
  assert.ok(isSwcraftClaudeSettings(claudeSettings(preMonitor, 30)));
  assert.ok(isSwcraftClaudeSettings(claudeSettings("^(Bash)$", 5, { env: undefined })));
  assert.ok(isSwcraftMcpJson(mcpJson()));
  assert.ok(isSwcraftMcpJson(mcpJson({ args: ["mcp-serve", "--project", "swcraft.yaml"] })));
  assert.ok(isSwcraftMcpJson(mcpJson({ env: { SWCRAFT_REVIEW: "elicit" } })));
  assert.ok(isSwcraftMcpJson(mcpJson({ env: undefined })));
});

test("the JSON recognisers: a second server, another key, command or handler makes the file the user's", () => {
  const matcher = "^(Read|Bash)$";
  const handler = { type: "command", command: "swcraft guard --host claude" };
  for (const text of [
    claudeSettings(matcher, 10, { permissions: { allow: ["Bash(ls)"] } }),
    claudeSettings(matcher, 10, { env: { CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS: "0", OTHER: "1" } }),
    claudeSettings(matcher, 10, { enabledMcpjsonServers: ["swcraft", "mine"] }),
    claudeSettings(matcher, 10, { hooks: undefined }),
    claudeSettings(matcher, 10, {
      hooks: { PreToolUse: [{ matcher, hooks: [handler, { type: "command", command: "mine" }] }] },
    }),
    claudeSettings(matcher, 10, {
      hooks: {
        PreToolUse: [{ matcher, hooks: [{ ...handler, command: "swcraft guard --host codex" }] }],
      },
    }),
    claudeSettings(matcher, 10, {
      hooks: { PreToolUse: [{ matcher, hooks: [handler] }], Stop: [] },
    }),
    claudeSettings(matcher, 10, { hooks: { PreToolUse: [] } }),
    "not json",
  ]) {
    assert.equal(isSwcraftClaudeSettings(text), false, text);
  }
  for (const text of [
    mcpJson({}, { mine: { command: "mine" } }),
    mcpJson({ command: "/usr/local/bin/swcraft" }),
    mcpJson({ args: ["serve"] }),
    mcpJson({ cwd: "." }),
    mcpJson({ env: { SWCRAFT_REVIEW: "panel", PATH: "/x" } }),
    JSON.stringify({
      mcpServers: { swcraft: { command: "swcraft", args: ["mcp-serve"] } },
      other: 1,
    }),
    "not json",
  ]) {
    assert.equal(isSwcraftMcpJson(text), false, text);
  }
});

test("applyOnboardingPlan: a .claude that is a file is kept and named, and every other copy is still written", () => {
  const dir = mkWorkspace();
  try {
    const claudeEntry = path.join(dir, ".claude");
    fs.writeFileSync(claudeEntry, "mine\n", "utf8");

    const kept = applyOnboardingPlan(planOnboarding(dir, EVERY_CLIENT_FILE), FIXTURES_AGENTS_DIR);

    assert.deepEqual(kept, [claudeEntry]);
    assert.equal(fs.readFileSync(claudeEntry, "utf8"), "mine\n");
    for (const copy of planOnboarding(dir, EVERY_CLIENT_FILE).fileCopies) {
      if (!copy.destPath.startsWith(claudeEntry + path.sep)) {
        assert.ok(fs.existsSync(copy.destPath), copy.destPath);
      }
    }
    assert.match(
      keptMessage(dir, claudeEntry, TABLE),
      /kept the existing \.claude, which is a symbolic link or not a folder/,
    );
  } finally {
    cleanup(dir);
  }
});

test("the markers: the bundled assets open with the marker lines this module recognises", () => {
  // The sentence is swcraft_tools.agents_md's, and the assets are byte copies of the template's
  // files (packageVsix.test.ts), so this pins the bundled assets and the template's copies;
  // `agents_md.py` itself is not read here.
  for (const assetName of [AGENTS_MD_ASSET_NAME, CLAUDE_MD_ASSET_NAME, KIRO_STEERING_ASSET_NAME]) {
    assert.equal(assetText(assetName).split("\n")[0], MARKDOWN_MARKER_LINE, assetName);
  }
  assert.equal(assetText(CODEX_CONFIG_ASSET_NAME).split("\n")[0], TOML_MARKER_LINE);
  assert.ok(hasOnboardMarker(`${MARKDOWN_MARKER_LINE}\r\nbody`, MARKDOWN_MARKER_LINE));
  assert.ok(hasOnboardMarker(MARKDOWN_MARKER_LINE, MARKDOWN_MARKER_LINE));
  assert.equal(hasOnboardMarker(` ${MARKDOWN_MARKER_LINE}\n`, MARKDOWN_MARKER_LINE), false);
});

test("the Codex hook's recognition is unchanged: isOwnFile's codex-hook is isSwcraftCodexHook", () => {
  for (const text of [
    assetText(CODEX_HOOK_ASSET_NAME),
    OLDER_SWCRAFT_CODEX_HOOK,
    '{"hooks":{"Stop":[]}}',
    "not json",
    `${MARKDOWN_MARKER_LINE}\n`,
  ]) {
    assert.equal(isOwnFile(text, "codex-hook"), isSwcraftCodexHook(text), text);
  }
});

test("keptMessage: every kept client file is named with the guide section, one line each", () => {
  const target = path.join(path.sep, "some", "target");
  for (const [relative] of CLIENT_FILES) {
    const message = keptMessage(target, inDir(target, relative), TABLE);
    assert.equal(message.includes("\n"), false);
    assert.ok(message.includes(`kept the existing ${relative}, which is not SWCraft's`), message);
    if (relative.startsWith(".kiro/")) {
      // Kiro's row names its page's own heading, so the line names the page.
      assert.ok(message.endsWith(`as the guide's "In Kiro" page says.`), message);
    } else {
      assert.ok(message.includes(`"${CLIENT_FILES_GUIDE_SECTION}"`), message);
      assert.match(message, /\(In VS Code\)/);
    }
    assert.match(message, /by hand/);
  }
  for (const folder of [".kiro", ".kiro/settings", ".kiro/steering", ".kiro/hooks"]) {
    const message = keptMessage(target, inDir(target, folder), TABLE);
    assert.ok(
      message.includes(`kept the existing ${folder}, which is a symbolic link or not a folder`),
      message,
    );
    assert.ok(message.endsWith(`as the guide's "In Kiro" page says.`), message);
  }
  const folder = keptMessage(target, path.join(target, ".claude"), TABLE);
  assert.match(folder, /kept the existing \.claude, which is a symbolic link or not a folder/);
  assert.ok(folder.includes(`"${CLIENT_FILES_GUIDE_SECTION}"`), folder);
  // The Codex hook keeps its own line; a linked .codex names config.toml and the guide too.
  assert.equal(keptMessage(target, codexHookPath(target), TABLE), keptCodexHookMessage());
  const codexFolder = keptMessage(target, path.join(target, ".codex"), TABLE);
  assert.equal(codexFolder, keptCodexHookMessage(path.join(target, ".codex")));
  assert.match(codexFolder, /config\.toml/);
  assert.ok(codexFolder.includes(`"${CLIENT_FILES_GUIDE_SECTION}"`), codexFolder);
  assert.equal(CLIENT_FILES_GUIDE_SECTION, "Codex and Claude Code in VS Code");
});

test("the notice's plan is the table's Copilot row alone: it writes no Codex, Claude Code or companion file", () => {
  const dir = mkWorkspace();
  try {
    const notice = planAgentProfileNotice(dir, TABLE);
    const copilot = TABLE.clients.find((client) => client.name === "copilot");
    assert.ok(copilot !== undefined);
    assert.deepEqual(notice.fileCopies, planOnboarding(dir, copilot.files).fileCopies);
    assert.deepEqual(
      notice.fileCopies,
      planOnboarding(dir, EVERY_CLIENT_FILE).fileCopies.slice(0, 3),
    );

    assert.equal(applyAgentProfilePlan(notice, FIXTURES_AGENTS_DIR).length, 3);

    for (const [relative] of CLIENT_FILES) {
      assert.equal(fs.existsSync(inDir(dir, relative)), false, `the notice wrote ${relative}`);
    }
    assert.equal(fs.existsSync(codexHookPath(dir)), false, "the notice wrote the Codex hook");
    assert.equal(fs.existsSync(path.join(dir, ".codex")), false);
    assert.equal(fs.existsSync(path.join(dir, ".claude")), false);
    assert.equal(fs.existsSync(path.join(dir, ".vscode")), false);
    assert.deepEqual(fs.readdirSync(dir).sort(), [".github"]);
  } finally {
    cleanup(dir);
  }
});

// --- The missing-profile notice ---------------------------------------------------

test("shouldOfferAgentProfile: only a SWCraft project with no profile and no dismissal is offered", () => {
  assert.equal(
    shouldOfferAgentProfile({
      projectDir: "/p",
      profileExists: false,
      dismissed: false,
      copilotInUse: true,
    }),
    true,
  );
  assert.equal(
    shouldOfferAgentProfile({
      projectDir: "/p",
      profileExists: true,
      dismissed: false,
      copilotInUse: true,
    }),
    false,
    "a project that has the profile is not asked",
  );
  assert.equal(
    shouldOfferAgentProfile({
      projectDir: "/p",
      profileExists: false,
      dismissed: true,
      copilotInUse: true,
    }),
    false,
    "'Not for this workspace' is remembered",
  );
  assert.equal(
    shouldOfferAgentProfile({
      projectDir: undefined,
      profileExists: false,
      dismissed: false,
      copilotInUse: true,
    }),
    false,
    "a window with no swcraft.yaml is no SWCraft project",
  );
});

test("the notice names the profile's read-only file access, never 'the SWCraft tools alone'", () => {
  // The profile grants `view` for a saved SWCraft answer, so "tools alone" would be a stale claim.
  assert.ok(!AGENT_PROFILE_NOTICE.includes("tools alone"), AGENT_PROFILE_NOTICE);
  assert.ok(
    AGENT_PROFILE_NOTICE.includes("read-only file access for a SWCraft answer saved to a file"),
  );
});

test("the notice's dismissal key is the one the plan names", () => {
  assert.equal(AGENT_PROFILE_NOTICE_DISMISSED_KEY, "swcraft.agentProfileNotice.dismissed");
});

test("agentProfileNoticeAction: Add applies, Not for this workspace remembers, closing does nothing", () => {
  assert.equal(agentProfileNoticeAction(ADD_AGENT_PROFILE_CHOICE), "apply");
  assert.equal(agentProfileNoticeAction(DISMISS_AGENT_PROFILE_CHOICE), "remember");
  assert.equal(
    agentProfileNoticeAction(undefined),
    "nothing",
    "a notice closed without a choice is asked again next time",
  );
  assert.equal(agentProfileNoticeAction("something else"), "nothing");
});

test("applyAgentProfilePlan: writes the profile, the instructions and Copilot's hook into a project that has none", () => {
  const dir = mkWorkspace();
  try {
    const written = applyAgentProfilePlan(planAgentProfileNotice(dir, TABLE), FIXTURES_AGENTS_DIR);

    const destinations: [string, string][] = [
      [AGENT_ASSET_NAME, path.join(dir, ".github", "agents", AGENT_ASSET_NAME)],
      [INSTRUCTIONS_ASSET_NAME, path.join(dir, ".github", INSTRUCTIONS_ASSET_NAME)],
      [HOOK_ASSET_NAME, path.join(dir, ".github", "hooks", "swcraft.json")],
    ];
    assert.deepEqual(
      written,
      destinations.map(([, destination]) => destination),
    );
    for (const [name, destination] of destinations) {
      assert.ok(
        fs.readFileSync(destination).equals(fs.readFileSync(path.join(FIXTURES_AGENTS_DIR, name))),
      );
    }
  } finally {
    cleanup(dir);
  }
});

test("applyAgentProfilePlan: an existing instructions file is left byte-equal", () => {
  const dir = mkWorkspace();
  try {
    const instructions = path.join(dir, ".github", INSTRUCTIONS_ASSET_NAME);
    fs.mkdirSync(path.dirname(instructions), { recursive: true });
    const own = Buffer.from("# This project's own Copilot instructions\n", "utf8");
    fs.writeFileSync(instructions, own);

    const written = applyAgentProfilePlan(planAgentProfileNotice(dir, TABLE), FIXTURES_AGENTS_DIR);

    assert.deepEqual(written, [
      path.join(dir, ".github", "agents", AGENT_ASSET_NAME),
      hookPath(dir),
    ]);
    assert.ok(fs.readFileSync(instructions).equals(own), "the user's instructions were changed");
  } finally {
    cleanup(dir);
  }
});

test("applyAgentProfilePlan: an existing hook file is left byte-equal, the rest is written", () => {
  // The notice never overwrites: a project whose own hook file is already there
  // keeps it, exactly as its instructions are kept.
  const dir = mkWorkspace();
  try {
    const hook = hookPath(dir);
    fs.mkdirSync(path.dirname(hook), { recursive: true });
    const own = Buffer.from('{ "hooks": {} }\n', "utf8");
    fs.writeFileSync(hook, own);

    const written = applyAgentProfilePlan(planAgentProfileNotice(dir, TABLE), FIXTURES_AGENTS_DIR);

    assert.deepEqual(written, [
      path.join(dir, ".github", "agents", AGENT_ASSET_NAME),
      path.join(dir, ".github", INSTRUCTIONS_ASSET_NAME),
    ]);
    assert.ok(fs.readFileSync(hook).equals(own), "the project's hook file was changed");
  } finally {
    cleanup(dir);
  }
});

test("applyAgentProfilePlan: an existing Codex hook file is left byte-equal: the notice has no Codex file", () => {
  const dir = mkWorkspace();
  try {
    const hook = codexHookPath(dir);
    fs.mkdirSync(path.dirname(hook), { recursive: true });
    const own = Buffer.from('{ "hooks": {} }\n', "utf8");
    fs.writeFileSync(hook, own);

    const written = applyAgentProfilePlan(planAgentProfileNotice(dir, TABLE), FIXTURES_AGENTS_DIR);

    assert.deepEqual(written, [
      path.join(dir, ".github", "agents", AGENT_ASSET_NAME),
      path.join(dir, ".github", INSTRUCTIONS_ASSET_NAME),
      hookPath(dir),
    ]);
    assert.ok(fs.readFileSync(hook).equals(own), "the project's Codex hook file was changed");
  } finally {
    cleanup(dir);
  }
});

test("applyAgentProfilePlan: never overwrites — a file that appeared after the plan is skipped", () => {
  // The plan was made when all three were absent; all three appear before it runs. The
  // exclusive copy leaves each one byte-equal and reports nothing written.
  const dir = mkWorkspace();
  try {
    const plan = planAgentProfileNotice(dir, TABLE);
    const own = Buffer.from("written by the user meanwhile\n", "utf8");
    for (const copy of plan.fileCopies) {
      fs.mkdirSync(path.dirname(copy.destPath), { recursive: true });
      fs.writeFileSync(copy.destPath, own);
    }

    assert.deepEqual(applyAgentProfilePlan(plan, FIXTURES_AGENTS_DIR), []);
    for (const copy of plan.fileCopies) {
      assert.ok(fs.readFileSync(copy.destPath).equals(own), `${copy.destPath} was overwritten`);
    }
  } finally {
    cleanup(dir);
  }
});

test("applyAgentProfilePlan: a missing source asset throws, so the caller can report it", () => {
  const dir = mkWorkspace();
  try {
    assert.throws(() =>
      applyAgentProfilePlan(planAgentProfileNotice(dir, TABLE), path.join(dir, "none")),
    );
  } finally {
    cleanup(dir);
  }
});

test("Onboard Project's plan is unchanged by the notice: the first three overwritten, the rest kept when foreign", () => {
  // The notice copies exclusively; *Onboard Project* still overwrites the first
  // three, because running it asks for this release's copies — and it is how a
  // project with an older profile, which the notice never fires for, gets the
  // hook. A different Codex hook file is the user's and is kept, and so is each
  // of the eight client files that is not SWCraft's.
  const dir = mkWorkspace();
  try {
    const plan = planOnboarding(dir, EVERY_CLIENT_FILE);
    assert.deepEqual(
      plan.fileCopies.map((copy) => copy.assetName),
      [
        AGENT_ASSET_NAME,
        INSTRUCTIONS_ASSET_NAME,
        HOOK_ASSET_NAME,
        CODEX_HOOK_ASSET_NAME,
        MCP_JSON_ASSET_NAME,
        CLAUDE_SETTINGS_ASSET_NAME,
        CODEX_CONFIG_ASSET_NAME,
        AGENTS_MD_ASSET_NAME,
        CLAUDE_MD_ASSET_NAME,
        KIRO_MCP_JSON_ASSET_NAME,
        KIRO_STEERING_ASSET_NAME,
        KIRO_HOOK_ASSET_NAME,
      ],
    );
    for (const copy of plan.fileCopies) {
      fs.mkdirSync(path.dirname(copy.destPath), { recursive: true });
      fs.writeFileSync(copy.destPath, "an older copy\n", "utf8");
    }
    assert.deepEqual(
      applyOnboardingPlan(plan, FIXTURES_AGENTS_DIR),
      plan.fileCopies.slice(3).map((copy) => copy.destPath),
    );

    for (const copy of plan.fileCopies.slice(0, 3)) {
      assert.ok(
        fs
          .readFileSync(copy.destPath)
          .equals(fs.readFileSync(path.join(FIXTURES_AGENTS_DIR, copy.assetName))),
        `${copy.destPath} was not replaced`,
      );
    }
    for (const copy of plan.fileCopies.slice(3)) {
      assert.equal(fs.readFileSync(copy.destPath, "utf8"), "an older copy\n");
    }
  } finally {
    cleanup(dir);
  }
});

test("the notice's adapter never runs swcraft init and writes through the exclusive plan only", () => {
  // The adapter is VS Code glue and cannot run here; its body is read as text,
  // the way workspaceTrust.test.ts reads the adapters.
  const body = functionBody(
    readSource("src/commands.ts"),
    "export async function offerAgentProfile(",
  );
  assert.match(body, /shouldOfferAgentProfile\(/);
  assert.match(body, /applyAgentProfilePlan\(\s*planAgentProfileNotice\(/);
  assert.doesNotMatch(body, /planOnboarding\(/);
  // The dismissal is written only on the "remember" answer, which
  // agentProfileNoticeAction() gives for "Not for this workspace" alone.
  assert.match(
    body,
    /if \(action === "remember"\) \{\s*await context\.workspaceState\.update\(AGENT_PROFILE_NOTICE_DISMISSED_KEY, true\);/,
  );
  assert.equal(body.match(/workspaceState\.update\(/g)?.length, 1);
  for (const forbidden of [
    /applyOnboardingPlan/,
    /initArgv/,
    /spawnCli/,
    /writeFileSync/,
    /copyFileSync/,
  ]) {
    assert.doesNotMatch(body, forbidden);
  }
  assert.match(readSource("src/extension.ts"), /offerAgentProfile\(context\)/);
});

// --- Onboarding never runs inside a project -------------------------------------
//
// Each test builds its folders under one temporary directory and states the
// boundary the command would compute, so a manifest above the temporary
// directory itself is never reached — except by the one test whose point is the
// walk to the filesystem root, which places its manifest inside the temporary
// directory and asserts only that it is found.

function folder(...parts: string[]): string {
  const dir = path.join(...parts);
  fs.mkdirSync(dir, { recursive: true });
  return dir;
}

function manifestIn(dir: string): void {
  fs.writeFileSync(path.join(dir, MANIFEST_FILENAME), "model_root: model\n", {
    encoding: "utf-8",
  });
}

test("enclosingProject: a folder with no manifest above it is onboarded", () => {
  const root = mkWorkspace();
  try {
    const target = folder(root, "a", "b");
    assert.equal(enclosingProject(target, onboardingBoundary(target, [root])), undefined);
  } finally {
    cleanup(root);
  }
});

test("enclosingProject: a parent two levels up holds a manifest — refused, that parent named", () => {
  const root = mkWorkspace();
  try {
    const project = folder(root, "project");
    manifestIn(project);
    const target = folder(project, "model", "swc");
    assert.equal(enclosingProject(target, onboardingBoundary(target, [root])), project);
  } finally {
    cleanup(root);
  }
});

test("enclosingProject: the workspace folder is the project, a subfolder is chosen — refused, the boundary itself checked", () => {
  const root = mkWorkspace();
  try {
    const workspace = folder(root, "workspace");
    manifestIn(workspace);
    const target = folder(workspace, "model", "swc");
    assert.equal(
      enclosingProject(target, onboardingBoundary(target, [workspace])),
      path.resolve(workspace),
    );
  } finally {
    cleanup(root);
  }
});

test("enclosingProject: only the chosen folder holds a manifest — re-onboarding a project root is not refused", () => {
  const root = mkWorkspace();
  try {
    const project = folder(root, "project");
    manifestIn(project);
    assert.equal(enclosingProject(project, onboardingBoundary(project, [root])), undefined);
    assert.equal(containedProject(project), undefined);
  } finally {
    cleanup(root);
  }
});

test("enclosingProject: the chosen folder and a parent both hold a manifest — refused", () => {
  const root = mkWorkspace();
  try {
    const project = folder(root, "project");
    manifestIn(project);
    const target = folder(project, "nested");
    manifestIn(target);
    assert.equal(enclosingProject(target, onboardingBoundary(target, [root])), project);
  } finally {
    cleanup(root);
  }
});

test("enclosingProject: the chosen folder is the workspace folder — nothing above it is walked", () => {
  const root = mkWorkspace();
  try {
    manifestIn(root);
    const workspace = folder(root, "workspace");
    const boundary = onboardingBoundary(workspace, [workspace]);
    assert.equal(boundary, path.resolve(workspace));
    assert.equal(enclosingProject(workspace, boundary), undefined);
  } finally {
    cleanup(root);
  }
});

test("enclosingProject: a subfolder of the workspace with a manifest above the workspace only is onboarded", () => {
  const root = mkWorkspace();
  try {
    manifestIn(root);
    const workspace = folder(root, "workspace");
    const target = folder(workspace, "sub");
    assert.equal(enclosingProject(target, onboardingBoundary(target, [workspace])), undefined);
  } finally {
    cleanup(root);
  }
});

test("onboardingBoundary: no workspace folder contains the chosen one — the walk reaches the filesystem root", () => {
  const root = mkWorkspace();
  try {
    manifestIn(root);
    const elsewhere = folder(root, "elsewhere");
    const target = folder(root, "outside", "deep");
    const boundary = onboardingBoundary(target, [elsewhere]);
    assert.equal(boundary, path.parse(path.resolve(target)).root);
    assert.equal(onboardingBoundary(target, []), boundary);
    assert.equal(enclosingProject(target, boundary), path.resolve(root));
  } finally {
    cleanup(root);
  }
});

test("onboardingBoundary: the outermost of two nested workspace folders is the boundary", () => {
  const root = mkWorkspace();
  try {
    const outer = folder(root, "outer");
    manifestIn(outer);
    const inner = folder(outer, "inner");
    const target = folder(inner, "x");
    for (const folders of [
      [outer, inner],
      [inner, outer],
    ]) {
      const boundary = onboardingBoundary(target, folders);
      assert.equal(boundary, path.resolve(outer));
      assert.equal(enclosingProject(target, boundary), path.resolve(outer));
      assert.equal(onboardingRefusal(target, folders), nestedProjectMessage(path.resolve(outer)));
    }
  } finally {
    cleanup(root);
  }
});

test("onboardingRefusal: the chosen folder IS an inner workspace folder — the outer one's manifest refuses, naming it", () => {
  const root = mkWorkspace();
  try {
    const w = folder(root, "w");
    manifestIn(w);
    const sub = folder(w, "sub");
    for (const folders of [
      [w, sub],
      [sub, w],
    ]) {
      assert.equal(onboardingBoundary(sub, folders), path.resolve(w));
      assert.equal(onboardingRefusal(sub, folders), nestedProjectMessage(path.resolve(w)));
    }
    // The outermost workspace folder chosen itself still walks nothing above it.
    manifestIn(root);
    const bare = folder(root, "bare");
    assert.equal(onboardingBoundary(bare, [bare, folder(bare, "inner")]), path.resolve(bare));
    assert.equal(onboardingRefusal(bare, [bare]), undefined);
  } finally {
    cleanup(root);
  }
});

test("onboardingBoundary: a sibling whose name shares a prefix is not inside", () => {
  const root = mkWorkspace();
  try {
    const proj = folder(root, "proj");
    manifestIn(proj);
    const sibling = folder(root, "proj-2");
    assert.equal(onboardingBoundary(sibling, [proj]), path.parse(path.resolve(sibling)).root);
    assert.equal(enclosingProject(sibling, root), undefined);
  } finally {
    cleanup(root);
  }
});

test("onboardingBoundary: a descendant whose name starts with two dots is inside", () => {
  const root = mkWorkspace();
  try {
    const proj = folder(root, "proj");
    manifestIn(proj);
    const assets = folder(proj, "..assets");
    assert.equal(onboardingBoundary(assets, [proj]), path.resolve(proj));
    assert.equal(enclosingProject(assets, root), path.resolve(proj));
  } finally {
    cleanup(root);
  }
});

test("containedProject: a folder with no manifest below it holds no project", () => {
  const root = mkWorkspace();
  try {
    folder(root, "a", "b", "c");
    assert.equal(containedProject(root), undefined);
  } finally {
    cleanup(root);
  }
});

test("containedProject: a manifest one level below is found, POSIX-spelled", () => {
  const root = mkWorkspace();
  try {
    manifestIn(folder(root, "project"));
    assert.equal(containedProject(root), "project");
  } finally {
    cleanup(root);
  }
});

test("containedProject: the depth bound — a manifest at depth 3 is found, one at depth 4 is not", () => {
  assert.equal(CONTAINED_PROJECT_MAX_DEPTH, 3);
  const root = mkWorkspace();
  try {
    const atThree = folder(root, "three");
    manifestIn(folder(atThree, "a", "b", "c"));
    assert.equal(containedProject(atThree), "a/b/c");
    const atFour = folder(root, "four");
    manifestIn(folder(atFour, "a", "b", "c", "d"));
    assert.equal(containedProject(atFour), undefined);
    assert.equal(containedProject(atFour, 4), "a/b/c/d", "the bound is the parameter");
  } finally {
    cleanup(root);
  }
});

test("containedProject: each skipped directory name is never entered", () => {
  assert.deepEqual([...CONTAINED_PROJECT_SKIPPED_DIRS], [".git", "node_modules", ".venv", "dist"]);
  for (const skipped of CONTAINED_PROJECT_SKIPPED_DIRS) {
    const root = mkWorkspace();
    try {
      manifestIn(folder(root, skipped));
      manifestIn(folder(root, "other", skipped));
      assert.equal(containedProject(root), undefined, skipped);
      manifestIn(folder(root, "kept"));
      assert.equal(containedProject(root), "kept", skipped);
    } finally {
      cleanup(root);
    }
  }
});

test("containedProject: the first manifest found wins — shallower first, then code-unit order", () => {
  const root = mkWorkspace();
  try {
    // Created out of order, so the answer cannot be the directory's own order.
    manifestIn(folder(root, "c"));
    manifestIn(folder(root, "a", "deep"));
    manifestIn(folder(root, "b"));
    assert.equal(containedProject(root), "b");
    fs.rmSync(path.join(root, "b"), { recursive: true });
    assert.equal(containedProject(root), "c");
    fs.rmSync(path.join(root, "c"), { recursive: true });
    assert.equal(containedProject(root), "a/deep");
  } finally {
    cleanup(root);
  }
});

test("containedProject: the chosen folder's own manifest is not below it", () => {
  const root = mkWorkspace();
  try {
    manifestIn(root);
    folder(root, "model", "swc");
    assert.equal(containedProject(root), undefined);
    manifestIn(folder(root, "inner"));
    assert.equal(containedProject(root), "inner");
  } finally {
    cleanup(root);
  }
});

test("containedProject: within one level, each folder's entries in code-unit order, parent by parent", () => {
  const root = mkWorkspace();
  try {
    // As paths, `a-x/b` sorts before `a/z` ("-" < "/"); parent by parent, `a` comes first.
    manifestIn(folder(root, "a-x", "b"));
    manifestIn(folder(root, "a", "z"));
    assert.equal(containedProject(root), "a/z");
  } finally {
    cleanup(root);
  }
});

test("containedProject: the breadth bound — past maxEntries entries read it stops and finds nothing", () => {
  assert.equal(CONTAINED_PROJECT_MAX_ENTRIES, 2000);
  const root = mkWorkspace();
  try {
    // The root lists five entries (four folders and a file); the manifest is one level below `d`.
    for (const name of ["a", "b", "c"]) {
      folder(root, name);
    }
    fs.writeFileSync(path.join(root, "notes.txt"), "", { encoding: "utf-8" });
    manifestIn(folder(root, "d"));
    assert.equal(containedProject(root, CONTAINED_PROJECT_MAX_DEPTH, 4), undefined);
    assert.equal(containedProject(root, CONTAINED_PROJECT_MAX_DEPTH, 5), "d");
    // Below the root: four entries, then `a` lists two, so the manifest under it needs six read.
    folder(root, "a", "x");
    manifestIn(folder(root, "a", "y"));
    fs.rmSync(path.join(root, "d"), { recursive: true });
    assert.equal(containedProject(root, CONTAINED_PROJECT_MAX_DEPTH, 5), undefined);
    assert.equal(containedProject(root, CONTAINED_PROJECT_MAX_DEPTH, 6), "a/y");
    assert.equal(containedProject(root), "a/y", "the default bound reads far more");
  } finally {
    cleanup(root);
  }
});

test("containedProject: a folder with more entries than the budget left stops the walk, its project not found", () => {
  const root = mkWorkspace();
  try {
    // The root lists one entry, `big`; `big` lists forty files and the project folder `p`.
    const big = folder(root, "big");
    for (let i = 0; i < 40; i++) {
      fs.writeFileSync(path.join(big, `f${String(i).padStart(2, "0")}.txt`), "", {
        encoding: "utf-8",
      });
    }
    manifestIn(folder(big, "p"));
    assert.equal(containedProject(root, CONTAINED_PROJECT_MAX_DEPTH, 41), undefined);
    assert.equal(containedProject(root, CONTAINED_PROJECT_MAX_DEPTH, 10), undefined);
    assert.equal(containedProject(root, CONTAINED_PROJECT_MAX_DEPTH, 42), "big/p");
    // A folder read whole within the budget is still sorted: `big/p` after `big/a`'s project.
    manifestIn(folder(big, "a"));
    assert.equal(containedProject(root, CONTAINED_PROJECT_MAX_DEPTH, 43), "big/a");
  } finally {
    cleanup(root);
  }
});

test("onboardingRefusal: a project root with a project nested below it is re-onboarded, not refused", () => {
  const root = mkWorkspace();
  try {
    const project = folder(root, "project");
    manifestIn(project);
    manifestIn(folder(project, "examples", "basic"));
    assert.equal(containedProject(project), "examples/basic", "the nested project is in reach");
    assert.equal(onboardingRefusal(project, [root]), undefined);
  } finally {
    cleanup(root);
  }
});

test("onboardingRefusal: a folder with no manifest of its own over a project is refused, the project named", () => {
  const root = mkWorkspace();
  try {
    const empty = folder(root, "empty");
    manifestIn(folder(empty, "a", "project"));
    assert.equal(onboardingRefusal(empty, [root]), containedProjectMessage("a/project"));
  } finally {
    cleanup(root);
  }
});

test("onboardingRefusal: the walk up runs always — with an own manifest, and before the walk down", () => {
  const root = mkWorkspace();
  try {
    const project = folder(root, "project");
    manifestIn(project);
    const nested = folder(project, "nested");
    manifestIn(nested);
    assert.equal(onboardingRefusal(nested, [root]), nestedProjectMessage(project));
    const bare = folder(project, "bare");
    manifestIn(folder(bare, "below"));
    assert.equal(onboardingRefusal(bare, [root]), nestedProjectMessage(project));
  } finally {
    cleanup(root);
  }
});

test("onboardingRefusal: a folder with no project above or below it is onboarded; the bounds are parameters", () => {
  const root = mkWorkspace();
  try {
    const target = folder(root, "target");
    manifestIn(folder(target, "a", "b", "c", "d"));
    assert.equal(onboardingRefusal(target, [root]), undefined);
    assert.equal(onboardingRefusal(target, [root], 4), containedProjectMessage("a/b/c/d"));
    assert.equal(onboardingRefusal(target, [root], 4, 1), undefined);
  } finally {
    cleanup(root);
  }
});

test("containedProjectMessage names the project below and says what to do", () => {
  assert.equal(
    containedProjectMessage("a/b"),
    "SWCraft: this folder contains the project at a/b; onboard that folder or choose an empty one.",
  );
});

test("nestedProjectMessage names the project's folder and says to onboard it", () => {
  const parent = path.join(os.tmpdir(), "project");
  const message = nestedProjectMessage(parent);
  assert.ok(message.includes(`inside the project at ${parent}`));
  assert.match(message, /onboard that folder\.$/);
});

// --- The chosen clients ---------------------------------------------------------------

/** Every file under *dir*, POSIX-spelled relative to it, sorted. */
function filesUnder(dir: string): string[] {
  const found: string[] = [];
  const walk = (folder: string): void => {
    for (const entry of fs.readdirSync(folder, { withFileTypes: true })) {
      const full = path.join(folder, entry.name);
      if (entry.isDirectory()) {
        walk(full);
      } else {
        found.push(path.relative(dir, full).split(path.sep).join("/"));
      }
    }
  };
  walk(dir);
  return found.sort();
}

test("Onboard Project writes exactly each chosen client's files, and the default writes Copilot's alone", () => {
  for (const client of TABLE.clients) {
    const dir = mkWorkspace();
    try {
      const plan = planOnboarding(dir, clientFilesFor(TABLE, [client.name]));
      const kept = applyOnboardingPlan(plan, FIXTURES_AGENTS_DIR);
      assert.deepEqual(kept, [], client.name);
      assert.deepEqual(filesUnder(dir), [...client.files].sort(), client.name);
      assert.deepEqual(writtenFiles(plan, kept).sort(), [...client.files].sort(), client.name);
    } finally {
      cleanup(dir);
    }
  }
  assert.deepEqual(clientFilesFor(TABLE, TABLE.default), [
    AGENT_PROFILE_RELATIVE_PATH,
    ".github/copilot-instructions.md",
    ".github/hooks/swcraft.json",
  ]);
});

test("Onboard Project removes nothing: an unchosen client's files stay byte-equal and unreported", () => {
  const dir = mkWorkspace();
  try {
    applyOnboardingPlan(planOnboarding(dir, EVERY_CLIENT_FILE), FIXTURES_AGENTS_DIR);
    const codexHook = codexHookPath(dir);
    fs.writeFileSync(codexHook, '{ "hooks": { "Stop": [] } }\n', "utf8");
    const before = new Map(
      filesUnder(dir).map((file) => [file, fs.readFileSync(inDir(dir, file))]),
    );

    const plan = planOnboarding(dir, clientFilesFor(TABLE, ["copilot"]));
    const kept = applyOnboardingPlan(plan, FIXTURES_AGENTS_DIR);

    assert.deepEqual(kept, [], "a file of a client not chosen is not reported");
    assert.deepEqual(filesUnder(dir), [...before.keys()]);
    for (const [file, bytes] of before) {
      assert.ok(fs.readFileSync(inDir(dir, file)).equals(bytes), file);
    }
  } finally {
    cleanup(dir);
  }
});

test("writtenFiles: every planned copy but a kept file and the copies inside a kept folder", () => {
  const dir = path.join(path.sep, "p");
  const plan = planOnboarding(dir, EVERY_CLIENT_FILE);
  const kept = [path.join(dir, ".codex"), path.join(dir, ".mcp.json")];
  assert.deepEqual(writtenFiles(plan, kept), [
    AGENT_PROFILE_RELATIVE_PATH,
    ".github/copilot-instructions.md",
    ".github/hooks/swcraft.json",
    ".claude/settings.json",
    "AGENTS.md",
    "CLAUDE.md",
    ".kiro/settings/mcp.json",
    ".kiro/steering/swcraft.md",
    ".kiro/hooks/swcraft-guard.json",
  ]);
  // A kept `.kiro` folder holds Kiro's three, and none of them is written.
  assert.deepEqual(
    writtenFiles(plan, [path.join(dir, ".kiro")]).filter((file) => file.startsWith(".kiro/")),
    [],
  );
});

const COMPANION = TABLE.copilotCompanionSettings;

function companionPath(dir: string): string {
  return inDir(dir, COMPANION.file);
}

test("applyCompanionSettings: an absent settings file is written with the table's keys alone", () => {
  const dir = mkWorkspace();
  try {
    assert.deepEqual(applyCompanionSettings(dir, COMPANION, false), {
      kind: "written",
      destPath: companionPath(dir),
    });
    assert.deepEqual(JSON.parse(fs.readFileSync(companionPath(dir), "utf8")), COMPANION.settings);
    assert.deepEqual(filesUnder(dir), [COMPANION.file]);
  } finally {
    cleanup(dir);
  }
});

test("applyCompanionSettings: an existing settings file is never edited and is reported kept", () => {
  const dir = mkWorkspace();
  try {
    const own = '{ "editor.tabSize": 4 }\n';
    fs.mkdirSync(path.dirname(companionPath(dir)), { recursive: true });
    fs.writeFileSync(companionPath(dir), own, "utf8");

    assert.deepEqual(applyCompanionSettings(dir, COMPANION, false), {
      kind: "kept",
      destPath: companionPath(dir),
    });
    assert.equal(fs.readFileSync(companionPath(dir), "utf8"), own);

    fs.writeFileSync(companionPath(dir), "not json", "utf8");
    assert.equal(applyCompanionSettings(dir, COMPANION, true).kind, "kept");
    assert.equal(fs.readFileSync(companionPath(dir), "utf8"), "not json");
  } finally {
    cleanup(dir);
  }
});

test("applyCompanionSettings: an existing file already holding the keys is present, untouched", () => {
  const dir = mkWorkspace();
  try {
    const own = JSON.stringify({ "editor.tabSize": 4, ...COMPANION.settings });
    fs.mkdirSync(path.dirname(companionPath(dir)), { recursive: true });
    fs.writeFileSync(companionPath(dir), own, "utf8");

    assert.equal(applyCompanionSettings(dir, COMPANION, false).kind, "present");
    assert.equal(fs.readFileSync(companionPath(dir), "utf8"), own);
  } finally {
    cleanup(dir);
  }
});

test("applyCompanionSettings: the file this run's swcraft init wrote is completed with the keys, its own kept", () => {
  const dir = mkWorkspace();
  try {
    const initOutput = { "files.exclude": { "**/*.arxml": false } };
    fs.mkdirSync(path.dirname(companionPath(dir)), { recursive: true });
    fs.writeFileSync(companionPath(dir), JSON.stringify(initOutput, null, 2), "utf8");

    assert.equal(applyCompanionSettings(dir, COMPANION, true).kind, "written");
    assert.deepEqual(JSON.parse(fs.readFileSync(companionPath(dir), "utf8")), {
      ...initOutput,
      ...COMPANION.settings,
    });
    assert.deepEqual(fs.readdirSync(path.dirname(companionPath(dir))), ["settings.json"]);
  } finally {
    cleanup(dir);
  }
});

test("applyCompanionSettings: a .vscode that is a link is kept and nothing is written through it", (t) => {
  const dir = mkWorkspace();
  const outside = mkWorkspace();
  try {
    const vscodeDir = path.dirname(companionPath(dir));
    if (!folderLinkOrSkip(t, outside, vscodeDir)) {
      return;
    }
    for (const completes of [false, true]) {
      assert.deepEqual(applyCompanionSettings(dir, COMPANION, completes), {
        kind: "kept",
        destPath: vscodeDir,
      });
    }
    assert.deepEqual(fs.readdirSync(outside), []);
  } finally {
    cleanup(dir);
    cleanup(outside);
  }
});

test("keptCompanionMessage: one line naming the file, the keys to add by hand and the guide section", () => {
  const target = path.join(path.sep, "some", "target");
  const message = keptCompanionMessage(target, COMPANION, companionPath(target));
  assert.equal(message.includes("\n"), false);
  assert.ok(message.includes(`kept the existing ${COMPANION.file};`), message);
  for (const [key, value] of Object.entries(COMPANION.settings)) {
    assert.ok(message.includes(`"${key}": ${JSON.stringify(value)}`), message);
  }
  assert.match(message, /by hand/);
  assert.ok(message.includes(`"${CLIENT_FILES_GUIDE_SECTION}"`), message);
  const folder = keptCompanionMessage(target, COMPANION, path.join(target, ".vscode"));
  assert.match(folder, /kept the existing \.vscode, which is a symbolic link or not a folder/);
  assert.ok(folder.includes(`to ${COMPANION.file} by hand`), folder);
});

test("Onboard Project writes the companion settings only for the table's rule, after the client files", () => {
  const body = functionBody(readSource("src/commands.ts"), "async function runOnboarding(");
  assert.match(
    body,
    /companion = companionApplies\(table, chosen\)\s*\?\s*applyCompanionSettings\(\s*targetDir,\s*table\.copilotCompanionSettings,\s*companionAbsentBeforeInit,?\s*\)\s*:\s*undefined;/,
  );
  // Absence is read before `swcraft init` runs, which writes `.vscode/settings.json` itself.
  assert.ok(
    body.indexOf("companionAbsentBeforeInit =") <
      body.indexOf("spawnCli(executable, steps.initArgv"),
  );
  assert.ok(
    body.indexOf("kept = applyOnboardingPlan(") < body.indexOf("? applyCompanionSettings("),
  );
});

// --- Copilot's three files are written link-safe too --------------------------------
//
// `copyFileSync` follows a symbolic link at the destination and through a linked `.github` or
// `.github/hooks`: a cloned repository committing `.github/copilot-instructions.md -> ~/.zshrc`
// would have that file overwritten. Every copy goes through `writeKeepingForeign()`
// (`always-own`) and the notice writes exclusively.

const COPILOT_FILES = clientFilesFor(TABLE, ["copilot"]);

test("Onboard Project: Copilot's files replace a regular file, as before", () => {
  const dir = mkWorkspace();
  try {
    const instructions = path.join(dir, ".github", INSTRUCTIONS_ASSET_NAME);
    fs.mkdirSync(path.dirname(instructions), { recursive: true });
    fs.writeFileSync(instructions, "an older copy\n", "utf8");
    const kept = applyOnboardingPlan(planOnboarding(dir, COPILOT_FILES), FIXTURES_AGENTS_DIR);
    assert.deepEqual(kept, []);
    assert.deepEqual(
      fs.readFileSync(instructions),
      fs.readFileSync(path.join(FIXTURES_AGENTS_DIR, INSTRUCTIONS_ASSET_NAME)),
    );
  } finally {
    cleanup(dir);
  }
});

for (const relative of COPILOT_FILES) {
  test(`Onboard Project: ${relative} as a link is kept and its target is left as it was`, (t) => {
    const dir = mkWorkspace();
    const outside = mkWorkspace();
    try {
      const target = path.join(outside, "zshrc");
      fs.writeFileSync(target, "mine\n", "utf8");
      const dest = path.join(dir, ...relative.split("/"));
      fs.mkdirSync(path.dirname(dest), { recursive: true });
      if (!fileLinkOrSkip(t, target, dest)) {
        return;
      }
      const kept = applyOnboardingPlan(planOnboarding(dir, COPILOT_FILES), FIXTURES_AGENTS_DIR);
      assert.deepEqual(kept, [dest]);
      assert.equal(fs.readFileSync(target, "utf8"), "mine\n");
      assert.ok(fs.lstatSync(dest).isSymbolicLink());
      const message = keptMessage(dir, dest, TABLE);
      assert.ok(
        message.includes(`kept the existing ${relative}, which is a symbolic link`),
        message,
      );
      assert.match(message, /run Onboard Project again/);
    } finally {
      cleanup(dir);
      cleanup(outside);
    }
  });

  test(`Onboard Project: ${relative} as a dangling link is kept and nothing is created where it points`, (t) => {
    const dir = mkWorkspace();
    const outside = mkWorkspace();
    try {
      const target = path.join(outside, "created");
      const dest = path.join(dir, ...relative.split("/"));
      fs.mkdirSync(path.dirname(dest), { recursive: true });
      if (!fileLinkOrSkip(t, target, dest)) {
        return;
      }
      const kept = applyOnboardingPlan(planOnboarding(dir, COPILOT_FILES), FIXTURES_AGENTS_DIR);
      assert.deepEqual(kept, [dest]);
      assert.deepEqual(fs.readdirSync(outside), []);
    } finally {
      cleanup(dir);
      cleanup(outside);
    }
  });
}

for (const folder of [".github", ".github/hooks"]) {
  test(`Onboard Project: a linked ${folder} is kept, named once, and nothing is written through it`, (t) => {
    const dir = mkWorkspace();
    const outside = mkWorkspace();
    try {
      const linkPath = path.join(dir, ...folder.split("/"));
      fs.mkdirSync(path.dirname(linkPath), { recursive: true });
      if (!folderLinkOrSkip(t, outside, linkPath)) {
        return;
      }
      const plan = planOnboarding(dir, COPILOT_FILES);
      const kept = applyOnboardingPlan(plan, FIXTURES_AGENTS_DIR);
      assert.deepEqual(kept, [linkPath]);
      assert.deepEqual(fs.readdirSync(outside), []);
      const message = keptMessage(dir, linkPath, TABLE);
      assert.ok(
        message.includes(`kept the existing ${folder}, which is a symbolic link or not a folder`),
      );
      assert.match(message, /run Onboard Project again to get SWCraft's files/);
      // Only what lies outside the kept folder was written.
      for (const written of writtenFiles(plan, kept)) {
        assert.ok(!written.startsWith(`${folder}/`), written);
      }
    } finally {
      cleanup(dir);
      cleanup(outside);
    }
  });

  test(`the Add-agent notice: a linked ${folder} gets nothing written through it`, (t) => {
    const dir = mkWorkspace();
    const outside = mkWorkspace();
    try {
      const linkPath = path.join(dir, ...folder.split("/"));
      fs.mkdirSync(path.dirname(linkPath), { recursive: true });
      if (!folderLinkOrSkip(t, outside, linkPath)) {
        return;
      }
      const written = applyAgentProfilePlan(
        planAgentProfileNotice(dir, TABLE),
        FIXTURES_AGENTS_DIR,
      );
      assert.deepEqual(fs.readdirSync(outside), []);
      for (const file of written) {
        assert.ok(!file.startsWith(linkPath + path.sep), file);
      }
    } finally {
      cleanup(dir);
      cleanup(outside);
    }
  });
}

test("the Add-agent notice: a file link and a dangling link at a Copilot file are left alone", (t) => {
  const dir = mkWorkspace();
  const outside = mkWorkspace();
  try {
    const target = path.join(outside, "zshrc");
    fs.writeFileSync(target, "mine\n", "utf8");
    const instructions = path.join(dir, ".github", INSTRUCTIONS_ASSET_NAME);
    const hook = hookPath(dir);
    fs.mkdirSync(path.dirname(hook), { recursive: true });
    if (
      !fileLinkOrSkip(t, target, instructions) ||
      !fileLinkOrSkip(t, path.join(outside, "created"), hook)
    ) {
      return;
    }
    const written = applyAgentProfilePlan(planAgentProfileNotice(dir, TABLE), FIXTURES_AGENTS_DIR);
    assert.deepEqual(written, [path.join(dir, ...AGENT_PROFILE_RELATIVE_PATH.split("/"))]);
    assert.equal(fs.readFileSync(target, "utf8"), "mine\n");
    assert.deepEqual(fs.readdirSync(outside).sort(), ["zshrc"]);
  } finally {
    cleanup(dir);
    cleanup(outside);
  }
});

test("the notice is not offered in a project that does not use Copilot", () => {
  assert.equal(
    shouldOfferAgentProfile({
      projectDir: "/p",
      profileExists: false,
      dismissed: false,
      copilotInUse: false,
    }),
    false,
  );
  const body = functionBody(
    readSource("src/commands.ts"),
    "export async function offerAgentProfile(",
  );
  assert.match(body, /copilotUnused\(context, projectDir\)/);
  assert.match(
    body,
    /shouldOfferAgentProfile\(\{ projectDir, profileExists, dismissed, copilotInUse \}\)/,
  );
});

/** Each guide page the table names, with its title (the page's `# ` heading). */
const GUIDE_PAGE_TITLES: ReadonlyMap<string, string> = new Map([
  ["vscode.md", "In VS Code"],
  ["kiro.md", "In Kiro"],
]);

test("GUIDE_SECTION_TITLES: every section the table names has its guide heading's title", () => {
  const sections = new Set([
    ...TABLE.clients.map((client) => client.guideSection),
    ...TABLE.conflicts.map((conflict) => conflict.guideSection),
  ]);
  assert.deepEqual([...GUIDE_SECTION_TITLES.keys()].sort(), [...sections].sort());
  for (const section of sections) {
    const known = GUIDE_SECTION_TITLES.get(section);
    assert.ok(known !== undefined, section);
    const [page, anchor] = section.split("#");
    const pageTitle = GUIDE_PAGE_TITLES.get(page);
    assert.ok(pageTitle !== undefined, page);
    assert.equal(known.page, pageTitle);
    const pagePath = path.join(PROJECT_ROOT, "..", "docs", "guide", page);
    const guide = fs.readFileSync(pagePath, "utf8");
    assert.match(guide, new RegExp(`^# ${pageTitle}\\b`, "m"));
    assert.ok(
      guide
        .split("\n")
        .some((line) => /^#+ /.test(line) && line.endsWith(` ${known.title} { #${anchor} }`)),
      section,
    );
  }
  assert.equal(CLIENT_FILES_GUIDE_SECTION, "Codex and Claude Code in VS Code");
});

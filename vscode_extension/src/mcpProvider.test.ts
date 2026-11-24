// Unit tests for the MCP server definitions this extension publishes.
//
// The SWCraft MCP server reaches VS Code through a *provider*, not a generated
// `.vscode/mcp.json`: the extension answers `provideMcpServerDefinitions` with one
// `McpStdioServerDefinition` per workspace folder that has a manifest, spawning the same
// `swcraft` executable every other command spawns.
//
// The module under test owns the whole decision — which folders get a server, the command
// line, the environment keys — and never imports `vscode`, so it runs under plain
// `node --test`. `commands.ts` turns a descriptor into the API's class and backs the change
// event with a `vscode.EventEmitter`.

import assert from "node:assert/strict";
import * as path from "node:path";
import { test } from "node:test";

import {
  MCP_PROVIDER_ID,
  MCP_SERVER_LABEL,
  planMcpServerDefinitions,
  REVIEW_MODE_ENV_KEY,
  REVIEW_MODE_ENV_VALUE,
  SwcraftMcpServerProvider,
} from "./mcpProvider";
import { MANIFEST_FILENAME } from "./project";
import {
  assertNoProductionModuleNames,
  readManifest,
  readSource,
  spy,
  withoutCommentLines,
} from "./testSupport";

const EXECUTABLE = "/Users/dev/.swcraft/current/bin/swcraft";
//: Deliberately **not** a release: a fixture equal to the shipped version would read as a
//: lockstep claim this file makes no part of (the rule `toolchain.test.ts` states beside its
//: own version fixtures).
const VERSION = "9.9.9";

function manifestIn(folder: string): string {
  return path.join(folder, MANIFEST_FILENAME);
}

/**
 * A `planMcpServerDefinitions` input for one healthy project folder, with per-case overrides
 * (the convention of `healthCheck.test.ts`'s `toolchainInput()`). `folders` defaults to the
 * one project folder most cases use; a case about several folders, or none, passes its own.
 */
function planInput(
  overrides: Partial<Parameters<typeof planMcpServerDefinitions>[0]> = {},
): Parameters<typeof planMcpServerDefinitions>[0] {
  return {
    folders: ["/work/vehicle-arch"],
    executable: EXECUTABLE,
    version: VERSION,
    toolchainPresent: true,
    ...overrides,
  };
}

// --- one definition per folder that has a root manifest ----------------------

test("planMcpServerDefinitions: a folder with a root manifest yields exactly one server", () => {
  const definitions = planMcpServerDefinitions(planInput(), () => true);
  assert.equal(definitions.length, 1);
  assert.equal(definitions[0].label, MCP_SERVER_LABEL);
});

test("planMcpServerDefinitions: a folder without a manifest yields no server at all", () => {
  // The extension activates on `workspaceContains:swcraft.yaml`, but a
  // multi-root window activates on *any* folder matching — the other folders
  // must not each get a server pointed at a manifest that is not there.
  assert.deepEqual(
    planMcpServerDefinitions(planInput({ folders: ["/work/not-a-project"] }), () => false),
    [],
  );
});

test("planMcpServerDefinitions: no folder open yields no server, and probes nothing", () => {
  const probe = spy(() => true);
  assert.deepEqual(planMcpServerDefinitions(planInput({ folders: [] }), probe.exists), []);
  assert.deepEqual(probe.probed, []);
});

test("planMcpServerDefinitions: only the workspace ROOT's manifest is probed", () => {
  // A nested `sub/swcraft.yaml` registers no server, and that is stated rather
  // than fixed: `activationEvents: workspaceContains:swcraft.yaml` already
  // assumes the root manifest, and `project.ts`'s `findManifest()` looks
  // nowhere else. The spy is the assertion — a comment could not say that only
  // one candidate was ever considered.
  const probe = spy(() => false);
  planMcpServerDefinitions(planInput(), probe.exists);
  assert.deepEqual(probe.probed, [manifestIn("/work/vehicle-arch")]);
});

test("planMcpServerDefinitions: a multi-root window gets one server per project folder", () => {
  const definitions = planMcpServerDefinitions(
    planInput({ folders: ["/work/vehicle-arch", "/work/notes", "/work/braking"] }),
    (candidate) => candidate !== manifestIn("/work/notes"),
  );
  assert.deepEqual(
    definitions.map((definition) => definition.cwd),
    ["/work/vehicle-arch", "/work/braking"],
  );
});

test("planMcpServerDefinitions: two folders with the same name are told apart", () => {
  // `path.basename` is not unique across roots: `/work/a/vehicle` and `/work/b/vehicle` would
  // both render `SWCraft (vehicle)`. When the names collide the whole path is the label: long,
  // and unambiguous.
  const definitions = planMcpServerDefinitions(
    planInput({ folders: ["/work/a/vehicle", "/work/b/vehicle", "/work/braking"] }),
    () => true,
  );
  assert.deepEqual(
    definitions.map((definition) => definition.label),
    [
      `${MCP_SERVER_LABEL} (/work/a/vehicle)`,
      `${MCP_SERVER_LABEL} (/work/b/vehicle)`,
      `${MCP_SERVER_LABEL} (braking)`,
    ],
  );
});

test("planMcpServerDefinitions: two servers carry distinguishable labels", () => {
  // One label for one server, the folder's name added only when a second
  // server would otherwise be indistinguishable in VS Code's server picker.
  const definitions = planMcpServerDefinitions(
    planInput({ folders: ["/work/vehicle-arch", "/work/braking"] }),
    () => true,
  );
  assert.deepEqual(
    definitions.map((definition) => definition.label),
    [`${MCP_SERVER_LABEL} (vehicle-arch)`, `${MCP_SERVER_LABEL} (braking)`],
  );
});

// --- the command line --------------------------------------------------------

test("planMcpServerDefinitions: the command is the resolved swcraft and the args are mcp-serve", () => {
  const [definition] = planMcpServerDefinitions(planInput(), () => true);
  assert.equal(definition.command, EXECUTABLE);
  assert.deepEqual(definition.args, ["mcp-serve", "--project", manifestIn("/work/vehicle-arch")]);
});

test("planMcpServerDefinitions: --project is ABSOLUTE, never relative to any cwd", () => {
  // `cmd_mcp_serve` stores `--project` **raw** into `SWCRAFT_MANIFEST`
  // (packages/tools/src/swcraft_tools/cli/handover.py), so a relative path resolved under
  // whatever cwd the extension host happens to have would break the server
  // rather than merely look untidy.
  const [definition] = planMcpServerDefinitions(planInput(), () => true);
  const project = definition.args[definition.args.indexOf("--project") + 1];
  assert.equal(path.isAbsolute(project), true, `--project ${project} is not absolute`);
  assert.equal(project, manifestIn("/work/vehicle-arch"));
});

test("planMcpServerDefinitions: cwd is the workspace folder", () => {
  const [definition] = planMcpServerDefinitions(planInput(), () => true);
  assert.equal(definition.cwd, "/work/vehicle-arch");
});

// `planMcpServerDefinitions` hands `version` back untouched; which string that is, is decided
// in `commands.ts` (`extensionVersion(context)` off the extension's own `packageJSON`) and is
// asserted nowhere. The fixture is deliberately not a release.
test("planMcpServerDefinitions: the version it is given is the version it publishes", () => {
  const [definition] = planMcpServerDefinitions(planInput({ version: "4.5.6" }), () => true);
  assert.equal(definition.version, "4.5.6");
});

// `Install Toolchain` fires `refresh()` because the executable may not have existed when VS
// Code last asked. The published set must differ then, or VS Code diffs it to "no change" and
// does not retry a server it failed to start (the walkthrough's flow: Install Toolchain, then
// chat).
test("planMcpServerDefinitions: an absent toolchain is a different published set", () => {
  const absent = planMcpServerDefinitions(planInput({ toolchainPresent: false }), () => true);
  const present = planMcpServerDefinitions(planInput(), () => true);
  assert.notDeepEqual(absent, present, "absent -> present must be a change VS Code can see");
  assert.notEqual(absent[0].version, present[0].version);
});

test("planMcpServerDefinitions: an absent toolchain changes the version and nothing else", () => {
  // The server is still published: the command, the arguments and the label are
  // what a failing start must name, and an unpublished server would report
  // nothing at all. It is the *version* that carries the change, which is the
  // field VS Code documents as "the tools may have changed".
  const [absent] = planMcpServerDefinitions(planInput({ toolchainPresent: false }), () => true);
  const [present] = planMcpServerDefinitions(planInput({ toolchainPresent: true }), () => true);
  assert.deepEqual({ ...absent, version: undefined }, { ...present, version: undefined });
  assert.equal(present.version, VERSION);
  assert.ok(absent.version.startsWith(`${VERSION}+`), absent.version);
});

// --- the environment ---------------------------------------------------------

test("planMcpServerDefinitions: env carries PYTHONUTF8 beside the review mode", () => {
  // Every other spawn in this extension sets it (`cliEnv()`), and it matters most here: on
  // Windows Python >= 3.11 writes a piped stdout in the locale encoding unless UTF-8 mode is
  // on, and the MCP server's whole conversation is a pipe (a traceback with a non-ASCII project
  // path would come back mojibake).
  const [definition] = planMcpServerDefinitions(planInput(), () => true);
  assert.equal(definition.env.PYTHONUTF8, "1");
});

test("planMcpServerDefinitions: env sets SWCRAFT_REVIEW=panel and exactly one key beside it", () => {
  // The review boundary: `panel` makes the server put the computed delta to the human in the
  // SWCraft review tab, one mechanism for every client (`elicit` is used by no VS Code
  // registration SWCraft ships, only by a `swcraft agent --script` replay); its absence is the
  // fail-closed `stage-only` default where nothing ever commits. The whole key **set** is
  // asserted: an extra key would reach the server's process environment silently.
  const [definition] = planMcpServerDefinitions(planInput(), () => true);
  assert.deepEqual(definition.env, {
    [REVIEW_MODE_ENV_KEY]: REVIEW_MODE_ENV_VALUE,
    PYTHONUTF8: "1",
  });
  assert.deepEqual(Object.keys(definition.env).sort(), ["PYTHONUTF8", "SWCRAFT_REVIEW"]);
  assert.equal(definition.env.SWCRAFT_REVIEW, "panel");
});

test("planMcpServerDefinitions: SWCRAFT_MANIFEST is NOT an env key — --project carries it", () => {
  // `cmd_mcp_serve` sets it from `--project` before it execs, so setting it here would be a
  // second declaration of the same fact with nothing keeping the two equal.
  const [definition] = planMcpServerDefinitions(planInput(), () => true);
  assert.equal("SWCRAFT_MANIFEST" in definition.env, false);
});

// --- the provider class and its change event ---------------------------------

test("SwcraftMcpServerProvider: definitions() re-reads its inputs on every call", () => {
  // The folder set and the resolved executable both change while a window is
  // open (a folder added to the workspace, `swcraft.executable` edited, a
  // toolchain installed). Reading them through a callback rather than
  // capturing them at construction is what makes the next call answer the
  // current state.
  let folders: string[] = [];
  const provider = new SwcraftMcpServerProvider(
    { fire: () => {} },
    () => planInput({ folders }),
    () => true,
  );
  assert.deepEqual(provider.definitions(), []);
  folders = ["/work/vehicle-arch"];
  assert.equal(provider.definitions().length, 1);
});

test("SwcraftMcpServerProvider: refresh() fires the change signal, and nothing else does", () => {
  let fired = 0;
  const provider = new SwcraftMcpServerProvider(
    { fire: () => fired++ },
    () => planInput(),
    () => true,
  );
  provider.definitions();
  assert.equal(fired, 0, "answering a definition request must not claim the set changed");
  provider.refresh();
  assert.equal(fired, 1);
  provider.refresh();
  assert.equal(fired, 2);
});

// --- the manifest declaration ------------------------------------------------

test("package.json declares mcpServerDefinitionProviders with the id the code registers", () => {
  // `vscode.lm.registerMcpServerDefinitionProvider` requires the contribution
  // point to declare the same id; a mismatch is not a compile error and not a
  // runtime throw — the provider is simply never consulted.
  const providers = readManifest().contributes?.mcpServerDefinitionProviders ?? [];
  assert.equal(providers.length, 1, "SWCraft publishes exactly one provider");
  assert.equal(providers[0].id, MCP_PROVIDER_ID);
  assert.equal(providers[0].label, MCP_SERVER_LABEL);
});

test("commands.ts registers the provider under the id package.json declares", () => {
  // Read out of the source rather than by importing `commands.ts`, which
  // imports `vscode` — the same route `packageCommands.test.ts` takes for the
  // command ids.
  const source = readSource("src/commands.ts");
  assert.match(
    source,
    /registerMcpServerDefinitionProvider\(\s*MCP_PROVIDER_ID/,
    "commands.ts must register the provider under the shared MCP_PROVIDER_ID constant",
  );
});

test("commands.ts carries the descriptor's cwd onto the definition it publishes", () => {
  // `cwd` is load-bearing: the server is spawned there, and it is the one field
  // `McpStdioServerDefinition`'s constructor does not take — it is assigned afterwards, so
  // deleting the assignment is neither a compile error nor a runtime throw, and the pure
  // planner's `cwd` case above never reaches the adapter. Structural, because `commands.ts`
  // imports `vscode`.
  const source = withoutCommentLines(readSource("src/commands.ts"));
  assert.match(
    source,
    /definition\.cwd\s*=\s*vscode\.Uri\.file\(descriptor\.cwd\)/,
    "toStdioDefinition must put the descriptor's cwd on the definition it returns",
  );
});

test("activate() registers the MCP provider", () => {
  // Registration must happen during activation — VS Code consults the provider
  // when a chat message is submitted, and an extension that registers later
  // publishes nothing on that turn.
  const source = readSource("src/extension.ts");
  assert.match(source, /registerMcpServerProvider\(context\)/);
});

test("activate() survives an MCP registration that throws", () => {
  // `registerMcpServerProvider()` runs first, because its return value is
  // `registerCommands()`'s argument, so a throw from
  // `vscode.lm.registerMcpServerDefinitionProvider` (an older VS Code, a withdrawn proposed
  // API, a host that does not implement it) would abort activation and register no commands,
  // `SWCraft: Install Toolchain` included. Structural: `extension.ts` imports `vscode`.
  const source = readSource("src/extension.ts");
  assert.match(
    source,
    /try \{\n\s*mcpProvider = registerMcpServerProvider\(context\);/,
    "the MCP registration must be attempted inside a try",
  );
  assert.match(
    source,
    /catch \(error\) \{[\s\S]*inertMcpProvider\(\)/,
    "and a throw must leave an inert provider behind so the commands still register",
  );
  assert.match(
    source,
    /registerCommands\(context, mcpProvider\)/,
    "the commands are registered either way",
  );
});

test("no module writes .vscode/mcp.json any more", () => {
  // The generated file is gone, not merely unused: asserted over every production module, so a
  // re-introduction anywhere fails here. The guard names VS Code's file, `.vscode/mcp.json`, in
  // the spellings a module would use (a path text, or `"mcp.json"` as a path component), not
  // every `mcp.json`: onboarding writes Claude Code's `.mcp.json` from the asset
  // `swcraft.mcp.json`, neither of which is VS Code's configuration.
  assertNoProductionModuleNames(
    [".vscode/mcp.json", '"mcp.json"', "'mcp.json'", "`mcp.json`"],
    "the extension registers the server itself now",
  );
});

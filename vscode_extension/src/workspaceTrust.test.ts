// `capabilities` in package.json: Workspace Trust and virtual workspaces.
//
// With the folder untrusted, the SWCraft view container is absent from the Activity Bar.
// SWCraft does not support untrusted workspaces, and VS Code 1.134.0 resolves an extension
// with a `main` and without the declaration to "not supported" as well
// (`getExtensionUntrustedWorkspaceSupportType` in `workbench.desktop.main.js`). `description` is
// the field VS Code renders in the Restricted Mode UI, and it applies only when `supported` is
// not `true`: it explains why the view is gone.
//
// Why `false` and not `"limited"` is the subject of the tests below. No `vscode` import: this
// runs under plain `node --test`, over the manifest and the sources as text.
import assert from "node:assert/strict";
import { test } from "node:test";
import { DIAGRAM_EDITOR_COMMANDS } from "./diagramsTab";
import { IMPACT_NON_SPAWNING_COMMANDS } from "./impactCore";
import { RESOURCE_NON_SPAWNING_COMMANDS } from "./resourceCore";
import { REVIEW_VIEW_COMMANDS } from "./reviewCore";
import {
  type CapabilityDeclaration,
  functionBody,
  readManifest,
  readSource,
  withoutCommentLines,
} from "./testSupport";
import { TRACE_NON_SPAWNING_COMMANDS } from "./traceCore";

/**
 * The VS Code APIs an extension must use to be honestly `"limited"`.
 *
 * `"limited"` means "enabled in an untrusted workspace with some functionality disabled", and
 * VS Code disables *nothing* on the extension's behalf beyond ignoring the settings named in
 * `restrictedConfigurations`; the rest is the extension's own job, through these two APIs.
 * Their absence from the sources makes `"limited"` a lie for SWCraft, and the pairing is
 * asserted in both directions below.
 */
const TRUST_GUARD_APIS = ["workspace.isTrusted", "onDidGrantWorkspaceTrust"];

/** The two VS Code adapter files — the only ones that may `import "vscode"`. */
const ADAPTER_FILES = ["src/commands.ts", "src/extension.ts"];

function untrustedWorkspaces(): CapabilityDeclaration {
  const declaration = readManifest().capabilities?.untrustedWorkspaces;
  assert.ok(
    declaration !== undefined,
    "package.json declares no capabilities.untrustedWorkspaces. Without it VS Code disables the " +
      "extension in Restricted Mode and says nothing — the SWCraft view simply is not there. " +
      "See this file's header.",
  );
  return declaration;
}

test("the extension declares a main entry point, which is what makes the trust default apply", () => {
  // `getExtensionUntrustedWorkspaceSupportType` short-circuits to `true` for an
  // extension with no `main`: a declaration-only extension runs no code, so
  // there is nothing to gate. SWCraft has one, so the declaration below is
  // load-bearing rather than decorative — and if `main` ever went away, this
  // test says why the rest of the file stopped mattering.
  assert.equal(typeof readManifest().main, "string");
});

test("SWCraft is not supported in untrusted workspaces, and says why", () => {
  const declaration = untrustedWorkspaces();
  assert.equal(
    declaration.supported,
    false,
    "SWCraft spawns the installed toolchain against the open folder's model, and publishes an " +
      "MCP server definition that tells VS Code how to start a second process against that same " +
      "workspace. Running a process over workspace content is exactly what Workspace Trust " +
      "exists to gate.",
  );
  const description = declaration.description ?? "";
  assert.notEqual(
    description.trim(),
    "",
    "supported:false without a description restores the silent disappearance the seated run " +
      "found — VS Code shows this string in the Restricted Mode UI and shows nothing without it",
  );
  // The description exists to answer "why is it gone, and what do I do?", so
  // both halves are pinned rather than the field's mere presence.
  assert.ok(
    /swcraft|executable|MCP server/.test(description),
    "the description must name what SWCraft runs against the workspace; that is the reason",
  );
  assert.ok(
    /[Tt]rust the folder/.test(description),
    "the description must tell the reader how to get the extension back",
  );
});

test("virtual workspaces are declared unsupported too, for a separate reason", () => {
  // The same missing-key defect: `getExtensionVirtualWorkspaceSupportType` defaults an
  // undeclared extension to `true`, which would claim SWCraft works in a workspace with no
  // on-disk resources. Every command it has spawns a local process against an `fsPath`.
  const declaration = readManifest().capabilities?.virtualWorkspaces;
  assert.ok(declaration !== undefined, "package.json declares no capabilities.virtualWorkspaces");
  assert.notEqual(
    typeof declaration,
    "boolean",
    "the object form is used so the refusal can carry a description; a bare `false` explains nothing",
  );
  const object = declaration as CapabilityDeclaration;
  assert.equal(object.supported, false);
  assert.notEqual((object.description ?? "").trim(), "");
});

test("restrictedConfigurations is absent, because supported is false and not 'limited'", () => {
  // VS Code reads it only when `supported` is `"limited"` (the configuration extension point
  // copies it into `restrictedProperties` under exactly that condition), and SWCraft's is
  // `false`: the whole extension is disabled in an untrusted window, so declaring the key would
  // be dead weight that reads like a gate. Of the settings SWCraft contributes,
  // `swcraft.executable` is `scope: machine`, which a workspace cannot set at all; each
  // experiment kind's configuration file (`swcraft.experiments.allocation.config`), the
  // requirements import's mapping file (`swcraft.requirementsImport.mapping`) and the test
  // tables' annotations folder (`swcraft.testTables.annotations`) are `scope: resource` paths
  // the CLI reads as data, never a program, and only in a trusted window —
  // `toolchain.test.ts` pins every scope.
  assert.equal(untrustedWorkspaces().restrictedConfigurations, undefined);
});

test('"limited" is refused while the sources carry no trust guard', () => {
  // The two-sided invariant. `"limited"` keeps the extension *enabled* in an untrusted window:
  // every command stays registered and the tree view clickable, so a `"limited"` declaration
  // with no guard would spawn the workspace's own Python in a folder the user declined to
  // trust. So the declaration and the guards move together: flip to `"limited"` and this test
  // demands the guards first; add the guards and it demands the declaration be reconsidered.
  const sources = ADAPTER_FILES.map(readSource).join("\n");
  const guarded = TRUST_GUARD_APIS.filter((api) => sources.includes(api));
  const supported = untrustedWorkspaces().supported;

  if (guarded.length === 0) {
    assert.equal(
      supported,
      false,
      `capabilities.untrustedWorkspaces.supported is ${JSON.stringify(supported)}, but neither ` +
        `${TRUST_GUARD_APIS.join(" nor ")} appears in ${ADAPTER_FILES.join(" / ")}. Nothing in ` +
        "the extension refuses to spawn in an untrusted folder, so anything other than `false` " +
        "is a claim the code does not honour.",
    );
    return;
  }
  assert.notEqual(
    supported,
    false,
    `${ADAPTER_FILES.join(" / ")} now uses ${guarded.join(", ")}, so the extension can behave ` +
      "differently in an untrusted folder. Revisit supported:false — and if it should stay, say " +
      "here why the guard exists for something else.",
  );
});

/**
 * Registered commands that reach no spawn, each with the reason it is allowed while
 * `untrustedWorkspaces: supported: false` stays:
 *
 * - the Trace Links view's commands that only edit the proposals file or open a file
 *   (`TRACE_NON_SPAWNING_COMMANDS`): the view's search and Apply spawn the toolchain, so in a
 *   restricted window it has nothing to show, and keeping the extension enabled for nine
 *   editors of one JSON file would leave the surface the test below warns about;
 * - the Pending Change Sets view's two commands (`REVIEW_VIEW_COMMANDS`), which re-read the
 *   reviews directory and open a review tab: in a restricted window no review tab opens, and
 *   the assistant's review waits, with no deadline, until its call is cancelled or its server
 *   ends, which abandons the change set;
 * - the Resources and Impact views' row clicks (`RESOURCE_NON_SPAWNING_COMMANDS`,
 *   `IMPACT_NON_SPAWNING_COMMANDS`), which open what a run drew, and no run happens there;
 * - Show in Diagram (`DIAGRAM_EDITOR_COMMANDS`), which shows a page a `generate diagrams` run
 *   wrote; its **Generate Diagrams** button runs the press through `swcraft.diagrams`, which
 *   spawns.
 */
const NON_SPAWNING_COMMANDS: readonly string[] = [
  ...TRACE_NON_SPAWNING_COMMANDS,
  ...REVIEW_VIEW_COMMANDS,
  ...RESOURCE_NON_SPAWNING_COMMANDS,
  ...IMPACT_NON_SPAWNING_COMMANDS,
  ...DIAGRAM_EDITOR_COMMANDS,
];

test("every command that spawns nothing is named, and none of them keeps limited support alive", () => {
  // The argument weighed and rejected: commands that spawn nothing (the Trace Links view's,
  // which write decisions into the proposals file or open files) could in principle be kept
  // alive by `"limited"`. It buys nothing: they edit what a search wrote, no search runs in a
  // restricted window, and an enabled extension would leave a surface where the next command
  // added inherits trust-free spawn rights by default.
  //
  // What this asserts is the fact that rests on, for every registered command: each handler
  // reaches, through the top-level functions of commands.ts, a `spawn(` or the
  // `resolveSwcraftExecutable(` a spawn runs — or is named in NON_SPAWNING_COMMANDS, which
  // must then really reach neither.
  //
  // What it does NOT reach: it is a text call graph, not a type-checked one. A call through a
  // method, a variable or an imported function is not followed (so `openGuideCommand` counts
  // through its `resolveSwcraftExecutable(`, not through the session that spawns), a reachable
  // call on a branch that never runs counts all the same, and a handler registered in any
  // shape but `registerCommand("id", () => name(…))`, `registerCommand("id", (arg) =>
  // name(…, arg))` or the *impact* lens's `(arg, lensUri) => name(…, arg, lensUri)` fails the
  // count below rather than being read.
  const code = withoutCommentLines(readSource("src/commands.ts"));
  const declarations = new Map<string, string>();
  for (const match of code.matchAll(/^(?:export )?(?:async )?function (\w+)\(/gm)) {
    declarations.set(match[1], functionBody(code, match[0]));
  }
  assert.ok(declarations.size > 10, `only ${declarations.size} functions read from commands.ts`);

  // `notifyWithHelp` is not followed: the one spawn it reaches is the guide's
  // (`openGuidePage` → `swcraft docs`), started only when the reader presses a
  // refusal's *Learn more* — the same spawn the folder-less `swcraft.openGuide`
  // makes, which is listed as spawning below. Followed, it would make every
  // command that refuses without a project "reach a spawn" through its own
  // refusal, and the list above would say nothing about the command's work.
  const NOT_FOLLOWED = new Set(["notifyWithHelp"]);
  const reaches = (name: string, seen = new Set<string>()): boolean => {
    if (seen.has(name) || NOT_FOLLOWED.has(name)) {
      return false;
    }
    seen.add(name);
    const body = declarations.get(name) ?? "";
    if (/\bspawn\(|\bresolveSwcraftExecutable\(/.test(body.slice(body.indexOf("{")))) {
      return true;
    }
    return [...declarations.keys()].some(
      (callee) =>
        callee !== name && new RegExp(`\\b${callee}\\(`).test(body) && reaches(callee, seen),
    );
  };

  const handlers = [
    ...code.matchAll(/registerCommand\(\s*"([^"]+)",\s*\((?:\w+(?:,\s*\w+)?)?\)\s*=>\s*(\w+)\(/g),
  ].map((match) => ({ id: match[1], handler: match[2] }));
  assert.equal(
    handlers.length,
    (code.match(/registerCommand\(/g) ?? []).length,
    "every registration is read in the `() => handler(…)` or `(arg) => handler(…, arg)` shape",
  );
  assert.ok(handlers.length > 0);
  for (const { id, handler } of handlers) {
    assert.ok(
      declarations.has(handler),
      `${id}'s handler ${handler} is not declared in commands.ts`,
    );
    assert.equal(
      reaches(handler),
      !NON_SPAWNING_COMMANDS.includes(id),
      NON_SPAWNING_COMMANDS.includes(id)
        ? `${id} is listed as spawning nothing but reaches a spawn — take it off the list`
        : `${id} (${handler}) reaches no spawn; the "no command only opens a file" argument in this ` +
            "file is stale — list it in NON_SPAWNING_COMMANDS and revisit supported:false",
    );
  }
});

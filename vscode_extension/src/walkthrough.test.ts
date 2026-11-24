// The `contributes.walkthroughs` entry of package.json: "The first hour with SWCraft".
//
// The walkthrough is VS Code's native contribution point: a manifest entry plus one Markdown file
// per step under `media/walkthrough/`, with no webview, no extra activation event (VS Code
// generates `onWalkthrough:<id>`) and no command behind it, so only the manifest and the files it
// points at are tested here. Packaging is `packageVsix.test.ts`'s media inventory; whether a real
// Extension Host renders the steps and ticks their checkmarks is checked by hand.
//
// No `vscode` import: this runs under plain `node --test`.
import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as path from "node:path";
import { test } from "node:test";

import {
  PROJECT_ROOT,
  productionModules,
  readManifest,
  readSource,
  type WalkthroughStep,
  walkthroughMediaPaths,
  walkthroughs,
} from "./testSupport";

/**
 * How many steps the walkthrough ships. The guide step is last: the reader who has finished the
 * first hour is the one who then needs the reference. A step count that drifts on its own is the
 * failure this pins.
 */
const STEP_COUNT = 7;

function theWalkthrough(): {
  id: string;
  title: string;
  description: string;
  steps: WalkthroughStep[];
} {
  const declared = walkthroughs();
  assert.equal(
    declared.length,
    1,
    `package.json must declare exactly one walkthrough, got ${declared.length}`,
  );
  return declared[0];
}

function declaredCommandIds(): Set<string> {
  const commands = readManifest().contributes?.commands;
  assert.ok(Array.isArray(commands), "package.json must carry contributes.commands as an array");
  return new Set(commands.map((entry) => entry.command));
}

function declaredViewIds(): Set<string> {
  const views = readManifest().contributes?.views;
  assert.ok(views !== undefined, "package.json must carry contributes.views");
  return new Set(Object.values(views).flatMap((container) => container.map((view) => view.id)));
}

/** The two VS Code adapter files — the only ones allowed to `import "vscode"`. */
const ADAPTER_FILES = ["src/commands.ts", "src/extension.ts"];

/**
 * Every context key the extension sets, read out of the source rather than
 * restated here.
 *
 * Two greps, because the key is spelled in two places by design: the literal is
 * an `export const` in a pure module (layerFile.ts's `LAYER_FILE_CONTEXT_KEY`),
 * and the `setContext` call is in an adapter file. A call whose key is an
 * unresolvable identifier fails loudly rather than being skipped — otherwise
 * this guard would quietly go vacuous the first time the spelling changed.
 */
function contextKeysTheExtensionSets(): Set<string> {
  const constants = new Map<string, string>();
  for (const file of productionModules()) {
    for (const match of readSource(path.posix.join("src", file)).matchAll(
      /export const ([A-Za-z_$][\w$]*)\s*=\s*"([^"]+)"/g,
    )) {
      constants.set(match[1], match[2]);
    }
  }
  const keys = new Set<string>();
  for (const file of ADAPTER_FILES) {
    for (const match of readSource(file).matchAll(
      /"setContext",\s*(?:"([^"]+)"|([A-Za-z_$][\w$]*))/g,
    )) {
      if (match[1] !== undefined) {
        keys.add(match[1]);
        continue;
      }
      const resolved = constants.get(match[2]);
      assert.ok(
        resolved !== undefined,
        `${file} sets a context key from "${match[2]}", which no pure module exports as a string literal`,
      );
      keys.add(resolved);
    }
  }
  return keys;
}

test("the walkthrough declares a non-empty id, title and description", () => {
  const walkthrough = theWalkthrough();
  for (const [key, value] of [
    ["id", walkthrough.id],
    ["title", walkthrough.title],
    ["description", walkthrough.description],
  ] as const) {
    assert.equal(typeof value, "string", `the walkthrough's ${key} must be a string`);
    assert.notEqual(value.trim(), "", `the walkthrough's ${key} must not be empty`);
  }
  // The title is what the Welcome page lists the walkthrough under.
  assert.equal(walkthrough.title, "The first hour with SWCraft");
});

test("the walkthrough ships exactly seven steps, in the taught order", () => {
  // The order is pinned rather than only counted: a step inserted in the middle would still count
  // seven.
  const steps = theWalkthrough().steps;
  assert.equal(steps.length, STEP_COUNT);
  assert.deepEqual(
    steps.map((step) => step.id),
    [
      "findThePanel",
      "onboardProject",
      "editALayerFile",
      "validate",
      "seeTheDiagrams",
      "talkToTheModel",
      "openTheGuide",
    ],
  );
});

test("every walkthrough step id is unique", () => {
  const ids = theWalkthrough().steps.map((step) => step.id);
  assert.deepEqual(
    ids,
    [...new Set(ids)],
    `walkthrough step ids must be unique, got: ${ids.join(", ")}`,
  );
});

test("every walkthrough step declares a non-empty id and title", () => {
  for (const step of theWalkthrough().steps) {
    assert.equal(typeof step.id, "string");
    assert.notEqual(step.id.trim(), "", "a walkthrough step must declare an id");
    assert.equal(typeof step.title, "string", `step "${step.id}" must declare a title`);
    assert.notEqual(step.title.trim(), "", `step "${step.id}" must declare a non-empty title`);
  }
});

test("every walkthrough step's media is the markdown variant", () => {
  // The contribution point also admits `image` and `svg`. Asserted here
  // because `walkthroughMediaPaths()` silently drops any step that is not
  // markdown, which would make both the existence check below and
  // packageVsix.test.ts's inventory assertion vacuous for that step.
  const steps = theWalkthrough().steps;
  for (const step of steps) {
    assert.equal(
      typeof step.media?.markdown,
      "string",
      `step "${step.id}" must declare media.markdown (the walkthrough ships Markdown, not images)`,
    );
  }
  assert.equal(
    walkthroughMediaPaths().length,
    steps.length,
    "every declared step must contribute exactly one media path",
  );
});

test("every walkthrough step's media.markdown file exists on disk", () => {
  for (const mediaPath of walkthroughMediaPaths()) {
    assert.ok(
      !path.isAbsolute(mediaPath) && !mediaPath.startsWith("./"),
      `media.markdown "${mediaPath}" must be a plain extension-relative path (VS Code resolves it with joinPath(extensionUri, path))`,
    );
    const resolved = path.join(PROJECT_ROOT, mediaPath);
    assert.ok(
      fs.existsSync(resolved),
      `media.markdown "${mediaPath}" does not resolve to a file: ${resolved}`,
    );
    assert.notEqual(
      fs.readFileSync(resolved, "utf8").trim(),
      "",
      `media.markdown "${mediaPath}" is empty — an empty step teaches nothing`,
    );
  }
});

test("every walkthrough step declares at least one completion event", () => {
  for (const step of theWalkthrough().steps) {
    assert.ok(
      Array.isArray(step.completionEvents) && step.completionEvents.length > 0,
      `step "${step.id}" declares no completionEvents — its checkmark could never be earned`,
    );
  }
});

/**
 * Command ids a step may complete on that SWCraft does **not** contribute, with where each one was
 * measured. An `onCommand:` step progresses on `commandService.onDidExecuteCommand`, which fires
 * for *every* command the workbench runs (VS Code 1.134.0, `initCompletionEventListeners()`), so a
 * host command is usable here; the allowlist stays small and argued because nothing in this
 * repository renames or keeps alive an id outside SWCraft's namespace.
 */
const HOST_COMMAND_IDS = new Map<string, string>([
  [
    "workbench.action.chat.open",
    "VS Code core, 1.134.0: a workbench chat action (not an extension command — it survives " +
      "`--disable-extensions`), and the id VS Code's own built-in Copilot walkthrough links to " +
      "from a step body. Its presence in a real host is measured by " +
      "src/test/integration/smoke.test.ts rather than asserted from the bundle here.",
  ],
]);

/** The `command:<id>` links a step's own description offers the reader. */
function commandLinksIn(step: WalkthroughStep): Set<string> {
  const links = new Set<string>();
  for (const match of (step.description ?? "").matchAll(/\]\(command:(?:toSide:)?([^)?]+)/g)) {
    links.add(match[1]);
  }
  return links;
}

test("every completionEvents onCommand: in SWCraft's namespace names a declared contributes.commands id", () => {
  // The failure this prevents: a step that teaches a command, spelled with a
  // typo or left behind by a renamed command, whose checkmark can never tick.
  const commandIds = declaredCommandIds();
  for (const step of theWalkthrough().steps) {
    for (const event of step.completionEvents ?? []) {
      if (!event.startsWith("onCommand:")) {
        continue;
      }
      const commandId = event.slice("onCommand:".length);
      if (!commandId.startsWith("swcraft.")) {
        assert.ok(
          HOST_COMMAND_IDS.has(commandId),
          `step "${step.id}" completes on "${event}", which is neither one of SWCraft's own ` +
            "commands nor in HOST_COMMAND_IDS. Add it there with where you measured it, or use " +
            "an id this extension contributes.",
        );
        continue;
      }
      assert.ok(
        commandIds.has(commandId),
        `step "${step.id}" completes on "${event}", but package.json's contributes.commands declares no "${commandId}"`,
      );
    }
  }
});

test("a step completing on a host command offers that command in its own description", () => {
  // The honesty rule for the allowlist above. `onCommand:` fires on *any*
  // execution of the id, from anywhere in the window — so a step that names a
  // host command without offering it would tick on something it never asked the
  // reader to do. A single-link paragraph renders as a button on the step
  // (1.134.0's `buildMarkdownDescription`), so this is a control the reader
  // clicks, not prose they scroll past.
  let checked = 0;
  for (const step of theWalkthrough().steps) {
    for (const event of step.completionEvents ?? []) {
      if (!event.startsWith("onCommand:")) {
        continue;
      }
      const commandId = event.slice("onCommand:".length);
      if (!HOST_COMMAND_IDS.has(commandId)) {
        continue;
      }
      assert.ok(
        commandLinksIn(step).has(commandId),
        `step "${step.id}" completes on "${event}" but its description carries no ` +
          `[…](command:${commandId}) link — the checkmark would tick on something the step ` +
          "never offered.",
      );
      checked += 1;
    }
  }
  assert.ok(checked > 0, "no step completes on a host command — this guard would be vacuous");
});

test("every completionEvents onView: names a declared contributes.views id", () => {
  // Step 1 teaches no command — it teaches where the panel is — so it completes
  // on `onView:swcraftView`, the view being opened, which is the honest event
  // for that step. The same drift guard as the onCommand: one above: the view
  // id is declared in exactly one other place and a rename must not leave the
  // step uncompletable.
  const viewIds = declaredViewIds();
  for (const step of theWalkthrough().steps) {
    for (const event of step.completionEvents ?? []) {
      if (!event.startsWith("onView:")) {
        continue;
      }
      const viewId = event.slice("onView:".length);
      assert.ok(
        viewIds.has(viewId),
        `step "${step.id}" completes on "${event}", but package.json's contributes.views declares no "${viewId}"`,
      );
    }
  }
});

test("every completion event is one of the kinds the contribution point defines", () => {
  // Measured against VS Code 1.134.0's `walkthroughs` extension point schema:
  // the completionEvents item snippets are onCommand, onLink, onView,
  // onSettingChanged, onContext, onExtensionInstalled and onStepSelected. An
  // event outside that set is not an error VS Code reports — the step simply
  // never completes.
  const known = [
    "onCommand:",
    "onLink:",
    "onView:",
    "onSettingChanged:",
    "onContext:",
    "onExtensionInstalled:",
  ];
  for (const step of theWalkthrough().steps) {
    for (const event of step.completionEvents ?? []) {
      assert.ok(
        event === "onStepSelected" || known.some((prefix) => event.startsWith(prefix)),
        `step "${step.id}" declares an unknown completion event "${event}"`,
      );
    }
  }
});

test("step 4 offers Build and completes on it", () => {
  // `Build` is what the reader clicks on the panel, and a step whose checkmark ticks on a command
  // it never offered teaches nothing (step 7's rule).
  const step = theWalkthrough().steps.find((candidate) => candidate.id === "validate");
  assert.ok(step, "the walkthrough must carry a step with id 'validate'");
  assert.ok(
    commandLinksIn(step).has("swcraft.build"),
    "the gate step's description must offer [Build](command:swcraft.build)",
  );
  assert.deepEqual(step.completionEvents, ["onCommand:swcraft.build"]);
});

test("step 5 offers Diagrams and completes on it", () => {
  const step = theWalkthrough().steps.find((candidate) => candidate.id === "seeTheDiagrams");
  assert.ok(step, "the walkthrough must carry a step with id 'seeTheDiagrams'");
  assert.ok(
    commandLinksIn(step).has("swcraft.diagrams"),
    "the diagrams step's description must offer [Diagrams](command:swcraft.diagrams)",
  );
  assert.deepEqual(step.completionEvents, ["onCommand:swcraft.diagrams"]);
});

test("no step completes on onStepSelected", () => {
  // `onStepSelected` completes a step when the reader *selects* it, so it says "you read this",
  // never "you did this". No step declares it, and adding one back has to be argued here against
  // the two replacements:
  //
  // - `editALayerFile` completes on `onContext:swcraft.activeEditorIsLayerFile` (commands.ts's
  //   `registerLayerFileContext`): with `onStepSelected` it ticked without the reader editing.
  // - `talkToTheModel` offers an **Open Copilot Chat** button and completes on
  //   `onCommand:workbench.action.chat.open`: less than the title promises, more than nothing, and
  //   the step's Markdown says which the tick means. The alternatives are worse:
  //   `onExtensionInstalled:GitHub.copilot-chat` ticks for everyone at start-up (Copilot Chat is a
  //   built-in in VS Code 1.134.0); `onLink:` needs an external link that would send the reader out
  //   of the editor; `onContext:` over the copied agent profile is true as soon as Onboard Project
  //   finishes.
  const onStepSelected = theWalkthrough()
    .steps.filter((step) => (step.completionEvents ?? []).includes("onStepSelected"))
    .map((step) => step.id);
  assert.deepEqual(
    onStepSelected,
    [],
    "a step completes on onStepSelected again — its checkmark means 'you looked at this'. " +
      "See this test's comment for the two steps that were moved off it and why.",
  );
});

test("every completionEvents onContext: names a context key the extension actually sets", () => {
  // The drift guard for `onContext:`: the key is a string in package.json and a string in the
  // adapter with no compiler between them, so a renamed constant would silently stop the step
  // completing. Grepped from both sides: the adapter's `setContext` calls name an imported
  // constant whose literal lives in a pure module.
  const events = theWalkthrough()
    .steps.flatMap((step) =>
      (step.completionEvents ?? []).map((event) => [step.id, event] as const),
    )
    .filter(([, event]) => event.startsWith("onContext:"));
  assert.ok(events.length > 0, "no step completes on onContext: — this guard would be vacuous");

  const keys = contextKeysTheExtensionSets();
  assert.ok(
    keys.size > 0,
    "no setContext call was found in the adapter — the grep below no longer matches the source",
  );
  for (const [stepId, event] of events) {
    const key = event.slice("onContext:".length);
    assert.ok(
      keys.has(key),
      `step "${stepId}" completes on "${event}", but no adapter setContext call sets "${key}" (found: ${[...keys].join(", ")})`,
    );
  }
});

test("step 7 offers the command it completes on", () => {
  // `onCommand:swcraft.openGuide` is SWCraft's own id, so the host-command
  // honesty rule above does not reach it — but the same reasoning does: a step
  // whose checkmark ticks on a command it never offered teaches nothing. The
  // button is the shortest route from the step to the guide.
  const step = theWalkthrough().steps.find((candidate) => candidate.id === "openTheGuide");
  assert.ok(step, "the walkthrough must carry a step with id 'openTheGuide'");
  assert.ok(
    commandLinksIn(step).has("swcraft.openGuide"),
    "the guide step's description must offer [Open Guide](command:swcraft.openGuide)",
  );
  assert.deepEqual(step.completionEvents, ["onCommand:swcraft.openGuide"]);
});

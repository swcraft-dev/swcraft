// @vscode/test-electron activation smoke test.
//
// Not run by `node --test`: runTests.ts loads it inside a real VS Code Extension Development
// Host through `extensionTestsPath`. `@vscode/test-electron` requires the module to export a
// `run(): Promise<void>` and wires up no node:test runner, so this is a plain async function.
//
// Scope: activation on the fixture workspace (`workspaceContains:swcraft.yaml`), command
// registration, and the host-side assertions `run()` calls. No CLI invocation: that would need
// a real `swcraft` on the test host's PATH.
//
// Every id in `contributes.commands` is expected, read out of the installed manifest:
// `registerCommands()` registers all of them once the extension activates, whichever
// activation event fired, so a declared id the host does not register is a defect.
//
// What it does not reach: the Command Palette with *no* workspace folder open (the
// `onCommand:swcraft.onboardProject` case) — this harness always opens the fixture
// workspace, so the two toolchain commands are asserted as registered here and as reachable
// with no folder open only by the seated gate; and the YAML schemas' rendering (completion,
// hover, diagnostics), which needs Red Hat YAML in the host over the network.
import * as assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import * as vscode from "vscode";

const EXTENSION_NAME = "swcraft-vscode";

/** One `contributes.commands` entry, typed as far as this file reads it. */
interface CommandContribution {
  command: string;
}

/**
 * Every command id the installed manifest declares, derived rather than listed (a restated
 * list goes stale). A floor guards the derivation: an empty list would make the loop below
 * assert nothing.
 */
function expectedCommands(extension: vscode.Extension<unknown>): string[] {
  const contributed = (
    extension.packageJSON?.contributes?.commands as CommandContribution[] | undefined
  )?.map((entry) => entry.command);
  assert.ok(
    contributed !== undefined && contributed.length >= 10,
    `the installed manifest declares ${contributed?.length ?? "no"} commands; this smoke run ` +
      "asserts each one is registered and would pass vacuously on a short list",
  );
  return contributed;
}

export async function run(): Promise<void> {
  const extension = vscode.extensions.all.find(
    (candidate) => candidate.packageJSON?.name === EXTENSION_NAME,
  );
  assert.ok(
    extension,
    `expected the '${EXTENSION_NAME}' extension to be discovered by the test host`,
  );

  if (!extension.isActive) {
    await extension.activate();
  }
  assert.ok(
    extension.isActive,
    "expected the extension to be active after opening the fixture workspace",
  );

  const registeredCommands = await vscode.commands.getCommands(true);
  for (const command of expectedCommands(extension)) {
    assert.ok(
      registeredCommands.includes(command),
      `expected command '${command}' to be registered after activation`,
    );
  }

  assertYamlValidationAssociation(extension);
  assertWalkthroughHostCommandsExist(extension, registeredCommands);
  await assertArxmlOpensAsXml();
  assertMarkdownPreviewIsDeclared();
  await assertPortInterfaceDefinition();
  await assertSliceTwoNavigation();
  await assertTraceLinksView(extension, registeredCommands);
  await assertPendingChangeSetsView(registeredCommands);
}

/**
 * The Pending Change Sets view (the panel review's fallback list): its two commands are
 * registered, the host registers the view's focus command, and the view can be focused and
 * refreshed in the fixture workspace. No approve or reject command exists: the review tab's
 * buttons are the only answer (packageCommands.test.ts holds that on the sources). What this
 * does NOT reach: a review tab opening by itself, which needs a SWCraft server waiting in
 * `panel` mode.
 */
async function assertPendingChangeSetsView(registeredCommands: string[]): Promise<void> {
  for (const id of ["swcraft.reviewOpen", "swcraft.reviewRefresh", "swcraftReview.focus"]) {
    assert.ok(registeredCommands.includes(id), `expected ${id} to be registered`);
  }
  assert.equal(
    registeredCommands
      .filter((id) => /^swcraft\.review/.test(id))
      .sort()
      .join(","),
    "swcraft.reviewOpen,swcraft.reviewRefresh",
    "the review view registers exactly its two commands",
  );
  await vscode.commands.executeCommand("swcraftReview.focus");
  await vscode.commands.executeCommand("swcraft.reviewRefresh");
}

/**
 * The Trace Links view (the panel's Labelling and Review nodes choose what it shows):
 * contributed below the panel, focusable, and the tree's own commands registered, the mode
 * nodes' pickers among them. Its nodes are the descriptor unit tests' (traceCore.test.ts).
 * What this does not reach: the rows VS Code renders, which no API reads back.
 */
async function assertTraceLinksView(
  extension: vscode.Extension<unknown>,
  registeredCommands: string[],
): Promise<void> {
  const views = (extension.packageJSON?.contributes?.views?.swcraft ?? []) as Array<{ id: string }>;
  assert.deepEqual(
    views.map((view) => view.id),
    ["swcraftView", "swcraftTrace", "swcraftReview"],
    "expected the Trace Links view below the panel, and the Pending Change Sets view below it",
  );
  for (const id of [
    "swcraft.traceChooseMode",
    "swcraft.traceNewSearch",
    "swcraft.traceOtherElement",
  ]) {
    assert.ok(registeredCommands.includes(id), `expected ${id} to be registered`);
  }
  assert.ok(
    registeredCommands.includes("swcraftTrace.focus"),
    "expected the host to register swcraftTrace.focus",
  );
  await vscode.commands.executeCommand("swcraftTrace.focus");
}

/** The fixture workspace's two model files, as absolute paths. */
function smokeFiles(): { source: string; target: string } {
  const workspace = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
  assert.ok(workspace, "expected the fixture workspace to be open");
  return {
    source: path.join(workspace, "model", "swc", "smoke", "SmokeSWC.yaml"),
    target: path.join(workspace, "model", "interfaces", "ISmokeSignal.yaml"),
  };
}

function sameFile(left: string, right: string): boolean {
  return fs.realpathSync(left) === fs.realpathSync(right);
}

/**
 * The outline, Go to Symbol in Workspace, the document links and the CodeLens answer in a
 * real host: `registerNavigation()` registered the four providers on the selectors they
 * answer through.
 *
 * - `vscode.executeDocumentSymbolProvider` on `SmokeSWC.yaml` answers the element as the root
 *   and its `ports` collection as a group holding `rSignal`;
 * - `vscode.executeWorkspaceSymbolProvider("ISmoke")` answers the interface's file;
 * - `vscode.executeLinkProvider` on `SmokeSWC.yaml`, with a resolve count so the host runs
 *   `resolveDocumentLink`, answers a link from the description whose target is the
 *   interface's file **with the `#L<n>` fragment of its identity line** (a target set at
 *   discovery would carry no fragment);
 * - `vscode.executeCodeLensProvider`, with no resolve count, answers the lenses on the
 *   component's identity line as provided: the *references* lens titled `references` with
 *   `editor.action.findReferences` (no count, nothing to resolve), and the *impact* lens.
 *
 * What this does NOT reach: what the *references* lens's click lists (Find All References runs
 * the CLI, which this host has not got), the *diagram* lens (the fixture has no `views_root`
 * pages), an import report's links, every grammar and outline case (the unit tests hold
 * those), and CI: this harness is not a `verify` stage.
 */
async function assertSliceTwoNavigation(): Promise<void> {
  const { source, target } = smokeFiles();
  const document = await vscode.workspace.openTextDocument(vscode.Uri.file(source));
  const lines = document.getText().split(/\r?\n/);
  const identityLine = lines.indexOf("name: SmokeSWC");
  assert.ok(identityLine >= 0, "the fixture component carries no 'name: SmokeSWC' line");

  const symbols =
    (await vscode.commands.executeCommand<vscode.DocumentSymbol[]>(
      "vscode.executeDocumentSymbolProvider",
      document.uri,
    )) ?? [];
  const root = symbols.find((symbol) => symbol.name === "SmokeSWC");
  assert.ok(
    root,
    `expected the outline's root to be SmokeSWC, got [${symbols.map((s) => s.name).join(", ")}]`,
  );
  const ports = (root.children ?? []).find((symbol) => symbol.name === "ports");
  assert.ok(
    ports,
    "expected the outline's root to hold a 'ports' group, got " +
      `[${(root.children ?? []).map((s) => s.name).join(", ")}]`,
  );
  assert.ok(
    (ports.children ?? []).some((symbol) => symbol.name === "rSignal"),
    "expected the outline's 'ports' group to hold rSignal",
  );

  const found =
    (await vscode.commands.executeCommand<vscode.SymbolInformation[]>(
      "vscode.executeWorkspaceSymbolProvider",
      "ISmoke",
    )) ?? [];
  assert.ok(
    found.some(
      (symbol) => symbol.name === "ISmokeSignal" && sameFile(symbol.location.uri.fsPath, target),
    ),
    `expected Go to Symbol in Workspace for 'ISmoke' to answer ${target}, got ` +
      `[${found.map((symbol) => `${symbol.name} ${symbol.location.uri.fsPath}`).join(", ")}]`,
  );

  const targetLines = fs.readFileSync(target, { encoding: "utf-8" }).split(/\r?\n/);
  const targetIdentity = targetLines.indexOf("name: ISmokeSignal") + 1;
  assert.ok(targetIdentity > 0, "the fixture interface carries no 'name: ISmokeSignal' line");
  const descriptionLine = lines.findIndex((text) => text.startsWith("description:"));
  const links =
    (await vscode.commands.executeCommand<vscode.DocumentLink[]>(
      "vscode.executeLinkProvider",
      document.uri,
      100,
    )) ?? [];
  const described = links.filter((link) => link.range.start.line === descriptionLine);
  assert.ok(
    described.some(
      (link) =>
        link.target !== undefined &&
        link.target.fragment === `L${targetIdentity}` &&
        sameFile(link.target.fsPath, target),
    ),
    `expected a resolved description link to ${target}#L${targetIdentity}, got ` +
      `[${described.map((link) => link.target?.toString() ?? "(no target)").join(", ")}]`,
  );

  const lenses =
    (await vscode.commands.executeCommand<vscode.CodeLens[]>(
      "vscode.executeCodeLensProvider",
      document.uri,
    )) ?? [];
  assert.ok(lenses.length > 0, "expected the CodeLens provider to answer lenses on SmokeSWC.yaml");
  assert.ok(
    lenses.every((lens) => lens.range.start.line === identityLine),
    `expected every lens on the identity line ${identityLine}, got lines ` +
      `[${lenses.map((lens) => lens.range.start.line).join(", ")}]`,
  );
  assert.ok(
    lenses.some(
      (lens) =>
        lens.command?.command === "swcraft.impact" && lens.command.arguments?.[0] === "SmokeSWC",
    ),
    "expected an 'impact' lens naming SmokeSWC on the identity line",
  );
  assert.ok(
    lenses.some(
      (lens) =>
        lens.command?.title === "references" &&
        lens.command.command === "editor.action.findReferences" &&
        (lens.command.arguments?.[1] as vscode.Position | undefined)?.line === identityLine,
    ),
    "expected a 'references' lens running Find All References at the identity line",
  );
}

/**
 * The built-in Markdown extension is present and declares `markdown.showPreview`, the command
 * an experiment's report opens with, and `markdown.preview.refresh`, which redraws it.
 *
 * A declaration read, as `assertArxmlOpensAsXml`: it needs no activation of that extension.
 * What this does NOT reach: that the preview renders a report; no experiment runs here (it
 * needs the CLI).
 */
function assertMarkdownPreviewIsDeclared(): void {
  const markdown = vscode.extensions.all.find(
    (candidate) => candidate.id === "vscode.markdown-language-features",
  );
  assert.ok(
    markdown,
    "expected VS Code's built-in Markdown extension to be present in the test host",
  );
  const commands: { command?: string }[] = markdown.packageJSON?.contributes?.commands ?? [];
  assert.ok(
    commands.some((entry) => entry.command === "markdown.showPreview"),
    "expected the built-in Markdown extension to declare markdown.showPreview",
  );
  assert.ok(
    commands.some((entry) => entry.command === "markdown.preview.refresh"),
    "expected the built-in Markdown extension to declare markdown.preview.refresh",
  );
}

/**
 * Go to Definition on a port's `interface` answers the interface's file, and the hover on the
 * same position shows a card naming the interface.
 *
 * Witnesses that `registerNavigation()` registered its providers and found the bundled
 * `schemas/references.json` (a missing artifact turns navigation off and would answer nothing
 * here). VS Code's built-in YAML grammar gives the fixture's files the `yaml` language id, so
 * no Marketplace extension is needed.
 *
 * What this does NOT reach: Find All References (it needs the CLI), every resolver case
 * (navigationCore's unit tests hold those), and CI: this harness is not a `verify` stage.
 */
async function assertPortInterfaceDefinition(): Promise<void> {
  const { source, target } = smokeFiles();
  const document = await vscode.workspace.openTextDocument(vscode.Uri.file(source));
  assert.equal(document.languageId, "yaml", "expected the fixture component to open as yaml");
  const line = document
    .getText()
    .split(/\r?\n/)
    .findIndex((text) => text.includes("interface: ISmokeSignal"));
  assert.ok(line >= 0, "the fixture component carries no 'interface: ISmokeSignal' line");
  const column = document.lineAt(line).text.indexOf("ISmokeSignal") + 2;
  const answers = await vscode.commands.executeCommand<
    Array<vscode.Location | vscode.LocationLink>
  >("vscode.executeDefinitionProvider", document.uri, new vscode.Position(line, column));
  const files = (answers ?? []).map((answer) =>
    "targetUri" in answer ? answer.targetUri.fsPath : answer.uri.fsPath,
  );
  assert.ok(
    files.some((file) => sameFile(file, target)),
    `expected Go to Definition on the port's interface to answer ${target}, got ` +
      `[${files.join(", ")}]`,
  );
  const hovers = await vscode.commands.executeCommand<vscode.Hover[]>(
    "vscode.executeHoverProvider",
    document.uri,
    new vscode.Position(line, column),
  );
  const hoverText = (hovers ?? [])
    .flatMap((hover) => hover.contents)
    .map((content) => (typeof content === "string" ? content : content.value))
    .join("\n");
  assert.ok(
    hoverText.includes("ISmokeSignal"),
    `expected the hover on the port's interface to name ISmokeSignal, got: ${hoverText}`,
  );
}

/**
 * The walkthrough's host-command completion events resolve in a real host.
 *
 * The "Talk to the model" step completes on `onCommand:workbench.action.chat.open` and offers
 * that command as a button. It is a VS Code *core* command, so nothing in this repository
 * would notice it being renamed: the step would stop ticking and the button do nothing. So it
 * is measured: the ids are read out of the installed manifest and checked against the
 * commands this host registered; `--disable-extensions` makes the check strict (a core
 * command is present anyway, an extension's would not be).
 *
 * What this does NOT reach: whether the command does what the step says; executing it would
 * open the Chat view in the test host.
 */
function assertWalkthroughHostCommandsExist(
  extension: vscode.Extension<unknown>,
  registeredCommands: string[],
): void {
  const walkthroughs: { steps?: { id: string; completionEvents?: string[] }[] }[] =
    extension.packageJSON?.contributes?.walkthroughs ?? [];
  const hostCommands = new Map<string, string>();
  for (const walkthrough of walkthroughs) {
    for (const step of walkthrough.steps ?? []) {
      for (const event of step.completionEvents ?? []) {
        if (!event.startsWith("onCommand:")) {
          continue;
        }
        const commandId = event.slice("onCommand:".length);
        if (!commandId.startsWith("swcraft.")) {
          hostCommands.set(commandId, step.id);
        }
      }
    }
  }
  assert.ok(
    hostCommands.size > 0,
    "no walkthrough step completes on a non-SWCraft command — this assertion would be vacuous",
  );
  for (const [commandId, stepId] of hostCommands) {
    assert.ok(
      registeredCommands.includes(commandId),
      `walkthrough step '${stepId}' completes on '${commandId}', which this host does not ` +
        "register. The step's button is dead and its checkmark can never be earned.",
    );
  }
}

function assertYamlValidationAssociation(extension: vscode.Extension<unknown>): void {
  const block: unknown = extension.packageJSON?.contributes?.yamlValidation;
  assert.ok(
    Array.isArray(block) && block.length > 0,
    "expected contributes.yamlValidation to be registered on the installed extension",
  );
  for (const entry of block as { fileMatch?: unknown; url?: unknown }[]) {
    assert.ok(
      Array.isArray(entry.fileMatch) && entry.fileMatch.length > 0,
      `expected a non-empty fileMatch on the entry for ${String(entry.url)}`,
    );
    assert.ok(
      typeof entry.url === "string" && entry.url.startsWith("./"),
      `expected an extension-relative url, got ${String(entry.url)}`,
    );
    // Red Hat YAML's client resolves a `./` url with joinPath(extensionUri, url);
    // do the same against the installed extension directory.
    const resolved = vscode.Uri.joinPath(extension.extensionUri, entry.url);
    assert.ok(
      fs.existsSync(resolved.fsPath),
      `expected ${entry.url} to resolve inside the installed extension, got ${resolved.fsPath}`,
    );
  }
}

/**
 * The `.arxml` -> `xml` association, measured on a running host.
 *
 * The probe file carries **no** `<?xml ... ?>` declaration: VS Code's built-in `xml` language
 * declares a `firstLine` pattern (`(\<\?xml.*)|(\<svg)|...`) the host falls back to when no
 * path association matches, so a `.arxml` file with the declaration resolves to `xml` even
 * without the contribution (one without it, to `plaintext`). This probe fails without it.
 *
 * The launcher's `--disable-extensions` leaves the built-ins (`vscode.xml` among them)
 * running: the grammar that renders the file is the built-in one, so highlighting survives
 * Red Hat XML's absence, and Red Hat XML (which contributes only `dtd` and `rnc`, never `xml`)
 * adds LemMinX's structure and navigation on top of the same id.
 */
async function assertArxmlOpensAsXml(): Promise<void> {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "swcraft-arxml-association-"));
  const probe = path.join(directory, "model.arxml");
  fs.writeFileSync(probe, "<AUTOSAR>\n</AUTOSAR>\n", "utf-8");
  try {
    const document = await vscode.workspace.openTextDocument(vscode.Uri.file(probe));
    assert.equal(
      document.languageId,
      "xml",
      "expected a .arxml file with no XML declaration to open as the built-in xml language",
    );
  } finally {
    fs.rmSync(directory, { recursive: true, force: true });
  }

  const builtInXml = vscode.extensions.all.find((candidate) => candidate.id === "vscode.xml");
  assert.ok(builtInXml, "expected VS Code's built-in xml extension to be present in the test host");
  const grammars: { language?: string }[] = builtInXml.packageJSON?.contributes?.grammars ?? [];
  assert.ok(
    grammars.some((grammar) => grammar.language === "xml"),
    "expected the built-in xml extension to contribute a grammar for the xml language id",
  );
}

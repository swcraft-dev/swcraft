// SWCraft VS Code extension — activation entry point.
//
// One of the two VS Code adapter files (with commands.ts) allowed to `import "vscode"`; all
// decision logic lives in plain modules that never import vscode, so they run under plain
// `node --test`.
//
// The extension never writes an architecture model file itself; a quick fix is an edit of
// the open buffer the user applies and saves. It shells out to the installed `swcraft`
// executable, publishes the MCP server, registers the read-only navigation providers, and
// places `swcraft check-model`'s findings in the Problems panel on Check Model and Build.
// The two link warnings' quick fixes apply on an explicit request alone and none is marked
// preferred, so neither a save nor Auto Fix applies one the user did not pick.
//
// The activation events are package.json's `activationEvents` (packageCommands.test.ts checks
// each is handled): a project, a rule or guide link, and the commands that work with no project
// open. Every event runs the same `activate()`, which registers every command.
//
// `registerMcpServerProvider()` runs first: its return value is `registerCommands()`'s second
// argument, and VS Code consults providers when a chat message is submitted, so a later
// registration publishes nothing on that turn. The views register after the commands their
// items invoke. `registerNavigation()` runs last and guarded: a missing or malformed
// `schemas/references.json` turns navigation off and takes nothing else with it.
import * as vscode from "vscode";

import {
  checkClientSwcraftOnPath,
  flushTraceWrites,
  logClientSwcraftFailure,
  offerAgentProfile,
  registerCommands,
  registerImpactView,
  registerLayerFileContext,
  registerMcpServerProvider,
  registerNavigation,
  registerResourceView,
  registerReviewView,
  registerTraceView,
  registerTreeView,
} from "./commands";
import { SwcraftMcpServerProvider } from "./mcpProvider";

/**
 * A provider that registers nothing and whose `refresh()` is a no-op.
 *
 * `registerCommands()` needs a provider for the two toolchain commands, and those commands
 * are what a reader reaches for when the editor integration is broken, so a failed MCP
 * registration must not take them with it.
 */
function inertMcpProvider(): SwcraftMcpServerProvider {
  return new SwcraftMcpServerProvider(
    { fire: () => undefined },
    () => ({ folders: [], executable: "", version: "", toolchainPresent: false }),
    () => false,
  );
}

export function activate(context: vscode.ExtensionContext): void {
  const workspaceRoot = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
  console.log(`SWCraft extension activated. workspaceRoot=${workspaceRoot ?? "<none>"}`);
  // Guarded: a throw here would register no commands at all, and
  // `vscode.lm.registerMcpServerDefinitionProvider` is the one API a host may not implement.
  let mcpProvider: SwcraftMcpServerProvider;
  try {
    mcpProvider = registerMcpServerProvider(context);
  } catch (error) {
    mcpProvider = inertMcpProvider();
    console.error(
      `SWCraft: the MCP server provider could not be registered (${
        error instanceof Error ? error.message : String(error)
      }). The SWCraft commands are registered; Agent mode will not see a SWCraft server.`,
    );
    void vscode.window.showWarningMessage(
      "SWCraft: this VS Code could not register the SWCraft MCP server, so Agent mode will not " +
        "see it. Every SWCraft command still works.",
    );
  }
  registerCommands(context, mcpProvider);
  registerTreeView(context);
  registerTraceView(context);
  registerResourceView(context);
  registerImpactView(context);
  registerReviewView(context);
  registerLayerFileContext(context);
  // Not awaited: the notice waits on the user, and activation must not.
  void offerAgentProfile(context).catch((error: unknown) => {
    console.error(
      `SWCraft: the agent profile notice failed (${
        error instanceof Error ? error.message : String(error)
      }).`,
    );
  });
  // Not awaited either: it may spawn two `swcraft --version` probes.
  void checkClientSwcraftOnPath(context).catch(logClientSwcraftFailure);
  // Guarded: navigation reads a bundled artifact, and its failure must not take the
  // commands, the tree or the layer-file context key with it.
  try {
    registerNavigation(context);
  } catch (error) {
    console.error(
      `SWCraft: navigation could not be registered (${
        error instanceof Error ? error.message : String(error)
      }). Every SWCraft command is registered.`,
    );
    void vscode.window.showWarningMessage(
      "SWCraft: navigation on model elements (definitions, hover, references, outline, symbols, " +
        "CodeLens, links) is off in this window — the extension's reference table could not be read. " +
        "Every SWCraft command still works.",
    );
  }
}

/**
 * VS Code awaits `deactivate()` and not an asynchronous disposal, so the Trace
 * Links view's queued decision is written here: the flush cancels the queue's
 * timer and writes the edit it held.
 */
export function deactivate(): Promise<void> {
  return flushTraceWrites();
}

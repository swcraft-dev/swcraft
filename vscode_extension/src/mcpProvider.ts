// The MCP server this extension publishes to VS Code through
// `vscode.lm.registerMcpServerDefinitionProvider`: `swcraft mcp-serve` of the one installed
// executable, with no generated `.vscode/mcp.json` for the user to keep and repair.
//
// No `vscode` import: this module runs under plain `node --test`. `commands.ts` is the
// adapter that turns each descriptor into a `vscode.McpStdioServerDefinition` and backs
// `refresh()` with a `vscode.EventEmitter`.
//
// **What this does not reach.** VS Code merges the definition's `env` over the extension
// host's environment (`McpStdioServerDefinition.env`: "variables in this environment will
// overwrite or remove (if null) the default environment variables of the editor's extension
// host"), so the keys below are what SWCraft *sets*, not the server's whole environment.

import * as path from "node:path";

import { MANIFEST_FILENAME } from "./project";

/**
 * The provider id, declared in `package.json`'s
 * `contributes.mcpServerDefinitionProviders` and passed to
 * `vscode.lm.registerMcpServerDefinitionProvider`. The two must agree: on a mismatch the
 * provider is silently never consulted, so `mcpProvider.test.ts` reads both.
 */
export const MCP_PROVIDER_ID = "swcraft.mcp";

/** The human-readable name VS Code shows for the provider and for a lone server. */
export const MCP_SERVER_LABEL = "SWCraft";

/** The review-mode environment key a definition sets. See `REVIEW_MODE_ENV_VALUE`. */
export const REVIEW_MODE_ENV_KEY = "SWCRAFT_REVIEW";

/**
 * The review-boundary channel. The MCP server reads `SWCRAFT_REVIEW` at start-up: `panel`
 * puts the *computed* delta (and, separately, a resource weakening) to the human in a review
 * tab this extension opens, whose answer is the only one that can commit a change set.
 * Without it the server runs the fail-closed `stage-only` default: Agent mode could stage and
 * never apply.
 *
 * `panel`, not `elicit`, because Codex declines MCP elicitation, and the review tab serves
 * Copilot, Codex and `swcraft agent` alike. The tab's limit: a process with the user's full
 * rights (a terminal command the user lets run included) can write a decision. Not the VS
 * Code tool-confirmation dialog either: that authorizes running the tool, not the delta it
 * computed, and *Allow in this Session* can suppress it.
 */
export const REVIEW_MODE_ENV_VALUE = "panel";

/** One MCP server, as this module decides it and before `vscode` types touch it. */
export interface McpServerDescriptor {
  /** The name VS Code shows in its server list. */
  label: string;
  /** The `swcraft` executable — `toolchain.ts`'s resolution, passed in. */
  command: string;
  /** `mcp-serve --project <absolute manifest path>`. */
  args: string[];
  /** The workspace folder the server runs in. */
  cwd: string;
  /** Merged over the extension host's environment — see the file header. */
  env: Record<string, string>;
  /**
   * A version string VS Code diffs to decide whether to ask again: the extension's own, plus
   * a marker when the toolchain is not on disk.
   */
  version: string;
}

/** What a definition set is computed from, all of it read at the call. */
export interface McpProviderInputs {
  /** Absolute paths of the open workspace folders, in VS Code's own order. */
  folders: readonly string[];
  /** The executable every SWCraft command spawns (`toolchain.ts`). */
  executable: string;
  /** The running extension's version, from its own `packageJSON`. */
  version: string;
  /**
   * Whether the executable above is **on disk** (`toolchain.ts`'s `resolution.exists`).
   *
   * It changes the published `version`, so the `refresh()` after *Install Toolchain* makes VS
   * Code retry a server it failed to start; byte-identical definitions would diff to "no
   * change". It does **not** reach the toolchain's *version* (that would need a spawn): an
   * in-place upgrade republishes the same definitions, which is correct, since the server's
   * command has not changed.
   */
  toolchainPresent: boolean;
}

/**
 * One server per workspace folder whose **root** carries a `swcraft.yaml`.
 *
 * Load-bearing, each pinned by a test:
 *
 * * **`--project` is absolute.** `cmd_mcp_serve` stores it raw into `SWCRAFT_MANIFEST`, so a
 *   relative path would resolve against the extension host's cwd.
 * * **`cwd` is the folder**, so the server's relative work (the audit log, git) happens
 *   inside the project.
 * * **`env` carries exactly two keys**: `SWCRAFT_REVIEW` and `PYTHONUTF8=1`. On Windows
 *   Python writes a piped stdout in the locale encoding unless UTF-8 mode is on, and an MCP
 *   server's conversation is a pipe.
 *
 * *exists* is injected so the decision stays pure; the adapter passes a
 * `statSync().isFile()`, not `fs.existsSync`, so a *directory* named `swcraft.yaml` publishes
 * nothing, as `project.ts`'s `findManifest()` would. It is asked **only** about
 * `<folder>/swcraft.yaml`: a nested manifest registers no server, as every other command
 * assumes the root manifest.
 */
export function planMcpServerDefinitions(
  inputs: McpProviderInputs,
  exists: (candidate: string) => boolean,
): McpServerDescriptor[] {
  const projects = inputs.folders
    .map((folder) => ({ folder, manifest: path.join(folder, MANIFEST_FILENAME) }))
    .filter((candidate) => exists(candidate.manifest));

  // The folder's name qualifies the label only when a second server would otherwise be
  // indistinguishable in VS Code's server list.
  const qualify = projects.length > 1;
  // `path.basename` is not unique across roots, so colliding names take the whole folder
  // path. This does not reach two roots that are one directory by two paths (a symlink, a
  // Windows substituted drive).
  const names = projects.map(({ folder }) => path.basename(folder));
  const distinguishes = (folder: string): string => {
    const name = path.basename(folder);
    return names.filter((candidate) => candidate === name).length > 1 ? folder : name;
  };

  return projects.map(({ folder, manifest }) => ({
    label: qualify ? `${MCP_SERVER_LABEL} (${distinguishes(folder)})` : MCP_SERVER_LABEL,
    command: inputs.executable,
    args: ["mcp-serve", "--project", manifest],
    cwd: folder,
    env: { [REVIEW_MODE_ENV_KEY]: REVIEW_MODE_ENV_VALUE, PYTHONUTF8: "1" },
    version: inputs.toolchainPresent ? inputs.version : `${inputs.version}+no-toolchain`,
  }));
}

/**
 * The signal `refresh()` fires: `vscode.EventEmitter<void>` in the adapter, a counter in the
 * tests.
 */
export interface McpChangeSignal {
  fire(): void;
}

/**
 * The provider's own state: the change signal, plus the callback that reads the inputs.
 *
 * The inputs are read at each call, not captured, because each changes while a window is
 * open. `refresh()` is the *only* thing that fires the signal; answering a request does not.
 * `commands.ts` calls it after a successful toolchain run or onboarding, and on a
 * workspace-folder or `swcraft.executable` change.
 *
 * Not reached: a `swcraft.yaml` created or deleted outside the onboard command (a terminal,
 * git). No file watcher is registered, since it would run in every window; the server then
 * appears at the next reload.
 */
export class SwcraftMcpServerProvider {
  constructor(
    private readonly signal: McpChangeSignal,
    private readonly read: () => McpProviderInputs,
    private readonly exists: (candidate: string) => boolean,
  ) {}

  /** The servers to publish, computed from the inputs as they are right now. */
  definitions(): McpServerDescriptor[] {
    return planMcpServerDefinitions(this.read(), this.exists);
  }

  /** Tell VS Code the set may have changed, so it asks again. */
  refresh(): void {
    this.signal.fire();
  }
}

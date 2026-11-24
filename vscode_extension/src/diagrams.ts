// `SWCraft: Diagrams` — the pure half: which file a press opens, what one press
// reports (`reportDiagramsPress()`), and the watch over the model whose events
// mark the artifacts *out of date*; a save regenerates nothing.
//
// Files, not a server: `swcraft generate diagrams` writes one HTML page per view
// and `index.html` under `views_root`; a press runs it through `spawnCli()` and
// shows the index in the diagrams' tab (`diagramsTab.ts`). Nothing outlives the
// press: no child session, no port.
//
// No `vscode` import, so it runs under plain `node --test`. The adapter
// (commands.ts) supplies the watcher, the manifest reader and what a change
// marks. It reads the disk only in `newestSourceChange()`.

import * as fs from "node:fs";
import * as path from "node:path";

/**
 * The page a press opens, as the generator names it: directly under
 * `views_root`, listing every view with the report as a section.
 */
export const DIAGRAMS_INDEX_FILENAME = "index.html";

/** `<views_root>/index.html` — *viewsRoot* as `project.ts`'s `resolveViewsRoot()` answers it. */
export function diagramsIndexPath(viewsRoot: string): string {
  return path.join(viewsRoot, DIAGRAMS_INDEX_FILENAME);
}

// --- what one `SWCraft: Diagrams` press reports -------------------------------

/**
 * How one press ended, as far as the panel and the notification care.
 *
 * - `refused` — a refusal before anything is generated: a toolchain run is in
 *   flight, or `views_root` is one the CLI refuses;
 * - `generation-refused` — `swcraft generate diagrams` did not exit 0 (its output
 *   is in the shared channel, so the notification is a warning, as for every
 *   command that finishes);
 * - `page-refused` — the diagrams' tab would not show the page: its script is
 *   not this release's, or it is not a page SWCraft generated
 *   (`diagramsTab.ts`'s `webviewDocument()`); *message* is the line the tab
 *   shows in its place, beside **Generate**;
 * - `opened` — the page was shown in the diagrams' tab. *view* is set when a
 *   CodeLens asked for one element's page: `found` when `<id>.html` exists after
 *   the generation and was the page opened, else the index was opened instead.
 *   The *diagram* lens's direct open (no generation) always has `found`.
 */
export type DiagramsPress =
  | { kind: "refused"; message: string }
  | { kind: "generation-refused"; message: string }
  | { kind: "page-refused"; message: string }
  | { kind: "opened"; view?: DiagramsView };

/** The one page a press was asked to open, and whether it existed after the generation. */
export interface DiagramsView {
  id: string;
  found: boolean;
}

/** `<id>.html` — the file name of view *id*'s page. */
export function viewPageName(id: string): string {
  return `${id}.html`;
}

/**
 * Is view *id*'s page listed in *viewsListing* (the `views_root` entries,
 * `undefined` when it cannot be listed)? The one test the CodeLens's diagram
 * lens and `swcraft.diagrams`' argument share.
 */
export function isListedView(viewsListing: readonly string[] | undefined, id: string): boolean {
  return viewsListing?.includes(viewPageName(id)) === true;
}

/** `<views_root>/<id>.html` — the page of view *id*, beside the index. */
export function diagramsViewPath(viewsRoot: string, id: string): string {
  return path.join(viewsRoot, viewPageName(id));
}

/** What showing a page in the diagrams' tab answered: shown, or refused with the line the tab shows instead. */
export type TabShow = { kind: "shown" } | { kind: "refused"; line: string };

/**
 * The press a shown page ends: `opened` (with the lens's *view*, if one asked)
 * when the tab shows it, `page-refused` carrying the tab's one line when it would
 * not (a page of another release, a page no longer under `views_root`). Both the
 * direct open and the full press end here.
 */
export function pressOfShow(show: TabShow, view?: DiagramsView): DiagramsPress {
  if (show.kind === "refused") {
    return { kind: "page-refused", message: show.line };
  }
  return view === undefined ? { kind: "opened" } : { kind: "opened", view };
}

/** The panel status and the one notification (if any) a press ends with. */
export interface DiagramsPressReport {
  /** Recorded under `swcraft.diagrams`: `treeView.ts`'s `CommandOutcome` shape. */
  status: { kind: "success" } | { kind: "failure"; message: string };
  notification: { severity: "error" | "warning" | "information"; message: string } | undefined;
}

/**
 * The status and the notification for every way a press ends, decided here so a
 * unit test covers them.
 *
 * A shown page is a silent success, unless a lens asked for a page the generation
 * no longer writes. A page the tab refuses is a failure carrying the tab's line;
 * no notification repeats it, since the tab shows it beside **Generate**.
 */
export function reportDiagramsPress(press: DiagramsPress): DiagramsPressReport {
  switch (press.kind) {
    case "refused":
      return {
        status: { kind: "failure", message: press.message },
        notification: { severity: "error", message: press.message },
      };
    case "generation-refused":
      return {
        status: { kind: "failure", message: press.message },
        notification: { severity: "warning", message: press.message },
      };
    case "page-refused":
      return { status: { kind: "failure", message: press.message }, notification: undefined };
    case "opened":
      // The lens's page is gone (the element was renamed or removed): say why the
      // index opened instead.
      return {
        status: { kind: "success" },
        notification:
          press.view?.found === false
            ? {
                severity: "information",
                message: `SWCraft: the diagram page ${viewPageName(press.view.id)} is no longer generated — the index of the diagrams was opened instead.`,
              }
            : undefined,
      };
  }
}

// --- the watch over the model: it marks, it never regenerates ---------------
//
// Regenerating on save costs seconds per save on a large model and would still
// leave the ARXML stale, so the watch only reports a change; the adapter turns it
// into the artifacts' *out of date* state (treeView.ts's `decideArtifactState()`).

/** `vscode.Disposable`'s shape, without importing `vscode`. */
export interface WatchDisposable {
  dispose(): void;
}

/** One watched glob, relative to an absolute base directory — `vscode.RelativePattern`'s two halves. */
export interface WatchPattern {
  base: string;
  glob: string;
}

/**
 * Start watching *pattern*, calling *onEvent* with the changed path on every
 * create, change and delete under it.
 */
export type WatchFactory = (
  pattern: WatchPattern,
  onEvent: (changedPath: string) => void,
) => WatchDisposable;

/** Everything under the resolved model root: every layer file of every layer. */
export function modelRootWatchPattern(modelRoot: string): WatchPattern {
  return { base: modelRoot, glob: "**" };
}

/** The manifest itself, in its own directory — `model_root` and the output paths live there. */
export function manifestWatchPattern(manifestPath: string): WatchPattern {
  return { base: path.dirname(manifestPath), glob: path.basename(manifestPath) };
}

/**
 * The two watched patterns of one project: the model root and the manifest.
 *
 * A manifest save can move `model_root`, so after every manifest event the root
 * is resolved again and, when it moved, the old watcher is disposed before the
 * new one is created; a late event from the disposed watcher is ignored. An
 * `undefined` root (a manifest the loader refuses) watches only the manifest, so
 * the save that repairs it arms the model root again.
 *
 * Both kinds of event call *onChange*: a manifest save can change what is
 * generated as much as a layer file can.
 *
 * Does not reach a change made while the window was closed
 * (`newestSourceChange()` reads that at activation).
 */
export class ModelWatch implements WatchDisposable {
  private modelRoot: string | undefined;
  private modelRootWatcher: WatchDisposable;
  private readonly manifestWatcher: WatchDisposable;
  private generation = 0;
  private disposed = false;

  constructor(
    private readonly manifestPath: string,
    private readonly resolveModelRoot: (manifestPath: string) => string | undefined,
    private readonly watch: WatchFactory,
    private readonly onChange: (changedPath: string) => void,
  ) {
    this.modelRoot = resolveModelRoot(manifestPath);
    this.modelRootWatcher = this.watchModelRoot();
    this.manifestWatcher = watch(manifestWatchPattern(manifestPath), (changedPath) =>
      this.manifestChanged(changedPath),
    );
  }

  dispose(): void {
    if (this.disposed) {
      return;
    }
    this.disposed = true;
    this.modelRootWatcher.dispose();
    this.manifestWatcher.dispose();
  }

  private watchModelRoot(): WatchDisposable {
    this.generation += 1;
    const generation = this.generation;
    if (this.modelRoot === undefined) {
      return { dispose: () => {} };
    }
    return this.watch(modelRootWatchPattern(this.modelRoot), (changedPath) => {
      if (!this.disposed && generation === this.generation) {
        this.onChange(changedPath);
      }
    });
  }

  private manifestChanged(changedPath: string): void {
    if (this.disposed) {
      return;
    }
    const modelRoot = this.resolveModelRoot(this.manifestPath);
    if (modelRoot !== this.modelRoot) {
      this.modelRootWatcher.dispose();
      this.modelRoot = modelRoot;
      this.modelRootWatcher = this.watchModelRoot();
    }
    this.onChange(changedPath);
  }
}

/**
 * The one model watch a window holds, keyed by workspace folder.
 *
 * Armed at activation (a window where *Diagrams* was never pressed must still
 * mark the ARXML *out of date*), after a workspace-folder change and after
 * *Onboard Project* succeeds, the one way a folder gains a manifest without a
 * folder change. Every arming disposes the watch it replaces first, so a second
 * arming of a folder reads its manifest afresh. Disposed with the window.
 */
export class ModelWatchSession implements WatchDisposable {
  private watch: WatchDisposable | undefined;

  constructor(private readonly arm: (folder: string) => WatchDisposable) {}

  /** Watch *folder*, disposing the watch this replaces first. */
  armFor(folder: string): void {
    this.dispose();
    this.watch = this.arm(folder);
  }

  /** Dispose the watch, if any. */
  dispose(): void {
    const watch = this.watch;
    this.watch = undefined;
    watch?.dispose();
  }
}

/**
 * The newest modification time, in milliseconds, of the manifest and of every
 * entry under *modelRoot* — directories included, because a deleted file leaves
 * only its directory to stat — or undefined when neither exists.
 *
 * Compared with the last successful generation the adapter keeps, not with the
 * files under `views_root` (`generate diagrams` keeps a file it would rewrite
 * unchanged), so a `git checkout` marks the artifacts *out of date*. Symbolic
 * links are not followed; an entry that vanishes during the walk is skipped. An
 * `undefined` *modelRoot* answers the manifest alone.
 */
export async function newestSourceChange(
  modelRoot: string | undefined,
  manifestPath: string,
): Promise<number | undefined> {
  let newest: number | undefined;
  const note = (mtimeMs: number): void => {
    newest = newest === undefined ? mtimeMs : Math.max(newest, mtimeMs);
  };
  const walk = async (entry: string): Promise<void> => {
    let stat: fs.Stats;
    try {
      stat = await fs.promises.lstat(entry);
    } catch {
      return;
    }
    note(stat.mtimeMs);
    if (!stat.isDirectory()) {
      return;
    }
    let children: string[];
    try {
      children = await fs.promises.readdir(entry);
    } catch {
      return;
    }
    for (const child of children) {
      await walk(path.join(entry, child));
    }
  };
  if (modelRoot !== undefined) {
    await walk(modelRoot);
  }
  try {
    note((await fs.promises.stat(manifestPath)).mtimeMs);
  } catch {
    // No manifest: the model's own times answer, or nothing does.
  }
  return newest;
}

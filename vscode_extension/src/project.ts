// Pure logic: SWCraft project manifest detection and the manifest's path fields.
//
// No `vscode` import. Only the workspace root is checked for `swcraft.yaml` (nested
// manifests are a documented limitation, see vscode_extension/README.md); the filename
// mirrors `_MANIFEST_FILENAME` in packages/tools/src/swcraft_tools/project.py.

import * as fs from "node:fs";
import * as path from "node:path";

export const MANIFEST_FILENAME = "swcraft.yaml";

/**
 * Locate `swcraft.yaml` at the root of *workspaceRoot*: its absolute path, or `undefined`
 * when absent. Subdirectories are not searched.
 */
export function findManifest(workspaceRoot: string): string | undefined {
  const candidate = path.join(workspaceRoot, MANIFEST_FILENAME);
  try {
    return fs.statSync(candidate).isFile() ? candidate : undefined;
  } catch {
    return undefined;
  }
}

// --- The manifest line reader -------------------------------------------------
//
// Reads the manifest's five path fields (`model_root`, `dist_root`, `views_root`,
// `outputs.arxml`, `outputs.needs`) so the extension can judge them as the loader does,
// without a YAML dependency. A small extractor for the plain block-mapping shape every
// `swcraft.yaml` uses, not a general YAML parser; flow style is not supported.

/**
 * The scalar a `key:` line's value part denotes, as PyYAML would read it.
 *
 * A quoted value is taken whole (a `#` inside it is content; what follows the closing
 * quote is ignored). For a *plain* scalar the comment rule is PyYAML's
 * `Scanner.scan_plain`, not "the first `#`": only a `#` at the start of the value or after
 * whitespace begins a comment.
 *
 * ```
 * c4: views/model#1.c4   -> "views/model#1.c4"
 * c4: views/model #1.c4  -> "views/model"
 * c4: a#b#c              -> "a#b#c"
 * ```
 *
 * What this does **not** reproduce: block and flow collections, multi-line and folded
 * scalars, anchors/aliases, tags, escapes inside a double-quoted scalar, and the
 * plain-scalar `: ` terminator. No `swcraft.yaml` SWCraft writes or documents uses them,
 * and the extension's answer only *locates* a directory; the CLI reads the file itself.
 */
function stripYamlScalar(raw: string): string {
  const value = raw.trim();
  const quoted = value.match(/^"([^"]*)"|^'([^']*)'/);
  if (quoted) {
    return quoted[1] ?? quoted[2] ?? "";
  }
  const comment = value.search(/(^|[ \t])#/);
  if (comment === -1) {
    return value.trim();
  }
  return value.slice(0, comment).trim();
}

/**
 * An unindented `key:` line, its value (if any) in group 1: `parseTopLevelScalar()`'s key
 * rule, shared with `parseOutputsKey()`, which matches it after removing the indentation.
 */
function topLevelKeyPattern(key: string): RegExp {
  return new RegExp(`^${key}[ \\t]*:(?:[ \\t]+([^ \\t].*)?)?$`);
}

/**
 * The value of the top-level *key* in *manifestText*, or *fallback* when the key is absent
 * or empty. An indented `key:` belongs to another block and is not read.
 *
 * As libyaml (`yaml.CSafeLoader`, the CLI's reader) reads it: spaces or tabs between the key
 * and its colon are accepted (`views_root : diagrams`); after the colon a space, a tab or
 * the line's end is required, since `views_root:value` is plain-scalar content the CLI
 * refuses at load, and `Diagrams` must not answer with a refusal of its own.
 *
 * What it does **not** reach, beside `stripYamlScalar()`'s list: a quoted key, a flow
 * mapping, a key reached through an anchor or merge key, a document separator, and a key
 * written twice (PyYAML keeps the last, this keeps the first).
 */
function parseTopLevelScalar(manifestText: string, key: string, fallback: string): string {
  const pattern = topLevelKeyPattern(key);
  for (const line of manifestText.split(/\r?\n/)) {
    const match = line.match(pattern);
    if (match) {
      const value = stripYamlScalar(match[1] ?? "");
      return value === "" ? fallback : value;
    }
  }
  return fallback;
}

// --- model_root resolution ---------------------------------------------------
//
// Read by layerFile.ts's path test. Reading `swcraft.yaml` is not reading the *model*
// (see the note at the head of layerFile.ts).

/** Mirrors Python's `SwcraftProject.model_root` default. */
export const DEFAULT_MODEL_ROOT = "model";

/**
 * The top-level `model_root` of *manifestText* (raw YAML), or `DEFAULT_MODEL_ROOT` when
 * absent, as the Pydantic default on the Python side.
 */
export function parseModelRoot(manifestText: string): string {
  return parseTopLevelScalar(manifestText, "model_root", DEFAULT_MODEL_ROOT);
}

/** Mirrors Python's `SwcraftProject.dist_root` default. */
export const DEFAULT_DIST_ROOT = "dist";

/** The top-level `dist_root` of *manifestText*, or `DEFAULT_DIST_ROOT` — `parseModelRoot()`'s rule. */
export function parseDistRoot(manifestText: string): string {
  return parseTopLevelScalar(manifestText, "dist_root", DEFAULT_DIST_ROOT);
}

// --- the outputs block (the ARXML the Build item marks) -----------------------
//
// The panel needs to know whether the ARXML is on disk (treeView.ts's
// `decideArtifactState()`), so it reads `outputs.arxml`; `outputs.needs` is read because the
// loader judges that path too (`readManifestPaths()`).

/** Mirrors Python's `OutputPaths.arxml` default. */
export const DEFAULT_ARXML_OUTPUT = "dist/model.arxml";

/** Mirrors Python's `OutputPaths.needs` default: the one `needs.json` `swcraft generate needs` writes. */
export const DEFAULT_NEEDS_OUTPUT = "dist/needs.json";

/** `project.AUDIT_LOG_FILE`: the audit log every model write appends to in `dist_root` (rule 4). */
export const AUDIT_LOG_FILE = "audit.jsonl";
/** `project.BUILD_REPORT_FILE`: the build report `validate --report` writes in `dist_root` (rule 4). */
export const BUILD_REPORT_FILE = "build-report.md";
/** `project.DIAGRAM_REPORT_FILE`: the report `generate diagrams` writes in `views_root` (rule 4). */
export const DIAGRAM_REPORT_FILE = "report.txt";
/** `project.DIAGRAM_FILE_SUFFIXES`: every other file `generate diagrams` writes in `views_root` (rule 4). */
export const DIAGRAM_FILE_SUFFIXES: readonly string[] = [".svg", ".html"];
/**
 * `project.REPORT_DIRECTORIES`' keys: the directories in `dist_root` where `metrics --report`,
 * `resource-report --report`, `change-report` and `generate test-tables` write every file of their
 * reports under a fixed name (rule 4: the directory itself or anything below it).
 */
export const REPORT_DIRECTORIES: readonly string[] = [
  "metrics",
  "resource-report",
  "change-report",
  "test-tables",
];
/**
 * `project.NAMED_OUTPUT_DIRECTORIES`' keys: the directories in `dist_root` whose files the model or
 * the user names (rule 4: the directory itself only).
 */
export const NAMED_OUTPUT_DIRECTORIES: readonly string[] = ["components", "experiments"];

/**
 * The `arxml` key of the top-level `outputs:` block in *manifestText*, or
 * `DEFAULT_ARXML_OUTPUT` when either is absent or the value is empty.
 *
 * The block is the lines after a valueless `outputs:` line, up to the next line that is
 * neither blank, a comment, nor indented; its keys are the lines at its first key's
 * indentation. What it does **not** reach, beside `parseTopLevelScalar()`'s list: a flow
 * mapping, unevenly indented keys, and a key written twice (this keeps the first).
 */
export function parseOutputsArxml(manifestText: string): string {
  return parseOutputsKey(manifestText, "arxml", DEFAULT_ARXML_OUTPUT);
}

/** The `needs` key of the top-level `outputs:` block — `parseOutputsArxml()`'s rule. */
export function parseOutputsNeeds(manifestText: string): string {
  return parseOutputsKey(manifestText, "needs", DEFAULT_NEEDS_OUTPUT);
}

function parseOutputsKey(manifestText: string, key: string, fallback: string): string {
  const keyPattern = topLevelKeyPattern(key);
  const lines = manifestText.split(/\r?\n/);
  const start = lines.findIndex((line) => /^outputs[ \t]*:(?:[ \t]+#.*)?[ \t]*$/.test(line));
  if (start === -1) {
    return fallback;
  }
  let indent: string | undefined;
  for (const line of lines.slice(start + 1)) {
    if (/^\s*(#.*)?$/.test(line)) {
      continue;
    }
    const leading = line.match(/^ +/)?.[0];
    if (leading === undefined) {
      break;
    }
    indent ??= leading;
    if (leading !== indent) {
      continue;
    }
    const match = line.slice(indent.length).match(keyPattern);
    if (match) {
      const value = stripYamlScalar(match[1] ?? "");
      return value === "" ? fallback : value;
    }
  }
  return fallback;
}

// --- the loader's path rules: one reading every caller goes through -----------
//
// `_validate_project_paths` (packages/tools/src/swcraft_tools/project.py) refuses the WHOLE
// manifest when any one of its five path fields breaks a rule, so the extension never
// judges a field piecemeal: every caller reads `readManifestPaths()` and acts only where its
// `refusal` is undefined; the Diagrams press reads it through `resolveViewsRoot()`.

/** The five path fields, in the order the loader's `candidates` lists them. */
export const MANIFEST_PATH_FIELDS = [
  "model_root",
  "dist_root",
  "views_root",
  "outputs.arxml",
  "outputs.needs",
] as const;

export type ManifestPathField = (typeof MANIFEST_PATH_FIELDS)[number];

/**
 * The loader's first refusal: which field, and which rule:
 * - `absolute` (rule 3): `dist_root` or `views_root` written absolute;
 * - `outside` (rule 1): resolves outside the project directory;
 * - `overlap` (rule 2): a generated path overlapping `model_root` either way;
 * - `collides` (rule 4): `outputs.needs` naming `outputs.arxml`, its `_old` sibling,
 *   `AUDIT_LOG_FILE` or `BUILD_REPORT_FILE` in `dist_root`, the manifest, a file directly in
 *   `views_root` named `report.txt` or ending `.svg`/`.html`, a path at or below a
 *   `REPORT_DIRECTORIES` directory in `dist_root`, or a `NAMED_OUTPUT_DIRECTORIES` directory;
 * - `loop`: resolving the field meets a symlink loop (`Path.resolve()` raises
 *   `RuntimeError('Symlink loop from …')` before any rule is read), so no command loads it.
 */
export interface ManifestPathRefusal {
  field: ManifestPathField;
  rule: "absolute" | "outside" | "overlap" | "collides" | "loop";
}

/**
 * The manifest's five path fields as written, as resolved, and the loader's verdict.
 *
 * `real` (and `realProjectRoot`) is Python's symlink-following `Path.resolve()`
 * (`pythonResolve()`): what the loader JUDGES, and what `refusal` is computed on, so a link
 * into the model or out of the project is refused as `require_project` refuses it.
 * `resolved` (and the lexical `projectRoot`) is where the extension ACTS, derived from
 * `real` by `actedOnPath()`. For every field inside the project, Python's resolution of
 * `resolved` is `real`.
 */
export interface ManifestPaths {
  projectRoot: string;
  written: Record<ManifestPathField, string>;
  resolved: Record<ManifestPathField, string>;
  realProjectRoot: string;
  real: Record<ManifestPathField, string>;
  refusal: ManifestPathRefusal | undefined;
}

/**
 * Where a build keeps the previous ARXML, `<stem>_old<suffix>` beside *file*, split as
 * Python 3.11's `PurePath.stem`/`.suffix` split a name
 * (`writer.arxml_xml.previous_arxml_path`): `model.` gives `model._old`, where Node's
 * `path.parse` would give `model_old.`.
 */
export function previousArxmlPath(file: string): string {
  const name = path.basename(file);
  const dot = name.lastIndexOf(".");
  const split = dot > 0 && dot < name.length - 1;
  const stem = split ? name.slice(0, dot) : name;
  const suffix = split ? name.slice(dot) : "";
  return path.join(path.dirname(file), `${stem}_old${suffix}`);
}

/**
 * `_validate_project_paths`' verdict on already-resolved paths, in its order, or
 * `undefined` when the loader accepts all five:
 * 1. rule 3 for `dist_root` then `views_root`, on the WRITTEN spelling (so both hosts answer
 *    alike);
 * 2. rule 1 for every field (`_contains`, exact-case, the project directory itself accepted);
 * 3. rule 2 for every field but `model_root` (`_overlaps`, case-folded on every platform);
 * 4. rule 4 for `outputs.needs` (`_needs_collision`, case-folded; *manifest* only when given).
 *
 * What rule 4 does not reach is stated in `project._validate_project_paths`' docstring. A
 * symlink loop is not judged here; `readManifestPaths()` places it before these rules.
 */
export function manifestPathRefusal(
  projectRoot: string,
  written: Record<ManifestPathField, string>,
  resolved: Record<ManifestPathField, string>,
  manifest?: string,
): ManifestPathRefusal | undefined {
  for (const field of ["dist_root", "views_root"] as const) {
    if (isAbsoluteAnywhere(written[field])) {
      return { field, rule: "absolute" };
    }
  }
  for (const field of MANIFEST_PATH_FIELDS) {
    if (!contains(projectRoot, resolved[field])) {
      return { field, rule: "outside" };
    }
  }
  for (const field of MANIFEST_PATH_FIELDS) {
    if (field !== "model_root" && overlapsModelRoot(resolved.model_root, resolved[field])) {
      return { field, rule: "overlap" };
    }
  }
  const kept = [
    resolved["outputs.arxml"],
    previousArxmlPath(resolved["outputs.arxml"]),
    path.join(resolved.dist_root, AUDIT_LOG_FILE),
    path.join(resolved.dist_root, BUILD_REPORT_FILE),
    ...(manifest === undefined ? [] : [manifest]),
  ];
  const needs = resolved["outputs.needs"].toLowerCase();
  if (kept.some((other) => other.toLowerCase() === needs)) {
    return { field: "outputs.needs", rule: "collides" };
  }
  const name = path.basename(needs);
  if (
    path.dirname(needs) === resolved.views_root.toLowerCase() &&
    (name === DIAGRAM_REPORT_FILE || DIAGRAM_FILE_SUFFIXES.some((suffix) => name.endsWith(suffix)))
  ) {
    return { field: "outputs.needs", rule: "collides" };
  }
  for (const directory of REPORT_DIRECTORIES) {
    const folded = path.join(resolved.dist_root, directory).toLowerCase();
    if (needs === folded || needs.startsWith(folded + path.sep)) {
      return { field: "outputs.needs", rule: "collides" };
    }
  }
  for (const directory of NAMED_OUTPUT_DIRECTORIES) {
    if (needs === path.join(resolved.dist_root, directory).toLowerCase()) {
      return { field: "outputs.needs", rule: "collides" };
    }
  }
  return undefined;
}

/**
 * Read the manifest at *manifestPath* once: its five path fields as written (the Pydantic
 * defaults where absent or empty), each resolved against the manifest's directory as
 * `SwcraftProject.resolved_*` resolves it, and the loader's verdict in its order: a symlink
 * loop, then `manifestPathRefusal()` on `real`. An unreadable manifest answers the defaults,
 * which the loader accepts (the CLI fails on it, but not for a path reason).
 *
 * What it does **not** reach: a link created or re-pointed after the call is judged at the
 * next call, not by a watch; `pythonResolve()`'s own limits; `toLowerCase()` stands in for
 * `str.casefold()`, which folds a few letters (`ß`) further; and a manifest the line reader
 * reads differently from PyYAML is judged on what the line reader found.
 */
export function readManifestPaths(manifestPath: string): ManifestPaths {
  const projectRoot = path.resolve(path.dirname(manifestPath));
  let text = "";
  try {
    text = fs.readFileSync(manifestPath, "utf8");
  } catch {
    // An unreadable manifest: every field takes its default below.
  }
  const written: Record<ManifestPathField, string> = {
    model_root: parseModelRoot(text),
    dist_root: parseDistRoot(text),
    views_root: parseViewsRoot(text),
    "outputs.arxml": parseOutputsArxml(text),
    "outputs.needs": parseOutputsNeeds(text),
  };
  // `manifest_path.parent.resolve()`, then `_resolved(project_dir / field)` for each field in
  // the loader's order; the first loop is the loader's first error.
  const realProjectRoot = pythonResolution(projectRoot).path;
  const real = {} as Record<ManifestPathField, string>;
  const resolved = {} as Record<ManifestPathField, string>;
  let loop: ManifestPathRefusal | undefined;
  for (const field of MANIFEST_PATH_FIELDS) {
    const resolution = pythonResolution(joinLikePathlib(realProjectRoot, written[field]));
    real[field] = resolution.path;
    resolved[field] = actedOnPath(projectRoot, realProjectRoot, written[field], resolution.path);
    if (resolution.loop && loop === undefined) {
      loop = { field, rule: "loop" };
    }
  }
  return {
    projectRoot,
    written,
    resolved,
    realProjectRoot,
    real,
    refusal:
      loop ??
      manifestPathRefusal(
        realProjectRoot,
        written,
        real,
        pythonResolution(joinLikePathlib(realProjectRoot, path.basename(manifestPath))).path,
      ),
  };
}

/**
 * Where the extension acts on a field the loader judged at *real*: DERIVED from *real*, so
 * the two cannot name different files however the join, a link or a drive reads.
 *
 * The workspace's own spelling, `path.resolve(projectRoot, written)`, is kept when Python's
 * resolution of it IS *real* (the common case, keeping watches and path tests matching what
 * VS Code reports below a symlinked directory). Otherwise *real*'s part below
 * *realProjectRoot* is re-rooted at *projectRoot*: the case of a link before a following
 * `..` (`lnk/../views`), or a drive-relative `C:views` under a workspace opened through a
 * link to another drive. A *real* outside the project is kept; the verdict refuses it.
 *
 * What it does **not** reach: a re-rooted path is spelled through the link target's names,
 * so an editor holding the file under the link's spelling does not match it; and the
 * Windows half (junction, substituted drive, another drive's current directory) is
 * `pythonResolve()`'s, unmeasured against CPython.
 */
function actedOnPath(
  projectRoot: string,
  realProjectRoot: string,
  written: string,
  real: string,
): string {
  const lexical = path.resolve(projectRoot, written);
  if (pythonResolution(lexical).path === real) {
    return lexical;
  }
  if (real === realProjectRoot) {
    return projectRoot;
  }
  if (contains(realProjectRoot, real)) {
    return projectRoot + real.slice(realProjectRoot.length);
  }
  return real;
}

/**
 * `Path(directory) / written`, as `pythonResolve()` goes on to read it, on the rules of
 * *host* (this host's by default; a test hands `path.win32` or `path.posix`).
 *
 * POSIX: an absolute *written* replaces *directory*, otherwise it is appended with `/`, and
 * `..` is kept for `pythonResolve()` to walk past a link, as pathlib keeps it.
 *
 * Windows joins drive and root apart, as `PureWindowsPath` does: `C:model` under `C:\proj`
 * is `C:\proj\model`, under `D:\proj` it is `C:model`, and a rooted `\model` keeps
 * *directory*'s drive. Appending would make `D:\proj\C:model`, inside the project, so the
 * verdict would accept what rule 1 refuses. `path.win32.resolve()` is that join followed by
 * `ntpath.realpath`'s first step, `abspath` (the drive's current directory, `..` collapsed
 * lexically before any link is read).
 */
export function joinLikePathlib(
  directory: string,
  written: string,
  host: path.PlatformPath = path,
): string {
  if (host.sep === "\\") {
    return host.resolve(directory, written);
  }
  return host.isAbsolute(written) ? written : directory + host.sep + written;
}

/** The extended-length prefixes `_resolved` drops from a Windows answer. */
const WINDOWS_EXTENDED_UNC_PREFIX = "\\\\?\\UNC\\";
const WINDOWS_EXTENDED_PREFIX = "\\\\?\\";

/**
 * `project._resolved(Path(candidate))`: Python 3.11's `Path.resolve()`
 * (`os.path.realpath(strict=False)`) without Windows' extended-length prefix.
 *
 * POSIX transcribes `posixpath._joinrealpath`: each existing symlink is replaced by its
 * target BEFORE a following `..` is applied, a missing component is kept as written, and a
 * loop throws `Symlink loop from '…'`, as `Path.resolve()` raises `RuntimeError`. Windows
 * follows `ntpath.realpath`: normalised lexically first, then the longest existing prefix
 * resolved (`fs.realpathSync.native`) and the rest appended as written.
 *
 * What it does **not** reach: on Windows a junction or substituted drive is answered as
 * Node's native realpath answers it, and a loop reads as a missing component, neither
 * measured against CPython; an `lstat` that fails for any reason reads as "not a link", as
 * non-strict `realpath` does.
 */
export function pythonResolve(candidate: string): string {
  const resolution = pythonResolution(candidate);
  if (resolution.loop) {
    throw new Error(`Symlink loop from '${resolution.path}'`);
  }
  return resolution.path;
}

/** `pythonResolve()`'s answer, with a loop reported rather than thrown. */
function pythonResolution(candidate: string): { path: string; loop: boolean } {
  const [spelled, loop] =
    process.platform === "win32" ? [windowsRealpath(candidate), false] : posixRealpath(candidate);
  if (spelled.startsWith(WINDOWS_EXTENDED_UNC_PREFIX)) {
    return { path: `\\\\${spelled.slice(WINDOWS_EXTENDED_UNC_PREFIX.length)}`, loop };
  }
  if (spelled.startsWith(WINDOWS_EXTENDED_PREFIX)) {
    return { path: spelled.slice(WINDOWS_EXTENDED_PREFIX.length), loop };
  }
  return { path: spelled, loop };
}

function windowsRealpath(candidate: string): string {
  const absolute = path.resolve(candidate);
  const tail: string[] = [];
  let head = absolute;
  for (;;) {
    try {
      return path.join(fs.realpathSync.native(head), ...tail);
    } catch {
      const parent = path.dirname(head);
      if (parent === head) {
        return absolute;
      }
      tail.unshift(path.basename(head));
      head = parent;
    }
  }
}

function posixRealpath(candidate: string): [string, boolean] {
  // `abspath(_joinrealpath('', filename))`; every caller hands an absolute path.
  const [walked, ok] = joinRealpath("/", candidate, new Map());
  return [path.posix.resolve(walked), !ok];
}

/** `posixpath._joinrealpath(path, rest, strict=False, seen)`, line for line. */
function joinRealpath(
  start: string,
  restIn: string,
  seen: Map<string, string | undefined>,
): [string, boolean] {
  let current = start;
  let rest = restIn;
  if (rest.startsWith("/")) {
    rest = rest.slice(1);
    current = "/";
  }
  while (rest !== "") {
    const cut = rest.indexOf("/");
    const name = cut === -1 ? rest : rest.slice(0, cut);
    rest = cut === -1 ? "" : rest.slice(cut + 1);
    if (name === "" || name === ".") {
      continue;
    }
    if (name === "..") {
      // *current* is always absolute here, so Python's relative-`..` branches never apply.
      current = path.posix.dirname(current);
      continue;
    }
    const next = current === "/" ? `/${name}` : `${current}/${name}`;
    let isLink = false;
    try {
      isLink = fs.lstatSync(next).isSymbolicLink();
    } catch {
      isLink = false;
    }
    if (!isLink) {
      current = next;
      continue;
    }
    if (seen.has(next)) {
      const known = seen.get(next);
      if (known !== undefined) {
        current = known;
        continue;
      }
      // A loop: non-strict realpath stops here and keeps the rest as written.
      return [rest === "" ? next : `${next}/${rest}`, false];
    }
    seen.set(next, undefined);
    let target: string;
    try {
      target = fs.readlinkSync(next);
    } catch {
      current = next;
      continue;
    }
    const [resolvedLink, ok] = joinRealpath(current, target, seen);
    if (!ok) {
      return [rest === "" ? resolvedLink : `${resolvedLink}/${rest}`, false];
    }
    current = resolvedLink;
    seen.set(next, current);
  }
  return [current, true];
}

/**
 * The model root of the project rooted at *manifestPath*
 * (`SwcraftProject.resolved_model_root`), or `undefined` when the loader would refuse the
 * manifest's paths, so nothing watches or walks a directory no command reads a model from
 * (`model_root: ..` would have the activation scan walk the project's parent).
 */
export function resolveModelRoot(manifestPath: string): string | undefined {
  const paths = readManifestPaths(manifestPath);
  return paths.refusal === undefined ? paths.resolved.model_root : undefined;
}

/** `_contains`: is *candidate* *container* itself, or inside it? Exact-case. */
function contains(container: string, candidate: string): boolean {
  return candidate === container || candidate.startsWith(container + path.sep);
}

/**
 * `_overlaps`, the loader's rule 2: do *modelRoot* and *target* name one place, or is either
 * inside the other, case-folded on every platform (`toLowerCase()` for `str.casefold()`).
 */
function overlapsModelRoot(modelRoot: string, target: string): boolean {
  const folded = (spelled: string): string => spelled.toLowerCase();
  return contains(folded(modelRoot), folded(target)) || contains(folded(target), folded(modelRoot));
}

// --- views_root resolution (SWCraft: Diagrams) -------------------------------
//
// `swcraft generate diagrams` writes every view, its page, `index.html` and the report into
// `views_root`, the one diagram address. The Diagrams command and the panel both need it
// resolved, and refused where the CLI would refuse it.

/** Mirrors Python's `SwcraftProject.views_root` default. */
export const DEFAULT_VIEWS_ROOT = "views";

/**
 * The top-level `views_root` of *manifestText*, or `DEFAULT_VIEWS_ROOT` when absent or
 * empty (`parseModelRoot()`'s rule).
 */
export function parseViewsRoot(manifestText: string): string {
  return parseTopLevelScalar(manifestText, "views_root", DEFAULT_VIEWS_ROOT);
}

/**
 * Python's `project.is_absolute_anywhere`: absolute under POSIX's rules **or** Windows',
 * because a manifest is a committed file read on both.
 *
 * Not `path.win32.isAbsolute`, which accepts a bare-rooted `\v` and a lone `\\host`, where
 * `PureWindowsPath.is_absolute()` needs a drive **and** a root: `C:\v`, `C:/v` and
 * `\\host\share` are absolute, `C:v` is not. What it does not reach is the device namespace
 * (`\\?\`, `\\.\`), read as a UNC-shaped drive only because the shape matches.
 */
function isAbsoluteAnywhere(written: string): boolean {
  return (
    path.posix.isAbsolute(written) ||
    /^[A-Za-z]:[\\/]/.test(written) ||
    /^[\\/]{2}[^\\/]+[\\/]/.test(written)
  );
}

/**
 * Why the CLI would refuse *written* as `views_root`, or `undefined` when it would accept
 * it: `swcraft clean-outputs`' three refusals (`_clean_root_problems` in
 * packages/tools/src/swcraft_tools/cli/build_commands.py), in its order and sentences:
 * written absolute, the project directory itself, outside the project directory.
 *
 * *projectRoot* and *target* are Python's symlink-following resolutions
 * (`ManifestPaths.real`), so a link leading out of the project is refused; *written* is the
 * manifest's spelling. Containment is exact-case, as `Path.is_relative_to` compares. The
 * loader's overlap with `model_root` is not one of these; `resolveViewsRoot()` applies it
 * after them.
 */
export function viewsRootRefusal(
  projectRoot: string,
  written: string,
  target: string,
): string | undefined {
  if (isAbsoluteAnywhere(written)) {
    return (
      `swcraft.yaml declares views_root: ${written}, an absolute path — ` +
      "it must be written relative to the project directory."
    );
  }
  if (target === projectRoot) {
    return (
      `swcraft.yaml declares views_root: ${written}, which is the project directory ` +
      "itself — cleaning it would remove the model."
    );
  }
  if (!target.startsWith(projectRoot + path.sep)) {
    return (
      `swcraft.yaml declares views_root: ${written}, which resolves to '${target}', ` +
      `outside the project directory '${projectRoot}'.`
    );
  }
  return undefined;
}

/**
 * `views_root` resolved for the project rooted at *manifestPath*, or the CLI's
 * reason for refusing it. `path` is always the resolved directory, so a refusal
 * can still be reported beside it.
 */
export type ViewsRootResolution =
  | { ok: true; path: string }
  | { ok: false; path: string; reason: string };

/**
 * Resolve the absolute views root of the project rooted at *manifestPath*
 * (`SwcraftProject.resolved_views_root`), `ok` only when `viewsRootRefusal()` finds nothing
 * and the directory does not overlap `model_root` (refused in
 * `viewsRootOverlapsModelRefusal()`'s sentence), so the Diagrams press never opens a
 * directory the CLI would never have written. An unreadable manifest answers the default. A
 * manifest refused for ANOTHER field is still `ok` here: the press leaves that to the CLI's
 * sentence, and the panel reads `readManifestPaths()`' whole-manifest verdict.
 */
export function resolveViewsRoot(manifestPath: string): ViewsRootResolution {
  const paths = readManifestPaths(manifestPath);
  const target = paths.resolved.views_root;
  // Judged and spelled on Python's resolution, as the CLI does; `path` is the acted-on
  // spelling the press opens.
  const real = paths.real.views_root;
  const reason = viewsRootRefusal(paths.realProjectRoot, paths.written.views_root, real);
  if (reason !== undefined) {
    return { ok: false, path: target, reason };
  }
  const modelRoot = paths.real.model_root;
  if (overlapsModelRoot(modelRoot, real)) {
    return { ok: false, path: target, reason: viewsRootOverlapsModelRefusal(real, modelRoot) };
  }
  return { ok: true, path: target };
}

/**
 * `ManifestPathOverlapsModelError.render(full_path=True)` for `views_root`
 * (packages/tools/src/swcraft_tools/project.py), byte for byte: the sentence the CLI
 * prints after `Error: ` when it loads such a manifest.
 */
export function viewsRootOverlapsModelRefusal(target: string, modelRoot: string): string {
  return (
    `Manifest field 'views_root' resolves to '${target}', which overlaps 'model_root' ` +
    `('${modelRoot}'). Generated paths must be outside the model: generating or ` +
    "cleaning them would otherwise write into, or delete, the model that is the source of truth."
  );
}

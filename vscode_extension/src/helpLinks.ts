// The logic of the extension's links: a finding's rule name in the Problems
// panel (opens that rule's row on the guide's *Checks* page), the *Learn more*
// button of the two refusals answered by a guide section, and the URI handler's
// two routes, `/guide` and `/model`.
//
// No `vscode` import: it runs under plain `node --test`. The adapter
// (commands.ts) calls `asExternalUri`, sets `Diagnostic.code`, registers the URI
// handler and shows the notifications; every decision is made here. Pure but for
// one default: `storageName.ts`' `resolveModelFile()` reads real paths.
//
// A rule link is `<uriScheme>://<extension id>/guide?page=…&anchor=…`; the
// adapter maps it through `vscode.env.asExternalUri` and uses the result
// unaltered, as that API's documentation asks. The handler opens the **served**
// guide (the release that wrote the finding) at `<page>#<anchor>`, so no rule
// list ships here.
//
// A section link, `vscode://<extension id>/guide?section=<page>#<anchor>`, is
// what the MCP tool `guide` composes (`swcraft_tools.guide_search.section_link()`);
// its scheme is fixed to `vscode`, so in Insiders or a fork it opens the stable
// editor, if any.
//
// A model link, `vscode://<extension id>/model?path=<storage name>`, opens that
// file of the window's project at its identity line.
//
// Any web page can raise these links, so each route accepts only its own
// alphabet: no filesystem path, URL, `..` or backslash passes, and the most a
// forged link can do is open a page of the served guide (or its *page not
// found*) or a model file.

import type { RunOutput } from "./commandCore";
import { channelQuote, checkStorageName, resolveModelFile } from "./storageName";

/** The guide's generated *Checks* page: one row per contract rule, each anchored by its rule id. */
export const RULES_PAGE = "reference/rules/";

/**
 * The hand-written sections a *Learn more* opens. Each anchor is declared in its
 * page's heading as `{ #<anchor> }`, so rewording the heading moves no link
 * (`helpLinks.test.ts` reads the page and asserts it).
 */
export const HELP_TOPICS = {
  /** No project in this window: no folder open, or no `swcraft.yaml` in it. */
  project: { page: "vscode/", anchor: "turning-a-folder-into-a-project" },
  /** The project's `swcraft.yaml` pins another release than the installed one. */
  pin: { page: "upgrading/", anchor: "the-procedure" },
} as const;

export type HelpTopic = keyof typeof HELP_TOPICS;

/** Every page a guide link may open: the help table's, and nothing else. */
export const HELP_PAGES: readonly string[] = [
  RULES_PAGE,
  ...Object.values(HELP_TOPICS).map((topic) => topic.page),
];

/**
 * The fixed opening of the CLI's toolchain-pin refusal line
 * (`swcraft_tools.cli.project_args.check_toolchain_pin`); the read-only commands'
 * `Warning:` never matches. `test_cli.py` asserts the refusal starts with it.
 */
export const PIN_REFUSAL_PREFIX = "Error: this project's swcraft.yaml pins toolchain_version";

/** The path of every guide link. */
const HELP_URI_PATH = "/guide";

/**
 * An anchor the handler accepts: the rule-id alphabet, bounded. Equal to
 * `swcraft_tools.guide_reference.render.ANCHOR_PATTERN`
 * (`test_guide_reference_rules.py` asserts it), so every rule's anchor links.
 */
export const ANCHOR_PATTERN = /^[A-Za-z0-9._-]{1,128}$/;

/** The components of a URI this module reads and writes — a `vscode.Uri` is one. */
export interface UriParts {
  readonly scheme: string;
  readonly authority: string;
  readonly path: string;
  readonly query: string;
  /** Read only for a section link's anchor; a rule link carries none. */
  readonly fragment?: string;
}

/**
 * The page of a section id: segments of lowercase letters, digits, `_` and `-`,
 * separated by `/`. `test_guide_search.py` asserts every real section id matches
 * this and `SECTION_ANCHOR_PATTERN`, so no link the tool composes is refused.
 */
export const SECTION_PAGE_PATTERN = /^[a-z0-9_-]+(?:\/[a-z0-9_-]+)*$/;

/** The anchor of a section id: one segment of the id's alphabet. */
export const SECTION_ANCHOR_PATTERN = /^[a-z0-9_-]+$/;

/** The longest section id a link may carry, anchor included: the guide tool's own request bound. */
export const SECTION_ID_LIMIT = 200;

/**
 * The served guide's path of a section *page*: the site uses directory URLs
 * (`how-to` → `how-to/`), and the home page `index` is the root, `""`.
 */
export function sectionPagePath(page: string): string {
  return page === "index" ? "" : `${page}/`;
}

/**
 * The guide link `<scheme>://<extension id>/guide?page=<page>[&anchor=<anchor>]`,
 * before `asExternalUri`, or `undefined` when the handler would refuse it (the
 * caller then shows the plain rule name). *scheme* is `vscode.env.uriScheme`.
 */
export function helpUri(
  scheme: string,
  extensionId: string,
  page: string,
  anchor?: string,
): UriParts | undefined {
  if (!HELP_PAGES.includes(page) || (anchor !== undefined && !ANCHOR_PATTERN.test(anchor))) {
    return undefined;
  }
  const query = new URLSearchParams({ page });
  if (anchor !== undefined) {
    query.set("anchor", anchor);
  }
  return { scheme, authority: extensionId, path: HELP_URI_PATH, query: query.toString() };
}

/** What a guide link asks for, once validated. */
export type HelpRequest =
  | { kind: "open"; page: string; anchor: string | undefined }
  | { kind: "refused"; reason: string };

/**
 * Validate a URI handed to the extension's `UriHandler`: this extension's id
 * (case-insensitively, as VS Code compares extension ids), the `/guide` path,
 * and either a **rule link** (one `page` of the help table, at most one `anchor`
 * of the rule-id alphabet) or a **section link** (one `section`, no `page` or
 * `anchor`). Other parameters, such as a desktop mapping's window id, are
 * ignored; anything else is refused with the reason.
 *
 * `URLSearchParams` decodes VS Code's already-decoded `query` a second time.
 * That is safe here: every accepted value is checked after the decode against an
 * alphabet with no `%`, `+` or space.
 */
export function parseHelpUri(
  uri: Pick<UriParts, "authority" | "path" | "query" | "fragment">,
  extensionId: string,
): HelpRequest {
  if (uri.authority.toLowerCase() !== extensionId.toLowerCase()) {
    return { kind: "refused", reason: "the link names another extension" };
  }
  if (uri.path !== HELP_URI_PATH) {
    return { kind: "refused", reason: "the link's path is not the guide's" };
  }
  const query = new URLSearchParams(uri.query);
  if (query.has("section")) {
    return parseSection(query, uri.fragment ?? "");
  }
  const pages = query.getAll("page");
  if (pages.length !== 1 || !HELP_PAGES.includes(pages[0])) {
    return { kind: "refused", reason: "the link names no page of the guide's help table" };
  }
  const anchors = query.getAll("anchor");
  if (anchors.length > 1 || (anchors.length === 1 && !ANCHOR_PATTERN.test(anchors[0]))) {
    return { kind: "refused", reason: "the link's anchor is not a rule or section name" };
  }
  return { kind: "open", page: pages[0], anchor: anchors[0] };
}

/** The path of the model route: `vscode://<extension id>/model?path=<storage name>`. */
export const MODEL_URI_PATH = "/model";

/** What a model link asks for, once validated: a storage name under `model_root`, or the refusal. */
export type ModelLinkRequest =
  | { kind: "open"; path: string }
  | { kind: "refused"; reason: string; path: string | undefined };

/**
 * Validate a model link: this extension's id (case-insensitively), the `/model`
 * path, exactly one `path` parameter, and that a storage name
 * `checkStorageName()` accepts. Other parameters are ignored. Whether the file
 * exists is the adapter's (`resolveModelFile()`, which repeats the lexical check
 * as the one gate every open passes).
 *
 * `vscode.Uri` keeps `query` already percent-decoded, so it is split by hand and
 * never decoded again (`queryValues()`). What this does not reach: an encoded
 * `&` or `=` inside the path splits the parameter (no valid storage name holds
 * one). A refusal carries the path only for the output channel's line, never a
 * notification, since any web page can raise such a link.
 */
export function parseModelUri(
  uri: Pick<UriParts, "authority" | "path" | "query">,
  extensionId: string,
): ModelLinkRequest {
  if (uri.authority.toLowerCase() !== extensionId.toLowerCase()) {
    return { kind: "refused", reason: "the link names another extension", path: undefined };
  }
  if (uri.path !== MODEL_URI_PATH) {
    return { kind: "refused", reason: "the link's path is not the model's", path: undefined };
  }
  const paths = queryValues(uri.query, "path");
  if (paths.length !== 1) {
    return { kind: "refused", reason: "the link names no single model file", path: undefined };
  }
  const reason = checkStorageName(paths[0]);
  if (reason !== undefined) {
    return { kind: "refused", reason, path: paths[0] };
  }
  return { kind: "open", path: paths[0] };
}

/**
 * The values of every *name* parameter of an already-decoded *query*: split at
 * `&`, each part at its first `=`, nothing decoded (see `parseModelUri()`).
 */
export function queryValues(query: string, name: string): string[] {
  const values: string[] = [];
  for (const part of query === "" ? [] : query.split("&")) {
    const equals = part.indexOf("=");
    const key = equals === -1 ? part : part.slice(0, equals);
    if (key === name) {
      values.push(equals === -1 ? "" : part.slice(equals + 1));
    }
  }
  return values;
}

/** The one output-channel line of a refused model link: the reason, and the path quoted when it had one. */
export function refusedModelLinkLine(request: {
  reason: string;
  path: string | undefined;
}): string {
  const quoted = request.path === undefined ? "" : ` ("${channelQuote(request.path)}")`;
  return `SWCraft: a model link was not opened: ${request.reason}${quoted}.`;
}

/** Which route of the extension's `UriHandler` a link takes, by its path. */
export function uriRoute(uri: Pick<UriParts, "path">): "model" | "guide" {
  return uri.path === MODEL_URI_PATH ? "model" : "guide";
}

/** The one notification of a model link: it quotes nothing from the link. */
export const NO_PROJECT_FOR_MODEL_LINK =
  "SWCraft: a model link was not opened — no SWCraft project is open in this window.";

/** What the handler does with a model link: one notification, one output-channel line, or the open. */
export type ModelLinkAction =
  | { kind: "notify"; message: string }
  | { kind: "log"; line: string }
  | { kind: "open"; path: string; name: string };

/**
 * The model route's decision, in order: a link `parseModelUri()` refuses is one
 * output-channel line; a valid link with no project open (*modelRoot*
 * `undefined`) is the one notification; a name *resolve* does not resolve to a
 * regular file under *modelRoot* is one line again; anything else opens. Only
 * the absence of a project, a fact of this window rather than of the link, is
 * said aloud, since any web page can raise such a link.
 */
export function modelLinkAction(
  uri: Pick<UriParts, "authority" | "path" | "query">,
  extensionId: string,
  modelRoot: string | undefined,
  resolve: typeof resolveModelFile = resolveModelFile,
): ModelLinkAction {
  const request = parseModelUri(uri, extensionId);
  if (request.kind === "refused") {
    return { kind: "log", line: refusedModelLinkLine(request) };
  }
  if (modelRoot === undefined) {
    return { kind: "notify", message: NO_PROJECT_FOR_MODEL_LINK };
  }
  const resolved = resolve(modelRoot, request.path);
  if (resolved.kind === "refused") {
    return {
      kind: "log",
      line: refusedModelLinkLine({ reason: resolved.reason, path: request.path }),
    };
  }
  return { kind: "open", path: resolved.path, name: request.path };
}

/**
 * A section link's request: exactly one `section`, no `page` or `anchor`; the
 * anchor is the URI's *fragment* or an encoded `#` in the query, never both.
 */
function parseSection(query: URLSearchParams, fragment: string): HelpRequest {
  const sections = query.getAll("section");
  if (sections.length !== 1 || query.has("page") || query.has("anchor")) {
    return { kind: "refused", reason: "the link names more than one section" };
  }
  const id = sections[0];
  const hash = id.indexOf("#");
  const page = hash === -1 ? id : id.slice(0, hash);
  const inQuery = hash === -1 ? undefined : id.slice(hash + 1);
  if (inQuery !== undefined && fragment !== "") {
    return { kind: "refused", reason: "the link names more than one section" };
  }
  return sectionTarget(page, inQuery ?? (fragment === "" ? undefined : fragment));
}

/**
 * Check a guide section's *page* and *anchor* (the rule both the section link and
 * `swcraft.openGuide` follow) and map the page to the served guide's path.
 */
function sectionTarget(page: string, anchor: string | undefined): HelpRequest {
  const complete = anchor === undefined ? page.length : page.length + 1 + anchor.length;
  if (complete > SECTION_ID_LIMIT || !SECTION_PAGE_PATTERN.test(page)) {
    return { kind: "refused", reason: "the link's section is not a section of the guide" };
  }
  if (anchor !== undefined && !SECTION_ANCHOR_PATTERN.test(anchor)) {
    return { kind: "refused", reason: "the link's anchor is not a rule or section name" };
  }
  return { kind: "open", page: sectionPagePath(page), anchor };
}

/**
 * The page and anchor of `swcraft.openGuide`'s optional argument, or `undefined`
 * (the guide's front page) for anything `sectionTarget()` refuses. A `command:`
 * link can pass any argument, so it is checked.
 */
export function guideCommandArg(raw: unknown): { page: string; anchor?: string } | undefined {
  if (typeof raw !== "object" || raw === null) {
    return undefined;
  }
  const { page, anchor } = raw as { page?: unknown; anchor?: unknown };
  if (typeof page !== "string" || (anchor !== undefined && typeof anchor !== "string")) {
    return undefined;
  }
  const target = sectionTarget(page, anchor);
  if (target.kind === "refused") {
    return undefined;
  }
  return { page: target.page, anchor: target.anchor };
}

/**
 * The served guide's address of *page* at *anchor*. *served* is the root
 * `swcraft docs` announced; it ends in `/` (guideServer.ts's pattern requires it).
 */
export function guidePageUrl(served: string, page: string, anchor?: string): string {
  return `${served}${page}${anchor === undefined ? "" : `#${anchor}`}`;
}

/**
 * VS Code's integrated browser's open command. Where it exists,
 * `simpleBrowser.show` hands every address to it, and it opens a new tab on each
 * call unless `reuseUrlFilter` names one to reuse (read from VS Code 1.143.1's
 * shipped code).
 */
export const INTEGRATED_BROWSER_OPEN = "workbench.action.browser.open";

/** The Simple Browser's command: one view per window, which it navigates when open. */
export const SIMPLE_BROWSER_SHOW = "simpleBrowser.show";

/** The command `openGuidePage()` runs to show the guide, and its arguments. */
export interface GuideTabOpen {
  readonly command: string;
  readonly args: readonly unknown[];
}

/**
 * The integrated browser's glob for the served guide's tabs: the guide's root as
 * *address* (its page *page*, mapped through `asExternalUri`) shows it, path
 * prefix included (it tells two guides apart behind a path-based port proxy),
 * and anything under it. What it does not reach: a mapping that rewrites *page*
 * itself reuses only that page's tab; an IPv6 host's brackets read as a glob
 * character class (the served host is `127.0.0.1`).
 */
export function guideTabFilter(address: string, page: string): string {
  const url = new URL(address);
  const root = url.pathname.endsWith(page)
    ? url.pathname.slice(0, url.pathname.length - page.length)
    : url.pathname;
  return `${url.protocol}//${url.host}${root.endsWith("/") ? root : `${root}/`}**`;
}

/**
 * One guide tab per window: which command shows *address* (the served guide's
 * *page*, mapped through `asExternalUri`), given *commands*
 * (`vscode.commands.getCommands(true)`).
 *
 * - The integrated browser present (VS Code 1.143.1 has it): its open command
 *   with `reuseUrlFilter` (`guideTabFilter`), so a tab of this guide is reused;
 *   the Simple Browser passes the address on without the filter.
 * - Absent: `simpleBrowser.show`, whose one view is navigated when open.
 */
export function guideTabOpen(
  commands: readonly string[],
  address: string,
  page: string,
): GuideTabOpen {
  if (commands.includes(INTEGRATED_BROWSER_OPEN)) {
    return {
      command: INTEGRATED_BROWSER_OPEN,
      args: [{ url: address, reuseUrlFilter: guideTabFilter(address, page) }],
    };
  }
  return { command: SIMPLE_BROWSER_SHOW, args: [address] };
}

/**
 * The guide section a failed CLI run's notification offers under *Learn more*:
 * `pin` when *run*'s stderr holds a line starting with `PIN_REFUSAL_PREFIX`,
 * else `undefined` (a run that could not launch cannot serve the guide either).
 */
export function helpTopicForRun(run: RunOutput): HelpTopic | undefined {
  if (run.spawnFailed) {
    return undefined;
  }
  return run.stderr.split(/\r?\n/).some((line) => line.startsWith(PIN_REFUSAL_PREFIX))
    ? "pin"
    : undefined;
}

/**
 * The rule a diagnostic's `code` names, in either form the extension writes: the
 * plain rule name, or `{ value: <rule>, target: <link> }`. A number names none.
 */
export function diagnosticRule(
  code: string | number | { value: string | number; target?: unknown } | undefined,
): string | undefined {
  if (typeof code === "string") {
    return code;
  }
  if (typeof code === "object" && code !== null && typeof code.value === "string") {
    return code.value;
  }
  return undefined;
}

/**
 * How long one `asExternalUri` call for a rule's link may take before it is given
 * up, so a hanging remote resolver does not leave its rule unlinked for the
 * window's life.
 */
export const RULE_LINK_CALL_LIMIT_MS = 10_000;

/** What `RuleLinkResolver` reads of a mapped link: its scheme. */
export interface MappedLink {
  readonly scheme: string;
}

/**
 * The rule links of the Problems panel, each rule's link mapped through *map*
 * (the adapter's `asExternalUri` call; `undefined` for a rule no link can name).
 *
 * A result in the product scheme (*productScheme*) is the desktop's mapping,
 * which only appends a fixed window id (VS Code 1.143.1's workbench code), so it
 * is kept for the window. Any other result, a web workbench's `https` address,
 * is assumed single-use and serves one run only. What this does not reach:
 * within one run a rule's findings share one link, so in a web workbench a
 * second click may do nothing; the web path is unmeasured.
 *
 * A run waits at most *waitMs*; an unresolved rule is its plain name in that
 * run. A call past *callLimitMs* is logged once and dropped (a late result is
 * discarded); a rejected call is dropped silently. Calls in flight are shared.
 */
export class RuleLinkResolver<U extends MappedLink> {
  private readonly kept = new Map<string, U>();
  private readonly pending = new Map<string, Promise<U | undefined>>();

  constructor(
    private readonly productScheme: string,
    private readonly map: (rule: string) => PromiseLike<U> | undefined,
    private readonly callLimitMs: number = RULE_LINK_CALL_LIMIT_MS,
  ) {}

  /**
   * The links of *rules* for one run: the kept ones, and those whose call
   * answers within *waitMs*. *log* takes a call's time-out line.
   */
  async resolve(
    rules: readonly string[],
    waitMs: number,
    log: (line: string) => void,
  ): Promise<ReadonlyMap<string, U>> {
    const links = new Map<string, U>();
    const waits: Promise<void>[] = [];
    for (const rule of new Set(rules)) {
      const kept = this.kept.get(rule);
      if (kept !== undefined) {
        links.set(rule, kept);
        continue;
      }
      const call = this.pending.get(rule) ?? this.call(rule, log);
      if (call === undefined) {
        continue;
      }
      waits.push(
        call.then((link) => {
          if (link !== undefined) {
            links.set(rule, link);
          }
        }),
      );
    }
    if (waits.length > 0) {
      let timer: ReturnType<typeof setTimeout> | undefined;
      await Promise.race([
        Promise.all(waits),
        new Promise<void>((resolve) => {
          timer = setTimeout(resolve, waitMs);
        }),
      ]);
      clearTimeout(timer);
    }
    // A snapshot: a call that answers after the wait adds nothing to this run.
    return new Map(links);
  }

  /** Issue *rule*'s call, registered in `pending` until it settles or passes its limit. */
  private call(rule: string, log: (line: string) => void): Promise<U | undefined> | undefined {
    let mapping: PromiseLike<U> | undefined;
    try {
      mapping = this.map(rule);
    } catch {
      return undefined;
    }
    if (mapping === undefined) {
      return undefined;
    }
    const mapped = mapping;
    const call = new Promise<U | undefined>((resolve) => {
      let settled = false;
      const finish = (link: U | undefined): void => {
        settled = true;
        clearTimeout(timer);
        if (this.pending.get(rule) === call) {
          this.pending.delete(rule);
        }
        resolve(link);
      };
      const timer = setTimeout(() => {
        if (!settled) {
          log(
            `SWCraft: the guide link of rule ${rule} was not resolved within ` +
              `${this.callLimitMs / 1000} s; it is shown as its plain name, and the next ` +
              "check asks again.",
          );
          finish(undefined);
        }
      }, this.callLimitMs);
      Promise.resolve(mapped).then(
        (link) => {
          if (settled) {
            return;
          }
          if (link.scheme === this.productScheme) {
            this.kept.set(rule, link);
          }
          finish(link);
        },
        () => {
          if (!settled) {
            finish(undefined);
          }
        },
      );
    });
    this.pending.set(rule, call);
    return call;
  }
}

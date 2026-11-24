// Pure logic of the panel review: the pending record's reader, which records a window shows, the
// Pending Change Sets view's nodes, the review tab's page, the approval check and the decision
// record with its first-answer-wins write.
//
// The other half is `packages/mcp/src/swcraft_mcp/panel_review.py`. The server writes
// `<change_set_id>.<question>.pending.json` into `<SWCraft home>/reviews/` and waits; this side
// writes `<change_set_id>.<question>.decision.json` beside it, accepted only when `format`,
// `change_set_id`, `question`, `wait` and `digest` all equal the server's record. `wait` is drawn
// afresh for every wait, so a decision never answers another wait filed under the same name. The
// record formats are that module's, key for key; `fixtures/review/` holds one record of each kind
// and both halves' tests read them.
//
// No `vscode` import: it runs under plain `node --test`; `commands.ts` hosts the tab and the view.
// `writeDecision` is the one function that touches a file, and it takes `fs` as a parameter.
//
// What it does **not** reach: neither the digest nor the wait token is a secret, so the binding of
// an approval to the shown text holds only against a writer that goes through this tab; a process
// with the user's full rights can write a decision of its own. The panel review's strength is the
// client's confinement to its workspace.

import { createHash, randomBytes } from "node:crypto";
import type * as nodeFs from "node:fs";
import * as path from "node:path";

import { type Platform, platformPath } from "./toolchain";

/** The record format both halves write. Any other value is refused. */
export const REVIEW_FORMAT = 2;

/**
 * How old a pending record's mtime may be and still belong to a live wait: four missed heartbeats
 * (the server refreshes it every 15 s). `panel_review.py`'s `STALE_SECONDS` is the same bound, and
 * the shared-vector tests on both sides assert the same literal.
 */
export const STALE_MS = 60_000;

/** The directory under the SWCraft home both halves meet in. */
export const REVIEWS_DIRNAME = "reviews";

export const PENDING_SUFFIX = ".pending.json";
export const DECISION_SUFFIX = ".decision.json";
const TMP_SUFFIX = ".tmp";

/** The two questions a change set can put: the delta, then (only after it) the safety one. */
export const QUESTIONS = ["delta", "safety"] as const;
export type Question = (typeof QUESTIONS)[number];

export type Decision = "approve" | "reject";

/**
 * The change-set id, as the engine allocates it (`editing/audit.py`'s `CHANGE_SET_ID_PATTERN`). It
 * becomes a file name, so a record carrying any other id is refused first.
 */
export const CHANGE_SET_ID_PATTERN = /^cs-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{6}$/;

const DIGEST_PATTERN = /^sha256:[0-9a-f]{64}$/;
/** `panel_review.new_wait_token`'s shape: 32 lowercase hex digits. */
const WAIT_PATTERN = /^[0-9a-f]{32}$/;
/** `created`, as `panel_review._utc` writes it. */
const UTC_PATTERN = /^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$/;

/** The line a tab shows when its record vanished. */
export const NO_LONGER_WAITING =
  "No longer waiting: answered elsewhere, or the assistant stopped waiting.";
/** The line a tab shows when another window answered first. */
export const ALREADY_ANSWERED = "This change set was already answered.";
/**
 * The line a tab shows, its buttons left live, when the click met an answer to an earlier copy of
 * the question (put back under a fresh wait token) still at the decision name: the server clears
 * that answer, so the question is still open.
 */
export const EARLIER_ANSWER_IN_PLACE =
  "Not sent: an answer to an earlier copy of this question was still in place. Choose again.";
/** A row's label when the assistant stated no intent. */
export const NO_INTENT = "(no intent given)";
/** The tab's header, above the meta line. */
export const REVIEW_HEADER = "Change set waiting for your decision";

/** The bound a project or client name is flattened to in the meta line and the tooltip. */
const NAME_LIMIT = 80;

// --- The pending record -----------------------------------------------------

/** One pending question, as `panel_review.pending_record` writes it. */
export interface PendingRecord {
  format: typeof REVIEW_FORMAT;
  change_set_id: string;
  question: Question;
  /** The server's token for this one wait; a decision carries it back. */
  wait: string;
  baseline: string;
  project: { name: string; manifest: string };
  /** The MCP client's own `clientInfo.name`, for information only; nothing decides on it. */
  client: string | null;
  intent: string | null;
  text: string;
  digest: string;
  created: string;
  /**
   * The warning's lines in `text`, `[start, end)`, present only on a delta question carrying the
   * warning (`review.py::DeltaReview`). `unknown` on purpose: it decides a highlight only, so a bad
   * value costs the highlight (`warningSpan`), never the record.
   */
  warning_lines?: unknown;
}

export type ParseResult = { ok: true; record: PendingRecord } | { ok: false; reason: string };

const PENDING_KEYS = [
  "format",
  "change_set_id",
  "question",
  "wait",
  "baseline",
  "project",
  "client",
  "intent",
  "text",
  "digest",
  "created",
] as const;
/** Every key a record may hold; `warning_lines` is the one it may leave out. */
const ALLOWED_KEYS: readonly string[] = [...PENDING_KEYS, "warning_lines"];
const PROJECT_KEYS = ["name", "manifest"] as const;

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function sameKeys(value: Record<string, unknown>, keys: readonly string[]): boolean {
  const own = Object.keys(value);
  return own.length === keys.length && keys.every((key) => Object.hasOwn(value, key));
}

/**
 * The pending record in *text*, or the reason it is not one.
 *
 * Strict, like the server's decision reader: exactly the record's keys (`warning_lines` may be
 * absent and is not checked here), each of its type, `format` the number 2 (a boolean is not a
 * format), an id the engine could have allocated, a 32-hex-digit wait token and a `sha256:<hex>`
 * digest. Whether the digest is the text's is `checkApproval`'s question, at click time.
 */
export function parsePending(text: string): ParseResult {
  let value: unknown;
  try {
    value = JSON.parse(text);
  } catch {
    return { ok: false, reason: "not JSON" };
  }
  if (!isObject(value)) {
    return { ok: false, reason: "not a JSON object" };
  }
  for (const key of PENDING_KEYS) {
    if (!Object.hasOwn(value, key)) {
      return { ok: false, reason: `no ${key}` };
    }
  }
  // Every key is present, so a set that is not exactly the record's has an unknown one.
  if (!Object.keys(value).every((key) => ALLOWED_KEYS.includes(key))) {
    return { ok: false, reason: "an unknown key" };
  }
  if (value.format !== REVIEW_FORMAT) {
    return { ok: false, reason: "an unknown format" };
  }
  const id = value.change_set_id;
  if (typeof id !== "string" || !CHANGE_SET_ID_PATTERN.test(id)) {
    return { ok: false, reason: "an invalid change_set_id" };
  }
  if (
    typeof value.question !== "string" ||
    !(QUESTIONS as readonly string[]).includes(value.question)
  ) {
    return { ok: false, reason: "an unknown question" };
  }
  if (typeof value.wait !== "string" || !WAIT_PATTERN.test(value.wait)) {
    return { ok: false, reason: "an invalid wait" };
  }
  const project = value.project;
  if (
    !isObject(project) ||
    !sameKeys(project, PROJECT_KEYS) ||
    typeof project.name !== "string" ||
    typeof project.manifest !== "string"
  ) {
    return { ok: false, reason: "an invalid project" };
  }
  for (const key of ["client", "intent"] as const) {
    if (value[key] !== null && typeof value[key] !== "string") {
      return { ok: false, reason: `an invalid ${key}` };
    }
  }
  for (const key of ["baseline", "text"] as const) {
    if (typeof value[key] !== "string") {
      return { ok: false, reason: `an invalid ${key}` };
    }
  }
  if (typeof value.digest !== "string" || !DIGEST_PATTERN.test(value.digest)) {
    return { ok: false, reason: "an invalid digest" };
  }
  const stamp = value.created;
  if (typeof stamp !== "string" || !UTC_PATTERN.test(stamp) || Number.isNaN(Date.parse(stamp))) {
    return { ok: false, reason: "an invalid created" };
  }
  return { ok: true, record: value as unknown as PendingRecord };
}

// --- Which records a window shows -------------------------------------------

/** `<SWCraft home>/reviews`, spelled the way *platform* spells paths. */
export function reviewsDir(home: string, platform: Platform): string {
  return platformPath(platform).join(home, REVIEWS_DIRNAME);
}

function normalisedPath(value: string, platform: Platform): string {
  if (platform !== "windows") {
    return value;
  }
  // `/` as the separator, then case-insensitive: the server writes `Path.resolve().as_posix()`
  // (`C:/Users/…`) while `realpathSync.native` answers `C:\Users\…`.
  return value.replace(/\\/g, "/").toLowerCase();
}

/**
 * Whether *a* and *b* name the same file: exact on POSIX; on Windows with `/` as the separator and
 * case-insensitively, the drive letter included. No `..` or link is resolved: both sides are real
 * paths already (`Path.resolve()` on the server, `fs.realpathSync.native` here).
 */
export function samePath(a: string, b: string, platform: Platform): boolean {
  return normalisedPath(a, platform) === normalisedPath(b, platform);
}

/**
 * Whether a pending record last modified at *mtimeMs* belongs to a live wait at *nowMs*: its
 * heartbeat is younger than `STALE_MS`. The record carries no deadline; its server refreshes the
 * mtime while it waits.
 */
export function isLive(mtimeMs: number, nowMs: number): boolean {
  return nowMs - mtimeMs < STALE_MS;
}

/** The key a tab and a row are known by: `<change_set_id>.<question>`. */
export function reviewKey(record: Pick<PendingRecord, "change_set_id" | "question">): string {
  return `${record.change_set_id}.${record.question}`;
}

/**
 * The identity of one pending record: its key, wait token, manifest, baseline, digest and
 * `created`. The key alone is not one: after a wait ends, another project's server in the same
 * window can draw the same change-set id within the same second, so a tab answers, and is enabled
 * again for, only the record whose identity it rendered. The wait token alone tells two waits
 * apart; the other fields stay beside it. Fields outside the tuple (project name, client, intent)
 * do not count: the server never rewrites a record, and the heartbeat moves only the mtime. A
 * record restored after a sweep carries a fresh wait token, so its tab opens afresh.
 */
export function reviewIdentity(record: PendingRecord): string {
  return JSON.stringify([
    record.change_set_id,
    record.question,
    record.wait,
    record.project.manifest,
    record.baseline,
    record.digest,
    record.created,
  ]);
}

/** Whether *a* and *b* are the same pending record by `reviewIdentity`. */
export function sameReview(a: PendingRecord, b: PendingRecord): boolean {
  return reviewIdentity(a) === reviewIdentity(b);
}

/** The pending record's file name. */
export function pendingFileName(record: Pick<PendingRecord, "change_set_id" | "question">): string {
  return `${reviewKey(record)}${PENDING_SUFFIX}`;
}

/** The decision record's file name. */
export function decisionFileName(
  record: Pick<PendingRecord, "change_set_id" | "question">,
): string {
  return `${reviewKey(record)}${DECISION_SUFFIX}`;
}

// --- What the view and the tab say ------------------------------------------

/**
 * *text* as one bounded line: every control, format or separator code point becomes a space.
 * `project.name` is free text an agent's file tools can edit, so it never reaches a line
 * unflattened.
 */
export function flattenLine(text: string, limit: number): string {
  const flat = text.replace(/[\p{Cc}\p{Cf}\p{Zl}\p{Zp}]/gu, " ");
  const points = Array.from(flat);
  return points.length <= limit ? flat : `${points.slice(0, limit - 1).join("")}…`;
}

/** The HH:MM of *iso* at *utcOffsetMinutes* from UTC. */
export function clockTime(iso: string, utcOffsetMinutes: number): string {
  const shifted = new Date(Date.parse(iso) + utcOffsetMinutes * 60_000);
  const hh = String(shifted.getUTCHours()).padStart(2, "0");
  const mm = String(shifted.getUTCMinutes()).padStart(2, "0");
  return `${hh}:${mm}`;
}

/** This machine's offset from UTC at *iso*, for `clockTime`; the host passes it. */
export function localUtcOffsetMinutes(iso: string): number {
  return -new Date(Date.parse(iso)).getTimezoneOffset();
}

function questionWords(question: Question): string {
  // *Safety*, not *weakened check*: the second question can be a partition trust granted, which
  // weakens nothing (`review.py::render_safety_review`).
  return question === "delta" ? "delta question" : "safety question";
}

/** The record's intent as one line, or *"(no intent given)"*: a row's label and the tab's title. */
export function intentLabel(record: PendingRecord): string {
  return record.intent ? flattenLine(record.intent, Number.POSITIVE_INFINITY) : NO_INTENT;
}

/** The tab's title, unescaped: *"Review: <intent>"*, flattened, or *"Review: (no intent given)"*. */
export function reviewTabTitle(record: PendingRecord): string {
  return `Review: ${intentLabel(record)}`;
}

/** One item of the meta line under the header: a label and its value, unescaped. */
export interface MetaItem {
  label: string;
  value: string;
  /** Shown after the value, outside its emphasis. */
  suffix?: string;
}

/**
 * The meta line under the header: the project's name, the client's name (left out when the server
 * did not know it), the change-set id and *Waiting since*. Names are flattened; `reviewHtml`
 * escapes everything.
 */
export function metaItems(record: PendingRecord, utcOffsetMinutes: number): MetaItem[] {
  const items: MetaItem[] = [
    { label: "Project", value: flattenLine(record.project.name, NAME_LIMIT) },
  ];
  if (record.client !== null) {
    items.push({ label: "Client", value: flattenLine(record.client, NAME_LIMIT) });
  }
  items.push(
    { label: "Change set", value: record.change_set_id },
    { label: "Waiting since", value: clockTime(record.created, utcOffsetMinutes) },
  );
  return items;
}

/**
 * The sentence beside the buttons until an answer, a refusal or a vanished
 * record replaces it. The wait has no deadline, so it names no time.
 */
export const WAITING_LINE =
  "The assistant waits until you answer. Closing this tab does not answer; the Pending Change Sets view reopens it.";

/** One row of the Pending Change Sets view. */
export interface ReviewNode {
  key: string;
  changeSetId: string;
  question: Question;
  label: string;
  description: string;
  tooltip: string;
}

/** The order the view and the tabs know records in: oldest first, then by id, then delta before safety. */
export function compareRecords(a: PendingRecord, b: PendingRecord): number {
  return (
    a.created.localeCompare(b.created) ||
    a.change_set_id.localeCompare(b.change_set_id) ||
    QUESTIONS.indexOf(a.question) - QUESTIONS.indexOf(b.question)
  );
}

/**
 * The view's rows in `compareRecords`' order: the intent as label, the question and its time as
 * description, the change-set id and project name as tooltip. *records* comes from
 * `windowReviews`, already live and sorted, so the view does not filter again.
 */
export function reviewNodes(
  records: readonly PendingRecord[],
  utcOffsetMinutes: number,
): ReviewNode[] {
  return records.map((record) => ({
    key: reviewKey(record),
    changeSetId: record.change_set_id,
    question: record.question,
    label: intentLabel(record),
    description: `${questionWords(record.question)}, since ${clockTime(record.created, utcOffsetMinutes)}`,
    tooltip: `${record.change_set_id}\n${flattenLine(record.project.name, NAME_LIMIT)}`,
  }));
}

/** *text* with the five HTML-significant characters escaped. */
export function escapeHtml(text: string): string {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

/** A CSP nonce's alphabet (base64); anything else could break out of its attribute. */
const NONCE_PATTERN = /^[A-Za-z0-9+/]+={0,2}$/;

/** A fresh nonce for one render of the tab. */
export function newNonce(): string {
  return randomBytes(18).toString("base64");
}

/** The Content Security Policy of the tab: nothing but the one nonce'd style and script. */
export function reviewCsp(nonce: string): string {
  return `default-src 'none'; style-src 'nonce-${nonce}'; script-src 'nonce-${nonce}'`;
}

/**
 * The tab's one script. It computes `sha256` over the `<pre>`'s `textContent` itself and posts
 * `{ decision, digest }`; it never reads a digest from the page. The host answers a refusal with
 * `{ type: "disable", line }` (both buttons disabled, the line shown), and a click that wrote
 * nothing but may be repeated with `{ type: "note", line }` (the line shown, the buttons live).
 */
export const REVIEW_SCRIPT = `(() => {
  const vscode = acquireVsCodeApi();
  const text = document.getElementById("text");
  const status = document.getElementById("status");
  const buttons = Array.from(document.querySelectorAll("button[data-decision]"));
  const disable = (line) => {
    for (const button of buttons) button.disabled = true;
    status.textContent = line;
  };
  const decide = async (decision) => {
    for (const button of buttons) button.disabled = true;
    try {
      const bytes = new TextEncoder().encode(text.textContent);
      const hash = new Uint8Array(await crypto.subtle.digest("SHA-256", bytes));
      let hex = "";
      for (const byte of hash) hex += byte.toString(16).padStart(2, "0");
      vscode.postMessage({ decision, digest: "sha256:" + hex });
    } catch (error) {
      disable("The answer could not be sent: " + String(error));
    }
  };
  for (const button of buttons) {
    button.addEventListener("click", () => {
      void decide(button.dataset.decision);
    });
  }
  window.addEventListener("message", (event) => {
    const message = event.data;
    if (message && message.type === "disable" && typeof message.line === "string") {
      disable(message.line);
    } else if (message && message.type === "note" && typeof message.line === "string") {
      for (const button of buttons) button.disabled = false;
      status.textContent = message.line;
    }
  });
})();`;

// Every colour is a VS Code theme variable, so the tab follows the user's
// theme; the three operation tints are the theme's chart colours.
const REVIEW_STYLE = `body { font-family: var(--vscode-font-family); font-size: var(--vscode-font-size);
  color: var(--vscode-foreground); background: var(--vscode-editor-background);
  margin: 0; padding: 18px 24px 24px; display: grid; gap: 14px; }
header { display: grid; gap: 4px; }
h1 { margin: 0; font-size: 16px; font-weight: 600; color: var(--vscode-foreground); }
.meta { display: flex; flex-wrap: wrap; gap: 6px 18px; font-size: 12px;
  color: var(--vscode-descriptionForeground); }
.meta b { font-weight: 500; color: var(--vscode-foreground); }
pre { margin: 0; padding: 14px 16px; border-radius: 4px; overflow-x: auto; white-space: pre-wrap;
  overflow-wrap: break-word;
  font-family: var(--vscode-editor-font-family); font-size: var(--vscode-editor-font-size);
  line-height: 1.5; color: var(--vscode-foreground);
  background: var(--vscode-textCodeBlock-background); border: 1px solid var(--vscode-panel-border); }
pre .q { font-weight: 600; }
pre .op-create { color: var(--vscode-charts-green); }
pre .op-update { color: var(--vscode-charts-blue); }
pre .op-remove { color: var(--vscode-charts-red); }
pre .warn { display: inline-block; min-width: 100%; box-sizing: border-box; padding: 6px 8px;
  border-radius: 3px; background: var(--vscode-inputValidation-warningBackground);
  border: 1px solid var(--vscode-inputValidation-warningBorder); }
pre .warn::before { content: "⚠ "; color: var(--vscode-editorWarning-foreground); }
.actions { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; }
button { font: inherit; font-weight: 600; padding: 7px 16px; border: none; border-radius: 3px;
  cursor: pointer; }
.approve { color: var(--vscode-button-foreground); background: var(--vscode-button-background); }
.approve:hover:not(:disabled) { background: var(--vscode-button-hoverBackground); }
.reject { color: var(--vscode-button-secondaryForeground);
  background: var(--vscode-button-secondaryBackground); }
.reject:hover:not(:disabled) { background: var(--vscode-button-secondaryHoverBackground); }
button:disabled { opacity: 0.5; cursor: default; }
.wait { margin-left: auto; font-size: 12px; color: var(--vscode-descriptionForeground); }`;

/** A delta entry's headline: its indent and position, then the operation word. */
const ENTRY_LINE = /^(\s+\d+\. )(create|update|remove)\b/;

/**
 * The warning's `[start, end)` lines, or `undefined` when *value* is not two non-negative integers,
 * in order, not empty, within *lineCount*. Anything else is no highlight, never a broken record.
 */
export function warningSpan(value: unknown, lineCount: number): [number, number] | undefined {
  if (!Array.isArray(value) || value.length !== 2) {
    return undefined;
  }
  const [first, past] = value as number[];
  if (!Number.isSafeInteger(first) || !Number.isSafeInteger(past)) {
    return undefined;
  }
  return first >= 0 && first < past && past <= lineCount ? [first, past] : undefined;
}

/**
 * *text* as the `<pre>`'s inner HTML: every line escaped, a delta entry line's operation word
 * (`  <n>. <operation> <address>`) in a tint span, the last non-empty line (the question) in an
 * emphasis span, and the *warningLines* in one warning span.
 *
 * Presentation only: the spans wrap already-escaped pieces and the lines are rejoined by `\n`, so
 * the `textContent` the script digests is exactly *text*; the warning mark is `::before`, outside
 * `textContent`. The highlight comes from the server's *warningLines*, **never** from the words on
 * a line: the text holds the assistant's intent, which can spell `WARNING:` too.
 */
export function textHtml(text: string, warningLines?: unknown): string {
  const lines = text.split("\n");
  const warning = warningSpan(warningLines, lines.length);
  let question = -1;
  for (let index = lines.length - 1; index >= 0; index--) {
    if (lines[index].trim() !== "") {
      question = index;
      break;
    }
  }
  const rendered = lines.map((line, index) => {
    if (index === question) {
      return `<span class="q">${escapeHtml(line)}</span>`;
    }
    const entry = ENTRY_LINE.exec(line);
    if (entry === null) {
      return escapeHtml(line);
    }
    const [head, prefix, operation] = entry;
    return `${escapeHtml(prefix)}<span class="op-${operation}">${operation}</span>${escapeHtml(line.slice(head.length))}`;
  });
  if (warning !== undefined) {
    const [start, end] = warning;
    rendered[start] = `<span class="warn">${rendered[start]}`;
    rendered[end - 1] = `${rendered[end - 1]}</span>`;
  }
  return rendered.join("\n");
}

/**
 * The review tab's document around *body*: the CSP allowing only the one nonce'd style and script,
 * `REVIEW_STYLE` (plus *style*, when given, in the same `<style>`), *title* escaped, *body* as
 * given (already escaped), then *script* when there is one. Without *style* the document is byte
 * for byte the review tab's.
 *
 * Both button tabs are built with it: the review tab and the imports' tab (`importTabCore.ts`),
 * which looks and behaves as this one. Sharing the page shares nothing of the review channel. A
 * *nonce* outside base64 throws, since it is written into two attributes.
 */
export function reviewTabPage(options: {
  nonce: string;
  title: string;
  body: readonly string[];
  script?: string;
  style?: string;
}): string {
  const { nonce, title, body, script, style } = options;
  if (!NONCE_PATTERN.test(nonce)) {
    throw new Error("a CSP nonce must be base64");
  }
  return [
    "<!DOCTYPE html>",
    '<html lang="en">',
    "<head>",
    '<meta charset="UTF-8">',
    `<meta http-equiv="Content-Security-Policy" content="${reviewCsp(nonce)}">`,
    '<meta name="viewport" content="width=device-width, initial-scale=1.0">',
    `<title>${escapeHtml(title)}</title>`,
    `<style nonce="${nonce}">${REVIEW_STYLE}${style === undefined ? "" : `\n${style}`}</style>`,
    "</head>",
    "<body>",
    ...body,
    ...(script === undefined ? [] : [`<script nonce="${nonce}">${script}</script>`]),
    "</body>",
    "</html>",
    "",
  ].join("\n");
}

/**
 * The review tab's page: the header and its meta line, the server's `text` in the one `<pre>`, and
 * the two buttons with the waiting sentence. The document around it is `reviewTabPage`'s.
 *
 * The `text` is HTML-escaped and never interpreted as Markdown or HTML, which keeps `review.py`'s
 * anti-forgery property over plain-text lines. The `<pre>` opens with a newline because the HTML
 * parser drops exactly one leading newline there.
 *
 * What the `<pre>` does **not** reproduce: the parser turns `\r` into `\n` and NUL into U+FFFD, so
 * such a text's approval is refused by `checkApproval`, fail-closed; the server flattens client
 * text, so no such character reaches a real record.
 *
 * Neither button has `autofocus`: the host opens the tab with `preserveFocus: true`, so a key typed
 * in a chat can never press Approve.
 */
export function reviewHtml(
  record: PendingRecord,
  options: { nonce: string; utcOffsetMinutes: number },
): string {
  const { nonce, utcOffsetMinutes } = options;
  const meta = metaItems(record, utcOffsetMinutes)
    .map(
      (item) =>
        `<span>${escapeHtml(item.label)} <b>${escapeHtml(item.value)}</b>${
          item.suffix === undefined ? "" : ` ${escapeHtml(item.suffix)}`
        }</span>`,
    )
    .join("");
  return reviewTabPage({
    nonce,
    title: reviewTabTitle(record),
    body: [
      "<header>",
      `<h1>${REVIEW_HEADER}</h1>`,
      `<div class="meta">${meta}</div>`,
      "</header>",
      `<pre id="text">\n${textHtml(record.text, record.warning_lines)}</pre>`,
      '<div class="actions">',
      '<button type="button" class="approve" data-decision="approve">✓ Approve</button>',
      '<button type="button" class="reject" data-decision="reject">✕ Reject</button>',
      `<span id="status" class="wait" role="status">${escapeHtml(WAITING_LINE)}</span>`,
      "</div>",
    ],
    script: REVIEW_SCRIPT,
  });
}

// --- The decision -----------------------------------------------------------

/** `sha256:<hex>` of *text* as UTF-8: `panel_review.text_digest`'s twin. */
export function textDigest(text: string): string {
  return `sha256:${createHash("sha256").update(text, "utf8").digest("hex")}`;
}

export type ApprovalCheck = { ok: true; digest: string } | { ok: false; reason: string };

/**
 * The digest an approval is written with, or why it is refused.
 *
 * Three digests must equal `textDigest(record.text)` of the record re-read at click time: the
 * record's own `digest`, the digest of *renderedText* (what the host put into the tab) and
 * *postedDigest* (what the tab's script computed). A rejection needs none of this: it commits
 * nothing.
 */
export function checkApproval(
  record: PendingRecord,
  renderedText: string,
  postedDigest: unknown,
): ApprovalCheck {
  const expected = textDigest(record.text);
  if (record.digest !== expected) {
    return { ok: false, reason: "The waiting question's digest does not match its text." };
  }
  if (textDigest(renderedText) !== expected) {
    return { ok: false, reason: "The question changed since this tab was opened." };
  }
  if (postedDigest !== expected) {
    return { ok: false, reason: "The text this tab shows does not match the waiting question." };
  }
  return { ok: true, digest: expected };
}

/**
 * The decision record's JSON, in `panel_review`'s key order with its trailing newline: exactly the
 * six keys its reader accepts. Throws when *digest* is not the record's own, since the server
 * would only refuse it.
 */
export function decisionRecord(record: PendingRecord, decision: Decision, digest: string): string {
  if (digest !== record.digest) {
    throw new Error("a decision carries the digest of its own pending record");
  }
  const document = {
    format: REVIEW_FORMAT,
    change_set_id: record.change_set_id,
    question: record.question,
    wait: record.wait,
    digest,
    decision,
  };
  return `${JSON.stringify(document, null, 2)}\n`;
}

/** The part of `node:fs` `writeDecision` uses, so a test can hand it the real one. */
export type DecisionFs = Pick<
  typeof nodeFs,
  "writeFileSync" | "linkSync" | "unlinkSync" | "readFileSync"
>;

/**
 * A write's outcome. A refusal with `live: true` leaves the tab's buttons live:
 * nothing answered this question yet, and the click may be made again.
 */
export type WriteOutcome = { ok: true; file: string } | { ok: false; reason: string; live?: true };

/**
 * Write the decision on *record* into *dir*, the first answer winning.
 *
 * The JSON goes to `.<final name>.<pid>.<random hex>.tmp` (mode `0o600`, matching neither glob),
 * created with `wx` (never truncating a file, which once linked *is* a final record), then
 * **linked** to the final name: `linkSync` refuses an existing target, so a second window's answer
 * is refused with `ALREADY_ANSWERED`. The temporary file is removed whenever the name is still the
 * file this call created.
 *
 * A refused link reads the file at the name (`answersRecord`): only a decision carrying *record*'s
 * own wait token answers this question. Anything else (an earlier copy's answer, which the server
 * clears after re-filing the record, a non-decision, an unreadable or vanished file) answers
 * nothing yet, so the refusal is `EARLIER_ANSWER_IN_PLACE` with `live: true`: disabling the tab
 * would leave a question nobody can answer from it.
 *
 * Any other failure is a refusal naming the error's code and no path.
 */
export function writeDecision(
  fs: DecisionFs,
  dir: string,
  record: PendingRecord,
  decision: Decision,
  digest: string,
): WriteOutcome {
  const json = decisionRecord(record, decision, digest);
  const name = decisionFileName(record);
  const final = path.join(dir, name);
  const tmp = path.join(
    dir,
    `.${name}.${process.pid}.${randomBytes(8).toString("hex")}${TMP_SUFFIX}`,
  );
  try {
    fs.writeFileSync(tmp, json, { flag: "wx", mode: 0o600, encoding: "utf8" });
  } catch (error) {
    const code = (error as NodeJS.ErrnoException).code;
    // `EEXIST`: the name is another write's file. Any other failure came after this call created the
    // file (a full disk), so it is this call's to remove.
    if (code !== "EEXIST") {
      removeQuietly(fs, tmp);
    }
    return { ok: false, reason: `The answer could not be written (${code ?? "unknown error"}).` };
  }
  try {
    fs.linkSync(tmp, final);
    return { ok: true, file: final };
  } catch (error) {
    const code = (error as NodeJS.ErrnoException).code;
    if (code === "EEXIST") {
      return answersRecord(fs, final, record)
        ? { ok: false, reason: ALREADY_ANSWERED }
        : { ok: false, reason: EARLIER_ANSWER_IN_PLACE, live: true };
    }
    return { ok: false, reason: `The answer could not be written (${code ?? "unknown error"}).` };
  } finally {
    removeQuietly(fs, tmp);
  }
}

/**
 * Whether the decision file at *file* answers *record*: a JSON object naming its change set, its
 * question and its own wait token, which only a reader of this very record can have. An unreadable
 * or unparsable file answers nothing.
 */
export function answersRecord(
  fs: Pick<DecisionFs, "readFileSync">,
  file: string,
  record: PendingRecord,
): boolean {
  let parsed: unknown;
  try {
    parsed = JSON.parse(fs.readFileSync(file, { encoding: "utf8" }));
  } catch {
    return false;
  }
  return (
    isObject(parsed) &&
    parsed.change_set_id === record.change_set_id &&
    parsed.question === record.question &&
    parsed.wait === record.wait
  );
}

function removeQuietly(fs: DecisionFs, file: string): void {
  try {
    fs.unlinkSync(file);
  } catch {
    // Never created, or already gone: nothing to withdraw.
  }
}

// --- What the host decides ----------------------------------------------------------

/** The Pending Change Sets view's id (`package.json`'s `views.swcraft`). */
export const REVIEW_VIEW_ID = "swcraftReview";
/** The review tab's webview view type. */
export const REVIEW_TAB_VIEW_TYPE = "swcraft.review";
/** The context key the view's `when` reads: a project is detected in this window. */
export const REVIEW_AVAILABLE_KEY = "swcraft.review.available";
/** How often the view re-reads the reviews directory while a project is detected. */
export const REVIEW_REREAD_MS = 3_000;

/** A row's click: opens or reveals its tab. It decides nothing. */
export const REVIEW_OPEN_COMMAND = "swcraft.reviewOpen";
/** The view's title-bar Refresh. */
export const REVIEW_REFRESH_COMMAND = "swcraft.reviewRefresh";
/**
 * The view's commands, all of them: neither approves or rejects. Approve and Reject exist only as
 * the tab's buttons, so an assistant that can run VS Code commands has nothing to call.
 */
export const REVIEW_VIEW_COMMANDS: readonly string[] = [
  REVIEW_OPEN_COMMAND,
  REVIEW_REFRESH_COMMAND,
];

/** One file of the reviews directory; its mtime is the waiting server's heartbeat (`isLive`). */
export interface ReviewEntry {
  name: string;
  text: string;
  mtimeMs: number;
}

/** What `readReviewEntry` needs of `node:fs`, injected so a test can fail either call. */
export type EntryFs = Pick<typeof nodeFs, "statSync" | "readFileSync">;

/**
 * One file of the reviews directory with its mtime, or `undefined` when it cannot be stat'ed or
 * read now (gone, or not a file); the next re-read decides. The stat comes first, so the mtime is
 * never newer than the text.
 */
export function readReviewEntry(fs: EntryFs, dir: string, name: string): ReviewEntry | undefined {
  const file = path.join(dir, name);
  try {
    const { mtimeMs } = fs.statSync(file);
    return { name, text: fs.readFileSync(file, { encoding: "utf8" }), mtimeMs };
  } catch {
    return undefined;
  }
}

/** Whether *name* can be a pending record's file: `*.pending.json`, no leading `.` (temporary). */
export function isPendingFileName(name: string): boolean {
  return !name.startsWith(".") && name.endsWith(PENDING_SUFFIX);
}

/**
 * The review keys a directory listing's *names* hold a pending record for,
 * whether or not the record parses: `reviewTransitions`' *listed*.
 */
export function listedReviewKeys(names: readonly string[]): Set<string> {
  return new Set(
    names.filter(isPendingFileName).map((name) => name.slice(0, -PENDING_SUFFIX.length)),
  );
}

/**
 * The records this window shows, in `compareRecords`' order: the live pending records of the
 * projects whose manifests are at *manifestRealPaths* (one per workspace folder with a root
 * manifest), each under its own file name. Unparsable files, records under another record's name
 * and temporary files are left out silently: the server's sweep owns them.
 *
 * *realPath* canonicalises the record's `manifest` first (the host passes
 * `fs.realpathSync.native`): the server's `Path.resolve()` keeps the letter case it was given on
 * macOS, while the window's side is the case on disk. On Windows `samePath` also ignores case.
 */
export function windowReviews(
  entries: readonly ReviewEntry[],
  manifestRealPaths: readonly string[],
  platform: Platform,
  nowMs: number,
  realPath: (manifest: string) => string = (manifest) => manifest,
): PendingRecord[] {
  const records: PendingRecord[] = [];
  for (const entry of entries) {
    if (!isPendingFileName(entry.name)) {
      continue;
    }
    const parsed = parsePending(entry.text);
    if (!parsed.ok || pendingFileName(parsed.record) !== entry.name) {
      continue;
    }
    const manifest = realPath(parsed.record.project.manifest);
    if (
      manifestRealPaths.some((own) => samePath(manifest, own, platform)) &&
      isLive(entry.mtimeMs, nowMs)
    ) {
      records.push(parsed.record);
    }
  }
  return records.sort(compareRecords);
}

/** What one re-read changes about the tabs, by key. */
export interface ReviewTransitions {
  /**
   * Open tabs whose key now holds a live record of another identity (a key reused by another
   * project's server): dispose each before `open` opens the new record.
   */
  dispose: string[];
  /**
   * Live records this window has not opened a tab for yet (a new key, or a known key now holding
   * another identity): open each, once.
   */
  open: string[];
  /** Open, enabled tabs whose question is no longer live: disable with `NO_LONGER_WAITING`. */
  disable: string[];
  /**
   * Tabs disabled with `NO_LONGER_WAITING` whose own record (same `reviewIdentity`) is live again: a
   * re-read that failed for a moment made it look gone, so its buttons come back. A tab disabled for
   * another reason stays disabled; another identity at its key never enables it (`dispose`).
   */
  enable: string[];
  /**
   * Opened questions neither live nor in a tab whose pending record a successful listing no longer
   * names: forgotten, so the map does not grow. Never on a failed or skipped listing, which would
   * make a closed tab reopen on the next good re-read.
   */
  forget: string[];
}

/**
 * The tab decisions of one re-read, every map keyed by `reviewKey` and holding `reviewIdentity`
 * values: *live* the records `windowReviews` found, *opened* the record last opened a tab for at
 * each key (a closed tab is not reopened by a re-read), *tabs* the open tabs with their rendered
 * record and disabling line, and *listed* the keys the directory listing named, or `undefined`
 * when it was not listed.
 */
export function reviewTransitions(
  live: ReadonlyMap<string, string>,
  opened: ReadonlyMap<string, string>,
  tabs: ReadonlyMap<string, { identity: string; disabled?: string }>,
  listed: ReadonlySet<string> | undefined,
): ReviewTransitions {
  const transitions: ReviewTransitions = {
    dispose: [],
    open: [],
    disable: [],
    enable: [],
    forget: [],
  };
  for (const [key, identity] of live) {
    if (opened.get(key) !== identity) {
      transitions.open.push(key);
    }
  }
  for (const [key, tab] of tabs) {
    const identity = live.get(key);
    if (identity === undefined) {
      if (tab.disabled === undefined) {
        transitions.disable.push(key);
      }
    } else if (identity !== tab.identity) {
      transitions.dispose.push(key);
    } else if (tab.disabled === NO_LONGER_WAITING) {
      transitions.enable.push(key);
    }
  }
  for (const key of opened.keys()) {
    if (!live.has(key) && !tabs.has(key) && listed !== undefined && !listed.has(key)) {
      transitions.forget.push(key);
    }
  }
  return transitions;
}

/** The view's badge tooltip for *count* waiting questions; no badge at zero. */
export function reviewBadgeTooltip(count: number): string | undefined {
  if (count <= 0) {
    return undefined;
  }
  return count === 1 ? "1 change set question waiting" : `${count} change set questions waiting`;
}

/** What a tab's script posted, once checked: the decision and the digest it computed. */
export interface TabAnswer {
  decision: Decision;
  digest: unknown;
}

/** The tab's message as an answer, or `undefined` for anything else. */
export function parseTabAnswer(message: unknown): TabAnswer | undefined {
  if (!isObject(message)) {
    return undefined;
  }
  const decision = message.decision;
  if (decision !== "approve" && decision !== "reject") {
    return undefined;
  }
  return { decision, digest: message.digest };
}

export type AnswerPlan =
  | { ok: true; record: PendingRecord; decision: Decision; digest: string }
  | { ok: false; reason: string };

/**
 * What a click on a tab writes, or why it writes nothing.
 *
 * *shown* is the tab's own record; nothing of the message chooses the question. *fresh* is the
 * record at that question's file re-read now (`undefined` when gone, unparsable, another project's
 * or stale). It must be *shown* by `reviewIdentity`: another wait under the same name is refused
 * even with an equal digest, since its question was never shown. An approval then needs
 * `checkApproval`'s three equal digests; a rejection is written with the record's own digest, which
 * the server's binding check requires.
 */
export function answerPlan(
  shown: PendingRecord,
  fresh: PendingRecord | undefined,
  answer: TabAnswer,
): AnswerPlan {
  if (fresh === undefined || !sameReview(fresh, shown)) {
    return { ok: false, reason: NO_LONGER_WAITING };
  }
  if (answer.decision === "reject") {
    return { ok: true, record: fresh, decision: "reject", digest: fresh.digest };
  }
  const check = checkApproval(fresh, shown.text, answer.digest);
  if (!check.ok) {
    return check;
  }
  return { ok: true, record: fresh, decision: "approve", digest: check.digest };
}

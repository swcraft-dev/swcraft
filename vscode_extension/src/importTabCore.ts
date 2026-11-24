// Pure logic of the imports' tab: the page an import's report and its buttons are shown in, the
// buttons each step offers, the tab's script and the check of what it posts, the tab controller
// (`ImportTabs`) and the flows (`importRequirementsFlow()`, `importComponentFlow()`,
// `traceApplyFlow()`, with the requirements import's questions `askRequirementsMapping()` and
// `askDeliveryVersion()`). No `vscode` import: it runs under plain `node --test` with scripted
// fakes; `commands.ts` hands in the webview, the dialogs, the setting and the runs.
//
// The page is the review tab's form, built by its own helper `reviewTabPage()` (its CSP with a
// fresh nonce, its style and document), never a copy: the report rendered as Markdown, the
// buttons in the review tab's `approve` and `reject` styles, the line beside them in its `wait`
// style, a warning in its `warn` yellow. A tab, not a non-modal question, so the choice cannot
// slide into the notification bell while the report is read.
//
// The boundary: this is **not the review channel**. Nothing here reads or writes a pending or a
// decision record, files anything under `reviews/`, computes a digest or writes an audit record.
// Of `reviewCore.ts` it imports the page helper and the HTML escape only, and it imports no file
// system module. An import's decision is one click in a tab its own command opened, and it runs
// that command's next CLI run, nothing else.
//
// The report is Markdown (`importReportMarkdown()`), rendered by VS Code's built-in
// `markdown.api.render`, handed in as a function (escaped text when it is absent or throws). The
// renderer's HTML is placed as it is: the page's CSP (`default-src 'none'`, script and style by
// nonce alone) keeps a script or event handler in it from running, and the tab's script binds the
// buttons of `.actions` alone.

import { randomBytes } from "node:crypto";
import * as path from "node:path";

import {
  type AppliedRunWording,
  askSettingPath,
  baselineArgFor,
  baselineBoxOptions,
  type CapturedRun,
  COMPONENT_IMPORT_REPORT_TITLE,
  COMPONENT_MARKDOWN_HEADING,
  codeFence,
  componentDryRunOutcome,
  escapeMarkdown,
  fileDeliveryVersion,
  IMPORT_APPLY_BUTTON,
  IMPORT_CANCEL_BUTTON,
  IMPORT_CHECK_MODEL_BUTTON,
  IMPORT_REPORT_TITLE,
  IMPORT_RUN_WORDING,
  IMPORT_UPDATE_BUTTON,
  type ImportReport,
  importAppliedOutcome,
  importDoneMessage,
  importDryRunOutcome,
  type MappingPickItem,
  mappingPickItems,
  SHOW_UPDATE_BUTTON,
  summaryOf,
  TRACE_APPLY_MARKDOWN_HEADING,
} from "./commandCore";
import { escapeHtml, reviewTabPage } from "./reviewCore";
import {
  APPLY_REPORT_TITLE,
  applyDoneMessage,
  applySummaryOf,
  BUSY_REFUSAL,
  TRACE_APPLY_WORDING,
  type TraceApplyRunOptions,
  traceApplyPreview,
} from "./traceCore";

/** The imports' tab's webview view type. */
export const IMPORT_TAB_VIEW_TYPE = "swcraft.import";

/** What one button of the tab asks the host to do. */
export type ImportAction = "apply" | "showUpdate" | "cancel" | "checkModel";

/** One button: its action, its label and whether it is the primary (the review tab's Approve style). */
export interface ImportTabButton {
  action: ImportAction;
  label: string;
  primary: boolean;
}

/** What the tab shows: one report, and the buttons and line of this step. */
export interface ImportTabView {
  /** The tab's title and the page's heading: *Component Import Report*, *Requirements Import Report*. */
  title: string;
  /** The extension's word for the run: *Dry run*, *Update dry run*, *Import*, *Update*. */
  run: string;
  /** The imported file; the page shows its name only. */
  file: string;
  report: ImportReport;
  /** No buttons: a refused dry run, a failed applied run, a decided step. */
  buttons: readonly ImportTabButton[];
  /** The line beside the buttons: the done line beside **Check Model**; none beside **Write** or **Show Update**, whose report already says what they do. */
  status?: string;
}

/** The one line a page shows above a report the Markdown renderer could not render. */
export const IMPORT_REPORT_PLAIN_LINE =
  "The Markdown renderer is not available; the report is shown as plain text.";

/**
 * How the page shows its report: the HTML the Markdown renderer returned, or
 * the plain-text fallback (the CLI's text escaped in one `<pre>`, the notes in
 * paragraphs, under {@link IMPORT_REPORT_PLAIN_LINE}).
 */
export type ImportReportRendering = { kind: "markdown"; html: string } | { kind: "plain" };

/** A Markdown renderer: VS Code's `markdown.api.render` in the host, a stub in the tests. */
export type MarkdownRenderer = (markdown: string) => unknown;

/**
 * The CLI's Markdown report as the tab shows it: a component report's heading is the file's name
 * alone and Trace Apply's `search '<name>'` alone ({@link TRACE_APPLY_MARKDOWN_HEADING}), since the
 * tab's title already says the rest; anything else is shown unchanged. The CLI's own file keeps
 * its heading.
 */
export function tabReportMarkdown(markdown: string): string {
  for (const heading of [COMPONENT_MARKDOWN_HEADING, TRACE_APPLY_MARKDOWN_HEADING]) {
    if (markdown.startsWith(heading)) {
      return `# ${markdown.slice(heading.length)}`;
    }
  }
  return markdown;
}

/**
 * *report* as the Markdown the tab renders: the CLI's own Markdown report (`report.markdown`, the
 * `--report` file of an exit 0) through {@link tabReportMarkdown}, or else the CLI's text in a
 * fenced code block ({@link codeFence}, no info string, so it renders verbatim). Both trailing-
 * trimmed. Then each extension line as a paragraph, every Markdown metacharacter escaped
 * (`escapeMarkdown()`), so a file name or path renders as written. A run that printed nothing has
 * no code block. The warnings are not part of it: the page shows them above the report.
 */
export function importReportMarkdown(report: ImportReport): string {
  const parts: string[] = [];
  const cliText = report.cliText.replace(/\s+$/, "");
  const cliMarkdown = report.markdown?.replace(/\s+$/, "");
  if (cliMarkdown !== undefined && cliMarkdown !== "") {
    parts.push(tabReportMarkdown(cliMarkdown));
  } else if (cliText !== "") {
    const fence = codeFence(cliText);
    parts.push(`${fence}\n${cliText}\n${fence}`);
  }
  for (const note of report.notes) {
    parts.push(escapeMarkdown(note));
  }
  return parts.length === 0 ? "" : `${parts.join("\n\n")}\n`;
}

/**
 * *report* rendered by *render*, or the plain-text fallback when there is no renderer, it throws
 * or rejects, or it returns anything but a string.
 */
export async function renderImportReport(
  report: ImportReport,
  render: MarkdownRenderer | undefined,
): Promise<ImportReportRendering> {
  if (render === undefined) {
    return { kind: "plain" };
  }
  try {
    const html = await render(importReportMarkdown(report));
    return typeof html === "string" ? { kind: "markdown", html } : { kind: "plain" };
  } catch {
    return { kind: "plain" };
  }
}

// The import pages' rules for paragraphs and the rendered Markdown, appended to the review tab's
// style by `reviewTabPage()`. A code block is a `<pre>` and takes the review tab's `pre` rule.
const IMPORT_MARKDOWN_STYLE = `p { margin: 0; }
.markdown { display: grid; gap: 10px; min-width: 0; }
.markdown > * { margin: 0; }
.markdown h1, .markdown h2, .markdown h3, .markdown h4 { margin: 0; font-size: 14px; font-weight: 600; }
.markdown ul, .markdown ol { margin: 0; padding-left: 22px; }
.markdown code { font-family: var(--vscode-editor-font-family); font-size: var(--vscode-editor-font-size); }
.markdown :not(pre) > code { padding: 1px 4px; border-radius: 3px;
  background: var(--vscode-textCodeBlock-background); }
.markdown pre code { padding: 0; background: none; }
.markdown table { border-collapse: collapse; }
.markdown th, .markdown td { padding: 4px 10px; text-align: left;
  border: 1px solid var(--vscode-panel-border); }
.plain-note { font-size: 12px; color: var(--vscode-descriptionForeground); }`;

/** The line beside the disabled buttons while a run started from the tab is busy. */
export const IMPORT_TAB_BUSY_LINE = "Running…";

/** Under a dry run's report: the apply button (**Write**, or **Update** after Show Update) and **Cancel**. */
export function applyButtons(update: boolean): ImportTabButton[] {
  return [
    {
      action: "apply",
      label: update ? IMPORT_UPDATE_BUTTON : IMPORT_APPLY_BUTTON,
      primary: true,
    },
    { action: "cancel", label: IMPORT_CANCEL_BUTTON, primary: false },
  ];
}

/** Under the refusal `--update` resolves (exit 3): **Show Update** and **Cancel**. */
export function showUpdateButtons(): ImportTabButton[] {
  return [
    { action: "showUpdate", label: SHOW_UPDATE_BUTTON, primary: true },
    { action: "cancel", label: IMPORT_CANCEL_BUTTON, primary: false },
  ];
}

/** Under an applied run that exited 0: **Check Model**, the one button. */
export function checkModelButtons(): ImportTabButton[] {
  return [{ action: "checkModel", label: IMPORT_CHECK_MODEL_BUTTON, primary: true }];
}

/** A page token: 32 lowercase hex digits, drawn afresh for every page that offers buttons. */
const TOKEN_PATTERN = /^[0-9a-f]{32}$/;

/** A fresh token for one page. */
export function newImportToken(): string {
  return randomBytes(16).toString("hex");
}

/**
 * The tab's script, bound to the page's *token*. A click disables every button at once, shows
 * {@link IMPORT_TAB_BUSY_LINE} and posts `{ action, token }` once; a disabled button posts nothing.
 * The host decides what the message runs ({@link acceptImportTabMessage}).
 */
export function importTabScript(token: string): string {
  if (!TOKEN_PATTERN.test(token)) {
    throw new Error("an import tab token is 32 lowercase hex digits");
  }
  return `(() => {
  const vscode = acquireVsCodeApi();
  const token = "${token}";
  const status = document.getElementById("status");
  const buttons = Array.from(document.querySelectorAll(".actions > button[data-action]"));
  for (const button of buttons) {
    button.addEventListener("click", () => {
      if (button.disabled) return;
      for (const each of buttons) each.disabled = true;
      status.textContent = ${JSON.stringify(IMPORT_TAB_BUSY_LINE)};
      vscode.postMessage({ action: button.dataset.action, token });
    });
  }
})();`;
}

/**
 * *view* as the tab's page, through `reviewTabPage()`: the heading and meta line (the run, the
 * file's name), the warning block when there is one, the report as *rendering* gives it (the
 * renderer's HTML in `<div id="report" class="markdown">`, or the fallback: the plain line, the
 * CLI's text escaped in one `<pre>`, the notes as paragraphs), then, only with buttons, the buttons
 * and the line beside them. Everything else is escaped; the renderer's HTML is the one piece
 * placed as it is, under the CSP.
 *
 * *busy* renders every button disabled with {@link IMPORT_TAB_BUSY_LINE} and no script, so a page
 * rebuilt during a run offers nothing. *token* is required with live buttons; a page without them
 * carries no script. No button has `autofocus`.
 */
export function importTabHtml(
  view: ImportTabView,
  options: { nonce: string; rendering: ImportReportRendering; token?: string; busy?: boolean },
): string {
  const { nonce, rendering, token, busy = false } = options;
  const body = [
    "<header>",
    `<h1>${escapeHtml(view.title)}</h1>`,
    `<div class="meta"><span>Run <b>${escapeHtml(view.run)}</b></span><span>File <b>${escapeHtml(
      path.basename(view.file),
    )}</b></span></div>`,
    "</header>",
  ];
  const warnings = view.report.warnings ?? [];
  if (warnings.length > 0) {
    body.push(
      `<pre id="warning"><span class="warn">${warnings.map(escapeHtml).join("\n")}</span></pre>`,
    );
  }
  if (rendering.kind === "markdown") {
    body.push(`<div id="report" class="markdown">\n${rendering.html}\n</div>`);
  } else {
    body.push(`<p class="plain-note">${escapeHtml(IMPORT_REPORT_PLAIN_LINE)}</p>`);
    const cliText = view.report.cliText.replace(/\s+$/, "");
    if (cliText !== "") {
      body.push(`<pre id="report">\n${escapeHtml(cliText)}</pre>`);
    }
    for (const note of view.report.notes) {
      body.push(`<p>${escapeHtml(note)}</p>`);
    }
  }
  if (view.buttons.length === 0) {
    return reviewTabPage({ nonce, title: view.title, body, style: IMPORT_MARKDOWN_STYLE });
  }
  if (!busy && token === undefined) {
    throw new Error("a page with live buttons carries its token");
  }
  const disabled = busy ? " disabled" : "";
  body.push('<div class="actions">');
  for (const button of view.buttons) {
    const cls = button.primary ? "approve" : "reject";
    body.push(
      `<button type="button" class="${cls}" data-action="${button.action}"${disabled}>${escapeHtml(button.label)}</button>`,
    );
  }
  const line = busy ? IMPORT_TAB_BUSY_LINE : (view.status ?? "");
  body.push(`<span id="status" class="wait" role="status">${escapeHtml(line)}</span>`, "</div>");
  const script = busy || token === undefined ? undefined : importTabScript(token);
  return reviewTabPage({ nonce, title: view.title, body, style: IMPORT_MARKDOWN_STYLE, script });
}

/** The choice a page is waiting for: its token and the actions its buttons offer. */
export interface PendingImportChoice {
  token: string;
  actions: readonly ImportAction[];
}

/**
 * The action *message* asks for, or `undefined` when it runs nothing. Fails closed: a choice must
 * be waiting (*pending*; none while busy, after a click or after the tab moved on), the message an
 * object carrying exactly that page's token, its action one the page offers.
 */
export function acceptImportTabMessage(
  message: unknown,
  pending: PendingImportChoice | undefined,
): ImportAction | undefined {
  if (pending === undefined || typeof message !== "object" || message === null) {
    return undefined;
  }
  const { action, token } = message as Record<string, unknown>;
  if (token !== pending.token || typeof action !== "string") {
    return undefined;
  }
  return pending.actions.find((each) => each === action);
}

// --- The tab's controller and the imports' flows ------------------------------------
//
// One tab per report title, reused by the next report of that title; one choice per page, ended by
// a click, the tab's close or a newer flow of the same title; one flow of a title at a time.

/** What the controller needs of one webview tab: `vscode.WebviewPanel` in the host. */
export interface ImportTabPanel {
  setHtml(html: string): void;
  /** Show the tab and give it the focus, so its report is read. */
  reveal(): void;
  dispose(): void;
}

/** The host's half of the tabs: a new tab for a title, and a fresh nonce for each page. */
export interface ImportTabsHost {
  /**
   * A new tab titled *title*, taking the focus. *events.message* gets what its page posts,
   * *events.disposed* its closing.
   */
  openPanel(
    title: string,
    events: { message: (message: unknown) => void; disposed: () => void },
  ): ImportTabPanel;
  nonce(): string;
  /** A fresh page token; {@link newImportToken} unless a test scripts it. */
  token?(): string;
}

/** One flow's hold on its title, from {@link ImportTabs.begin} to {@link ImportTabs.end}. */
export interface ImportFlowTicket {
  readonly title: string;
  /** Whether the flow waits for a choice of its tab, the one time a newer flow may replace it. */
  waiting: boolean;
}

interface OpenImportTab {
  panel: ImportTabPanel;
  view: ImportTabView;
  rendering: ImportReportRendering;
  pending?: PendingImportChoice & { resolve: (action: ImportAction | undefined) => void };
}

/**
 * The imports' tabs and the flows that hold them. A flow of a title begins ({@link begin}) only
 * when no other flow of it is busy (running the CLI or rendering a report); a flow going on to
 * Check Model ends its ticket first. A flow waiting for a choice is replaced instead: its choice
 * ends with no action, so it runs nothing, and the new flow's report takes the tab. So two applied
 * runs of one import never run over the model at once, and a page's buttons always belong to the
 * flow that acts on them.
 */
export class ImportTabs {
  private readonly tabs = new Map<string, OpenImportTab>();
  private readonly flows = new Map<string, ImportFlowTicket>();

  constructor(private readonly host: ImportTabsHost) {}

  /** Whether a flow of *title* is busy, so a new one would be refused. */
  isBusy(title: string): boolean {
    const flow = this.flows.get(title);
    return flow !== undefined && !flow.waiting;
  }

  /**
   * Begin a flow of *title*: `undefined` while another is busy (the caller refuses), else its
   * ticket. A flow waiting for a choice is replaced; its page is rendered busy until the new
   * report replaces it.
   */
  begin(title: string): ImportFlowTicket | undefined {
    if (this.isBusy(title)) {
      return undefined;
    }
    const tab = this.tabs.get(title);
    if (tab?.pending !== undefined) {
      this.endChoice(tab);
      this.render(tab);
    }
    const ticket: ImportFlowTicket = { title, waiting: false };
    this.flows.set(title, ticket);
    return ticket;
  }

  /** The end of *ticket*'s flow; a flow already replaced ends nothing of its successor's. */
  end(ticket: ImportFlowTicket): void {
    if (this.flows.get(ticket.title) === ticket) {
      this.flows.delete(ticket.title);
    }
  }

  /** Show *view* in its tab, its report as *rendering*, with no choice to make and no button. */
  show(view: ImportTabView, rendering: ImportReportRendering): void {
    this.render(this.tabFor({ ...view, buttons: [] }, rendering));
  }

  /**
   * Show *view* in its tab and wait for one click: the action, or `undefined` when the tab is closed
   * or a newer flow replaces the page first. *ticket*'s flow is waiting until it answers.
   */
  ask(
    ticket: ImportFlowTicket,
    view: ImportTabView,
    rendering: ImportReportRendering,
  ): Promise<ImportAction | undefined> {
    const tab = this.tabFor(view, rendering);
    ticket.waiting = true;
    return new Promise((resolve) => {
      tab.pending = {
        token: this.host.token?.() ?? newImportToken(),
        actions: view.buttons.map((button) => button.action),
        resolve: (action) => {
          ticket.waiting = false;
          resolve(action);
        },
      };
      this.render(tab);
    });
  }

  /**
   * *view*'s tab: the open one of its title, revealed, or a new one. A choice the open tab waited
   * for ends first, running nothing. The tab takes the focus so the report is read; no button has
   * `autofocus`.
   */
  private tabFor(view: ImportTabView, rendering: ImportReportRendering): OpenImportTab {
    const open = this.tabs.get(view.title);
    if (open !== undefined) {
      this.endChoice(open);
      open.view = view;
      open.rendering = rendering;
      open.panel.reveal();
      return open;
    }
    const tab: OpenImportTab = {
      view,
      rendering,
      panel: this.host.openPanel(view.title, {
        message: (message) => this.answer(tab, message),
        disposed: () => {
          this.endChoice(tab);
          if (this.tabs.get(view.title) === tab) {
            this.tabs.delete(view.title);
          }
        },
      }),
    };
    this.tabs.set(view.title, tab);
    return tab;
  }

  /**
   * Render *tab*'s page from its view. Buttons are live only while a choice waits, under its token;
   * otherwise disabled with no script, so a page rebuilt while hidden offers nothing.
   */
  private render(tab: OpenImportTab): void {
    tab.panel.setHtml(
      importTabHtml(tab.view, {
        nonce: this.host.nonce(),
        rendering: tab.rendering,
        token: tab.pending?.token,
        busy: tab.pending === undefined,
      }),
    );
  }

  /**
   * A message from *tab*'s page; only what `acceptImportTabMessage()` accepts runs, and the choice
   * then ends, so a second click finds none. **Cancel** closes the tab, as the review tab's
   * **Reject** does; any other action re-renders the page busy until the flow's next report.
   */
  private answer(tab: OpenImportTab, message: unknown): void {
    const pending = tab.pending;
    const action = acceptImportTabMessage(message, pending);
    if (action === undefined || pending === undefined) {
      return;
    }
    tab.pending = undefined;
    pending.resolve(action);
    if (action === "cancel") {
      tab.panel.dispose();
      return;
    }
    this.render(tab);
  }

  /** End *tab*'s waiting choice, if any, with no action: its flow returns and runs nothing. */
  private endChoice(tab: OpenImportTab): void {
    const pending = tab.pending;
    tab.pending = undefined;
    pending?.resolve(undefined);
  }
}

/** The notification a second start of *label* gets while a flow of it is busy. */
export function importBusyMessage(label: string): string {
  return `SWCraft: ${label} is already running in this window — wait for it to finish.`;
}

/** What one import's flow needs of its host besides the runs. */
export interface ImportFlowHost {
  tabs: ImportTabs;
  /** The command's label (`SPAWNING_COMMAND_LABELS`), named by a start refused while a run of it is busy. */
  label: string;
  /** The executable the runs spawn, named by a run that could not start it. */
  executable: string;
  /** The file to import, from the open dialog; `undefined` when it is cancelled. */
  pickFile(): Promise<string | undefined>;
  /** *report* rendered for its tab (`renderImportReport()` over VS Code's renderer). */
  render(report: ImportReport): Promise<ImportReportRendering>;
  /** Record *run*'s outcome on the panel's item (`recordCliOutcome()`). */
  record(run: CapturedRun): void;
  /** Run `SWCraft: Check Model`. */
  checkModel(): Promise<void>;
  /** Say *message*, a refusal: a notification, and nothing else. */
  refuse(message: string): void;
}

/**
 * The file of a flow of *title*, *questions*' answers about it, and its ticket; `undefined` when
 * the dialog is cancelled, a question answers `undefined` (Esc), or another flow of *title* is
 * busy (refused with {@link importBusyMessage}), checked before the dialog and again after, since
 * a flow may have begun meanwhile. The questions come before the ticket, so an Esc leaves a
 * waiting tab as it was.
 */
async function beginImport<T>(
  host: ImportFlowHost,
  title: string,
  questions: (file: string) => Promise<T | undefined>,
): Promise<{ file: string; answers: T; ticket: ImportFlowTicket } | undefined> {
  if (host.tabs.isBusy(title)) {
    host.refuse(importBusyMessage(host.label));
    return undefined;
  }
  const file = await host.pickFile();
  if (file === undefined) {
    return undefined;
  }
  const answers = await questions(file);
  if (answers === undefined) {
    return undefined;
  }
  const ticket = host.tabs.begin(title);
  if (ticket === undefined) {
    host.refuse(importBusyMessage(host.label));
    return undefined;
  }
  return { file, answers, ticket };
}

/** A refused dry run: its report in the tab with no button, and its outcome recorded. */
async function showRefusedDryRun(
  host: DryRunSequenceHost,
  view: Omit<ImportTabView, "report" | "buttons">,
  report: ImportReport,
  run: CapturedRun,
): Promise<void> {
  host.tabs.show({ ...view, report, buttons: [] }, await host.render(report));
  host.record(run);
}

/** What the dry-run sequence needs of its host: the tabs, the renderer and the outcome recorder. */
export type DryRunSequenceHost = Pick<ImportFlowHost, "tabs" | "render" | "record">;

/**
 * The end of every dry-run sequence flow, after the applied run: its report, read by *wording*,
 * replaces the page in the same tab, without buttons. A failed run's outcome is recorded at once;
 * an exit 0 is handed to *succeeded* with its tab, which shows it and records the outcome. Answers
 * whether the run exited 0.
 */
async function finishApplied(
  host: DryRunSequenceHost & Pick<ImportFlowHost, "executable">,
  view: Pick<ImportTabView, "title" | "run" | "file">,
  appliedRun: CapturedRun,
  wording: AppliedRunWording,
  succeeded: (done: ImportTabView, rendering: ImportReportRendering) => Promise<void>,
): Promise<boolean> {
  const applied = importAppliedOutcome(appliedRun, host.executable, wording);
  const done: ImportTabView = { ...view, report: applied.report, buttons: [] };
  const rendering = await host.render(applied.report);
  if (applied.kind !== "imported") {
    host.tabs.show(done, rendering);
    host.record(appliedRun);
    return false;
  }
  await succeeded(done, rendering);
  return true;
}

/**
 * The end of either import's flow: on exit 0 the tab offers **Check Model** beside *doneLine*,
 * which runs `SWCraft: Check Model` only on a click, after the flow has ended; the page then keeps
 * the report without a button. The outcome is recorded once, right after the report is shown.
 */
async function finishImport(
  host: ImportFlowHost,
  ticket: ImportFlowTicket,
  view: Pick<ImportTabView, "title" | "run" | "file">,
  appliedRun: CapturedRun,
  doneLine: string,
): Promise<void> {
  await finishApplied(host, view, appliedRun, IMPORT_RUN_WORDING, async (done, rendering) => {
    const next = host.tabs.ask(
      ticket,
      { ...done, buttons: checkModelButtons(), status: doneLine },
      rendering,
    );
    host.record(appliedRun);
    if ((await next) === "checkModel") {
      host.tabs.show(done, rendering);
      host.tabs.end(ticket);
      await host.checkModel();
    }
  });
}

/**
 * One flow's dry-run sequence after its dry run: *preview*'s report in *view*'s tab, and *write*
 * only when *offer* holds and the apply button is clicked.
 */
export interface DryRunSequence<R = void> {
  ticket: ImportFlowTicket;
  /** The preview's tab: its title, its run word and its file. */
  view: Pick<ImportTabView, "title" | "run" | "file">;
  previewRun: CapturedRun;
  /** The dry run's report, and whether its Write is offered: a refused dry run's is not. */
  preview: { report: ImportReport; offer: boolean };
  /** The buttons under an offered preview: {@link applyButtons}. */
  buttons: ImportTabButton[];
  /** The applied run, started only on the apply button. */
  write(): Promise<CapturedRun>;
  /**
   * Asked on the apply button, before *write*: a refusal's message when the applied run must not
   * start now (Trace Apply's unsaved proposals buffer), else `undefined`.
   */
  writeRefusal?(): string | undefined;
  /** The applied run's end: its report into the same tab ({@link finishApplied}); what it answers is the sequence's. */
  finish(appliedRun: CapturedRun): Promise<R>;
}

/**
 * What came of a dry-run sequence: written, with what *finish* answered; declined; shown with no
 * button; or refused at the apply button with *message*.
 */
export type DryRunSequenceEnd<R> =
  | { answer: "written"; finished: R }
  | { answer: "declined" }
  | { answer: "shown" }
  | { answer: "refused"; message: string };

/**
 * The dry-run sequence the imports and Trace Apply share. A preview not offered is shown without a
 * button and recorded; an offered one waits for one click (nothing stands beside the buttons: the
 * report already says what Write would do), and only the apply action runs *write* and then
 * *finish*. Cancel or a closed tab runs and records nothing. An offered dry run is **not**
 * recorded: the panel item shows the last outcome that ended a flow, and a waiting preview ends
 * none, so a cancelled Apply leaves the item's earlier outcome in place. An apply click
 * *writeRefusal* refuses runs and records nothing: the preview stays without a button and the
 * caller says the refusal.
 */
export async function dryRunSequence<R>(
  host: DryRunSequenceHost,
  sequence: DryRunSequence<R>,
): Promise<DryRunSequenceEnd<R>> {
  const { ticket, view, previewRun, preview } = sequence;
  if (!preview.offer) {
    await showRefusedDryRun(host, view, preview.report, previewRun);
    return { answer: "shown" };
  }
  const answer = await host.tabs.ask(
    ticket,
    { ...view, report: preview.report, buttons: sequence.buttons },
    await host.render(preview.report),
  );
  if (answer !== "apply") {
    return { answer: "declined" };
  }
  const refusal = sequence.writeRefusal?.();
  if (refusal !== undefined) {
    host.tabs.show(
      { ...view, report: preview.report, buttons: [] },
      await host.render(preview.report),
    );
    return { answer: "refused", message: refusal };
  }
  return { answer: "written", finished: await sequence.finish(await sequence.write()) };
}

/**
 * What the requirements import's questions need of their host besides {@link ImportFlowHost}. Each
 * prompt answers `undefined` for Esc.
 */
export interface RequirementsImportHost extends ImportFlowHost {
  /** The project's directory, which a remembered mapping is stored relative to. */
  projectDir: string;
  /**
   * The mapping setting's value in the workspace folder alone (`inspect().workspaceFolderValue`):
   * the one place {@link rememberMapping} writes and clears, so a user or workspace-file value is
   * neither offered nor left behind.
   */
  rememberedMapping(): unknown;
  /** The mapping pick over *items*; the item picked. */
  pickMapping(items: MappingPickItem[]): Promise<MappingPickItem | undefined>;
  /** The open dialog behind *Choose another mapping file…*: the file's absolute path. */
  pickMappingFile(): Promise<string | undefined>;
  /**
   * Write *value* into the workspace folder's mapping setting, or clear it for `undefined`. A failed
   * write is the host's line in the channel, never a throw.
   */
  rememberMapping(value: string | undefined): Promise<void>;
  /** The needs file's text, or `undefined` when it cannot be read. */
  readFile(file: string): Promise<string | undefined>;
  /** The version box with *options* ({@link baselineBoxOptions}): the value entered. */
  askVersion(options: ReturnType<typeof baselineBoxOptions>): Promise<string | undefined>;
}

/** The requirements import's answers both its runs carry: `--mapping=` and `--baseline=`, each absent for none. */
export interface RequirementsImportAnswers {
  mapping: string | undefined;
  baseline: string | undefined;
}

/**
 * The requirements import's mapping pick: the remembered mapping (*last used*), *Choose another
 * mapping file…* or *SWCraft's own names (no mapping)*. `{ mapping }` is the absolute file for the
 * argv, `undefined` inside for no mapping; `undefined` itself is Esc, and the command ends before
 * any run.
 *
 * The setting is written at the pick, before the run, so a run refused for another reason does not
 * forget a good mapping. The remembered value is used as stored and not checked here: a missing or
 * refused file is the dry run's refusal, and the next import offers it again.
 */
export async function askRequirementsMapping(
  host: RequirementsImportHost,
): Promise<{ mapping: string | undefined } | undefined> {
  const remembered = host.rememberedMapping();
  const answer = await askSettingPath({
    projectDir: host.projectDir,
    remembered,
    items: mappingPickItems(remembered),
    pick: (items) => host.pickMapping(items),
    pickPath: () => host.pickMappingFile(),
    remember: (value) => host.rememberMapping(value),
    noneValue: undefined,
  });
  return answer === undefined ? undefined : { mapping: answer.path };
}

/**
 * The delivery's version for *file*: the box prefilled with the file's own `current_version`
 * (`fileDeliveryVersion()`, the one key the extension reads), empty and required when the file
 * cannot be read or names none. `{ baseline }` is the `--baseline` both runs carry, none when the
 * file's version is kept ({@link baselineArgFor}); `undefined` itself is Esc.
 */
export async function askDeliveryVersion(
  host: RequirementsImportHost,
  file: string,
): Promise<{ baseline: string | undefined } | undefined> {
  const text = await host.readFile(file);
  const fileVersion = text === undefined ? undefined : fileDeliveryVersion(text);
  const typed = await host.askVersion(baselineBoxOptions(fileVersion));
  if (typed === undefined) {
    return undefined;
  }
  return { baseline: baselineArgFor(typed, fileVersion) };
}

/** The requirements import's two questions, in their order: the mapping, then the version. */
async function askRequirementsQuestions(
  host: RequirementsImportHost,
  file: string,
): Promise<RequirementsImportAnswers | undefined> {
  const chosen = await askRequirementsMapping(host);
  if (chosen === undefined) {
    return undefined;
  }
  const version = await askDeliveryVersion(host, file);
  if (version === undefined) {
    return undefined;
  }
  return { mapping: chosen.mapping, baseline: version.baseline };
}

/**
 * `SWCraft: Import Requirements`'s flow: the file picked, the mapping and version asked (Esc ends
 * the flow before any run), `swcraft import requirements --dry-run`, its report in the
 * *Requirements Import Report* tab with **Write** and **Cancel**, and the applied run (the same
 * argv without `--dry-run`) only on **Write**. A refused dry run is shown without a button and
 * recorded; a declined choice records nothing; the applied run is always recorded. A start while
 * another flow of it is busy is refused.
 */
export async function importRequirementsFlow(
  host: RequirementsImportHost,
  run: (
    file: string,
    options: { dryRun: boolean } & RequirementsImportAnswers,
  ) => Promise<CapturedRun>,
): Promise<void> {
  const title = IMPORT_REPORT_TITLE;
  const begun = await beginImport(host, title, (file) => askRequirementsQuestions(host, file));
  if (begun === undefined) {
    return;
  }
  const { file, answers, ticket } = begun;
  try {
    const tabView = (word: string) => ({ title, run: word, file });
    const previewRun = await run(file, { dryRun: true, ...answers });
    const preview = importDryRunOutcome(previewRun, host.executable, IMPORT_RUN_WORDING);
    await dryRunSequence(host, {
      ticket,
      view: tabView("Dry run"),
      previewRun,
      preview: { report: preview.report, offer: preview.kind === "report" },
      buttons: applyButtons(false),
      write: () => run(file, { dryRun: false, ...answers }),
      finish: (appliedRun) =>
        finishImport(
          host,
          ticket,
          tabView("Import"),
          appliedRun,
          importDoneMessage(file, summaryOf(appliedRun.stdout)),
        ),
    });
  } finally {
    host.tabs.end(ticket);
  }
}

/**
 * `SWCraft: Import Component`'s flow: the file picked, `swcraft import component --dry-run`, its
 * report in the *Component Import Report* tab with **Write** and **Cancel**, and the applied run
 * (the last dry run's argv without `--dry-run`) only on the apply button. A dry run with exit
 * status 3 (`UPDATE_RESOLVES_EXIT`) offers **Show Update** and **Cancel**; Show Update runs the
 * `--dry-run --update` preview into the same tab, then offering **Update** and **Cancel**. The
 * offer is decided by the exit status, never the refusal's text. Every other refused dry run is
 * shown without a button and recorded. Cancel or a closed tab runs and records nothing. A start
 * while another flow of it is busy is refused.
 */
export async function importComponentFlow(
  host: ImportFlowHost,
  run: (file: string, options: { dryRun: boolean; update: boolean }) => Promise<CapturedRun>,
): Promise<void> {
  const title = COMPONENT_IMPORT_REPORT_TITLE;
  const begun = await beginImport(host, title, async () => ({}));
  if (begun === undefined) {
    return;
  }
  const { file, ticket } = begun;
  try {
    const tabView = (word: string) => ({ title, run: word, file });
    let update = false;
    let previewRun = await run(file, { dryRun: true, update });
    let preview = componentDryRunOutcome(previewRun, host.executable, update);
    if (preview.kind === "updateResolves") {
      const offer = await host.tabs.ask(
        ticket,
        { ...tabView("Dry run"), report: preview.report, buttons: showUpdateButtons() },
        await host.render(preview.report),
      );
      if (offer !== "showUpdate") {
        return;
      }
      update = true;
      previewRun = await run(file, { dryRun: true, update });
      preview = componentDryRunOutcome(previewRun, host.executable, update);
    }
    // The buttons are offered whatever the report says, `unchanged` included.
    await dryRunSequence(host, {
      ticket,
      view: tabView(update ? "Update dry run" : "Dry run"),
      previewRun,
      preview: { report: preview.report, offer: preview.kind === "report" },
      buttons: applyButtons(update),
      write: () => run(file, { dryRun: false, update }),
      finish: (appliedRun) =>
        finishImport(
          host,
          ticket,
          tabView(update ? "Update" : "Import"),
          appliedRun,
          importDoneMessage(file, undefined),
        ),
    });
  } finally {
    host.tabs.end(ticket);
  }
}

// --- Trace Apply in the imports' dry-run sequence ----------------------------------
//
// Apply… opens the preview in the *Trace Apply Report* tab with **Write** and **Cancel**, the
// imports' tab and buttons; Write runs the applied run bound to the previewed plan by
// `--expect-plan`, and the same tab turns into the applied report. No modal and no notification:
// the done line goes to the output channel.

/** What Trace Apply's flow needs of its host besides the dry-run sequence's. */
export interface TraceApplyHost extends DryRunSequenceHost {
  /** The executable the runs spawn, named by a run that could not start it. */
  executable: string;
  /** Reload the proposals file into the view after the applied run, whatever it came to. */
  reload(): void;
  /** One plain line in the output channel: the applied run's done line. */
  log(line: string): void;
  /** Say *message*, a refusal: a notification, and nothing else. */
  refuse(message: string): void;
  /**
   * Asked on **Write**, before the applied run: the refusal when the proposals file has unsaved
   * edits (the view asks the same before the dry run), else `undefined`.
   */
  writeRefusal(): string | undefined;
}

/** What the Apply flow ended in: applied (or nothing to apply), or stopped. */
export type TraceApplyOutcome = "applied" | "stopped";

/**
 * Trace Apply's flow over the proposals *file*: the dry run, its preview in the *Trace Apply
 * Report* tab ({@link traceApplyPreview}) through {@link dryRunSequence} (**Write** and **Cancel**
 * under a plan that writes something; no button under *No change.* or a refused or unreadable dry
 * run), and on Write the applied run with `--expect-plan`, the proposals reloaded, its report in
 * the same tab, its outcome recorded and, on exit 0, its done line ({@link applyDoneMessage}) in
 * the channel. Write re-asks *writeRefusal* first (the buffer may have become dirty meanwhile); a
 * refusal runs and records nothing and is said through *refuse*. The dry run is recorded only when
 * its preview offers no Write. Answers *applied* for an exit-0 applied run or a plan with no
 * change, *stopped* otherwise. A start while another Apply flow holds the tab is refused with
 * `BUSY_REFUSAL`.
 */
export async function traceApplyFlow(
  host: TraceApplyHost,
  file: string,
  run: (options: TraceApplyRunOptions) => Promise<CapturedRun>,
): Promise<TraceApplyOutcome> {
  const ticket = host.tabs.begin(APPLY_REPORT_TITLE);
  if (ticket === undefined) {
    host.refuse(BUSY_REFUSAL);
    return "stopped";
  }
  try {
    const previewRun = await run({ dryRun: true });
    const preview = traceApplyPreview(previewRun, host.executable);
    const expectPlan = preview.kind === "write" ? preview.plan : undefined;
    const ended = await dryRunSequence(host, {
      ticket,
      view: { title: APPLY_REPORT_TITLE, run: "Dry run", file },
      previewRun,
      preview: { report: preview.report, offer: expectPlan !== undefined },
      buttons: applyButtons(false),
      writeRefusal: () => host.writeRefusal(),
      write: () => {
        if (expectPlan === undefined) {
          // dryRunSequence runs write only for an offered preview, and only a plan offers one.
          throw new Error("traceApplyFlow: Write was offered without a previewed plan");
        }
        return run({ dryRun: false, expectPlan });
      },
      finish: (appliedRun) => {
        host.reload();
        const view = { title: APPLY_REPORT_TITLE, run: "Apply", file };
        return finishApplied(
          host,
          view,
          appliedRun,
          TRACE_APPLY_WORDING,
          async (done, rendering) => {
            host.tabs.show(done, rendering);
            host.record(appliedRun);
            const summary = applySummaryOf(appliedRun.stdout);
            host.log(
              summary === undefined
                ? "SWCraft: the decisions were applied."
                : applyDoneMessage(summary),
            );
          },
        );
      },
    });
    if (ended.answer === "written") {
      return ended.finished ? "applied" : "stopped";
    }
    if (ended.answer === "refused") {
      host.refuse(ended.message);
      return "stopped";
    }
    return preview.kind === "noChange" ? "applied" : "stopped";
  } finally {
    host.tabs.end(ticket);
  }
}

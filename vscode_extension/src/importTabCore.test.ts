// The imports' tab (importTabCore.ts): the review tab's page and styles holding an import's report
// and its buttons. The host's shape (one tab per title, Cancel closing it as Reject does, the
// boundary to the review channel) is pinned in commandCore.test.ts.

import assert from "node:assert/strict";
import * as os from "node:os";
import * as path from "node:path";
import { test } from "node:test";

import {
  BASELINE_PROMPT_TEXT,
  type CapturedRun,
  COMPONENT_IMPORT_REPORT_TITLE,
  COMPONENT_MARKDOWN_HEADING,
  COMPONENT_UPDATE_OFFER_LINE,
  codeFence,
  componentDryRunOutcome,
  IMPORT_INCOMPLETE_LINE,
  IMPORT_REPORT_FAILED_LINE,
  IMPORT_REPORT_TITLE,
  IMPORT_RUN_WORDING,
  type ImportReport,
  importAppliedOutcome,
  importDryRunOutcome,
  importWarningLines,
  type MappingChoice,
  type MappingPickItem,
  NOTHING_WRITTEN_LINE,
  TRACE_APPLY_MARKDOWN_HEADING,
} from "./commandCore";
import {
  acceptImportTabMessage,
  applyButtons,
  askDeliveryVersion,
  askRequirementsMapping,
  checkModelButtons,
  IMPORT_REPORT_PLAIN_LINE,
  IMPORT_TAB_BUSY_LINE,
  IMPORT_TAB_VIEW_TYPE,
  type ImportAction,
  type ImportFlowHost,
  type ImportReportRendering,
  ImportTabs,
  type ImportTabView,
  importBusyMessage,
  importComponentFlow,
  importReportMarkdown,
  importRequirementsFlow,
  importTabHtml,
  importTabScript,
  newImportToken,
  type RequirementsImportAnswers,
  type RequirementsImportHost,
  renderImportReport,
  showUpdateButtons,
  type TraceApplyHost,
  tabReportMarkdown,
  traceApplyFlow,
} from "./importTabCore";
import { COMPONENT_REPORT_HEADER } from "./linkCore";
import { escapeHtml, reviewCsp, reviewTabPage } from "./reviewCore";
import {
  APPLY_REPORT_TITLE,
  BUSY_REFUSAL,
  buildTraceApplyArgv,
  DIRTY_REFUSAL,
  isDirtyRefusal,
  TRACE_APPLY_UNREADABLE_LINE,
  TRACE_APPLY_WORDING,
  type TraceApplyRunOptions,
} from "./traceCore";

const NONCE = "abc123";
const PLAIN: ImportReportRendering = { kind: "plain" };
const RENDERED = {
  kind: "markdown",
  html: "<pre><code>SWC SpeedSensor: created\n</code></pre>",
} as const;
const TOKEN = "0123456789abcdef0123456789abcdef"; // gitleaks:allow -- a made-up hex test vector, not a secret
const SOURCE = path.resolve(os.tmpdir(), "SpeedSensor.arxml");
const DRY_RUN_REPORT = [
  "Component import report: SpeedSensor.arxml",
  "(DRY RUN -- no files were written)",
  "SWC SpeedSensor: created",
  "CREATED 1 interface(s)",
  "  - ISpeed",
  "",
].join("\n");

function view(overrides: Partial<ImportTabView> = {}): ImportTabView {
  return {
    title: COMPONENT_IMPORT_REPORT_TITLE,
    run: "Dry run",
    file: SOURCE,
    report: { cliText: DRY_RUN_REPORT, notes: [] },
    buttons: applyButtons(false),
    ...overrides,
  };
}

function bodyOf(html: string): string {
  return html.slice(html.indexOf("<body>"), html.indexOf("</body>"));
}

function styleOf(html: string): string {
  return html.slice(html.indexOf("<style"), html.indexOf("</style>"));
}

// --- The page ----------------------------------------------------------------

test("importTabHtml: the heading and meta line, the rendered report, then the buttons and an empty line", () => {
  const html = importTabHtml(view(), { nonce: NONCE, rendering: RENDERED, token: TOKEN });
  const body = bodyOf(html);
  const order = [
    "<h1>Component Import Report</h1>",
    '<div class="meta"><span>Run <b>Dry run</b></span><span>File <b>SpeedSensor.arxml</b></span></div>',
    `<div id="report" class="markdown">\n${RENDERED.html}\n</div>`,
    '<div class="actions">',
    '<button type="button" class="approve" data-action="apply">Write</button>',
    '<button type="button" class="reject" data-action="cancel">Cancel</button>',
    '<span id="status" class="wait" role="status"></span>',
  ];
  let at = -1;
  for (const piece of order) {
    const next = body.indexOf(piece);
    assert.ok(next > at, `'${piece}' follows the piece before it`);
    at = next;
  }
  // The file's name only, never its directory.
  assert.ok(!html.includes(path.dirname(SOURCE)), "the page names no directory");
  // Rendered, the page has no plain-text fallback and no line saying so.
  assert.ok(!html.includes(IMPORT_REPORT_PLAIN_LINE));
  assert.ok(!html.includes('<pre id="report">'));
});

test("importTabHtml: the plain-text fallback — its one line, the CLI's text in the <pre>, the notes as paragraphs", () => {
  const offer = view({
    report: { cliText: DRY_RUN_REPORT, notes: [COMPONENT_UPDATE_OFFER_LINE] },
    buttons: showUpdateButtons(),
  });
  const html = importTabHtml(offer, { nonce: NONCE, rendering: PLAIN, token: TOKEN });
  const body = bodyOf(html);
  const order = [
    "</header>",
    `<p class="plain-note">${IMPORT_REPORT_PLAIN_LINE}</p>`,
    `<pre id="report">\n${DRY_RUN_REPORT.trimEnd()}</pre>`,
    `<p>${COMPONENT_UPDATE_OFFER_LINE}</p>`,
    '<div class="actions">',
  ];
  let at = -1;
  for (const piece of order) {
    const next = body.indexOf(piece);
    assert.ok(next > at, `'${piece}' follows the piece before it`);
    at = next;
  }
  assert.ok(!html.includes('class="markdown"'));
  assert.equal(
    IMPORT_REPORT_PLAIN_LINE,
    "The Markdown renderer is not available; the report is shown as plain text.",
  );
});

test("importTabHtml: the page is the review tab's — one shared helper, its CSP, its style", () => {
  const html = importTabHtml(view(), { nonce: NONCE, rendering: PLAIN, token: TOKEN });
  const shared = reviewTabPage({ nonce: NONCE, title: "x", body: [] });
  // The review tab's style, not a copy of it, and the import tab's Markdown rules after it.
  assert.ok(styleOf(html).startsWith(styleOf(shared)), "the review tab's style comes first");
  assert.match(styleOf(html), /\n\.markdown \{[^}]*\}/);
  assert.ok(!styleOf(shared).includes(".markdown"), "the review tab's own page carries none");
  assert.ok(
    html.includes(`<meta http-equiv="Content-Security-Policy" content="${reviewCsp(NONCE)}">`),
  );
  assert.ok(html.includes(`<style nonce="${NONCE}">`));
  assert.equal(html.match(/<script nonce="abc123">/g)?.length, 1);
  assert.equal(html.match(/<script/g)?.length, 1);
  assert.ok(html.startsWith("<!DOCTYPE html>\n"));
  assert.ok(html.includes("<title>Component Import Report</title>"));
  // The primary and the secondary button take the review tab's Approve and Reject styles.
  const style = styleOf(html);
  assert.match(style, /\.approve \{[^}]*var\(--vscode-button-background\)/);
  assert.match(style, /\.reject \{[^}]*var\(--vscode-button-secondaryBackground\)/);
  assert.match(style, /pre \.warn \{[^}]*var\(--vscode-inputValidation-warningBackground\)/);
  assert.ok(!/autofocus/i.test(html), "no button has autofocus");
  assert.ok(!/\son[a-z]+=/i.test(html), "no inline event handler");
  assert.ok(!/\ssrc=|\shref=/i.test(html), "no resource is loaded");
  assert.equal(IMPORT_TAB_VIEW_TYPE, "swcraft.import");
});

test("importTabHtml: the button sets — Write or Update with Cancel, Show Update with Cancel, Check Model alone", () => {
  const buttons = (html: string) =>
    [
      ...html.matchAll(
        /<button type="button" class="(\w+)" data-action="(\w+)">([^<]*)<\/button>/g,
      ),
    ].map((match) => [match[1], match[2], match[3]]);
  assert.deepEqual(
    buttons(importTabHtml(view(), { nonce: NONCE, rendering: PLAIN, token: TOKEN })),
    [
      ["approve", "apply", "Write"],
      ["reject", "cancel", "Cancel"],
    ],
  );
  assert.deepEqual(
    buttons(
      importTabHtml(view({ run: "Update dry run", buttons: applyButtons(true) }), {
        nonce: NONCE,
        rendering: PLAIN,
        token: TOKEN,
      }),
    ),
    [
      ["approve", "apply", "Update"],
      ["reject", "cancel", "Cancel"],
    ],
  );
  assert.deepEqual(
    buttons(
      importTabHtml(view({ buttons: showUpdateButtons() }), {
        nonce: NONCE,
        rendering: PLAIN,
        token: TOKEN,
      }),
    ),
    [
      ["approve", "showUpdate", "Show Update"],
      ["reject", "cancel", "Cancel"],
    ],
  );
  assert.deepEqual(
    buttons(
      importTabHtml(view({ run: "Import", buttons: checkModelButtons() }), {
        nonce: NONCE,
        rendering: PLAIN,
        token: TOKEN,
      }),
    ),
    [["approve", "checkModel", "Check Model"]],
  );
});

test("importTabHtml: Import Requirements takes the same form", () => {
  const html = importTabHtml(
    {
      title: IMPORT_REPORT_TITLE,
      run: "Dry run",
      file: "/work/req/needs.json",
      report: { cliText: "Summary: 1 added, 0 changed\n", notes: [] },
      buttons: applyButtons(false),
    },
    { nonce: NONCE, rendering: PLAIN, token: TOKEN },
  );
  assert.ok(html.includes("<h1>Requirements Import Report</h1>"));
  assert.ok(html.includes("File <b>needs.json</b>"));
  assert.ok(
    html.indexOf('<pre id="report">') < html.indexOf('<div class="actions">'),
    "the report above the buttons",
  );
  assert.ok(html.includes('class="approve" data-action="apply">Write</button>'));
  assert.ok(html.includes('class="reject" data-action="cancel">Cancel</button>'));
  assert.ok(
    html.includes('<span id="status" class="wait" role="status"></span>'),
    "no line beside the buttons: the report above them is the question",
  );
});

test("importTabHtml: a warning block stands above the report in the review tab's yellow", () => {
  const warnings = ["Warning: the manifest key 'colour' is unknown"];
  const html = importTabHtml(view({ report: { cliText: DRY_RUN_REPORT, notes: [], warnings } }), {
    nonce: NONCE,
    rendering: PLAIN,
    token: TOKEN,
  });
  const block = html.indexOf(
    `<pre id="warning"><span class="warn">Warning: the manifest key &#39;colour&#39; is unknown</span></pre>`,
  );
  assert.ok(block !== -1, html);
  assert.ok(block < html.indexOf('<pre id="report">'), "above the report");
  assert.ok(block > html.indexOf("</header>"), "under the heading");
  // No warning, no block.
  assert.ok(
    !importTabHtml(view(), { nonce: NONCE, rendering: PLAIN, token: TOKEN }).includes(
      'id="warning"',
    ),
  );
});

test("importTabHtml: busy renders every button disabled and the busy line, with no script and no token", () => {
  const html = importTabHtml(view(), { nonce: NONCE, rendering: PLAIN, busy: true });
  assert.equal(html.match(/<button [^>]* disabled>/g)?.length, 2);
  assert.ok(html.includes(`role="status">${IMPORT_TAB_BUSY_LINE}</span>`));
  assert.doesNotMatch(html, /<script/);
  assert.throws(
    () => importTabHtml(view(), { nonce: NONCE, rendering: PLAIN }),
    /live buttons carries its token/,
  );
  const live = importTabHtml(view(), { nonce: NONCE, rendering: PLAIN, token: TOKEN });
  assert.equal(/ disabled>/.test(live), false);
});

// --- Error and edge cases --------------------------------------------------------

test("importTabHtml: a refused dry run ends with Nothing was written. and has no button and no script", () => {
  const outcome = importDryRunOutcome(
    { exitCode: 1, spawnFailed: false, stdout: "", stderr: "Error: SWC 'X' diverges\n" },
    "/opt/swcraft",
    IMPORT_RUN_WORDING,
  );
  const html = importTabHtml(view({ report: outcome.report, buttons: [], status: undefined }), {
    nonce: NONCE,
    rendering: PLAIN,
  });
  assert.ok(html.includes(`<p>${NOTHING_WRITTEN_LINE}</p>\n</body>`), html);
  assert.ok(html.includes("Error: SWC &#39;X&#39; diverges"));
  assert.doesNotMatch(html, /<button|<script|class="actions"/);
});

test("importTabHtml: a failed applied run never says Nothing was written, ends with the did-not-complete line", () => {
  for (const run of [
    { exitCode: 1, spawnFailed: false, stdout: "", stderr: "Error: aborted\n" },
    { exitCode: null, spawnFailed: true, stdout: "", stderr: "" },
    { exitCode: null, spawnFailed: false, stdout: "", stderr: "" },
  ]) {
    const applied = importAppliedOutcome(run, "/opt/swcraft", IMPORT_RUN_WORDING);
    assert.equal(applied.kind, "failed");
    const html = importTabHtml(view({ run: "Import", report: applied.report, buttons: [] }), {
      nonce: NONCE,
      rendering: PLAIN,
    });
    assert.doesNotMatch(html, /Nothing was written/);
    assert.ok(html.includes(`<p>${IMPORT_INCOMPLETE_LINE}</p>\n</body>`), html);
    assert.doesNotMatch(html, /<button/);
  }
});

test("importTabHtml: CLI text, notes, the file name, the run and the line cannot inject HTML", () => {
  const hostile = "<script>alert(1)</script>\n</pre><b>x</b>\n<img src=x onerror=alert(1)>\n";
  const html = importTabHtml(
    view({
      title: "Report <i>",
      run: "<u>run</u>",
      file: path.resolve(os.tmpdir(), "<b>evil</b>.arxml"),
      report: {
        cliText: hostile,
        notes: ["note <script>x</script>"],
        warnings: ["Warning: <b>bold</b>"],
      },
      status: "status </span><button>",
    }),
    { nonce: NONCE, rendering: PLAIN, token: TOKEN },
  );
  assert.equal(html.match(/<script/g)?.length, 1, "the one nonce'd script");
  assert.equal(html.match(/<button/g)?.length, 2, "the two buttons of the view");
  assert.equal(html.match(/<pre/g)?.length, 2, "the warning and the report");
  assert.equal(html.match(/<\/pre>/g)?.length, 2);
  assert.ok(
    !html.includes("<b>x</b>") && !html.includes("<b>evil</b>") && !html.includes("<b>bold</b>"),
  );
  assert.ok(!html.includes("<img") && !html.includes("<u>") && !html.includes("<i>"));
  assert.ok(html.includes("&lt;script&gt;alert(1)&lt;/script&gt;"));
  assert.ok(html.includes("&lt;/pre&gt;&lt;b&gt;x&lt;/b&gt;"));
  assert.ok(html.includes("<title>Report &lt;i&gt;</title>"));
});

test("importTabHtml: a page with buttons needs its token; a bad token or nonce throws", () => {
  assert.throws(() => importTabHtml(view(), { nonce: NONCE, rendering: PLAIN }), /token/);
  assert.throws(() => importTabScript('0123"; alert(1); "'), /token/);
  assert.throws(() => importTabScript("ABCDEF0123456789ABCDEF0123456789"), /token/);
  assert.throws(
    () => importTabHtml(view(), { nonce: 'a"b', rendering: PLAIN, token: TOKEN }),
    /nonce/,
  );
  const token = newImportToken();
  assert.match(token, /^[0-9a-f]{32}$/);
  assert.notEqual(token, newImportToken());
});

// --- The warning lines ----------------------------------------------------------

test("importWarningLines: Warning: and WARNING: at a line's start, stdout's then stderr's, CRLF trimmed", () => {
  assert.deepEqual(
    importWarningLines(
      "SWC A: created\r\nWARNING: one\r\n  Warning: indented, a value\n",
      "Warning: two\nwarning: lower case\nError: x\n",
    ),
    ["WARNING: one", "Warning: two"],
  );
  assert.deepEqual(importWarningLines("", ""), []);
});

test("the outcomes carry the run's warning lines, and only when it printed any", () => {
  const warned = {
    exitCode: 0,
    spawnFailed: false,
    stdout: DRY_RUN_REPORT,
    stderr: "Warning: w\n",
  };
  assert.deepEqual(importDryRunOutcome(warned, "/s", IMPORT_RUN_WORDING).report.warnings, [
    "Warning: w",
  ]);
  assert.deepEqual(importAppliedOutcome(warned, "/s", IMPORT_RUN_WORDING).report.warnings, [
    "Warning: w",
  ]);
  const exit3 = { ...warned, exitCode: 3, stdout: "" };
  assert.deepEqual(componentDryRunOutcome(exit3, "/s", false).report.warnings, ["Warning: w"]);
  const quiet = { ...warned, stderr: "" };
  assert.equal("warnings" in importDryRunOutcome(quiet, "/s", IMPORT_RUN_WORDING).report, false);
});

// --- What a message runs -----------------------------------------------------------

test("acceptImportTabMessage: the page's own token and one of its actions is the one yes", () => {
  const pending = { token: TOKEN, actions: ["apply", "cancel"] as const };
  assert.equal(acceptImportTabMessage({ action: "apply", token: TOKEN }, pending), "apply");
  assert.equal(acceptImportTabMessage({ action: "cancel", token: TOKEN }, pending), "cancel");
});

test("acceptImportTabMessage: no choice waiting, an earlier page's token, an unknown or unoffered action, anything else runs nothing", () => {
  const pending = { token: TOKEN, actions: ["apply", "cancel"] as const };
  // A late message or a second click: the choice has ended (busy, or decided).
  assert.equal(acceptImportTabMessage({ action: "apply", token: TOKEN }, undefined), undefined);
  // A message of an earlier page.
  assert.equal(
    acceptImportTabMessage({ action: "apply", token: newImportToken() }, pending),
    undefined,
  );
  assert.equal(acceptImportTabMessage({ action: "apply" }, pending), undefined);
  // An action the page does not offer, or none of the tab's.
  for (const action of ["checkModel", "showUpdate", "approve", "reject", "Write", "", 1, null]) {
    assert.equal(
      acceptImportTabMessage({ action, token: TOKEN }, pending),
      undefined,
      String(action),
    );
  }
  for (const message of [undefined, null, "apply", 7, [], ["apply", TOKEN]]) {
    assert.equal(acceptImportTabMessage(message, pending), undefined, String(message));
  }
});

// --- The tab's script, run against a stand-in for the page --------------------------

function runScript(actions: readonly string[]) {
  const posted: unknown[] = [];
  const buttons = actions.map((action) => {
    const handlers: (() => void)[] = [];
    return {
      dataset: { action },
      disabled: false,
      addEventListener: (_: string, handler: () => void) => handlers.push(handler),
      click: () => {
        for (const handler of handlers) handler();
      },
    };
  });
  const status = { textContent: "" };
  const document = {
    getElementById: () => status,
    querySelectorAll: () => buttons,
  };
  const acquireVsCodeApi = () => ({ postMessage: (message: unknown) => posted.push(message) });
  new Function("acquireVsCodeApi", "document", importTabScript(TOKEN))(acquireVsCodeApi, document);
  return { posted, buttons, status };
}

test("the tab's script: one click posts its action and the page's token once, and disables every button", () => {
  const page = runScript(["apply", "cancel"]);
  page.buttons[0].click();
  assert.deepEqual(page.posted, [{ action: "apply", token: TOKEN }]);
  assert.ok(page.buttons.every((button) => button.disabled));
  assert.equal(page.status.textContent, IMPORT_TAB_BUSY_LINE);
  // A second click, on either button, posts nothing.
  page.buttons[0].click();
  page.buttons[1].click();
  assert.equal(page.posted.length, 1);
  // The one message the host takes runs the apply once; the same message again finds no choice.
  const pending = { token: TOKEN, actions: ["apply", "cancel"] as const };
  assert.equal(acceptImportTabMessage(page.posted[0], pending), "apply");
  assert.equal(acceptImportTabMessage(page.posted[0], undefined), undefined);
});

test("the tab's script: Cancel posts cancel, nothing else", () => {
  const page = runScript(["apply", "cancel"]);
  page.buttons[1].click();
  assert.deepEqual(page.posted, [{ action: "cancel", token: TOKEN }]);
});

// --- The report as Markdown --------------------------------------------------------

const run = (exitCode: number | null, stdout: string, stderr: string, spawnFailed = false) => ({
  exitCode,
  spawnFailed,
  stdout,
  stderr,
});
const DIVERGES = "Error: SWC 'SpeedSensor' already exists in the target project and diverges\n";

test("importReportMarkdown: a created dry run is its stdout fenced, with no line of the extension's under it", () => {
  const outcome = componentDryRunOutcome(run(0, DRY_RUN_REPORT, ""), "/s", false);
  assert.equal(
    importReportMarkdown(outcome.report),
    [
      "```",
      "Component import report: SpeedSensor.arxml",
      "(DRY RUN -- no files were written)",
      "SWC SpeedSensor: created",
      "CREATED 1 interface(s)",
      "  - ISpeed",
      "```",
      "",
    ].join("\n"),
  );
});

test("importReportMarkdown: exit 3, the update dry run, a refusal, an applied and a failed applied run, Requirements", () => {
  const fenced = (text: string) => `\`\`\`\n${text.trimEnd()}\n\`\`\``;
  const md = (...parts: string[]) => `${parts.join("\n\n")}\n`;
  // Exit 3: the stderr fenced, then the offer.
  assert.equal(
    importReportMarkdown(componentDryRunOutcome(run(3, "", DIVERGES), "/s", false).report),
    md(fenced(DIVERGES), escapeMarkdownLine(COMPONENT_UPDATE_OFFER_LINE)),
  );
  // The update dry run: its stdout fenced, nothing under it.
  const updateReport = DRY_RUN_REPORT.replace("created", "updated");
  assert.equal(
    importReportMarkdown(componentDryRunOutcome(run(0, updateReport, ""), "/s", true).report),
    md(fenced(updateReport)),
  );
  // A refusal: the stderr fenced, `Error: ` kept, then Nothing was written.
  assert.equal(
    importReportMarkdown(
      importDryRunOutcome(run(1, "", DIVERGES), "/s", IMPORT_RUN_WORDING).report,
    ),
    md(fenced(DIVERGES), "Nothing was written\\."),
  );
  // A spawn failure: no code block, the extension's two lines, the path escaped.
  assert.equal(
    importReportMarkdown(
      importDryRunOutcome(run(null, "", "", true), "/opt/my_swcraft", IMPORT_RUN_WORDING).report,
    ),
    md("The SWCraft CLI could not be started \\(/opt/my\\_swcraft\\)\\.", "Nothing was written\\."),
  );
  // An applied run that exited 0: its stdout alone.
  const applied = "SWC SpeedSensor: created\n";
  assert.equal(
    importReportMarkdown(
      importAppliedOutcome(run(0, applied, ""), "/s", IMPORT_RUN_WORDING).report,
    ),
    md(fenced(applied)),
  );
  // A failed applied run: the did-not-complete line, never Nothing was written.
  const failed = importReportMarkdown(
    importAppliedOutcome(run(1, "", "Error: aborted\n"), "/s", IMPORT_RUN_WORDING).report,
  );
  assert.equal(failed, md(fenced("Error: aborted"), escapeMarkdownLine(IMPORT_INCOMPLETE_LINE)));
  assert.doesNotMatch(failed, /Nothing was written/);
  // Import Requirements: the same shape, nothing under it either.
  const summary = "Summary: 1 added, 0 changed\n";
  assert.equal(
    importReportMarkdown(importDryRunOutcome(run(0, summary, ""), "/s", IMPORT_RUN_WORDING).report),
    md(fenced(summary)),
  );
  // Nothing at all: no Markdown.
  assert.equal(importReportMarkdown({ cliText: " \n", notes: [] }), "");
});

/** A note as the Markdown carries it: every metacharacter backslash-escaped. */
function escapeMarkdownLine(text: string): string {
  return text.replace(/[\\`*_{}[\]()#+\-.!<>|~&]/g, "\\$&");
}

test("codeFence: one backtick longer than the longest run of the text, never shorter than three", () => {
  assert.equal(codeFence("no backticks"), "```");
  assert.equal(codeFence("a `b` c ``d``"), "```");
  assert.equal(codeFence("```\nfenced\n```"), "````");
  assert.equal(codeFence("x ````` y ``"), "``````");
  // The CLI's text cannot close its own fence: no line of it is the fence or longer.
  const hostile = "line\n```\n<script>alert(1)</script>\n````\n# heading\n";
  const markdown = importReportMarkdown({ cliText: hostile, notes: ["Nothing was written."] });
  const fence = codeFence(hostile.trimEnd());
  assert.equal(fence, "`````");
  assert.ok(markdown.startsWith(`${fence}\n${hostile.trimEnd()}\n${fence}\n\n`), markdown);
  const fenceLines = markdown.split("\n").filter((line) => /^`{3,}/.test(line));
  assert.deepEqual(
    fenceLines.filter((line) => line.startsWith(fence)),
    [fence, fence],
    "only the opening and the closing fence are that long",
  );
});

test("renderImportReport: the renderer's HTML is placed in the page, the Markdown handed to it", async () => {
  const seen: string[] = [];
  const rendering = await renderImportReport(view().report, async (markdown) => {
    seen.push(markdown);
    return "<p>rendered</p>";
  });
  assert.deepEqual(rendering, { kind: "markdown", html: "<p>rendered</p>" });
  assert.deepEqual(seen, [importReportMarkdown(view().report)]);
  // A synchronous renderer is taken too.
  assert.deepEqual(await renderImportReport(view().report, () => "<p>sync</p>"), {
    kind: "markdown",
    html: "<p>sync</p>",
  });
  const html = importTabHtml(view(), { nonce: NONCE, rendering, token: TOKEN });
  assert.ok(html.includes('<div id="report" class="markdown">\n<p>rendered</p>\n</div>'), html);
});

// --- Error and edge cases of the rendering -----------------------------------------

test("renderImportReport: no renderer, a throwing or rejecting one, or a non-string answer falls back to plain text", async () => {
  const fallbacks = [
    await renderImportReport(view().report, undefined),
    await renderImportReport(view().report, () => {
      throw new Error("command 'markdown.api.render' not found");
    }),
    await renderImportReport(view().report, () => Promise.reject(new Error("rejected"))),
    await renderImportReport(view().report, () => undefined),
    await renderImportReport(view().report, async () => ({ html: "<p>x</p>" })),
  ];
  for (const rendering of fallbacks) {
    assert.deepEqual(rendering, { kind: "plain" });
    const html = importTabHtml(view(), { nonce: NONCE, rendering, token: TOKEN });
    assert.ok(html.includes(`<p class="plain-note">${IMPORT_REPORT_PLAIN_LINE}</p>`));
    assert.ok(html.includes(`<pre id="report">\n${escapeHtml(DRY_RUN_REPORT.trimEnd())}</pre>`));
    assert.ok(!html.includes("To apply this import later"));
  }
});

test("importTabHtml: a script in the rendered report never runs — the CSP admits the nonce'd script alone", async () => {
  // A stub renderer that returns the report's HTML unsanitised.
  const hostile =
    '<script>alert(1)</script><img src=x onerror="alert(2)"><button data-action="apply">Cancel</button>';
  const rendering = await renderImportReport(
    { cliText: "<script>alert(1)</script>", notes: [] },
    () => hostile,
  );
  assert.equal(rendering.kind, "markdown");
  for (const token of [TOKEN, undefined]) {
    const page = importTabHtml(
      view(token === undefined ? { buttons: [], status: undefined } : {}),
      { nonce: NONCE, rendering, token },
    );
    // The CSP: nothing by default, style and script by the nonce alone — no
    // 'unsafe-inline', so neither an unnonced <script> nor an onerror runs.
    assert.ok(
      page.includes(
        `<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'nonce-${NONCE}'; script-src 'nonce-${NONCE}'">`,
      ),
    );
    assert.doesNotMatch(page, /unsafe-inline|unsafe-eval|unsafe-hashes/);
    // The core adds no unnonced script: every <script the page holds beyond the
    // renderer's own is the nonce'd one, and only a page with buttons has it.
    const scripts = page.match(/<script[^>]*>/g) ?? [];
    const own = scripts.filter((tag) => tag !== "<script>");
    assert.deepEqual(own, token === undefined ? [] : [`<script nonce="${NONCE}">`]);
    assert.equal(scripts.length - own.length, 1, "only the renderer's, which the CSP blocks");
  }
  // The tab's script binds the buttons of its own actions row only, never one the report holds.
  assert.match(importTabScript(TOKEN), /querySelectorAll\("\.actions > button\[data-action\]"\)/);
});

// --- The CLI's own Markdown report (`--report <file>.md`) ---------------------

const CLI_MARKDOWN =
  "# Component import report\\: SpeedSensor\\.arxml\n\n(DRY RUN -- no files were written)\n\n**SWC SpeedSensor\\: created**\n\n## CREATED 1 interface(s)\n\n- ISpeed\n";

// The tab's heading of the same report: the file's name alone, under the tab's own title.
const TAB_MARKDOWN = CLI_MARKDOWN.replace("# Component import report\\: ", "# ");

test("importReportMarkdown: the CLI's Markdown report headed by the file's name, then the extension's lines; no fence", () => {
  const markdown = importReportMarkdown({
    cliText: DRY_RUN_REPORT,
    markdown: `${CLI_MARKDOWN}\n\n`,
    notes: [COMPONENT_UPDATE_OFFER_LINE],
  });
  assert.equal(markdown, `${TAB_MARKDOWN}\n${escapeMarkdownLine(COMPONENT_UPDATE_OFFER_LINE)}\n`);
  assert.ok(markdown.startsWith("# SpeedSensor\\.arxml\n"));
  assert.ok(!markdown.includes("```"), "the CLI's text is not fenced when its Markdown is there");
});

test("importReportMarkdown: an empty Markdown file falls back to the fenced text", () => {
  const markdown = importReportMarkdown({
    cliText: "SWC S: created\n",
    markdown: " \n",
    notes: [],
  });
  assert.equal(markdown, "```\nSWC S: created\n```\n");
});

test("renderImportReport: the renderer is handed the CLI's Markdown; the plain fallback still shows the text", async () => {
  const report = { cliText: DRY_RUN_REPORT, markdown: CLI_MARKDOWN, notes: [] };
  const seen: string[] = [];
  const rendering = await renderImportReport(report, (markdown) => {
    seen.push(markdown);
    return "<h1>Component import report: SpeedSensor.arxml</h1>";
  });
  assert.deepEqual(seen, [TAB_MARKDOWN]);
  const html = importTabHtml(view({ report }), { nonce: NONCE, rendering, token: TOKEN });
  assert.ok(
    html.includes(
      '<div id="report" class="markdown">\n<h1>Component import report: SpeedSensor.arxml</h1>\n</div>',
    ),
  );
  const plain = importTabHtml(view({ report }), {
    nonce: NONCE,
    rendering: await renderImportReport(report, undefined),
    token: TOKEN,
  });
  assert.ok(
    plain.includes(`<pre id="report">\n${escapeHtml(DRY_RUN_REPORT.replace(/\s+$/, ""))}</pre>`),
  );
});

test("tabReportMarkdown: the component report's heading is the file's name, Trace Apply's the search; anything else is unchanged", () => {
  // The heading the CLI writes (report.py's render_markdown, pinned there by
  // test_import_report_markdown.py): the title, its colon escaped, then the name.
  assert.equal(COMPONENT_MARKDOWN_HEADING, "# Component import report\\: ");
  assert.ok(COMPONENT_MARKDOWN_HEADING.startsWith(`# ${COMPONENT_REPORT_HEADER.slice(0, -2)}`));
  assert.equal(
    tabReportMarkdown(
      "# Component import report\\: a\\_\\[b\\](c)\\.arxml\\#\n\nSource: /x/a.arxml\n",
    ),
    "# a\\_\\[b\\](c)\\.arxml\\#\n\nSource: /x/a.arxml\n",
  );
  // Trace Apply's report document (trace_apply.py's render_report_markdown, pinned by
  // test_trace_apply_report_markdown.py): the tab's title says Trace Apply Report, so the
  // heading keeps the search alone and the title appears once.
  assert.equal(TRACE_APPLY_MARKDOWN_HEADING, "# Trace Apply Report — ");
  assert.equal(
    tabReportMarkdown(
      "# Trace Apply Report — search 'trace'\n\nApplied 2026-11-09 13:06 (UTC+01:00).\n",
    ),
    "# search 'trace'\n\nApplied 2026-11-09 13:06 (UTC+01:00).\n",
  );
  // The requirements report, an unescaped or an older heading, and the text form stay as written.
  for (const other of [
    "# Trace apply\\: proposals of the search 'trace'\n",
    "# Requirements import\\: baseline v1\n",
    "# Component import report: S.arxml\n",
    "Component import report: S.arxml\n",
    "## Component import report\\: S.arxml\n",
  ]) {
    assert.equal(tabReportMarkdown(other), other);
  }
});

test("the Trace Apply Report tab names its title once: the page's heading, the report's cut to the search", () => {
  const markdown =
    "# Trace Apply Report — search 'trace'\n\nApplied 2026-11-09 13:06 (UTC+01:00).\n\n## Result\n";
  const shown = importReportMarkdown({ cliText: "Trace apply: ...\n", notes: [], markdown });
  assert.equal(APPLY_REPORT_TITLE, "Trace Apply Report");
  assert.ok(shown.startsWith("# search 'trace'\n"));
  assert.equal(shown.includes(APPLY_REPORT_TITLE), false);
});

test("the Write step's page carries no question and no re-run line, in either import", () => {
  for (const [title, outcome] of [
    [
      COMPONENT_IMPORT_REPORT_TITLE,
      componentDryRunOutcome(run(0, DRY_RUN_REPORT, ""), "/s", false),
    ],
    [
      IMPORT_REPORT_TITLE,
      importDryRunOutcome(run(0, "Summary: 1 added\n", ""), "/s", IMPORT_RUN_WORDING),
    ],
  ] as const) {
    assert.equal(outcome.kind, "report");
    assert.deepEqual(outcome.report.notes, []);
    const html = importTabHtml(
      { title, run: "Dry run", file: SOURCE, report: outcome.report, buttons: applyButtons(false) },
      { nonce: NONCE, rendering: PLAIN, token: TOKEN },
    );
    assert.ok(html.includes('<span id="status" class="wait" role="status"></span>'));
    assert.doesNotMatch(html, /To apply this import later|SpeedSensor\.arxml:|\?<\/span>/);
  }
});

// --- The tab's controller and the two flows, driven by scripted fakes ------------
//
// What the host in commands.ts hands in is faked: the webview (a panel that
// keeps its pages and posts what a click would post), the dialog, the runs,
// the renderer, the outcome record and Check Model. The flows themselves are
// the code the commands run.

/** A fake webview tab: its pages, in order, and the page's own click. */
class FakePanel {
  pages: string[] = [];
  reveals = 0;
  disposed = false;
  constructor(
    readonly title: string,
    private readonly events: { message: (message: unknown) => void; disposed: () => void },
  ) {}
  setHtml(html: string): void {
    assert.equal(this.disposed, false, "no page is set on a closed tab");
    this.pages.push(html);
  }
  reveal(): void {
    this.reveals += 1;
  }
  dispose(): void {
    if (!this.disposed) {
      this.disposed = true;
      this.events.disposed();
    }
  }
  get page(): string {
    return this.pages[this.pages.length - 1] ?? "";
  }
  /** The live buttons' actions on the page now; none on a busy page. */
  get live(): string[] {
    return [...this.page.matchAll(/<button [^>]*data-action="(\w+)">/g)].map((match) => match[1]);
  }
  /** The page's token, from its script; `undefined` on a page with no script. */
  get token(): string | undefined {
    return /const token = "([0-9a-f]{32})"/.exec(this.page)?.[1];
  }
  /** What a click on *action* posts: the page's own token. */
  click(action: ImportAction, token = this.token): void {
    this.events.message({ action, token });
  }
}

const tick = (): Promise<void> => new Promise((resolve) => setImmediate(resolve));

/** Wait until *ready* holds, turn by turn; fails rather than hangs. */
async function until(ready: () => boolean, what: string): Promise<void> {
  for (let turn = 0; turn < 100; turn += 1) {
    if (ready()) {
      return;
    }
    await tick();
  }
  assert.fail(`never: ${what}`);
}

/** A run that settles when the test says so. */
function pending<T>(): { promise: Promise<T>; settle: (value: T) => void } {
  let settle: (value: T) => void = () => {};
  const promise = new Promise<T>((resolve) => {
    settle = resolve;
  });
  return { promise, settle };
}

const REQUIREMENTS_FILE = path.resolve(os.tmpdir(), "needs.json");
const PROJECT_DIR = path.resolve(os.tmpdir(), "project");
const SUMMARY = "Summary: 1 added, 0 changed, 0 removed, 0 stubs created, 0 source updates";

/**
 * One controller with fake panels, and a flow host whose every call is logged
 * in *log*. The requirements import's questions answer by default without a
 * log line — SWCraft's own names, the file's own version kept — and say what
 * they were asked in *asked*, so the flows' logs stay the runs' and records'.
 */
function harness(options: { files?: (string | undefined)[] } = {}) {
  const panels: FakePanel[] = [];
  let tokens = 0;
  const tabs = new ImportTabs({
    openPanel: (title, events) => {
      const panel = new FakePanel(title, events);
      panels.push(panel);
      return panel;
    },
    nonce: () => NONCE,
    token: () => {
      tokens += 1;
      return tokens.toString(16).padStart(32, "0");
    },
  });
  const log: string[] = [];
  const files = options.files ?? [SOURCE];
  const asked: string[] = [];
  const host = (overrides: Partial<RequirementsImportHost> = {}): RequirementsImportHost => ({
    tabs,
    label: "Import Requirements",
    executable: "/opt/swcraft",
    pickFile: async () => {
      log.push("pick");
      return files.length > 1 ? files.shift() : files[0];
    },
    render: async () => PLAIN,
    record: (ran: CapturedRun) => {
      log.push(`record ${ran.exitCode}`);
    },
    checkModel: async () => {
      log.push("checkModel");
    },
    refuse: (message) => {
      log.push(`refuse ${message}`);
    },
    projectDir: PROJECT_DIR,
    rememberedMapping: () => undefined,
    pickMapping: async (items) => {
      asked.push(`mapping ${items.map((item) => item.choice).join(" ")}`);
      return items.find((item) => item.choice === "none");
    },
    pickMappingFile: async () => assert.fail("no mapping dialog"),
    rememberMapping: async (value) => {
      asked.push(`remember ${String(value)}`);
    },
    readFile: async () => JSON.stringify({ current_version: "v1" }),
    askVersion: async (box) => {
      asked.push(`version ${box.value}`);
      return box.value;
    },
    ...overrides,
  });
  /** The open (not disposed) panel of *title*. */
  const open = (title: string): FakePanel | undefined =>
    panels.filter((panel) => panel.title === title && !panel.disposed).pop();
  return { panels, tabs, log, asked, host, open };
}

test("importRequirementsFlow: the dry run's report with Write and Cancel, then the applied run, then Check Model on a click", async () => {
  const { log, host, open, panels } = harness({ files: [REQUIREMENTS_FILE] });
  const runs: boolean[] = [];
  const flow = importRequirementsFlow(host(), async (file, { dryRun }) => {
    assert.equal(file, REQUIREMENTS_FILE);
    runs.push(dryRun);
    log.push(`run ${dryRun ? "dry" : "applied"}`);
    return run(0, `Requirements import: baseline v1\n${SUMMARY}\n`, "");
  });
  await until(() => open(IMPORT_REPORT_TITLE)?.live.length === 2, "Write and Cancel");
  const tab = open(IMPORT_REPORT_TITLE);
  assert.ok(tab !== undefined);
  assert.deepEqual(tab.live, ["apply", "cancel"]);
  assert.ok(tab.page.includes('role="status"></span>'), "nothing stands beside Write");
  tab.click("apply");
  await until(() => tab.live.includes("checkModel"), "Check Model");
  assert.ok(tab.page.includes(`needs.json was imported: ${SUMMARY.slice("Summary: ".length)}.`));
  assert.deepEqual(runs, [true, false]);
  assert.deepEqual(log, ["pick", "run dry", "run applied", "record 0"]);
  tab.click("checkModel");
  await flow;
  assert.deepEqual(log.slice(-1), ["checkModel"]);
  assert.deepEqual(tab.live, [], "the page keeps the report with no button");
  assert.doesNotMatch(tab.page, /<button/);
  assert.equal(panels.length, 1, "one tab for every report of the flow");
});

test("the import flows: an import applied before its report failed ends on the applied line, not the incomplete one", async () => {
  const applied = "Error: the import was applied, but its report could not be rendered (boom).\n";
  const flows = [
    {
      title: IMPORT_REPORT_TITLE,
      start: (host: RequirementsImportHost) =>
        importRequirementsFlow(host, async (_file, { dryRun }) =>
          dryRun ? run(0, `${SUMMARY}\n`, "") : run(1, "", applied),
        ),
    },
    {
      title: COMPONENT_IMPORT_REPORT_TITLE,
      start: (host: ImportFlowHost) =>
        importComponentFlow(host, async (_file, options) =>
          options.dryRun ? run(0, DRY_RUN_REPORT, "") : run(1, "", applied),
        ),
    },
  ];
  for (const { title, start } of flows) {
    const { log, host, open } = harness({ files: [REQUIREMENTS_FILE] });
    const flow = start(host());
    await until(() => open(title)?.live.length === 2, `${title}: Write and Cancel`);
    open(title)?.click("apply");
    await flow;
    const page = open(title)?.page ?? "";
    assert.ok(page.includes(`<p>${IMPORT_REPORT_FAILED_LINE}</p>`), page);
    assert.doesNotMatch(page, /did not complete|<button/);
    assert.deepEqual(log, ["pick", "record 1"], title);
  }
});

test("importRequirementsFlow: a cancelled pick runs nothing; a refused dry run is shown with no button and recorded", async () => {
  const cancelled = harness({ files: [undefined] });
  await importRequirementsFlow(cancelled.host(), async () => assert.fail("no run"));
  assert.deepEqual(cancelled.log, ["pick"]);
  assert.equal(cancelled.panels.length, 0);

  const refused = harness();
  await importRequirementsFlow(refused.host(), async () => run(1, "", "Error: refused\n"));
  const tab = refused.open(IMPORT_REPORT_TITLE);
  assert.ok(tab !== undefined);
  assert.doesNotMatch(tab.page, /<button|<script/);
  assert.ok(tab.page.includes(`<p>${NOTHING_WRITTEN_LINE}</p>`));
  assert.deepEqual(refused.log, ["pick", "record 1"]);
});

test("the import flows: Cancel closes the tab and a closed tab is the same no — nothing more runs or is recorded", async () => {
  for (const close of ["cancel", "closed"] as const) {
    const { log, host, open } = harness();
    let runs = 0;
    const flow = importRequirementsFlow(host(), async () => {
      runs += 1;
      return run(0, `${SUMMARY}\n`, "");
    });
    await until(() => open(IMPORT_REPORT_TITLE)?.live.length === 2, "Write and Cancel");
    const tab = open(IMPORT_REPORT_TITLE);
    assert.ok(tab !== undefined);
    if (close === "cancel") {
      tab.click("cancel");
    } else {
      tab.dispose();
    }
    await flow;
    assert.equal(tab.disposed, true, close);
    assert.equal(runs, 1, `${close}: the dry run alone`);
    assert.deepEqual(log, ["pick"], `${close}: nothing recorded`);
  }
});

test("importComponentFlow: exit 3 offers Show Update with no line beside it; the update dry run, then Update applies with --update", async () => {
  const { log, host, open, panels } = harness();
  const runs: { dryRun: boolean; update: boolean }[] = [];
  const results = [
    run(3, "", DIVERGES),
    run(0, DRY_RUN_REPORT.replace("created", "updated"), ""),
    run(0, "Component import report: SpeedSensor.arxml\nSWC SpeedSensor: updated\n", ""),
  ];
  const flow = importComponentFlow(host(), async (_file, options) => {
    runs.push(options);
    const next = results.shift();
    assert.ok(next !== undefined);
    return next;
  });
  await until(() => open(COMPONENT_IMPORT_REPORT_TITLE)?.live[0] === "showUpdate", "Show Update");
  const tab = open(COMPONENT_IMPORT_REPORT_TITLE);
  assert.ok(tab !== undefined);
  assert.deepEqual(tab.live, ["showUpdate", "cancel"]);
  // The offer is said once, at the end of the report; nothing stands beside the button.
  assert.ok(tab.page.includes(`<p>${COMPONENT_UPDATE_OFFER_LINE}</p>`));
  assert.ok(tab.page.includes('role="status"></span>'));
  assert.equal(tab.page.split("Show Update shows what").length, 2, "the offer is said once");
  tab.click("showUpdate");
  await until(() => tab.live[0] === "apply" && tab.page.includes("Update dry run"), "Update");
  assert.ok(tab.page.includes(">Update</button>"));
  tab.click("apply");
  await until(() => tab.live.includes("checkModel"), "Check Model");
  assert.ok(tab.page.includes("Run <b>Update</b>"));
  assert.ok(tab.page.includes("SpeedSensor.arxml was imported."));
  assert.deepEqual(runs, [
    { dryRun: true, update: false },
    { dryRun: true, update: true },
    { dryRun: false, update: true },
  ]);
  assert.deepEqual(
    log,
    ["pick", "record 0"],
    "a declined or answered offer records nothing of its own",
  );
  tab.dispose();
  await flow;
  assert.equal(panels.length, 1);
});

test("importComponentFlow: Cancel and a closed tab on Show Update run nothing more", async () => {
  for (const close of ["cancel", "closed"] as const) {
    const { log, host, open } = harness();
    let runs = 0;
    const flow = importComponentFlow(host(), async () => {
      runs += 1;
      return run(3, "", DIVERGES);
    });
    await until(() => open(COMPONENT_IMPORT_REPORT_TITLE)?.live.length === 2, "Show Update");
    const tab = open(COMPONENT_IMPORT_REPORT_TITLE);
    assert.ok(tab !== undefined);
    if (close === "cancel") {
      tab.click("cancel");
    } else {
      tab.dispose();
    }
    await flow;
    assert.equal(runs, 1, close);
    assert.deepEqual(log, ["pick"], close);
  }
});

test("the import flows: a tab closed while a run is busy reopens with that run's report", async () => {
  // During the update dry run, and during the applied run.
  const { log, host, open, panels } = harness();
  const update = pending<CapturedRun>();
  const applied = pending<CapturedRun>();
  const results = [Promise.resolve(run(3, "", DIVERGES)), update.promise, applied.promise];
  const flow = importComponentFlow(host(), () => {
    const next = results.shift();
    assert.ok(next !== undefined);
    return next;
  });
  await until(() => open(COMPONENT_IMPORT_REPORT_TITLE)?.live.length === 2, "Show Update");
  const first = open(COMPONENT_IMPORT_REPORT_TITLE);
  assert.ok(first !== undefined);
  first.click("showUpdate");
  await tick();
  assert.deepEqual(first.live, [], "busy: the buttons are disabled");
  assert.ok(first.page.includes(IMPORT_TAB_BUSY_LINE));
  first.dispose();
  update.settle(run(0, DRY_RUN_REPORT, ""));
  await until(() => open(COMPONENT_IMPORT_REPORT_TITLE)?.live[0] === "apply", "a new tab");
  const second = open(COMPONENT_IMPORT_REPORT_TITLE);
  assert.ok(second !== undefined && second !== first);
  second.click("apply");
  await tick();
  second.dispose();
  applied.settle(run(1, "", "Error: aborted\n"));
  await flow;
  const third = open(COMPONENT_IMPORT_REPORT_TITLE);
  assert.ok(third !== undefined && third !== second);
  assert.ok(third.page.includes(`<p>${IMPORT_INCOMPLETE_LINE}</p>`));
  assert.doesNotMatch(third.page, /<button/, "a failed applied run offers nothing");
  assert.deepEqual(log, ["pick", "record 1"]);
  assert.equal(panels.length, 3);
});

test("the import flows: a second start while a run is busy is refused, before its dialog and after it", async () => {
  const { log, host, open, tabs } = harness();
  const applied = pending<CapturedRun>();
  const results = [Promise.resolve(run(0, `${SUMMARY}\n`, "")), applied.promise];
  const first = importRequirementsFlow(host(), () => {
    const next = results.shift();
    assert.ok(next !== undefined);
    return next;
  });
  await until(() => open(IMPORT_REPORT_TITLE)?.live.length === 2, "Write and Cancel");
  assert.equal(tabs.isBusy(IMPORT_REPORT_TITLE), false, "waiting for a choice is not busy");
  // A second start opens its dialog while the first waits; Write is clicked
  // before the dialog is answered, so the first's applied run is busy by then.
  const picked = pending<string | undefined>();
  const second = importRequirementsFlow(
    host({
      pickFile: () => {
        log.push("pick 2");
        return picked.promise;
      },
    }),
    async () => assert.fail("the second flow never runs"),
  );
  await tick();
  open(IMPORT_REPORT_TITLE)?.click("apply");
  await tick();
  assert.equal(tabs.isBusy(IMPORT_REPORT_TITLE), true);
  picked.settle(REQUIREMENTS_FILE);
  await second;
  const refusal = `refuse ${importBusyMessage("Import Requirements")}`;
  assert.deepEqual(log, ["pick", "pick 2", refusal], "refused after its dialog");
  // A third start while the run is still busy is refused before its dialog.
  await importRequirementsFlow(host(), async () => assert.fail("never runs"));
  assert.deepEqual(log.slice(3), [refusal], "refused before its dialog: no pick");
  assert.equal(
    importBusyMessage("Import Requirements"),
    "SWCraft: Import Requirements is already running in this window — wait for it to finish.",
  );
  applied.settle(run(0, `${SUMMARY}\n`, ""));
  await until(() => open(IMPORT_REPORT_TITLE)?.live.includes("checkModel") === true, "the end");
  // Waiting on Check Model is not busy either; after its click the flow has ended
  // before Check Model runs, so a new start is never refused for it.
  open(IMPORT_REPORT_TITLE)?.click("checkModel");
  await first;
  assert.equal(tabs.isBusy(IMPORT_REPORT_TITLE), false);
});

test("the import flows: a new start replaces a flow waiting for a choice; the old page's click runs nothing", async () => {
  const { log, host, open, panels } = harness();
  const runs: string[] = [];
  const firstFlow = importComponentFlow(host(), async (_file, options) => {
    runs.push(`first ${options.dryRun ? "dry" : "applied"}`);
    return run(0, DRY_RUN_REPORT, "");
  });
  await until(() => open(COMPONENT_IMPORT_REPORT_TITLE)?.live.length === 2, "the first's Write");
  const tab = open(COMPONENT_IMPORT_REPORT_TITLE);
  assert.ok(tab !== undefined);
  const staleToken = tab.token;
  const second = pending<CapturedRun>();
  const secondFlow = importComponentFlow(host(), (_file, options) => {
    runs.push(`second ${options.dryRun ? "dry" : "applied"}`);
    return options.dryRun ? second.promise : Promise.resolve(run(1, "", "Error: x\n"));
  });
  await firstFlow;
  // The first flow's choice ended with no action; its page is busy for the second's run.
  assert.deepEqual(tab.live, []);
  assert.ok(tab.page.includes(IMPORT_TAB_BUSY_LINE));
  tab.click("apply", staleToken);
  second.settle(run(0, DRY_RUN_REPORT, ""));
  await until(() => tab.live.length === 2, "the second's Write");
  // The stale token runs nothing; the new page's own does.
  tab.click("apply", staleToken);
  await tick();
  assert.deepEqual(runs, ["first dry", "second dry"]);
  tab.click("apply");
  await secondFlow;
  assert.deepEqual(runs, ["first dry", "second dry", "second applied"]);
  assert.deepEqual(log, ["pick", "pick", "record 1"]);
  assert.equal(panels.length, 1, "the tab of the title is reused");
  assert.ok(tab.reveals >= 2, "a report shown in the open tab reveals it");
});

// --- The requirements import's two questions, driven through the same fakes -------
//
// Between the file and the dry run the flow asks for the mapping (the pick,
// remembered in the workspace folder's setting at the pick) and the delivery's
// version (the box prefilled with the file's own `current_version`). What the
// fakes do not reach is VS Code's settings API and its quick pick and input box
// themselves: commandCore.test.ts pins the adapter's calls into them by shape.

/** A requirements flow whose two runs are recorded with their options; each run a dry-run report. */
function requirementsRuns() {
  const runs: ({ dryRun: boolean } & RequirementsImportAnswers)[] = [];
  const runner = async (file: string, options: { dryRun: boolean } & RequirementsImportAnswers) => {
    assert.equal(file, REQUIREMENTS_FILE);
    runs.push(options);
    return run(0, `Requirements import: baseline v1\n${SUMMARY}\n`, "");
  };
  return { runs, runner };
}

/** Run a requirements flow to its applied run's end: Write clicked, Check Model declined by closing. */
async function writeAndClose(
  flow: Promise<void>,
  open: (title: string) => FakePanel | undefined,
): Promise<void> {
  await until(() => open(IMPORT_REPORT_TITLE)?.live.length === 2, "Write and Cancel");
  open(IMPORT_REPORT_TITLE)?.click("apply");
  await until(() => open(IMPORT_REPORT_TITLE)?.live.includes("checkModel") === true, "the end");
  open(IMPORT_REPORT_TITLE)?.dispose();
  await flow;
}

test("importRequirementsFlow: the pick, the mapping, the version, then the runs; both runs carry the same --mapping= and --baseline=", async () => {
  const mappingFile = path.join(PROJECT_DIR, "mappings", "needs-mapping.yaml");
  const { log, asked, host, open } = harness({ files: [REQUIREMENTS_FILE] });
  const { runs, runner } = requirementsRuns();
  const order: string[] = [];
  const flow = importRequirementsFlow(
    host({
      pickFile: async () => {
        order.push("file");
        log.push("pick");
        return REQUIREMENTS_FILE;
      },
      pickMapping: async (items) => {
        order.push("mapping");
        return items.find((item) => item.choice === "choose");
      },
      pickMappingFile: async () => {
        order.push("mapping file");
        return mappingFile;
      },
      rememberMapping: async (value) => {
        order.push(`remember ${String(value)}`);
      },
      askVersion: async () => {
        order.push("version");
        return "v2.3";
      },
    }),
    async (file, options) => {
      order.push(options.dryRun ? "dry run" : "applied run");
      return runner(file, options);
    },
  );
  await writeAndClose(flow, open);
  assert.deepEqual(order, [
    "file",
    "mapping",
    "mapping file",
    "remember mappings/needs-mapping.yaml",
    "version",
    "dry run",
    "applied run",
  ]);
  assert.deepEqual(runs, [
    { dryRun: true, mapping: mappingFile, baseline: "v2.3" },
    { dryRun: false, mapping: mappingFile, baseline: "v2.3" },
  ]);
  assert.deepEqual(asked, []);
  assert.deepEqual(log, ["pick", "record 0"]);
});

test("askRequirementsMapping: last used, choose another, SWCraft's own names — what each answers and what it writes or clears", async () => {
  const inside = path.join(PROJECT_DIR, "needs-mapping.yaml");
  const outside = path.resolve(os.tmpdir(), "elsewhere", "needs-mapping.yaml");
  const cases: {
    remembered: unknown;
    choice: MappingChoice;
    dialog?: string;
    offered: MappingChoice[];
    mapping: string | undefined;
    written: (string | undefined)[];
  }[] = [
    // The remembered value, offered first and used as stored: resolved against
    // the project, never re-written.
    {
      remembered: "maps/needs-mapping.yaml",
      choice: "remembered",
      offered: ["remembered", "choose", "none"],
      mapping: path.join(PROJECT_DIR, "maps", "needs-mapping.yaml"),
      written: [],
    },
    // Choose another: written at the pick, relative inside the project, absolute outside.
    {
      remembered: undefined,
      choice: "choose",
      dialog: inside,
      offered: ["choose", "none"],
      mapping: inside,
      written: ["needs-mapping.yaml"],
    },
    {
      remembered: "old.yaml",
      choice: "choose",
      dialog: outside,
      offered: ["remembered", "choose", "none"],
      mapping: outside,
      written: [outside],
    },
    // SWCraft's own names: no --mapping, and the setting cleared.
    {
      remembered: "old.yaml",
      choice: "none",
      offered: ["remembered", "choose", "none"],
      mapping: undefined,
      written: [undefined],
    },
    // A blank or non-string value is no remembered mapping.
    {
      remembered: "  ",
      choice: "none",
      offered: ["choose", "none"],
      mapping: undefined,
      written: [undefined],
    },
    {
      remembered: 7,
      choice: "none",
      offered: ["choose", "none"],
      mapping: undefined,
      written: [undefined],
    },
  ];
  for (const { remembered, choice, dialog, offered, mapping, written } of cases) {
    const { host } = harness();
    const writes: (string | undefined)[] = [];
    let items: MappingPickItem[] = [];
    const answer = await askRequirementsMapping(
      host({
        rememberedMapping: () => remembered,
        pickMapping: async (offeredItems) => {
          items = offeredItems;
          return offeredItems.find((item) => item.choice === choice);
        },
        pickMappingFile: async () => dialog,
        rememberMapping: async (value) => {
          writes.push(value);
        },
      }),
    );
    const what = `${String(remembered)} / ${choice}`;
    assert.deepEqual(
      items.map((item) => item.choice),
      offered,
      what,
    );
    if (offered[0] === "remembered") {
      assert.equal(items[0].description, "last used", what);
    }
    assert.deepEqual(answer, { mapping }, what);
    assert.deepEqual(writes, written, what);
  }
});

test("importRequirementsFlow: Esc at the mapping pick, in its dialog or at the version box runs nothing, writes nothing and opens no tab", async () => {
  for (const at of ["pick", "dialog", "version"] as const) {
    const { log, host, panels } = harness({ files: [REQUIREMENTS_FILE] });
    const writes: (string | undefined)[] = [];
    let versionAsked = false;
    await importRequirementsFlow(
      host({
        pickMapping: async (items) =>
          at === "pick"
            ? undefined
            : items.find((item) => item.choice === (at === "dialog" ? "choose" : "none")),
        pickMappingFile: async () => undefined,
        rememberMapping: async (value) => {
          writes.push(value);
        },
        askVersion: async () => {
          versionAsked = true;
          return undefined;
        },
      }),
      async () => assert.fail(`${at}: no run`),
    );
    assert.deepEqual(log, ["pick"], `${at}: nothing recorded`);
    assert.equal(panels.length, 0, `${at}: no tab`);
    assert.equal(versionAsked, at === "version", `${at}: the version is asked after the mapping`);
    // Esc at the pick or in its dialog writes nothing; SWCraft's own names were
    // chosen before the version's Esc, so the setting was cleared at that pick.
    assert.deepEqual(writes, at === "version" ? [undefined] : [], at);
  }
});

test("askDeliveryVersion: the box prefilled with the file's current_version, empty and required without one; a kept value is no --baseline", async () => {
  const cases: {
    text: string | undefined;
    typed: string | undefined;
    value: string;
    prompt: string;
    answer: { baseline: string | undefined } | undefined;
  }[] = [
    // The file's own version kept: no --baseline, the CLI records the file's.
    {
      text: JSON.stringify({ current_version: "v2.3", versions: {} }),
      typed: "v2.3",
      value: "v2.3",
      prompt: BASELINE_PROMPT_TEXT.prompt,
      answer: { baseline: undefined },
    },
    // Another value typed over it: passed as typed.
    {
      text: JSON.stringify({ current_version: "v2.3" }),
      typed: "abc1234",
      value: "v2.3",
      prompt: BASELINE_PROMPT_TEXT.prompt,
      answer: { baseline: "abc1234" },
    },
    // A leading byte order mark is skipped, as the CLI's reader skips it.
    {
      text: `﻿${JSON.stringify({ current_version: "v3" })}`,
      typed: "v3",
      value: "v3",
      prompt: BASELINE_PROMPT_TEXT.prompt,
      answer: { baseline: undefined },
    },
    // No version in the file, a blank one, no JSON, or no file read: empty and required.
    {
      text: JSON.stringify({ versions: {} }),
      typed: "v1",
      value: "",
      prompt: BASELINE_PROMPT_TEXT.promptRequired,
      answer: { baseline: "v1" },
    },
    {
      text: JSON.stringify({ current_version: "  " }),
      typed: "v1",
      value: "",
      prompt: BASELINE_PROMPT_TEXT.promptRequired,
      answer: { baseline: "v1" },
    },
    {
      text: "not json",
      typed: "v1",
      value: "",
      prompt: BASELINE_PROMPT_TEXT.promptRequired,
      answer: { baseline: "v1" },
    },
    {
      text: undefined,
      typed: "v1",
      value: "",
      prompt: BASELINE_PROMPT_TEXT.promptRequired,
      answer: { baseline: "v1" },
    },
    // Esc.
    {
      text: JSON.stringify({ current_version: "v2.3" }),
      typed: undefined,
      value: "v2.3",
      prompt: BASELINE_PROMPT_TEXT.prompt,
      answer: undefined,
    },
  ];
  for (const { text, typed, value, prompt, answer } of cases) {
    const { host } = harness();
    const read: string[] = [];
    let box: { value: string; prompt: string; validateInput: (v: string) => string | undefined } = {
      value: "?",
      prompt: "?",
      validateInput: () => undefined,
    };
    const got = await askDeliveryVersion(
      host({
        readFile: async (file) => {
          read.push(file);
          return text;
        },
        askVersion: async (options) => {
          box = options;
          return typed;
        },
      }),
      REQUIREMENTS_FILE,
    );
    const what = `${String(text)} / ${String(typed)}`;
    assert.deepEqual(read, [REQUIREMENTS_FILE], `${what}: the chosen file is read once`);
    assert.equal(box.value, value, what);
    assert.equal(box.prompt, prompt, what);
    assert.equal(box.validateInput(" "), BASELINE_PROMPT_TEXT.required, `${what}: blank refused`);
    assert.equal(box.validateInput("v9"), undefined, what);
    assert.deepEqual(got, answer, what);
  }
});

// --- Trace Apply in the imports' dry-run sequence ---------------------------------------

const PROPOSALS = path.resolve(os.tmpdir(), "proposals.json");
const DIGEST = `sha256:${"ab".repeat(32)}`;
const APPLY_SUMMARY =
  "Summary: 4 written, 1 confirmed, 3 rejected, 1 already done, 1 stale, 2 not written, 4 undecided; 4 files";
const APPLY_DRY_RUN = `Trace apply: proposals of the search 'trace' (dry run)\n${APPLY_SUMMARY}\nPlan: ${DIGEST}\n`;
const APPLY_APPLIED = `Trace apply: proposals of the search 'trace'\n${APPLY_SUMMARY}\nPlan: ${DIGEST}\n`;

/** The harness's tabs and records, with Trace Apply's own host calls logged as well. */
function traceHarness() {
  const base = harness();
  /** Whether the proposals file has unsaved edits in an editor; a test sets it while the preview waits. */
  const buffer = { dirty: false };
  const host: TraceApplyHost = {
    ...base.host(),
    reload: () => {
      base.log.push("reload");
    },
    log: (line) => {
      base.log.push(`channel ${line}`);
    },
    writeRefusal: () => isDirtyRefusal(buffer.dirty),
  };
  return { ...base, traceHost: host, buffer };
}

test("traceApplyFlow: the preview with Write and Cancel, then the applied run bound by --expect-plan, in the same tab", async () => {
  const { log, open, panels, traceHost } = traceHarness();
  const runs: TraceApplyRunOptions[] = [];
  const flow = traceApplyFlow(traceHost, PROPOSALS, async (options) => {
    runs.push(options);
    log.push(`run ${options.dryRun ? "dry" : "applied"}`);
    return options.dryRun
      ? { ...run(0, APPLY_DRY_RUN, ""), reportMarkdown: "# Trace Apply Report — search 'trace'\n" }
      : run(0, APPLY_APPLIED, "");
  });
  await until(() => open(APPLY_REPORT_TITLE)?.live.length === 2, "Write and Cancel");
  const tab = open(APPLY_REPORT_TITLE);
  assert.ok(tab !== undefined);
  assert.deepEqual(tab.live, ["apply", "cancel"]);
  assert.ok(tab.page.includes(">Write</button>") && tab.page.includes(">Cancel</button>"));
  assert.ok(tab.page.includes("Run <b>Dry run</b>"));
  assert.ok(tab.page.includes('role="status"></span>'), "nothing stands beside Write");
  assert.doesNotMatch(tab.page, /To apply later|Show Report/);
  assert.deepEqual(log, ["run dry"], "nothing recorded while the preview waits");
  tab.click("apply");
  assert.equal(await flow, "applied");
  assert.deepEqual(runs, [{ dryRun: true }, { dryRun: false, expectPlan: DIGEST }]);
  // The applied run's argv carries the previewed plan, the CLI's binding.
  assert.equal(
    buildTraceApplyArgv("swcraft.yaml", PROPOSALS, runs[1]).at(-1),
    `--expect-plan=${DIGEST}`,
  );
  assert.equal(panels.length, 1, "the same tab turns into the applied report");
  assert.equal(tab.disposed, false);
  assert.ok(tab.page.includes("Run <b>Apply</b>"));
  assert.doesNotMatch(tab.page, /<button|<script/);
  assert.deepEqual(log, [
    "run dry",
    "run applied",
    "reload",
    "record 0",
    "channel SWCraft: wrote 5 links into 4 files; recorded 3 rejections; marked 1 already done; marked 1 stale; left 2 links to add by hand.",
  ]);
});

test("traceApplyFlow: the preview and the applied report render the CLI's Markdown, its title cut once", async () => {
  const { open, traceHost } = traceHarness();
  const rendered: ImportReport[] = [];
  const host: TraceApplyHost = {
    ...traceHost,
    render: async (report) => {
      rendered.push(report);
      return PLAIN;
    },
  };
  const flow = traceApplyFlow(host, PROPOSALS, async (options) =>
    options.dryRun
      ? {
          ...run(0, APPLY_DRY_RUN, ""),
          reportMarkdown: "# Trace Apply Report — search 'trace'\n\nPreview\n",
        }
      : {
          ...run(0, APPLY_APPLIED, ""),
          reportMarkdown: "# Trace Apply Report — search 'trace'\n\nApplied\n",
        },
  );
  await until(() => open(APPLY_REPORT_TITLE)?.live.length === 2, "Write and Cancel");
  open(APPLY_REPORT_TITLE)?.click("apply");
  assert.equal(await flow, "applied");
  assert.equal(rendered.length, 2, "the preview and the applied report");
  const [preview, applied] = rendered.map((report) => importReportMarkdown(report));
  assert.ok(preview.startsWith("# search 'trace'\n\nPreview"), preview);
  assert.ok(applied.startsWith("# search 'trace'\n\nApplied"), applied);
  for (const markdown of [preview, applied]) {
    assert.doesNotMatch(
      markdown,
      /Trace Apply Report|```/,
      "the CLI's Markdown, never its text fenced",
    );
  }
});

test("traceApplyFlow: an applied run with no readable Summary: line says the plain done line", async () => {
  const { log, open, traceHost } = traceHarness();
  const flow = traceApplyFlow(traceHost, PROPOSALS, async (options) =>
    options.dryRun
      ? run(0, APPLY_DRY_RUN, "")
      : run(0, "Trace apply: proposals of the search 'trace'\n", ""),
  );
  await until(() => open(APPLY_REPORT_TITLE)?.live.length === 2, "Write and Cancel");
  open(APPLY_REPORT_TITLE)?.click("apply");
  assert.equal(await flow, "applied");
  assert.deepEqual(log, ["reload", "record 0", "channel SWCraft: the decisions were applied."]);
});

test("traceApplyFlow: a proposals buffer made dirty while the preview waits refuses Write: no applied run, the refusal said", async () => {
  const { log, open, panels, traceHost, buffer } = traceHarness();
  const flow = traceApplyFlow(traceHost, PROPOSALS, async (options) => {
    log.push(`run ${options.dryRun ? "dry" : "applied"}`);
    return run(0, options.dryRun ? APPLY_DRY_RUN : APPLY_APPLIED, "");
  });
  await until(() => open(APPLY_REPORT_TITLE)?.live.length === 2, "Write and Cancel");
  const tab = open(APPLY_REPORT_TITLE);
  assert.ok(tab !== undefined);
  buffer.dirty = true;
  tab.click("apply");
  assert.equal(await flow, "stopped");
  assert.deepEqual(
    log,
    ["run dry", `refuse ${DIRTY_REFUSAL}`],
    "no applied run, nothing recorded or reloaded",
  );
  assert.equal(panels.length, 1);
  assert.equal(tab.disposed, false, "the preview stays in its tab");
  assert.ok(tab.page.includes("Run <b>Dry run</b>"));
  assert.doesNotMatch(tab.page, /<button[^>]*>Write|<script/, "with no Write to click");
});

test("traceApplyFlow: Cancel and a closed tab write nothing, record nothing and stop", async () => {
  for (const close of ["cancel", "closed"] as const) {
    const { log, open, traceHost } = traceHarness();
    const flow = traceApplyFlow(traceHost, PROPOSALS, async (options) => {
      assert.equal(options.dryRun, true, `${close}: the dry run alone`);
      log.push("run dry");
      return run(0, APPLY_DRY_RUN, "");
    });
    await until(() => open(APPLY_REPORT_TITLE)?.live.length === 2, "Write and Cancel");
    const tab = open(APPLY_REPORT_TITLE);
    assert.ok(tab !== undefined);
    if (close === "cancel") {
      tab.click("cancel");
    } else {
      tab.dispose();
    }
    assert.equal(await flow, "stopped", close);
    assert.equal(tab.disposed, true, close);
    assert.deepEqual(log, ["run dry"], `${close}: nothing recorded, nothing reloaded`);
  }
});

test("traceApplyFlow: No change., a refused and an unreadable dry run show their report with no Write", async () => {
  const cases = [
    {
      stdout: `Trace apply (dry run)\n${APPLY_SUMMARY.replace("4 written", "0 written")}\nNo change.\n`,
      stderr: "",
      exitCode: 0,
      outcome: "applied",
      line: "No change.",
    },
    {
      stdout: "",
      stderr: "Error: refused\n",
      exitCode: 1,
      outcome: "stopped",
      line: NOTHING_WRITTEN_LINE,
    },
    {
      stdout: "Trace apply (dry run)\n",
      stderr: "",
      exitCode: 0,
      outcome: "stopped",
      line: TRACE_APPLY_UNREADABLE_LINE,
    },
  ];
  for (const { stdout, stderr, exitCode, outcome, line } of cases) {
    const { log, open, traceHost } = traceHarness();
    const got = await traceApplyFlow(traceHost, PROPOSALS, async (options) => {
      assert.equal(options.dryRun, true, `${line}: the dry run alone`);
      return run(exitCode, stdout, stderr);
    });
    assert.equal(got, outcome, line);
    const page = open(APPLY_REPORT_TITLE)?.page ?? "";
    assert.ok(page.includes(escapeHtml(line)), `${line}: ${page}`);
    assert.doesNotMatch(page, /<button|<script/, line);
    assert.deepEqual(
      log,
      [`record ${exitCode}`],
      `${line}: recorded as the imports' refused dry run is`,
    );
  }
});

test("traceApplyFlow: a failed applied run ends on Apply's own line, applied-before-the-report included", async () => {
  for (const [stderr, line] of [
    ["Error: the plan changed since the dry run\n", TRACE_APPLY_WORDING.incomplete],
    [
      "Error: the decisions were applied and its report printed, but --report r.md could not be written (boom).\n",
      TRACE_APPLY_WORDING.reportFailed,
    ],
  ]) {
    const { log, open, traceHost } = traceHarness();
    const flow = traceApplyFlow(traceHost, PROPOSALS, async (options) =>
      options.dryRun ? run(0, APPLY_DRY_RUN, "") : run(1, "", stderr),
    );
    await until(() => open(APPLY_REPORT_TITLE)?.live.length === 2, "Write and Cancel");
    open(APPLY_REPORT_TITLE)?.click("apply");
    assert.equal(await flow, "stopped");
    const page = open(APPLY_REPORT_TITLE)?.page ?? "";
    assert.ok(page.includes(`<p>${line}</p>`), page);
    assert.doesNotMatch(page, /The import|<button/);
    assert.deepEqual(
      log,
      ["reload", "record 1"],
      "the proposals reloaded whatever the run came to",
    );
  }
});

test("traceApplyFlow: a second start while an Apply flow is busy is refused", async () => {
  const { log, tabs, traceHost } = traceHarness();
  const ticket = tabs.begin(APPLY_REPORT_TITLE);
  assert.ok(ticket !== undefined);
  assert.equal(
    await traceApplyFlow(traceHost, PROPOSALS, async () => assert.fail("no run")),
    "stopped",
  );
  assert.deepEqual(log, [`refuse ${BUSY_REFUSAL}`]);
  tabs.end(ticket);
});
